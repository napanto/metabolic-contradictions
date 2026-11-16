# Amendment 05B — v2 pair-classifier selection

13 November 2026. Prospective registration, signed before any new paid inference.
Authority: `CLASSIFIER_SELECTION_BRIEF.md` and the approved decisions of
13 November, §§5–6. This amendment supersedes only the v1 classifier selection
rule for the v2 pipeline. V1 prompts, selection, predictions and benchmark remain
unchanged. Work is confined to branch `v2-classifier-selection`; no merge, push,
deployment, contact with others, or writes to the sibling project are authorized.

## Data and frozen task

Use the existing ManConCorpus v1 split, **without question** only: development
585 pairs (240 inconsistent, 345 consistent; 8 questions, 7 connected components)
for selection, then one exploratory test of 1,190 pairs (16 questions, 15 connected
components). No new splitting or test-directed changes.

Use `configs/prompts_mancon.json`, version **mancon-dev-v2**, unchanged, including
the existing `benchmark_llm.batch_request` and JSON schema. Preserve v1 ordering
and batches of 8 (last batch smaller). Temperature 0, seed 42. The model sees
only the two claim texts and local item IDs, never labels or question text.
No output repair, rationale editing, label adjudication, or selective re-generation.
Malformed/missing outputs remain invalid and in the denominator.

Frozen SHA256:

| File | SHA256 |
|---|---|
| `data/derived/mancon_pairs.jsonl` | `b8cc538e32f2daf5dad679153e2744505878e5c7e8750fd6e705956cba5afdb5` |
| `data/derived/mancon_split.json` | `2edc668dd5e48451cb1b8069cf11f3335f3363d92160d1019bd819cc5c9dedcc` |
| `configs/prompts_mancon.json` | `c0e26ac67e88675ac6629f7e4a137872e5aba8e7f90b0e6c31d446cef93ce9f1` |

Canonical v1 prompt digest (`json.dumps(..., sort_keys=True)`):
`d947d2b52ab9930669efb5da5d07c20637d28f9cb8b0cecec4cd600ab3f5b024`.

## Live grid and declared availability

The authoritative catalogue and all four endpoint listings were downloaded from
OpenRouter on 13 November before inference. Raw evidence and acquisition hashes
are in `papers/inbox-2026-11-13/classifier-selection/`; machine-readable routes,
prices and exclusions are frozen in `configs/classifier_selection_v2.json`.

| Model | Registered configurations | Live interpretation |
|---|---|---|
| `qwen/qwen3.6-35b-a3b` | off, on | No `supported_efforts`; binary disabled/enabled, no invented named levels |
| `qwen/qwen3.8-flash` | off, low, medium, xhigh (memo grid, all unavailable) | Current catalogue omits `supported_efforts`, exposes `supports_max_tokens`; only Alibaba endpoints with **unknown** quantization |
| `deepseek/deepseek-v4.1-flash-1017` | none/off, low, high, max | Live `supported_efforts`: max, high, low, none |
| `z-ai/glm-5.3-flash` | low, high, max | Live `supported_efforts`: max, high, low; reasoning mandatory |

Thus nine configurations are executable under the required fp8 policy; all four
historical Qwen3.8 rows are retained as unavailable, not poor-scoring candidates.
Its current binary/token-budget interface likewise cannot be exercised on an
eligible endpoint. The memo's 13 executable configurations are not corroborated
by the live catalogue. No paid calls to unknown/fp4 providers or invented effort
levels will be used to fill that gap. Report unavailable levels and reasons.

## Routing, smoke measurement and execution

Use only the explicitly enumerated, currently active **fp8** endpoints supporting
seed, temperature, reasoning, max_tokens, response_format and structured_outputs.
No automatic fallback outside that set. Each request pins exactly one allowed
endpoint with `only`, `order`, `allow_fallbacks=false`, `require_parameters=true`,
and `quantizations=["fp8"]`. Record requested endpoint, returned provider,
canonical model, request hash, usage, cost, finish reason and elapsed time.

Route cheapest first, with PrimeIntellect first for Qwen3.6. The frozen ordering
uses the quote for 1,000 input plus 4,000 output tokens (and request price), then
endpoint tag; overflow uses the next allowed free provider. Enforce at most 16
in-flight requests **per provider name across models and endpoint variants**.
Known-free 429s use bounded adaptive exponential backoff and Retry-After, with
at most 12 attempts; reduce pressure on the throttled provider. Authentication,
accounting and ambiguous billable failures stop new work and drain in-flight
requests. Preserve reservations for unknown costs; do not invent usage.

After signing this registration, use the **first 10 development pairs** for each
available configuration (batches 8+2), solely to measure output/reasoning length,
schema support, cost and latency. Smoke outputs are separate; all 585 dev pairs
are subsequently generated with the frozen calibrated cap. Do not inspect smoke
F1 to alter prompts, reasoning choices or candidates.

