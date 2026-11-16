"""Addendum 08B re-validation on invented run directories built from public package items.

Items (all synthetic answers on public v2.1 pair items):
  A  valid under the original rule, untouched;
  B  free text where the page shows no box (q5 factor not 'other');
  C  free text over 200 characters in the 'other' box of Q2;
  D  attempt 1 invalid only for free text (outcome detail), attempt 2 valid: under rule 3 as
     written (Correction of 03:02 CET) attempt 1 is final and attempt 2 is the recorded alternative;
  E  attempt 1 not JSON, attempt 2 with an invalid coded answer.
"""
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from chl_rag import oracle_revalidation as b08
from chl_rag import human_analysis as h
from chl_rag.site_instrument import annotation_from_reference_response, encode_response
from chl_rag.pair_instrument import FIELDS

ITEMS = [json.loads(line) for line in Path("annotation/study_pairs/items.jsonl").read_text().splitlines()][:5]
LONG = "x" * 250
NOTE = "Synthetic note."


def q1():
    return {side: {field: {"answer": "yes", "problems": []} for field in FIELDS} for side in ("a", "b")}


def canonical(item, path):
    return {"instrument_version": "2.0", "pipeline_version": "2.0", "item_id": item["item_id"], "task": "pair",
            "path": path, "rationale": NOTE}


def concordant():
    return {"q1": q1(), "q2": {k: {"answer": "yes"} for k in ("exposure", "outcome", "comparison")}, "q3": "yes"}


def apparent():
    return {**concordant(), "q3": "no", "q4": "opposite", "q5": {"answer": "yes", "factor": "population"}}


def other_reason():
    path = concordant()
    del path["q3"]
    path["q2"]["comparison"] = {"answer": "no", "reason": "other", "detail": "short"}
    path["q2_primary"] = "comparison"
    return path


def wire(item, path, **inject):
    raw = encode_response(canonical(item, path), item)
    for where, text in inject.items():
        node = raw["path"]
        for key in where.split(".")[:-1]:
            node = node[key]
        node[where.split(".")[-1]] = text
    return json.dumps(raw)


def write_item(root, judge, item, attempts):
    """attempts: list of (raw_text, strict_status)."""
    directory = root / judge / "max" / "items" / item["item_id"]
    directory.mkdir(parents=True)
    (directory / "item.json").write_text(json.dumps(item))
    answer = None
    for number, (raw, status) in enumerate(attempts, 1):
        dispatch = directory / f"dispatch-{number}"
        dispatch.mkdir()
        (dispatch / "raw_output.txt").write_text(raw)
        (dispatch / "metadata.json").write_text(json.dumps(
            {"counts_as_attempt": True, "attempt": number, "status": status,
             "raw_output_sha256": hashlib.sha256(raw.encode()).hexdigest()}))
        if status == "valid":
            answer = annotation_from_reference_response(json.loads(raw), item)
    final = "valid" if attempts[-1][1] == "valid" else "invalid"
    (directory / "result.json").write_text(json.dumps(
        {"judge": judge, "item_id": item["item_id"], "task": "pair", "pair_binding": "61c08a1",
         "status": final, "answer": answer, "attempt_statuses": [s for _, s in attempts]}))


@pytest.fixture
def run(tmp_path):
    A, B, C, D, E = ITEMS
    write_item(tmp_path, "fable", A, [(wire(A, concordant()), "valid")])
    write_item(tmp_path, "fable", B, [(wire(B, apparent(), **{"q5.detail": "Text where no box is shown."}), "invalid")] * 2)
    write_item(tmp_path, "fable", C, [(wire(C, other_reason(), **{"q2.comparison.detail": LONG}), "invalid")] * 2)
    write_item(tmp_path, "fable", D, [(wire(D, concordant(), **{"q2.outcome.detail": "Outcome text."}), "invalid"),
                                      (wire(D, concordant()), "valid")])
    bad = canonical(E, concordant())
    bad_wire = encode_response(bad, E)
    bad_wire["path"]["q1"]["a"]["result"] = {"answer": "no", "problems": []}
    write_item(tmp_path, "fable", E, [("not json", "invalid"), (json.dumps(bad_wire), "invalid")])
    return tmp_path


