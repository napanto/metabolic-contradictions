"""Addendum 08E: ordered pair lists ready for classification, one per tau* candidate.

Order (08E DRAFT section 4): (1) 1,500 new T3 controls at >= tau*, ascending sha256(pair_id);
(2) all new T1, then all new T2 at >= tau*, ascending sha256(pair_id); (3) the lower band
[lower, tau*) of T1+T2 pairs, ascending sha256(pair_id), classified until B is reached.
Records use the 08A/08D frame format. No model API is called: the classifier requests are
built offline for a check of the first pairs of every list.

    PYTHONPATH=src .venv/bin/python scripts/build_08e_pair_lists.py --out-dir <dir> --manifest <json>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np

from chl_rag.claim_pairing import enumerate_new_pairs, load_claims, semantic_pair_record

PROJECT = Path(".")
CLAIMS = PROJECT / "data/derived/production/full/claims_normalized.jsonl"
EXTRACTIONS = PROJECT / "data/derived/production/full/extractions.jsonl"
FRAME_08D = PROJECT / "data/derived/production_v21_uncapped/full/pairs_candidates.jsonl"
CORPUS = PROJECT / "data/derived/corpus.jsonl"
CANDIDATES = {0.90: 0.88, 0.92: 0.90}   # tau* -> lower edge of the sampled band
CONTROLS = 1500


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def order_key(pair_id):
    return hashlib.sha256(pair_id.encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path.home() / ".cache/chl-newpairs"))
    ap.add_argument("--threads", type=int, default=28)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--candidates", default=None,
                    help="tau*:lower pairs, e.g. 0.88:0.85 (default: the two pre-registration candidates)")
    args = ap.parse_args()
    global CANDIDATES
    if args.candidates:
        CANDIDATES = {float(t): float(l) for t, l in (x.split(":") for x in args.candidates.split(","))}
    t0 = time.monotonic()
    claims = load_claims(CLAIMS)
    exposures = sorted({c["exposure"] for c in claims})
    outcomes = sorted({c["outcome"] for c in claims})
    cache = Path(args.cache).expanduser()

    def vectors(name, texts):
        key = hashlib.sha256(("\n".join(texts) + "|medcpt").encode()).hexdigest()[:16]
        return dict(zip(texts, np.load(cache / f"medcpt_{name}_{key}.npy")))

    ev, ov = vectors("exposure", exposures), vectors("outcome", outcomes)
    existing = set()
    with open(FRAME_08D) as handle:
        for line in handle:
            r = json.loads(line)
            existing.add(tuple(sorted((r["claim_a"], r["claim_b"]))))
    floor = min(CANDIDATES.values())
    found = list(enumerate_new_pairs(claims, ev, ov, floor, threads=args.threads,
                                     existing_pairs=frozenset(existing)))
    needed = {p["a"]["claim_id"] for p in found} | {p["b"]["claim_id"] for p in found}
    full = {}
    with open(CLAIMS) as handle:
        for line in handle:
            r = json.loads(line)
            if r["claim_id"] in needed:
                full[r["claim_id"]] = r
    records = []
    for p in found:
        rec = semantic_pair_record(full[p["a"]["claim_id"]], full[p["b"]["claim_id"]],
                                   p["score"], p["cos_exposure"], p["cos_outcome"])
        records.append((order_key(rec["pair_id"]), p["pattern"], p["score"], rec))
    if len({r[3]["pair_id"] for r in records}) != len(records):
        raise SystemExit("duplicate pair ids among new semantic pairs")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"addendum": "08E (lists prepared before registration; nothing classified)",
                "encoder": "MedCPT query encoder (primary by the registered criterion)",
                "claims_sha256": sha(CLAIMS), "frame_08D_sha256": sha(FRAME_08D),
                "new_pairs_enumerated_at_or_above": floor, "lists": {}}
    for tau, lower in sorted(CANDIDATES.items()):
        at = [r for r in records if r[2] >= tau - 1e-6]
        band = sorted((r for r in records if lower - 1e-6 <= r[2] < tau - 1e-6 and r[1] != "concordant"),
                      key=lambda r: r[0])
        controls = sorted((r for r in at if r[1] == "concordant"), key=lambda r: r[0])[:CONTROLS]
        t1 = sorted((r for r in at if r[1] == "opposite"), key=lambda r: r[0])
        t2 = sorted((r for r in at if r[1] == "effect_null"), key=lambda r: r[0])
        steps = [("1_controls_T3", controls), ("2a_T1", t1), ("2b_T2", t2), ("3_band_T1T2", band)]
        path = out_dir / f"pairs_08e_tau{int(round(tau * 100)):03d}.jsonl"
        position = 0
        with open(path, "w") as handle:
            for step, rows in steps:
                for _k, _pat, _score, rec in rows:
                    position += 1
                    rec = dict(rec)
                    rec["order_08e"] = {"tau_star": tau, "step": step, "position": position,
                                        "sha256_order_key": _k, "band": [lower, tau] if step.startswith("3") else None}
                    handle.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        manifest["lists"][f"{tau:.2f}"] = {
            "path": str(path), "sha256": sha(path), "pairs": position,
            "steps": {step: len(rows) for step, rows in steps},
            "band": [lower, tau], "classify_through_step_2": len(controls) + len(t1) + len(t2)}
    # Offline check: the classifier request builds for the first pairs of each list.
    from chl_rag.candidate_pairs import classification_request
    sample = []
    for info in manifest["lists"].values():
        with open(info["path"]) as handle:
            for _ in range(3):
                sample.append(json.loads(handle.readline()))
    pmids = {p["pmid_a"] for p in sample} | {p["pmid_b"] for p in sample}
    extractions, articles = {}, {}
    with open(EXTRACTIONS) as handle:
        for line in handle:
            e = json.loads(line)
            if e["pmid"] in pmids:
                extractions[e["pmid"]] = e
    with open(CORPUS) as handle:
        for line in handle:
            a = json.loads(line)
            if a["pmid"] in pmids:
                articles[a["pmid"]] = a
    built = 0
    for p in sample:
        classification_request(p, articles, extractions, model="z-ai/glm-5.3-flash", reasoning="high",
                               max_output_tokens=131072,
                               prompt_path=str(PROJECT / "configs/classification_prompt.json"))
        built += 1
    manifest["offline_request_check"] = {"requests_built": built, "api_calls": 0}
    manifest["seconds"] = round(time.monotonic() - t0, 1)
    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(args.manifest).write_text(json.dumps(manifest, indent=1))
    print(json.dumps({t: {k: v for k, v in i.items() if k != "path"} for t, i in manifest["lists"].items()}, indent=1))


if __name__ == "__main__":
    main()
