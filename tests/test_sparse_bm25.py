import json

import numpy as np
import pytest
from rank_bm25 import BM25Okapi

from chl_rag.sparse_bm25 import SparseBM25


@pytest.mark.parametrize("corpus", [
    ["common common alpha", "common beta", "common common common gamma", "common delta epsilon", "rare alpha", ""],
    ["common common alpha", "common alpha", "common common alpha"],
    ["alpha", "beta", "gamma", "delta", "epsilon"]
])
def test_exact_scores_and_ranking_including_negative_idf(corpus):
    corpus = [text.split() for text in corpus]
    expected = BM25Okapi(corpus)
    actual = SparseBM25.from_tokenized(corpus)
    assert actual.average_idf == expected.average_idf
    for query in [[], ["unknown"], ["common"], ["common", "alpha", "common"], ["beta", "gamma", "delta"], ["rare", "alpha"]]:
        scores = expected.get_scores(query)
        np.testing.assert_allclose(actual.get_scores(query), scores, rtol=1e-14, atol=1e-14)
        # Both implementations evaluated with the documented sentence-index
        # tie-break, avoiding NumPy quicksort's unspecified ordering of ties.
        ranks = np.lexsort((np.arange(len(corpus)), -scores))
        for k in [0, 1, 3, 20]:
            ids, values = actual.top_k(query, k)
            np.testing.assert_array_equal(ids, ranks[:k])
            np.testing.assert_allclose(values, scores[ids], rtol=1e-14, atol=1e-14)
            positive, vals = actual.top_k(query, k, positive_only=True)
            want = ranks[scores[ranks] > 0][:k]
            np.testing.assert_array_equal(positive, want)
            assert np.all(vals > 0)


def test_checkpoint_roundtrip_detects_corruption(tmp_path):
    index = SparseBM25.from_tokenized([["a", "a", "b"], ["c"], ["a", "d"]])
    prefix = tmp_path / "bm25"
    index.save(prefix, source_sha256="fixture")
    restored = SparseBM25.load(prefix)
    np.testing.assert_array_equal(index.get_scores(["a", "b"]), restored.get_scores(["a", "b"]))
    vocabulary = tmp_path / "bm25_vocabulary.json"
    vocabulary.write_text('["tampered"]')
    with pytest.raises(ValueError, match="checksum"):
        SparseBM25.load(prefix)


def test_changed_source_rebuilds_checkpoint(tmp_path):
    source, prefix = tmp_path / "sentences.jsonl", tmp_path / "bm25"
    source.write_text(json.dumps({"text": "alpha"}) + "\n" + json.dumps({"text": "beta"}) + "\n")
    old = SparseBM25.load_or_build(source, prefix, tokenizer=str.split, tokenizer_identity="split:v1")
    same = SparseBM25.load_or_build(source, prefix, tokenizer=str.split, tokenizer_identity="split:v1")
    assert old.metadata["created_at"] == same.metadata["created_at"]
    with source.open("a") as out:
        out.write(json.dumps({"text": "gamma"}) + "\n")
    new = SparseBM25.load_or_build(source, prefix, tokenizer=str.split, tokenizer_identity="split:v1")
    assert new.corpus_size == 3
    assert new.metadata["source_sha256"] != old.metadata["source_sha256"]


def test_empty_inputs_and_bad_parameters_fail_explicitly():
    with pytest.raises(ValueError, match="empty"):
        SparseBM25.from_tokenized([[], []])
    with pytest.raises(ValueError, match="k1"):
        SparseBM25.from_tokenized([["a"]], k1=0)
