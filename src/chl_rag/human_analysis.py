"""Final human analysis of the system's pair study, with the two-judge oracle comparison.

Offline only: no model or network call. Inputs are the site's private `export-v2`
directory (answers, metadata, corrections), the public site package, the optional
validated oracle outputs of the two judges and the optional v2.1 classifications.

The public result holds aggregates only. Pair-level references and disagreements go
to a private file (0600) and never into the public JSON, the macros or the CLI
output. Session pseudonyms, notes and individual answers are never written.

Registered rules implemented here: amendment 07 and its addendum A (coverage,
consensus, third and fourth reads, minimum sample), the participant-correction
contract, the instrument split rule of configs/instrument_amendment_20261115.json
and the draft amendment 08 (two judges, system label, low/high comparison).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
from collections import Counter
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator

from .annotation import PAIR_LABELS
from .io import read_jsonl, utcnow
from .metrics import cohen_kappa
from .oracle_evaluate import _interval, agreement_interval, bounded_rate, design_weighted_precision
from .report_tables import tex
from .student_validation import _bootstrap, consensus_label, human_agreement
from .classifier_metrics import derived_six_label
from .pair_instrument_study_start import ANSWERS as Q1_ANSWERS
from .pair_instrument_study_start import FIELDS as Q1_FIELDS
from .pair_instrument_study_start import validate_path
from .claim_schema import CLASSIFICATION

SEED = 20261124
REPLICATES = 2000
ARMS = ("biomarker", "drug", "diet")
SIDES = ("a", "b")
TRUE_FLAGS = frozenset({"contradictory", "apparent_contradiction", "not_replicated"})
JUDGES = ("astra", "fable")
SETTINGS = ("low", "high", "max")  # max: addendum 08C, optional
MAX_READS = 4
MIN_COVERED_PER_ARM = 5
IMPRECISE_BELOW_COVERED = 30
KAPPA_THRESHOLD = 0.40
BINDING = "configs/human_analysis_binding.json"
SCHEMA = "configs/human_analysis_results.schema.json"
ANSWER_KEYS = frozenset({
    "item_id", "read_id", "instrument_version", "pipeline_version", "path", "field_problems",
    "relevance", "other_detail", "binary_inconsistent", "package_sha256", "config_sha256",
    "session_pseudonym", "label", "factor", "rationale", "time_on_item_seconds",
    "submitted_at_utc", "assigned_at_utc", "paid", "replacement", "surplus", "excluded",
    "quality_failed", "counted", "preview"})
FLAG_KEYS = ("paid", "replacement", "surplus", "excluded", "quality_failed", "counted", "preview")
CORRECTION_KEYS = frozenset({
    "audit_id", "recorded_at_utc", "session_pseudonym", "read_id", "item_id", "corrected_path",
    "corrected_rationale", "derived_old", "derived_new", "changes", "source", "disclosure",
    "recorded_by", "derived_with", "applied", "supersedes_audit_id", "changes_relative_to"})
# A1/A2 of v2.1-wording-20261115: options offered only before that deploy.
NEW_COMPARISON_REASONS = frozenset({"comparator", "other", "outcome_definition"})
CORE_FIELDS = ("result", "comparison")
SELECTION_NOTE = ("All 35 pairs were flagged by the earlier low classifier; the low/high comparison "
                  "is conditional on that selection and says nothing about pairs only the high "
                  "setting would flag.")
NON_REPRESENTATIVE_NOTE = ("The 70 sides are the findings chosen because they form flagged pairs; "
                           "they do not represent the extraction as a whole.")


class AnalysisError(ValueError):
    """Safe error: names the failed check, never a row, note, identifier or value."""

    def __init__(self, field):
        self.field = field
        super().__init__("Human analysis validation failed at " + field)


def _check(condition, field):
    if not condition:
        raise AnalysisError(field)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _time(value):
    moment = datetime.fromisoformat(value)
    _check(moment.tzinfo is not None, "timestamp.timezone")
    return moment.timestamp()


# ---------------------------------------------------------------------------
# Inputs


def load_binding(path=BINDING):
    binding = json.loads(Path(path).read_text())
    _check(binding.get("schema_version") == 1, "binding.version")
    segments = binding["instrument_timeline"]["segments"]
    previous = None
    for segment in segments:
        lo, hi = _time(segment["reload_earliest"]), _time(segment["reload_latest"])
        _check(lo <= hi and (previous is None or previous <= lo), "binding.timeline_order")
        previous = hi
    return binding


def load_package(directory, binding):
    directory = Path(directory)
    manifest = json.loads((directory / "items.manifest.json").read_text())
    _check(manifest["package_sha256"] == binding["package"]["package_sha256"], "package.sha256")
    raw = (directory / "items.jsonl").read_bytes()
    _check(_sha(raw) == binding["package"]["file_sha256"], "package.file_sha256")
    items = {}
    for line in raw.decode().splitlines():
        row = json.loads(line)
        _check(row["task"] == "pair" and row["arm"] in ARMS, "package.item")
        items[row["item_id"]] = {"arm": row["arm"]}
    _check(len(items) == binding["package"]["item_count"], "package.count")
    retired = set(binding["package"]["retired_items"])
    _check(retired <= set(items), "package.retired")
    live = {key: value for key, value in items.items() if key not in retired}
    _check(len(live) == binding["package"]["live_pairs"], "package.live")
    return live, manifest.get("supersedes", {})


def _validate_row(row, index):
    _check(isinstance(row, dict) and set(row) == ANSWER_KEYS, "answers.keys")
    _check(row["instrument_version"] == "2.0" and row["pipeline_version"] == "2.0", "answers.version")
    for key in FLAG_KEYS:
        _check(row[key] in (0, 1, True, False), "answers.flags")
    _check(not row["preview"], "answers.preview")
    _check(bool(row["paid"]) != bool(row["surplus"]), "answers.paid_surplus")
    _check(not row["counted"] or (row["paid"] and not row["surplus"] and not row["excluded"]),
           "answers.counted")
    assigned, submitted = _time(row["assigned_at_utc"]), _time(row["submitted_at_utc"])
    _check(assigned <= submitted, "answers.time_order")
    try:
        derived = validate_path(row["path"], row["rationale"])
    except ValueError:
        raise AnalysisError("answers.path") from None
    _check(derived["path"] == row["path"], "answers.canonical_path")
    for key in ("label", "factor", "field_problems", "relevance", "other_detail"):
        _check(derived[key] == row[key], "answers.derivation")
    _check(bool(row["binary_inconsistent"]) == derived["binary_inconsistent"], "answers.binary")
    return {**deepcopy(row), "_order": index, "_assigned": assigned, "_submitted": submitted}


def load_export(directory, *, live, binding):
    """Verify byte hashes and every row; return rows, metadata and corrections."""
    directory = Path(directory)
    answers = (directory / "answers.jsonl").read_bytes()
    metadata_bytes = (directory / "metadata.json").read_bytes()
    corrections_path = directory / "corrections.jsonl"
    corrections_bytes = corrections_path.read_bytes() if corrections_path.exists() else b""
    metadata = json.loads(metadata_bytes)
    _check(metadata.get("answers_sha256") == _sha(answers), "metadata.answers_sha256")
    _check(metadata.get("participant_corrections_sha256") == _sha(corrections_bytes),
           "metadata.corrections_sha256")
    _check(metadata.get("participant_corrections") == corrections_bytes.count(b"\n"),
           "metadata.corrections_count")
    _check(metadata.get("site_pair_package_sha256") == binding["package"]["package_sha256"],
           "metadata.package")
    _check(metadata.get("site_configuration_sha256") == binding["site_configuration_sha256"],
           "metadata.configuration")
    rows = [_validate_row(json.loads(line), index)
            for index, line in enumerate(answers.decode().splitlines()) if line.strip()]
    seen = set()
    for row in rows:
        _check(row["item_id"] in live, "answers.item_not_live")
        key = (row["session_pseudonym"], row["item_id"])
        _check(key not in seen, "answers.duplicate_read")
        seen.add(key)
        _check(row["package_sha256"] == metadata["site_pair_package_sha256"]
               and row["config_sha256"] == metadata["site_configuration_sha256"], "answers.provenance")
        _check(row["session_pseudonym"] in metadata["eligibility"], "metadata.eligibility_keys")
    corrections = [json.loads(line) for line in corrections_bytes.decode().splitlines() if line.strip()]
    provenance = {"answers_sha256": metadata["answers_sha256"],
                  "metadata_sha256": _sha(metadata_bytes),
                  "corrections_sha256": metadata["participant_corrections_sha256"],
                  "snapshot_at": metadata["snapshot_at"], "exported_at": metadata["exported_at"],
                  "collection_closed": metadata["collection_closed"],
                  "site_instrument_sha256_at_export": metadata["site_instrument_sha256"],
                  "site_pair_package_sha256": metadata["site_pair_package_sha256"],
                  "site_configuration_sha256": metadata["site_configuration_sha256"],
                  "rows": len(rows), "sessions": len(metadata["eligibility"])}
    return rows, metadata, corrections, provenance


def apply_corrections(rows, corrections):
    """Primary answers use the applied entry of each chain; originals stay for sensitivity."""
    by_read = {(row["session_pseudonym"], row["read_id"]): row for row in rows}
    applied = {}
    for entry in corrections:
        _check(set(entry) == CORRECTION_KEYS, "corrections.keys")
        key = (entry["session_pseudonym"], entry["read_id"])
        _check(key in by_read and by_read[key]["item_id"] == entry["item_id"], "corrections.read")
        if entry["applied"] is True:
            _check(key not in applied, "corrections.one_applied_per_read")
            applied[key] = entry
    corrected = []
    for row in rows:
        entry = applied.get((row["session_pseudonym"], row["read_id"]))
        if entry is None:
            corrected.append(row)
            continue
        try:
            derived = validate_path(entry["corrected_path"], entry["corrected_rationale"])
        except ValueError:
            raise AnalysisError("corrections.path") from None
        summary = {"label": derived["label"], "factor": derived["factor"],
                   "binary_inconsistent": derived["binary_inconsistent"]}
        _check(summary == {**entry["derived_new"],
                           "binary_inconsistent": bool(entry["derived_new"]["binary_inconsistent"])},
               "corrections.derivation")
        corrected.append({**row, **{k: derived[k] for k in (
            "path", "field_problems", "relevance", "other_detail", "label", "factor",
            "binary_inconsistent", "rationale")}, "_corrected": True})
    return corrected, {"entries": len(corrections), "chains_applied": len(applied),
                       "reads_corrected": len(applied),
                       "label_changed": sum(1 for row, new in zip(rows, corrected)
                                            if row["label"] != new["label"]),
                       "binary_changed": sum(1 for row, new in zip(rows, corrected)
                                             if bool(row["binary_inconsistent"]) != bool(new["binary_inconsistent"]))}


ADDENDUM_08B_SHA256 = "d14d12941e74f40da34978d77f55424e45af9e7c77304e6a438fb968c6149ae4"  # with the Correction
FINAL_08B = "two_judge_final_08b.jsonl"


def oracle_rows_from_08b(run_root, judge, live):
    """Addendum 08B outputs (<run>/two_judge_final_08b.jsonl, chl_rag.oracle_revalidation) -> rows."""
    rows, counts, seen = [], Counter(), set()
    for record in read_jsonl(Path(run_root) / FINAL_08B):
        key = record.get("item_id")
        _check(record.get("judge") == judge and key in live and key not in seen, f"adapter08b.{judge}.identity")
        _check(record.get("addendum_sha256") == ADDENDUM_08B_SHA256, f"adapter08b.{judge}.addendum")
        seen.add(key)
        if record["status"] == "valid":
            _check(isinstance(record.get("annotation"), dict) and record["annotation"].get("item_id") == key,
                   f"adapter08b.{judge}.annotation")
            rows.append({"judge": judge, "item_id": key, "status": "valid", "annotation": record["annotation"]})
            counts["valid_strict" if record["strict_status"] == "valid" else "valid_by_08b"] += 1
        else:
            _check(record["status"] == "invalid", f"adapter08b.{judge}.status")
            rows.append({"judge": judge, "item_id": key, "status": "invalid", "annotation": None})
            counts["invalid"] += 1
    return rows, dict(counts)


def oracle_rows_from_run(run_root, judge, live, *, pair_binding="61c08a1"):
    """Adapter: two-judge run outputs (<root>/items/<item_id>/result.json) -> analysis rows.

    Layout and fields of chl_rag.reference_batches.TwoJudgeRun (branch
    oracle-api-20261116). A valid result gives its decoded `answer` (the 61c08a1
    annotation record); any other final status gives an invalid row; an item with no
    final result (never run, or still open) gives no row and counts as missing.
    """
    root = Path(run_root) / "items"
    present = {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()
    _check(present <= set(live), f"adapter.{judge}.unknown_item")
    rows, counts = [], Counter()
    for key in sorted(live):
        path = root / key / "result.json"
        if not path.exists():
            counts["not_final" if key in present else "not_started"] += 1
            continue
        result = json.loads(path.read_text())
        _check(result.get("judge") == judge and result.get("item_id") == key
               and result.get("task") == "pair", f"adapter.{judge}.identity")
        _check(result.get("pair_binding") == pair_binding, f"adapter.{judge}.binding")
        if result.get("status") == "valid":
            answer = result.get("answer")
            _check(isinstance(answer, dict) and answer.get("item_id") == key, f"adapter.{judge}.answer")
            rows.append({"judge": judge, "item_id": key, "status": "valid", "annotation": answer})
            counts["valid"] += 1
        elif result.get("status") == "not_run":
            counts["not_run"] += 1
        else:
            rows.append({"judge": judge, "item_id": key, "status": "invalid", "annotation": None})
            counts["invalid:" + str(result.get("status"))] += 1
    return rows, dict(counts)


def load_oracle(path, judge, live, *, mode="auto"):
    """Rows {judge, item_id, status: valid|invalid, annotation} from a JSONL file, or from a
    two-judge run directory: mode '08b' reads the addendum-08B outputs, 'strict' the original
    results (amendment 08 as first registered); 'auto' prefers 08B when it exists."""
    labels, invalid, seen = {}, set(), set()
    if Path(path).is_dir():
        if mode == "auto":
            mode = "08b" if (Path(path) / FINAL_08B).exists() else "strict"
        source, adapter = (oracle_rows_from_08b(path, judge, live) if mode == "08b"
                           else oracle_rows_from_run(path, judge, live))
    else:
        _check(mode in ("auto", "rows"), f"oracle.{judge}.mode")
        source, adapter, mode = read_jsonl(path), None, "rows"
    for row in source:
        _check(set(row) == {"judge", "item_id", "status", "annotation"}, f"oracle.{judge}.keys")
        _check(row["judge"] == judge and row["item_id"] in live, f"oracle.{judge}.item")
        _check(row["item_id"] not in seen, f"oracle.{judge}.duplicate")
        seen.add(row["item_id"])
        if row["status"] == "invalid":
            _check(row["annotation"] is None, f"oracle.{judge}.invalid_has_annotation")
            invalid.add(row["item_id"])
            continue
        _check(row["status"] == "valid", f"oracle.{judge}.status")
        note = row["annotation"]
        try:
            derived = validate_path(note["path"], note.get("rationale", ""))
        except (ValueError, KeyError, TypeError):
            raise AnalysisError(f"oracle.{judge}.path") from None
        for key in ("label", "factor", "field_problems", "relevance", "other_detail"):
            _check(note.get(key) == derived[key], f"oracle.{judge}.derivation")
        _check(bool(note.get("binary_inconsistent")) == derived["binary_inconsistent"],
               f"oracle.{judge}.binary")
        labels[row["item_id"]] = {"label": derived["label"], "path": derived["path"], "factor": derived["factor"]}
    validity = {"valid": len(labels), "invalid": len(invalid),
                "missing": len(set(live) - seen), "expected": len(live)}
    validity["validity_rule"] = {"08b": "addendum 08B (coded answers)",
                                 "strict": "amendment 08 as first registered (strict)",
                                 "rows": "as supplied"}[mode]
    if adapter is not None:
        validity["run_statuses"] = adapter
    return labels, validity


def _classification_rows(source):
    """Per-pair JSON records (directory, subdirectories ignored) or one JSONL file."""
    source = Path(source)
    if source.is_dir():
        for file in sorted(source.glob("v2-pair-*.json")):
            yield json.loads(file.read_text())
    else:
        yield from read_jsonl(source)


def load_classification(source, setting, live):
    """Classifier records -> six-way labels of valid judgments; invalid ones are system failures.

    Accepted record: {pair_id, setting, status: valid|invalid, verdict} as written by the
    v2.1 classification run. A pair whose label is invalid is never imputed: it is
    reported as a system failure and left out of precision denominators.
    """
    labels, failures, seen = {}, set(), set()
    for row in _classification_rows(source):
        _check(isinstance(row, dict) and {"pair_id", "status", "verdict"} <= set(row),
               f"classification.{setting}.keys")
        key = row["pair_id"]
        _check(key in live and key not in seen, f"classification.{setting}.item")
        _check(row.get("setting", setting) == setting, f"classification.{setting}.setting")
        seen.add(key)
        if row["status"] == "invalid":
            _check(row["verdict"] is None, f"classification.{setting}.invalid_has_verdict")
            failures.add(key)
            continue
        _check(row["status"] == "valid", f"classification.{setting}.status")
        try:
            Draft202012Validator(CLASSIFICATION).validate(row["verdict"])
            labels[key] = derived_six_label(row["verdict"])
        except (ValueError, TypeError) as exc:
            raise AnalysisError(f"classification.{setting}.judgment") from exc
    return labels, {"valid": len(labels), "system_failures": len(failures),
                    "missing": len(set(live) - seen), "expected": len(live),
                    "rule": "an invalid system label is a system failure: reported, never imputed, "
                            "left out of precision denominators; agreement analyses that do not "
                            "use the system label keep the pair"}


def classification_factors(source, setting, live):
    """Factor of each valid classifier judgment (descriptive factor agreement)."""
    factors = {}
    for row in _classification_rows(source):
        if row.get("pair_id") in live and row.get("status") == "valid" and row.get("verdict"):
            factors[row["pair_id"]] = row["verdict"]["factor"]
    return factors


def earlier_labels(sampling_path, supersedes, live):
    """The v2 system label: the predicted class (stratum) of the superseded pair."""
    sampling = json.loads(Path(sampling_path).read_text())
    by_old = {row["item_id"]: row for row in sampling["items"] if row["task"] == "pair"}
    labels, mapping = {}, {}
    for key, value in live.items():
        old = by_old.get(supersedes.get(key))
        if old is None:
            continue
        arm, predicted = old["stratum"].split("/")
        _check(arm == value["arm"], "sampling.arm")
        labels[key] = predicted
        mapping[key] = {"task": "pair", "arm": arm, "label": predicted,
                        "total_inclusion_probability": old["inclusion_probability"]}
    return labels, mapping, sampling["pair_frames"]


# ---------------------------------------------------------------------------
# Instrument versions


def classify_instrument(row, segments):
    """Segment of the wording a read saw, by the registered split rule."""
    possible = []
    for index, segment in enumerate(segments):
        lo = _time(segment["reload_earliest"])
        start_ok = row["_submitted"] >= lo
        nxt = segments[index + 1] if index + 1 < len(segments) else None
        served = row.get("_started", row["_assigned"])
        end_ok = nxt is None or served < _time(nxt["reload_latest"])
        if start_ok and end_ok:
            possible.append(index)
    if not possible:
        return {"segment": None, "status": "before_first_segment"}
    changed = [i for i, s in enumerate(segments) if s.get("options_changed")]
    if changed and _uses_earlier_options(row["path"]):
        # Options removed at the first options deploy were never offered after it.
        kept = [i for i in possible if i < min(changed)]
        if not kept:
            return {"segment": None, "status": "anomaly_earlier_options_after_deploy"}
        if len(kept) == 1 and len(possible) > 1:
            return {"segment": segments[kept[0]]["id"], "status": "settled_by_options"}
        possible = kept
    if len(possible) == 1:
        return {"segment": segments[possible[0]]["id"], "status": "certain"}
    return {"segment": None, "status": "ambiguous",
            "candidates": [segments[i]["id"] for i in possible]}


def _uses_earlier_options(path):
    q2 = path.get("q2") or {}
    reasons = {value.get("reason") for value in q2.values() if isinstance(value, dict)}
    if reasons - {None} - NEW_COMPARISON_REASONS:
        return True
    if path.get("q1_relevance") == "cannot_establish_comparison":
        core = any(path["q1"][side][field]["answer"] != "yes" for side in SIDES for field in CORE_FIELDS)
        return not core
    return False


def load_started(path, rows):
    """Optional sidecar {assignment_id, started_utc} (metadata only): exact split on started."""
    raw = Path(path).read_bytes()
    started = {}
    for line in raw.decode().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        _check(set(entry) == {"assignment_id", "started_utc"}, "started.keys")
        _check(entry["assignment_id"] not in started, "started.duplicate")
        started[entry["assignment_id"]] = entry["started_utc"]
    fallback = 0
    for row in rows:
        _check(row["read_id"] in started, "started.coverage")
        value = started[row["read_id"]]
        if value is None:
            fallback += 1
            continue
        moment = _time(value)
        _check(row["_assigned"] <= moment <= row["_submitted"], "started.order")
        row["_started"] = moment
    return {"sha256": _sha(raw), "entries": len(started), "rows_without_started": fallback}


def instrument_report(rows, binding, started=None):
    segments = binding["instrument_timeline"]["segments"]
    counts = {segment["id"]: Counter() for segment in segments}
    ambiguous = Counter()
    tags = {}
    for row in rows:
        result = classify_instrument(row, segments)
        tags[(row["session_pseudonym"], row["read_id"])] = result
        if result["segment"]:
            counts[result["segment"]][result["status"]] += 1
        else:
            ambiguous[result["status"] if result["status"] != "ambiguous"
                      else "ambiguous:" + "|".join(result["candidates"])] += 1
    layout = segments[-1]["id"]
    return {"segments": [{"id": s["id"], "instrument_export_sha256": s["instrument_export_sha256"],
                          "reload_earliest": s["reload_earliest"], "reload_latest": s["reload_latest"],
                          "reads": dict(counts[s["id"]])} for s in segments],
            "unassigned": dict(ambiguous),
            "rule": binding["instrument_timeline"]["split_rule"],
            "split_basis": ("assignments.started from the sidecar (exact rule)" if started
                            else "assigned_at_utc as a lower bound of assignments.started"),
            "started_sidecar": started,
            "export_limitation": binding["instrument_timeline"]["export_limitation"],
            "layout_segment": layout}, tags


# ---------------------------------------------------------------------------
# Populations and consensus


def populations(rows, metadata):
    """Primary: counted, paid, not surplus, not excluded, not flagged, eligible.
    Sensitivity: every paid, nonsurplus read of an eligible session."""
    primary, accepted = [], []
    reasons = {"primary": Counter(), "all_accepted": Counter()}
    for row in rows:
        eligible = metadata["eligibility"][row["session_pseudonym"]]["eligible"]
        base = ("eligibility_false" if eligible is False else "eligibility_pending" if eligible is None
                else "surplus_or_unpaid" if not row["paid"] or row["surplus"] else None)
        if base:
            reasons["primary"][base] += 1
            reasons["all_accepted"][base] += 1
            continue
        accepted.append(row)
        reason = ("excluded_session" if row["excluded"] else "quality_failed" if row["quality_failed"]
                  else "not_counted_in_slot" if not row["counted"] else None)
        if reason:
            reasons["primary"][reason] += 1
        else:
            primary.append(row)
    return primary, accepted, {k: dict(v) for k, v in reasons.items()}


def group_reads(reads, live):
    groups = {key: [] for key in live}
    for row in reads:
        groups[row["item_id"]].append(row)
    for group in groups.values():
        group.sort(key=lambda row: (row["_submitted"], row["_order"]))
    return groups


def pair_states(groups, *, cap):
    states, beyond = {}, 0
    for key, group in groups.items():
        labels = [row["label"] for row in group]
        if cap and len(labels) > MAX_READS:
            beyond += len(labels) - MAX_READS
            labels = labels[:MAX_READS]
        n = len(labels)
        label = consensus_label(labels) if n >= 2 else None
        if label is not None:
            needed = next(k for k in range(2, n + 1) if consensus_label(labels[:k]) is not None)
            status = "resolved"
        else:
            needed = None
            status = ("unresolved_final" if n >= MAX_READS else "pending_fourth" if n == 3
                      else "pending_third" if n == 2 else "uncovered")
        states[key] = {"reads": n, "covered": n >= 2, "status": status, "label": label,
                       "reads_needed": needed, "labels": labels}
    return states, beyond


def _rate(successes, n, expected):
    return bounded_rate(successes, n, expected)


def _precision(reference, flagged, live, *, arms=True):
    """Share of system-flagged pairs whose reference is a true flag; missing -> bounds."""
    def one(keys):
        keys = [k for k in keys if k in flagged]
        valid = [k for k in keys if k in reference]
        return _rate(sum(reference[k] in TRUE_FLAGS for k in valid), len(valid), len(keys))
    result = {"overall": one(live)}
    if arms:
        for arm in ARMS:
            result[arm] = {**one([k for k, v in live.items() if v["arm"] == arm]),
                           "descriptive_only": True}
    return result


AGREEMENT_KEYS = ("category_raw_agreement", "category_kappa", "category_ac1",
                  "binary_raw_agreement", "binary_kappa", "binary_ac1")
AGREEMENT_NOTE = ("raw agreement, Cohen's kappa and Gwet's AC1 (Gwet 2008), each with a pair-cluster "
                  "percentile bootstrap; AC1 uses the full scale: six categories, two binary values")


def gwet_ac1(first, second, categories):
    """Gwet's AC1 for two raters over a fixed scale of K categories."""
    n = len(first)
    if not n:
        return None
    _check(set(first) | set(second) <= set(categories), "agreement.scale")
    observed = sum(a == b for a, b in zip(first, second)) / n
    counts = Counter(first) + Counter(second)
    chance = sum((counts[c] / (2 * n)) * (1 - counts[c] / (2 * n)) for c in categories) / (len(categories) - 1)
    return (observed - chance) / (1 - chance) if chance < 1 else None


