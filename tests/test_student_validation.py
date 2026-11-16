"""Only invented reads and published methodological examples; no study data."""
import copy

import pytest

from chl_rag.student_validation import (
    adapt_site_export,
    consensus_label,
    human_agreement,
    nominal_alpha,
    oracle_agreement,
    student_validation,
)


def read(pair, session, label="concordant", **changes):
    row = {"item_id": pair, "session_pseudonym": session, "label": label,
           "factor": "none", "rationale": "", "time_on_item_seconds": 120,
           "assigned_at_utc": "2026-11-14T10:00:00Z",
           "submitted_at_utc": "2026-11-14T10:02:00Z", "paid": 1,
           "replacement": 0, "surplus": 0, "excluded": 0,
           "quality_failed": 0, "counted": 1}
    return {**row, **changes}


@pytest.mark.parametrize(("labels", "expected"), [
    ([], None), (["concordant"], None),
    (["concordant", "concordant"], "concordant"),
    (["concordant", "contradictory"], None),
    (["contradictory", "not_replicated", "apparent_contradiction"], None),
    (["concordant", "contradictory", "not_replicated", "concordant"], "concordant"),
    (["concordant", "concordant", "contradictory", "contradictory"], None),
])
def test_consensus_requires_unique_category_leader_with_two_votes(labels, expected):
    assert consensus_label(labels) == expected


def test_alpha_matches_primary_source_binary_worked_example():
    # Krippendorff (2011, updated 2013), pages 2–3, Meg and Owen.
    meg = [0, 1, 0, 0, 0, 0, 0, 0, 1, 0]
    owen = [1, 1, 1, 0, 0, 1, 0, 0, 0, 0]
    result = nominal_alpha(zip(meg, owen))
    assert result["observed_disagreement"] == pytest.approx(.4)
    assert result["expected_disagreement"] == pytest.approx(168 / 380)
    assert result["alpha"] == pytest.approx(.09523809523809523)


def test_alpha_matches_primary_source_missing_variable_rater_example():
    # Source section C, pages 4–5. Last item is a singleton excluded from marginals.
    raters = [
        [1, 2, 3, 3, 2, 1, 4, 1, 2, None, None, None],
        [1, 2, 3, 3, 2, 2, 4, 1, 2, 5, None, 3],
        [None, 3, 3, 3, 2, 3, 4, 2, 2, 5, 1, None],
        [1, 2, 3, 3, 2, 4, 4, 1, 2, 5, 1, None],
    ]
    result = nominal_alpha(zip(*raters))
    assert result["pairable_values"] == 40
    assert result["pairable_units"] == 11
    assert result["unpairable_units"] == 1
    assert result["observed_disagreement"] == pytest.approx(.2)
    assert result["alpha"] == pytest.approx(1 - .2 / (1 - 344 / 1560))
    raters[1][-1] = "unseen_singleton"
    assert nominal_alpha(zip(*raters)) == result


def test_alpha_handles_no_variation_and_negative_agreement_without_imputation():
    assert nominal_alpha([[None], []])["undefined_reason"] == "no_pairable_units"
    assert nominal_alpha([["a", "a"], ["a"]])["alpha"] is None
    assert nominal_alpha([["a", "b"]])["alpha"] == 0
    assert nominal_alpha([["a", "b"], ["b", "a"]])["alpha"] == pytest.approx(-.5)
    assert nominal_alpha([["a", "a"], ["b", "b"]])["alpha"] == 1
    with pytest.raises(ValueError, match="None"):
        nominal_alpha([[float("nan"), "a"]])


def test_first_two_rule_separates_six_categories_and_binary_agreement():
    labels = {"invented-a": ["contradictory", "not_replicated", "not_replicated"],
              "invented-b": ["concordant", "concordant", "contradictory"],
              "invented-c": ["extraction_error"], "invented-d": []}
    report = human_agreement(labels, replicates=30, seed=18)
    assert report["pairs_with_two_reads"] == 2
    assert report["pairs_with_one_read"] == report["pairs_with_no_reads"] == 1
    assert report["metrics"]["category_raw_agreement"]["estimate"] == .5
    assert report["metrics"]["binary_raw_agreement"]["estimate"] == 1
    assert report["metrics"]["binary_alpha"]["estimate"] == 1
    assert report["metrics"]["binary_alpha"]["bootstrap"]["undefined_resamples"] > 0
    assert report == human_agreement(labels, replicates=30, seed=18)


