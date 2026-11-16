import itertools
import json

import numpy as np

from chl_rag import claim_pairing as cp


def claim(cid, pmid, sign, exposure="metformin", outcome="hba1c", arm="drug", ctype="between_group",
          ekey=None, okey=None, ckey=None):
    return {"claim_id": cid, "pmid": pmid, "arm": arm, "comparison_type": ctype, "sign": sign,
            "exposure": exposure, "outcome": outcome, "exposure_key": ekey or "DRUG:" + exposure,
            "outcome_key": okey or "OUTCOME:" + outcome, "exposure_class_key": ckey}


def brute(claims, ev, ov, t):
    counts = {}
    for a, b in itertools.combinations(claims, 2):
        if a["pmid"] == b["pmid"] or a["arm"] != b["arm"] or a["comparison_type"] != b["comparison_type"]:
            continue
        if min(ev[a["exposure"]] @ ev[b["exposure"]], ov[a["outcome"]] @ ov[b["outcome"]]) < t - 1e-6:
            continue
        p = cp.pattern(a["sign"], b["sign"])
        counts[p] = counts.get(p, 0) + 1
    return counts


def vec(*xs):
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_counts_match_brute_force_with_shared_pmids_and_thresholds():
    ev = {"metformin": vec(1, 0, 0), "met": vec(0.97, 0.243, 0), "insulin": vec(0, 1, 0)}
    ov = {"hba1c": vec(1, 0), "glycated hemoglobin": vec(0.95, 0.312), "weight": vec(0, 1)}
    claims = [
        claim("a1", "1", "+"), claim("a2", "1", "-"), claim("b1", "2", "-"),
        claim("c1", "3", "0", exposure="met"), claim("c2", "3", "+", exposure="met", outcome="glycated hemoglobin"),
        claim("d1", "4", "+", exposure="insulin"), claim("e1", "5", "-", outcome="weight"),
        claim("f1", "6", "-", ctype="association"),
    ]
    for t in (0.90, 0.96, 0.99):
        got = cp.count_grid(claims, ev, ov, [t], threads=1)["thresholds"][f"{t:.2f}"]["claim_pairs_by_pattern"]
        assert {k: v for k, v in got.items() if v} == brute(claims, ev, ov, t), t


def test_split_key_existing_and_new():
    ev = {"metformin": vec(1, 0), "metformin hydrochloride": vec(0.99, 0.141)}
    ov = {"hba1c": vec(1, 0)}
    claims = [claim("a", "1", "+"),
              claim("b", "2", "-"),                                   # same keys as a -> key route
              claim("c", "3", "0", exposure="metformin hydrochloride"),  # different key -> new or existing
              claim("d", "4", "-", exposure="metformin hydrochloride")]
    existing = frozenset({tuple(sorted(("a", "c")))})
    out = cp.count_grid(claims, ev, ov, [0.95], threads=1, existing_pairs=existing)["thresholds"]["0.95"]
    assert out["key_compatible"] == {"opposite": 1, "effect_null": 1}  # a-b, and c-d (same text, same key)
    assert out["in_existing_frame_not_key"] == {"effect_null": 1}  # a-c
    # b-c effect_null and a-d opposite: different keys, not in the frame
    assert out["new_not_in_existing_frames"] == {"effect_null": 1, "opposite": 1, "concordant": 1}  # b-d concordant
    assert out["claim_pairs_by_pattern"]["concordant"] == 1      # b-d


def test_strict_key_compatible_class_member():
    a = claim("a", "1", "+", exposure="sglt2 inhibitors", ekey="DRUGCLASS:sglt2")
    b = claim("b", "2", "-", exposure="dapagliflozin", ekey="DRUG:dapagliflozin", ckey="DRUGCLASS:sglt2")
    assert cp.strict_key_compatible(a, b)
    c = claim("c", "3", "-", exposure="empagliflozin", ekey="DRUG:empagliflozin", ckey="DRUGCLASS:sglt2")
    assert not cp.strict_key_compatible(b, c)  # two molecules are never collapsed by class


def test_encode_texts_normalises_and_keeps_order():
    fake = lambda chunk: np.array([[len(s), 1.0] for s in chunk], dtype=np.float32)  # noqa: E731
    out = cp.encode_texts(["abcd", "a", "abc"], encoder=fake, batch_size=2)
    assert np.allclose(np.linalg.norm(out, axis=1), 1)
    assert out[1][0] < out[2][0] < out[0][0]


def test_load_claims_filters(tmp_path):
    rows = [
        {"claim_id": "x", "pmid": 1, "arm": "drug", "comparison_type": "between_group", "sign": "+",
         "hedged": False, "comparison_identifiable": True, "claim": {"exposure": " Metformin ", "outcome": "HbA1c"}},
        {"claim_id": "y", "pmid": 2, "arm": "drug", "comparison_type": "between_group", "sign": None,
         "hedged": False, "comparison_identifiable": True, "claim": {"exposure": "m", "outcome": "h"}},
        {"claim_id": "z", "pmid": 3, "arm": "drug", "comparison_type": "unclear", "sign": "-",
         "hedged": False, "comparison_identifiable": False, "claim": {"exposure": "m", "outcome": "h"}},
        {"claim_id": "w", "pmid": 4, "arm": "drug", "comparison_type": "between_group", "sign": "-",
         "hedged": True, "comparison_identifiable": True, "claim": {"exposure": "m", "outcome": "h"}},
    ]
    path = tmp_path / "c.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    got = cp.load_claims(path)
    assert [c["claim_id"] for c in got] == ["x"] and got[0]["exposure"] == "metformin"


def test_generic_exposures_are_excluded_from_semantic_route():
    ev = {"placebo": vec(1, 0), "metformin": vec(0, 1)}
    ov = {"hba1c": vec(1, 0)}
    claims = [claim("a", "1", "+", exposure="placebo"), claim("b", "2", "-", exposure="placebo"),
              claim("c", "3", "+"), claim("d", "4", "-")]
    out = cp.count_grid(claims, ev, ov, [0.9], threads=1)
    assert out["excluded_generic_exposure_claims"] == 2
    got = out["thresholds"]["0.90"]["claim_pairs_by_pattern"]
    assert {k: v for k, v in got.items() if v} == {"opposite": 1}


def test_new_scores_returned_sorted_descending():
    ev = {"metformin": vec(1, 0), "metformin hcl": vec(0.96, 0.28)}
    ov = {"hba1c": vec(1, 0)}
    claims = [claim("a", "1", "+"), claim("b", "2", "-", exposure="metformin hcl"),
              claim("c", "3", "0", exposure="metformin hcl", okey="OUTCOME:other")]
    out = cp.count_grid(claims, ev, ov, [0.9], threads=1)
    scores = out["new_scores"]
    assert np.allclose(scores["opposite"], [0.96], atol=1e-3)
    assert np.allclose(scores["effect_null"], [1.0, 0.96], atol=1e-3)  # b-c within one unit, then a-c
