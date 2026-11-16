"""SYNTHETIC fixtures only: tests/fixtures/synthetic_study was built with the site's own code
(scripts/build_synthetic_fixture.py) from a temporary database with invented sessions.

Scenario, primary population after participant corrections (expected numbers checked by hand):
  P1 biomarker  S1 contradictory, S2 contradictory -> resolved at 2 (true flag);
                plus a displaced read (paid, not counted) and a surplus read
  P2 biomarker  concordant, not_replicated, not_replicated -> resolved at 3 (true flag);
                two reads mark sample size of A as missing
  P8 biomarker  S1 not_replicated corrected twice to concordant, S4 concordant -> resolved at 2
  P3 drug       contradictory, concordant, not_comparable, concordant -> resolved at 4
  P4 drug       four different categories -> unresolved after four reads
  P5 diet       apparent_contradiction, extraction_error -> needs a third read;
                a third read from an excluded session enters only the all-read sensitivity
  P6 diet       one valid read; one read from a rejected session -> uncovered
  P7 diet       not_comparable twice -> resolved at 2; the first uses an option removed at 19:59:19
Classifications: low flags every pair except P3 (concordant); P5 is a low system failure;
high differs from low on P1 (category only), P2 and P8 (binary).
Judges: Astra valid on 34 (P6 missing), Fable valid on 34 (P4 invalid).
"""
import json
import shutil
from pathlib import Path

import pytest

from chl_rag import human_analysis as h

FIXTURE = Path("tests/fixtures/synthetic_study")


def run(tmp_path=None, **overrides):
    kwargs = {"binding_path": FIXTURE / "binding.json",
              "oracles": {"astra": FIXTURE / "oracle_astra.jsonl",
                          "fable": FIXTURE / "oracle_fable.jsonl"},
              "classifications": {"low": FIXTURE / "classification/low",
                                  "high": FIXTURE / "classification/high"},
              "sampling_path": FIXTURE / "sampling.json"}
    kwargs.update(overrides)
    return h.analyse(FIXTURE / "export", **kwargs)


@pytest.fixture(scope="module")
def roles():
    return json.loads((FIXTURE / "scenario.json").read_text())["roles"]


@pytest.fixture(scope="module")
def result():
    saved = h.REPLICATES
    h.REPLICATES = 200
    try:
        public, private = run()
    finally:
        h.REPLICATES = saved
    return public, private


def test_schema_and_privacy(result):
    public, private = result
    h.validate_result(public)
    text = json.dumps(public)
    export = [json.loads(line) for line in (FIXTURE / "export/answers.jsonl").read_text().splitlines()]
    for row in export:
        assert row["session_pseudonym"] not in text
        assert row["read_id"] not in text
    assert "Synthetic annotation rationale" not in text
    assert "Synthetic annotation rationale" not in json.dumps(private)
    assert public["status"] == "interim_snapshot" and public["seed"] == 20261124


def test_pair_states_primary(result):
    s = result[0]["students"]["primary"]
    assert s["reads"] == 20
    assert s["pair_states"] == {"resolved": 5, "unresolved_final": 1, "pending_third": 1, "uncovered": 28}
    assert (s["covered_pairs"], s["resolved_pairs"]) == (7, 5)
    assert s["reads_needed_for_resolution"] == {"2": 3, "3": 1, "4": 1}
    assert {a: s["by_arm"][a]["covered"] for a in h.ARMS} == {"biomarker": 3, "drug": 2, "diet": 2}
    assert s["minimum_sample"]["status"] == "descriptive_only" and not s["minimum_sample"]["met"]
    assert s["consensus_categories"] == {"contradictory": 1, "not_replicated": 1, "concordant": 2,
                                         "not_comparable": 1}
    assert s["exclusions"] == {"eligibility_false": 1, "surplus_or_unpaid": 1, "excluded_session": 1,
                               "not_counted_in_slot": 1}


def test_human_agreement_first_two_reads(result):
    hh = result[0]["students"]["primary"]["human_human_first_two_reads"]
    assert hh["pairs_with_two_reads"] == 7 and hh["pairs_with_one_read"] == 1
    # Agreeing first two reads: P1, P7, P8 (category); P1, P7, P8 (binary).
    assert hh["metrics"]["category_raw_agreement"]["estimate"] == pytest.approx(3 / 7)
    assert hh["metrics"]["binary_raw_agreement"]["estimate"] == pytest.approx(3 / 7)


