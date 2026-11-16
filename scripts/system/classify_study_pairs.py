"""Classify the 35 live v2.1 human pairs with GLM-5.3 Flash, low and high reasoning.

Why (16 November, Antonio): build_v21_handoff.py rebuilt the human pairs from the corrected
v2.1 extraction but never reclassified them, so the system's label for the v2.1 pairs does not
exist yet. "low" is the registered production classifier (glm-5.3-flash__low, cap 65536);
"high" is Antonio's variant, identical except reasoning high and the P-calibrated high cap
(131072). Requests are built exactly as production classification: the frozen v2
classification prompt, both full numbered abstracts, the v2.1 claims, claim-id order of the
two sides, temperature 0 / seed 42, pinned fp8 routes with no fallback, the 480 s total POST
deadline and one identical retry on the same provider. The production classification hold of
tenstorrent/fp8 (persistent HTTP 429) is mirrored in a client-owned route copy. Spend goes to
the SQLite production ledger, phase production_classification.

No student or oracle answer is read. Run from the main project root:
    .venv/bin/python <this script> data/derived/study_pair_classification [--plan]
"""

import asyncio
import collections
import json
import sys
import time
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, "src")
from chl_rag.io import digest, read_jsonl  # noqa: E402
from chl_rag.model_client import V2Client  # noqa: E402
from chl_rag.model_runs import _parse_response  # noqa: E402
from chl_rag.extraction import pair_item  # noqa: E402
from chl_rag.candidate_pairs import classification_request, validate_classification  # noqa: E402
from chl_rag.sampling import FLAGGED  # noqa: E402
from chl_rag.sqlite_ledger import SQLitePhaseLedger  # noqa: E402

OUT = Path(sys.argv[1])
PLAN_ONLY = "--plan" in sys.argv
MODEL = "z-ai/glm-5.3-flash"
SETTINGS = {"low": ("low", 65536), "high": ("high", 131072)}
HANDOFF = Path("annotation/study_pairs")
EXTRACTED = Path("data/derived/extraction_correction/human_order_prompt3")
# Retired on the site with the audited retire-item command (deploy log, 15 November):
# identical to v2-pair-1083 after the v2.1 rebuild, never assigned or read.
RETIRED = {"v2-pair-1100"}
HELD_ROUTE = "tenstorrent/fp8"


class TotalDeadlineHTTP:
    def __init__(self, client, seconds=480):
        self.client, self.seconds = client, seconds

    async def post(self, *args, **kwargs):
        try:
            return await asyncio.wait_for(self.client.post(*args, **kwargs), timeout=self.seconds)
        except TimeoutError:
            raise httpx.ReadTimeout(f"Total model POST wall deadline exceeded ({self.seconds:g}s)") from None

    def __getattr__(self, name):
        return getattr(self.client, name)


def load():
    items = [json.loads(line) for line in (HANDOFF / "items.jsonl").read_text().splitlines()]
    live = [i for i in items if i["item_id"] not in RETIRED]
    assert len(items) == 36 and len(live) == 35, (len(items), len(live))
    pmids = {i[s]["article"]["pmid"] for i in live for s in "ab"}
    articles = {a["pmid"]: a for a in read_jsonl("data/derived/corpus.jsonl") if a["pmid"] in pmids}
    extractions = {}
    for pmid in pmids:
        record = json.loads((EXTRACTED / f"{pmid}.json").read_text())
        assert record["status"] == "valid", pmid
        extractions[pmid] = record["extraction"]["extraction"]
    pairs = []
    for item in live:
        pa, pb = item["a"]["article"]["pmid"], item["b"]["article"]["pmid"]
        ca, cb = item["a"]["claim"]["claim_id"], item["b"]["claim"]["claim_id"]
        # The handoff item must be exactly the item rebuilt from these extractions.
        rebuilt = pair_item(item["item_id"], item["arm"], articles[pa], extractions[pa], ca,
                            articles[pb], extractions[pb], cb)
        assert rebuilt == item, item["item_id"]
        if ca > cb:  # production order (make_pair): lower claim id is side a
            pa, pb, ca, cb = pb, pa, cb, ca
        pairs.append({"pair_id": item["item_id"], "arm": item["arm"], "pmid_a": pa, "pmid_b": pb,
                      "claim_a": ca, "claim_b": cb,
                      "site_side_of_a": "a" if ca == item["a"]["claim"]["claim_id"] else "b"})
    return pairs, articles, extractions


