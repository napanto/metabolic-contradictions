"""Download PubTator 3 BioC JSON in paced batches, retaining coverage failures."""
from __future__ import annotations

import argparse
import gzip
import io
import json
import urllib.parse

from .corpus import DERIVED, ROOT, TOOL, request, sha256, utcnow, write_json

RAW = ROOT / "data/raw/pubtator"
ENDPOINT = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson"


def documents(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ["PubTator3", "documents"]:
            if key in data:
                return data[key]
    raise ValueError(f"Unexpected PubTator export structure: {type(data)}")


def normalize(doc):
    entities, relations = [], []
    for passage in doc.get("passages", []):
        for annotation in passage.get("annotations", []):
            infons = annotation.get("infons", {})
            entities.append({"text": annotation.get("text"), "identifier": infons.get("identifier"),
                             "type": infons.get("type"), "name": infons.get("name"),
                             "accession": infons.get("accession"), "valid": infons.get("valid"),
                             "locations": annotation.get("locations", []),
                             "passage_type": passage.get("infons", {}).get("type")})
    raw_relations = doc.get("relations", []) + [r for p in doc.get("passages", []) for r in p.get("relations", [])]
    for rel in raw_relations:
        info = rel.get("infons", {})
        role1, role2 = info.get("role1", {}), info.get("role2", {})
        if not isinstance(role1, dict) or not isinstance(role2, dict):
            continue
        relations.append({"type": info.get("type"), "score": float(info["score"]) if info.get("score") else None,
                          "entity1": {k: role1.get(k) for k in ["identifier", "type", "name", "accession"]},
                          "entity2": {k: role2.get(k) for k in ["identifier", "type", "name", "accession"]}})
    return {"pmid": str(doc.get("pmid") or doc["id"]), "entities": entities, "relations": relations}


def download(batch_size=100):
    RAW.mkdir(parents=True, exist_ok=True)
    ids = (DERIVED / "pmids.txt").read_text().splitlines()
    found, manifest_batches, failures = set(), [], []
    target = DERIVED / "pubtator.jsonl.gz"
    tmp = target.with_suffix(".gz.tmp")
    entity_count = relation_count = 0
    with tmp.open("wb") as binary, gzip.GzipFile(filename="", mode="wb", fileobj=binary, mtime=0) as compressed, io.TextIOWrapper(compressed, encoding="utf-8") as out:
        for start in range(0, len(ids), batch_size):
            batch = ids[start:start+batch_size]
            name = f"{start:06d}-{sha256(','.join(batch).encode())[:10]}.json.gz"
            path = RAW / name
            try:
                if path.exists():
                    raw = gzip.decompress(path.read_bytes())
                else:
                    raw = request(ENDPOINT + "?" + urllib.parse.urlencode({"pmids": ",".join(batch), "tool": TOOL}), interval=1.0)
                docs = documents(json.loads(raw))
                unexpected = {str(d.get("pmid") or d["id"]) for d in docs} - set(batch)
                if unexpected:
                    raise ValueError(f"Unexpected PubTator IDs: {unexpected}")
                if not path.exists():
                    path.write_bytes(gzip.compress(raw, mtime=0))
                batch_found = set()
                for doc in docs:
                    row = normalize(doc)
                    if row["pmid"] in found:
                        continue
                    out.write(json.dumps(row, ensure_ascii=True) + "\n")
                    found.add(row["pmid"])
                    batch_found.add(row["pmid"])
                    entity_count += len(row["entities"])
                    relation_count += len(row["relations"])
                manifest_batches.append({"file": str(path.relative_to(ROOT)), "requested": len(batch),
                                         "returned": len(batch_found), "missing": sorted(set(batch)-batch_found, key=int),
                                         "sha256_uncompressed": sha256(raw)})
            except Exception as exc:  # noqa: BLE001 -- batch boundary records failures and coverage
                failures.append({"start": start, "pmids": batch, "error": f"{type(exc).__name__}: {exc}"})
                write_json(DERIVED / "pubtator_failures.json", failures)
                print(f"PubTator failed batch {start}: {exc}", flush=True)
            print(f"PubTator attempted {min(start+batch_size,len(ids))}/{len(ids)}; covered {len(found)}", flush=True)
    tmp.replace(target)
    manifest = {"retrieved_utc": utcnow(), "endpoint": ENDPOINT,
                "source_pmids_sha256": sha256((DERIVED / "pmids.txt").read_bytes()),
                "requested": len(ids), "available": len(found),
                "missing_pmids": sorted(set(ids)-found, key=int), "failed_batches": failures,
                "entity_mentions": entity_count, "relations": relation_count,
                "batches": manifest_batches, "sha256": sha256(target.read_bytes())}
    write_json(DERIVED / "pubtator_manifest.json", manifest)
    write_json(ROOT / "results/corpus_pubtator.json", {k:v for k,v in manifest.items() if k != "batches"})


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--batch-size", type=int, default=100)
    args = p.parse_args()
    if not 1 <= args.batch_size <= 100:
        p.error("Batch size must be between 1 and 100")
    download(args.batch_size)