def _pair_stats(comparisons):
    if not comparisons:
        return dict.fromkeys(AGREEMENT_KEYS)
    first, second = zip(*comparisons)
    category = cohen_kappa(first, second)
    bf, bs = [x in TRUE_FLAGS for x in first], [x in TRUE_FLAGS for x in second]
    binary = cohen_kappa(bf, bs)
    return {"category_raw_agreement": category["agreement"], "category_kappa": category["kappa"],
            "category_ac1": gwet_ac1(first, second, PAIR_LABELS),
            "binary_raw_agreement": binary["agreement"], "binary_kappa": binary["kappa"],
            "binary_ac1": gwet_ac1(bf, bs, (True, False))}


def agreement_groups(pair_comparisons):
    """Like student_validation.oracle_agreement (same resampling and seed), plus Gwet's AC1."""
    groups = [list(group) for group in pair_comparisons.values() if group]
    _check(all(a in PAIR_LABELS and b in PAIR_LABELS for group in groups for a, b in group), "agreement.labels")
    return {"matched_pairs": len(groups), "matched_reads": sum(map(len, groups)),
            "metrics": _bootstrap(groups, lambda sample: _pair_stats([c for g in sample for c in g]),
                                  replicates=REPLICATES, seed=SEED),
            "indices": AGREEMENT_NOTE}


