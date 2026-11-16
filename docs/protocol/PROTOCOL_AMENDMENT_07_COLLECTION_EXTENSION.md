# Amendment 07: extended student collection, coverage-first assignment

Registered on Sunday 15 November 2026 before 18:30 CET, by Antonio's decision. It is prospective for every student read submitted after its registration commit. It overrides the section "Sunday closure and analysis" of `PROTOCOL_AMENDMENT_06_V2_EVALUATION.md` and the Sunday rule of `PROTOCOL_ADDENDUM_05_DECISIONS_B.md` only where stated below. Everything else in amendment 06 stays in force.

## Why

At about 17:40 CET the live site package had 35 pairs (13 biomarker, 12 drug, 10 diet). Only 4 had two valid student reads, 8 had one, 23 had none. No diet pair had any read. The cause is mechanical: at equal priority the site chose the lowest item ID, and item IDs are ordered by arm. In addition, 34 of 43 real reservations were held by sessions with no submission for more than 30 minutes. This decision used coverage counts and reservation counts only. No student label, answer or note was inspected to make it.

## 1. Collection period

Student reads count without the 18:30 limit. Collection stays open until Antonio closes it manually, or until the site has no pair left to assign. The 12:00 and 18:30 database snapshots are kept for audit. They no longer decide anything.

## 2. Analysis freeze

The delivered analysis uses every valid student read present in the final private export. The export time and its file hashes are recorded with the analysis. Reads submitted after the freeze can only enter a separate, declared update. They never silently change the delivered results.

## 3. Coverage and minimum student sample

A pair is covered when it has at least two valid filtered student reads, as before, regardless of agreement. Category consensus still requires a unique majority of at least two votes. The minimum student-only reference sample is 5 covered pairs per arm, 15 in all. Below that, the student-only analysis is reported as descriptive only. Per-arm estimates are always descriptive. Actual covered, unresolved, uncovered and excluded counts are reported in every case.

## 4. Author fallback withdrawn (declared deviation)

At the 12:00 snapshot fewer than 30 pairs were covered, so the registered rule would have the author annotate the uncovered pairs. Antonio will not annotate any pair, so that rule is withdrawn. This is a declared deviation from amendment 06 and addendum 05 B, taken after the 12:00 coverage count was known, and the report must state it as such.

The primary human reference is the student consensus on pairs covered at the freeze. There is no mixed reference and there are no author labels. Uncovered and unresolved pairs are handled with the pessimistic and optimistic missing-reference bounds already registered. The oracle reliability rule, which compares binary true-flag kappa with 0.40, is computed on covered pairs only. With fewer than 30 covered pairs, its result is reported with its bootstrap interval and called imprecise. It is never replaced by a more favourable gate.

## 5. Assignment of real pairs (site change, prospective)

Real pairs are no longer reserved in blocks. The site assigns one real pair at a time, when the participant needs the next one. The "continue or finish" checkpoints stay the same: after the practice pair plus 4 real pairs, then every 5. A participant holds only the pair being read.

A read counts as "done or in progress" if it was submitted and is valid, or if it is the pair currently held by a session with a submission in the last 30 minutes. Each new assignment takes the first available slot in this order:

1. A first read on a pair, in an arm with fewer than 5 pairs that have at least one read done or in progress.
2. A second read, in an arm with fewer than 5 pairs that have two reads done or in progress. Pairs that already have one valid read come first.
3. Third and fourth reads required by the protocol. A third read follows a nominal disagreement between the first two reads. A fourth read follows a three-way split, at most 8 pairs.
4. Everything else: first the second read of pairs with a single read, then first reads of new pairs.

Ties are broken in this order:
- the arm furthest behind on the measure of the current step;
- then, within an arm, the pair with more valid reads;
- then a pseudo-random order fixed now, ascending `sha256("metabolic-evidence-coverage-20261115:" + item_id)`;
- then the lowest slot.

A held pair is released if its session has submitted nothing for 30 minutes (`STALE_MINUTES`). An answer to a released pair is accepted. It is paid if it arrives within 2 hours of the pair being received (`LATE_PAYMENT_SECONDS`). It also counts as a read if its slot is still unfilled; otherwise it is a paid surplus read that does not count.

