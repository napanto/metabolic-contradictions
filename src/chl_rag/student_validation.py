"""Draft amendment-05 statistics, with no model calls or collection side effects.

Inputs are in-memory, deidentified reads. Nothing here changes the oracle run,
registers the amendment, adjudicates disagreements, or infers missing labels.
"""
from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime

import numpy as np

from .annotation import FACTORS, FLAGGED, PAIR_LABELS
from .metrics import cohen_kappa

EXPORT_FIELDS = frozenset({
    "item_id", "session_pseudonym", "label", "factor", "rationale",
    "time_on_item_seconds", "submitted_at_utc", "assigned_at_utc", "paid",
    "replacement", "surplus", "excluded", "quality_failed", "counted", "preview",
})
EXPORT_FLAGS = ("paid", "replacement", "surplus", "excluded", "quality_failed", "counted")
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 20261117


def adapt_site_export(rows, expected_pair_ids):
    """Validate the documented real-answer JSONL contract; preserve every row.

    The caller supplies the frozen pair whitelist and preserves original export
    order. The current exporter sorts answer IDs but omits them; row order is
    therefore the tie-break for second-resolution submission timestamps.
    No personal-data or student-answer files are opened by this function.
    """
    expected = list(expected_pair_ids)
    if (not expected or len(expected) != len(set(expected))
            or any(not isinstance(key, str) or not key for key in expected)):
        raise ValueError("Expected pair IDs must be distinct nonempty strings")
    expected = set(expected)
    result, seen, sessions = [], set(), {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) - EXPORT_FIELDS:
            raise ValueError("Unexpected site export fields; review the documented contract")
        if EXPORT_FIELDS - {"rationale", "factor", "preview"} - set(row):
            raise ValueError("Missing site export fields")
        key, session = row["item_id"], row["session_pseudonym"]
        if not isinstance(key, str) or key not in expected:
            raise ValueError("Foreign or synthetic item ID in real-pair export")
        if not isinstance(session, str) or not session.strip():
            raise ValueError("Missing session pseudonym")
        if (key, session) in seen:
            raise ValueError("Duplicate session/pair read; do not silently deduplicate")
        seen.add((key, session))
        if (row["label"] not in PAIR_LABELS
                or row.get("factor") not in (None, "", *FACTORS)):
            raise ValueError("Unknown category or factor")
        if not isinstance(row.get("rationale", ""), str):
            raise TypeError("Rationale must be text; it may be empty")
        for flag in EXPORT_FLAGS:
            if type(row[flag]) not in (int, bool) or row[flag] not in (0, 1):
                raise ValueError(f"{flag} must be a JSON boolean or integer 0/1")
        if "preview" in row:
            if type(row["preview"]) not in (int, bool) or row["preview"] not in (0, 1):
                raise ValueError("preview must be a JSON boolean or integer 0/1")
            if row["preview"]:
                raise ValueError("Preview rows are calibration only, never student analysis inputs")
        if row["paid"] == row["surplus"]:
            raise ValueError("A real read must be accepted/paid or unpaid surplus")
        if row["counted"] and (not row["paid"] or row["surplus"] or row["excluded"]):
            raise ValueError("Counted read has incompatible eligibility flags")
        state = (row["excluded"], row["quality_failed"])
        if session in sessions and sessions[session] != state:
            raise ValueError("Session flags vary within export; require a coherent snapshot")
        sessions[session] = state
        seconds = row["time_on_item_seconds"]
        if (not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
                or not math.isfinite(seconds) or not 0 <= seconds <= 86400):
            raise ValueError("Invalid time on item")
        times = []
        for field in ("assigned_at_utc", "submitted_at_utc"):
            value = row[field]
            if not isinstance(value, str) or len(value) != 20 or not value.endswith("Z"):
                raise ValueError("Site timestamps must be YYYY-MM-DDTHH:MM:SSZ")
            try:
                parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
                if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
                    raise ValueError("Noncanonical timestamp")
                times.append(parsed)
            except ValueError as exc:
                raise ValueError("Invalid site timestamp") from exc
        if times[1] < times[0]:
            raise ValueError("Submission precedes assignment")
        # Keep source fields verbatim; ordinal is provenance, never a new answer.
        result.append({**row, "export_row": index + 1})
    return result


