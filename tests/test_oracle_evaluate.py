"""Amendment 04 analysis tests: all evidence and annotation judgments are synthetic."""
from copy import deepcopy

import pytest

from chl_rag.annotation import FIELDS
from chl_rag.io import digest, write_json, write_jsonl
from chl_rag.oracle_evaluate import (
    ORACLE_NAME,
    SECONDARY_NAME,
    abstract_interval,
    agreement_interval,
    agreement_report,
    bounded_rate,
    design_weighted_precision,
    load_labels,
    reference_metrics,
    reliability_decision,
    run,
    validate_design,
)


def item(index, *, arm="drug", claims=0):
    if claims:
        return {"item_id": f"extraction-{index:03d}", "task": "extraction", "arm": arm,
                "claims": [{"claim_id": f"synthetic-{index}-{n}"} for n in range(claims)]}
    return {"item_id": f"pair-{index:03d}", "task": "pair", "arm": arm}


def label(public, package_hash, *, role="oracle", value="contradictory", revision=1):
    result = {"item_id": public["item_id"], "task": public["task"],
              "annotator": {"oracle": ORACLE_NAME, "human": "Antonio",
                            "secondary": SECONDARY_NAME}[role],
              "package_sha256": package_hash, "revision": revision}
    if role != "human":
        result["annotator_type"] = "LLM"
    if role == "oracle":
        result.update(model="gpt-6-astra", reasoning_effort="max")
    if public["task"] == "pair":
        result.update(label=value, factor="unknown", rationale="Synthetic fixture; no real evidence.")
    else:
        result["claims"] = [{"claim_id": claim["claim_id"],
                             "fields": {field: "correct" for field in FIELDS},
                             "evidence_support": "supported"} for claim in public["claims"]]
    return result


def design_rows(public, *, frame_counts=None):
    frame_counts = frame_counts or {"drug/contradictory": 100}
    counts = {stratum: sum(row["task"] == "pair" and row["arm"] == stratum.split("/")[0]
                           for row in public) for stratum in frame_counts}
    frames = {key: {"arm": key.split("/")[0], "predicted_class": key.split("/")[1],
                    "frame_count": n, "selected": counts[key]}
              for key, n in frame_counts.items()}
    mapping = []
    for row in public:
        meta = {key: row[key] for key in ("item_id", "task", "arm")}
        if row["task"] == "pair":
            key = f"{row['arm']}/contradictory"
            meta.update(pair_id="source-" + row["item_id"], label="contradictory",
                        inclusion_probability=counts[key] / frame_counts[key])
        else:
            meta["pmid"] = "synthetic-" + row["item_id"]
        mapping.append(meta)
    extraction_frames = {arm: {"selected": sum(row["task"] == "extraction" and row["arm"] == arm
                                              for row in public), "frame_count": 100}
                         for arm in {row["arm"] for row in public if row["task"] == "extraction"}}
    return {"blind_items_sha256": digest(public), "items": mapping, "pair_frames": frames,
            "extraction_frames": extraction_frames}


def subset_manifest(public, selected, original):
    selected_ids = {row["item_id"] for row in selected}
    metadata = [deepcopy(row) for row in original["items"] if row["item_id"] in selected_ids]
    frames = deepcopy(original["pair_frames"])
    for key, frame in frames.items():
        frame["original_selected"] = frame["selected"]
        frame["selected"] = sum(row["task"] == "pair"
                                and f"{row['arm']}/{row['label']}" == key for row in metadata)
    for meta in metadata:
        if meta["task"] == "pair":
            frame = frames[f"{meta['arm']}/{meta['label']}"]
            first = meta["inclusion_probability"]
            second = frame["selected"] / frame["original_selected"]
        else:
            frame = original["extraction_frames"][meta["arm"]]
            first = frame["selected"] / frame["frame_count"]
            second = sum(row["task"] == "extraction" and row["arm"] == meta["arm"]
                         for row in selected) / frame["selected"]
        meta.update(first_stage_probability=first, second_stage_probability=second,
                    total_inclusion_probability=first * second)
    return {"blind_items_sha256": digest(selected), "parent_package_sha256": digest(public),
            "items": metadata, "pair_frames": frames}