def test_sensitivities(result):
    students = result[0]["students"]
    original = students["sensitivity_original_answers"]
    assert original["pair_states"] == {"resolved": 4, "unresolved_final": 1, "pending_third": 2, "uncovered": 28}
    accepted = students["sensitivity_all_accepted_reads"]
    assert accepted["pair_states"] == {"resolved": 6, "unresolved_final": 1, "uncovered": 28}
    assert accepted["reads_needed_for_resolution"] == {"2": 3, "3": 2, "4": 1}
    assert accepted["exclusions"] == {"eligibility_false": 1, "surplus_or_unpaid": 1}
    assert result[0]["corrections"] == {"entries": 2, "chains_applied": 1, "reads_corrected": 1,
                                        "label_changed": 1, "binary_changed": 1}


def test_system_precision_and_failure(result):
    system = result[0]["students"]["primary"]["system"]
    assert system["flagged_pairs"] == 33 and system["system_failures_excluded"] == 1
    p = system["precision"]
    assert (p["overall"]["successes"], p["overall"]["n"], p["overall"]["expected"]) == (2, 4, 33)
    assert p["overall"]["missing_reference_bounds"] == pytest.approx([2 / 33, 31 / 33])
    assert [(p[a]["successes"], p[a]["n"], p[a]["expected"]) for a in h.ARMS] == [(2, 3, 13), (0, 0, 11), (0, 1, 9)]
    earlier = result[0]["students"]["primary"]["earlier_v2_label"]
    assert earlier["flagged_pairs"] == 35
    assert (earlier["precision"]["overall"]["successes"], earlier["precision"]["overall"]["n"]) == (2, 5)
    validity = result[0]["classification_validity"]
    assert validity["low"]["system_failures"] == 1 and validity["high"]["system_failures"] == 0


def test_instrument_split(result):
    segments = {s["id"]: s["reads"] for s in result[0]["instrument_versions"]["segments"]}
    assert segments["eligibility-20261114"] == {"certain": 5}
    assert segments["keyrules2-20261114"] == {"certain": 2}
    assert segments["population-20261114"] == {"certain": 1}
    assert segments["practice-20261114"] == {"certain": 7}
    assert segments["boxes-20261115"] == {"settled_by_options": 1}
    assert segments["layout-20261115"] == {"certain": 5}
    assert result[0]["instrument_versions"]["unassigned"] == {
        "ambiguous:keyrules-20261114|keyrules2-20261114": 1,
        "ambiguous:boxes-20261115|wording-20261115": 1,
        "ambiguous:population-20261114|practice-20261114": 1}


def test_q1_checks(result):
    fields = result[0]["q1_checks"]["fields"]
    assert result[0]["q1_checks"]["sides_expected"] == 70
    size = fields["sample_size"]
    assert (size["correct"]["successes"], size["correct"]["n"], size["correct"]["expected"]) == (13, 14, 70)
    assert size["consensus_answers"] == {"yes": 13, "no": 1}
    assert fields["context"]["consensus_answers"] == {"yes": 13, "cannot_tell": 1}  # strict: not correct
    assert fields["result"]["correct"]["n"] == 13  # P5 side A has a 1-1 split
    split = size["read_answers_by_layout_split"]
    assert sum(split["after"].values()) == 6 and sum(split["unassigned"].values()) == 6