def consensus_label(labels):
    """Return the unique category leader with >=2 votes; otherwise None."""
    labels = list(labels)
    if any(label not in PAIR_LABELS for label in labels):
        raise ValueError("Consensus requires the six guideline categories")
    ranked = Counter(labels).most_common()
    if (ranked and ranked[0][1] >= 2
            and (len(ranked) == 1 or ranked[0][1] > ranked[1][1])):
        return ranked[0][0]
    return None


def nominal_alpha(units):
    """Krippendorff nominal alpha from units of ratings; None denotes missing.

    Coincidences in each unit have weight 1/(m_u-1). Singleton values do not
    enter either the observed disagreement or the chance marginals. Categories
    are nominal even when represented by integers. See methods source archive.
    """
    marginals, total, observed_sum, pairable = Counter(), 0, 0.0, 0
    unit_count = 0
    for unit in units:
        unit_count += 1
        ratings = [value for value in unit if value is not None]
        for value in ratings:
            if not isinstance(value, (str, int, float, bool)):
                raise TypeError("Nominal ratings must be scalar categories or None")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Use None for missing ratings, never NaN/infinity")
        m = len(ratings)
        if m < 2:
            continue
        counts = Counter(ratings)
        observed_sum += (m * m - sum(n * n for n in counts.values())) / (m - 1)
        marginals.update(counts)
        total += m
        pairable += 1
    observed = observed_sum / total if total else None
    expected = ((total * total - sum(n * n for n in marginals.values()))
                / (total * (total - 1))) if total else None
    alpha = 1 - observed / expected if expected else None
    return {"alpha": alpha, "observed_disagreement": observed,
            "expected_disagreement": expected, "pairable_values": total,
            "pairable_units": pairable, "unpairable_units": unit_count - pairable,
            "undefined_reason": "no_pairable_units" if not total
            else "no_category_variation" if not expected else None}


def _bootstrap_interval(values, replicates, seed):
    defined = [float(value) for value in values
               if value is not None and math.isfinite(value)]
    interval = np.quantile(defined, [0.025, 0.975]).tolist() if defined else None
    return {"ci95": interval, "method": "pair-cluster percentile bootstrap",
            "replicates": replicates, "seed": seed, "defined_resamples": len(defined),
            "undefined_resamples": replicates - len(defined),
            "degenerate": interval[0] == interval[1] if interval else None}


def _bootstrap(groups, estimator, *, replicates, seed):
    if type(replicates) is not int or replicates < 1:
        raise ValueError("Bootstrap replicates must be a positive integer")
    rng = random.Random(seed)
    point = estimator(groups)
    samples = [estimator(rng.choices(groups, k=len(groups)))
               for _ in range(replicates)] if groups else []
    return {key: {"estimate": value, "bootstrap": _bootstrap_interval(
        [sample[key] for sample in samples], replicates, seed)} for key, value in point.items()}


def human_agreement(pair_labels, *, replicates=BOOTSTRAP_REPLICATES, seed=BOOTSTRAP_SEED):
    """First two valid chronological reads only; later escalation cannot inflate it."""
    selected = [list(labels)[:2] for labels in pair_labels.values()]
    complete = [labels for labels in selected if len(labels) == 2]
    for labels in selected:
        if any(label not in PAIR_LABELS for label in labels):
            raise ValueError("Unknown category in human agreement")

    def estimate(units):
        binary = [[label in FLAGGED for label in unit] for unit in units]
        return {"category_raw_agreement": statistics.mean(a == b for a, b in units)
                if units else None, "category_alpha": nominal_alpha(units)["alpha"],
                "binary_raw_agreement": statistics.mean(a == b for a, b in binary)
                if units else None, "binary_alpha": nominal_alpha(binary)["alpha"]}

    return {"expected_pairs": len(selected), "pairs_with_two_reads": len(complete),
            "pairs_with_one_read": sum(len(labels) == 1 for labels in selected),
            "pairs_with_no_reads": sum(not labels for labels in selected),
            "metrics": _bootstrap(complete, estimate, replicates=replicates, seed=seed),
            "category_alpha_detail": nominal_alpha(selected),
            "binary_alpha_detail": nominal_alpha(
                [[label in FLAGGED for label in labels] for labels in selected])}