def _agreement(first, second):
    """Matched-pair category and binary agreement: raw, kappa and AC1, pair bootstrap."""
    return agreement_groups({k: [(first[k], second[k])] for k in sorted(set(first) & set(second))})


def q1_agreement(groups):
    """Per-check agreement (agreement_interval) plus Gwet's AC1 on the three answers, same draws."""
    base = agreement_interval(groups, method="pair bootstrap within arm", replicates=REPLICATES, seed=SEED)
    groups = {key: groups[key] for key in sorted(groups) if groups[key]}
    pairs = [pair for units in groups.values() for unit in units for pair in unit]

    def ac1(sample):
        return gwet_ac1([a for a, _ in sample], [b for _, b in sample], Q1_ANSWERS) if sample else None

    rng, values = random.Random(SEED), []
    if pairs:
        for _ in range(REPLICATES):
            draw = [pair for units in groups.values() for unit in rng.choices(units, k=len(units)) for pair in unit]
            values.append(ac1(draw))
    return {**base, "ac1": ac1(pairs),
            "ac1_interval": _interval(values, REPLICATES, SEED, "pair bootstrap within arm")}


STUDY_SAMPLE_NOTE = ("Precision on the study sample: the denominator is the pairs this setting flags among "
                     "the 35 live pairs, a sample stratified on the earlier flags; it is not extrapolated "
                     "to the corpus.")