def test_judges(result):
    oracle = result[0]["oracle"]
    assert oracle["validity"]["astra"] == {"valid": 34, "invalid": 0, "missing": 1, "expected": 35,
                                           "validity_rule": "as supplied"}
    assert oracle["validity"]["fable"] == {"valid": 34, "invalid": 1, "missing": 0, "expected": 35,
                                           "validity_rule": "as supplied"}
    astra, fable = (oracle["judges"][j]["reliability"] for j in h.JUDGES)
    # Astra binary: 4/5 agree; marginals 3/2 vs 2/3 -> kappa (0.8-0.48)/0.52.
    assert astra["binary_kappa"] == pytest.approx(0.32 / 0.52) and astra["status"] == "threshold_met"
    # Fable binary: 3/5 agree; marginals 2/3 vs 2/3 -> kappa (0.6-0.52)/0.48.
    assert fable["binary_kappa"] == pytest.approx(0.08 / 0.48) and fable["status"] == "unreliable"
    assert astra["imprecise"] and astra["covered_pairs"] == 7
    inter = oracle["inter_judge"]
    assert inter["pairs_both_valid"] == 33
    assert inter["pair"]["metrics"]["category_raw_agreement"]["estimate"] == pytest.approx(29 / 33)
    assert inter["pair"]["metrics"]["binary_raw_agreement"]["estimate"] == pytest.approx(30 / 33)
    assert inter["pair"]["metrics"]["binary_kappa"]["estimate"] == pytest.approx(
        (30 / 33 - 882 / 1089) / (1 - 882 / 1089))
    assert inter["q1_checks"]["sample_size"]["agreement"] == pytest.approx(65 / 66)
    assert oracle["oracle_reference"]["status"] == "not_available"


def test_oracle_reference_when_both_judges_pass(tmp_path, roles):
    rows = [json.loads(line) for line in (FIXTURE / "oracle_astra.jsonl").read_text().splitlines()]
    fable = tmp_path / "oracle_fable.jsonl"
    fable.write_text("".join(json.dumps({**row, "judge": "fable"}) + "\n" for row in rows))
    saved = h.REPLICATES
    h.REPLICATES = 20
    try:
        public, _ = run(oracles={"astra": FIXTURE / "oracle_astra.jsonl", "fable": fable})
    finally:
        h.REPLICATES = saved
    reference = public["oracle"]["oracle_reference"]
    assert reference["status"] == "available"
    assert reference["pairs_with_reference"] == 34 and reference["missing_pairs"] == 1
    # Astra flags P1, P2, P5, P8; low flags all but P3 and P5 (failure) -> 3 true of 33 flagged.
    p = reference["precision"]["overall"]
    assert (p["successes"], p["n"], p["expected"]) == (3, 32, 33)


def test_classifier_comparison(result):
    c = result[0]["classifier_effort_comparison"]
    assert c["status"] == "descriptive_only" and "conditional on that selection" in c["note"]
    assert c["pairs_both_classified"] == 34 and c["flagged"] == {"low": 33, "high": 32}
    assert c["differ"]["category"] == 3 and c["differ"]["binary"] == 2
    assert c["differ"]["reference_sides_category"]["student_consensus"] == {"low": 1, "neither": 1, "high": 1}
    assert c["differ"]["reference_sides_binary"] == {"student_consensus": {"low": 1, "high": 1},
                                                     "astra": {"low": 2}, "fable": {"high": 2}}
    assert set(c["agreement"]["low"]) == {"student_consensus", "astra", "fable"}


def test_macros(result):
    text = h.macros(result[0])
    assert r"\providecommand{\HAResolvedPairs}{5}" in text
    assert r"\providecommand{\HAPrecisionStudentsBounds}{[0.061, 0.939]}" in text
    assert "v2" not in text.lower().replace("\\providecommand", "")


def test_rejects_tampered_export(tmp_path):
    copy = tmp_path / "export"
    shutil.copytree(FIXTURE / "export", copy)
    lines = (copy / "answers.jsonl").read_text().splitlines()
    row = json.loads(lines[0])
    row["label"] = "concordant" if row["label"] != "concordant" else "contradictory"
    (copy / "answers.jsonl").write_text("\n".join([json.dumps(row), *lines[1:]]) + "\n")
    binding = h.load_binding(FIXTURE / "binding.json")
    live, _ = h.load_package(binding["package"]["directory"], binding)
    with pytest.raises(h.AnalysisError) as error:
        h.load_export(copy, live=live, binding=binding)
    assert error.value.field == "metadata.answers_sha256"
    metadata = json.loads((copy / "metadata.json").read_text())
    metadata["answers_sha256"] = h._sha((copy / "answers.jsonl").read_bytes())
    (copy / "metadata.json").write_text(json.dumps(metadata))
    with pytest.raises(h.AnalysisError) as error:
        h.load_export(copy, live=live, binding=binding)
    assert error.value.field == "answers.derivation"


