"""Exact BM25Okapi contributions in sparse inverted postings, without GPU.

Implements rank_bm25.BM25Okapi's installed scoring formula, including its epsilon
adjustment when raw IDF is negative. Scores use float64. Only query-term postings
are traversed in Python; top-k uses NumPy selection and explicit deterministic ties.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from array import array
from collections import Counter
from pathlib import Path

import numpy as np
from scipy import sparse

from .io import file_sha, read_jsonl, utcnow, write_json


class SparseBM25:
    VERSION = 1

    def __init__(self, weights, vocabulary, metadata):
        self.weights = weights.tocsc(copy=False)
        self.vocabulary = vocabulary
        self.metadata = metadata
        self.corpus_size = self.weights.shape[0]
        self.avgdl = metadata["avgdl"]
        self.average_idf = metadata["average_idf"]

    @classmethod
    def from_tokenized(cls, corpus, *, k1=1.5, b=0.75, epsilon=0.25):
        if k1 <= 0 or not 0 <= b <= 1 or epsilon < 0:
            raise ValueError("BM25 requires k1>0, 0<=b<=1, and epsilon>=0")
        vocabulary = {}
        indices, frequencies, indptr = array("i"), array("d"), array("q", [0])
        lengths = []
        for document in corpus:
            counts = Counter(document)
            lengths.append(sum(counts.values()))
            for token, count in counts.items():
                if token not in vocabulary:
                    vocabulary[token] = len(vocabulary)
                indices.append(vocabulary[token])
                frequencies.append(count)
            indptr.append(len(indices))
        if not lengths or not vocabulary:
            raise ValueError("Cannot build BM25 from an empty corpus or an empty vocabulary")
        tf = sparse.csr_matrix((np.frombuffer(frequencies, dtype=np.float64),
                                np.frombuffer(indices, dtype=np.int32),
                                np.frombuffer(indptr, dtype=np.int64)),
                               shape=(len(lengths), len(vocabulary))).tocsc()
        df = np.diff(tf.indptr)
        n = len(lengths)
        idf = np.empty(len(vocabulary), dtype=np.float64)
        idf_sum = 0.0
        for i, freq in enumerate(df):
            # math.log and vocabulary encounter order reproduce rank_bm25's
            # average IDF, including when epsilon * average_idf remains negative.
            value = math.log(n - int(freq) + 0.5) - math.log(int(freq) + 0.5)
            idf[i] = value
            idf_sum += value
        average_idf = idf_sum / len(vocabulary)
        idf[idf < 0] = epsilon * average_idf
        avgdl = sum(lengths) / n
        doc_norm = k1 * (1 - b + b * np.asarray(lengths, dtype=np.float64) / avgdl)
        tf.data = np.repeat(idf, df) * (tf.data * (k1 + 1) / (tf.data + doc_norm[tf.indices]))
        metadata = {"version": cls.VERSION, "k1": k1, "b": b, "epsilon": epsilon,
                    "documents": n, "vocabulary_size": len(vocabulary), "postings": int(tf.nnz),
                    "avgdl": avgdl, "average_idf": average_idf, "dtype": "float64",
                    "tie_policy": "descending score, ascending sentence index"}
        return cls(tf, vocabulary, metadata)

    def get_scores(self, query):
        scores = np.zeros(self.corpus_size, dtype=np.float64)
        # Do not multiply by query-term counts: retaining query iteration order
        # also preserves rank_bm25's floating-point addition behavior.
        for token in query:
            term = self.vocabulary.get(token)
            if term is None:
                continue
            start, end = self.weights.indptr[term:term + 2]
            docs = self.weights.indices[start:end]
            scores[docs] += self.weights.data[start:end]
        return scores

    def top_k(self, query, k=100, *, positive_only=False):
        if k < 0:
            raise ValueError("k must be nonnegative")
        if not k:
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
        scores = self.get_scores(query)
        candidates = np.flatnonzero(scores > 0) if positive_only else np.arange(self.corpus_size)
        if len(candidates) > k:
            cutoff = np.partition(scores[candidates], len(candidates) - k)[len(candidates) - k]
            above = candidates[scores[candidates] > cutoff]
            # candidates are ordered by index, so ties at the boundary are stable.
            tied = candidates[scores[candidates] == cutoff][:k - len(above)]
            candidates = np.concatenate([above, tied])
        order = np.lexsort((candidates, -scores[candidates]))
        selected = candidates[order]
        return selected, scores[selected]

    @staticmethod
    def _paths(prefix):
        prefix = Path(prefix)
        return (prefix.with_name(prefix.name + "_weights.npz"),
                prefix.with_name(prefix.name + "_vocabulary.json"),
                prefix.with_name(prefix.name + "_metadata.json"))

    def save(self, prefix, **provenance):
        matrix_path, vocabulary_path, metadata_path = self._paths(prefix)
        matrix_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = matrix_path.with_name(matrix_path.stem + ".tmp.npz")
        sparse.save_npz(tmp, self.weights, compressed=True)
        tmp.replace(matrix_path)
        ordered = sorted(self.vocabulary, key=self.vocabulary.get)
        write_json(vocabulary_path, ordered)
        metadata = {**self.metadata, **provenance, "created_at": utcnow(),
                    "weights_sha256": file_sha(matrix_path),
                    "vocabulary_sha256": file_sha(vocabulary_path)}
        # Commit marker written last: partial builds are never accepted as valid.
        write_json(metadata_path, metadata)
        self.metadata = metadata

    @classmethod
    def load(cls, prefix):
        matrix_path, vocabulary_path, metadata_path = cls._paths(prefix)
        metadata = json.loads(metadata_path.read_text())
        if metadata["version"] != cls.VERSION:
            raise ValueError("BM25 checkpoint version mismatch")
        for path, key in [(matrix_path, "weights_sha256"), (vocabulary_path, "vocabulary_sha256")]:
            if file_sha(path) != metadata[key]:
                raise ValueError(f"BM25 checkpoint checksum mismatch: {path}")
        vocabulary = {token: i for i, token in enumerate(json.loads(vocabulary_path.read_text()))}
        weights = sparse.load_npz(matrix_path)
        if weights.shape != (metadata["documents"], len(vocabulary)):
            raise ValueError("BM25 checkpoint shape mismatch")
        return cls(weights, vocabulary, metadata)

    @classmethod
    def load_or_build(cls, source="data/index/sentences.jsonl", prefix="data/index/bm25", *,
                      tokenizer, tokenizer_identity="chl_rag.index.tokens:v1", k1=1.5, b=0.75, epsilon=0.25):
        identity = {"source_sha256": file_sha(source), "tokenizer": tokenizer_identity,
                    "k1": k1, "b": b, "epsilon": epsilon}
        paths = cls._paths(prefix)
        if all(p.exists() for p in paths):
            metadata = json.loads(paths[-1].read_text())
            if all(metadata.get(key) == value for key, value in identity.items()) and metadata.get("version") == cls.VERSION:
                return cls.load(prefix)
        started = time.monotonic()
        model = cls.from_tokenized((tokenizer(s["text"]) for s in read_jsonl(source)), k1=k1, b=b, epsilon=epsilon)
        model.save(prefix, **identity, build_seconds=time.monotonic() - started)
        return model


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="data/index/sentences.jsonl")
    parser.add_argument("--prefix", default="data/index/bm25")
    args = parser.parse_args()
    from .index import tokens
    index = SparseBM25.load_or_build(args.source, args.prefix, tokenizer=tokens)
    print(json.dumps(index.metadata), flush=True)
