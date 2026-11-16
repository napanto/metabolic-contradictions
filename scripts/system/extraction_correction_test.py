"""Quick extraction test (Antonio, 14 November 13:20): does deciding the assertion last help?

Arms on 40 random F abstracts outside the human subset:
  control       frozen prompt and wire schema, re-run (run-to-run noise);
  order         frozen prompt, wire schema with result.assertion moved last;
  order_prompt  order + prompt additions (assertion rules, counts, population, context).
The 66 human-subset abstracts run blind with order_prompt, fixed before any result is seen.
Spend is recorded in the SQLite production ledger (phase production_extraction).
Run from the project root with .venv/bin/python.
"""

import asyncio
import contextlib
import gzip
import json
import random
import sys
import time
from copy import deepcopy
from pathlib import Path

import httpx

sys.path.insert(0, "src")
from chl_rag.io import read_jsonl  # noqa: E402
from chl_rag.model_client import V2Client  # noqa: E402
from chl_rag.model_runs import _parse_response  # noqa: E402
from chl_rag.extraction import WIRE_MODEL_EXTRACTION, extraction_request, validate_extraction  # noqa: E402
from chl_rag.sqlite_ledger import SQLitePhaseLedger  # noqa: E402

OUT = Path(sys.argv[1])
ONLY = sys.argv[2] if len(sys.argv) > 2 else "all"
MODEL, REASONING, CAP = "z-ai/glm-5.3-flash", "high", 131072

ADDITION = """

Additional result-assertion rules. Decide result.assertion last, after copying the evidence, the estimate direction and the statistical information for this finding. Use 'directional' whenever the abstract reports for this finding that a quantity was higher, lower, increased, decreased, improved, worsened, reduced, associated, correlated or predicted, whether or not it was statistically significant. Use 'explicit_no_difference' only when the authors explicitly state for this finding that there was no difference, no change, no association or no significant effect (for example 'did not differ', 'was not associated', 'no significant change', 'similar between groups'). Use 'not_confirmed' only when the authors explicitly state, for this exact analysis, that an expected or hypothesised effect was not confirmed or did not persist (for example after adjustment). A significant p-value, a confidence interval that excludes the null, or wording such as 'significantly' for this finding is incompatible with 'explicit_no_difference' and 'not_confirmed'. Examples: 'X showed a significant inverse correlation with HbA1c (P < 0.05)' is directional with '-'; 'Y increased significantly with the number of components' is directional with '+'; 'the risk was significantly lower in group A than in group B (HR 0.80; 95% CI 0.70-0.90)' is directional with '-'; 'drug A reduced body weight compared with placebo' is directional with '-'; 'HbA1c did not differ between groups (p = 0.41)' is explicit_no_difference.

Sample size, population and context. Whenever the abstract states how many participants were studied, record that count in study.total_n, and record a group's n when the abstract gives it for that group; never omit a stated count, and never compute a count the abstract does not state. For an association without groups, record the population analysed (for example 'adults with type 2 diabetes') in analysis.analysis_population. Record the biological material (for example serum, plasma, urine, whole blood or tissue) in context.sample and the country or setting in context.region whenever the abstract states them."""


ADDITION2 = ADDITION.split("\n\nSample size, population and context.")[0] + """

Population and biological material. For an association without groups, record the population analysed (for example 'adults with type 2 diabetes') in analysis.analysis_population. Record the biological material (for example serum, plasma, urine, whole blood or tissue) in context.sample whenever the abstract states it."""


ADDITION3 = ADDITION.split("\n\nSample size, population and context.")[0] + """

Sample size. Fill study.total_n whenever any sentence states how many participants the study included, enrolled, randomized or analysed; leave it null only when no such count is stated anywhere in the abstract. Examples: 'A cross-sectional study was conducted (N = 87, aged 30-65 years)' gives value 87, unit participants, scope enrolled, reported_text 'N = 87'; 'Among a total of 12 480 newly treated patients' gives value 12480, reported_text 'a total of 12 480 newly treated patients'; 'Of 2350 patients, 1410 achieved the target' gives value 2350, reported_text 'Of 2350 patients'. When the abstract states a group's count, record it in that group's n. Never add, subtract or divide stated numbers to obtain a count.

Population, material and place. For an association without groups, record the population analysed (for example 'adults with type 2 diabetes') in analysis.analysis_population. Record the biological material (for example serum, plasma, urine, whole blood or tissue) in context.sample, and the country or city where the study was conducted (for example 'Lyon, France') in context.region, whenever the abstract states them."""


ADDITION4 = ADDITION3.split("\n\nPopulation, material and place.")[0] + """

Population, material and place. For an association without groups, record the population analysed (for example 'adults with type 2 diabetes') in analysis.analysis_population. context.region: the country, region or city of the study population or setting whenever the abstract names it, including through a nationality (for example 'in Lyon, France' gives 'Lyon, France'; 'Danish adults' gives 'Denmark'); the same region applies to every finding of that study. context.sample: the biological material in which this finding's exposure or outcome was measured whenever the abstract names it (for example 'serum ferritin' or 'ferritin measured in serum' gives 'serum'; 'plasma adiponectin' gives 'plasma'; 'urinary albumin' gives 'urine'); use null for findings not measured in a biological material, such as body weight or blood pressure."""