def test_rejects_wrong_configuration(tmp_path):
    binding = json.loads((FIXTURE / "binding.json").read_text())
    binding["site_configuration_sha256"] = "0" * 64
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(binding))
    with pytest.raises(h.AnalysisError) as error:
        h.analyse(FIXTURE / "export", binding_path=path)
    assert error.value.field == "metadata.configuration"


def test_no_fifth_read_and_states():
    rows = [{"label": label, "_submitted": i, "_order": i, "item_id": "x"} for i, label in
            enumerate(["contradictory", "concordant", "not_comparable", "extraction_error", "concordant"])]
    states, beyond = h.pair_states({"x": rows}, cap=True)
    assert beyond == 1 and states["x"]["status"] == "unresolved_final" and states["x"]["label"] is None
    states, _ = h.pair_states({"x": rows}, cap=False)
    assert states["x"]["label"] == "concordant" and states["x"]["reads_needed"] == 5
    states, _ = h.pair_states({"x": rows[:3]}, cap=True)
    assert states["x"]["status"] == "pending_fourth"


def test_earlier_options_after_the_deploy_are_an_anomaly():
    binding = h.load_binding(FIXTURE / "binding.json")
    segments = binding["instrument_timeline"]["segments"]
    path = json.loads((FIXTURE / "export/answers.jsonl").read_text().splitlines()[0])["path"]
    path = json.loads(json.dumps(path))
    path["q2"] = {"exposure": {"answer": "yes"}, "outcome": {"answer": "yes"},
                  "comparison": {"answer": "no", "reason": "dose"}}
    row = {"path": path, "_assigned": h._time("2026-11-15T22:00:00+01:00"),
           "_submitted": h._time("2026-11-15T22:05:00+01:00")}
    assert h.classify_instrument(row, segments)["status"] == "anomaly_earlier_options_after_deploy"


def test_invalid_classification_with_verdict_is_rejected(tmp_path, roles):
    directory = tmp_path / "low"
    shutil.copytree(FIXTURE / "classification/low", directory)
    record = json.loads((directory / f"{roles['P1']}.json").read_text())
    record["status"] = "invalid"
    (directory / f"{roles['P1']}.json").write_text(json.dumps(record))
    binding = h.load_binding(FIXTURE / "binding.json")
    live, _ = h.load_package(binding["package"]["directory"], binding)
    with pytest.raises(h.AnalysisError) as error:
        h.load_classification(directory, "low", live)
    assert error.value.field == "classification.low.invalid_has_verdict"


# ----------------------------------------------------------------------------- follow-ups

RUN = Path("tests/fixtures/synthetic_oracle_run")


def _started_sidecar(tmp_path, shifts):
    """started = assigned_at except for the named reads (role, session order) -> new clock time."""
    rows = [json.loads(line) for line in (FIXTURE / "export/answers.jsonl").read_text().splitlines()]
    lines = []
    for row in rows:
        started = shifts.get((row["item_id"], row["assigned_at_utc"]), row["assigned_at_utc"])
        lines.append(json.dumps({"assignment_id": row["read_id"], "started_utc": started}))
    path = tmp_path / "started.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path, rows


def test_started_sidecar_settles_reads_exactly(tmp_path, roles):
    rows = [json.loads(line) for line in (FIXTURE / "export/answers.jsonl").read_text().splitlines()]
    assigned = {(r["item_id"], r["submitted_at_utc"]): r["assigned_at_utc"] for r in rows}

    def key(role, submitted):
        hit = [k for k in assigned if k[0] == roles[role] and k[1].startswith(submitted)]
        assert len(hit) == 1
        return (hit[0][0], assigned[hit[0]])

    shifts = {key("P2", "2026-11-14T15:10"): "2026-11-14T15:06:00.000000+00:00",   # 16:06 CET, after 16:04:58
              key("P8", "2026-11-14T19:15"): "2026-11-14T19:05:00.000000+00:00",   # 20:05 CET, after 20:04:30
              key("P5", "2026-11-15T19:05"): "2026-11-15T18:52:00.000000+00:00"}   # 19:52 CET, still before 19:59:19
    path, _ = _started_sidecar(tmp_path, shifts)
    saved = h.REPLICATES
    h.REPLICATES = 20
    try:
        public, _ = run(started_path=path)
    finally:
        h.REPLICATES = saved
    versions = public["instrument_versions"]
    segments = {s["id"]: s["reads"] for s in versions["segments"]}
    assert segments["keyrules2-20261114"] == {"certain": 3}
    assert segments["practice-20261114"] == {"certain": 8}
    assert versions["unassigned"] == {"ambiguous:boxes-20261115|wording-20261115": 1}
    assert versions["split_basis"].startswith("assignments.started")
    assert versions["started_sidecar"]["entries"] == 24 and versions["started_sidecar"]["rows_without_started"] == 0
    for row in json.loads((FIXTURE / "scenario.json").read_text())["roles"].values():
        assert row not in json.dumps(versions["started_sidecar"])
    h.validate_result(public)


