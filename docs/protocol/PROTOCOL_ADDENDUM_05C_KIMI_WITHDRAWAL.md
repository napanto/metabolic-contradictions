# Addendum 05C — PI withdrawal of Kimi and interrupted-run recovery

13 November 2026. Signed before resumed inference. Antonio's instruction in
`CLASSIFIER_SELECTION_BRIEF.md`, “Round 2 change (2026-11-13, 21:10)” (signed
commit `cfb6fbf`), withdraws **both Kimi models and all eight settings** for time
reasons, before any Kimi classification metric was computed or seen.

Record each as **“ritirato (decisione di Antonio, tempi)”**. Do not compute or
report F1, F0.5, precision, recall, confidence intervals or other performance
metrics from partial Kimi outputs. Exclude them from ranking. Keep their raw
cache unchanged and retain incurred charges in the ledger, separately from
the performance table. Withdrawal is the PI's decision, not a low result.

The other **26 configurations**, their native token limits, prompt, schema,
data, decoding, routes and scoring rules remain unchanged. Mistral/none's
documented provider rejection remains an availability finding. The original
manifest stays byte-for-byte unchanged; the signed overlay
`configs/mancon_screen_withdrawal.json` excludes Kimi before jobs, cache restore,
or scoring, preserving every existing request identity for the other models.

The interrupted process has exited. **46 non-Kimi in-flight requests** have no
saved response. Their aggregate worst-case reservation is **USD61.047763188431254**.
The exact reservations, hashes, batches and pairs are frozen in
`results/mancon_screen_interruption_recovery.json`; the pre-recovery ledger and
read-only credit snapshot are retained. Settle a pending call only if its genuine
saved response supplies its cost. Otherwise move the complete reservation to
retained liability, with actual cost and usage unknown. Do not invent zero
charges or infer a per-call cost from shared-account differences.

Antonio explicitly requested resumption of unfinished work. Permit **one guarded
operational retry per identified interrupted non-Kimi batch**, with identical
scientific input/configuration and an allowed route. Preserve the original start
record and liability; known-free rejections do not consume that one billable
retry. No completed generation is repeated. Retried pairs retain first-generation
invalidity under the existing conservative validity gate; report recovered final
validity and interruption counts separately so this is not mistaken for a model
schema failure. Cost tie-breaks use actual cost plus retained liability where
the original charge is unknown; report both components distinctly.

The USD25 shared-account floor remains mandatory. The observed balance of
USD85.820119623 minus retained liability leaves USD24.772356434568746 before any
new reservation. Therefore new inference must await a confirmed top-up under
the existing rule. Record **“Serve da Antonio: ricarica OpenRouter di circa 20 USD”**
in STATUS: approximately USD5.76 expected remaining cost, native-limit reservation
headroom, and concurrent-account activity motivate this request. This is a reserve,
not a claim that USD61.05 was actually billed. Prepare the restart while paused;
resume only once the brief confirms funding and live credit satisfies the floor.

Keep the Italian table current as the remaining configurations finish; Kimi rows
remain withdrawn with no performance metrics. Deliver the screen JSON, short
report section, ledger/audit and signed commits on this branch. No ManCon test,
production selection, merge, push, or modification of `../project`; then stop.