SETTING_ROLES = {"low": "the system: registered label", "high": "high reasoning effort",
                 "max": "maximum reasoning effort (descriptive)"}


def setting_summaries(settings, validity, consensus, live):
    out = {}
    for name in SETTINGS:
        labels = (settings or {}).get(name)
        if labels is None:
            continue
        flagged = {k for k, v in labels.items() if v in TRUE_FLAGS}
        out[name] = {"role": SETTING_ROLES[name], "flagged_pairs": len(flagged),
                     "invalid_labels": validity[name]["system_failures"],
                     "precision_on_the_study_sample": _precision(consensus, flagged, live),
                     "agreement_with_consensus": _agreement(labels, consensus), "note": STUDY_SAMPLE_NOTE}
    return out


def student_summary(groups, live, states, beyond, system, earlier, design, settings=None, validity=None):
    by_arm = {}
    for arm in ARMS:
        keys = [k for k, v in live.items() if v["arm"] == arm]
        by_arm[arm] = {"pairs": len(keys), "covered": sum(states[k]["covered"] for k in keys),
                       **Counter(states[k]["status"] for k in keys)}
    covered = sum(s["covered"] for s in states.values())
    consensus = {k: s["label"] for k, s in states.items() if s["label"]}
    minimum = {arm: by_arm[arm]["covered"] >= MIN_COVERED_PER_ARM for arm in ARMS}
    result = {
        "pairs_expected": len(live),
        "pair_states": dict(Counter(s["status"] for s in states.values())),
        "covered_pairs": covered, "resolved_pairs": len(consensus), "by_arm": by_arm,
        "reads": sum(len(g) for g in groups.values()),
        "reads_beyond_fourth_ignored": beyond,
        "reads_needed_for_resolution": {str(k): v for k, v in sorted(Counter(
            s["reads_needed"] for s in states.values() if s["reads_needed"]).items())},
        "minimum_sample": {"rule": f"at least {MIN_COVERED_PER_ARM} covered pairs per arm",
                           "met_by_arm": minimum, "met": all(minimum.values()),
                           "status": "meets_minimum" if all(minimum.values()) else "descriptive_only",
                           "per_arm_estimates": "always descriptive"},
        "consensus_categories": dict(Counter(consensus.values())),
        "human_human_first_two_reads": human_agreement(
            {k: [row["label"] for row in g] for k, g in groups.items()}, replicates=REPLICATES, seed=SEED),
    }
    if system is not None:
        flagged = {k for k, v in system.items() if v in TRUE_FLAGS}
        result["system"] = {"label_source": "v2.1 classification, low reasoning (the system)",
                            "flagged_pairs": len(flagged),
                            "system_failures_excluded": len(live) - len(system),
                            "labels": dict(Counter(system.values())),
                            "precision": _precision(consensus, flagged, live),
                            "precision_name": "precision on the study sample",
                            "agreement_with_consensus": _agreement(system, consensus)}
    else:
        result["system"] = {"status": "pending_v21_classification"}
    result["settings"] = setting_summaries(settings, validity, consensus, live)
    if earlier is not None:
        flagged = {k for k, v in earlier.items() if v in TRUE_FLAGS}
        result["earlier_v2_label"] = {
            "label_source": "v2 classification on the extraction before its correction (secondary)",
            "flagged_pairs": len(flagged), "precision": _precision(consensus, flagged, live),
            "agreement_with_consensus": _agreement(earlier, consensus)}
        if design is not None:
            mapping, frames = design
            result["earlier_v2_label"]["design_weighted_precision"] = {
                **design_weighted_precision({k: {"label": v} for k, v in consensus.items()},
                                            mapping, frames, two_stage=True,
                                            replicates=REPLICATES, seed=SEED),
                "note": "Original two-stage inclusion probabilities of the 40-pair subset; 5 of those "
                        "pairs are not in the live package (4 dropped, 1 retired), so this is secondary."}
    return result, consensus


def _majority(values):
    ranked = Counter(values).most_common()
    if ranked and ranked[0][1] >= 2 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
        return ranked[0][0]
    return None


