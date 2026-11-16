# 05C correction — operator interruptions are not model attempts

13 November 2026. Signed before offline recomputation. Authority: Antonio's
“Round 2 correction (2026-11-13, 22:20)” in `CLASSIFIER_SELECTION_BRIEF.md`,
signed commit `525300e`. No new inference or paid calls are authorized.

Apply one uniform, outcome-independent rule to every active configuration:
an attempt whose response was lost because the operator stopped the process
is excluded from model-attempt validity. For those pairs, the first model
attempt is the first subsequent request whose response reached us. A valid
received response therefore does not inherit an invalidity flag solely from
`response_lost_at_user_interruption`. A failed received response still fails.

Non-operator transport failures, truncation (`length limit`), empty/unparseable
outputs and schema/ID errors continue counting against validity. The terminal
DeepSeek Pro/max ReadError remains eight invalid pairs. This correction changes
neither predictions nor abstention scoring, F1, F0.5, their confidence intervals,
precision/recall, costs, token limits, prompts or routing.

Preserve the previously preregistered first-attempt validity and report a second,
corrected validity column. Apply the unchanged 98% eligibility threshold to the
corrected column; retain old eligibility/ranking for audit and recompute the
descriptive large-model ranking with the existing F0.5/CI/cost/latency rules.
Keep a pair-level ledger for every pair invalid under either definition, with
evidence and causes: interruption, transport, truncation, empty or unparseable,
schema. A recovered pair can have an interruption cause and no remaining
corrected invalidity; a failed retry can have both causes.

Use only saved non-Kimi responses, output/call records and the signed interruption
manifest. Kimi remains withdrawn with no performance metrics. Round 1 and the
ManCon test stay unchanged. Update the screen JSON, per-configuration metrics,
report and Italian STATUS, verify invariance of all classification scores and
raw evidence, commit signed on this branch, then stop. No merge or push.
