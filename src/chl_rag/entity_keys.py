"""Deterministically capped exact-key candidates and optional hybrid retrieval.

Sampling is balanced by PMID and avoids materializing quadratic claim products.
The targeted, uncapped control audit is separate from the main candidate frame.
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from .io import digest, file_sha, read_jsonl, utcnow, write_json, write_jsonl
from .entity_normalize import normal_form

CONTROL_PMIDS = {"irisin": ["27300472"], "eggs": ["26739035", "32453379"],
                 "amputation": ["28605608", "32744411", "29938883"],
                 "heart_failure": ["29938883"]}
DIABETES_KEYS = {"DISEASE:type_2_diabetes", "DISEASE:type_1_diabetes",
                 "DISEASE:gestational_diabetes", "DISEASE:diabetes"}


def pair_id(a, b):
    return digest(sorted([a["claim_id"], b["claim_id"]]))[:24]


def sign_pattern(a, b):
    if a["sign"] == b["sign"]:
        return "concordant"
    return "effect_null" if "0" in {a["sign"], b["sign"]} else "opposite"


def exposure_compatibility(a, b, *, retrieval=False):
    if a["exposure_key"] == b["exposure_key"]:
        return a["exposure_key"], False
    # A class assertion may be compared with one of its members. Two different
    # molecules are never silently collapsed because they share a class.
    if a.get("exposure_class_key") == b["exposure_key"]:
        return b["exposure_key"], True
    if b.get("exposure_class_key") == a["exposure_key"]:
        return a["exposure_key"], True
    if (retrieval and a["arm"] == b["arm"] == "biomarker"
            and a["kind"] == b["kind"] and a["kind"] in {"level", "association"}
            and a["exposure_key"] in DIABETES_KEYS and b["exposure_key"] in DIABETES_KEYS
            and a["outcome_key"] == b["outcome_key"]):
        return "DISEASEFAMILY:diabetes", False
    return None, False


def outcome_compatibility(a, b, *, retrieval=False):
    if a["outcome_key"] == b["outcome_key"]:
        return a["outcome_key"]
    if not retrieval:
        return None
    if a["outcome_key"] in DIABETES_KEYS and b["outcome_key"] in DIABETES_KEYS:
        return "DISEASEFAMILY:diabetes"
    fa, fb = a["normalization"]["outcome_family"], b["normalization"]["outcome_family"]
    if fa and fa == fb and not fa.startswith("TEXT:"):
        return "FAMILY:" + fa
    ia = set(a["normalization"]["outcome"]["pubtator_ids"])
    ib = set(b["normalization"]["outcome"]["pubtator_ids"])
    if ia and ia == ib:
        return "ENTITY:" + next(iter(ia))
    # Conservative lexical proposals supplement the retrieved semantic context;
    # unlike normalization, proposing a pair does not assert equivalence.
    ta, tb = set(normal_form(a["outcome"]).split()), set(normal_form(b["outcome"]).split())
    if min(len(ta), len(tb)) >= 2 and len(ta & tb) / len(ta | tb) >= 0.8:
        return "RETRIEVED_TEXT:" + digest(sorted([a["outcome_key"], b["outcome_key"]]))[:16]
    return None


def compatible_key(a, b, *, retrieval=False):
    if a["pmid"] == b["pmid"] or a["arm"] != b["arm"] or a.get("hedged") or b.get("hedged"):
        return None
    exposure, _ = exposure_compatibility(a, b, retrieval=retrieval)
    outcome = outcome_compatibility(a, b, retrieval=retrieval)
    if not exposure or not outcome:
        return None
    return "|".join([a["arm"], exposure, outcome])


def pubtator_baseline(a, b):
    aa, bb = a.get("pubtator_relation", {}), b.get("pubtator_relation", {})
    applicable = aa.get("applicable", False) and bb.get("applicable", False)
    covered = applicable and all(x.get("document_available") and x.get("entity_pair_linked") for x in [aa, bb])
    entity_keys = []
    for claim in [a, b]:
        norm = claim["normalization"]
        entity_keys.append(sorted(norm["exposure"]["pubtator_ids"] + norm["outcome"]["pubtator_ids"]))
    same_pair = entity_keys[0] == entity_keys[1] and len(entity_keys[0]) == 2
    sa, sb = aa.get("signs", []), bb.get("signs", [])
    flag = same_pair and (("+" in sa and "-" in sb) or ("-" in sa and "+" in sb))
    return {"applicable": applicable, "covered": bool(covered), "same_entity_pair": same_pair,
            "relations_available_both": bool(aa.get("relation_available") and bb.get("relation_available")),
            "signs_a": sa, "signs_b": sb, "flag": bool(flag) if covered else None}


def make_pair(a, b, key, origin, score=None):
    if a["claim_id"] > b["claim_id"]:
        a, b = b, a
    differences = []
    for field in ["population", "region", "design", "duration", "dose", "sample"]:
        x, y = a.get("context", {}).get(field), b.get("context", {}).get(field)
        if x is not None and y is not None and normal_form(str(x)) != normal_form(str(y)):
            differences.append({"field": field, "a": x, "b": y})
    for field in ["comparator", "kind"]:
        x, y = a.get(field), b.get(field)
        if x is not None and y is not None and normal_form(str(x)) != normal_form(str(y)):
            differences.append({"field": field, "a": x, "b": y})
    if exposure_compatibility(a, b)[1]:
        differences.append({"field": "molecule_class", "a": a["exposure"], "b": b["exposure"]})
    if a["outcome_key"] != b["outcome_key"]:
        differences.append({"field": "outcome_definition", "a": a["outcome"], "b": b["outcome"]})
    if ((a["exposure_key"] in DIABETES_KEYS and b["exposure_key"] in DIABETES_KEYS and a["exposure_key"] != b["exposure_key"])
            or (a["outcome_key"] in DIABETES_KEYS and b["outcome_key"] in DIABETES_KEYS and a["outcome_key"] != b["outcome_key"])):
        differences.append({"field": "disease_subtype", "a": [a["exposure"], a["outcome"]],
                            "b": [b["exposure"], b["outcome"]]})
    return {"pair_id": pair_id(a, b), "claim_a": a["claim_id"], "claim_b": b["claim_id"],
            "pmid_a": a["pmid"], "pmid_b": b["pmid"], "arm": a["arm"], "pair_key": key,
            "sign_pattern": sign_pattern(a, b), "origin": origin, "origins": [origin],
            "retrieval_score": score, "context_differences": differences,
            "pubtator_baseline": pubtator_baseline(a, b)}


def potential_count(left, right, *, same=False):
    a, b = Counter(x["pmid"] for x in left), Counter(x["pmid"] for x in right)
    count = len(left) * len(right) - sum(n * b[p] for p, n in a.items())
    return count // 2 if same else count


def sample_cross(left, right, limit, seed, *, same=False):
    """Sample document groups before claims, bounding work for enormous keys."""
    possible = potential_count(left, right, same=same)
    if not possible or not limit:
        return [], possible
    lg, rg = defaultdict(list), defaultdict(list)
    for row in left:
        lg[row["pmid"]].append(row)
    for row in right:
        rg[row["pmid"]].append(row)
    rng = random.Random(int(digest([42, seed]), 16))
    chosen = {}
    if possible <= max(500, limit * 20):
        for lp in sorted(lg):
            for rp in sorted(rg):
                if lp == rp or (same and lp > rp):
                    continue
                for a, b in itertools.product(lg[lp], rg[rp]):
                    chosen[pair_id(a, b)] = (a, b)
        keys = sorted(chosen)
        rng.shuffle(keys)
        return [chosen[k] for k in keys[:limit]], possible
    lps, rps = sorted(lg), sorted(rg)
    right_positions = {p: i for i, p in enumerate(rps)}
    for _ in range(max(500, limit * 100)):
        lp = rng.choice(lps)
        excluded = right_positions.get(lp)
        size = len(rps) - (excluded is not None)
        if not size:
            continue
        ri = rng.randrange(size)
        if excluded is not None and ri >= excluded:
            ri += 1
        a, b = rng.choice(lg[lp]), rng.choice(rg[rps[ri]])
        chosen[pair_id(a, b)] = (a, b)
        if len(chosen) >= min(limit, possible):
            break
    return [chosen[k] for k in sorted(chosen)], possible


def cap(rows, inconsistent_cap=30, concordant_cap=5):
    by_id = {}
    for row in rows:
        if row["pair_id"] in by_id:
            old = by_id[row["pair_id"]]
            old["origins"] = sorted(set(old["origins"] + row["origins"]))
        else:
            by_id[row["pair_id"]] = row
    ordered = sorted(by_id.values(), key=lambda r: digest([42, r["pair_id"]]))
    opposite = [r for r in ordered if r["sign_pattern"] == "opposite"]
    null = [r for r in ordered if r["sign_pattern"] == "effect_null"]
    concordant = [r for r in ordered if r["sign_pattern"] == "concordant"]
    return (opposite + null)[:inconsistent_cap] + concordant[:concordant_cap]


def key_candidates(claims, inconsistent_cap=30, concordant_cap=5):
    claims = sorted((c for c in claims if not c.get("hedged")), key=lambda c: c["claim_id"])
    groups, bridges = defaultdict(list), defaultdict(list)
    for claim in claims:
        groups[claim["pair_key"]].append(claim)
        cls = claim.get("exposure_class_key")
        if cls and claim["exposure_key"] != cls:
            bridges["|".join([claim["arm"], cls, claim["outcome_key"]])].append(claim)
    selected, stats = {}, {}
    for key, rows in sorted(groups.items()):
        buckets = {s: [c for c in rows if c["sign"] == s] for s in ["+", "-", "0"]}
        proposals, possible = [], Counter()
        for sa, sb in [("+", "-"), ("+", "0"), ("-", "0"), ("+", "+"), ("-", "-"), ("0", "0")]:
            kind = "concordant" if sa == sb else "effect_null" if "0" in [sa, sb] else "opposite"
            pairs, count = sample_cross(buckets[sa], buckets[sb], concordant_cap if kind == "concordant" else inconsistent_cap,
                                       [key, sa, sb], same=sa == sb)
            possible[kind] += count
            proposals.extend(make_pair(a, b, key, "key") for a, b in pairs)
        if key in bridges:
            for sa, sb in itertools.product(["+", "-", "0"], repeat=2):
                right = [c for c in bridges[key] if c["sign"] == sb]
                kind = "concordant" if sa == sb else "effect_null" if "0" in [sa, sb] else "opposite"
                pairs, count = sample_cross(buckets[sa], right, concordant_cap if kind == "concordant" else inconsistent_cap,
                                           [key, "class_bridge", sa, sb])
                possible[kind] += count
                proposals.extend(make_pair(a, b, key, "class_bridge") for a, b in pairs)
        kept = cap(proposals, inconsistent_cap, concordant_cap)
        for row in kept:
            selected[row["pair_id"]] = row
        if kept or sum(possible.values()):
            stats[key] = {"claims": len(rows), "unique_pmids": len({r["pmid"] for r in rows}),
                          "eligible_pair_counts": dict(possible), "selected": len(kept)}
    return sorted(selected.values(), key=lambda r: r["pair_id"]), stats


def retrieve_candidates(claims, base, *, retriever, cache_dir="data/raw/pair_retrieval",
                        inconsistent_cap=30, concordant_cap=5):
    by_pmid = defaultdict(list)
    for c in claims:
        if not c.get("hedged"):
            by_pmid[c["pmid"]].append(c)
    grouped = defaultdict(list)
    for row in base:
        grouped[row["pair_key"]].append(row)
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    in_memory, origins_seen = {}, set()
    # Cache key records the frozen index; endpoint/model changes cannot reuse an
    # unrelated retrieval result. Query encoder metadata is part of index.json.
    index_identity = json.loads(Path("results/index.json").read_text()) if Path("results/index.json").exists() else {"injected_retriever": type(retriever).__name__}
    for position, a in enumerate(sorted(claims, key=lambda c: c["claim_id"])):
        if a.get("hedged"):
            continue
        query = a["normalization"]["exposure"]["label"] + " " + a["normalization"]["outcome"]["label"]
        cache_key = digest({"query": query, "k": 21, "index": index_identity})
        if cache_key not in in_memory:
            path = cache / (cache_key + ".json")
            if path.exists():
                hits = json.loads(path.read_text())["hits"]
            else:
                hits = retriever.search(query, k=21)
                write_json(path, {"query": query, "index_identity": index_identity, "hits": hits})
            in_memory[cache_key] = hits
        hits = [h for h in in_memory[cache_key] if h["pmid"] != a["pmid"]][:20]
        for hit in hits:
            for b in by_pmid.get(hit["pmid"], []):
                key = compatible_key(a, b, retrieval=True)
                if key is None:
                    continue
                row = make_pair(a, b, key, "retrieval", hit.get("rrf_score"))
                origins_seen.add(row["pair_id"])
                grouped[key].append(row)
                # Bounded proposals; deterministic hash ranking makes the final
                # cap independent of input iteration order.
                if len(grouped[key]) > 4 * (inconsistent_cap + concordant_cap):
                    grouped[key] = cap(grouped[key], inconsistent_cap, concordant_cap)
        if position and position % 250 == 0:
            print(json.dumps({"claims_queried": position, "total": len(claims), "unique_queries": len(in_memory)}), flush=True)
    selected = {}
    for rows in grouped.values():
        for row in cap(rows, inconsistent_cap, concordant_cap):
            if row["pair_id"] in selected:
                old = selected[row["pair_id"]]
                old["origins"] = sorted(set(old["origins"] + row["origins"]))
            else:
                selected[row["pair_id"]] = row
    old_ids = {r["pair_id"] for r in base}
    return sorted(selected.values(), key=lambda r: r["pair_id"]), {
        "unique_retrieval_queries": len(in_memory), "top_k": 20,
        "retrieval_compatible_pairs_seen": len(origins_seen),
        "added_over_key": len(set(selected) - old_ids),
        "key_pairs_displaced_by_shared_cap": len(old_ids - set(selected)),
        "cache_directory": str(cache)}


def control_audit(claims, key_pairs, hybrid_pairs=None, corpus="data/derived/corpus.jsonl"):
    metadata = {r["pmid"]: {"publication_types": r["publication_types"], "design_eligible": r["design_eligible"]}
                for r in read_jsonl(corpus)} if Path(corpus).exists() else {}
    present = set(metadata) if metadata else {c["pmid"] for c in claims}
    key_ids = {p["pair_id"] for p in key_pairs}
    hybrid_ids = {p["pair_id"] for p in hybrid_pairs} if hybrid_pairs is not None else None
    by_pmid = defaultdict(list)
    for row in claims:
        if not row.get("hedged"):
            by_pmid[row["pmid"]].append(row)
    audit, targeted = {}, {}
    for name, pmids in CONTROL_PMIDS.items():
        rows = [r for p in pmids for r in by_pmid[p]]
        wanted = []
        for row in rows:
            expo, outcome = row["exposure_key"], row["normalization"]["outcome_family"]
            if name == "eggs" and expo == "DIET:eggs" and outcome == "DISEASE:type_2_diabetes" or name in {"amputation", "heart_failure"} and outcome == name or name == "irisin" and (expo == "BIOMARKER:irisin" or row["outcome_key"] == "BIOMARKER:irisin"):
                wanted.append(row)
        primary_audit, irisin_pair_space = None, None
        if name == "irisin":
            wanted = []
            for row in claims:
                source = metadata.get(row["pmid"], {})
                primary = source.get("design_eligible", False) and not (
                    set(source.get("publication_types", [])) & {"Meta-Analysis", "Review", "Systematic Review"})
                axes = {row["exposure_key"], row["outcome_key"]}
                if (primary and not row.get("hedged") and row["arm"] == "biomarker"
                        and row["kind"] in {"level", "association"}
                        and "BIOMARKER:irisin" in axes and bool(axes & DIABETES_KEYS)):
                    wanted.append(row)
            primary_audit = {
                "scope": "All extracted design-eligible primary-study irisin/diabetes level or association claims; PubMed Review/Systematic Review/Meta-Analysis excluded. Directions are system extractions, not gold contradictions.",
                "primary_pmids": sorted({r["pmid"] for r in wanted}),
                "sign_counts": dict(Counter(r["sign"] for r in wanted)),
                "claims": [{k: r.get(k) for k in ["claim_id", "pmid", "kind", "exposure", "outcome", "sign", "significant", "context", "evidence_text"]}
                           for r in sorted(wanted, key=lambda c: c["claim_id"])],
                "reference_review_present": "27300472" in present,
            }
        proposals = {}
        # Negative control considers all comparable partner studies in the
        # extracted corpus. Positive controls use only the fixed references.
        if name == "irisin":
            groups = defaultdict(list)
            for row in wanted:
                # Opposite axes and level-versus-association claims are separate
                # comparisons; subtype differences remain explicit candidates.
                groups[(row["kind"], row["exposure_key"] == "BIOMARKER:irisin")].append(row)
            irisin_pair_space = Counter()
            for group, source_rows in groups.items():
                buckets = {s: [c for c in source_rows if c["sign"] == s] for s in ["+", "-", "0"]}
                for sa, sb in [("+", "-"), ("+", "0"), ("-", "0"), ("+", "+"), ("-", "-"), ("0", "0")]:
                    pattern = "concordant" if sa == sb else "effect_null" if "0" in [sa, sb] else "opposite"
                    sampled, possible = sample_cross(buckets[sa], buckets[sb], 10 if pattern == "concordant" else 120,
                                                     ["irisin_primary_control", group, sa, sb], same=sa == sb)
                    irisin_pair_space[pattern] += possible
                    for a, b in sampled:
                        key = compatible_key(a, b, retrieval=True)
                        if key:
                            row = make_pair(a, b, key, "targeted_control")
                            proposals[row["pair_id"]] = row
            proposals = {r["pair_id"]: r for r in cap(proposals.values(), 120, 10)}
        else:
            partners = claims if name == "heart_failure" else wanted
            for a in wanted:
                for b in partners:
                    key = compatible_key(a, b, retrieval=True)
                    if key:
                        row = make_pair(a, b, key, "targeted_control")
                        proposals[row["pair_id"]] = row
        targeted.update(proposals)
        audit[name] = {"fixed_pmids": pmids, "pmids_present": [p for p in pmids if p in present],
                       "pmids_with_any_claims": [p for p in pmids if by_pmid[p]],
                       "relevant_claims": len(wanted),
                       "uncapped_targeted_pairs": sum(irisin_pair_space.values()) if irisin_pair_space is not None else len(proposals),
                       "targeted_pairs_written": len(proposals),
                       "targeted_pair_cap": {"inconsistent": 120, "concordant": 10} if name == "irisin" else None,
                       "eligible_pair_counts_by_sign": dict(irisin_pair_space) if irisin_pair_space is not None else None,
                       "primary_study_audit": primary_audit,
                       "pairs_in_key_candidates": len(set(proposals) & key_ids),
                       "pairs_in_hybrid_candidates": len(set(proposals) & hybrid_ids) if hybrid_ids is not None else None,
                       "targeted_pair_ids": sorted(proposals)}
    return audit, sorted(targeted.values(), key=lambda r: r["pair_id"])


def run(claims_path="data/derived/claims_normalized.jsonl", retrieval=False,
        inconsistent_cap=30, concordant_cap=5):
    claims = list(read_jsonl(claims_path))
    if len({c["claim_id"] for c in claims}) != len(claims):
        raise ValueError("Duplicate claim IDs: resolve duplicates before candidate generation")
    key_pairs, key_stats = key_candidates(claims, inconsistent_cap, concordant_cap)
    write_jsonl("data/derived/pairs_key.jsonl", key_pairs)
    final, retrieval_stats = key_pairs, {"status": "not_run", "reason": "--retrieval not selected; hybrid index required"}
    if retrieval:
        from .index import Retriever
        final, retrieval_stats = retrieve_candidates(claims, key_pairs, retriever=Retriever(),
                                                     inconsistent_cap=inconsistent_cap, concordant_cap=concordant_cap)
        write_jsonl("data/derived/pairs_hybrid.jsonl", final)
    write_jsonl("data/derived/pairs_candidates.jsonl", final)
    controls, targeted = control_audit(claims, key_pairs, final if retrieval else None)
    write_jsonl("data/derived/pairs_controls.jsonl", targeted)
    summary = {"created_at": utcnow(), "claims_sha256": file_sha(claims_path),
               "claims": len(claims), "hedged_excluded": sum(bool(c.get("hedged")) for c in claims),
               "key_pairs": len(key_pairs), "final_pairs": len(final),
               "counts_by_arm": dict(Counter(p["arm"] for p in final)),
               "counts_by_sign_pattern": dict(Counter(p["sign_pattern"] for p in final)),
               "caps": {"opposite_then_effect_null": inconsistent_cap, "concordant": concordant_cap},
               "sampling": "Seed42; PMID-balanced bounded sampling within sign buckets; deterministic hash caps. No exhaustive quadratic products.",
               "retrieval": retrieval_stats, "controls": controls,
               "pubtator_baseline": {"covered_pairs": sum(p["pubtator_baseline"]["covered"] for p in final),
                                      "flagged_pairs": sum(p["pubtator_baseline"]["flag"] is True for p in final)},
               "output_sha256": file_sha("data/derived/pairs_candidates.jsonl")}
    write_json("results/normalization_pairs.json", summary)
    write_json("results/normalization_pair_keys.json", key_stats)
    print(json.dumps(summary), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", default="data/derived/claims_normalized.jsonl")
    parser.add_argument("--retrieval", action="store_true")
    parser.add_argument("--inconsistent-cap", type=int, default=30)
    parser.add_argument("--concordant-cap", type=int, default=5)
    args = parser.parse_args()
    if args.inconsistent_cap < 0 or args.concordant_cap < 0:
        parser.error("Caps must be nonnegative")
    run(args.claims, args.retrieval, args.inconsistent_cap, args.concordant_cap)
