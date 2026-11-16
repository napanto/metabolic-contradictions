"""Macros and a CSV for the report and slides on candidate pairing (08A capped, 08D uncapped, 08E semantic).

Macro names carry no digits. Values not yet known (tau*, B before 08D ends; every
post-classification value before classification ends) print a visible placeholder.
"""
from __future__ import annotations

import csv
import json
import math
from collections import Counter
from pathlib import Path

PLACEHOLDER = r"\textbf{[n/a]}"
ROUTES = {("entity_key",): "KeyOnly", ("hybrid_retrieval",): "RetrievalOnly",
          ("entity_key", "hybrid_retrieval"): "Both", ("semantic_claim",): "Semantic"}
ARMS = {"biomarker": "Biomarker", "drug": "Drug", "diet": "Diet"}
SIGNS = {"opposite": "Opposite", "effect_null": "EffectNull", "concordant": "Concordant",
         "unrepresented": "Unrepresented"}
CATEGORIES = {"concordant": "Concordant", "contradictory": "Contradictory",
              "apparent_contradiction": "ApparentContradiction", "not_replicated": "NotReplicated",
              "not_comparable": "NotComparable"}
FLAGGED = {"contradictory", "apparent_contradiction", "not_replicated"}
STRATA = {"1_controls_T3": "Controls", "2a_T1": "TOne", "2b_T2": "TTwo", "3_band_T1T2": "Band"}


def fmt_int(n):
    return f"{n:,}" if isinstance(n, int) else PLACEHOLDER


def fmt(x, digits=2):
    if x is None:
        return PLACEHOLDER
    if isinstance(x, int):
        return fmt_int(x)
    return f"{x:.{digits}f}"


def frame_counts(path):
    by_route, by_arm, by_sign = Counter(), Counter(), Counter()
    with open(path) as handle:
        for line in handle:
            r = json.loads(line)
            by_route[ROUTES.get(tuple(sorted(r["origins"])), "Other")] += 1
            by_arm[ARMS.get(r["arm"], "Other")] += 1
            by_sign[SIGNS.get(r["sign_pattern"], "Other")] += 1
    return by_route, by_arm, by_sign


def wilson(k, n, z=1.959964):
    """Wilson score interval for k successes in n trials."""
    if not n:
        return (float("nan"), float("nan"))
    phat = k / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (centre - half, centre + half)


def classified_stats(path, list_path=None):
    """Per route (and, for semantic pairs, per stratum of the 08E list): classified, valid, flagged, categories.

    path: JSONL with pair_id, status and verdict.label (pairs_classified format).
    list_path: the 08E ordered list, to map semantic pair ids to strata.
    """
    strata = {}
    if list_path:
        with open(list_path) as handle:
            for line in handle:
                r = json.loads(line)
                strata[r["pair_id"]] = (STRATA[r["order_08e"]["step"]], ROUTES[tuple(sorted(r["origins"]))])
    stats = {}
    with open(path) as handle:
        for line in handle:
            r = json.loads(line)
            route = ROUTES.get(tuple(sorted(r.get("origins") or [])), None)
            stratum = strata.get(r.get("pair_id"), (None, route))[0]
            route = route or strata.get(r.get("pair_id"), (None, "Semantic"))[1]
            for key in [route] + ([route + stratum] if stratum else []):
                s = stats.setdefault(key, Counter())
                s["classified"] += 1
                verdict = r.get("verdict") or {}
                status = r.get("status") or r.get("classification_status") or "valid"
                if status == "valid" and verdict.get("label"):
                    s["valid"] += 1
                    s["cat_" + verdict["label"]] += 1
                    if verdict["label"] in FLAGGED:
                        s["flagged"] += 1
                    if r.get("sign_pattern") in ("opposite", "effect_null"):
                        s["disc_valid"] += 1
                        s["disc_not_comparable"] += verdict["label"] == "not_comparable"
    return stats


