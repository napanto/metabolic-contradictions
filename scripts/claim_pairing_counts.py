"""Addendum 08E DRAFT: offline counts of claim-level semantic candidate pairs, two encoders.

No model API is called. MedCPT and SapBERT run locally on CPU. Nothing is classified.
Outputs: counts per threshold and encoder, overlap diagnostics with the strict key route split by
cause, the encoder criterion at budget-matched volume, and tau* as a function of the budget B.

    PYTHONPATH=src .venv/bin/python scripts/claim_pairing_counts.py \
        --claims <claims_normalized.jsonl> --frame <08D pairs_candidates.jsonl> \
        --out results/claim_pairing/counts_08E.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from pathlib import Path

import numpy as np

from chl_rag.claim_pairing import (GENERIC_EXPOSURES, SAPBERT, count_grid, encode_texts, load_claims,
                                   pattern, strict_key_compatible)

GRID = (0.80, 0.85, 0.88, 0.90, 0.92, 0.94, 0.96, 0.98)
CONTROLS = 1500
LIKE_FOR_LIKE_T1T2 = 11391   # 08D frame, opposite + effect-versus-null
FRAME_ALL = 42972            # 08D frame, all signs


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def cached_vectors(texts, cache, name, model, threads):
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(("\n".join(texts) + "|" + model).encode()).hexdigest()[:16]
    legacy = cache / f"{name}_{hashlib.sha256(chr(10).join(texts).encode()).hexdigest()[:16]}.npy"
    path = cache / f"{model}_{name}_{key}.npy"
    if model == "medcpt" and legacy.exists() and not path.exists():
        legacy.rename(path)
    if path.exists():
        return np.load(path), 0.0
    started = time.monotonic()
    vectors = encode_texts(texts, threads=threads, model=model)
    np.save(path, vectors)
    return vectors, time.monotonic() - started


def frame_rows(path):
    rows = []
    with open(path) as handle:
        for line in handle:
            r = json.loads(line)
            rows.append(tuple(sorted((r["claim_a"], r["claim_b"]))))
    return rows


def key_route_diagnostics(frame, by_id, ev, ov, thresholds):
    """Strict-key T1+T2 pairs of the frame: recovered per tau, and causes when not recovered."""
    rows = []
    for a_id, b_id in frame:
        a, b = by_id.get(a_id), by_id.get(b_id)
        if not a or not b or not strict_key_compatible(a, b) or pattern(a["sign"], b["sign"]) == "concordant":
            continue
        ce = float(ev[a["exposure"]] @ ev[b["exposure"]])
        co = float(ov[a["outcome"]] @ ov[b["outcome"]])
        nonidentical = a["exposure"] != b["exposure"] or a["outcome"] != b["outcome"]
        if a["exposure"] in GENERIC_EXPOSURES or b["exposure"] in GENERIC_EXPOSURES:
            cause = "stoplist"
        elif a["exposure_key"] != b["exposure_key"]:
            cause = "class_member"
        elif a["comparison_type"] != b["comparison_type"]:
            cause = "comparison_type"
        else:
            cause = None
        rows.append((ce, co, nonidentical, cause))
    out = {}
    for t in thresholds:
        tally = {"key_route_T1T2": len(rows), "recovered": 0, "stoplist": 0, "class_member": 0,
                 "comparison_type": 0, "fails_E_only": 0, "fails_O_only": 0, "fails_both": 0}
        for ce, co, _n, cause in rows:
            if cause:
                tally[cause] += 1
            elif ce >= t - 1e-6 and co >= t - 1e-6:
                tally["recovered"] += 1
            elif ce < t - 1e-6 and co >= t - 1e-6:
                tally["fails_E_only"] += 1
            elif co < t - 1e-6 and ce >= t - 1e-6:
                tally["fails_O_only"] += 1
            else:
                tally["fails_both"] += 1
        out[f"{t:.2f}"] = tally
    return out, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--claims", required=True)
    ap.add_argument("--frame", required=True, help="08D pairs_candidates.jsonl")
    ap.add_argument("--cache", default=str(Path.home() / ".cache/chl-newpairs"))
    ap.add_argument("--threads", type=int, default=28)
    ap.add_argument("--floor", type=float, default=0.80, help="enumeration floor for the per-pair split")
    ap.add_argument("--cost", type=float, action="append", default=None, help="USD per classified pair")
    ap.add_argument("--budgets", default="15,20,25,30")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    costs = args.cost or [0.00108, 0.00111]
    budgets = [float(b) for b in args.budgets.split(",")]

    t0 = time.monotonic()
    claims = load_claims(args.claims)
    by_id = {c["claim_id"]: c for c in claims}
    exposures = sorted({c["exposure"] for c in claims})
    outcomes = sorted({c["outcome"] for c in claims})
    frame = frame_rows(args.frame)
    existing = frozenset(frame)
    cache = Path(args.cache).expanduser()
    result = {"addendum": "08E DRAFT: counts only, no classification",
              "claims_path": args.claims, "claims_sha256": sha(args.claims),
              "frame_path": args.frame, "frame_sha256": sha(args.frame), "frame_pairs": len(frame),
              "eligible_claims": len(claims), "unique_exposure_texts": len(exposures),
              "unique_outcome_texts": len(outcomes), "controls": CONTROLS, "costs_usd_per_pair": costs,
              "encoders": {}}
    for model in ("medcpt", "sapbert"):
        ev_arr, t_e = cached_vectors(exposures, cache, "exposure", model, args.threads)
        ov_arr, t_o = cached_vectors(outcomes, cache, "outcome", model, args.threads)
        ev, ov = dict(zip(exposures, ev_arr)), dict(zip(outcomes, ov_arr))
        grid = count_grid(claims, ev, ov, GRID, threads=args.threads, enumerate_floor=args.floor,
                          existing_pairs=existing)
        new_scores = grid.pop("new_scores")
        t1t2 = np.sort(np.concatenate([new_scores["opposite"], new_scores["effect_null"]]))[::-1]
        diag, key_rows = key_route_diagnostics(frame, by_id, ev, ov, GRID)
        table = {}
        for t in GRID:
            row = grid["thresholds"][f"{t:.2f}"]
            new = row["new_not_in_existing_frames"]
            if new is None:
                table[f"{t:.2f}"] = {"new_T1": None, "new_T2": None, "new_T3": None}
                continue
            n1, n2, n3 = new.get("opposite", 0), new.get("effect_null", 0), new.get("concordant", 0)
            table[f"{t:.2f}"] = {"new_T1": n1, "new_T2": n2, "new_T3": n3, "new_T1T2": n1 + n2,
                                 "new_all_signs": n1 + n2 + n3,
                                 "ratio_T1T2_vs_11391": round((n1 + n2) / LIKE_FOR_LIKE_T1T2, 2),
                                 "ratio_all_vs_42972": round((n1 + n2 + n3) / FRAME_ALL, 2),
                                 "ten_x_fails": (n1 + n2) >= 10 * LIKE_FOR_LIKE_T1T2
                                 or (n1 + n2 + n3) >= 10 * FRAME_ALL}
        # tau* per budget and cost: smallest grid value with (new T1+T2 + controls) * c <= B.
        tau_star = {}
        for c in costs:
            for b in budgets:
                choice = None
                for t in GRID:
                    v = table[f"{t:.2f}"].get("new_T1T2")
                    if v is not None and (v + CONTROLS) * c <= b:
                        choice = t
                        break
                entry = {"tau_star": choice}
                if choice is not None:
                    spent = (table[f"{choice:.2f}"]["new_T1T2"] + CONTROLS) * c
                    idx = GRID.index(choice)
                    if idx > 0 and table[f"{GRID[idx - 1]:.2f}"].get("new_T1T2") is not None:
                        band = table[f"{GRID[idx - 1]:.2f}"]["new_T1T2"] - table[f"{choice:.2f}"]["new_T1T2"]
                        sample = min(band, int(math.floor((b - spent) / c)))
                        entry.update({"band": f"[{GRID[idx - 1]:.2f}, {choice:.2f})", "band_pairs": band,
                                      "band_sampled": sample, "cost_at_tau_star": round(spent, 2)})
                tau_star[f"B={b:g},c={c}"] = entry
        # Encoder criterion: each encoder proposes its K most similar new T1+T2 pairs,
        # K = floor((B - 1500c)/c); recovered = key-route T1+T2 with a non-identical string at score >= s_K.
        criterion = {}
        eligible_key = [(min(ce, co), cause) for ce, co, nonid, cause in key_rows if nonid]
        for c in costs:
            for b in budgets:
                k = int(math.floor((b - CONTROLS * c) / c))
                s_k = float(t1t2[k - 1]) if 0 < k <= t1t2.size else (float(t1t2[-1]) if t1t2.size else None)
                recovered = sum(1 for m, cause in eligible_key if cause is None and s_k is not None and m >= s_k)
                criterion[f"B={b:g},c={c}"] = {"K": k, "K_exceeds_enumerated": k > t1t2.size,
                                               "score_at_K": s_k, "key_T1T2_nonidentical": len(eligible_key),
                                               "recovered": recovered,
                                               "recovered_pct": round(100 * recovered / len(eligible_key), 1)
                                               if eligible_key else None}
        result["encoders"][model] = {
            "encode_seconds": {"exposure": round(t_e, 1), "outcome": round(t_o, 1)},
            "revision": SAPBERT["revision"] if model == "sapbert" else "configs/medcpt.json (MedCPT-Query-Encoder)",
            "grid": grid, "new_pairs_table": table, "key_route_overlap_by_cause": diag,
            "tau_star": tau_star, "encoder_criterion": criterion,
        }
        print(model, json.dumps({"encode": result["encoders"][model]["encode_seconds"],
                                 "table": {t: (r.get("new_T1T2"), r.get("new_T3")) for t, r in table.items()}}))
    # Winner per budget: more recovered (pct); under 2 points -> SapBERT.
    winners = {}
    for key in result["encoders"]["medcpt"]["encoder_criterion"]:
        m = result["encoders"]["medcpt"]["encoder_criterion"][key]["recovered_pct"]
        s = result["encoders"]["sapbert"]["encoder_criterion"][key]["recovered_pct"]
        winners[key] = {"medcpt_pct": m, "sapbert_pct": s,
                        "primary": "sapbert" if (s is not None and m is not None and (s >= m or m - s < 2)) else "medcpt"}
    result["encoder_choice"] = winners
    result["total_seconds"] = round(time.monotonic() - t0, 1)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=1))
    print(json.dumps(winners, indent=1))


if __name__ == "__main__":
    main()