def reordered_schema():
    schema = deepcopy(WIRE_MODEL_EXTRACTION)
    props = schema["properties"]["claims"]["items"]["properties"]["result"]["properties"]
    assertion = props.pop("assertion")
    props["assertion"] = assertion
    return schema


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


async def one(client, article, arm, sem, folder):
    path = OUT / folder / f"{article['pmid']}.json"
    if path.exists():
        return json.loads(path.read_text())
    request = extraction_request(article, model=MODEL, reasoning=REASONING, max_output_tokens=CAP)
    messages, schema = request["messages"], request["schema"]
    if arm != "control":
        schema = reordered_schema()
    if arm in ("order_prompt", "order_prompt2", "order_prompt3", "order_prompt4"):
        messages = deepcopy(messages)
        messages[0]["content"] += {"order_prompt": ADDITION, "order_prompt2": ADDITION2,
                                   "order_prompt3": ADDITION3, "order_prompt4": ADDITION4}[arm]
    record = {"pmid": article["pmid"], "arm": arm, "attempts": []}
    provider = None
    async with sem:
        for number in (1, 2):
            started = time.monotonic()
            try:
                response, meta = await client.generate(
                    MODEL, messages, schema, REASONING, CAP, forced_provider=provider,
                    cache_namespace=f"order-test-20261114/{arm}/{article['pmid']}/attempt-{number}",
                )
                provider = meta["requested_provider"]
                parsed = _parse_response(response)
                validated = validate_extraction(article, parsed)
                record["attempts"].append({"status": "valid", "seconds": time.monotonic() - started})
                record.update(status="valid", extraction=validated)
                break
            except Exception as exc:  # noqa: BLE001 -- recorded; a second attempt follows
                record["attempts"].append(
                    {"status": "invalid", "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                     "seconds": time.monotonic() - started}
                )
                if type(exc).__name__ in {"BudgetError", "AccountingError", "RequestConfigurationError"}:
                    break
        else:
            record["status"] = "invalid"
        record.setdefault("status", "invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False))
    print(json.dumps({"arm": folder, "pmid": article["pmid"], "status": record["status"],
                      "attempts": len(record["attempts"])}), flush=True)
    return record


async def main():
    human = set()
    for line in open("annotation/study_pairs_uncorrected/items.jsonl"):
        item = json.loads(line)
        human |= {item["a"]["article"]["pmid"], item["b"]["article"]["pmid"]}
    f_pmids = json.loads(Path("data/derived/partitions.json").read_text())["partition_pmids"]["F"]
    pool = sorted(set(f_pmids) - human)
    test = random.Random(20261114).sample(pool, 40)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "selection.json").write_text(json.dumps({"seed": 20261114, "test": test,
                                                    "human": sorted(human)}, indent=1))
    wanted = set(test) | human
    articles = {a["pmid"]: a for a in read_jsonl("data/derived/corpus.jsonl") if a["pmid"] in wanted}
    jobs = []
    if ONLY in ("all", "test"):
        jobs += [(articles[p], arm) for arm in ("control", "order", "order_prompt") for p in test]
    if ONLY == "v4":
        jobs += [(articles[p], "order_prompt4") for p in test]
        jobs += [(articles[p], "human_order_prompt4") for p in sorted(human)]
    if ONLY == "v3":
        jobs += [(articles[p], "order_prompt3") for p in test]
        jobs += [(articles[p], "human_order_prompt3") for p in sorted(human)]
    if ONLY == "v2":
        jobs += [(articles[p], "order_prompt2") for p in test]
        jobs += [(articles[p], "human_order_prompt2") for p in sorted(human)]
    if ONLY in ("all", "human"):
        jobs += [(articles[p], "human_order_prompt") for p in sorted(human)]
    ledger = SQLitePhaseLedger("openrouter", "production_extraction")
    try:
        with (contextlib.nullcontext() if ONLY in ("v2", "v3", "v4") else ledger.legacy_json_lease()):
            client = V2Client("production_extraction", config="configs/routing_registry.json",
                              ledger=ledger, concurrency=96)
            client.http = TotalDeadlineHTTP(client.http)
            sem = asyncio.Semaphore(96)
            try:
                results = await asyncio.gather(*[
                    one(client, art, folder.removeprefix("human_"), sem, folder)
                    for art, folder in jobs])
            finally:
                await client.close()
                try:
                    ledger.export_json()
                except Exception as exc:  # noqa: BLE001 -- the other run exports
                    print(json.dumps({"export_skipped": str(exc)}), flush=True)
                print(json.dumps({"ledger": ledger.totals()}, default=str), flush=True)
    finally:
        ledger.close()
    return results


if __name__ == "__main__":
    asyncio.run(main())