def oracle_agreement(pair_comparisons, *, replicates=BOOTSTRAP_REPLICATES,
                     seed=BOOTSTRAP_SEED):
    """Cohen kappa/raw agreement; every pair's oracle/read comparisons stay together.

    With one consensus per pair this is ordinary matched-pair analysis. With
    individual reads it is read-weighted, with repeated reads clustered by pair.
    """
    groups = [list(group) for group in pair_comparisons.values() if group]
    for group in groups:
        if any(a not in PAIR_LABELS or b not in PAIR_LABELS for a, b in group):
            raise ValueError("Unknown category in oracle comparison")

    def estimate(sample):
        comparisons = [comparison for group in sample for comparison in group]
        if not comparisons:
            return dict.fromkeys(("category_raw_agreement", "category_kappa",
                                  "binary_raw_agreement", "binary_kappa"))
        first, second = zip(*comparisons)
        category = cohen_kappa(first, second)
        binary = cohen_kappa([label in FLAGGED for label in first],
                             [label in FLAGGED for label in second])
        return {"category_raw_agreement": category["agreement"],
                "category_kappa": category["kappa"],
                "binary_raw_agreement": binary["agreement"], "binary_kappa": binary["kappa"]}

    return {"matched_pairs": len(groups), "matched_reads": sum(map(len, groups)),
            "metrics": _bootstrap(groups, estimate, replicates=replicates, seed=seed)}


