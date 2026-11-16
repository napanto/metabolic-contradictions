"""Amendment 04 judge-referenced analysis; never substitutes an LLM for Antonio.

This module intentionally leaves the original human-reference evaluator untouched.
All intervals are descriptive; missing-reference bounds are sensitivity bounds,
not confidence intervals. Implement and test this module using synthetic labels.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from .annotation import ARMS, FIELDS, validate_label
from .io import digest, file_sha, read_jsonl, utcnow, write_json
from .metrics import cohen_kappa, wilson

TRUE_FLAGS = {"contradictory", "apparent_contradiction", "not_replicated"}
ORACLE_NAME = "GPT-6 Astra isolated oracle"
SECONDARY_NAME = "Independent Codex LLM"
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 20261115


def _index(rows, description):
    result = {}
    for row in rows:
        key = row.get("item_id")
        if not isinstance(key, str) or key in result:
            raise ValueError(f"Invalid or duplicate item ID in {description}")
        result[key] = row
    return result


def _close(actual, expected, description):
    if (not isinstance(actual, (float, int)) or isinstance(actual, bool)
            or not math.isfinite(actual) or not math.isclose(actual, expected, rel_tol=1e-10)):
        raise ValueError(f"Invalid {description}")


def validate_design(rows, manifest, *, parent=None, parent_manifest=None):
    """Check public/private binding and frozen first/two-stage sampling weights."""
    if not rows:
        raise ValueError("Empty annotation package")
    items = _index(rows, "package")
    if manifest.get("blind_items_sha256") != digest(rows):
        raise ValueError("Package hash differs from sampling manifest")
    mapping = _index(manifest["items"], "sampling manifest")
    if set(mapping) != set(items):
        raise ValueError("Sampling manifest IDs differ from package")
    full_mapping = _index(parent_manifest["items"], "parent manifest") if parent else None
    if parent and manifest.get("parent_package_sha256") != digest(list(parent.values())):
        raise ValueError("Human manifest parent package hash mismatch")
    counts = Counter()
    abstract_counts = Counter()
    for key, item in items.items():
        meta = mapping[key]
        if item["task"] not in {"pair", "extraction"} or item["arm"] not in ARMS:
            raise ValueError("Unsupported package task/arm")
        if any(meta.get(field) != item[field] for field in ("task", "arm")):
            raise ValueError("Sampling manifest item task/arm mismatch")
        if parent and (key not in parent or parent[key] != item):
            raise ValueError("Human package item differs from frozen parent")
        if item["task"] == "pair":
            if meta.get("label") not in TRUE_FLAGS or not meta.get("pair_id"):
                raise ValueError("Pair sampling metadata must identify its original stratum")
            counts[f"{meta['arm']}/{meta['label']}"] += 1
            if parent and any(meta.get(field) != full_mapping[key].get(field)
                              for field in ("pair_id", "label")):
                raise ValueError("Human pair stratum differs from parent")
        else:
            abstract_counts[item["arm"]] += 1
            claim_ids = [c["claim_id"] for c in item["claims"]]
            if len(claim_ids) != len(set(claim_ids)) or not claim_ids:
                raise ValueError("Extraction package requires distinct nonempty claims")
    frames = manifest["pair_frames"]
    if set(counts) - set(frames):
        raise ValueError("Pair stratum missing from manifest frames")
    if parent and set(frames) != set(parent_manifest["pair_frames"]):
        raise ValueError("Human sampling frame differs from parent")
    for key, frame in frames.items():
        n, population = frame["selected"], frame["frame_count"]
        if (not isinstance(n, int) or not isinstance(population, int)
                or not 0 <= n <= population or n != counts[key]
                or key != f"{frame['arm']}/{frame['predicted_class']}"):
            raise ValueError("Invalid pair frame counts/stratum")
        if "inclusion_probability" in frame and population and not parent:
            _close(frame["inclusion_probability"], n / population,
                   f"frame inclusion probability for {key}")
        original = parent_manifest["pair_frames"][key] if parent else frame
        if parent and (population != original["frame_count"]
                       or frame.get("original_selected") != original["selected"]):
            raise ValueError("Human pair frame does not retain original sampling counts")
        for item_id, meta in mapping.items():
            if meta["task"] != "pair" or key != f"{meta['arm']}/{meta['label']}":
                continue
            if not population or not original["selected"]:
                raise ValueError("Sampled pair has an empty population frame")
            first = original["selected"] / population
            if parent:
                second = n / original["selected"]
                for field, expected in (("first_stage_probability", first),
                                        ("second_stage_probability", second),
                                        ("total_inclusion_probability", first * second)):
                    _close(meta.get(field), expected, f"{field} for {item_id}")
            else:
                _close(meta.get("inclusion_probability"), first,
                       f"inclusion probability for {item_id}")
    if parent:
        for item_id, meta in mapping.items():
            if meta["task"] != "extraction":
                continue
            arm = meta["arm"]
            frame = parent_manifest["extraction_frames"][arm]
            first = frame["selected"] / frame["frame_count"]
            second = abstract_counts[arm] / frame["selected"]
            for field, expected in (("first_stage_probability", first),
                                    ("second_stage_probability", second),
                                    ("total_inclusion_probability", first * second)):
                _close(meta.get(field), expected, f"{field} for {item_id}")
    return items, mapping


def load_labels(path, items, *, role, effort=None):
    """Validate every revision. Wrong identity/binding is fatal, invalid oracle is missing."""
    labels, invalid = {}, {}
    package_hash = digest(list(items.values()))
    revisions = {}
    expected = {"human": "Antonio", "oracle": ORACLE_NAME, "secondary": SECONDARY_NAME}[role]
    if not Path(path).exists():
        return labels, invalid
    for row in read_jsonl(path):
        key = row.get("item_id")
        if key not in items:
            raise ValueError(f"Unknown {role} annotation item ID")
        if row.get("annotator") != expected:
            raise ValueError(f"Invalid {role} annotator identity")
        if role == "human":
            if row.get("annotator_type") not in (None, "human"):
                raise ValueError("Human labels must be Antonio's manual judgments, never LLM labels")
        elif row.get("annotator_type") != "LLM":
            raise ValueError(f"The {role} annotator must be explicitly identified as LLM")
        if row.get("package_sha256") != package_hash:
            raise ValueError(f"The {role} labels refer to a different frozen package hash")
        if role == "oracle":
            if key in revisions:
                raise ValueError("Oracle labels must have one primary output per item")
            if row.get("model", "gpt-6-astra") != "gpt-6-astra":
                raise ValueError("Unexpected primary oracle model")
            if effort and row.get("reasoning_effort", row.get("effort", effort)) != effort:
                raise ValueError("Primary oracle cannot mix reasoning efforts")
        revision = row.get("revision", 1)
        if (not isinstance(revision, int) or isinstance(revision, bool) or revision < 1
                or revision <= revisions.get(key, 0)):
            raise ValueError(f"Nonmonotonic {role} annotation revision")
        revisions[key] = revision
        try:
            validate_label(row, items[key])
        except (ValueError, TypeError, KeyError) as exc:
            if role != "oracle":
                raise ValueError(f"Invalid {role} annotation for {key}: {exc}") from exc
            invalid[key] = "schema_invalid"
            continue
        labels[key] = row
    return labels, invalid


def _interval(values, replicates, seed, method, **metadata):
    valid = [float(value) for value in values if value is not None and math.isfinite(value)]
    interval = np.quantile(valid, [0.025, 0.975]).tolist() if valid else None
    return {"ci95": interval, "method": method, "replicates": replicates, "seed": seed,
            "defined_resamples": len(valid), "undefined_resamples": replicates - len(valid),
            "degenerate": interval[0] == interval[1] if interval else None,
            **metadata}


def bounded_rate(successes, n, expected):
    """Missing is neither positive nor negative; give both extreme completions."""
    if not 0 <= successes <= n <= expected:
        raise ValueError("Invalid observed/planned reference counts")
    return {"successes": successes, "n": n, "expected": expected,
            "estimate": successes / n if n else None, "wilson95": wilson(successes, n),
            "coverage": n / expected if expected else None,
            "missing_reference_bounds": [successes / expected,
                                         (successes + expected - n) / expected]
            if expected else None,
            "bounds_scope": "All planned sampled units; missing references set to false/true. "
                            "These are sensitivity bounds, not confidence intervals."}


def design_weighted_precision(labels, mapping, frames, *, arms=None,
                              two_stage=False, replicates=BOOTSTRAP_REPLICATES,
                              seed=BOOTSTRAP_SEED):
    """Hájek valid-reference ratio, original inclusion weights and stratified resampling."""
    chosen = {key: frame for key, frame in sorted(frames.items())
              if frame["frame_count"] and (arms is None or frame["arm"] in arms)}
    groups = {key: [] for key in chosen}
    all_weight = valid_weight = successes = 0.0
    counts = Counter()
    for item_id, meta in sorted(mapping.items()):
        if meta["task"] != "pair":
            continue
        key = f"{meta['arm']}/{meta['label']}"
        if key not in chosen:
            continue
        probability = meta["total_inclusion_probability" if two_stage
                           else "inclusion_probability"]
        if not 0 < probability <= 1:
            raise ValueError("Inclusion probabilities must be in (0, 1]")
        weight = 1 / probability
        all_weight += weight
        counts[key] += 1
        if item_id in labels:
            value = int(labels[item_id]["label"] in TRUE_FLAGS)
            groups[key].append((weight * value, weight))
            valid_weight += weight
            successes += weight * value
    empty = [key for key, group in groups.items() if not group]
    unsampled = [key for key in groups if not counts[key]]
    estimable = bool(chosen) and not empty
    estimate = successes / valid_weight if estimable else None
    # An unsampled population stratum cannot be bounded by observed sample weights.
    bound_denominator = all_weight + sum(chosen[key]["frame_count"] for key in unsampled)
    bounds = [successes / bound_denominator,
              (successes + bound_denominator - valid_weight) / bound_denominator] \
        if bound_denominator else None
    rng, values = random.Random(seed), []
    if estimable:
        for _ in range(replicates):
            selected = [entry for group in groups.values()
                        for entry in rng.choices(group, k=len(group))]
            values.append(sum(value for value, _ in selected) / sum(w for _, w in selected))
    complete = estimable and math.isclose(valid_weight, all_weight)
    return {"status": "complete" if complete else "partial_valid_references" if estimable
            else "not_estimable", "estimate": estimate,
            "population_flags": sum(frame["frame_count"] for frame in chosen.values()),
            "valid_references": sum(len(group) for group in groups.values()),
            "expected_references": sum(counts.values()),
            "valid_weight": valid_weight, "planned_weight": bound_denominator,
            "weighted_coverage": valid_weight / bound_denominator if bound_denominator else None,
            "missing_reference_bounds": bounds, "empty_reference_strata": empty,
            "strata": {key: {"expected": counts[key], "valid": len(group)}
                       for key, group in groups.items()},
            "weighting": "1 / (first_stage_probability * second_stage_probability)"
            if two_stage else "1 / original_inclusion_probability",
            "estimator": "Hájek ratio over valid references; no point estimate when a whole "
                         "population stratum has no valid reference",
            "bootstrap": _interval(values, replicates, seed,
                                   "arm/predicted-class stratified percentile bootstrap"),
            "limitation": "Intervals condition on observed reference validity and do not correct "
                          "nonrandom missingness. Repeated PMIDs and claim pairs can remain "
                          "dependent; the interval does not fully model that dependence."}


def abstract_interval(rows, items, selector, *, replicates=BOOTSTRAP_REPLICATES,
                      seed=BOOTSTRAP_SEED):
    """Resample complete abstracts within arm, preserving all their claims."""
    groups = defaultdict(list)
    for row in rows:
        claims = row["claims"]
        groups[items[row["item_id"]]["arm"]].append(
            (sum(selector(claim) for claim in claims), len(claims)))
    groups = dict(sorted(groups.items()))
    rng, values = random.Random(seed), []
    if groups:
        for _ in range(replicates):
            draw = [entry for group in groups.values()
                    for entry in rng.choices(group, k=len(group))]
            n = sum(count for _, count in draw)
            values.append(sum(good for good, _ in draw) / n if n else None)
    return _interval(values, replicates, seed, "abstract-cluster percentile bootstrap within arm",
                     n_clusters=sum(len(group) for group in groups.values()),
                     clusters_by_arm={arm: len(group) for arm, group in groups.items()})


def reference_metrics(labels, items, mapping, frames, *, two_stage=False,
                      replicates=BOOTSTRAP_REPLICATES, seed=BOOTSTRAP_SEED):
    result = {"pairs": {}, "extraction": {}}
    for arm in ("overall", *ARMS):
        expected = {key: item for key, item in items.items()
                    if arm == "overall" or item["arm"] == arm}
        pairs = {key: labels[key] for key in expected
                 if key in labels and expected[key]["task"] == "pair"}
        pair_n = sum(item["task"] == "pair" for item in expected.values())
        result["pairs"][arm] = {
            "sample_precision": bounded_rate(sum(row["label"] in TRUE_FLAGS
                                                 for row in pairs.values()), len(pairs), pair_n),
            "sample_interval_scope": "Descriptive Wilson interval for valid references in the "
                                     "stratified sample; not unweighted corpus prevalence",
            "breakdown": dict(Counter(row["label"] for row in pairs.values())),
            "design_weighted_precision": design_weighted_precision(
                labels, mapping, frames, arms=None if arm == "overall" else {arm},
                two_stage=two_stage, replicates=replicates, seed=seed)}
        abstracts = [labels[key] for key in expected
                     if key in labels and expected[key]["task"] == "extraction"]
        expected_abstracts = [item for item in expected.values() if item["task"] == "extraction"]
        expected_claims = sum(len(item["claims"]) for item in expected_abstracts)
        claims = [claim for row in abstracts for claim in row["claims"]]
        fields = {}
        for field in FIELDS:
            counts = Counter(claim["fields"][field] for claim in claims)
            fields[field] = {**bounded_rate(counts["correct"], len(claims), expected_claims),
                             "counts": dict(counts),
                             "cluster_interval": abstract_interval(
                                 abstracts, items, lambda c, f=field: c["fields"][f] == "correct",
                                 replicates=replicates, seed=seed)}
        support = Counter(claim["evidence_support"] for claim in claims)
        result["extraction"][arm] = {
            "abstracts": len(abstracts), "expected_abstracts": len(expected_abstracts),
            "claims": len(claims), "expected_claims": expected_claims,
            "fields": fields, "uncertain_counts_as_incorrect": True,
            "evidence_support": {**bounded_rate(support["supported"], len(claims), expected_claims),
                                 "counts": dict(support),
                                 "cluster_interval": abstract_interval(
                                     abstracts, items, lambda c: c["evidence_support"] == "supported",
                                     replicates=replicates, seed=seed)}}
    return result


def _agreement_stat(pairs):
    if not pairs:
        return {"n": 0, "agreement": None, "chance_agreement": None, "kappa": None}
    return cohen_kappa([a for a, _ in pairs], [b for _, b in pairs])


def agreement_interval(groups, *, method, replicates=BOOTSTRAP_REPLICATES, seed=BOOTSTRAP_SEED):
    """Each group is a sampling stratum; each unit contains one/all claim comparisons."""
    groups = {key: groups[key] for key in sorted(groups) if groups[key]}
    pairs = [pair for units in groups.values() for unit in units for pair in unit]
    point = _agreement_stat(pairs)
    raw, kappas = [], []
    rng = random.Random(seed)
    if pairs:
        for _ in range(replicates):
            draw = [pair for units in groups.values()
                    for unit in rng.choices(units, k=len(units)) for pair in unit]
            estimate = _agreement_stat(draw)
            raw.append(estimate["agreement"])
            kappas.append(estimate["kappa"])
    return {**point, "raw_agreement_interval": _interval(raw, replicates, seed, method),
            "kappa_interval": _interval(kappas, replicates, seed, method),
            "n_resampling_units": sum(len(units) for units in groups.values()),
            "units_by_stratum": {key: len(units) for key, units in groups.items()},
            "kappa_status": "defined" if point["kappa"] is not None else "undefined"}


def agreement_report(first, second, items, mapping, *, replicates=BOOTSTRAP_REPLICATES,
                     seed=BOOTSTRAP_SEED):
    matched = set(items) & set(first) & set(second)
    pair_groups, binary_groups = defaultdict(list), defaultdict(list)
    field_groups = {field: defaultdict(list) for field in (*FIELDS, "evidence_support")}
    disagreements = {"pairs": [], "extraction": []}
    for key in sorted(matched):
        a, b, item = first[key], second[key], items[key]
        if item["task"] == "pair":
            stratum = f"{mapping[key]['arm']}/{mapping[key]['label']}"
            pair_groups[stratum].append([(a["label"], b["label"])])
            binary_groups[stratum].append([(a["label"] in TRUE_FLAGS, b["label"] in TRUE_FLAGS)])
            if a["label"] != b["label"] or a["factor"] != b["factor"]:
                disagreements["pairs"].append({
                    "item_id": key, "pair_id": mapping[key]["pair_id"], "arm": item["arm"],
                    "first_label": a["label"], "second_label": b["label"],
                    "binary_disagreement": (a["label"] in TRUE_FLAGS) != (b["label"] in TRUE_FLAGS),
                    "first_factor": a["factor"], "second_factor": b["factor"],
                    "first_rationale": a["rationale"], "second_rationale": b["rationale"]})
            continue
        a_claims = {claim["claim_id"]: claim for claim in a["claims"]}
        b_claims = {claim["claim_id"]: claim for claim in b["claims"]}
        for field in field_groups:
            unit = []
            for claim_id in sorted(a_claims):
                ac, bc = a_claims[claim_id], b_claims[claim_id]
                av, bv = ((ac[field], bc[field]) if field == "evidence_support"
                          else (ac["fields"][field], bc["fields"][field]))
                unit.append((av, bv))
                if av != bv:
                    disagreements["extraction"].append({
                        "item_id": key, "claim_id": claim_id, "arm": item["arm"],
                        "field": field, "first": av, "second": bv,
                        "first_note": ac.get("note", ""), "second_note": bc.get("note", "")})
            field_groups[field][item["arm"]].append(unit)
    kwargs = {"replicates": replicates, "seed": seed}
    expected_pairs = sum(item["task"] == "pair" for item in items.values())
    matched_pairs = sum(items[key]["task"] == "pair" for key in matched)
    fields = {field: agreement_interval(groups, method="abstract-cluster bootstrap within arm",
                                       **kwargs) for field, groups in field_groups.items()}
    transition_counts = Counter(f"{r['first_label']} -> {r['second_label']}"
                                for r in disagreements["pairs"]
                                if r["first_label"] != r["second_label"])
    return {"expected_items": len(items), "matched_items": len(matched),
            "missing_match_ids": sorted(set(items) - matched),
            "missing_first_ids": sorted(set(items) - set(first)),
            "missing_second_ids": sorted(set(items) - set(second)),
            "expected_pairs": expected_pairs, "paired_items": matched_pairs,
            "paired_abstracts": len(matched) - matched_pairs,
            "categories": agreement_interval(pair_groups,
                                              method="arm/predicted-class stratified bootstrap",
                                              **kwargs),
            "binary_true_flag": agreement_interval(binary_groups,
                                                    method="arm/predicted-class stratified bootstrap",
                                                    **kwargs),
            "extraction_fields": {field: fields[field] for field in FIELDS},
            "extraction_evidence_support": fields["evidence_support"],
            "disagreements": disagreements,
            "disagreement_summary": {"pair_category_transitions": dict(transition_counts),
                                     "binary_true_flag": sum(row["binary_disagreement"]
                                                             for row in disagreements["pairs"]),
                                     "extraction_fields": dict(Counter(row["field"]
                                                                      for row in disagreements["extraction"]))},
            "adjudication": "None; disagreements are retained with both judgments and rationales. "
                            "Differences describe judge behavior, not which judgment is true."}


def reliability_decision(binary_agreement, *, human_complete, oracle_complete,
                         threshold=0.40):
    point = binary_agreement.get("kappa")
    if not human_complete or not oracle_complete:
        status, primary = "pending_human_validation", "oracle_provisional"
    elif point is None:
        status, primary = "inconclusive_undefined_kappa", "oracle_provisional"
    elif point < threshold:
        status, primary = "oracle_unreliable", "human_subset"
    else:
        status, primary = "threshold_met", "oracle"
    return {"status": status, "primary_estimates": primary, "binary_kappa": point,
            "threshold": threshold, "threshold_applies_to": "point estimate, not confidence bound",
            "oracle_reliable": status == "threshold_met" if status in
            {"threshold_met", "oracle_unreliable"} else None,
            "undefined_kappa_is_not_a_pass": True}


def _coverage(labels, items, invalid=None):
    return {"expected": len(items), "valid": len(labels),
            "fraction": len(labels) / len(items) if items else None,
            "missing_ids": sorted(set(items) - set(labels)),
            "invalid_ids": sorted(invalid or {}), "invalid_reasons": invalid or {},
            "by_task": {task: {"expected": sum(i["task"] == task for i in items.values()),
                               "valid": sum(row["task"] == task for row in labels.values())}
                        for task in ("pair", "extraction")}}


def run(oracle="annotation/oracle_max/labels.jsonl", human="annotation/antonio_subset_labels.jsonl",
        second="annotation/llm_labels.jsonl", package="annotation/package/items.jsonl",
        manifest="data/derived/annotation_sampling_private.json",
        human_package="annotation/human_subset/items.jsonl",
        human_manifest="data/derived/human_subset_sampling_private.json",
        run_manifest="annotation/oracle_max/run_manifest.json",
        config="configs/evaluation_amendment04.json",
        output="results/metabolic_oracle_evaluation.json"):
    """Execute only after implementation is frozen; metadata errors fail closed."""
    spec = json.loads(Path(config).read_text())
    if (spec["amendment"] != 4 or spec["bootstrap_replicates"] != BOOTSTRAP_REPLICATES
            or spec["bootstrap_seed"] != BOOTSTRAP_SEED
            or spec["binary_kappa_reliability_threshold"] != 0.40):
        raise ValueError("Unexpected amendment analysis specification")
    missing_packages = [str(path) for path in (package, manifest, human_package, human_manifest)
                        if not Path(path).exists()]
    if missing_packages:
        result = {"status": "pending_annotation_packages", "missing_files": missing_packages,
                  "generated_at": utcnow(), "gold_labels_substituted": False}
        write_json(output, result)
        return result
    rows, human_rows = list(read_jsonl(package)), list(read_jsonl(human_package))
    private = json.loads(Path(manifest).read_text())
    human_private = json.loads(Path(human_manifest).read_text())
    items, mapping = validate_design(rows, private)
    if digest(rows) != spec["parent_package_sha256"]:
        raise ValueError("Parent package differs from signed amendment hash")
    if set(items) != set(spec["oracle_order"]) or len(items) != len(spec["oracle_order"]):
        raise ValueError("Oracle order differs from frozen package IDs")
    if "stratum_counts" in spec and {
        key: frame["selected"] for key, frame in private["pair_frames"].items()
        if frame["selected"]
    } != spec["stratum_counts"]:
        raise ValueError("Original sampled strata differ from signed amendment")
    human_items, human_mapping = validate_design(human_rows, human_private,
                                                parent=items, parent_manifest=private)
    if set(human_items) != set(spec["human_ids"]) or len(human_items) != len(spec["human_ids"]):
        raise ValueError("Human subset differs from prespecified IDs")
    if sum(len(item["claims"]) for item in human_rows if item["task"] == "extraction") != spec["human_claims"]:
        raise ValueError("Human extraction claim count differs from amendment")
    if "stratum_allocation" in spec and {
        key: frame["selected"] for key, frame in human_private["pair_frames"].items()
        if frame["selected"]
    } != spec["stratum_allocation"]:
        raise ValueError("Human sampled strata differ from signed amendment")
    oracle_run = json.loads(Path(run_manifest).read_text()) if Path(run_manifest).exists() else {}
    effort = oracle_run.get("effort", spec["effort"])
    if effort not in {spec["effort"], spec["fallback_effort"]}:
        raise ValueError("Unexpected oracle reasoning effort")
    oracle_labels, invalid = load_labels(oracle, items, role="oracle", effort=effort)
    human_labels, _ = load_labels(human, human_items, role="human")
    other_labels, _ = load_labels(second, items, role="secondary")
    if oracle_labels:
        for field, expected in (("model", spec["model"]),
                                ("package_sha256", spec["parent_package_sha256"]),
                                ("guidelines_sha256", spec["guidelines_sha256"])):
            if oracle_run.get(field) != expected:
                raise ValueError(f"Oracle run manifest {field} mismatch")
    for key in oracle_run.get("invalid_item_ids", []):
        if key not in items or key in oracle_labels:
            raise ValueError("Oracle run manifest invalid IDs conflict with primary labels")
        invalid.setdefault(key, "invalid_after_permitted_attempts")
    oracle_finished = len(oracle_labels) + len(invalid) == len(items)
    human_complete = len(human_labels) == len(human_items)
    agreement_human = agreement_report(oracle_labels, human_labels, human_items, human_mapping)
    agreement_other = agreement_report(oracle_labels, other_labels, items, mapping)
    reliability = reliability_decision(agreement_human["binary_true_flag"],
                                       human_complete=human_complete, oracle_complete=oracle_finished)
    pending = []
    if not oracle_finished:
        pending.append("oracle_labels")
    if not human_complete:
        pending.append("human_labels")
    if len(other_labels) != len(items):
        pending.append("secondary_labels")
    result = {
        "schema_version": 1, "amendment": 4, "generated_at": utcnow(),
        "status": "pending_" + pending[0] if pending else
        "complete_with_missing_references" if invalid else "complete",
        "pending_inputs": pending, "gold_labels_substituted": False,
        "package_sha256": digest(rows), "human_package_sha256": digest(human_rows),
        "source_sha256": {name: file_sha(path) if Path(path).exists() else None
                          for name, path in (("oracle", oracle), ("human", human), ("secondary", second),
                                             ("sampling_manifest", manifest),
                                             ("human_sampling_manifest", human_manifest),
                                             ("oracle_run_manifest", run_manifest), ("config", config))},
        "reference": {"name": ORACLE_NAME, "model": spec["model"], "reasoning_effort": effort,
                      "description": "Frontier LLM judge (GPT-6 Astra, " + effort +
                      " reasoning effort, isolated, run through Codex CLI on a ChatGPT subscription: "
                      "an agent wrapper rather than a raw API call; closed weights), " +
                      ("validated on a random human subset" if human_complete else
                       "to be validated on a random human subset"),
                      "same_model_family_as_pipeline_agent": True,
                      "scope": "Judge-referenced estimates, not human gold or clinical truth"},
        "secondary_reference": {"name": SECONDARY_NAME, "reasoning_effort": "xhigh",
                                "technically_isolated": False,
                                "blinding": "instruction-based in-run pass",
                                "independent_human_validation": False},
        "reference_validity": {"oracle": _coverage(oracle_labels, items, invalid),
                               "human": _coverage(human_labels, human_items),
                               "secondary": _coverage(other_labels, items)},
        "reliability": reliability, "primary_estimates": reliability["primary_estimates"],
        "oracle": reference_metrics(oracle_labels, items, mapping, private["pair_frames"]),
        "human_subset": reference_metrics(human_labels, human_items, human_mapping,
                                           human_private["pair_frames"], two_stage=True),
        "agreement": {"oracle_human": agreement_human, "oracle_secondary": agreement_other},
        "bootstrap": {"replicates": BOOTSTRAP_REPLICATES, "seed": BOOTSTRAP_SEED,
                      "interval": "95% percentile; undefined resamples excluded and counted; "
                                  "degenerate intervals explicitly flagged"},
        "limitations": [("Human extraction validation has six abstract clusters and 31 claims; "
                         "small-cluster intervals have limited precision."),
                        "Repeated PMIDs create dependence across pair items.",
                        ("Extraction arms are article query-arm sampling strata; all retained claims "
                         "in each sampled abstract contribute."),
                        "Evidence support and schema/reference validity are reported separately.",
                        "Missing-reference bounds are sensitivity ranges, not uncertainty intervals."],
        "recall": {"whole_corpus": None, "reason": "No exhaustively annotated corpus; "
                  "whole-corpus recall is not measurable"}}
    result["oracle"]["interpretation"] = (
        "unreliable" if reliability["status"] == "oracle_unreliable"
        else "validation_threshold_met" if reliability["status"] == "threshold_met"
        else "provisional; human validation pending or inconclusive")
    result["human_subset"]["role"] = (
        "primary" if reliability["primary_estimates"] == "human_subset" else "secondary")
    write_json(output, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for option, default in (
        ("oracle", "annotation/oracle_max/labels.jsonl"),
        ("human", "annotation/antonio_subset_labels.jsonl"),
        ("second", "annotation/llm_labels.jsonl"),
        ("package", "annotation/package/items.jsonl"),
        ("manifest", "data/derived/annotation_sampling_private.json"),
        ("human-package", "annotation/human_subset/items.jsonl"),
        ("human-manifest", "data/derived/human_subset_sampling_private.json"),
        ("run-manifest", "annotation/oracle_max/run_manifest.json"),
        ("config", "configs/evaluation_amendment04.json"),
        ("output", "results/metabolic_oracle_evaluation.json")):
        parser.add_argument("--" + option, default=default)
    result = run(**vars(parser.parse_args()))
    print(json.dumps({key: result.get(key) for key in
                      ("status", "primary_estimates", "pending_inputs")}))