def routes_copy():
    document = json.loads(Path("results/production_uncorrected/classifier_routes.json").read_text())
    model = document["models"][MODEL]
    assert sum(r["tag"] == HELD_ROUTE for r in model["routes"]) == 1
    model["routes"] = [r for r in model["routes"] if r["tag"] != HELD_ROUTE]
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "classifier_routes_client_copy.json"
    path.write_text(json.dumps(document, indent=1) + "\n")
    return path


async def classify(client, pair, articles, extractions, setting):
    reasoning, cap = SETTINGS[setting]
    path = OUT / setting / f"{pair['pair_id']}.json"
    request = classification_request(pair, articles, extractions, model=MODEL,
                                     reasoning=reasoning, max_output_tokens=cap)
    if path.exists():
        saved = json.loads(path.read_text())
        assert saved["request_identity"] == request["identity"]
        return saved
    record = {"pair_id": pair["pair_id"], "setting": setting, "model": MODEL, "reasoning": reasoning,
              "max_output_tokens": cap, "request_identity": request["identity"],
              "prompt_sha256": request["prompt_sha256"], "schema_sha256": request["schema_sha256"],
              "site_side_of_a": pair["site_side_of_a"], "attempts": []}
    (OUT / setting / "requests").mkdir(parents=True, exist_ok=True)
    (OUT / setting / "requests" / f"{pair['pair_id']}.json").write_text(
        json.dumps({"messages": request["messages"], "schema": request["schema"]}, ensure_ascii=False))
    provider, verdict = None, None
    for number in (1, 2):
        started = time.monotonic()
        attempt = {"number": number}
        try:
            response, meta = await client.generate(
                MODEL, request["messages"], request["schema"], reasoning, cap, forced_provider=provider,
                cache_namespace=f"classification-v21-20261116/{setting}/{request['identity']}/attempt-{number}")
            provider = meta["requested_provider"]
            attempt.update(response=response, meta=meta, usage=response.get("usage"))
            verdict = validate_classification(request["payload"], _parse_response(response))
            attempt.update(status="valid", verdict=verdict)
        except Exception as exc:  # noqa: BLE001 -- recorded; one identical retry follows
            provider = getattr(exc, "requested_provider", provider)
            attempt.update(status="invalid", error=f"{type(exc).__name__}: {str(exc)[:300]}")
            if type(exc).__name__ in {"BudgetError", "AccountingError", "RequestConfigurationError"}:
                attempt["seconds"] = time.monotonic() - started
                record["attempts"].append(attempt)
                record["status"] = "blocked"
                break
        attempt["seconds"] = time.monotonic() - started
        record["attempts"].append(attempt)
        if verdict is not None:
            break
    record.setdefault("status", "valid" if verdict is not None else "invalid")
    record["verdict"] = verdict
    path.write_text(json.dumps(record, ensure_ascii=False))
    print(json.dumps({"setting": setting, "pair": pair["pair_id"], "status": record["status"],
                      "attempts": len(record["attempts"])}), flush=True)
    return record


def v2_labels():
    sampling = json.loads(Path("data/derived/annotation_sampling_private.json").read_text())
    source = {i["item_id"]: i["source_pair_id"] for i in sampling["items"] if i["task"] == "pair"}
    supersedes = json.loads((HANDOFF / "items.manifest.json").read_text())["supersedes"]
    wanted = {source[old]: new for new, old in supersedes.items()}
    labels = {}
    for row in read_jsonl("data/derived/production_uncorrected/full/pairs_classified.jsonl"):
        if row["pair_id"] in wanted:
            labels[wanted[row["pair_id"]]] = (row.get("verdict") or {}).get("label")
    return labels


