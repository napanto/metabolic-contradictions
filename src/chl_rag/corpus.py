"""Freeze exact PubMed queries and fetch compressed, restartable XML batches."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data/raw/pubmed"
DERIVED = ROOT / "data/derived"
TOOL = "chl-inconsistency-rag"
API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"


def utcnow():
    return dt.datetime.now(dt.UTC).isoformat()


def sha256(data: bytes):
    return hashlib.sha256(data).hexdigest()


def file_sha256(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


class FrozenCorpusDriftError(RuntimeError):
    """Current source/derived bytes differ from the frozen acquisition."""


def refuse_drift(*, kind, original_path, expected_sha256, data=None, candidate_path=None, manifest_sha256=None):
    """Quarantine proposed bytes; never alter a frozen source or derived artifact."""
    observed = sha256(data) if data is not None else file_sha256(candidate_path)
    folder = ROOT / "data/raw/source_drift" / f"{kind}-{observed[:16]}"
    folder.mkdir(parents=True, exist_ok=True)
    quarantined = folder / ("source.xml.gz" if data is not None else "corpus.jsonl.gz")
    if not quarantined.exists():
        if data is not None:
            quarantined.write_bytes(gzip.compress(data, mtime=0))
        else:
            with (Path(candidate_path).open("rb") as source,
                  quarantined.open("wb") as raw_target,
                  gzip.GzipFile(filename="", mode="wb", fileobj=raw_target, mtime=0) as target):
                shutil.copyfileobj(source, target)
    audit = {"checked_utc": utcnow(), "status": "refused_source_drift", "kind": kind,
             "original_path": str(Path(original_path).relative_to(ROOT)),
             "expected_sha256": expected_sha256, "observed_sha256": observed,
             "quarantined_file": str(quarantined.relative_to(ROOT)),
             "frozen_manifest_sha256": manifest_sha256,
             "frozen_files_modified": False,
             "action": "Obtain the original frozen source archive, or create a separately versioned corpus. No automatic re-freeze."}
    if not (folder / "metadata.json").exists():
        write_json(folder / "metadata.json", audit)
    write_json(ROOT / "results/corpus_rebuild.json", audit)
    raise FrozenCorpusDriftError(f"Frozen corpus {kind} checksum mismatch; changed bytes quarantined at {quarantined}")


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    temp.replace(path)


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w") as out:
        for row in rows:
            # Escape Unicode line separators too: str.splitlines otherwise splits
            # valid JSON strings containing U+0085/U+2028/U+2029 in PubMed text.
            out.write(json.dumps(row, ensure_ascii=True) + "\n")
    temp.replace(path)


def pace(interval=0.4):
    """Cross-process lock keeps this project's NCBI clients <=2.5 starts/s."""
    RAW.mkdir(parents=True, exist_ok=True)
    with (RAW / ".rate-limit").open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        previous = float(f.read() or "0")
        time.sleep(max(0, previous + interval - time.time()))
        f.seek(0)
        f.truncate()
        f.write(str(time.time()))
        f.flush()


def request(url, *, retries=6, interval=0.4, data=None):
    for attempt in range(retries):
        pace(interval)
        try:
            req = urllib.request.Request(url, data=data, headers={
                "User-Agent": TOOL + "/1.0 (academic literature research)",
                **({"Content-Type": "application/json"} if data else {})})
            with urllib.request.urlopen(req, timeout=120) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500 and exc.code not in (408, 429):
                raise
            if attempt == retries - 1:
                raise
            delay = min(60, 2 ** (attempt + 1))
            if isinstance(exc, urllib.error.HTTPError):
                delay = max(delay, int(exc.headers.get("Retry-After", "0") or 0))
            status = f" {exc.code}" if isinstance(exc, urllib.error.HTTPError) else ""
            print(f"Retry {attempt + 1}: {type(exc).__name__}{status}; wait {delay}s", flush=True)
            time.sleep(delay)
    raise RuntimeError("unreachable")