def test_original_and_two_stage_weights_are_population_weights():
    public = [item(1, arm="biomarker"), item(2, arm="biomarker"), item(3), item(4)]
    original = design_rows(public, frame_counts={"biomarker/contradictory": 90,
                                               "drug/contradictory": 10})
    selected = [public[0], public[2]]
    human_manifest = subset_manifest(public, selected, original)
    labels = {row["item_id"]: label(row, digest(public), value="contradictory"
                                   if row["arm"] == "biomarker" else "concordant") for row in public}
    mapping = {row["item_id"]: row for row in original["items"]}
    weighted = design_weighted_precision(labels, mapping, original["pair_frames"], replicates=100)
    assert weighted["estimate"] == pytest.approx(.9)
    assert weighted["planned_weight"] == 100
    assert weighted["bootstrap"]["ci95"] == pytest.approx([.9, .9])
    assert weighted["bootstrap"]["degenerate"] is True
    human_map = {row["item_id"]: row for row in human_manifest["items"]}
    human = design_weighted_precision(labels, human_map, human_manifest["pair_frames"],
                                     two_stage=True, replicates=100)
    assert human["estimate"] == pytest.approx(.9)
    assert human["planned_weight"] == 100
    assert "second_stage_probability" in human["weighting"]


def test_partial_validity_keeps_original_weights_and_gives_missing_bounds():
    public = [item(n) for n in range(1, 5)]
    design = design_rows(public)
    mapping = {row["item_id"]: row for row in design["items"]}
    labels = {row["item_id"]: label(row, digest(public), value=value)
              for row, value in zip(public, ["contradictory", "concordant"])}
    weighted = design_weighted_precision(labels, mapping, design["pair_frames"], replicates=100)
    assert weighted["status"] == "partial_valid_references"
    assert weighted["estimate"] == .5
    assert weighted["weighted_coverage"] == .5
    assert weighted["missing_reference_bounds"] == [.25, .75]
    assert bounded_rate(1, 2, 4)["missing_reference_bounds"] == [.25, .75]


def test_wholly_missing_stratum_has_no_weighted_point_but_retains_bounds():
    public = [item(1, arm="biomarker"), item(2)]
    design = design_rows(public, frame_counts={"biomarker/contradictory": 90,
                                             "drug/contradictory": 10})
    mapping = {row["item_id"]: row for row in design["items"]}
    labels = {public[0]["item_id"]: label(public[0], digest(public))}
    result = design_weighted_precision(labels, mapping, design["pair_frames"], replicates=100)
    assert result["status"] == "not_estimable"
    assert result["estimate"] is None
    assert result["missing_reference_bounds"] == [.9, 1]
    assert result["empty_reference_strata"] == ["drug/contradictory"]
    assert result["bootstrap"]["ci95"] is None
    assert result["bootstrap"]["undefined_resamples"] == 100


def test_zero_references_are_not_zero_accuracy():
    result = bounded_rate(0, 0, 8)
    assert result["estimate"] is None
    assert result["wilson95"] is None
    assert result["missing_reference_bounds"] == [0, 1]
    assert bounded_rate(0, 0, 0)["missing_reference_bounds"] is None


def test_bootstrap_keeps_abstracts_intact_and_stratifies_by_arm():
    public = [item(1, arm="biomarker", claims=9), item(2, claims=1)]
    labels = [label(row, digest(public)) for row in public]
    labels[1]["claims"][0]["fields"]["sign"] = "incorrect"
    items = {row["item_id"]: row for row in public}
    result = abstract_interval(labels, items, lambda claim: claim["fields"]["sign"] == "correct",
                               replicates=100)
    # One cluster in each arm: a claim-level or nonstratified bootstrap would vary.
    assert result["ci95"] == [.9, .9]
    assert result["degenerate"] is True
    assert result["clusters_by_arm"] == {"biomarker": 1, "drug": 1}
    assert result["n_clusters"] == 2


