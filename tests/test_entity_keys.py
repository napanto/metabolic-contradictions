import json
import random

from chl_rag.entity_normalize import Normalizer
from chl_rag.entity_keys import (
    compatible_key,
    control_audit,
    key_candidates,
    make_pair,
    potential_count,
    retrieve_candidates,
    sample_cross,
)


def claims(n=80):
    normalizer = Normalizer()
    result = []
    for i in range(n):
        result.append(normalizer.normalize({
            "claim_id": str(i), "pmid": str(i // 2), "arm": "drug", "exposure": "canagliflozin",
            "exposure_class": "SGLT2 inhibitors", "outcome": "amputation", "kind": "risk",
            "sign": ["+", "-", "0"][i % 3], "significant": None, "hedged": False,
            "comparator": "placebo", "context": {}, "evidence": [1], "evidence_text": ["example"]}))
    return result


def test_caps_same_pmid_exclusion_and_order_independence():
    rows = claims()
    rows[0]["hedged"] = True
    expected, _ = key_candidates(rows)
    random.Random(99).shuffle(rows)
    actual, stats = key_candidates(rows)
    assert actual == expected
    assert len(actual) == 35
    assert all(p["pmid_a"] != p["pmid_b"] for p in actual)
    assert all("0" not in [p["claim_a"], p["claim_b"]] for p in actual)
    assert sum(p["sign_pattern"] == "opposite" for p in actual) == 30
    assert sum(p["sign_pattern"] == "concordant" for p in actual) == 5
    assert next(iter(stats.values()))["eligible_pair_counts"]["opposite"] > 30


def test_class_bridge_does_not_pair_distinct_molecules():
    n = Normalizer()
    a, b, c = claims(3)
    b = n.normalize({**b, "pmid": "2", "exposure": "dapagliflozin"})
    c = n.normalize({**c, "pmid": "3", "exposure": "SGLT2 inhibitors"})
    assert compatible_key(a, b) is None
    assert compatible_key(a, c) is not None
    pairs, _ = key_candidates([a, b, c])
    assert len(pairs) == 2
    assert all(p["origin"] == "class_bridge" for p in pairs)
    assert all(any(d["field"] == "molecule_class" for d in p["context_differences"]) for p in pairs)


def test_bounded_sampler_handles_huge_duplicate_document_groups():
    # 100M raw products, but sampling operates on document groups and never
    # constructs that product. Only cross-document claim pairs are eligible.
    rows = [{"claim_id": str(i), "pmid": str(i // 5000)} for i in range(10000)]
    selected, possible = sample_cross(rows, rows, 30, "stress", same=True)
    assert possible == 25000000
    assert len(selected) == 30
    assert all(a["pmid"] != b["pmid"] for a, b in selected)
    assert potential_count(rows[:5000], rows[:5000], same=True) == 0


def test_missing_pubtator_is_uncovered_not_a_negative_label():
    a, b = claims(4)[::2]
    pair = make_pair(a, b, compatible_key(a, b), "key")
    assert pair["pubtator_baseline"]["flag"] is None
    assert pair["pubtator_baseline"]["covered"] is False


def test_retrieval_can_propose_distinct_measurement_without_merging(tmp_path):
    n = Normalizer()
    a, b = claims(4)[::2]
    a = n.normalize({**a, "outcome": "HOMA-IR"})
    b = n.normalize({**b, "outcome": "insulin resistance"})
    assert compatible_key(a, b) is None
    assert compatible_key(a, b, retrieval=True) is not None
    class FakeRetriever:
        def search(self, query, k=20):
            return [{"pmid": "0", "rrf_score": .03}, {"pmid": "1", "rrf_score": .02}]
    result, stats = retrieve_candidates([a, b], [], retriever=FakeRetriever(), cache_dir=tmp_path)
    assert len(result) == 1 and stats["added_over_key"] == 1
    assert result[0]["origins"] == ["retrieval"]
    assert any(d["field"] == "outcome_definition" for d in result[0]["context_differences"])
    assert a["outcome_key"] != b["outcome_key"]


def test_irisin_control_audits_primary_studies_when_reference_review_is_out_of_scope(tmp_path):
    n = Normalizer()
    rows = []
    for i, (disease, sign) in enumerate([("T2D", "-"), ("T2D", "+"), ("T1D", "+"), ("T2D", "+")], 1):
        rows.append(n.normalize({"claim_id": str(i), "pmid": str(i), "arm": "biomarker",
                                 "exposure": disease, "outcome": "irisin", "exposure_class": None,
                                 "kind": "level", "sign": sign, "significant": True, "hedged": False,
                                 "comparator": "healthy controls", "context": {"region": "Europe"},
                                 "evidence_text": ["System extraction, not gold."]}))
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text("\n".join(json.dumps({"pmid": str(i), "design_eligible": True,
                                            "publication_types": ["Meta-Analysis" if i == 4 else "Observational Study"]})
                                for i in range(1, 5)) + "\n")
    audit, targeted = control_audit(rows, [], corpus=corpus)
    result = audit["irisin"]
    assert result["pmids_present"] == []
    assert result["primary_study_audit"]["primary_pmids"] == ["1", "2", "3"]
    assert result["uncapped_targeted_pairs"] == 3 and result["targeted_pairs_written"] == 3
    assert len(targeted) == 3
    assert all("4" not in [p["claim_a"], p["claim_b"]] for p in targeted)
    assert any(any(d["field"] == "disease_subtype" for d in p["context_differences"]) for p in targeted)
    assert compatible_key(rows[0], rows[2]) is None
    assert compatible_key(rows[0], rows[2], retrieval=True) is not None
