"""Auditable binary metrics and uncertainty, including clustered comparisons."""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from collections.abc import Sequence


def confusion(y_true: Sequence[int], y_pred: Sequence[int]) -> dict[str, int]:
    if len(y_true) != len(y_pred):
        raise ValueError("Label vectors have different lengths")
    if any(x not in (0, 1) for x in (*y_true, *y_pred)):
        raise ValueError("Binary labels must be 0 or 1")
    counts = Counter(zip(y_true, y_pred))
    return {"tn": counts[0, 0], "fp": counts[0, 1], "fn": counts[1, 0], "tp": counts[1, 1]}


def _from_counts(c: dict[str, int]) -> dict[str, float]:
    tp, fp, fn, tn = (c[k] for k in ("tp", "fp", "fn", "tn"))
    safe = lambda numerator, denominator: numerator / denominator if denominator else 0.0
    return {
        "precision": safe(tp, tp + fp), "recall": safe(tp, tp + fn),
        "f1": safe(2 * tp, 2 * tp + fp + fn),
        "accuracy": safe(tp + tn, tp + fp + fn + tn),
        "specificity": safe(tn, tn + fp),
        "macro_f1": (safe(2 * tp, 2 * tp + fp + fn) + safe(2 * tn, 2 * tn + fp + fn)) / 2,
    }


def binary_metrics(y_true: Sequence[int], y_pred: Sequence[int]) -> dict:
    c = confusion(y_true, y_pred)
    return {"n": len(y_true), "positive_label": "inconsistent", "confusion": c,
            **_from_counts(c), "zero_denominator_convention": 0.0}


def wilson(successes: int, total: int, z: float = 1.959963984540054) -> list[float] | None:
    """Two-sided Wilson score interval; undefined for an empty denominator."""
    if not 0 <= successes <= total:
        raise ValueError("Require 0 <= successes <= total")
    if total == 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [0.0 if successes == 0 else max(0.0, center - radius),
            1.0 if successes == total else min(1.0, center + radius)]


def _percentile(values: list[float], q: float) -> float:
    values = sorted(values)
    position = (len(values) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def clustered_bootstrap(y_true: Sequence[int], y_pred: Sequence[int], groups: Sequence[str],
                        replicates: int = 2000, seed: int = 42) -> dict:
    """Resample questions, preserving all dependent pairs within each question."""
    if len(y_true) != len(groups) or len(y_pred) != len(groups) or not groups:
        raise ValueError("Need nonempty aligned labels, predictions and groups")
    if replicates < 2:
        raise ValueError("At least two replicates are required")
    indices = defaultdict(list)
    for i, group in enumerate(groups):
        indices[group].append(i)
    group_counts = [confusion([y_true[i] for i in indices[g]], [y_pred[i] for i in indices[g]])
                    for g in sorted(indices)]
    rng = random.Random(seed)
    estimates = defaultdict(list)
    undefined = Counter()
    for _ in range(replicates):
        chosen = rng.choices(group_counts, k=len(group_counts))
        c = {key: sum(row[key] for row in chosen) for key in ("tp", "tn", "fp", "fn")}
        for metric, denominator in {"precision": c["tp"] + c["fp"], "recall": c["tp"] + c["fn"],
                                    "f1": 2 * c["tp"] + c["fp"] + c["fn"],
                                    "specificity": c["tn"] + c["fp"]}.items():
            undefined[metric] += int(denominator == 0)
        for metric, value in _from_counts(c).items():
            estimates[metric].append(value)
    return {"method": "question-cluster percentile bootstrap", "confidence": 0.95,
            "replicates": replicates, "seed": seed, "n_clusters": len(indices),
            "undefined_resamples": dict(undefined), "zero_denominator_convention": 0.0,
            "intervals": {key: [_percentile(v, 0.025), _percentile(v, 0.975)]
                          for key, v in estimates.items()}}


def paired_cluster_bootstrap(y_true: Sequence[int], first: Sequence[int], second: Sequence[int],
                             groups: Sequence[str], replicates: int = 2000, seed: int = 42) -> dict:
    """Paired F1 difference, resampling the same questions for both systems."""
    if not (len(y_true) == len(first) == len(second) == len(groups)) or not groups:
        raise ValueError("Need nonempty aligned data")
    indices = defaultdict(list)
    for i, group in enumerate(groups):
        indices[group].append(i)
    counts = []
    for group in sorted(indices):
        idx = indices[group]
        truth = [y_true[i] for i in idx]
        counts.append((confusion(truth, [first[i] for i in idx]),
                       confusion(truth, [second[i] for i in idx])))
    rng = random.Random(seed)
    differences = []
    for _ in range(replicates):
        sample = rng.choices(counts, k=len(counts))
        totals = [{key: sum(c[j][key] for c in sample) for key in ("tp", "tn", "fp", "fn")}
                  for j in (0, 1)]
        differences.append(_from_counts(totals[0])["f1"] - _from_counts(totals[1])["f1"])
    return {"difference_first_minus_second": binary_metrics(y_true, first)["f1"] - binary_metrics(y_true, second)["f1"],
            "ci95": [_percentile(differences, 0.025), _percentile(differences, 0.975)],
            "method": "paired question-cluster percentile bootstrap", "replicates": replicates,
            "seed": seed, "n_clusters": len(counts)}


def cohen_kappa(first: Sequence[str], second: Sequence[str]) -> dict:
    if len(first) != len(second) or not first:
        raise ValueError("Need nonempty aligned annotations")
    n = len(first)
    observed = sum(a == b for a, b in zip(first, second)) / n
    a, b = Counter(first), Counter(second)
    expected = sum(a[k] * b[k] for k in a.keys() | b.keys()) / n ** 2
    return {"n": n, "agreement": observed, "chance_agreement": expected,
            "kappa": (observed - expected) / (1 - expected) if expected < 1 else None}
