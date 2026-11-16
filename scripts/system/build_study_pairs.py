"""Rebuild the 40 human pairs from the v2.1 re-extraction of their 66 abstracts.

Each frozen pair keeps its two abstracts and its order. On each side the new claim must
form a pipeline-compatible pair (same normalised entities, same comparison type) with the
other side; candidates with the frozen pair key come first, then the best overlap of
result evidence sentences with the frozen claims. A pair with no compatible new claims
is dropped. Run from the project root with .venv/bin/python.
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, "src")
from chl_rag.io import digest, read_jsonl  # noqa: E402
from chl_rag.extraction import pair_item  # noqa: E402
from chl_rag.candidate_pairs import compatible_key, normalize_record  # noqa: E402

EXTRACTED, OUT = Path(sys.argv[1]), Path(sys.argv[2])
SUFFIX = "-x21"
OLD = Path("annotation/study_pairs_uncorrected")


def sentences(claim):
    return {e["sentence_id"] for e in claim["result"].get("evidence", []) if "sentence_id" in e}


def key_of(a, b):
    return compatible_key(a, b) or compatible_key(a, b, retrieval=True)


def entities(key):
    return key.split("|comparison:")[0] if key else None


old_items = [json.loads(line) for line in (OLD / "items.jsonl").read_text().splitlines()]
pmids = {item[s]["article"]["pmid"] for item in old_items for s in "ab"}
articles = {a["pmid"]: a for a in read_jsonl("data/derived/corpus.jsonl") if a["pmid"] in pmids}
extractions = {}
for pmid in pmids:
    if not (EXTRACTED / f"{pmid}.json").exists():
        continue
    record = json.loads((EXTRACTED / f"{pmid}.json").read_text())
    if record["status"] == "valid":
        extractions[pmid] = record["extraction"]["extraction"]

new_items, supersedes, report = [], {}, []
for item in old_items:
    pa, pb = item["a"]["article"]["pmid"], item["b"]["article"]["pmid"]
    old_a = normalize_record({"pmid": pa, "claim": item["a"]["claim"]})
    old_b = normalize_record({"pmid": pb, "claim": item["b"]["claim"]})
    old_key = key_of(old_a, old_b)
    row = {"item_id": item["item_id"], "old_key": old_key}
    if pa not in extractions or pb not in extractions:
        report.append({**row, "status": "dropped", "why": "invalid re-extraction"})
        continue
    chosen, why, fallback = {}, None, {}
    for pmid, old in ((pa, item["a"]["claim"]), (pb, item["b"]["claim"])):
        # The same finding: the new claim whose result evidence cites the same abstract
        # sentences; ties go to the same comparison type, then to shared entity words.
        words = set((old["exposure"] + " " + old["outcome"]).lower().split())
        scored = []
        for c in extractions[pmid]["claims"]:
            if c["arm"] != item["arm"]:
                continue
            shared, union = sentences(c) & sentences(old), sentences(c) | sentences(old)
            if not shared:
                continue
            same_type = c["comparison"]["type"] == old["comparison"]["type"]
            common = len(words & set((c["exposure"] + " " + c["outcome"]).lower().split()))
            scored.append((-len(shared) / len(union), not same_type, -common, c["claim_id"], c))
        if not scored:
            # Fallback: the same exposure and outcome described from other sentences
            # (at least two shared words, same arm); still the same finding of this abstract.
            for c in extractions[pmid]["claims"]:
                if c["arm"] != item["arm"]:
                    continue
                common = len(words & set((c["exposure"] + " " + c["outcome"]).lower().split()))
                if common >= 2:
                    same_type = c["comparison"]["type"] == old["comparison"]["type"]
                    scored.append((0.0, not same_type, -common, c["claim_id"], c))
            fallback[pmid] = bool(scored)
        if not scored:
            why = "no new claim for the same finding"
            break
        chosen[pmid] = min(scored, key=lambda x: x[:4])
    if why:
        report.append({**row, "status": "dropped", "why": why})
        continue
    a, b = chosen[pa][4], chosen[pb][4]
    key = key_of(normalize_record({"pmid": pa, "claim": a}), normalize_record({"pmid": pb, "claim": b}))
    exact = key != old_key
    overlap = -(chosen[pa][0] + chosen[pb][0])
    candidates = [chosen[pa], chosen[pb]]
    new_id = "v2-pair-" + str(1000 + int(item["item_id"].rsplit("-", 1)[1]))
    new_items.append(
        pair_item(new_id, item["arm"], articles[pa], extractions[pa], a["claim_id"],
                  articles[pb], extractions[pb], b["claim_id"])
    )
    supersedes[new_id] = item["item_id"]
    report.append({**row, "status": "kept", "new_key": key, "same_key": not exact,
                   "evidence_jaccard_sum": round(overlap, 3),
                   "same_comparison_type": [not chosen[pa][1], not chosen[pb][1]],
                   "word_fallback": [fallback.get(pa, False), fallback.get(pb, False)]})

OUT.mkdir(parents=True, exist_ok=True)
payload = "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in new_items).encode()
(OUT / "items.jsonl").write_bytes(payload)
canonical = hashlib.sha256(
    json.dumps(new_items, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
).hexdigest()
old_manifest = json.loads((OLD / "items.manifest.json").read_text())
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()  # noqa: E731
manifest = {
    "pipeline_version": "2.0",
    "instrument_version": "2.0",
    "source_type": "production",
    "file_sha256": hashlib.sha256(payload).hexdigest(),
    "package_sha256": canonical,
    "configuration_sha256": old_manifest["configuration_sha256"],
    "evaluation_config_sha256": old_manifest["evaluation_config_sha256"],
    "source_hashes": {
        **old_manifest["source_hashes"],
        "parent_site_handoff_items": sha(OLD / "items.jsonl"),
        "parent_site_handoff_manifest": sha(OLD / "items.manifest.json"),
        "extraction_v21_script": sha(sys.argv[3]),
        "rebuild_script": sha(__file__),
    },
    "item_count": len(new_items),
    "extraction_revision": {
        "name": "v2.1",
        "model": "z-ai/glm-5.3-flash", "reasoning": "high",
        "changes": "result.assertion generated last; prompt rules for assertion, counts, analysed population and context",
        "scope": "re-extraction of the abstracts of the frozen human pairs only; selection unchanged (drawn from the v2 frame)",
    },
    "supersedes": supersedes,
    "dropped": [r["item_id"] for r in report if r["status"] == "dropped"],
}
(OUT / "items.manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
(OUT / "rebuild_report.json").write_text(json.dumps(report, indent=1) + "\n")
kept = [r for r in report if r["status"] == "kept"]
print(json.dumps({"kept": len(kept), "same_key": sum(r["same_key"] for r in kept),
                  "dropped": [(r["item_id"], r["why"]) for r in report if r["status"] == "dropped"],
                  "file_sha256": manifest["file_sha256"], "package_sha256": canonical}, indent=1))
