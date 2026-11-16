import json

from chl_rag import pairing_report as pr


def _frame(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows))
    return path


def test_build_placeholders_and_names(tmp_path):
    pair = lambda o, arm, s: {"origins": o, "arm": arm, "sign_pattern": s}  # noqa: E731
    capped = _frame(tmp_path / "c.jsonl", [pair(["entity_key"], "drug", "opposite")])
    uncapped = _frame(tmp_path / "u.jsonl", [pair(["entity_key"], "drug", "opposite"),
                                             pair(["hybrid_retrieval"], "diet", "concordant")])
    table = {"0.90": {"new_T1": 1, "new_T2": 2, "new_T3": 3, "new_T1T2": 3, "new_all_signs": 6,
                      "ratio_T1T2_vs_11391": 0.0, "ratio_all_vs_42972": 0.0}}
    diag = {"0.90": {"key_route_T1T2": 5, "recovered": 4, "stoplist": 0, "class_member": 1, "comparison_type": 0,
                     "fails_E_only": 0, "fails_O_only": 0, "fails_both": 0}}
    enc = {"new_pairs_table": table, "key_route_overlap_by_cause": diag, "grid": {"excluded_generic_exposure_claims": 0,
           "seconds": 1}, "encode_seconds": {"exposure": 60, "outcome": 60}, "encoder_criterion": {}}
    counts = tmp_path / "counts_08E.json"
    counts.write_text(json.dumps({"eligible_claims": 10, "unique_exposure_texts": 2, "unique_outcome_texts": 2,
                                  "encoders": {"medcpt": enc, "sapbert": enc}}))
    lists = tmp_path / "lists.json"
    lists.write_text(json.dumps({"lists": {"0.90": {"steps": {"1_controls_T3": 1, "2a_T1": 1, "2b_T2": 2,
                                                              "3_band_T1T2": 7}, "band": [0.88, 0.9], "pairs": 11,
                                                    "classify_through_step_2": 4}}}))
    m = pr.build(capped=capped, uncapped=uncapped, counts_08e=counts, lists_manifest=lists, out_dir=tmp_path / "out")
    assert m["PairCappedTotal"] == "1" and m["PairUncappedTotal"] == "2" and m["PairUncappedAdded"] == "1"
    assert m["PairTauStar"] == pr.PLACEHOLDER and m["PairClsSemanticValid"] == pr.PLACEHOLDER
    assert not any(ch.isdigit() for name in m for ch in name)
    m = pr.build(capped=capped, uncapped=uncapped, counts_08e=counts, lists_manifest=lists, out_dir=tmp_path / "out",
                 tau_star=0.90, budget=0.01, cost=0.001)
    assert m["PairSemNewTOne"] == "1" and m["PairSemBandSampled"] == "6"
    assert (tmp_path / "out" / "macros.tex").read_text().count("\\renewcommand") == len(m)
