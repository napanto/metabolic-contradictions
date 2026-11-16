#!/usr/bin/env python3
"""Addendum 08A: snapshot the public GLM-5.3-Flash endpoint catalogue and derive the v2.1 routes.

Public catalogue only (no key). Eligible routes, by Antonio's rule of 16 November 2026:
fp8 only; status 0; max_completion_tokens >= the registered extraction cap (131072);
supports reasoning, response_format or structured_outputs, and seed (the registered
request sends seed 42 with require_parameters); same canonical model version as the
registered routes. Tenstorrent stays excluded (cap 131000; most requests rejected on
14 November). Price ceilings follow the v2 rule: twice the listed price.

Usage (from the main project root): python snapshot_routes.py OUT_DIR
Writes OUT_DIR/configs/provider_snapshots/glm-5.3-flash_endpoints_<UTC>.json and
OUT_DIR/configs/corpus_routes.json.
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

URL = "https://openrouter.ai/api/v1/models/z-ai/glm-5.3-flash/endpoints"
MODEL = "z-ai/glm-5.3-flash"
CAP = 131072
EXCLUDED = {"tenstorrent/fp8": "cap 131000 below 131072; most requests rejected on 14 November"}


def main():
    out = Path(sys.argv[1])
    base = json.loads(Path("configs/routing_registry.json").read_text())["models"][MODEL]
    response = httpx.get(URL, timeout=30)
    response.raise_for_status()
    raw = response.content
    fetched = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_rel = f"configs/provider_snapshots/glm-5.3-flash_endpoints_{fetched}.json"
    (out / snapshot_rel).parent.mkdir(parents=True, exist_ok=True)
    (out / snapshot_rel).write_bytes(raw)
    snapshot_sha = hashlib.sha256(raw).hexdigest()
    endpoints = json.loads(raw)["data"]["endpoints"]
    routes, decisions = [], []
    for e in endpoints:
        params = set(e.get("supported_parameters") or [])
        version = (e.get("name") or "").rsplit(" | ", 1)[-1]
        reasons = []
        if e.get("quantization") != "fp8":
            reasons.append("quantization " + str(e.get("quantization")))
        if e.get("status") != 0:
            reasons.append("status " + str(e.get("status")))
        if (e.get("max_completion_tokens") or 0) < CAP:
            reasons.append("max_completion_tokens " + str(e.get("max_completion_tokens")))
        if "reasoning" not in params:
            reasons.append("no reasoning")
        if not params & {"response_format", "structured_outputs"}:
            reasons.append("no response_format/structured_outputs")
        if "seed" not in params:
            reasons.append("no seed")
        if version != base["canonical_slug"]:
            reasons.append("model version " + version)
        if e.get("tag") in EXCLUDED:
            reasons.append(EXCLUDED[e["tag"]])
        decisions.append({"tag": e.get("tag"), "provider_name": e.get("provider_name"),
                          "quantization": e.get("quantization"), "eligible": not reasons,
                          "reasons": reasons})
        if reasons:
            continue
        price = e["pricing"]
        routes.append({
            "tag": e["tag"], "provider_name": e["provider_name"], "quantization": "fp8",
            "model_id": MODEL, "name": e.get("name"), "context_length": e.get("context_length"),
            "max_completion_tokens": e["max_completion_tokens"],
            "supported_parameters": sorted(params), "pricing": price,
            "price_source": snapshot_rel, "price_source_sha256": snapshot_sha,
            "price_ceiling": {"prompt": str(Decimal(price["prompt"]) * 2),
                              "completion": str(Decimal(price["completion"]) * 2),
                              "request": str(Decimal(price.get("request", "0") or "0") * 2)},
        })
    model = {k: v for k, v in base.items() if k != "routes"}
    model.update(routes=routes, snapshot=snapshot_rel, snapshot_sha256=snapshot_sha,
                 authorization="Antonio, 16 November 2026 (addendum 08A): every eligible fp8 route")
    document = {"schema_version": 1, "purpose": "addendum 08A v2.1 corpus routes",
                "fetched_at_utc": fetched, "source_url": URL, "eligibility_cap": CAP,
                "decisions": decisions, "models": {MODEL: model}}
    (out / "configs/corpus_routes.json").write_text(json.dumps(document, indent=1) + "\n")
    print(json.dumps({"snapshot": snapshot_rel, "snapshot_sha256": snapshot_sha,
                      "eligible": [r["tag"] for r in routes]}))


if __name__ == "__main__":
    main()
