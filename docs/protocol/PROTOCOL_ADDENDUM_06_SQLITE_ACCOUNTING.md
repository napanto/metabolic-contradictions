# Indexed accounting without changing the GLM system

14 November2026. This operational revision removes a measured accounting bottleneck while preserving the registered production and reference designs. It is authorized within the requested complete project execution and follows Antonio’s03:38 whole-balance and04:15 uncertain-cost instructions.

The previous ledger rewrote its full JSON history for each credit-age check, reservation and settlement. Synthetic benchmarks measured1.36seconds/request at6107 historical calls,4.61seconds at20000 and11.50seconds at50000, before inference. The reviewed SQLite implementation measured about9.6milliseconds/request at50000 entries using WAL and synchronousFULL. This is an accounting benchmark, not a model-quality result.

The new ledger is injected into the existing V2Client and unchanged extraction/classification controllers. Model, effort, output caps, temperature/seed, prompt/schema bytes, source validation, route-selection rules, provider limits, retry semantics, source F, checkpoints and scientific production binding remain unchanged. Scheduling timing can improve; actual provider routes remain recorded. The original configuration stays SHA256 afff5c500863af50d1a40fa18af7c9a4d81b297f4bea5eae7adf8f78de8245ce.

Before migration, pause new admissions for maintenance, drain all HTTP requests and stop every old JSON writer/controller. Acquire the production and JSON-ledger locks. Verify and archive the exact original JSON bytes/hash; import all IDs, ordered calls, pending/uncertain liabilities, credit history and audit fields once. Publish the database and an immutable migration proof without overwriting an existing database. Migration makes no model request, settles no uncertainty and invents no cost or source result.

A signed backend authority binds the drained input hash, immutable evaluation-config hash, database/export paths, this protocol and the exact new ledger/runner plus activated constructor/usage-guard hashes. The legacy JSON-ledger constructor rejects that marker, preventing later legacy writers between runs or after a crash. The SQLite runner also holds the old JSON lock throughout its lifetime and detects unrecognized export drift. Already loaded old objects are stopped before cutover; a marker cannot revoke them retroactively.

Reservations and settlement use BEGIN IMMEDIATE transactions and incremental Decimal totals. There is no floor, buffer or phase/account threshold below the real balance. Per-request worst-case reservations remain; known over-reservation charges remain fatal. Missing/invalid cost and usage metadata remain unknown, never zero, with raw responses preserved. The04:15 rule continues: uncertain cost alone does not stop production; report a rate above approximately5% while continuing. Indexed request and observed-response hashes preserve exact cache accounting.

The compatible JSON file is an audit export, atomically refreshed periodically and at phase end rather than rewritten per request. Export-intent/checksum records distinguish a recoverable process interruption from a foreign rewrite. Full snapshots reconcile incremental totals with the preserved records. The new backend does not authorize inference or alter an existing scientific record.

The small metadata guards in finish() and _usage() treat non-object usage/details and invalid cost scalars as missing financial information. They do not alter model responses, extracted values or semantic validation. Signed original runtime bytes and exact approved replacements remain in the operational companion.

Independent review and39 synthetic tests cover trace equivalence, exact import, thread/process no-overspend, full-balance use, pause/price/unknown/cache behavior, restart, duplicate settlement, export-intent failures before/after rename, protected writer activation and both controller injections. No real API call or operational migration occurred during those tests. Benchmark and review artifacts accompany this revision.

The maintenance admission pause is cleared after verified migration. Full F resumes from the same checkpoints without repeating completed outputs. Classification still requires a cost projection on its actual complete frame. The separate explicit-Antonio Astra release and human-collection locks remain unchanged.

## Bounded drain and total request deadline

Antonio’s additional INBOX06:35 instruction bounds the maintenance drain. At actual cancellation time06:45:29CET only one request remained; the other straggler had finished. PMID31515744 attempt1 was cancelled, its original bytes archived and its full0.141418872USD reservation retained as uncertain. Exact wire-hash reconstruction pins the one remaining attempt2 to near-ai/fp8. No response, generation, usage or zero cost is invented, and the source stays in F.

Prospectively, the new runner enforces a **480-second total wall-clock deadline** on each model HTTP request, including reading its complete response body. Keep-alive comments cannot extend this timer. Expiry follows the existing uncertain-liability and one-identical-retry path, with the same model, messages, schema, parameters, provider and request hash. No partial response is accepted or repaired. This is an explicit transport deadline, not a change to the model token cap, reasoning effort or scientific source population. Astra remains held.