def _distribution(values):
    values = list(values)
    return {"n": len(values), "median": statistics.median(values) if values else None,
            "q25_q75": np.quantile(values, [.25, .75]).tolist() if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def _population_analysis(reads, expected_pair_ids, oracle_labels, *, replicates, seed):
    grouped = {key: [] for key in expected_pair_ids}
    for read in reads:
        grouped[read["item_id"]].append(read)
    for group in grouped.values():
        group.sort(key=lambda read: (read["submitted_at_utc"], read["export_row"]))
    pairs, matched, individual, by_session, disagreements = {}, {}, {}, defaultdict(dict), []
    individual_disagreements = []
    for key, group in grouped.items():
        labels = [read["label"] for read in group]
        label = consensus_label(labels)
        pairs[key] = {"label": label, "status": "resolved" if label is not None else
                      "insufficient_reads" if len(group) < 2 else "unresolved",
                      "votes": dict(sorted(Counter(labels).items())), "read_count": len(group),
                      "read_rows": [read["export_row"] for read in group],
                      "first_two_read_rows": [read["export_row"] for read in group[:2]],
                      "true_flag": label in FLAGGED if label is not None else None}
        if key not in oracle_labels:
            continue
        oracle = oracle_labels[key]
        if label is not None:
            matched[key] = [(oracle, label)]
            if oracle != label:
                disagreements.append({"item_id": key, "oracle_label": oracle,
                                      "student_label": label,
                                      "binary_disagreement": (oracle in FLAGGED)
                                      != (label in FLAGGED)})
        individual[key] = [(oracle, read["label"]) for read in group]
        for read in group:
            by_session[read["session_pseudonym"]][key] = [(oracle, read["label"])]
            if oracle != read["label"]:
                individual_disagreements.append({
                    "item_id": key, "export_row": read["export_row"],
                    "session_pseudonym": read["session_pseudonym"], "oracle_label": oracle,
                    "student_label": read["label"],
                    "binary_disagreement": (oracle in FLAGGED) != (read["label"] in FLAGGED),
                })
    consensus = oracle_agreement(matched, replicates=replicates, seed=seed)
    kappa = consensus["metrics"]["binary_kappa"]["estimate"]
    statuses = Counter(pair["status"] for pair in pairs.values())
    return {"pairs": pairs, "read_count": len(reads),
            "session_count": len({read["session_pseudonym"] for read in reads}),
            "resolved_pairs": statuses["resolved"], "unresolved_pairs": statuses["unresolved"],
            "insufficient_read_pairs": statuses["insufficient_reads"],
            "pairs_below_two_reads": sum(pair["read_count"] < 2 for pair in pairs.values()),
            "human_human": human_agreement(
                {key: [read["label"] for read in group] for key, group in grouped.items()},
                replicates=replicates, seed=seed),
            "oracle_consensus": consensus,
            "reliability_rule": {"binary_kappa_threshold": .40, "binary_kappa": kappa,
                                 "oracle_unreliable": kappa < .40 if kappa is not None else None,
                                 "status": "not_estimable" if kappa is None else
                                 "below_threshold" if kappa < .40 else "threshold_met"},
            "oracle_individual_reads": oracle_agreement(
                individual, replicates=replicates, seed=seed),
            "oracle_by_session": {session: oracle_agreement(
                groups, replicates=replicates, seed=seed)
                for session, groups in sorted(by_session.items())},
            "disagreements": disagreements,
            "individual_disagreements": individual_disagreements,
            "time_per_pair_seconds": _distribution(read["time_on_item_seconds"] for read in reads)}


def student_validation(rows, expected_pair_ids, oracle_labels=None, *,
                       replicates=BOOTSTRAP_REPLICATES, seed=BOOTSTRAP_SEED):
    """Produce primary and all-accepted-read sensitivity reports from a raw export.

    Final stored quality flags are authoritative; this does not replay the live
    exclusion process from a final snapshot. Demographics are absent from the
    documented answer export and are reported as unavailable, never invented.
    """
    expected = sorted(expected_pair_ids)
    reads = adapt_site_export(rows, expected)
    oracle_labels = {} if oracle_labels is None else dict(oracle_labels)
    if set(oracle_labels) - set(expected):
        raise ValueError("Oracle comparison contains a foreign pair ID")
    if any(label not in PAIR_LABELS for label in oracle_labels.values()):
        raise ValueError("Invalid oracle category")
    accepted = [read for read in reads if read["paid"] and not read["surplus"]]
    primary = [read for read in accepted
               if read["counted"] and not read["excluded"] and not read["quality_failed"]]
    sessions = {read["session_pseudonym"]: read for read in reads}
    own = defaultdict(list)
    peers = defaultdict(list)
    for read in accepted:
        own[read["session_pseudonym"]].append(read)
        if read["counted"]:
            peers[read["item_id"]].append(read)
    shared_counts, match_counts, match_rates, median_times = [], [], [], []
    for session, group in own.items():
        shared = matches = 0
        for read in group:
            others = [peer for peer in peers[read["item_id"]]
                      if peer["session_pseudonym"] != session]
            if others:
                shared += 1
                matches += any(peer["label"] == read["label"] for peer in others)
        shared_counts.append(shared)
        match_counts.append(matches)
        if shared:
            match_rates.append(matches / shared)
        median_times.append(statistics.median(read["time_on_item_seconds"] for read in group))
    return {"status": "draft_analysis_pending_configuration_registration",
            "expected_pairs": len(expected), "export_rows": len(reads),
            "retained_reads": reads,
            "excluded_sessions": sum(bool(read["excluded"]) for read in sessions.values()),
            "flagged_sessions": sum(bool(read["quality_failed"]) for read in sessions.values()),
            "excluded_or_flagged_sessions": sum(bool(read["excluded"] or read["quality_failed"])
                                                for read in sessions.values()),
            "unpaid_surplus_reads": sum(bool(read["surplus"]) for read in reads),
            "accepted_but_not_counted_reads": sum(not read["counted"] for read in accepted),
            "primary": _population_analysis(primary, expected, oracle_labels,
                                             replicates=replicates, seed=seed),
            "sensitivity_all_accepted_reads": _population_analysis(
                accepted, expected, oracle_labels, replicates=replicates, seed=seed),
            "quality_aggregate": {"scope": "final snapshot; not flag-time reconstruction",
                                  "peer_rule": "any currently counted other reader matches",
                                  "shared_pairs_per_session": _distribution(shared_counts),
                                  "category_matches_per_session": _distribution(match_counts),
                                  "category_match_rates_per_session": _distribution(match_rates),
                                  "median_seconds_per_session": _distribution(median_times)},
            "demographics": {"status": "unavailable_in_documented_answer_export",
                             "programme": None, "year": None},
            "limitations": [
                "Agreement-based exclusions can inflate primary agreement; report sensitivity.",
                "Pair bootstrap does not model dependence across shared readers or abstracts.",
                "Submission ties use original export row order; export omits answer IDs.",
                "Pair whitelist does not establish package/configuration hash binding.",
                "Server elapsed time starts at assignment activation and includes pauses.",
                "Students see deterministic claim sentences; oracle and author fallback use raw fields.",
                "An undefined binary kappa is not evidence that the reliability rule passed.",
            ]}