def _hashes(root):
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def test_revalidation_rules(run):
    before = _hashes(run)
    records, manifest = b08.apply(run, "fable")
    after = _hashes(run)
    new = set(after) - set(before)
    assert {p.name for p in new} == {b08.OUTPUT, b08.MANIFEST}
    assert all(after[p] == digest for p, digest in before.items())  # existing files untouched
    by = {r["item_id"]: r for r in records}
    A, B, C, D, E = (i["item_id"] for i in ITEMS)
    assert by[A]["status"] == "valid" and by[A]["strict_status"] == "valid" and by[A]["validation_changes"] == []
    result = json.loads((run / "fable/max/items" / A / "result.json").read_text())
    assert json.dumps(by[A]["annotation"], sort_keys=True) == json.dumps(result["answer"], sort_keys=True)
    assert by[B]["status"] == "valid" and by[B]["strict_status"] == "invalid" and by[B]["attempt_used"] == 1
    assert by[B]["free_text_as_returned"] == {"path.q5.detail": "Text where no box is shown."}
    assert by[B]["validation_changes"] == [{"field": "path.q5.detail", "action": "removed_no_box"}]
    assert by[C]["status"] == "valid" and by[C]["free_text_as_returned"] == {"path.q2.comparison.detail": LONG}
    assert by[C]["validation_changes"] == [{"field": "path.q2.comparison.detail", "action": "cut_to_200"}]
    assert by[C]["annotation"]["other_detail"] == LONG[:200]
    assert by[D]["status"] == "valid" and by[D]["attempt_used"] == 1 and by[D]["literal_rule_attempt"] == 2
    assert by[D]["original_final_attempt"] == 2 and not by[D]["annotation_identical_to_original_result"]
    assert by[D]["validation_changes"] == [{"field": "path.q2.outcome.detail", "action": "removed_no_box"}]
    assert by[D]["literal_rule_attempt_raw_output_sha256"] == hashlib.sha256(
        (run / "fable/max/items" / D / "dispatch-2/raw_output.txt").read_bytes()).hexdigest()
    assert by[A]["annotation_identical_to_original_result"] and by[A]["literal_rule_attempt"] is None
    assert by[E]["status"] == "invalid" and by[E]["annotation"] is None
    assert by[E]["attempts_valid_under_08b"] == [False, False]
    assert all(r["addendum_sha256"] == b08.ADDENDUM_SHA256 for r in records)
    assert manifest["counts"] == {"items": 5, "valid_08b": 4, "invalid_08b": 1, "valid_strict": 2,
                                  "made_valid_by_08b": 2, "free_text_adapted_in_used_attempt": 3,
                                  "final_attempt_changed_from_original": 1}
    assert manifest["addendum_registered_sha256"] == b08.REGISTERED_SHA256


def test_registered_addendum_hash():
    assert hashlib.sha256(Path(b08.ADDENDUM).read_bytes()).hexdigest() == b08.ADDENDUM_SHA256
    assert h.ADDENDUM_08B_SHA256 == b08.ADDENDUM_SHA256


def test_refuses_to_overwrite_different_output(run):
    b08.apply(run, "fable")
    target = run / "fable/max" / b08.OUTPUT
    target.write_text(target.read_text() + "\n")
    with pytest.raises(b08.Revalidation08BError):
        b08.apply(run, "fable")


def test_tampered_raw_output_is_refused(run):
    item = ITEMS[0]["item_id"]
    raw = run / "fable/max/items" / item / "dispatch-1/raw_output.txt"
    raw.write_text(raw.read_text() + " ")
    with pytest.raises(b08.Revalidation08BError):
        b08.apply(run, "fable", write=False)


def test_adapter_reads_08b_primary_and_strict_sensitivity(run):
    b08.apply(run, "fable")
    live = {i["item_id"]: {"arm": i["arm"]} for i in ITEMS}
    labels, validity = h.load_oracle(run / "fable/max", "fable", live)
    assert (validity["valid"], validity["invalid"]) == (4, 1)
    assert validity["validity_rule"].startswith("addendum 08B")
    assert validity["run_statuses"] == {"valid_strict": 2, "valid_by_08b": 2, "invalid": 1}
    strict, strict_validity = h.load_oracle(run / "fable/max", "fable", live, mode="strict")
    assert (strict_validity["valid"], strict_validity["invalid"]) == (2, 3)
    assert strict_validity["validity_rule"].startswith("amendment 08")
    assert set(strict) < set(labels)


def test_validation_copy_never_changes_coded_answers():
    item = ITEMS[2]
    raw = json.loads(wire(item, other_reason(), **{"q2.comparison.detail": LONG}))
    copy, texts, _ = b08.validation_copy(raw)
    stripped = deepcopy(raw)
    stripped["path"]["q2"]["comparison"]["detail"] = None
    copy_stripped = deepcopy(copy)
    copy_stripped["path"]["q2"]["comparison"]["detail"] = None
    assert copy_stripped == stripped and texts == {"path.q2.comparison.detail": LONG}
    assert raw["path"]["q2"]["comparison"]["detail"] == LONG  # the original is not mutated