def test_uncertainty_is_incorrect_and_missing_claims_bound_full_denominator():
    public = [item(1, claims=2), item(2, claims=3)]
    first = label(public[0], digest(public))
    first["claims"][0]["fields"]["sign"] = "uncertain"
    first["claims"][0]["evidence_support"] = "uncertain"
    design = design_rows(public, frame_counts={})
    result = reference_metrics({first["item_id"]: first},
                               {row["item_id"]: row for row in public},
                               {row["item_id"]: row for row in design["items"]}, {}, replicates=50)
    extraction = result["extraction"]["overall"]
    sign = extraction["fields"]["sign"]
    assert sign["estimate"] == .5
    assert sign["n"] == 2
    assert sign["expected"] == 5
    assert sign["missing_reference_bounds"] == [.2, .8]
    assert sign["counts"] == {"uncertain": 1, "correct": 1}
    assert extraction["evidence_support"]["estimate"] == .5


@pytest.mark.parametrize("kappa,expected,primary", [
    (.399, "oracle_unreliable", "human_subset"),
    (.4, "threshold_met", "oracle"),
    (.8, "threshold_met", "oracle"),
    (None, "inconclusive_undefined_kappa", "oracle_provisional"),
])
def test_reliability_rule_uses_point_estimate_and_never_passes_undefined(kappa, expected, primary):
    result = reliability_decision({"kappa": kappa, "kappa_interval": {"ci95": [0, 1]}},
                                  human_complete=True, oracle_complete=True)
    assert result["status"] == expected
    assert result["primary_estimates"] == primary
    if kappa is None:
        assert result["oracle_reliable"] is None


def test_partial_human_validation_remains_pending():
    result = reliability_decision({"kappa": 1}, human_complete=False, oracle_complete=True)
    assert result["status"] == "pending_human_validation"
    assert result["oracle_reliable"] is None


def test_bootstrap_reports_undefined_kappas_and_stratum_degeneracy():
    all_agree = agreement_interval({"drug/contradictory": [[("yes", "yes")]]},
                                   method="synthetic", replicates=100)
    assert all_agree["agreement"] == 1
    assert all_agree["kappa"] is None
    assert all_agree["kappa_interval"]["undefined_resamples"] == 100
    assert all_agree["kappa_interval"]["ci95"] is None
    assert all_agree["raw_agreement_interval"]["degenerate"] is True
    mixed = agreement_interval({"drug/contradictory": [[("yes", "yes")], [("no", "no")]]},
                               method="synthetic", replicates=100)
    assert mixed["kappa"] == 1
    assert 0 < mixed["kappa_interval"]["undefined_resamples"] < 100
    assert mixed["kappa_interval"]["ci95"] == [1, 1]
    stratified = agreement_interval({"a": [[("yes", "yes")]], "b": [[("no", "no")]]},
                                    method="synthetic", replicates=100)
    assert stratified["kappa_interval"]["undefined_resamples"] == 0


def test_agreement_aligns_claim_ids_and_lists_disagreements_without_adjudication():
    public = [item(1), item(2, claims=2)]
    items = {row["item_id"]: row for row in public}
    design = design_rows(public)
    mapping = {row["item_id"]: row for row in design["items"]}
    first = {row["item_id"]: label(row, digest(public)) for row in public}
    second = deepcopy(first)
    second["pair-001"]["label"] = "concordant"
    second["extraction-002"]["claims"].reverse()
    second["extraction-002"]["claims"][0]["fields"]["sign"] = "incorrect"
    second["extraction-002"]["claims"][0]["evidence_support"] = "unsupported"
    result = agreement_report(first, second, items, mapping, replicates=100)
    assert result["matched_items"] == 2
    assert result["binary_true_flag"]["agreement"] == 0
    assert result["extraction_fields"]["sign"]["agreement"] == .5
    assert result["extraction_fields"]["exposure"]["agreement"] == 1
    assert result["extraction_evidence_support"]["agreement"] == .5
    assert result["disagreements"]["extraction"][0]["claim_id"] == "synthetic-2-1"
    assert result["disagreement_summary"]["binary_true_flag"] == 1
    assert result["adjudication"].startswith("None")
    missing = agreement_report(first, {}, items, mapping, replicates=50)
    assert missing["matched_items"] == 0
    assert missing["categories"]["agreement"] is None
    assert missing["binary_true_flag"]["kappa"] is None
    assert missing["missing_match_ids"] == ["extraction-002", "pair-001"]


