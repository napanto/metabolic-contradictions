# Amendment 05C — expanded ManCon development screen

13 November 2026. Signed before any Round 2 paid inference. Authority: Antonio's
Round 2 instruction in `CLASSIFIER_SELECTION_BRIEF.md` and decisions §§14–15.
Target: a complete table in about 60 minutes; publish each completed row promptly.

This is an **external screen** for the report and to inform Antonio's choice of
large model(s) for the full v2 experiment. It **does not select the production
classifier**, whose primary selection uses metabolic development pairs (§14).
Round 1 and the v1 benchmark remain unchanged. **No ManCon test inference,
evaluation, or modification is authorized in this round.**

## Frozen task and grid

Use the same 585 without-question dev pairs, v1 split, 8 questions and **7
independent connected components**, and unchanged `mancon-dev-v2` prompt/schema.
Batch size 8, final batch 1; temperature 0; seed 42 wherever supported. Labels
and question text are excluded from model inputs. Invalid final outputs remain
errors (FN for gold positive, FP for gold negative); no semantic repair or
performance-directed rerun. Save raw requests/responses, IDs, provider, usage,
cost, latency, validity and failures. Previously completed requests are reused.

Live catalogue and endpoint snapshots, timestamps and hashes are archived in
`papers/inbox-2026-11-13/mancon-screen/`. Exact IDs, source/input checksums,
reasoning metadata, routes and fixed prices are frozen in
`configs/mancon_screen.json`. The live grid contains **34 configurations**:

| Group | Exact model ID | Distinct settings | Native output maximum |
|---|---|---|---:|
| large | `moonshotai/kimi-k3.1-pro` | max, high, low, none | 943718 |
| large | `moonshotai/kimi-k3` | max, high, low, none | 943718 |
| large | `qwen/qwen3.8-2.4t-a95b` | xhigh, medium, low | 131072 |
| large | `z-ai/glm-5.3` | max, high, low | 943718 |
| large | `z-ai/glm-5.2` | xhigh, high, none | 943718 |
| large | `deepseek/deepseek-v4-pro-0813` | max, high, low, none | 943718 |
| large | `deepseek/deepseek-r1-0528` | on (mandatory) | 32768 |
| large | `mistralai/mistral-large-4-0` | high, none | 262144 |
| large | `nvidia/nemotron-3-ultra-550b-a55b` | high, medium, none | 182520 |
| mid | `google/gemma-4-31b-it` | off, on | 16384 |
| mid | `openai/gpt-oss-120b` | high, medium, low | 65536 |
| mid | `minimax/minimax-m3` | off, on | 512000 |

Named settings come from live `reasoning.supported_efforts`. Where that field
is absent, use the advertised reasoning toggle, respecting mandatory reasoning.
`none` means off. Do not substitute aliases or `:batch`/`:free` model variants.
**Skip smoke tests.** Every request uses the model's catalogue
`top_provider.max_completion_tokens` above, with no effort-dependent reduction.
Only endpoints able to accept that full limit enter the declared route set.

## Routing and scheduling

Use active endpoints supporting temperature, native maximum output, reasoning
(where relevant), response_format and structured_outputs. Prefer seed-supporting
endpoints whenever any meet those conditions; otherwise omit seed and disclose
it. Nemotron/KT is the registered seed exception. Among qualifying endpoints,
choose the cheapest known-quantization endpoint using a fixed quote of 1000
input + 4000 output tokens, then permit all eligible endpoints of that **same
declared quantization**. If none has known quantization, pin the single cheapest
provider/endpoint and record `unknown`. Quantization need not match across models.
The manifest records every exact route; no fallback outside that set is allowed.

Request each chosen endpoint with only/order pinning, required parameters and
fallbacks disabled. Known quantization is explicitly filtered; unknown is allowed
only for the single registered route. Route cheapest first. Fixed price ceilings
are **1.25 times the archived uncached quotes**, applied both to max_price and
worst-case reservations; actual billed cost remains separately reported.
Generation-free route/parameter rejections may try another declared compatible
route and are retained. If every route rejects a configuration, report it as
unavailable, not a weak result. Do not hide client implementation errors as model
unavailability. Unknown billable failures retain reservations and stop admission.

Launch all configuration queues together, scheduling fairly rather than letting
one provider's blocked queue delay the others. At most **16 concurrent requests
per provider name across all models/endpoints**; bounded adaptive backoff on
429, recording every attempt. Funding reservations may reduce instantaneous
concurrency. The 60-minute target does not authorize dropping configurations,
lowering effort, truncating native limits, or disguising incomplete results.

## Metrics and live table

Report F1 (the preregistered ManCon metric), **F0.5 = 5TP/(5TP+FN+4FP)**,
precision, recall, both 95% CIs, schema validity, token usage, cost, mean call
latency and provider mix. Use the same **2000 component-bootstrap resamples,
seed 42**, percentile 2.5/97.5; F1 and F0.5 share each resample. Preserve v1's
zero-denominator convention and report undefined resamples. Eligibility requires
at least **98% first-generation pair-level schema validity** and complete dev
coverage. Also report request validity and invalid/missing counts.

Descriptively rank eligible **large** configurations by F0.5. Form an inclusive
0.01 tie pool relative to the maximum; prefer higher F0.5 bootstrap lower bound,
then lower actual cost, then lower mean latency (configuration ID for an exact
residual tie). With **only seven independent components**, intervals are wide
and this ranking is indicative. Partial rankings are explicitly provisional.
Do not convert the ranking into a production-classifier selection.

Show all 13 Round 1 configurations alongside, preserving their original data,
F1/CIs, validity and accounting. Derive F0.5 and its CI offline from the frozen
Round 1 dev predictions in the new screen artifact only; do not rerun inference
or change Round 1 files. After **each configuration completes**, atomically
refresh the full JSON and the Italian table in `STATUS_CLASSIFIER.md`, including
unfinished and unavailable rows explicitly. Preserve the old status separately.

## Shared-account funds and delivery

Only OpenRouter is paid. Read the external credential in memory, never print it.
Use independent `results/spend_mancon_screen.json`, no fixed branch cap, and
reserve uncached input/schema/framing plus the full native output maximum before
every call at its fixed ceilings. Preserve unknown liabilities; no fabricated
usage. The planning projection is about USD60, with a USD100 upper scenario;
update it from observed costs without removing candidates for cost.

Read shared account balance through **GET /api/v1/credits** before inference and
frequently during admission (about every 2 seconds). Subtract this runner's
in-flight worst-case reservations, retained liabilities and settled charges
since the snapshot. Never admit a call that would leave less than **USD25**.
If temporary reservations consume the headroom, wait for already-running calls
to settle. If the balance itself falls below the protected floor, or insufficient
funds persist after settling work drains, pause new calls and write
**“Serve da Antonio: ricarica OpenRouter di circa X USD”** in STATUS, supported by
the remaining estimate and observed balance. Resume only after top-up is
explicitly confirmed in the brief and credit is available. Another runner's
future spending is not visible; refresh shared-account checks continuously.

Deliver `results/v2_mancon_screen.json`, append a short Round 2 section to
`docs/v2_classifier_selection.md`, and keep Italian STATUS current. Preserve
Round 1 results and v1 files. Commit signed on this branch, without merge, push,
deployment, interpersonal contact, or modifications to `../project`; then stop.
