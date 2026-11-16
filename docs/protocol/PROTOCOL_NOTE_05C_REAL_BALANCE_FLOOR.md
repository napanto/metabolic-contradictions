# 05C funding note — PI decision, real-balance floor USD15

13 November 2026, signed before resumed inference. Antonio's decision in
`CLASSIFIER_SELECTION_BRIEF.md`, “Round 2 decision (2026-11-13, 21:20)”
(signed commit `9da0dbe`), supersedes the previous funding pause and authorizes
resumption **without a top-up**.

Use the real balance from `GET /api/v1/credits`, less worst-case reservations
for calls still in flight **in this process only**. Protect a **USD15** floor.
Conservatively account for this process's charges settling after a balance
snapshot until the next refresh. Do not subtract retained liabilities of the
interrupted calls again: the PI states their billing is already reflected in
the account balance. Those liabilities remain unchanged in the ledger as
reported upper bounds; no unknown per-call cost is fabricated or set to zero.

Mark the earlier USD20 top-up alert as superseded by this signed decision,
preserving its history. This is explicit authorization to resume, not a claim
that funds were added. Any new breach of the USD15 floor still pauses admission
and is reported in STATUS under the existing notification rule.

The eight Kimi configurations remain withdrawn, with no metrics computed or
reported from their partial outputs. Resume the other 26 configurations using
all completed caches and the already-registered single retries of interrupted
batches. Their scientific settings, native output limits, metrics and all
Round 1/v1 artifacts remain unchanged. No ManCon test, merge or push; complete
the requested report, ledger audit and signed delivery, then stop.