@pytest.mark.parametrize("role,change", [
    ("human", {"annotator_type": "LLM"}),
    ("human", {"annotator": ORACLE_NAME}),
    ("oracle", {"annotator": "Antonio"}),
    ("oracle", {"annotator_type": "human"}),
    ("secondary", {"annotator": ORACLE_NAME}),
    ("oracle", {"package_sha256": "wrong"}),
    ("human", {"package_sha256": "wrong"}),
    ("oracle", {"model": "some-other-model"}),
    ("oracle", {"reasoning_effort": "xhigh"}),
])
def test_reference_identity_binding_and_effort_are_strict(tmp_path, role, change):
    public = [item(1)]
    row = {**label(public[0], digest(public), role=role), **change}
    path = tmp_path / "synthetic_labels.jsonl"
    write_jsonl(path, [row])
    with pytest.raises(ValueError):
        load_labels(path, {row["item_id"]: public[0]}, role=role, effort="max")


def test_invalid_oracle_judgment_becomes_missing_without_label_invention(tmp_path):
    public = [item(1)]
    row = label(public[0], digest(public), value="INVALID")
    path = tmp_path / "synthetic_labels.jsonl"
    write_jsonl(path, [row])
    labels, invalid = load_labels(path, {row["item_id"]: public[0]}, role="oracle", effort="max")
    assert labels == {}
    assert invalid == {"pair-001": "schema_invalid"}


def test_revisions_validate_all_rows_and_latest_human_only(tmp_path):
    public = [item(1)]
    first = label(public[0], digest(public), role="human")
    second = label(public[0], digest(public), role="human", value="concordant", revision=2)
    path = tmp_path / "synthetic_labels.jsonl"
    write_jsonl(path, [first, second])
    rows, invalid = load_labels(path, {public[0]["item_id"]: public[0]}, role="human")
    assert len(rows) == 1
    assert rows["pair-001"]["label"] == "concordant"
    assert invalid == {}
    write_jsonl(path, [second, first])
    with pytest.raises(ValueError, match="revision"):
        load_labels(path, {public[0]["item_id"]: public[0]}, role="human")


def test_human_design_requires_unchanged_items_and_correct_stage_probabilities():
    public = [item(1), item(2), item(3, claims=1), item(4, claims=2)]
    original = design_rows(public)
    parent, _ = validate_design(public, original)
    selected = [public[0], public[2]]
    subset = subset_manifest(public, selected, original)
    validate_design(selected, subset, parent=parent, parent_manifest=original)
    wrong = deepcopy(subset)
    wrong["items"][0]["total_inclusion_probability"] = .5
    with pytest.raises(ValueError, match="total_inclusion_probability"):
        validate_design(selected, wrong, parent=parent, parent_manifest=original)
    wrong = deepcopy(selected)
    wrong[0]["extra_private_verdict"] = "concordant"
    wrong_manifest = {**subset, "blind_items_sha256": digest(wrong)}
    with pytest.raises(ValueError, match="differs from frozen parent"):
        validate_design(wrong, wrong_manifest, parent=parent, parent_manifest=original)
    wrong_manifest = {**subset, "parent_package_sha256": "wrong"}
    with pytest.raises(ValueError, match="parent package hash"):
        validate_design(selected, wrong_manifest, parent=parent, parent_manifest=original)


def integration_fixture(tmp_path, monkeypatch, *, oracle_present=False, human_present=False):
    monkeypatch.chdir(tmp_path)
    public = [item(1), item(2)]
    original = design_rows(public)
    subset = subset_manifest(public, public, original)
    write_jsonl("package.jsonl", public)
    write_jsonl("human_package.jsonl", public)
    write_json("manifest.json", original)
    write_json("human_manifest.json", subset)
    config = {"amendment": 4, "bootstrap_replicates": 2000, "bootstrap_seed": 20261115,
              "binary_kappa_reliability_threshold": .4, "parent_package_sha256": digest(public),
              "oracle_order": [row["item_id"] for row in public],
              "human_ids": [row["item_id"] for row in public], "human_claims": 0,
              "effort": "max", "fallback_effort": "xhigh", "model": "gpt-6-astra",
              "guidelines_sha256": "synthetic-guidelines-hash"}
    write_json("config.json", config)
    write_json("run_manifest.json", {"model": "gpt-6-astra", "effort": "max",
                                     "package_sha256": digest(public),
                                     "guidelines_sha256": config["guidelines_sha256"]})
    for role, present in (("oracle", oracle_present), ("human", human_present), ("secondary", True)):
        if present:
            write_jsonl(f"{role}.jsonl", [label(row, digest(public), role=role, value=value)
                                         for row, value in zip(public, ["contradictory", "concordant"])])
    return {"oracle": "oracle.jsonl", "human": "human.jsonl", "second": "secondary.jsonl",
            "package": "package.jsonl", "manifest": "manifest.json",
            "human_package": "human_package.jsonl", "human_manifest": "human_manifest.json",
            "run_manifest": "run_manifest.json", "config": "config.json",
            "output": "result.json"}