def build(*, capped, uncapped, counts_08e, lists_manifest, out_dir, tau_star=None, budget=None,
          cost=None, classified=()):
    """Write macros.tex and pairing_counts.csv; return the macro dict."""
    m = {}
    rows = []
    cr, _ca, _cs = frame_counts(capped)
    m["PairCappedTotal"] = fmt_int(sum(cr.values()))
    for r in ("KeyOnly", "RetrievalOnly", "Both"):
        m[f"PairCapped{r}"] = fmt_int(cr[r])
        rows.append(("08A_capped", "route", r, cr[r]))
    ur, ua, us = frame_counts(uncapped)
    m["PairUncappedTotal"] = fmt_int(sum(ur.values()))
    m["PairUncappedAdded"] = fmt_int(sum(ur.values()) - sum(cr.values()))
    for r in ("KeyOnly", "RetrievalOnly", "Both"):
        m[f"PairUncapped{r}"] = fmt_int(ur[r])
        rows.append(("08D_uncapped", "route", r, ur[r]))
    for a in ARMS.values():
        m[f"PairUncapped{a}"] = fmt_int(ua[a])
        rows.append(("08D_uncapped", "arm", a, ua[a]))
    for s in SIGNS.values():
        m[f"PairUncapped{s}"] = fmt_int(us[s])
        rows.append(("08D_uncapped", "sign", s, us[s]))

    c = json.loads(Path(counts_08e).read_text())
    lists = json.loads(Path(lists_manifest).read_text())
    primary = "medcpt"
    other = "sapbert"
    enc = c["encoders"][primary]
    names = {"medcpt": "MedCPT (query encoder)", "sapbert": "SapBERT"}
    m["PairSemEncoder"] = names[primary]
    m["PairSemEncoderOther"] = names[other]
    m["PairSemEligibleClaims"] = fmt_int(c["eligible_claims"])
    m["PairSemExposureTexts"] = fmt_int(c["unique_exposure_texts"])
    m["PairSemOutcomeTexts"] = fmt_int(c["unique_outcome_texts"])
    m["PairSemGenericExcluded"] = fmt_int(enc["grid"]["excluded_generic_exposure_claims"])
    first_run = Path(counts_08e).with_name("counts_grid_vs08A.json")
    if first_run.exists():   # MedCPT encoding was timed on the first run; later runs read the cache
        secs = json.loads(first_run.read_text())["encode_seconds"]
        m["PairSemEncodeMinutes"] = fmt((secs["exposure"] + secs["outcome"]) / 60, 0)
    else:
        m["PairSemEncodeMinutes"] = PLACEHOLDER
    sap = c["encoders"]["sapbert"]["encode_seconds"]
    m["PairSemEncodeMinutesOther"] = fmt((sap["exposure"] + sap["outcome"]) / 60, 0)
    m["PairSemSearchSeconds"] = fmt(enc["grid"]["seconds"], 0)
    for name, key in (("Primary", primary), ("Other", other)):
        crit = c["encoders"][key]["encoder_criterion"]
        k = f"B={budget:g},c={cost}" if (budget is not None and cost is not None) else None
        if k and k not in crit and crit:
            # The criterion was computed offline on a grid of B and c; use the nearest grid point.
            def dist(key):
                b, cc = (float(x.split("=")[1]) for x in key.split(","))
                return (abs(b - budget), abs(cc - cost))
            k = min(crit, key=dist)
            m["PairSemRecoveryGridPoint"] = k.replace(",", ", ").replace("c=", "c = ").replace("B=", "B = ")
        value = crit.get(k, {}).get("recovered_pct") if k else None
        m[f"PairSemRecovery{name}"] = fmt(value, 1)
    for t, row in enc["new_pairs_table"].items():
        rows.append(("08E_semantic", "new_T1T2_at_tau", t, row.get("new_T1T2")))
        rows.append(("08E_semantic", "new_T3_at_tau", t, row.get("new_T3")))
        diag = enc["key_route_overlap_by_cause"][t]
        rows.append(("08E_semantic", "key_route_recovered_at_tau", t, diag["recovered"]))
    for t, row in c["encoders"][other]["new_pairs_table"].items():
        rows.append(("08E_semantic_other_encoder", "new_T1T2_at_tau", t, row.get("new_T1T2")))

    m["PairTauStar"] = fmt(tau_star) if tau_star is not None else PLACEHOLDER
    m["PairBudgetUSD"] = fmt(budget) if budget is not None else PLACEHOLDER
    m["PairCostPerPair"] = f"{cost:.5f}" if cost is not None else PLACEHOLDER
    keys = ("PairSemNewTOne", "PairSemNewTTwo", "PairSemNewTThree", "PairSemControls", "PairSemBandLow",
            "PairSemBandHigh", "PairSemBandPairs", "PairSemBandSampled", "PairSemTenXRatioContr",
            "PairSemTenXRatioAll", "PairSemKeyRoutePairs", "PairSemKeyRecovered", "PairSemMissStoplist",
            "PairSemMissClassMember", "PairSemMissComparison", "PairSemMissExposureOnly",
            "PairSemMissOutcomeOnly", "PairSemMissBoth", "PairSemListPairs")
    for k in keys:
        m[k] = PLACEHOLDER
    if tau_star is not None:
        t = f"{tau_star:.2f}"
        row = enc["new_pairs_table"][t]
        info = lists["lists"][t]
        diag = enc["key_route_overlap_by_cause"][t]
        m.update({"PairSemNewTOne": fmt_int(row["new_T1"]), "PairSemNewTTwo": fmt_int(row["new_T2"]),
                  "PairSemNewTThree": fmt_int(row["new_T3"]), "PairSemControls": fmt_int(info["steps"]["1_controls_T3"]),
                  "PairSemBandLow": fmt(info["band"][0]), "PairSemBandHigh": fmt(info["band"][1]),
                  "PairSemBandPairs": fmt_int(info["steps"]["3_band_T1T2"]),
                  "PairSemTenXRatioContr": fmt(row["ratio_T1T2_vs_11391"]),
                  "PairSemTenXRatioAll": fmt(row["ratio_all_vs_42972"]),
                  "PairSemKeyRoutePairs": fmt_int(diag["key_route_T1T2"]),
                  "PairSemKeyRecovered": fmt_int(diag["recovered"]),
                  "PairSemMissStoplist": fmt_int(diag["stoplist"]),
                  "PairSemMissClassMember": fmt_int(diag["class_member"]),
                  "PairSemMissComparison": fmt_int(diag["comparison_type"]),
                  "PairSemMissExposureOnly": fmt_int(diag["fails_E_only"]),
                  "PairSemMissOutcomeOnly": fmt_int(diag["fails_O_only"]),
                  "PairSemMissBoth": fmt_int(diag["fails_both"]),
                  "PairSemListPairs": fmt_int(info["pairs"])})
        if budget is not None and cost is not None:
            spent = (info["classify_through_step_2"]) * cost
            m["PairSemBandSampled"] = fmt_int(max(0, min(info["steps"]["3_band_T1T2"],
                                                         int(math.floor((budget - spent) / cost)))))

    # Post-classification, per route and (semantic) stratum: placeholders until outputs exist.
    stats = {}
    for path, list_path in classified:
        for k, v in classified_stats(path, list_path).items():
            stats.setdefault(k, Counter()).update(v)
    groups = ["KeyOnly", "RetrievalOnly", "Both", "Semantic"] + ["Semantic" + s for s in STRATA.values()]
    for g in groups:
        s = stats.get(g)
        m[f"PairCls{g}Classified"] = fmt_int(s["classified"]) if s else PLACEHOLDER
        m[f"PairCls{g}Valid"] = fmt_int(s["valid"]) if s else PLACEHOLDER
        m[f"PairCls{g}FlaggedShare"] = (f"{100 * s['flagged'] / s['valid']:.1f}\\%" if s and s["valid"] else PLACEHOLDER)
        m[f"PairCls{g}DiscNotComparableShare"] = (f"{100 * s['disc_not_comparable'] / s['disc_valid']:.0f}\\%"
                                                  if s and s.get("disc_valid") else PLACEHOLDER)
        m[f"PairCls{g}NotComparableShare"] = (f"{100 * s['cat_not_comparable'] / s['valid']:.0f}\\%"
                                              if s and s["valid"] and "cat_not_comparable" in s else PLACEHOLDER)
        for label, name in CATEGORIES.items():
            m[f"PairCls{g}{name}"] = fmt_int(s["cat_" + label]) if s else PLACEHOLDER
        if s:
            rows.append(("classified", g, "valid", s["valid"]))
            rows.append(("classified", g, "flagged", s["flagged"]))

    key = [stats[g] for g in ("KeyOnly", "Both") if g in stats]
    dv = sum(x.get("disc_valid", 0) for x in key)
    m["PairClsKeyRouteDiscNotComparableShare"] = (
        f"{100 * sum(x.get('disc_not_comparable', 0) for x in key) / dv:.0f}\\%" if dv else PLACEHOLDER)

    # 08E outcome: what was actually classified (replaces the pre-run projection of the band sample).
    m.update({k: PLACEHOLDER for k in ("PairSemTTwoSampledShare", "PairSemTTwoFlaggedLow", "PairSemTTwoFlaggedHigh",
                                       "PairSemTOneFlaggedLow", "PairSemTOneFlaggedHigh", "PairSemSpentUSD",
                                       "PairSemCostPerPairActual", "PairSemStopTime", "PairSemTTwoUnclassified")})
    if "Semantic" in stats and tau_star is not None:
        row = enc["new_pairs_table"][f"{tau_star:.2f}"]
        band = stats.get("SemanticBand")
        m["PairSemBandSampled"] = fmt_int(band["classified"] if band else 0)
        t2 = stats.get("SemanticTTwo")
        if t2:
            m["PairSemTTwoSampledShare"] = f"{100 * t2['classified'] / row['new_T2']:.1f}\\%"
            m["PairSemTTwoUnclassified"] = fmt_int(row["new_T2"] - t2["classified"])
        for g, name in (("SemanticTTwo", "TTwo"), ("SemanticTOne", "TOne")):
            st = stats.get(g)
            if st and st["valid"]:
                lo, hi = wilson(st["flagged"], st["valid"])
                m[f"PairSem{name}FlaggedLow"] = f"{100 * lo:.1f}\\%"
                m[f"PairSem{name}FlaggedHigh"] = f"{100 * hi:.1f}\\%"
        summary = Path("results/production_v21_08e/summary.json")
        if summary.exists():
            sm = json.loads(summary.read_text())
            m["PairSemSpentUSD"] = fmt(sm["spent_08E_usd"])
            m["PairSemCostPerPairActual"] = f"{sm['spent_08E_usd'] / sm['terminal_pairs']:.5f}"
            if sm.get("refusal", {}).get("at"):
                from datetime import datetime
                from zoneinfo import ZoneInfo
                at = datetime.fromisoformat(sm["refusal"]["at"]).astimezone(ZoneInfo("Europe/Rome"))
                m["PairSemStopTime"] = at.strftime("%H:%M")

    for name in m:
        if any(ch.isdigit() for ch in name):
            raise ValueError(f"macro name with a digit: {name}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lines = ["% Generated by chl_rag.pairing_report; do not edit by hand."]
    lines += [f"\\providecommand{{\\{k}}}{{}}\\renewcommand{{\\{k}}}{{{v}}}" for k, v in sorted(m.items())]
    (out / "macros.tex").write_text("\n".join(lines) + "\n")
    with open(out / "pairing_counts.csv", "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["source", "dimension", "value", "count"])
        w.writerows(rows)
    return m