def summarize(records, v2):
    out = {"model": MODEL, "settings": {}, "changes": {}}
    for setting in SETTINGS:
        rows = [r for r in records if r["setting"] == setting]
        tokens = collections.Counter()
        cost = Decimal(0)
        for r in rows:
            for a in r["attempts"]:
                usage = a.get("usage") or {}
                tokens["prompt"] += usage.get("prompt_tokens") or 0
                tokens["completion"] += usage.get("completion_tokens") or 0
                tokens["reasoning"] += (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
                if usage.get("cost") is not None:
                    cost += Decimal(str(usage["cost"]))
        completions = sorted((a.get("usage") or {}).get("completion_tokens") or 0
                             for r in rows for a in r["attempts"] if a.get("usage"))
        out["settings"][setting] = {
            "pairs": len(rows), "valid": sum(r["status"] == "valid" for r in rows),
            "first_attempt_valid": sum(r["attempts"][0]["status"] == "valid" for r in rows if r["attempts"]),
            "attempts": sum(len(r["attempts"]) for r in rows), "tokens": dict(tokens),
            "max_completion_tokens_observed": completions[-1] if completions else None,
            "reported_cost_usd": str(cost)}
    label = {(r["setting"], r["pair_id"]): (r["verdict"] or {}).get("label") if r["verdict"] else None
             for r in records}
    ids = sorted({r["pair_id"] for r in records})

    def compare(left, right):
        both = [i for i in ids if left(i) and right(i)]
        return {"compared": len(both),
                "category_changed": sum(left(i) != right(i) for i in both),
                "binary_flag_changed": sum((left(i) in FLAGGED) != (right(i) in FLAGGED) for i in both),
                "flagged_left": sum(left(i) in FLAGGED for i in both),
                "flagged_right": sum(right(i) in FLAGGED for i in both)}

    out["changes"]["low_vs_high"] = compare(lambda i: label[("low", i)], lambda i: label[("high", i)])
    out["changes"]["v2_vs_v21_low"] = compare(lambda i: v2.get(i), lambda i: label[("low", i)])
    out["changes"]["v2_vs_v21_high"] = compare(lambda i: v2.get(i), lambda i: label[("high", i)])
    out["label_counts"] = {s: dict(collections.Counter(label[(s, i)] for i in ids)) for s in SETTINGS}
    out["label_counts"]["v2"] = dict(collections.Counter(v2.get(i) for i in ids))
    return out


async def main():
    pairs, articles, extractions = load()
    routes = routes_copy()
    ledger = SQLitePhaseLedger("openrouter", "production_classification")
    try:
        client = V2Client("production_classification", config=str(routes), ledger=ledger, concurrency=70)
        client.http = TotalDeadlineHTTP(client.http)
        try:
            if PLAN_ONLY:
                worst = Decimal(0)
                for setting, (reasoning, cap) in SETTINGS.items():
                    for pair in pairs:
                        request = classification_request(pair, articles, extractions, model=MODEL,
                                                         reasoning=reasoning, max_output_tokens=cap)
                        route = max((r for r in client.models[MODEL]["routes"] if r["max_completion_tokens"] >= cap),
                                    key=lambda r: Decimal(r.get("price_ceiling", r["pricing"])["completion"]))
                        body = client.make_request(MODEL, request["messages"], request["schema"], reasoning, cap, route)
                        worst += Decimal(str(client.reserve_cost(body)))
                print(json.dumps({"pairs": len(pairs), "requests": 2 * len(pairs),
                                  "worst_case_reservation_usd": str(round(worst, 4)),
                                  "pilot_pairs_present": sorted({"v2-pair-1043", "v2-pair-1087"}
                                                                & {p["pair_id"] for p in pairs})}))
                return
            records = await asyncio.gather(*[classify(client, pair, articles, extractions, setting)
                                             for setting in SETTINGS for pair in pairs])
        finally:
            await client.close()
            try:
                ledger.export_json()
            except Exception as exc:  # noqa: BLE001
                print(json.dumps({"export_skipped": str(exc)}), flush=True)
            totals = ledger.totals()
            print(json.dumps({"ledger": totals}, default=str), flush=True)
    finally:
        ledger.close()
    summary = summarize(records, v2_labels())
    summary["retired_excluded"] = sorted(RETIRED)
    summary["route_hold_mirrored"] = HELD_ROUTE
    summary["extraction_source"] = str(EXTRACTED)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