def test_pending_oracle_does_not_use_secondary_as_reference(tmp_path, monkeypatch):
    kwargs = integration_fixture(tmp_path, monkeypatch)
    result = run(**kwargs)
    assert result["status"] == "pending_oracle_labels"
    assert result["pending_inputs"] == ["oracle_labels", "human_labels"]
    assert result["reference_validity"]["oracle"]["valid"] == 0
    assert result["reference_validity"]["secondary"]["valid"] == 2
    assert result["oracle"]["pairs"]["overall"]["sample_precision"]["estimate"] is None
    assert result["gold_labels_substituted"] is False


def test_complete_oracle_pending_human_is_provisional_and_keeps_legacy_output(tmp_path, monkeypatch):
    kwargs = integration_fixture(tmp_path, monkeypatch, oracle_present=True)
    write_json("results/metabolic_evaluation.json", {"preserved": True})
    original_bytes = (tmp_path / "results/metabolic_evaluation.json").read_bytes()
    result = run(**kwargs)
    assert result["status"] == "pending_human_labels"
    assert result["primary_estimates"] == "oracle_provisional"
    assert result["human_subset"]["pairs"]["overall"]["sample_precision"]["estimate"] is None
    assert result["reliability"]["oracle_reliable"] is None
    assert (tmp_path / "results/metabolic_evaluation.json").read_bytes() == original_bytes


def test_complete_matched_validation_has_prespecified_bootstraps(tmp_path, monkeypatch):
    result = run(**integration_fixture(tmp_path, monkeypatch, oracle_present=True, human_present=True))
    assert result["status"] == "complete"
    assert result["primary_estimates"] == "oracle"
    interval = result["agreement"]["oracle_human"]["binary_true_flag"]["kappa_interval"]
    assert interval["replicates"] == 2000
    assert interval["seed"] == 20261115
    assert interval["undefined_resamples"] > 0
    assert result["agreement"]["oracle_secondary"]["matched_items"] == 2


def test_low_kappa_promotes_human_and_marks_oracle_unreliable(tmp_path, monkeypatch):
    kwargs = integration_fixture(tmp_path, monkeypatch, oracle_present=True, human_present=True)
    public = [item(1), item(2)]
    write_jsonl("human.jsonl", [label(row, digest(public), role="human", value=value)
                               for row, value in zip(public, ["concordant", "contradictory"])])
    result = run(**kwargs)
    assert result["agreement"]["oracle_human"]["binary_true_flag"]["kappa"] == -1
    assert result["primary_estimates"] == "human_subset"
    assert result["oracle"]["interpretation"] == "unreliable"
    assert result["human_subset"]["role"] == "primary"
    assert result["reliability"]["oracle_reliable"] is False


def test_final_invalid_oracle_is_missing_reference_not_a_false_judgment(tmp_path, monkeypatch):
    kwargs = integration_fixture(tmp_path, monkeypatch, oracle_present=True, human_present=True)
    public = [item(1), item(2)]
    write_jsonl("oracle.jsonl", [label(public[0], digest(public)),
                                label(public[1], digest(public), value="INVALID")])
    result = run(**kwargs)
    assert result["status"] == "complete_with_missing_references"
    assert result["reference_validity"]["oracle"]["valid"] == 1
    assert result["reference_validity"]["oracle"]["invalid_ids"] == ["pair-002"]
    precision = result["oracle"]["pairs"]["overall"]["sample_precision"]
    assert precision["estimate"] == 1
    assert precision["missing_reference_bounds"] == [.5, 1]
    assert result["reliability"]["status"] == "inconclusive_undefined_kappa"
    assert result["primary_estimates"] == "oracle_provisional"