def test_started_sidecar_must_cover_and_order(tmp_path):
    path, _ = _started_sidecar(tmp_path, {})
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[1:]) + "\n")
    with pytest.raises(h.AnalysisError) as error:
        run(started_path=path, oracles=None, classifications=None)
    assert error.value.field == "started.coverage"
    late = json.loads(lines[0])
    late["started_utc"] = "2026-11-17T00:00:00+00:00"
    path.write_text("\n".join([json.dumps(late), *lines[1:]]) + "\n")
    with pytest.raises(h.AnalysisError) as error:
        run(started_path=path, oracles=None, classifications=None)
    assert error.value.field == "started.order"


def test_adapter_on_fake_transport_outputs(tmp_path):
    live = {f"v2-pair-00{i}": {"arm": "biomarker"} for i in range(1, 5)}
    rows, counts = h.oracle_rows_from_run(RUN / "astra", "astra", live)
    assert counts == {"valid": 4} and all(r["status"] == "valid" for r in rows)
    labels, validity = h.load_oracle(RUN / "astra", "astra", live)
    assert validity == {"valid": 4, "invalid": 0, "missing": 0, "expected": 4, "run_statuses": {"valid": 4},
                        "validity_rule": "amendment 08 as first registered (strict)"}
    labels, validity = h.load_oracle(RUN / "fable", "fable", live)
    assert validity["valid"] == 3 and validity["invalid"] == 1
    assert validity["run_statuses"] == {"valid": 3, "invalid:invalid": 1}
    assert "v2-pair-002" not in labels
    # An item still open (directory without result.json) or never started is missing, not invalid.
    copy = tmp_path / "fable"
    shutil.copytree(RUN / "fable", copy)
    (copy / "items/v2-pair-004/result.json").unlink()
    live["v2-pair-005"] = {"arm": "diet"}
    _, validity = h.load_oracle(copy, "fable", live)
    assert validity["missing"] == 2 and validity["run_statuses"]["not_final"] == 1
    assert validity["run_statuses"]["not_started"] == 1
    with pytest.raises(h.AnalysisError) as error:
        h.load_oracle(RUN / "fable", "astra", live)
    assert error.value.field == "adapter.astra.identity"


def test_binding_records_exact_instants_and_unchanged_reloads():
    binding = json.loads(Path("configs/human_analysis_binding.json").read_text())
    timeline = binding["instrument_timeline"]
    assert all(s["reload_earliest"] == s["reload_latest"] for s in timeline["segments"])
    population = next(s for s in timeline["segments"] if s["id"] == "population-20261114")
    assert population["reload_earliest"] == "2026-11-14T19:07:06.20+01:00"
    check = timeline["unchanged_instrument_reloads"]
    assert check["passed"] and len(check["tags"]) == 16
    assert all(t["equal_to_segment"] and t["deploy_log_line"] for t in check["tags"])


def test_report_macros_present_and_defined(result):
    text = h.macros(result[0])
    for name in ("HAQOneResult", "HAQOneContext", "HAInterJudgePairs", "HAAstraKappaCI", "HAFableComparedPairs",
                 "HALowBinaryAgreeStudents", "HAHighFlagged", "HAPrecisionStudentsCI", "HAHumanPairs", "HAReadsNeeded"):
        assert "\\providecommand{\\" + name + "}{" in text, name
    assert "\\providecommand{\\HAHumanPairs}{\\pending{}}" not in text