def test_bootstrap_retains_every_read_in_a_pair_cluster():
    result = oracle_agreement({"invented-a": [
        ("concordant", "concordant"), ("concordant", "contradictory"),
        ("concordant", "not_comparable"), ("concordant", "extraction_error"),
    ]}, replicates=30, seed=1)
    assert result["matched_pairs"] == 1
    assert result["matched_reads"] == 4
    for metric in ("category_raw_agreement", "binary_raw_agreement"):
        assert result["metrics"][metric]["bootstrap"]["degenerate"] is True
    assert result["metrics"]["category_raw_agreement"]["bootstrap"]["ci95"] == [.25, .25]


def test_export_preserves_optional_rationale_original_flags_and_order_without_mutation():
    rows = [read("invented-a", "s1", quality_failed=1), read("invented-a", "s2")]
    del rows[1]["rationale"]
    original = copy.deepcopy(rows)
    result = adapt_site_export(rows, ["invented-a"])
    assert rows == original
    assert result[0] == {**rows[0], "export_row": 1}
    assert result[1]["export_row"] == 2


def test_optional_factor_is_not_invented():
    row = read("invented-a", "s1")
    del row["factor"]
    result = adapt_site_export([row], ["invented-a"])
    assert "factor" not in result[0]
    assert adapt_site_export([read("invented-a", "s1", factor=None)],
                             ["invented-a"])[0]["factor"] is None


def test_production_preview_marker_is_preserved_and_legacy_rows_remain_accepted():
    rows = [read("invented-a", "s1", preview=0), read("invented-a", "s2")]
    result = adapt_site_export(rows, ["invented-a"])
    assert result[0]["preview"] == 0
    assert "preview" not in result[1]


@pytest.mark.parametrize("preview", [1, True, "0", "1", None, 2])
def test_preview_and_invalid_preview_markers_never_enter_analysis(preview):
    with pytest.raises(ValueError, match="[Pp]review"):
        adapt_site_export([read("invented-a", "s1", preview=preview)], ["invented-a"])


@pytest.mark.parametrize("change", [
    {"item_id": "practice-001"}, {"item_id": "foreign-pair"}, {"label": "unresolved"},
    {"factor": "invented"}, {"kind": "synthetic"}, {"name": "Personal data"},
    {"paid": "1"}, {"excluded": 1}, {"time_on_item_seconds": -1},
    {"time_on_item_seconds": float("inf")}, {"time_on_item_seconds": True},
    {"time_on_item_seconds": 86401}, {"paid": 0},
    {"submitted_at_utc": "2026-11-14T09:00:00Z"},
    {"submitted_at_utc": "2026-11-14T10:02:00+01:00"},
    {"submitted_at_utc": "2026-1-14T10:02:00Z"},
])
def test_export_rejects_invalid_foreign_synthetic_or_personal_rows(change):
    with pytest.raises(ValueError):
        adapt_site_export([read("invented-a", "s1", **change)], ["invented-a"])


def test_export_rejects_duplicate_reads_and_inconsistent_snapshot_flags():
    first = read("invented-a", "s1")
    with pytest.raises(ValueError, match="Duplicate"):
        adapt_site_export([first, first], ["invented-a"])
    with pytest.raises(ValueError, match="coherent snapshot"):
        adapt_site_export([first, read("invented-b", "s1", quality_failed=1)],
                          ["invented-a", "invented-b"])


