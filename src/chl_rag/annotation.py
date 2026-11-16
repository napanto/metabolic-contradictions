"""Prepare blind human evaluation samples and serve a loopback-only annotation UI.

Sampling metadata and predictions stay outside the served package. Antonio's
labels are append-only JSONL; this module does not adjudicate them.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import threading
from collections import Counter, defaultdict
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .io import canonical, digest, file_sha, read_jsonl, write_json, write_jsonl
from .sentences import sentences

ARMS = ("biomarker", "drug", "diet")
ARM_ALIASES = {"bio": "biomarker", "biomarkers": "biomarker", "drugs": "drug"}
FIELDS = ("exposure", "outcome", "sign", "kind", "comparator", "context")
FIELD_LABELS = {"correct", "incorrect", "uncertain"}
SUPPORT_LABELS = {"supported", "unsupported", "uncertain"}
PAIR_LABELS = (
    "contradictory", "apparent_contradiction", "not_replicated",
    "extraction_error", "not_comparable", "concordant",
)
FACTORS = (
    "population", "region", "disease_subtype", "design", "comparator",
    "molecule_class", "dose", "duration", "outcome_definition", "sample",
    "unknown", "none", "other",
)
FLAGGED = PAIR_LABELS[:3]
LABEL_ALIASES = {
    "contradiction": "contradictory", "apparent": "apparent_contradiction",
    "context_explained": "apparent_contradiction", "non_replicated": "not_replicated",
}


def arm_name(value):
    return ARM_ALIASES.get(str(value), str(value))


def dev_pmids(value):
    """Accept the documented PMID-list and per-arm development manifests."""
    if isinstance(value, (str, int)):
        return {str(value)} if str(value).isdigit() else set()
    if isinstance(value, list):
        return set().union(*(dev_pmids(v) for v in value)) if value else set()
    if isinstance(value, dict):
        if "pmid" in value:
            return {str(value["pmid"])}
        allowed = {"pmids", "ids", "items", "articles", "development_pmids", "by_arm",
                   "selection_by_arm", "development", "selected", *ARMS, *ARM_ALIASES}
        parts = [dev_pmids(v) for k, v in value.items() if k in allowed]
        return set().union(*parts) if parts else set()
    return set()


def allocate_strata(counts, target):
    """Largest-remainder proportional allocation, with one per nonempty class."""
    counts = {k: int(v) for k, v in counts.items() if v > 0}
    target = min(target, sum(counts.values()))
    if not counts:
        return {}
    if target < len(counts):
        raise ValueError("Requested sample cannot cover each nonempty class")
    quotas = {k: target * n / sum(counts.values()) for k, n in counts.items()}
    allocation = {k: min(n, max(1, math.floor(quotas[k]))) for k, n in counts.items()}
    while sum(allocation.values()) < target:
        eligible = [k for k in counts if allocation[k] < counts[k]]
        key = min(eligible, key=lambda k: (-(quotas[k] - allocation[k]), k))
        allocation[key] += 1
    while sum(allocation.values()) > target:
        eligible = [k for k in counts if allocation[k] > 1]
        key = min(eligible, key=lambda k: (-(allocation[k] - quotas[k]), k))
        allocation[key] -= 1
    return allocation


def unique_index(rows, field):
    result = {}
    for row in rows:
        key = str(row[field])
        if key in result:
            raise ValueError(f"Duplicate {field}: {key}")
        result[key] = row
    return result


def public_article(article):
    source_sentences = article.get("sentences") or sentences(article)
    return {
        "pmid": str(article["pmid"]), "title": article.get("title", ""),
        "abstract": article.get("abstract", ""), "year": article.get("year"),
        "publication_types": list(article.get("publication_types") or []),
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{article['pmid']}/",
        "sentences": [{"id": int(s["id"]), "text": s["text"],
                       "section": s.get("section", "")} for s in source_sentences],
    }


def public_claim(claim, article, *, extraction):
    """Allowlist fields; never pass pair labels, predictions, scores or rationales."""
    sentence_map = {s["id"]: s for s in public_article(article)["sentences"]}
    ids = [int(i) for i in claim.get("evidence", [])]
    if not ids or any(i not in sentence_map for i in ids):
        raise ValueError(f"Invalid source evidence for claim {claim['claim_id']}")
    evidence = [{"id": i, "text": sentence_map[i]["text"]} for i in ids]
    supplied = claim.get("evidence_text")
    if supplied is not None and list(supplied) != [s["text"] for s in evidence]:
        raise ValueError(f"Evidence text does not match source for {claim['claim_id']}")
    visible = {k: claim.get(k) for k in
               ("exposure", "exposure_class", "outcome", "kind", "comparator")}
    context = claim.get("context") or {}
    visible["context"] = {k: context.get(k) for k in
                          ("population", "region", "design", "n", "duration", "dose", "sample")}
    # These are claim inputs under assessment, not the system's pair verdict.
    # Pair annotators need them to distinguish extraction errors from agreement.
    visible.update({k: claim.get(k) for k in ("sign", "significant", "hedged")})
    return {"claim_id": str(claim["claim_id"]), "fields": visible, "evidence": evidence}


def _claim_ref(value):
    return str(value["claim_id"]) if isinstance(value, dict) else str(value)


def prepare(corpus_path, claims_path, pairs_path, dev_path, output_dir, manifest_path,
            extraction_per_arm=15, pair_targets=None):
    """Freeze deterministic samples. Existing different samples are never replaced."""
    pair_targets = pair_targets or {"biomarker": 34, "drug": 33, "diet": 33}
    articles = unique_index(read_jsonl(corpus_path), "pmid")
    claims = unique_index(read_jsonl(claims_path), "claim_id")
    development = dev_pmids(json.loads(Path(dev_path).read_text()))
    if not development:
        raise ValueError("Development manifest contains no recognizable PMIDs")
    by_pmid = defaultdict(list)
    for claim in claims.values():
        pmid = str(claim["pmid"])
        if pmid not in articles:
            raise ValueError(f"Claim PMID {pmid} missing from corpus")
        by_pmid[pmid].append(claim)
    for group in by_pmid.values():
        group.sort(key=lambda c: str(c["claim_id"]))

    manifest = {
        "schema_version": 1, "seeds": {"extraction": 43, "pairs": 44},
        "sources": {"corpus_sha256": file_sha(corpus_path),
                    "claims_sha256": file_sha(claims_path),
                    "pairs_sha256": file_sha(pairs_path), "dev_sha256": file_sha(dev_path)},
        "excluded_dev_pmids": sorted(development), "extraction_frames": {},
        "pair_frames": {}, "items": [], "shortages": [],
        "extraction_probability_note": "Conditional on prior-arm selections; overlapping PMIDs are drawn once.",
        "pair_probability_note": "Within-arm/class simple random sampling; pi = selected / frame.",
        "extraction_frame_definition": "Non-development abstracts with at least one retained claim; zero-claim processing coverage is reported separately.",
    }
    extraction_rng = random.Random(43)
    used = set(development)
    chosen_abstracts = []
    for arm in ARMS:
        frame = []
        for pmid, group in sorted(by_pmid.items()):
            raw_arms = articles[pmid].get("arms") or [c.get("arm") for c in group]
            if pmid not in used and arm in {arm_name(a) for a in raw_arms}:
                frame.append(pmid)
        n = min(extraction_per_arm, len(frame))
        selection = extraction_rng.sample(frame, n)
        used.update(selection)
        manifest["extraction_frames"][arm] = {
            "frame_count": len(frame), "requested": extraction_per_arm, "selected": n,
            "conditional_inclusion_probability": n / len(frame) if frame else None,
            "frame_pmids": frame, "selected_pmids": sorted(selection),
        }
        if n < extraction_per_arm:
            manifest["shortages"].append({"task": "extraction", "arm": arm,
                                           "requested": extraction_per_arm, "available": n})
        chosen_abstracts.extend((arm, pmid) for pmid in selection)
    extraction_rng.shuffle(chosen_abstracts)
    items = []
    for index, (arm, pmid) in enumerate(chosen_abstracts, 1):
        item_id = f"extraction-{index:03d}"
        items.append({"item_id": item_id, "task": "extraction", "arm": arm,
                      "article": public_article(articles[pmid]),
                      "claims": [public_claim(c, articles[pmid], extraction=True)
                                 for c in by_pmid[pmid]]})
        manifest["items"].append({"item_id": item_id, "task": "extraction", "pmid": pmid,
                                   "arm": arm})

    frames = defaultdict(list)
    pairs_seen = set()
    pair_ids = set()
    excluded = Counter()
    for pair in sorted(read_jsonl(pairs_path), key=lambda p: str(p["pair_id"])):
        pair_id = str(pair["pair_id"])
        if pair_id in pair_ids:
            raise ValueError(f"Duplicate pair_id: {pair_id}")
        pair_ids.add(pair_id)
        label = LABEL_ALIASES.get(pair.get("label"), pair.get("label"))
        if label not in FLAGGED:
            excluded["not_flagged"] += 1
            continue
        a, b = _claim_ref(pair["claim_a"]), _claim_ref(pair["claim_b"])
        if a not in claims or b not in claims:
            raise ValueError(f"Unknown claim in pair {pair_id}")
        pa, pb = str(claims[a]["pmid"]), str(claims[b]["pmid"])
        if pa in development or pb in development:
            excluded["development"] += 1
            continue
        if pa == pb:
            excluded["same_abstract"] += 1
            continue
        key = tuple(sorted((a, b)))
        if key in pairs_seen:
            excluded["duplicate_claim_pair"] += 1
            continue
        pairs_seen.add(key)
        arm = arm_name(pair.get("arm", claims[a].get("arm")))
        if arm not in ARMS:
            raise ValueError(f"Unsupported arm in pair {pair_id}: {arm}")
        frames[(arm, label)].append({"pair_id": pair_id, "claim_a": a, "claim_b": b,
                                      "label": label, "arm": arm})
    manifest["pair_exclusions"] = dict(excluded)
    pair_rng = random.Random(44)
    chosen_pairs = []
    for arm in ARMS:
        counts = {label: len(frames[(arm, label)]) for label in FLAGGED}
        allocation = allocate_strata(counts, pair_targets[arm])
        selected_total = sum(allocation.values())
        if selected_total < pair_targets[arm]:
            manifest["shortages"].append({"task": "pairs", "arm": arm,
                                           "requested": pair_targets[arm],
                                           "available": selected_total})
        for label in FLAGGED:
            frame, n = frames[(arm, label)], allocation.get(label, 0)
            selection = pair_rng.sample(frame, n)
            pi = n / len(frame) if frame else None
            manifest["pair_frames"][f"{arm}/{label}"] = {
                "arm": arm, "predicted_class": label, "frame_count": len(frame),
                "selected": n, "inclusion_probability": pi,
                "frame_pair_ids": [p["pair_id"] for p in frame],
                "selected_pair_ids": sorted(p["pair_id"] for p in selection),
            }
            chosen_pairs.extend((p, pi) for p in selection)
    pair_rng.shuffle(chosen_pairs)
    for index, (pair, pi) in enumerate(chosen_pairs, 1):
        item_id = f"pair-{index:03d}"
        refs = [pair["claim_a"], pair["claim_b"]]
        if pair_rng.random() < 0.5:
            refs.reverse()
        sides = []
        for ref in refs:
            claim = claims[ref]
            article = articles[str(claim["pmid"])]
            sides.append({"article": public_article(article),
                          "claim": public_claim(claim, article, extraction=False)})
        items.append({"item_id": item_id, "task": "pair", "arm": pair["arm"],
                      "a": sides[0], "b": sides[1]})
        manifest["items"].append({"item_id": item_id, "task": "pair", **pair,
                                   "inclusion_probability": pi,
                                   "display_claim_order": refs})
    manifest["counts"] = {"extraction_abstracts": len(chosen_abstracts),
                          "extraction_claims": sum(len(i["claims"]) for i in items
                                                   if i["task"] == "extraction"),
                          "pairs": len(chosen_pairs)}
    manifest["blind_items_sha256"] = digest(items)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    item_path = output_dir / "items.jsonl"
    if item_path.exists() and list(read_jsonl(item_path)) != items:
        raise ValueError("A different frozen annotation sample already exists; use a new output directory")
    write_jsonl(item_path, items)
    write_jsonl(output_dir / "second_annotator_items.jsonl", items)
    write_json(manifest_path, manifest)
    return manifest


def validate_label(payload, item):
    if payload.get("item_id") != item["item_id"] or payload.get("task") != item["task"]:
        raise ValueError("Annotation item/task mismatch")
    result = {"item_id": item["item_id"], "task": item["task"],
              "note": str(payload.get("note", ""))[:10000],
              "revision_note": str(payload.get("revision_note", ""))[:2000]}
    if item["task"] == "extraction":
        expected = {c["claim_id"] for c in item["claims"]}
        given = payload.get("claims", [])
        if len(given) != len(expected) or {c.get("claim_id") for c in given} != expected:
            raise ValueError("Every extracted claim must be assessed exactly once")
        result["claims"] = []
        for claim in sorted(given, key=lambda c: c["claim_id"]):
            fields = claim.get("fields", {})
            if set(fields) != set(FIELDS) or any(v not in FIELD_LABELS for v in fields.values()):
                raise ValueError("Complete all six claim fields")
            if claim.get("evidence_support") not in SUPPORT_LABELS:
                raise ValueError("Assess evidence support for every claim")
            result["claims"].append({"claim_id": claim["claim_id"], "fields": fields,
                                     "evidence_support": claim["evidence_support"],
                                     "note": str(claim.get("note", ""))[:5000]})
    else:
        if payload.get("label") not in PAIR_LABELS or payload.get("factor") not in FACTORS:
            raise ValueError("Choose a pair category and context factor")
        if not str(payload.get("rationale", "")).strip():
            raise ValueError("A brief evidence-based rationale is required")
        result.update({"label": payload["label"], "factor": payload["factor"],
                       "rationale": str(payload["rationale"])[:10000]})
    return result


def _judgment(record):
    return {k: v for k, v in record.items()
            if k not in {"annotator", "revision", "saved_at", "revision_note", "package_sha256"}}


class AnnotationStore:
    """Append-only storage. Only server operations should inspect human labels."""

    def __init__(self, items, path):
        self.items = unique_index(items, "item_id")
        self.path = Path(path)
        self.lock = threading.Lock()
        self.package_sha256 = digest(items)

    def history(self):
        if not self.path.exists():
            return []
        rows = list(read_jsonl(self.path))
        for row in rows:
            if row.get("annotator") != "Antonio" or row.get("annotator_type") not in (None, "human"):
                raise ValueError("Only Antonio's manual annotations belong in the human reference")
            if row.get("package_sha256") != self.package_sha256:
                raise ValueError("Labels belong to a different annotation package")
        return rows

    def latest(self):
        return {row["item_id"]: row for row in self.history()}

    def save(self, payloads):
        with self.lock:
            latest = self.latest()
            pending = []
            for payload in payloads:
                if not isinstance(payload, dict):
                    raise TypeError("Each annotation must be a JSON object")
                if (payload.get("annotator") not in (None, "Antonio")
                        or payload.get("annotator_type") not in (None, "human")):
                    raise ValueError("Only Antonio's manual annotations belong in the human reference")
                if payload.get("package_sha256", self.package_sha256) != self.package_sha256:
                    raise ValueError("Imported labels belong to a different annotation package")
                item_id = payload.get("item_id")
                if item_id not in self.items:
                    raise ValueError("Unknown item ID")
                record = validate_label(payload, self.items[item_id])
                old = latest.get(item_id)
                if old and _judgment(old) == _judgment(record):
                    continue
                if old and not record["revision_note"].strip():
                    raise ValueError("Explain revisions in the revision-note field")
                record.update({"annotator": "Antonio", "revision": old["revision"] + 1 if old else 1,
                               "saved_at": datetime.now(UTC).isoformat(),
                               "package_sha256": self.package_sha256})
                pending.append(record)
                latest[item_id] = record
            if pending:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    for record in pending:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    import os
                    os.fsync(handle.fileno())
            return pending


def make_server(items_path, labels_path, ui_path, *, host="127.0.0.1", port=8765, tasks=None):
    if host != "127.0.0.1":
        raise ValueError("Annotation server must bind to 127.0.0.1")
    items = list(read_jsonl(items_path))
    if not items:
        raise ValueError("No real annotation items have been prepared")
    store = AnnotationStore(items, labels_path)
    if tasks is not None and (not tasks or not set(tasks) <= {"extraction", "pair"}):
        raise ValueError("Unsupported visible annotation task")
    visible = [item for item in items if tasks is None or item["task"] in tasks]
    visible_ids = {item["item_id"] for item in visible}
    if not visible:
        raise ValueError("No items in the requested annotation view")
    ui_bytes = Path(ui_path).read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Evidence and annotations never enter request logs.

        def respond(self, status, body, mime="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else canonical(body)
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def allowed(self):
            allowed_hosts = {f"127.0.0.1:{self.server.server_port}",
                             f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{h}" for h in allowed_hosts}:
                return False
            return self.headers.get("Sec-Fetch-Site") != "cross-site"

        def do_GET(self):
            if not self.allowed():
                return self.respond(403, {"error": "Local requests only"})
            route = urlsplit(self.path).path
            try:
                if route == "/":
                    return self.respond(200, ui_bytes, "text/html; charset=utf-8")
                if route == "/api/items":
                    return self.respond(200, {"items": visible, "package_sha256": store.package_sha256,
                                              "visible_items_sha256": digest(visible), "package_item_count": len(items)})
                if route == "/api/labels":
                    return self.respond(200, {key: row for key, row in store.latest().items() if key in visible_ids})
                if route == "/api/export":
                    data = b"".join(canonical(r) + b"\n" for r in store.history() if r["item_id"] in visible_ids)
                    return self.respond(200, data, "application/x-ndjson; charset=utf-8")
                return self.respond(404, {"error": "Not found"})
            except (ValueError, OSError) as exc:
                return self.respond(400, {"error": str(exc)})

        def do_POST(self):
            if not self.allowed():
                return self.respond(403, {"error": "Local requests only"})
            if urlsplit(self.path).path != "/api/labels":
                return self.respond(404, {"error": "Not found"})
            try:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("JSON required")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2_000_000:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                records = payload if isinstance(payload, list) else [payload]
                if any(not isinstance(record, dict) or record.get("item_id") not in visible_ids for record in records):
                    raise ValueError("This annotation view accepts only its displayed items")
                saved = store.save(records)
                return self.respond(200, {"saved": saved, "complete": len(set(store.latest()) & visible_ids)})
            except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
                return self.respond(400, {"error": str(exc)})

    server = ThreadingHTTPServer((host, port), Handler)
    server.annotation_store = store
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    for flag, default in [("corpus", "data/derived/corpus.jsonl"),
                          ("claims", "data/derived/claims_normalized.jsonl"),
                          ("pairs", "data/derived/pairs.jsonl"),
                          ("dev", "data/derived/metabolic_dev.json")]:
        prep.add_argument("--" + flag, default=default)
    prep.add_argument("--output", default="annotation/package")
    prep.add_argument("--manifest", default="data/derived/annotation_sampling_private.json")
    serve = sub.add_parser("serve")
    serve.add_argument("--items", default="annotation/package/items.jsonl")
    serve.add_argument("--labels", default="annotation/antonio_labels.jsonl")
    serve.add_argument("--ui", default="annotation/index.html")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--tasks", nargs="+", choices=["extraction", "pair"])
    args = parser.parse_args(argv)
    if args.command == "prepare":
        if Path(args.pairs).resolve() == Path("data/derived/pairs.jsonl").resolve():
            summary = json.loads(Path("results/classification.json").read_text())
            if (summary.get("status") != "complete" or summary.get("unprocessed_pairs", 0)
                    or summary.get("output_sha256") != file_sha(args.pairs)
                    or summary.get("input_sha256", {}).get("claims") != file_sha(args.claims)):
                raise ValueError("Final annotation sampling requires complete, hash-matching classification")
            candidates = Path("data/derived/pairs_candidates.jsonl")
            if (summary.get("input_sha256", {}).get("pairs") != file_sha(candidates)
                    or summary.get("requested_pairs") != sum(1 for _ in read_jsonl(candidates))):
                raise ValueError("Final annotation sampling requires the entire frozen candidate frame")
        result = prepare(args.corpus, args.claims, args.pairs, args.dev, args.output, args.manifest)
        print(json.dumps({"counts": result["counts"], "shortages": result["shortages"]}))
    else:
        server = make_server(args.items, args.labels, args.ui, port=args.port, tasks=args.tasks)
        print(f"Annotazione: http://127.0.0.1:{server.server_port} — Ctrl+C per terminare", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