def test_participants_exact_counts(result):
    part = result[0]["participants"]
    demo = result[0]["demographics"]
    groups = {g["group"]: g["n"] for g in part["programme_groups"]["groups"]}
    assert sum(groups.values()) == sum(demo["programme"].values())
    assert sorted(groups.values()) == sorted(v for v in demo["programme"].values() if v)
    assert part["with_valid_read"] >= 1 and part["reads_per_participant"]["min"] >= 1
    text = h.macros(result[0])
    for name in ("HAParticipants", "HAReadsPerParticipantMedian", "HAReadsPerParticipantRange",
                 "HAProgrammeRows", "HAProgrammeGroups", "HAPairsBiomarker", "HAUncoveredDiet", "HASnapshotTime"):
        assert "\\providecommand{\\" + name + "}{" in text, name
    assert "Other programmes" not in text


# ----------------------------------------------------------------------------- addendum 08B

def test_strict_sensitivity_and_08b_primary_are_both_reported(tmp_path, roles):
    rows = [json.loads(line) for line in (FIXTURE / "oracle_fable.jsonl").read_text().splitlines()]
    strict_rows = [({**row, "status": "invalid", "annotation": None}
                    if row["item_id"] in {roles["P1"], roles["P2"]} else row) for row in rows]
    strict = tmp_path / "oracle_fable_strict.jsonl"
    strict.write_text("".join(json.dumps(row) + "\n" for row in strict_rows))
    saved = h.REPLICATES
    h.REPLICATES = 20
    try:
        public, _ = run(oracles_strict={"astra": FIXTURE / "oracle_astra.jsonl", "fable": strict})
    finally:
        h.REPLICATES = saved
    h.validate_result(public)
    assert public["oracle"]["validity"]["fable"]["valid"] == 34
    sensitivity = public["oracle_sensitivity_strict"]
    assert sensitivity["validity"]["fable"]["valid"] == 32 and sensitivity["validity"]["fable"]["invalid"] == 3
    assert sensitivity["inter_judge"]["pairs_both_valid"] == 31
    assert "first registered" in sensitivity["rule"]
    text = h.macros(public)
    for name in ("HAFableValid}{34}", "HAFableValidStrict}{32}", "HAAstraValidStrict}{34}",
                 "HAInterJudgePairsStrict}{31}", "HAFableKappaStrict}", "HAFableBinaryAgreementStrict}",
                 "HAInterJudgeBinaryAgreementStrict}", "HAOracleReferenceStrict}", "HAFableKappaCIStrict}"):
        assert "\\" + name in text


def test_without_strict_outputs_the_sensitivity_is_not_applicable(result):
    assert result[0]["oracle_sensitivity_strict"]["status"] == "not_applicable"
    assert r"\providecommand{\HAFableValidStrict}{\pending{}}" in h.macros(result[0])


def test_optional_max_setting_in_the_comparison(tmp_path):
    maximum = tmp_path / "max"
    maximum.mkdir()
    for path in (FIXTURE / "classification/high").glob("v2-pair-*.json"):
        record = json.loads(path.read_text())
        (maximum / path.name).write_text(json.dumps({**record, "setting": "max"}))
    saved = h.REPLICATES
    h.REPLICATES = 20
    try:
        public, _ = run(classifications={"low": FIXTURE / "classification/low",
                                         "high": FIXTURE / "classification/high",
                                         "max": maximum})
    finally:
        h.REPLICATES = saved
    comp = public["classifier_effort_comparison"]
    assert comp["flagged"] == {"low": 33, "high": 32, "max": 32} and "max" in comp["agreement"]
    text = h.macros(public)
    assert r"\HAMaxFlagged}{32}" in text and r"\HAMaxBinaryAgreeStudents}" in text


# ----------------------------------------------------------------------------- 16 November additions

def test_gwet_ac1_by_hand():
    # pa 0.75; pi(T) 0.375, pi(F) 0.625; pe 0.46875; AC1 (0.75-0.46875)/0.53125.
    assert h.gwet_ac1([True, True, False, False], [True, False, False, False], (True, False)) == pytest.approx(
        0.28125 / 0.53125)
    assert h.gwet_ac1(["yes"] * 3, ["yes"] * 3, h.Q1_ANSWERS) == pytest.approx(1.0)
    assert h.gwet_ac1([], [], (True, False)) is None


