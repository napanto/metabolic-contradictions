# Operational price ceilings for the unchanged classifier grid

13 November 2026, before resuming paid calls and before any selection/test.
The 13 configurations, claims, prompt/schema, seeds, temperature, token caps,
validity rule, bootstrap and selection rule remain fixed. No F1 has been
computed or inspected. This is an operational accounting/routing update in the
same spirit as the existing v1 routing-price amendment 03, not a new model grid.

Two generation-free routing rejections show that exact live quotes are too
fragile as lasting routing ceilings. Morph/GLM first increased from
USD0.107/0.113 to USD0.116/0.122 per million input/output tokens; the latest
snapshot has returned to the original quote. PrimeIntellect/Qwen3.6 then changed
its output quote from USD0.112 to USD0.113 per million. The second HTTP404 stopped
new requests; active requests drain normally. No completed prediction is retried.

Freeze `configs/classifier_selection_v2_runtime_prices.json` and its freshly
downloaded endpoint snapshots. Every provider was already declared in the base
or Antonio's correction manifest; all 27 route identities, quantizations,
required parameters and active statuses match. No new provider is added.

For each declared endpoint, retain its current published uncached input/output
and request quote separately from fixed routing ceilings of **twice those
verified quotes**. Zero request fees retain a zero ceiling. Apply the ceilings
to OpenRouter `provider.max_price` and to the worst-case reservation made before
every call. Project remaining costs using these same ceilings and the existing
50% usage margin. Current quotes determine cheapest-first ordering, retaining
PrimeIntellect first for Qwen3.6; the per-provider concurrency/backoff policy is
unchanged. A further increase beyond these frozen ceilings still stops spending.

The evidence-checked runtime price configuration supersedes price-only endpoint
disables when the current quote is within the new bound. Thus Morph/GLM can
resume within the same originally declared fp8 set. A change in identity,
quantization, required parameters or active status still excludes an endpoint.
Qwen3.8 remains exclusively on Alibaba/us-east-1, quantization unknown.

The base and correction manifests stay byte-for-byte unchanged. Runtime pricing
is applied separately, preserving all old request/cache identities and old
responses with their actual original costs. Retain failed attempts and routing
history. The permanent interrupted-call liability and its single authorized
retry are unaffected. This update authorizes no additional response-loss retry.

Antonio's USD30 funding-alert rule remains in force, with `cap: null`; if the
revised full-grid plus test projection crosses the alert, pause and request the
specified top-up without removing configurations or lowering reasoning. Freeze
this runtime configuration, addendum and source hashes with selection before
the sole exploratory test. All prior signatures and scientific results remain
preserved; no history is rewritten.
