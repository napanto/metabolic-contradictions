"""Sentence BM25 plus abstract MedCPT, with checkpointed CPU-only embeddings."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import numpy as np

from .io import file_sha, read_jsonl, utcnow, write_json, write_jsonl
from .sentences import sentences


def tokens(text):
    return re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower())


def load_encoder(kind, threads=8):
    import torch
    from transformers import AutoModel, AutoTokenizer

    torch.set_num_threads(threads)
    model_id = f"ncbi/MedCPT-{kind}-Encoder"
    config = next(
        r for r in json.loads(Path("configs/medcpt.json").read_text()) if r["model"] == model_id
    )
    options = {
        "revision": config["revision"],
        "cache_dir": "data/models/hf",
        "local_files_only": True,
        "token": False,
    }
    tokenizer = AutoTokenizer.from_pretrained(model_id, **options)
    model = AutoModel.from_pretrained(model_id, **options).eval().to("cpu")
    return tokenizer, model, config


def encode(texts, tokenizer, model, max_length):
    import torch

    batch = tokenizer(
        texts, padding=True, truncation=True, max_length=max_length, return_tensors="pt"
    )
    with torch.inference_mode():
        # MedCPT uses the unnormalized CLS vector and inner product.
        embeddings = model(**batch).last_hidden_state[:, 0, :].cpu().numpy()
    return np.asarray(embeddings, dtype=np.float32)


def build(corpus="data/derived/corpus.jsonl", batch_size=32, threads=8, limit=None):
    import faiss

    started = time.monotonic()
    output = Path("data/index")
    output.mkdir(parents=True, exist_ok=True)
    articles = list(read_jsonl(corpus))
    if limit:
        articles = articles[:limit]
    source_hash = file_sha(corpus)
    identity = {
        "corpus_sha256": source_hash,
        "count": len(articles),
        "max_length": 512,
        "models": json.loads(Path("configs/medcpt.json").read_text()),
    }
    progress_path = output / "progress.json"
    progress = (
        json.loads(progress_path.read_text())
        if progress_path.exists()
        else {**identity, "completed": 0}
    )
    if any(progress.get(k) != v for k, v in identity.items()):
        raise RuntimeError(
            "Index inputs changed: preserve or remove old data/index before rebuilding"
        )
    completed_rows = progress.get("completed_indices", list(range(progress["completed"])))
    if (
        not isinstance(progress["completed"], int)
        or not 0 <= progress["completed"] <= len(articles)
        or len(completed_rows) != progress["completed"]
        or any(
            not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(articles)
            for i in completed_rows
        )
        or len(set(completed_rows)) != len(completed_rows)
    ):
        raise RuntimeError("Invalid completed indices in embedding checkpoint")
    matrix_path = output / "embeddings.npy"
    if completed_rows and not matrix_path.exists():
        raise RuntimeError(
            "Embedding checkpoint matrix is missing; refusing to skip completed rows"
        )
    if matrix_path.exists():
        previous_matrix = np.load(matrix_path, mmap_mode="r", allow_pickle=False)
        if (
            previous_matrix.ndim != 2
            or previous_matrix.shape[0] != len(articles)
            or previous_matrix.dtype != np.float32
        ):
            raise RuntimeError("Embedding checkpoint matrix has wrong shape or dtype")
        if completed_rows and not np.isfinite(previous_matrix[completed_rows]).all():
            raise RuntimeError("Embedding checkpoint completed rows contain nonfinite values")
    if not (output / "sentences.jsonl").exists():
        write_jsonl(
            output / "sentences.jsonl",
            ({"pmid": a["pmid"], **s} for a in articles for s in sentences(a)),
        )
        write_jsonl(
            output / "articles.jsonl",
            ({"pmid": a["pmid"], "title": a["title"], "arms": a["arms"]} for a in articles),
        )
    tokenizer, model, config = load_encoder("Article", threads)
    if matrix_path.exists() and previous_matrix.shape[1] != model.config.hidden_size:
        raise RuntimeError("Embedding checkpoint matrix has wrong encoder dimension")
    matrix = np.lib.format.open_memmap(
        output / "embeddings.npy",
        mode="r+" if (output / "embeddings.npy").exists() else "w+",
        dtype=np.float32,
        shape=(len(articles), model.config.hidden_size),
    )
    initial = progress["completed"]
    elapsed_before = progress.get(
        "elapsed_total_seconds", progress.get("elapsed_session_seconds", 0)
    )
    completed = set(completed_rows)
    lengths_path = output / "token_lengths.npy"
    lengths_meta = output / "token_lengths_meta.json"
    if (
        lengths_path.exists()
        and lengths_meta.exists()
        and json.loads(lengths_meta.read_text()) == identity
    ):
        lengths = np.load(lengths_path, allow_pickle=False)
    else:
        lengths = []
        for start in range(0, len(articles), 512):
            tokenized = tokenizer(
                [[a["title"], a["abstract"]] for a in articles[start : start + 512]],
                truncation=True,
                max_length=512,
                return_length=True,
            )
            lengths.extend(tokenized["length"])
        lengths = np.asarray(lengths, dtype=np.int16)
        np.save(lengths_path, lengths)
        write_json(lengths_meta, identity)
    pending = sorted(
        (i for i in range(len(articles)) if i not in completed), key=lambda i: (int(lengths[i]), i)
    )
    for start in range(0, len(pending), batch_size):
        indices = pending[start : start + batch_size]
        batch = [articles[i] for i in indices]
        # Article input is a tokenizer pair [title, abstract], per NCBI reference.
        matrix[indices] = encode(
            [[a["title"], a["abstract"]] for a in batch], tokenizer, model, 512
        )
        matrix.flush()
        completed.update(indices)
        progress["completed_indices"] = sorted(completed)
        progress["completed"] = len(completed)
        progress["updated_at"] = utcnow()
        progress["elapsed_session_seconds"] = time.monotonic() - started
        progress["elapsed_total_seconds"] = elapsed_before + progress["elapsed_session_seconds"]
        progress["batching"] = "length buckets, original matrix row order preserved"
        write_json(progress_path, progress)
        if start == 0 or (start // batch_size) % 25 == 0:
            rate = (progress["completed"] - initial) / (time.monotonic() - started)
            print(
                json.dumps(
                    {
                        "embedded": progress["completed"],
                        "total": len(articles),
                        "abstracts_per_second": round(rate, 2),
                        "eta_hours": round(
                            (len(articles) - progress["completed"]) / max(rate, 0.001) / 3600, 2
                        ),
                    }
                ),
                flush=True,
            )
    index = faiss.IndexFlatIP(matrix.shape[1])
    index.add(np.asarray(matrix))
    faiss.write_index(index, str(output / "medcpt.faiss"))
    write_json(
        "results/index.json",
        {
            **identity,
            "completed_at": utcnow(),
            "device": "cpu",
            "batch_size": batch_size,
            "threads": threads,
            "elapsed_session_seconds": time.monotonic() - started,
            "elapsed_total_seconds": elapsed_before + time.monotonic() - started,
            "batching": "length buckets, original matrix row order preserved",
            "at_token_limit": int(np.sum(lengths == 512)),
            "embedding_level": "abstract",
            "lexical_level": "sentence",
            "rrf_k": 60,
            "article_encoder": config,
            "index_sha256": file_sha(output / "medcpt.faiss"),
        },
    )


class Retriever:
    def __init__(self):
        import faiss

        from .sparse_bm25 import SparseBM25

        self.articles = list(read_jsonl("data/index/articles.jsonl"))
        self.sentences = list(read_jsonl("data/index/sentences.jsonl"))
        self.bm25 = SparseBM25.load_or_build(tokenizer=tokens)
        self.index = faiss.read_index("data/index/medcpt.faiss")
        self.tokenizer, self.model, _ = load_encoder("Query")

    def search(self, query, k=20, exclude_pmid=None):
        sentence_top, _ = self.bm25.top_k(tokens(query), max(100, k * 10), positive_only=True)
        lexical = list(dict.fromkeys(self.sentences[i]["pmid"] for i in sentence_top))
        embedding = encode([query], self.tokenizer, self.model, 64)
        _, ids = self.index.search(embedding, min(max(100, k * 5), self.index.ntotal))
        dense = [self.articles[i]["pmid"] for i in ids[0] if i >= 0]
        combined = {}
        for ranking in [lexical, dense]:
            for rank, pmid in enumerate(ranking, 1):
                if pmid != exclude_pmid:
                    combined[pmid] = combined.get(pmid, 0) + 1 / (60 + rank)
        return [
            {"pmid": pmid, "rrf_score": score}
            for pmid, score in sorted(combined.items(), key=lambda x: (-x[1], x[0]))[:k]
        ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="data/derived/corpus.jsonl")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    build(args.corpus, args.batch_size, args.threads, args.limit)
