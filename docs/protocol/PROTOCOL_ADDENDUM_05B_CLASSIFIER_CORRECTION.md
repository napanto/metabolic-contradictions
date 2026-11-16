# Addendum to 05B — Antonio's correction and interrupted-run recovery

13 November 2026. Signed before any resumed paid call, Qwen3.8 inference,
selection, or exploratory test. Antonio explicitly instructed this correction
in the conversation and in the two appended sections of
`CLASSIFIER_SELECTION_BRIEF.md` (18:56 and 18:58). This addendum supersedes the
conflicting routing/budget passages of 05B; the original signed text is retained.
No dev F1 has been computed or inspected, no winner selected, and no test run.

## All four models remain in the grid

The fp8 equivalence requirement applies when distributing **one model over
multiple providers**. Qwen3.8 on **Alibaba alone**, with quantization recorded as
`unknown`, is expressly allowed. Restore all four Qwen3.8 rows: **off, low,
medium, xhigh**. There are now **13 available configurations**, not nine.

The newly archived live OpenRouter catalogue still omits Qwen3.8
`supported_efforts` and declares `supports_max_tokens`. This absence is recorded,
not silently replaced by a purported gateway field. Alibaba's official API
documentation explicitly supports low, medium and xhigh, with none/off disabling
thinking; high/max are aliases of xhigh and minimal aliases low, not additional
distinct levels. Its documented reasoning budgets map to these native levels.
Use the four distinct settings through OpenRouter's reasoning parameter and
check acceptance on the fixed smoke pairs. All inference remains on OpenRouter;
no direct Alibaba API calls or additional paid service is authorized.

Freeze the single endpoint `alibaba/us-east-1`, the same endpoint as v1, at the
archived quote USD0.15/0.47 per million input/output tokens. Do not apply an fp8
filter to this endpoint or describe its quantization as verified. Other models
retain their original declared fp8 routes, pricing ceilings and concurrency
policy. The supplemental manifest is
`configs/classifier_selection_v2_addendum.json`; the base manifest stays intact.
The effective manifest replaces only Qwen3.8's availability/model metadata and
the explicitly amended budget policy.

Source snapshots and provider documentation, acquisition hashes and coverage are
in `papers/inbox-2026-11-13/classifier-selection/`. The original catalogue snapshot
and initial unavailable assessment remain as historical records.

Run the same first 10 dev pairs for each added Qwen3.8 configuration, separately
from its complete 585-pair dev run. Initial total token caps: off 2,048; low
16,384; medium 32,768; xhigh 65,536. Apply the original measured calibration,
visible-JSON margin and doubling rule up to Alibaba's declared 131,072 output
limit; the medium gateway fraction is 0.5. No prompt/schema edits or quality-based
screening. Retain the previously calibrated caps and completed predictions for
the original nine configurations, with their original request/cache identity.
Complete every available configuration before selection. All metric, validity,
bootstrap, tie and one-time exploratory-test rules remain as in 05B, subject
only to the explicit interrupted-attempt accounting below.

## USD30 is an alert point

There is **no fixed USD30 branch cap**. The ledger continues reserving worst-case
cost before every call, including retries, and retains unknown liabilities.
Record `cap: null`, `alert_threshold_usd: 30` and the authorizing addendum; never
erase or reset prior spend. Continue the complete-grid plus selected-test
projection with the registered 50% margin.

If the projection exceeds USD30, pause new inference and write
**“Serve da Antonio: ricarica OpenRouter di circa X USD”** in
`STATUS_CLASSIFIER.md`, with X supported by the projected requirement and live
available credit. Do not remove models, reduce effort, or silently continue.
Resume only after a top-up is confirmed in the brief. This is a funding alert
and confirmation point, not a permanent experiment cap. No such alert is
currently triggered by the measured projection.

## Preserve completed work and account for the interruption

Before Antonio's intentional interruption, a known-free HTTP404 stopped the
queue when Morph's GLM quote rose above its frozen ceiling. The refreshed endpoint
snapshot confirms USD0.116/0.122 per million instead of USD0.107/0.113. Disable
only `morph/fp8` for GLM via the evidence-checked availability record
`results/v2_classifier_routing_availability.json`. Do not raise prices or add
providers. Completed Morph generations remain valid observations from the
previously allowed route.

The interrupted process has exited. **4,304 completed dev predictions** are
preserved; no completed prediction is regenerated. One in-flight generation
lost its response: GLM/max, Novita, dev batch 52 (eight pairs), reservation
`42267f0697d540a2aa0a430aa7c9d92f`. No output or label from that attempt was seen.
Its full worst-case liability is **USD0.0198418542**. Three shared-account usage
snapshots are archived, with the first and third more than 60 seconds apart.
They show a stable excess of approximately USD0.00457729 over the initial
account baseline plus settled classifier calls. This is **not** an identified
per-call charge or proof of zero charge; another branch shares the account.
The available key cannot read current per-generation analytics (HTTP403).

Antonio explicitly requested resumption of unfinished batches. Therefore permit
**exactly one operational retry of this identified interrupted dev batch** with
the same claims, prompt, schema, decoding and calibrated total token cap, routed
within its already declared eligible provider set. This exception applies to
the interruption, not schema failures or observed incorrect labels, and does
not authorize a second exploratory test.

Move the original pending record to a permanently retained liability entry,
preserving all provenance and null actual cost/usage. Do not settle it at zero
or fabricate a response. The signed recovery manifest
`results/v2_classifier_interruption_recovery.json` identifies the exact original
reservation, request hash, pairs, evidence and single retry allowance. Preserve
the original start marker and interruption history. A second missing-response
retry is not authorized by this exception.

All eight retried pairs count as **first-generation invalid** for the 98% gate,
even if the retry returns valid labels. Primary F1 uses the eventual recorded
predictions; invalid final outputs remain errors under the original rule.
Report original failure, retry and final validity separately. Cost reporting
distinguishes response-backed actual spend from retained unknown liability;
if a cost tie-break is needed for a configuration containing this interruption,
use actual spend plus its full retained liability, clearly labeled a conservative
upper bound. Missing original tokens and latency remain unknown, not zero;
latency summaries identify their observed denominator.

Resume all not-yet-completed batches and run the four added Qwen3.8 configurations
in parallel under the provider limits. The final selection freezes this addendum,
the effective manifest, availability/recovery records, calibrated caps, all dev
results and implementation hashes before the sole exploratory test. Keep status
current, finish the deliverables, sign the final commit, and stop on this branch.