def q1_checks(groups, live, tags, layout_segment):
    expected = 2 * len(live)
    fields = {}
    for field in Q1_FIELDS:
        reads, consensus, layout = Counter(), Counter(), {"before": Counter(), "after": Counter(),
                                                          "unassigned": Counter()}
        correct = with_reference = 0
        for key in live:
            for side in SIDES:
                answers = []
                for row in groups[key]:
                    answer = row["path"]["q1"][side][field]["answer"]
                    answers.append(answer)
                    tag = tags[(row["session_pseudonym"], row["read_id"])]
                    where = ("after" if tag["segment"] == layout_segment else "unassigned"
                             if tag["segment"] is None else "before")
                    layout[where][answer] += 1
                reads.update(answers)
                value = _majority(answers)
                if value is not None:
                    with_reference += 1
                    consensus[value] += 1
                    correct += value == "yes"
        fields[field] = {"correct": _rate(correct, with_reference, expected),
                         "consensus_answers": dict(consensus), "read_answers": dict(reads),
                         "read_answers_by_layout_split": {k: dict(v) for k, v in layout.items()}}
    return {"sides_expected": expected, "reference_rule": "unique majority of at least 2 reads per "
            "side and check, as for categories; 'cannot tell' counts as not correct (strict)",
            "answers": list(Q1_ANSWERS), "fields": fields, "note": NON_REPRESENTATIVE_NOTE}


# ---------------------------------------------------------------------------
# Oracle and classifier comparisons


def reliability(agreement, covered):
    kappa = agreement["metrics"]["binary_kappa"]["estimate"] if agreement["matched_pairs"] else None
    status = ("inconclusive_undefined_kappa" if kappa is None else "unreliable"
              if kappa < KAPPA_THRESHOLD else "threshold_met")
    return {"status": status, "binary_kappa": kappa, "threshold": KAPPA_THRESHOLD,
            "covered_pairs": covered, "resolved_pairs_compared": agreement["matched_pairs"],
            "imprecise": covered < IMPRECISE_BELOW_COVERED,
            "undefined_is_not_a_pass": True}


def oracle_section(judges, validity, consensus, covered, groups, live, system):
    out = {"judges": {}, "validity": validity}
    for judge, labels in judges.items():
        plain = {k: v["label"] for k, v in labels.items()}
        vs = _agreement(plain, consensus)
        reads = {k: [(plain[k], row["label"]) for row in groups[k]] for k in plain if groups[k]}
        out["judges"][judge] = {
            "vs_student_consensus": vs, "reliability": reliability(vs, covered),
            "vs_individual_reads": agreement_groups(reads),
            "alone": {"precision": _precision(plain, {k for k, v in system.items() if v in TRUE_FLAGS},
                                              live) if system else None,
                      "categories": dict(Counter(plain.values())),
                      "role": "secondary; never replaces a less favourable primary result"},
            "q1_yes_rate": {f: _rate(sum(labels[k]["path"]["q1"][s][f]["answer"] == "yes"
                                         for k in labels for s in SIDES), 2 * len(labels), 2 * len(live))
                            for f in Q1_FIELDS}}
    if len(judges) == 2:
        first, second = (judges[j] for j in JUDGES)
        both = sorted(set(first) & set(second))
        out["inter_judge"] = {
            "pairs_both_valid": len(both),
            "pair": _agreement({k: first[k]["label"] for k in both}, {k: second[k]["label"] for k in both}),
            "q1_checks": {f: q1_agreement(
                {arm: [[(first[k]["path"]["q1"][s][f]["answer"], second[k]["path"]["q1"][s][f]["answer"])
                        for s in SIDES] for k in both if live[k]["arm"] == arm] for arm in ARMS})
                for f in Q1_FIELDS}}
        statuses = [out["judges"][j]["reliability"]["status"] for j in JUDGES]
        if all(s == "threshold_met" for s in statuses):
            reference = {k: first[k]["label"] for k in both if first[k]["label"] == second[k]["label"]}
            out["oracle_reference"] = {
                "status": "available", "pairs_with_reference": len(reference),
                "missing_pairs": len(live) - len(reference),
                "categories": dict(Counter(reference.values())),
                "precision": _precision(reference, {k for k, v in system.items() if v in TRUE_FLAGS}, live)
                if system else None,
                "rule": "label on which both judges agree; a disagreement or invalid answer is missing"}
        else:
            reference = None
            out["oracle_reference"] = {"status": "not_available",
                                       "reason": "both judges must meet the reliability rule",
                                       "judge_status": dict(zip(JUDGES, statuses))}
    else:
        reference = None
        out["oracle_reference"] = {"status": "not_available", "reason": "both judges are required"}
    return out, reference


def factor_agreement(groups, consensus, judges, factors, live):
    """Descriptive: on pairs whose student consensus or oracle category is apparent contradiction,
    does each setting's factor match the consensus factor and the oracle factor? Counts only."""
    apparent = "apparent_contradiction"
    student = {}
    for key, label in consensus.items():
        if label == apparent:
            value = _majority([row["factor"] for row in groups[key] if row["label"] == apparent])
            student[key] = value
    oracle, oracle_apparent = {}, set()
    if set(judges) == set(JUDGES):
        a, b = judges["astra"], judges["fable"]
        for key in sorted(set(a) & set(b)):
            if a[key]["label"] == b[key]["label"] == apparent:
                oracle_apparent.add(key)
                if a[key]["factor"] == b[key]["factor"]:
                    oracle[key] = a[key]["factor"]
    student_factor = {k: v for k, v in student.items() if v is not None}

    def matches(reference):
        return {name: sum(values.get(k) == f for k, f in reference.items())
                for name, values in factors.items()}

    return {"pairs_considered": len(set(student) | oracle_apparent),
            "student_consensus": {"apparent_pairs": len(student), "with_consensus_factor": len(student_factor),
                                  "factor_rule": "unique majority of at least 2 among the reads in the consensus "
                                                 "category", "matches": matches(student_factor)},
            "oracle": {"apparent_pairs": len(oracle_apparent), "with_agreed_factor": len(oracle),
                       "basis": "pairs where Astra and Fable agree on apparent contradiction (validity as "
                                "supplied, addendum 08B for run outputs); the factor counts only where they "
                                "also agree on it", "matches": matches(oracle)},
            "invalid_or_other_category_counts_as_mismatch": True,
            "status": "descriptive_only"}


def classifier_comparison(settings, consensus, reference, judges):
    if not {"low", "high"} <= set(settings):
        return {"status": "pending_low_and_high_classifications", "note": SELECTION_NOTE}
    low, high = settings["low"], settings["high"]
    references = {"student_consensus": consensus}
    if reference is not None:
        references["oracle_reference"] = reference
    for judge, labels in judges.items():
        references[judge] = {k: v["label"] for k, v in labels.items()}
    per_setting = {name: {ref: _agreement(labels, values) for ref, values in references.items()}
                   for name, labels in settings.items()}
    both = sorted(set(low) & set(high))
    category = [k for k in both if low[k] != high[k]]
    binary = [k for k in both if (low[k] in TRUE_FLAGS) != (high[k] in TRUE_FLAGS)]

    def sides(keys, *, binary_level):
        result = {}
        for ref, values in references.items():
            counts = Counter()
            for k in keys:
                if k not in values:
                    counts["no_reference"] += 1
                    continue
                v = values[k]
                if binary_level:
                    v, lo, hi = v in TRUE_FLAGS, low[k] in TRUE_FLAGS, high[k] in TRUE_FLAGS
                else:
                    lo, hi = low[k], high[k]
                counts["low" if v == lo else "high" if v == hi else "neither"] += 1
            result[ref] = dict(counts)
        return result

    return {"status": "descriptive_only", "pairs_both_classified": len(both),
            "agreement": per_setting,
            "differ": {"category": len(category), "binary": len(binary),
                       "reference_sides_category": sides(category, binary_level=False),
                       "reference_sides_binary": sides(binary, binary_level=True)},
            "flagged": {name: sum(v in TRUE_FLAGS for v in labels.values()) for name, labels in settings.items()},
            "note": SELECTION_NOTE}


# ---------------------------------------------------------------------------
# Run


def _public_sanity(value):
    text = json.dumps(value)
    for forbidden in ("session_pseudonym", "rationale\":", "read_id"):
        _check(forbidden not in text, "public.privacy")