def search(query, name):
    path = RAW / "search" / (name + ".json.gz")
    if path.exists():
        data = gzip.decompress(path.read_bytes())
    else:
        params = {"db": "pubmed", "term": query, "retmode": "json", "retmax": 9999,
                  "usehistory": "y", "tool": TOOL, "sort": "pub_date"}
        data = request(API + "esearch.fcgi?" + urllib.parse.urlencode(params))
        obj = json.loads(data)
        if "esearchresult" not in obj or "ERROR" in obj.get("esearchresult", {}):
            raise ValueError(f"Invalid ESearch result: {obj}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(data, mtime=0))
    result = json.loads(data)["esearchresult"]
    if "errorlist" in result:
        raise ValueError(f"Query warnings for {name}: {result['errorlist']}")
    return result


def retrieve_ids(query, name):
    result = search(query, name)
    count = int(result["count"])
    ids = set(result["idlist"])
    if count > 9999:
        ids = set()
        for year in range(2016, 2026):
            part = search(f"({query}) AND {year}[dp]", f"{name}-{year}")
            if int(part["count"]) > 9999 or len(part["idlist"]) != int(part["count"]):
                raise ValueError(f"Partition {name}/{year} is truncated")
            ids.update(part["idlist"])
    if len(ids) != count:
        raise ValueError(f"Frozen {name}: {len(ids)} distinct IDs != ESearch {count}")
    return ids


def freeze():
    path = DERIVED / "corpus_manifest.json"
    if path.exists():
        manifest = json.loads(path.read_text())
        if manifest["queries"] != json.loads((ROOT / "configs/queries.json").read_text()):
            raise ValueError("Query config changed after freeze; preserve this corpus and create a new explicit version")
        for filename, key in [("pmids.txt", "pmids_sha256"), ("pmid_membership.jsonl", "membership_sha256")]:
            if sha256((DERIVED / filename).read_bytes()) != manifest[key]:
                raise ValueError(f"Frozen {filename} checksum mismatch")
        print(f"Using existing freeze: {manifest['retrieved_utc']}", flush=True)
        return manifest
    queries = json.loads((ROOT / "configs/queries.json").read_text())
    armsets, designsets = {}, {}
    exact_queries = {}
    for arm, key in [("biomarker", "BIO"), ("drug", "DRUG"), ("diet", "DIET")]:
        q = f"{queries['BASE']} AND {queries[key]}"
        exact_queries[arm] = q
        armsets[arm] = retrieve_ids(q, arm)
        designsets[arm] = retrieve_ids(q + " AND " + queries["DESIGN"], arm + "-design")
        if not designsets[arm] <= armsets[arm]:
            raise ValueError(f"Design set not contained in {arm}")
        print(f"{arm}: {len(armsets[arm])}; design {len(designsets[arm])}", flush=True)
    union = set.union(*armsets.values())
    design = set.union(*designsets.values())
    union_q = queries["BASE"] + " AND (" + " OR ".join(queries[k] for k in ["BIO", "DRUG", "DIET"]) + ")"
    for label, query, ids in [("union-count", union_q, union),
                              ("union-design-count", union_q + " AND " + queries["DESIGN"], design)]:
        observed = int(search(query, label)["count"])
        if observed != len(ids):
            raise ValueError(f"{label}: ESearch {observed} != frozen union {len(ids)}")
    retracted = retrieve_ids(union_q + ' AND "Retracted Publication"[pt]', "retracted")
    rows = [{"pmid": p, "arms": [a for a, ids in armsets.items() if p in ids],
             "design_eligible": p in design, "retracted": p in retracted}
            for p in sorted(union, key=int)]
    write_jsonl(DERIVED / "pmid_membership.jsonl", rows)
    frozen_pmids = ("\n".join(row["pmid"] for row in rows) + "\n").encode()
    (DERIVED / "pmids.txt").write_bytes(frozen_pmids)
    manifest = {"retrieved_utc": utcnow(), "tool": TOOL,
                "queries": queries, "exact_arm_queries": exact_queries,
                "union_query": union_q, "counts": {
                    "union": len(union), "design_union": len(design),
                    "retracted": len(retracted), "retracted_design": len(retracted & design),
                    "arms": {a: {"all": len(armsets[a]), "design": len(designsets[a])} for a in armsets}},
                "pmids_sha256": sha256(frozen_pmids),
                "membership_sha256": sha256((DERIVED / "pmid_membership.jsonl").read_bytes()),
                "search_strategy": "ESearch usehistory=y; >9999 results partitioned by publication year, deduplicated; exact union counts verified",
                "expected_brief_counts": {"union": 29559, "design_union": 14627, "retracted": 26}}
    write_json(path, manifest)
    return manifest


def node_text(node):
    return "".join(node.itertext()).strip() if node is not None else ""


def parse_article(article, membership):
    citation = article.find("MedlineCitation")
    if citation is None:
        raise ValueError("Non-PubmedArticle in frozen corpus")
    a = citation.find("Article")
    sections = [{"label": s.get("Label"), "nlm_category": s.get("NlmCategory"),
                 "text": node_text(s)} for s in a.findall("Abstract/AbstractText")]
    other_abstracts = [{"type": other.get("Type"), "language": other.get("Language"),
                       "text": "\n".join(node_text(s) for s in other.findall("AbstractText"))}
                      for other in citation.findall("OtherAbstract")]
    title = node_text(a.find("ArticleTitle"))
    abstract = "\n".join(s["text"] for s in sections)
    types = [node_text(x) for x in a.findall("PublicationTypeList/PublicationType")]
    mesh = [{"id": d.get("UI"), "name": node_text(d),
             "major_topic": d.get("MajorTopicYN") == "Y"}
            for d in citation.findall("MeshHeadingList/MeshHeading/DescriptorName")]
    date = a.find("Journal/JournalIssue/PubDate")
    year = date.findtext("Year") if date is not None else None
    if not year and date is not None:
        match = re.search(r"\b(?:19|20)\d{2}\b", node_text(date))
        year = match.group() if match else None
    dois = [node_text(x) for x in article.findall("PubmedData/ArticleIdList/ArticleId") if x.get("IdType") == "doi"]
    summary = (title + " " + abstract).lower()
    control = bool(re.search(r"\birisin\b|\beggs?\b", summary)) or (
        "amput" in summary and bool(re.search(r"sglt2|sodium.glucose|canagliflozin|dapagliflozin|empagliflozin|ertugliflozin", summary)))
    priority = (1 if control else 2 if set(types) & {"Randomized Controlled Trial", "Meta-Analysis"} else 3)
    if not membership["design_eligible"]:
        priority = 4
    return {**membership, "title": title, "abstract": abstract, "sections": sections,
            "has_primary_abstract": bool(abstract), "other_abstracts": other_abstracts,
            "year": int(year) if year else None, "journal": node_text(a.find("Journal/Title")),
            "publication_types": types, "mesh": mesh, "doi": dois[0] if dois else None,
            "retracted": membership["retracted"] or "Retracted Publication" in types,
            "priority": priority,
            "retraction_links": [{"type": x.get("RefType"), "pmid": x.findtext("PMID")}
                                 for x in citation.findall("CommentsCorrectionsList/CommentsCorrections")
                                 if "Retract" in x.get("RefType", "")]}


def fetch():
    manifest = freeze()
    manifest_path = DERIVED / "corpus_manifest.json"
    original_manifest_sha = file_sha256(manifest_path)
    expected_corpus_sha = manifest.get("corpus_sha256")
    frozen_batches = {b["file"]: b for b in manifest.get("batches", [])}
    corpus_path = DERIVED / "corpus.jsonl"
    if expected_corpus_sha and not frozen_batches:
        raise ValueError("Frozen corpus manifest has a derived checksum but no source-batch checksums")
    if expected_corpus_sha and corpus_path.exists() and file_sha256(corpus_path) != expected_corpus_sha:
        refuse_drift(kind="existing-derived", original_path=corpus_path,
                     expected_sha256=expected_corpus_sha, candidate_path=corpus_path,
                     manifest_sha256=original_manifest_sha)
    members = {x["pmid"]: x for x in map(json.loads, (DERIVED / "pmid_membership.jsonl").read_text().splitlines())}
    ids = sorted(members, key=int)
    records, batches = {}, []
    live_batches = 0
    for start in range(0, len(ids), 200):
        batch = ids[start:start+200]
        path = RAW / "efetch" / f"{start:06d}.xml.gz"
        source_name = str(path.relative_to(ROOT))
        if expected_corpus_sha and source_name not in frozen_batches:
            raise ValueError(f"Frozen manifest lacks source checksum for {source_name}")
        if path.exists():
            data = gzip.decompress(path.read_bytes())
        else:
            params = {"db": "pubmed", "id": ",".join(batch), "retmode": "xml", "tool": TOOL}
            data = request(API + "efetch.fcgi?" + urllib.parse.urlencode(params))
            live_batches += 1
        expected_batch_sha = frozen_batches.get(source_name, {}).get("sha256_uncompressed")
        if expected_corpus_sha and not expected_batch_sha:
            raise ValueError(f"Frozen source checksum missing for {source_name}")
        if expected_batch_sha and sha256(data) != expected_batch_sha:
            refuse_drift(kind="source-xml", original_path=path,
                         expected_sha256=expected_batch_sha, data=data,
                         manifest_sha256=original_manifest_sha)
        xml = ET.fromstring(data)
        found = {a.findtext("MedlineCitation/PMID") for a in xml.findall("PubmedArticle")}
        if found != set(batch):
            raise ValueError(f"Batch {start}: missing {set(batch)-found}, unexpected {found-set(batch)}")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(data, mtime=0))
        for a in xml.findall("PubmedArticle"):
            p = a.findtext("MedlineCitation/PMID")
            records[p] = parse_article(a, members[p])
        batches.append({"file": str(path.relative_to(ROOT)), "pmids": len(batch), "sha256_uncompressed": sha256(data)})
        print(f"PubMed fetched {len(records)}/{len(ids)}", flush=True)
    rows = [records[p] for p in ids]
    missing_abstracts = [{"pmid": r["pmid"], "design_eligible": r["design_eligible"],
                         "other_abstract_types": [a["type"] for a in r["other_abstracts"]]}
                        for r in rows if not r["has_primary_abstract"]]
    if any(not r["abstract"] and not r["other_abstracts"] for r in rows):
        raise ValueError("hasabstract record has neither primary Abstract nor OtherAbstract; investigate")
    candidate_path = DERIVED / f"corpus.rebuild-{os.getpid()}.jsonl"
    write_jsonl(candidate_path, rows)
    candidate_sha = file_sha256(candidate_path)
    if expected_corpus_sha and candidate_sha != expected_corpus_sha:
        try:
            refuse_drift(kind="derived", original_path=corpus_path,
                         expected_sha256=expected_corpus_sha, candidate_path=candidate_path,
                         manifest_sha256=original_manifest_sha)
        finally:
            candidate_path.unlink(missing_ok=True)
    if expected_corpus_sha:
        if corpus_path.exists():
            candidate_path.unlink()
        else:
            candidate_path.replace(corpus_path)
        write_json(ROOT / "results/corpus_rebuild.json", {
            "checked_utc": utcnow(), "status": "verified_frozen_rebuild",
            "corpus_sha256": candidate_sha, "records": len(rows),
            "source_batches_verified": len(batches), "live_downloaded_batches": live_batches,
            "frozen_manifest_sha256": original_manifest_sha, "frozen_manifest_modified": False})
        print("Frozen source and derived checksums verified; original manifest preserved", flush=True)
        return
    candidate_path.replace(corpus_path)
    manifest.update({"fetched_utc": utcnow(), "batches": batches,
                     "corpus_sha256": candidate_sha,
                     "record_count": len(rows), "priority_counts": dict(collections.Counter(r["priority"] for r in rows)),
                     "missing_primary_abstract": missing_abstracts,
                     "abstract_handling": "PubMed hasabstract also matches OtherAbstract plain-language summaries. Preserve their metadata separately; never silently use these as a primary abstract. Empty-primary records remain in frozen membership; exclude from claim extraction.",
                     "structured_abstracts": sum(any(s["label"] or s["nlm_category"] for s in r["sections"]) for r in rows)})
    write_json(DERIVED / "corpus_manifest.json", manifest)
    write_json(ROOT / "results/corpus_summary.json", {k:v for k,v in manifest.items() if k not in ["batches", "queries", "exact_arm_queries", "union_query"]})
    print(json.dumps(manifest["counts"], indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["freeze", "fetch", "all"], default="all", nargs="?")
    args = parser.parse_args()
    (freeze if args.stage == "freeze" else fetch)()