def test_primary_excludes_flagged_reads_and_sensitivity_retains_every_accepted_read():
    rows = [read("invented-a", "flagged", "contradictory", quality_failed=1),
            read("invented-a", "excluded", "contradictory", excluded=1, counted=0),
            read("invented-a", "valid-a", "concordant"),
            read("invented-a", "valid-b", "concordant", replacement=1),
            read("invented-a", "surplus", "contradictory", paid=0, surplus=1, counted=0),
            read("invented-b", "valid-a", "contradictory"),
            read("invented-b", "valid-b", "contradictory")]
    result = student_validation(rows, ["invented-a", "invented-b"],
                                {"invented-a": "concordant", "invented-b": "contradictory"},
                                replicates=20)
    assert result["flagged_sessions"] == result["excluded_sessions"] == 1
    assert result["excluded_or_flagged_sessions"] == 2
    assert result["unpaid_surplus_reads"] == 1
    assert len(result["retained_reads"]) == 7
    primary = result["primary"]
    assert primary["read_count"] == 4
    assert primary["pairs"]["invented-a"]["first_two_read_rows"] == [3, 4]
    assert primary["reliability_rule"]["status"] == "threshold_met"
    sensitivity = result["sensitivity_all_accepted_reads"]
    assert sensitivity["read_count"] == 6
    assert sensitivity["unresolved_pairs"] == 1
    assert sensitivity["pairs"]["invented-a"]["true_flag"] is None
    assert sensitivity["oracle_consensus"]["matched_pairs"] == 1
    assert sensitivity["oracle_individual_reads"]["matched_reads"] == 6
    assert result["demographics"]["status"] == "unavailable_in_documented_answer_export"


def test_unresolved_category_never_becomes_binary_consensus():
    rows = [read("invented-a", "s1", "contradictory"),
            read("invented-a", "s2", "not_replicated"),
            read("invented-a", "s3", "apparent_contradiction")]
    report = student_validation(rows, ["invented-a"], {"invented-a": "contradictory"},
                                replicates=10)["primary"]
    assert report["pairs"]["invented-a"]["true_flag"] is None
    assert report["oracle_consensus"]["matched_pairs"] == 0
    assert report["reliability_rule"]["oracle_unreliable"] is None
    assert report["human_human"]["metrics"]["binary_raw_agreement"]["estimate"] == 1


def test_incomplete_collection_is_not_resolved_and_undefined_kappa_never_passes():
    report = student_validation([read("invented-a", "s1")],
                                ["invented-a", "invented-b"], replicates=10)["primary"]
    assert report["insufficient_read_pairs"] == 2
    assert report["pairs_below_two_reads"] == 2
    assert report["resolved_pairs"] == 0
    assert report["reliability_rule"]["status"] == "not_estimable"
    assert report["reliability_rule"]["oracle_unreliable"] is None
    interval = report["human_human"]["metrics"]["category_alpha"]["bootstrap"]
    assert interval["undefined_resamples"] == 10
    assert interval["ci95"] is None


def test_low_binary_kappa_preserves_amendment04_rule():
    rows = [read("invented-a", "s1", "concordant"), read("invented-a", "s2", "concordant"),
            read("invented-b", "s1", "contradictory"),
            read("invented-b", "s2", "contradictory")]
    result = student_validation(rows, ["invented-a", "invented-b"],
                                {"invented-a": "contradictory", "invented-b": "concordant"},
                                replicates=10)["primary"]
    assert result["reliability_rule"]["oracle_unreliable"] is True
    assert result["reliability_rule"]["status"] == "below_threshold"
    assert len(result["disagreements"]) == 2


def test_first_reads_use_timestamp_then_original_export_order():
    rows = [read("invented-a", "s1", "concordant", submitted_at_utc="2026-11-14T10:04:00Z"),
            read("invented-a", "s2", "contradictory"),
            read("invented-a", "s3", "not_replicated")]
    result = student_validation(rows, ["invented-a"], replicates=10)["primary"]
    assert result["pairs"]["invented-a"]["first_two_read_rows"] == [2, 3]


def test_quality_diagnostic_matches_any_peer_not_peer_majority():
    rows = [read("invented-a", "s1", "contradictory"),
            read("invented-a", "s2", "contradictory"),
            read("invented-a", "s3", "concordant"),
            read("invented-a", "s4", "concordant")]
    report = student_validation(rows, ["invented-a"], replicates=10)
    # Every person has one matching peer, although two of three peers disagree.
    diagnostic = report["quality_aggregate"]["category_matches_per_session"]
    assert diagnostic["n"] == 4
    assert diagnostic["min"] == diagnostic["max"] == 1
    assert report["flagged_sessions"] == 0


def test_no_data_report_is_serializable_without_nan():
    import json

    report = student_validation([], ["invented-a"], replicates=10)
    json.dumps(report, allow_nan=False)
    assert report["primary"]["pairs_below_two_reads"] == 1