PROGRAMME_NAMES = {"medicine_and_surgery": "Medicine and surgery", "biomedical_engineering": "Biomedical engineering"}


def _programme_name(key):
    return PROGRAMME_NAMES.get(key, key.replace("_", " ").capitalize())


def participants_summary(primary_rows, demographics):
    """Aggregate participant counts for the report. Exact counts: by Antonio's decision of
    16 November no programme group is merged or suppressed."""
    per_session = Counter(row["session_pseudonym"] for row in primary_rows)
    n = len(per_session)
    reads = sorted(per_session.values())
    per = None
    if n:
        mid = n // 2
        median = reads[mid] if n % 2 else (reads[mid - 1] + reads[mid]) / 2
        per = {"median": median, "min": reads[0], "max": reads[-1]}
    programmes = (demographics or {}).get("programme") or {}
    groups = [{"group": _programme_name(k), "n": v}
              for k, v in sorted(programmes.items(), key=lambda kv: (-kv[1], kv[0])) if v]
    return {"population": "sessions with at least one valid read in the primary population",
            "with_valid_read": n,
            "reads_per_participant": per,
            "programme_population": (demographics or {}).get("population"),
            "programme_sessions": (demographics or {}).get("sessions"),
            "programme_groups": {"groups": groups},
            "programme_missing": (demographics or {}).get("programme_missing"),
            "cell_rule": "exact counts, no merging or suppression (Antonio, 16 November)"}


def analyse(export_dir, *, binding_path=BINDING, package_dir=None, oracles=None,
            classifications=None, sampling_path=None, started_path=None, oracles_strict=None):
    binding = load_binding(binding_path)
    live, supersedes = load_package(package_dir or binding["package"]["directory"], binding)
    rows, metadata, corrections, provenance = load_export(export_dir, live=live, binding=binding)
    start = _time(binding["recruited_collection_start"])
    _check(all(row["_submitted"] >= start for row in rows), "answers.before_recruited_collection")
    started = load_started(started_path, rows) if started_path else None
    corrected, correction_counts = apply_corrections(rows, corrections)
    instrument, tags = instrument_report(rows, binding, started)
    system, system_validity, settings, factors = None, {}, {}, {}
    for name, source in (classifications or {}).items():
        _check(name in SETTINGS, "classification.setting")
        settings[name], system_validity[name] = load_classification(source, name, live)
        factors[name] = classification_factors(source, name, live)
    system = settings.get("low")
    earlier = design = None
    if sampling_path:
        earlier, mapping, frames = earlier_labels(sampling_path, supersedes, live)
        design = (mapping, frames)
    variants = {}
    variants_rows = {}
    private = {}
    for name, answer_rows in (("corrected", corrected), ("original", rows)):
        primary, accepted, reasons = populations(answer_rows, metadata)
        variants_rows[f"primary_{name}"] = primary
        for population, reads, cap in (("primary", primary, True), ("all_accepted", accepted, False)):
            groups = group_reads(reads, live)
            states, beyond = pair_states(groups, cap=cap)
            summary, consensus = student_summary(groups, live, states, beyond, system, earlier, design,
                                                 settings, system_validity)
            summary["exclusions"] = reasons[population]
            variants[f"{population}_{name}"] = (summary, consensus, groups, states)
    summary, consensus, groups, states = variants["primary_corrected"]
    covered = summary["covered_pairs"]
    q1 = q1_checks(groups, live, tags, instrument["layout_segment"])
    judges, validity, strict, strict_validity = {}, {}, {}, {}
    for judge, source in (oracles or {}).items():
        _check(judge in JUDGES, "oracle.judge")
        judges[judge], validity[judge] = load_oracle(source, judge, live)
        if (oracles_strict or {}).get(judge):
            strict[judge], strict_validity[judge] = load_oracle(oracles_strict[judge], judge, live)
        elif Path(source).is_dir() and (Path(source) / FINAL_08B).exists():
            strict[judge], strict_validity[judge] = load_oracle(source, judge, live, mode="strict")
    if judges:
        oracle, reference = oracle_section(judges, validity, consensus, covered, groups, live, system or {})
    else:
        oracle, reference = {"status": "pending_oracle_outputs"}, None
    if strict and set(strict) == set(judges):
        sensitivity, _ = oracle_section(strict, strict_validity, consensus, covered, groups, live, system or {})
        sensitivity["rule"] = ("amendment 08 as first registered: an answer with free text where the page has "
                               "no box, or over 200 characters, is invalid (addendum 08B, rule 4)")
    else:
        sensitivity = {"status": "not_applicable",
                       "reason": "no strict outputs supplied next to an addendum-08B primary"}
    comparison = classifier_comparison(settings, consensus, reference, judges)
    factor = factor_agreement(groups, consensus, judges, factors, live)
    result = {
        "schema_version": 1, "generated_at": utcnow(),
        "status": "final_snapshot" if metadata["collection_closed"] else "interim_snapshot",
        "seed": SEED, "replicates": REPLICATES,
        "provenance": {**provenance, "binding_sha256": _sha(Path(binding_path).read_bytes()),
                       "package_sha256": binding["package"]["package_sha256"]},
        "package": {"live_pairs": len(live), "retired": sorted(binding["package"]["retired_items"]),
                    "dropped_before_collection": len(binding["package"]["dropped_before_collection"]),
                    "by_arm": dict(Counter(v["arm"] for v in live.values()))},
        "demographics": metadata.get("demographics"),
        "participants": participants_summary(variants_rows["primary_corrected"], metadata.get("demographics")),
        "corrections": correction_counts,
        "instrument_versions": instrument,
        "students": {"primary": summary,
                     "sensitivity_original_answers": variants["primary_original"][0],
                     "sensitivity_all_accepted_reads": variants["all_accepted_corrected"][0],
                     "rules": {"covered": "at least 2 valid filtered reads",
                               "consensus": "unique majority of at least 2 votes",
                               "reads": "third after a nominal disagreement, fourth after a three-way "
                                        "split, no fifth; unresolved after four stays unresolved",
                               "author_fallback": "withdrawn; no author labels (amendment 07)"}},
        "q1_checks": q1,
        "classification_validity": system_validity,
        "oracle": oracle,
        "oracle_sensitivity_strict": sensitivity,
        "classifier_effort_comparison": comparison,
        "factor_agreement": factor,
        "limitations": [NON_REPRESENTATIVE_NOTE, SELECTION_NOTE,
                        binding["instrument_timeline"]["export_limitation"],
                        ("Intervals condition on observed eligibility and do not model readers shared "
                         "across pairs, informative missingness or the adaptive assignment.")]}
    _public_sanity(result)
    private = {"pairs": {k: {"arm": live[k]["arm"], "status": states[k]["status"],
                             "consensus": states[k]["label"], "system_low": (system or {}).get(k),
                             "system_high": settings.get("high", {}).get(k),
                             "earlier_v2": (earlier or {}).get(k),
                             **{j: judges[j].get(k, {}).get("label") for j in judges}}
                         for k in sorted(live)},
               "privacy": "Pair-level references; private local file, never sent to a model."}
    return result, private


def validate_result(result, schema_path=SCHEMA):
    errors = list(Draft202012Validator(json.loads(Path(schema_path).read_text())).iter_errors(result))
    _check(not errors, "results.schema:" + (".".join(map(str, errors[0].absolute_path)) if errors else ""))