def test_ac1_next_to_every_kappa(result):
    astra = result[0]["oracle"]["judges"]["astra"]["vs_student_consensus"]["metrics"]
    assert astra["binary_ac1"]["estimate"] == pytest.approx(0.6)  # pa 0.8, pe 0.5
    # categories: contr 2, nr 3, conc 3, nc 2 of 10 -> pe 0.74 / 5; pa 0.8.
    assert astra["category_ac1"]["estimate"] == pytest.approx((0.8 - 0.148) / 0.852)
    assert astra["binary_ac1"]["bootstrap"]["seed"] == h.SEED
    assert astra["binary_kappa"]["estimate"] == pytest.approx(0.32 / 0.52)  # unchanged
    size = result[0]["oracle"]["inter_judge"]["q1_checks"]["sample_size"]
    chance = 131 / 17424
    assert size["ac1"] == pytest.approx((65 / 66 - chance) / (1 - chance))
    assert size["ac1_interval"]["seed"] == h.SEED
    reads = result[0]["oracle"]["judges"]["fable"]["vs_individual_reads"]["metrics"]
    assert {"binary_ac1", "category_ac1"} <= set(reads)


def test_precision_on_the_study_sample_per_setting(result):
    settings = result[0]["students"]["primary"]["settings"]
    assert set(settings) == {"low", "high"}
    low, high = settings["low"], settings["high"]
    assert (low["flagged_pairs"], low["invalid_labels"]) == (33, 1)
    assert (high["flagged_pairs"], high["invalid_labels"]) == (32, 0)
    p = high["precision_on_the_study_sample"]["overall"]
    assert (p["successes"], p["n"], p["expected"]) == (1, 2, 32)
    metrics = high["agreement_with_consensus"]["metrics"]
    assert metrics["category_raw_agreement"]["estimate"] == pytest.approx(2 / 5)
    assert metrics["binary_raw_agreement"]["estimate"] == pytest.approx(3 / 5)
    assert "not extrapolated to the corpus" in high["note"]
    text = h.macros(result[0])
    for expected in (r"\HALowFlaggedDenominator}{33}", r"\HAHighFlaggedDenominator}{32}",
                     r"\HAHighPrecisionStudents}{0.500}", r"\HAHighPrecisionStudentsN}{2}",
                     r"\HAMaxFlaggedDenominator}{\pending{}}", r"\HAAstraACOne}{0.60}",
                     r"\HAHighCategoryAgreeStudents}{0.40}", r"\HASystemBinaryACOne}",
                     r"\HAInterJudgeCategoryACOne}", r"\HAFactorOracleMatchLow}{0}"):
        assert expected in text


def test_factor_agreement_counts():
    def read(label, factor):
        return {"label": label, "factor": factor}
    groups = {"p1": [read("apparent_contradiction", "dose"), read("apparent_contradiction", "dose"),
                     read("concordant", "none")],
              "p2": [read("apparent_contradiction", "dose"), read("apparent_contradiction", "population")],
              "p3": [read("concordant", "none"), read("concordant", "none")]}
    consensus = {"p1": "apparent_contradiction", "p2": "apparent_contradiction", "p3": "concordant"}

    def judge(values):
        return {k: {"label": "apparent_contradiction", "factor": f} for k, f in values.items()}

    judges = {"astra": judge({"p1": "dose", "p4": "population", "p5": "dose"}),
              "fable": judge({"p1": "dose", "p4": "population", "p5": "region"})}
    factors = {"low": {"p1": "dose", "p4": "none"}, "high": {"p1": "population", "p4": "population"}}
    out = h.factor_agreement(groups, consensus, judges, factors, live={})
    assert out["pairs_considered"] == 4
    assert out["student_consensus"]["apparent_pairs"] == 2 and out["student_consensus"]["with_consensus_factor"] == 1
    assert out["student_consensus"]["matches"] == {"low": 1, "high": 0}
    assert out["oracle"]["apparent_pairs"] == 3 and out["oracle"]["with_agreed_factor"] == 2
    assert out["oracle"]["matches"] == {"low": 1, "high": 1}


def test_factor_agreement_in_the_synthetic_run(result):
    factor = result[0]["factor_agreement"]
    # P5: both judges apparent contradiction with the same factor; no student consensus is apparent.
    assert factor["pairs_considered"] == 1 and factor["oracle"]["with_agreed_factor"] == 1
    assert factor["oracle"]["matches"] == {"low": 0, "high": 0}
    assert factor["student_consensus"]["apparent_pairs"] == 0
