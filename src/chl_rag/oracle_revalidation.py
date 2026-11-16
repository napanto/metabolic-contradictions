"""Addendum 08B: re-validate the saved two-judge answers on their coded answers.

No model or network call, and no new request. For every item of a two-judge run
(<root>/<judge>/max/items/<item_id>/) the saved raw outputs of the counted attempts are
read, checked against their recorded sha256 and validated again with
chl_rag.site_instrument.annotation_from_reference_response on a VALIDATION COPY:
- the free-text `detail` is removed (null in the wire, the only empty form the validator
  accepts) where the page shows no box, i.e. the q2 reason or the q5 factor is not 'other';
- it is cut at 200 characters where the page has a box.
Coded answers are never changed. The exact original texts are kept in
`free_text_as_returned`. Existing run files are only read; the outputs are new files
next to the run: two_judge_final_08b.jsonl and two_judge_final_08b_manifest.json.

Final attempt (PROTOCOL_ADDENDUM_08B_FREE_TEXT.md, rule 3 and its Correction of 03:02 CET):
rule 3 as written, for every item the first attempt valid under 08B is final. Where that
differs from the attempt final under the original rule (five items: Astra 1, Fable 4,
whose first attempt failed on free text alone), the originally valid attempt is recorded
as the alternative in `literal_rule_attempt`, as the Correction names it, with its raw
output sha256. Items whose final attempt is unchanged keep their annotation byte for byte.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from jsonschema import ValidationError

from .io import utcnow
from .site_instrument import annotation_from_reference_response

ADDENDUM = "PROTOCOL_ADDENDUM_08B_FREE_TEXT.md"
ADDENDUM_SHA256 = "d14d12941e74f40da34978d77f55424e45af9e7c77304e6a438fb968c6149ae4"  # with the Correction
REGISTERED_SHA256 = "8ac2bf5ee96b893a861d9fd645d99d64d01accd7efe29e829ffa5833b80de817"  # 02:54 registration
MAX_DETAIL = 200
OUTPUT = "two_judge_final_08b.jsonl"
MANIFEST = "two_judge_final_08b_manifest.json"
JUDGES = ("astra", "fable")


class Revalidation08BError(ValueError):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def validation_copy(raw):
    """Copy of a wire response with free text adapted to the page; coded answers untouched."""
    copy = deepcopy(raw)
    texts, changes = {}, []

    def adapt(name, row, selector):
        value = row.get("detail")
        if value is None:
            return
        texts[name] = value
        if selector != "other":
            row["detail"] = None
            changes.append({"field": name, "action": "removed_no_box"})
        elif isinstance(value, str) and len(value) > MAX_DETAIL:
            row["detail"] = value[:MAX_DETAIL]
            changes.append({"field": name, "action": f"cut_to_{MAX_DETAIL}"})

    path = copy.get("path") if isinstance(copy, dict) else None
    if isinstance(path, dict):
        q2 = path.get("q2")
        if isinstance(q2, dict):
            for field in sorted(q2):
                row = q2[field]
                if isinstance(row, dict) and "detail" in row:
                    adapt(f"path.q2.{field}.detail", row, row.get("reason"))
        q5 = path.get("q5")
        if isinstance(q5, dict) and "detail" in q5:
            adapt("path.q5.detail", q5, q5.get("factor"))
    return copy, texts, changes


def revalidate(raw_text, item):
    """One saved attempt under 08B: {'valid', 'annotation'|'error', 'free_text_as_returned', ...}."""
    try:
        raw = json.loads(raw_text)
    except ValueError:
        return {"valid": False, "error": "output is not JSON", "free_text_as_returned": {},
                "validation_changes": []}
    copy, texts, changes = validation_copy(raw)
    try:
        annotation = annotation_from_reference_response(copy, item)
    except ValidationError as exc:
        error = "Schema validation failed at " + "/".join(map(str, exc.absolute_path))
        return {"valid": False, "error": error, "free_text_as_returned": texts, "validation_changes": changes}
    except (ValueError, KeyError, TypeError) as exc:
        return {"valid": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "free_text_as_returned": texts, "validation_changes": changes}
    return {"valid": True, "annotation": annotation, "free_text_as_returned": texts,
            "validation_changes": changes}


def _attempts(item_dir):
    rows = []
    for dispatch in sorted(item_dir.glob("dispatch-*"), key=lambda p: int(p.name.split("-")[1])):
        metadata_path = dispatch / "metadata.json"
        if not metadata_path.exists():
            continue
        metadata = json.loads(metadata_path.read_text())
        if not metadata.get("counts_as_attempt"):
            continue
        raw_path = dispatch / "raw_output.txt"
        raw = raw_path.read_bytes()
        if metadata.get("raw_output_sha256") and metadata["raw_output_sha256"] != _sha(raw):
            raise Revalidation08BError(f"Saved raw output does not match its recorded hash: {dispatch}")
        rows.append({"attempt": metadata["attempt"], "dispatch": dispatch.name, "status": metadata.get("status"),
                     "raw": raw.decode(), "raw_output_sha256": _sha(raw)})
    numbers = [row["attempt"] for row in rows]
    if numbers != list(range(1, len(rows) + 1)):
        raise Revalidation08BError(f"Attempt numbering is not 1..n: {item_dir}")
    return rows


def process_item(item_dir, judge):
    item_dir = Path(item_dir)
    result = json.loads((item_dir / "result.json").read_text())
    item = json.loads((item_dir / "item.json").read_text())
    if result.get("judge") != judge or result.get("item_id") != item_dir.name or item.get("item_id") != item_dir.name:
        raise Revalidation08BError(f"Item identity mismatch: {item_dir}")
    attempts = _attempts(item_dir)
    checks = [revalidate(row["raw"], item) for row in attempts]
    first_valid = next((row["attempt"] for row, check in zip(attempts, checks) if check["valid"]), None)
    strict = result["status"]
    original = max((row["attempt"] for row in attempts if row["status"] == "valid"), default=None)
    if strict == "valid":
        check = checks[original - 1]
        if not check["valid"] or _canonical(check["annotation"]) != _canonical(result["answer"]):
            raise Revalidation08BError(f"The original final attempt is not valid under 08B: {item_dir}")
    if first_valid is not None:
        used, check, status = first_valid, checks[first_valid - 1], "valid"
    else:
        used, check, status = None, None, "invalid"
    alternative = original if strict == "valid" and original != used else None
    return {"judge": judge, "item_id": item_dir.name, "status": status, "attempt_used": used,
            "annotation": check["annotation"] if check else None,
            "free_text_as_returned": check["free_text_as_returned"] if check else {},
            "validation_changes": check["validation_changes"] if check else [],
            "raw_output_sha256": attempts[used - 1]["raw_output_sha256"] if used else None,
            "strict_status": strict, "strict_attempt_statuses": [row["status"] for row in attempts],
            "attempts_valid_under_08b": [check["valid"] for check in checks],
            "attempt_errors_under_08b": [None if check["valid"] else check["error"] for check in checks],
            "original_final_attempt": original if strict == "valid" else None,
            "literal_rule_attempt": alternative,
            "literal_rule_attempt_raw_output_sha256": attempts[alternative - 1]["raw_output_sha256"] if alternative else None,
            "annotation_identical_to_original_result": strict == "valid" and alternative is None,
            "addendum": ADDENDUM, "addendum_sha256": ADDENDUM_SHA256}


def apply(root, judge, *, write=True):
    run = Path(root) / judge / "max"
    items = sorted(p for p in (run / "items").iterdir() if p.is_dir())
    records = [process_item(item_dir, judge) for item_dir in items]
    body = "".join(_canonical(record) + "\n" for record in records).encode()
    counts = {"items": len(records),
              "valid_08b": sum(r["status"] == "valid" for r in records),
              "invalid_08b": sum(r["status"] == "invalid" for r in records),
              "valid_strict": sum(r["strict_status"] == "valid" for r in records),
              "made_valid_by_08b": sum(r["status"] == "valid" and r["strict_status"] != "valid" for r in records),
              "free_text_adapted_in_used_attempt": sum(bool(r["validation_changes"]) for r in records),
              "final_attempt_changed_from_original": sum(r["literal_rule_attempt"] is not None for r in records)}
    manifest = {"judge": judge, "addendum": ADDENDUM, "addendum_sha256": ADDENDUM_SHA256,
                "addendum_registered_sha256": REGISTERED_SHA256,
                "generated_at": utcnow(), "output": OUTPUT, "output_sha256": _sha(body), "counts": counts,
                "rule": "validity on coded answers; free text removed where no box and cut at 200 characters "
                        "only in the validation copy; rule 3 as written: the first attempt valid under 08B is "
                        "final for every item; the attempt final under the original rule, where different, is "
                        "recorded as the literal_rule_attempt alternative (Correction, 03:02 CET)",
                "existing_files_modified": False}
    if write:
        target = run / OUTPUT
        if target.exists() and target.read_bytes() != body:
            raise Revalidation08BError(f"{target} exists with different content; refusing to overwrite")
        target.write_bytes(body)
        (run / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return records, manifest