Initial total `max_tokens`: off/none 2,048; on/low 16,384; high 32,768;
max/xhigh 65,536. On smoke truncation or consumption at least 90% of the limit,
double the total limit up to the endpoint's declared ceiling and repeat the
same smoke data. Retain and charge every attempt. An infeasible configuration
is reported, never ranked as artificially inaccurate because of a small cap.

Freeze each final cap before bulk dev: at least its initial cap, twice the
largest observed completion rounded up to 1,024, and enough room for the visible
JSON after reasoning allocation. Visible margin is max(2,048, twice observed
visible tokens); use JSON UTF8 bytes conservatively if reasoning-token detail
is absent. For gateway fraction mapping allow visible_margin/(1-f), where
f=.2 for low, .8 for high, .95 for max/xhigh; binary on has no invented fraction.
Use endpoint limits and measured smoke evidence; never lower the maximum effort
to save time. Further cap changes after bulk output begins require stopping and
reporting, not selectively rerunning truncated rows.

Run the entire available dev grid concurrently, interleaving configurations so
maximum effort is not deferred. Retain all calls, completed and invalid rows,
provider mix and cap measurements. No performance-based early elimination.

## Metric, uncertainty and one-time selection

Primary metric: inconsistent-class F1 over all 585 pairs. Invalid predictions
count as errors: FN for a gold positive, FP for a gold negative. Report precision,
recall, accuracy and valid-only metrics secondarily. Eligibility requires complete
dev coverage and at least **98% first-generation pair-level schema validity**;
valid means the complete strict batch schema and exact unique IDs pass. Also
report request-level validity. No repair can conceal an invalid first output.

Use the unchanged v1 connected-question-component percentile bootstrap: 2,000
resamples, seed 42, 2.5th/97.5th percentiles, with component count and undefined
resamples reported. Never bootstrap dependent pairs as independent observations.

Among eligible configurations, take the maximum F1 and include all configurations
within **0.01 inclusive**. Choose highest bootstrap F1 lower bound, then lower
actual dev request cost, then lower mean request latency. Cost includes all
attributable dev attempts, excludes smoke; latency is mean elapsed per dev API
attempt. A remaining exact tie uses lexicographic configuration ID. No v1 Qwen
portability preference or extraction-field metric applies. If none is eligible,
stop and report; do not select from test. Freeze the selected ID, caps, routing,
prompt/data/code hashes and dev result before any test call.

## Hard budget and audit

**USD30 hard cap for this branch**, including all smoke, failed/billable attempts,
full dev grid and the single exploratory test. No other paid API. Credentials
are read in memory only from `~/.config/chl-openrouter.env`, never printed/saved.
Use the independent, locked, atomically updated ledger
`results/spend_classifier_selection.json`. Before **every** request reserve its
worst-case uncached input bound, full output cap and request fee at frozen price
ceilings; include schema and framing in input bounds. Enforce those ceilings
with provider `max_price`; a price change cannot silently overrun reservations.
Settled spend plus in-flight reservations plus unknown liabilities must stay
at or below USD30. A cap refusal stops the run and is disclosed.

Before bulk dev, project remaining full grid plus the most expensive possible
selected test using smoke token costs repriced at the most expensive eligible
route and a 50% margin, plus committed spend/liabilities. Repeat before test and
as new usage changes the projection. If projected total exceeds USD30, **stop
and report**, without dropping expensive candidates or using a cheaper effort.
The ledger's worst-case per-call reservations remain the hard enforcement even
when observed usage is below output limits. Smoke and final provider-account
snapshots are read-only; concurrent master spending prevents attributing shared
account deltas to this branch alone.

The final audit documents this ledger as **additional** to the v1/master ledgers.
It must sum each distinct ledger once, preserving both settled spend and unknown
liabilities; this branch's USD30 is not the sibling project's allowance. Never
modify the sibling ledger. If master is still spending, identify any combined
audit as a timestamped snapshot requiring final integration after it finishes.

## Exploratory test and delivery

Only after dev selection is frozen, run the selected configuration **once** over
all 1,190 without-question test pairs, unchanged prompt, routing policy, decoding
and calibrated caps. A persistent test-start lock prevents a second run; safe
resume may use recorded requests only and must not regenerate completed items.
No tuning, alternative model evaluation, selection changes or semantic fixes
after viewing test outputs. This is explicitly **exploratory**, because v1
already used the test; the v1 result remains the pre-registered benchmark.

Deliver `results/v2_classifier_selection.json` with every grid row, intervals,
validity, tokens, cost, latency, providers and unavailable levels;
`results/v2_classifier_test_exploratory.json`; a short
`docs/v2_classifier_selection.md`; the ledger, provenance and reproducible code.
Keep `STATUS_CLASSIFIER.md` current in Italian. Commit the deliverables signed
on this branch, verify signatures, and stop. Do not merge into master.
