"""Frozen source-level partitions for v2; never reads old judgment values.

Only identifiers are taken from v1 packages/audits. Source text is normalized
mechanically to detect exact duplicates, never exposed for prompt construction.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from .io import digest, file_sha, read_jsonl, write_json

ARMS = ("biomarker", "drug", "diet")
SEEDS = {"S": 20261118, "H": 20261125, "reserve": 20261119,
         "candidate_anonymization": 20261126}


def normalize_text(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def normalize_doi(value):
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", normalize_text(value))


def identifier_pmids(value, key=""):
    """Extract explicit PMID fields recursively without interpreting judgments."""
    if isinstance(value, dict):
        return set().union(*(identifier_pmids(v, k) for k, v in value.items()))
    if isinstance(value, list):
        return set().union(*(identifier_pmids(v, key) for v in value))
    if "pmid" in key.lower() and isinstance(value, (str, int)) and str(value).isdigit():
        return {str(value)}
    return set()


def package_pmids(rows):
    result = set()
    for row in rows:
        articles = ([row["article"]] if row["task"] == "extraction" else
                    [row["a"]["article"], row["b"]["article"]])
        result.update(str(article["pmid"]) for article in articles)
    return result


def duplicate_groups(articles):
    """Quarantine whole connected DOI/exact-abstract/explicit-study-ID groups."""
    parent = {str(row["pmid"]): str(row["pmid"]) for row in articles}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)
    by_key = defaultdict(list)
    for row in articles:
        pmid = str(row["pmid"])
        for kind, value in (("doi", normalize_doi(row.get("doi"))),
                            ("abstract", normalize_text(row.get("abstract")))):
            if value:
                by_key[(kind, digest(value))].append(pmid)
        for name in ("study_ids", "trial_registration_ids"):
            for value in row.get(name, []) or []:
                if value:
                    by_key[(name, normalize_text(value))].append(pmid)
    reasons = []
    for (kind, key), pmids in sorted(by_key.items()):
        if len(set(pmids)) > 1:
            for pmid in pmids[1:]:
                union(pmids[0], pmid)
            reasons.append({"kind": kind, "key_sha256": key if kind in {"doi", "abstract"} else digest(key),
                            "pmids": sorted(set(pmids))})
    groups = defaultdict(list)
    for pmid in parent:
        groups[find(pmid)].append(pmid)
    return sorted(sorted(v) for v in groups.values() if len(v) > 1), reasons


def primary_arm(article):
    return next(arm for arm in ARMS if arm in article["arms"])


def draw(pool, articles_by_id, per_arm, seed):
    rng = random.Random(seed)
    selected = []
    for arm in ARMS:
        candidates = sorted(p for p in pool if primary_arm(articles_by_id[p]) == arm)
        chosen = rng.sample(candidates, min(per_arm, len(candidates)))
        selected.extend({"pmid": p, "arm": arm} for p in chosen)
    return selected


def construct(articles, dev_items, old_package, audit, preview_pmids=()):
    by_id = {str(row["pmid"]): row for row in articles}
    if len(by_id) != len(articles):
        raise ValueError("Duplicate PMID records in frozen corpus")
    eligible = {p for p, row in by_id.items() if row["design_eligible"] and row.get("abstract")}
    p = {str(x["pmid"]) for x in dev_items}
    if len(p) != 30 or not p <= eligible:
        raise ValueError("Original 30-item development partition is not intact")
    v1 = package_pmids(old_package) & eligible
    audit_ids = identifier_pmids(audit) & eligible
    preview = {str(x) for x in preview_pmids} & eligible
    groups, reasons = duplicate_groups(articles)
    linked = set().union(*(set(group) for group in groups)) & eligible if groups else set()
    excluded = p | v1 | audit_ids | preview | linked
    pool = eligible - excluded
    s = draw(pool, by_id, 12, SEEDS["S"])
    s_ids = {x["pmid"] for x in s}
    h = draw(pool - s_ids, by_id, 8, SEEDS["H"])
    h_ids = {x["pmid"] for x in h}
    if len(s) != 36 or len(h) != 24:
        raise ValueError("Insufficient source-disjoint selection/check pool")
    f = pool - s_ids - h_ids
    reserve = draw(f, by_id, 1000, SEEDS["reserve"])
    assigned = {"P": p, "V1": v1 - p, "audit": audit_ids - p - v1,
                "preview_additional": preview - p - v1 - audit_ids,
                "duplicates_additional": linked - p - v1 - audit_ids - preview,
                "S": s_ids, "H": h_ids, "F": f,
                "not_extraction_eligible": set(by_id) - eligible}
    assert sum(map(len, assigned.values())) == len(by_id)
    result = {"schema_version": 1, "pipeline_version": "v2", "seeds": SEEDS,
              "arm_order": list(ARMS), "counts": {k: len(v) for k, v in assigned.items()},
              "partition_pmids": {k: sorted(v) for k, v in assigned.items()},
              "partition_sha256": {k: digest(sorted(v)) for k, v in assigned.items()},
              "draws": {"P": dev_items, "S": s, "H": h, "reserve": reserve},
              "reserve_count": len(reserve), "reserve_by_arm": dict(Counter(x["arm"] for x in reserve)),
              "F_by_arm": dict(Counter(primary_arm(by_id[x]) for x in f)),
              "full_production_pmids": sorted(eligible),
              "reserve_first_production_order": [x["pmid"] for x in reserve] + sorted(eligible - {x["pmid"] for x in reserve}),
              "exclusion_audit": {"v1_source_count": len(v1), "audit_source_count": len(audit_ids),
                  "preview_registry_pmids": sorted(preview), "duplicate_groups": groups,
                  "duplicate_keys": reasons,
                  "preview_policy": "All v1 package sources excluded, conservatively covering every original human-subset preview item. Explicit additional exposures expand quarantine before sampling.",
                  "study_link_limit": "Exact DOI/abstract and any explicit study_ids/trial_registration_ids only; no guarantee of detecting shared cohorts from abstracts."},
              "heldout_policy": "H identities are fixed here; its source/inventories/judgments must not be opened by selectors before a unique S winner is locked. No H-based reselection.",
              "labels_read": False}
    return result


def build(root=Path('.'), preview_registry=None):
    root = Path(root)
    paths = {"corpus": "data/derived/corpus.jsonl", "dev": "data/derived/metabolic_dev.json",
             "old_package": "annotation/package/items.jsonl", "audit": "results/control_source_review.json"}
    preview = []
    if preview_registry:
        registry = json.loads(Path(preview_registry).read_text())
        preview = registry["pmids"]
    result = construct(list(read_jsonl(root / paths["corpus"])),
                       json.loads((root / paths["dev"]).read_text())["items"],
                       list(read_jsonl(root / paths["old_package"])),
                       json.loads((root / paths["audit"]).read_text()), preview)
    result["source_sha256"] = {name: file_sha(root / path) for name, path in paths.items()}
    if preview_registry:
        result["source_sha256"]["preview_registry"] = file_sha(preview_registry)
    target = root / "data/derived/partitions.json"
    if target.exists() and json.loads(target.read_text()) != result:
        raise ValueError("Refusing to replace frozen v2 partitions; record a prospective amendment first")
    write_json(target, result)
    # Source bodies are deliberately absent from these manifests.
    for name in ("P", "S", "H", "reserve"):
        write_json(target.parent / f"{name}_manifest.json", {
            "partition": name, "items": result["draws"][name],
            "items_sha256": digest(result["draws"][name]),
            "partition_manifest_sha256": file_sha(target),
            "heldout": name == "H"})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path('.'))
    parser.add_argument("--preview-registry", type=Path)
    args = parser.parse_args()
    result = build(args.root, args.preview_registry)
    print(json.dumps({"counts": result["counts"], "reserve_by_arm": result["reserve_by_arm"]}, indent=2))