def _num(value, digits=3):
    return r"\pending{}" if value is None else f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def macros(result):
    s = result["students"]["primary"]
    m = {"HAStatus": tex("Final snapshot" if result["status"] == "final_snapshot" else "Interim snapshot"),
         "HASnapshotAt": tex(result["provenance"]["snapshot_at"][:16].replace("T", " ")),
         "HASnapshotTime": tex(_snapshot_time(result["provenance"]["snapshot_at"])),
         "HALivePairs": _num(result["package"]["live_pairs"]),
         "HAReads": _num(s["reads"]), "HACoveredPairs": _num(s["covered_pairs"]),
         "HAResolvedPairs": _num(s["resolved_pairs"]),
         "HAUnresolvedFinalPairs": _num(s["pair_states"].get("unresolved_final", 0)),
         "HAPendingPairs": _num(s["pair_states"].get("pending_third", 0) + s["pair_states"].get("pending_fourth", 0)),
         "HAUncoveredPairs": _num(s["pair_states"].get("uncovered", 0)),
         "HAMinimumSample": tex("met" if s["minimum_sample"]["met"] else "not met (descriptive only)"),
         "HACorrectedReads": _num(result["corrections"]["reads_corrected"]),
         "HAHumanCategoryAgreement": _num(s["human_human_first_two_reads"]["metrics"]["category_raw_agreement"]["estimate"]),
         "HAHumanCategoryAlpha": _num(s["human_human_first_two_reads"]["metrics"]["category_alpha"]["estimate"]),
         "HAHumanBinaryAlpha": _num(s["human_human_first_two_reads"]["metrics"]["binary_alpha"]["estimate"])}
    system = s.get("system", {})
    if "precision" in system:
        p = system["precision"]["overall"]
        m.update(HASystemFlagged=_num(system["flagged_pairs"]), HAPrecisionStudents=_num(p["estimate"]),
                 HAPrecisionStudentsN=_num(p["n"]),
                 HAPrecisionStudentsBounds=tex("[" + ", ".join(f"{x:.3f}" for x in p["missing_reference_bounds"]) + "]")
                 if p["missing_reference_bounds"] else r"\pending{}")
    oracle = result["oracle"]
    for judge, name in (("astra", "Astra"), ("fable", "Fable")):
        r = oracle.get("judges", {}).get(judge, {}).get("reliability")
        m[f"HA{name}Kappa"] = _num(r["binary_kappa"]) if r else r"\pending{}"
        m[f"HA{name}Reliability"] = tex(r["status"].replace("_", " ")) if r else r"\pending{}"
    inter = oracle.get("inter_judge")
    m["HAInterJudgeCategoryAgreement"] = _num(inter["pair"]["metrics"]["category_raw_agreement"]["estimate"]) if inter else r"\pending{}"
    m["HAInterJudgeBinaryKappa"] = _num(inter["pair"]["metrics"]["binary_kappa"]["estimate"]) if inter else r"\pending{}"
    ref = oracle.get("oracle_reference", {})
    m["HAOracleReference"] = tex(ref.get("status", "pending").replace("_", " "))
    comp = result["classifier_effort_comparison"]
    m["HALowHighDifferCategory"] = _num(comp["differ"]["category"]) if "differ" in comp else r"\pending{}"
    m["HALowHighDifferBinary"] = _num(comp["differ"]["binary"]) if "differ" in comp else r"\pending{}"
    m.update(_report_macros(result))
    m.update(_oracle_macros(result["oracle"], ""))
    m.update(_oracle_macros(result.get("oracle_sensitivity_strict") or {}, "Strict"))
    m.update(_addition_macros(result))
    return "".join("\\providecommand{\\" + k + "}{" + v + "}\n" for k, v in m.items())


def _snapshot_time(value):
    """Analysis snapshot instant for the report, in CET: e.g. '16 November 2026, 11:00 CET'."""
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is not None:
        from datetime import timedelta, timezone
        when = when.astimezone(timezone(timedelta(hours=1)))
    return f"{when.day} {when.strftime('%B %Y, %H:%M')} CET"


def _ci(interval, digits=2):
    """'[lo, hi]' for a bootstrap or Wilson interval; a visible placeholder when undefined."""
    ci = interval.get("ci95") if isinstance(interval, dict) else interval
    if not ci or any(x is None for x in ci):
        return r"\pending{}"
    return tex("[" + ", ".join(f"{x:.{digits}f}" for x in ci) + "]")


def _metric(block, name, digits=2):
    metric = (block or {}).get("metrics", {}).get(name)
    if not metric or metric.get("estimate") is None:
        return r"\pending{}", r"\pending{}"
    return f"{metric['estimate']:.{digits}f}", _ci(metric.get("bootstrap", {}), digits)


Q1_MACRO_NAMES = {"result": "Result", "comparison": "Comparison", "significance": "Significance",
                  "analysis": "Analysis", "groups": "Groups", "sample_size": "SampleSize", "context": "Context"}


def _report_macros(result):
    """Additional report values (16 November): intervals, per-check correctness, low/high agreement.
    Every value comes from the public result; undefined values print the visible \\pending{} marker."""
    s = result["students"]["primary"]
    m = {"HAPairsExpected": _num(s.get("pairs_expected")),
         "HAReadsNeeded": tex(", ".join(f"{k} for {v} pair{'s' if v != 1 else ''}" for k, v in sorted(s.get("reads_needed_for_resolution", {}).items())))
         if s.get("reads_needed_for_resolution") else r"\pending{}"}
    for arm, name in (("biomarker", "Biomarker"), ("drug", "Drug"), ("diet", "Diet")):
        row = s.get("by_arm", {}).get(arm, {})
        m[f"HACovered{name}"] = _num(row.get("covered"))
        m[f"HAResolved{name}"] = _num(row.get("resolved"))
    for arm, name in (("biomarker", "Biomarker"), ("drug", "Drug"), ("diet", "Diet")):
        row = s.get("by_arm", {}).get(arm, {})
        m[f"HAPairs{name}"] = _num(row.get("pairs"))
        m[f"HAUncovered{name}"] = _num(row.get("uncovered", 0))
        m[f"HAUnresolved{name}"] = _num(row.get("unresolved_final", 0))
        m[f"HAPending{name}"] = _num(row.get("pending_third", 0) + row.get("pending_fourth", 0))
    part = result.get("participants") or {}
    m["HAParticipants"] = _num(part.get("with_valid_read"))
    per = part.get("reads_per_participant")
    m["HAReadsPerParticipantMedian"] = (f"{per['median']:g}" if per else r"\pending{}")
    m["HAReadsPerParticipantRange"] = (tex(f"{per['min']}--{per['max']}") if per else r"\pending{}")
    groups = (part.get("programme_groups") or {}).get("groups") or []
    m["HAProgrammeSessions"] = _num(part.get("programme_sessions"))
    m["HAProgrammeGroups"] = (tex("; ".join(f"{g['group']} {g['n']}" for g in groups)) if groups else r"\pending{}")
    m["HAProgrammeRows"] = ("".join(tex(g["group"]) + " & " + str(g["n"]) + r"\\" for g in groups)
                            if groups else r"\pending{} & \\")
    human = s["human_human_first_two_reads"]
    m["HAHumanPairs"] = _num(human.get("pairs_with_two_reads"))
    system = s.get("system", {})
    m["HASystemFailures"] = _num(system.get("system_failures_excluded"))
    overall = system.get("precision", {}).get("overall", {})
    m["HAPrecisionStudentsCI"] = _ci(overall.get("wilson95"))
    agree = system.get("agreement_with_consensus")
    m["HASystemBinaryKappa"], m["HASystemBinaryKappaCI"] = _metric(agree, "binary_kappa")
    q1 = result.get("q1_checks", {}).get("fields", {})
    for field, name in Q1_MACRO_NAMES.items():
        correct = q1.get(field, {}).get("correct", {})
        m[f"HAQOne{name}"] = (tex(f"{correct['successes']}/{correct['n']}") if correct.get("n") else r"\pending{}")
    oracle = result["oracle"]
    for judge, name in (("astra", "Astra"), ("fable", "Fable")):
        j = oracle.get("judges", {}).get(judge, {})
        _, m[f"HA{name}KappaCI"] = _metric(j.get("vs_student_consensus"), "binary_kappa")
        rel = j.get("reliability") or {}
        m[f"HA{name}ComparedPairs"] = _num(rel.get("resolved_pairs_compared"))
        valid = oracle.get("validity", {}).get(judge, {})
        m[f"HA{name}Valid"] = _num(valid.get("valid")) if isinstance(valid, dict) else r"\pending{}"
    inter = oracle.get("inter_judge") or {}
    m["HAInterJudgePairs"] = _num(inter.get("pairs_both_valid"))
    _, m["HAInterJudgeBinaryKappaCI"] = _metric(inter.get("pair"), "binary_kappa")
    m["HAInterJudgeCategoryKappa"], m["HAInterJudgeCategoryKappaCI"] = _metric(inter.get("pair"), "category_kappa")
    comp = result["classifier_effort_comparison"]
    for setting, sname in (("low", "Low"), ("high", "High"), ("max", "Max")):
        if setting not in comp.get("flagged", {}) and setting == "max":
            continue  # maximum effort is optional; its macros then come from the 08C decision
        m[f"HA{sname}Flagged"] = _num(comp.get("flagged", {}).get(setting))
        for ref, rname in (("student_consensus", "Students"), ("astra", "Astra"), ("fable", "Fable")):
            est, _ = _metric(comp.get("agreement", {}).get(setting, {}).get(ref), "binary_raw_agreement")
            m[f"HA{sname}BinaryAgree{rname}"] = est
    return m