The following stay as they were:
- no reader gets the same pair twice, or a superseded version of a pair already read;
- at most 16 replacement reads;
- at most 8 pairs with a fourth read;
- quality control, eligibility and one participation per person;
- the frozen instrument and the signed configuration snapshot.

The participant rules on the site are updated to describe this.

## 6. Related records

The payment change of the same day (3.00 EUR per real pair from 17:00, the practice pair always 1.00 EUR, no budget cap) is recorded in `configs/v2_human_collection_lock_amendment_pay.json`. The site tag that implements section 5, with its deploy time, is added to the deploy log when it is live. Section 5 applies from that moment; sections 1 to 4 apply from this registration.

## Implementation record (added after deploy, 15 November 2026)

- **Section 5 deploy.** Section 5 went live with site tag `v2.1-onepair-20261115` (commit 3d5a8ff), reloaded at 18:12:43 CET. At 18:12:48, `release-blocks` returned to the pool 34 unopened reservations and 9 open pairs held by inactive sessions. The 3 open pairs of active sessions were kept. Practice pairs were not touched.
- **Earlier version withdrawn.** A first, narrower version (tag `v2.1-stale-20261115`) was live from 17:50:01 to 17:56:01, then rolled back. It made no assignment and released nothing.
- **Inactivity in practice.** A session counts as inactive only if it has had neither a submission nor any other recorded action for 30 minutes. This protects participants more than the text of section 5.
- **Late answers.** An answer to a released pair whose slot is still unfilled is counted and paid even after 2 hours. This is the existing payment code, unchanged. The 2-hour guarantee in section 5 is a minimum, not a cut-off.
- **How the order is applied:**
  - In steps 2 and 4, pairs with a valid read come before the arm tie-break.
  - The step 3 tie-break uses the arm's pairs that have two reads.
  - A participant without a practice pair is admitted only if at least `MIN_PAYOUT_PAIRS` distinct free pairs exist.
- **Payment amendment.** It is recorded in `configs/v2_human_collection_lock_amendment_pay.json` (commit d93ef68).

## Addendum A: third and fourth reads until 5 resolved pairs per arm (15 November 2026, before 19:00 CET)

Antonio's decision, registered before its site change. It is prospective from the deploy of that change, which is added to the deploy log. It was decided on aggregate counts only. At 18:47:
- 3 pairs were resolved;
- 3 of the 6 pairs with two valid reads were waiting for a third read after a nominal disagreement;
- no fourth read had been granted.

No student label, answer or note was inspected.

Aim: the assignment order works towards 5 **resolved** pairs per arm, 15 in all. A resolved pair has a category consensus, as already defined: at least 2 valid votes for one category and more than for any other category.

1. **Fourth reads.** The cap of 8 pairs with a fourth read is withdrawn. Every pair whose three valid reads give three different categories receives a fourth read.
2. **Ceiling of reads.** There is no fifth read. A pair with no consensus after four reads stays unresolved and is reported as such.
3. **Step 2.** In the measure of step 2 of section 5 (pairs with two reads done or in progress), a pair that is finally unresolved after four reads no longer counts. If such a pair leaves an arm with fewer than 5 counted pairs, step 2 resumes for that arm with the same tie-breaks. Steps 2 and 3 therefore continue until each arm has 5 resolved pairs, or until its pairs run out.
4. **Unchanged.** The order of the steps and the tie-breaks stay as in section 5. So do the cap of 16 replacement reads and the definition of a covered pair. Section 3, the minimum student sample of 5 covered pairs per arm, is unchanged. Resolved, unresolved and covered counts are all reported, together with the number of reads each resolved pair needed.

**Implementation record of addendum A.**
- **Deploy.** Site tag `v2.1-fourth-20261115` (commit e8c8a92) was reloaded at 18:57:08 CET, after 258 s with no session action. The configuration snapshot is unchanged (65c3f425…).
- **Activation.** The cap was withdrawn at 18:57:10 by the audited `withdraw-fourth-cap` command (metadata key `fourth_cap_withdrawn`). Addendum A applies from that instant.
- **Code.** The assignment order and the public completion meter read the same pair state, so every fourth read the order requires also counts as a required read.
- **Counts at activation:** 3 resolved pairs, 3 pairs pending a third read, no fourth read granted.