def _oracle_macros(oracle, suffix):
    """Oracle values for one validity rule. suffix '' = addendum 08B (primary): only the values not
    defined above; 'Strict' = amendment 08 as first registered (sensitivity): the full set."""
    m = {}
    judges = oracle.get("judges", {}) if isinstance(oracle, dict) else {}
    inter = oracle.get("inter_judge") or {} if isinstance(oracle, dict) else {}
    for judge, name in (("astra", "Astra"), ("fable", "Fable")):
        j = judges.get(judge, {})
        rel = j.get("reliability") or {}
        m[f"HA{name}BinaryAgreement{suffix}"], m[f"HA{name}BinaryAgreementCI{suffix}"] = _metric(
            j.get("vs_student_consensus"), "binary_raw_agreement")
        m[f"HA{name}ACOne{suffix}"], m[f"HA{name}ACOneCI{suffix}"] = _metric(
            j.get("vs_student_consensus"), "binary_ac1")
        if suffix:
            valid = (oracle.get("validity") or {}).get(judge) or {}
            m[f"HA{name}Valid{suffix}"] = _num(valid.get("valid")) if valid else r"\pending{}"
            m[f"HA{name}Kappa{suffix}"] = _num(rel.get("binary_kappa")) if rel else r"\pending{}"
            _, m[f"HA{name}KappaCI{suffix}"] = _metric(j.get("vs_student_consensus"), "binary_kappa")
            m[f"HA{name}Reliability{suffix}"] = (tex(rel["status"].replace("_", " ")) if rel
                                                 else r"\pending{}")
            m[f"HA{name}ComparedPairs{suffix}"] = _num(rel.get("resolved_pairs_compared"))
    m[f"HAInterJudgeBinaryAgreement{suffix}"], _ = _metric(inter.get("pair"), "binary_raw_agreement")
    m[f"HAInterJudgeBinaryACOne{suffix}"], m[f"HAInterJudgeBinaryACOneCI{suffix}"] = _metric(
        inter.get("pair"), "binary_ac1")
    m[f"HAInterJudgeCategoryACOne{suffix}"], m[f"HAInterJudgeCategoryACOneCI{suffix}"] = _metric(
        inter.get("pair"), "category_ac1")
    if suffix:
        m[f"HAInterJudgePairs{suffix}"] = _num(inter.get("pairs_both_valid"))
        m[f"HAInterJudgeCategoryAgreement{suffix}"], _ = _metric(inter.get("pair"), "category_raw_agreement", 3)
        m[f"HAInterJudgeBinaryKappa{suffix}"], m[f"HAInterJudgeBinaryKappaCI{suffix}"] = _metric(
            inter.get("pair"), "binary_kappa", 3)
        m[f"HAInterJudgeCategoryKappa{suffix}"], m[f"HAInterJudgeCategoryKappaCI{suffix}"] = _metric(
            inter.get("pair"), "category_kappa")
        reference = oracle.get("oracle_reference", {}) if isinstance(oracle, dict) else {}
        m[f"HAOracleReference{suffix}"] = (tex(reference["status"].replace("_", " "))
                                           if reference.get("status") else r"\pending{}")
    return m


SETTING_MACRO_NAMES = {"low": "Low", "high": "High", "max": "Max"}


def _addition_macros(result):
    """Per-setting precision on the study sample and agreement with the students; AC1 next to the
    system kappa; descriptive factor agreement (16 November additions)."""
    m = {}
    s = result["students"]["primary"]
    for setting, name in SETTING_MACRO_NAMES.items():
        block = s.get("settings", {}).get(setting) or {}
        overall = (block.get("precision_on_the_study_sample") or {}).get("overall") or {}
        agree = block.get("agreement_with_consensus")
        m[f"HA{name}FlaggedDenominator"] = _num(block.get("flagged_pairs"))
        m[f"HA{name}Invalid"] = _num(block.get("invalid_labels"))
        m[f"HA{name}PrecisionStudents"] = _num(overall.get("estimate"))
        m[f"HA{name}PrecisionStudentsN"] = _num(overall.get("n"))
        m[f"HA{name}PrecisionStudentsCI"] = _ci(overall.get("wilson95"))
        bounds = overall.get("missing_reference_bounds")
        m[f"HA{name}PrecisionStudentsBounds"] = (tex("[" + ", ".join(f"{x:.3f}" for x in bounds) + "]")
                                                 if bounds else r"\pending{}")
        for metric, label in (("binary_kappa", "BinaryKappa"), ("binary_ac1", "BinaryACOne"),
                              ("category_raw_agreement", "CategoryAgree"), ("category_kappa", "CategoryKappa"),
                              ("category_ac1", "CategoryACOne")):
            m[f"HA{name}{label}Students"], m[f"HA{name}{label}StudentsCI"] = _metric(agree, metric)
    system = s.get("system", {}).get("agreement_with_consensus")
    m["HASystemBinaryAgreement"], m["HASystemBinaryAgreementCI"] = _metric(system, "binary_raw_agreement")
    m["HASystemBinaryACOne"], m["HASystemBinaryACOneCI"] = _metric(system, "binary_ac1")
    factor = result.get("factor_agreement") or {}
    m["HAFactorPairs"] = _num(factor.get("pairs_considered"))
    for source, label, size in (("student_consensus", "Students", "with_consensus_factor"),
                                ("oracle", "Oracle", "with_agreed_factor")):
        block = factor.get(source) or {}
        m[f"HAFactor{label}Pairs"] = _num(block.get(size))
        for setting, name in SETTING_MACRO_NAMES.items():
            m[f"HAFactor{label}Match{name}"] = _num((block.get("matches") or {}).get(setting))
    return m


def _private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=1, sort_keys=True)


def _pairs(values, option):
    out = {}
    for value in values or ():
        name, _, path = value.partition("=")
        _check(name and path, option)
        out[name] = path
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--export", required=True, help="export-v2 directory (private)")
    parser.add_argument("--binding", default=BINDING)
    parser.add_argument("--package", default=None)
    parser.add_argument("--oracle", action="append",
                        help="judge=PATH; PATH is a rows JSONL or a two-judge run directory; repeat")
    parser.add_argument("--oracle-strict", action="append",
                        help="judge=PATH: strict rows for the sensitivity analysis; a run directory "
                             "with addendum-08B outputs supplies them automatically")
    parser.add_argument("--assignment-started", default=None,
                        help="optional JSONL {assignment_id, started_utc}: exact instrument split")
    parser.add_argument("--classification", action="append", help="low=PATH, high=PATH, optionally max=PATH (addendum 08C); repeat")
    parser.add_argument("--sampling", default="data/derived/human_subset_sampling_private.json")
    parser.add_argument("--output", default="results/human_analysis.json")
    parser.add_argument("--private-dir", default="data/raw/human_private")
    parser.add_argument("--macros", default="report/human_macros.tex")
    args = parser.parse_args(argv)
    try:
        result, private = analyse(args.export, binding_path=args.binding, package_dir=args.package,
                                  oracles=_pairs(args.oracle, "cli.oracle"),
                                  classifications=_pairs(args.classification, "cli.classification"),
                                  sampling_path=args.sampling if args.sampling and Path(args.sampling).exists() else None,
                                  started_path=args.assignment_started,
                                  oracles_strict=_pairs(args.oracle_strict, "cli.oracle_strict"))
        validate_result(result)
    except AnalysisError as exc:
        print(json.dumps({"status": "validation_error", "field": exc.field}))
        raise SystemExit(2) from None
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    _private_write(Path(args.private_dir) / "pairs.json", private)
    Path(args.macros).parent.mkdir(parents=True, exist_ok=True)
    Path(args.macros).write_text(macros(result))
    s = result["students"]["primary"]
    print(json.dumps({"status": result["status"], "reads": s["reads"], "covered_pairs": s["covered_pairs"],
                      "resolved_pairs": s["resolved_pairs"], "output": args.output}))


if __name__ == "__main__":
    main()
