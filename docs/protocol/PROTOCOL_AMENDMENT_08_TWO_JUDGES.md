# Amendment 08: final oracle with two judges, pair task only, through batch APIs

Registered on Monday 16 November 2026 at 01:56 CET, by Antonio's decisions of 00:15–01:50 CET. It precedes every final oracle request, including the pilot. No final oracle output exists for the system: the 14 November Astra pilot used the extraction before its correction and remains historical. Antonio did not inspect any student label, answer or note to take these decisions.

This amendment overrides `PROTOCOL_ADDENDUM_05_ASTRA_ONLY.md` and the sections "Astra reference and current hold" and "No Fable" of `PROTOCOL_AMENDMENT_06_V2_EVALUATION.md` where stated below. Amendment 07 and its addendum A stay in force unchanged.

## Why

The registered design had one judge: Astra, through the Codex CLI. That gives one closed judge, from the same family as the agent that wrote the system prompts, with backend instructions that cannot be inspected, and no measure of agreement between judges. With few student-covered pairs, the agreement between two judges from different families is the most informative check on the oracle. Calling both judges through their APIs removes the Codex scaffolding.

## 1. Judges

- **GPT-6 Astra** (`gpt-6-astra`), through the OpenAI API.
- **Claude Fable 5.2** (`claude-fable-5-2`), through the Anthropic API.

Both judges run at the same reasoning effort, `max`, on every item. There is no per-task or per-judge difference. Maximum output is 128,000 tokens for both judges. A response cut off at that limit is invalid. No tools, no repository and no conversation history are given. Each item is one independent request.

Antonio's stop of 14 November 00:50 on Anthropic requests is lifted only for this run, from his explicit go.

## 2. Scope

- **Items.** The 35 pairs live on the site: the 36 pairs of the v2.1 handoff `c8915e23…`, minus the pair retired as a duplicate.
- **Instrument.** The final site instrument: tag `v2.1-layout-20261115`, commit 61c08a1, export `dfe916b7…`. It is shown in English, with the same check order, fixed labels and texts the site uses. Binding hashes: `configs/v2_oracle_binding_61c08a1.json` (`526e89b3e03062eb1975daba71366cd4846383406214b4d6e372943deed0073c`).
- **Declared deviations from amendment 06**, both moved to future work:
  - the other 65 pairs of sample C are not judged;
  - the extraction task on the 45 C abstracts is not run. This includes the same-abstract comparison with the prototype.

  Extraction correctness is therefore reported only on the 70 sides of the 35 pairs. These claims were selected because they form flagged pairs, so they are not representative of the extraction as a whole.

## 3. What the judges see

The prompt is the site's English instrument: questions, options, help texts, guidelines and path rules, verbatim. Three reductions were approved before any output:
- the abstract-task section is removed from pair calls;
- the JSON is compact;
- duplicated glossary entries are removed.

Each request contains both numbered abstracts, the publication types and the two extracted findings as the site renders them.

The judges never see:
- the system's category;
- any student answer;
- the other judge's answer.

The program derives the category and the factor from the answers with exactly the rules used for a student answer. An answer that breaks a page rule the site enforces is invalid.

## 4. Running

1. **Pilot.** Two pairs, the first two in ascending `sha256("oracle-pilot-20261116:" + item_id)`, the same for both judges. They are sent through the standard synchronous APIs at full price. The pilot checks format, validation and output length. A valid pilot answer whose parameters equal the final ones counts as that item's final answer. Only the transport is recorded as different.
2. **Main run.** The other items go through the batch APIs, at half price: OpenAI Batch for Astra, Anthropic Message Batches for Fable. Each item is a single-request batch, and all are submitted together.
3. **Retries.** Each item has at most one identical retry, sent as soon as its failure is known. An item still invalid after that stays missing and is never imputed. A rejection before generation (rate limit, overload, a server error with no output, a connection error) is not an attempt: it is resent with backoff until accepted, subject to the cutoff below.
4. **Cutoff, 11:00 CET on 16 November 2026.**
   - Batches still open are cancelled.
   - Completed answers are kept.
   - Items without a result are sent at once through the synchronous APIs at full price, with identical parameters. For an item never processed this is its first attempt. Otherwise it is its retry.
5. **Funding.** Antonio confirmed the Anthropic credit. Each request reserves its worst-case cost before dispatch, as before. Final allocations equal the worst case of a full batch:
   - Fable: 115 USD;
   - Astra: 135 USD.

   Each is 35 times the largest worst-case batch reservation of one pair, rounded up to the next 5 USD.

   Expected spending is about 18–39 USD for Fable and 10–20 USD for Astra. Full-price fallback items are admitted only within the remaining allocation.
6. **Release.** Nothing is sent before a signed release that binds this amendment, the binding record and Antonio's explicit go. The release lock of amendment 06 stays.

## 5. Analysis

- **Primary reference.** The student consensus stays the primary human reference (amendment 07, section 4).
- **Reliability rule, per judge.**
  - For each judge separately, compute binary true-flag kappa against the student consensus. Use only the pairs covered at the analysis freeze, with a pair bootstrap interval.
  - Below 0.40, that judge's estimates are reported as unreliable. An undefined kappa is inconclusive.
  - With fewer than 30 covered pairs, the result is called imprecise (amendment 07).
  - The two judges' rules are reported side by side. Neither replaces the other.
- **Agreement between judges.** Report raw agreement and kappa for the binary flag and the category on all 35 pairs, with intervals. Report the same per Q1 check on the 70 sides.
- **Oracle reference.** It exists only when both judges pass the reliability rule. It is then the category on which both judges agree, and the binary flag follows from that category. Where they disagree, the pair is missing, under the pessimistic and optimistic bounds already registered.
- **No adjudication.** Neither Antonio nor Claude settles disagreements. No oracle output changes the instrument, the site, the student rules or the system prompts.
- **Sensitivity.** Each judge alone is reported as a secondary analysis. It never replaces a less favourable primary result.

## 6. The system's label on the 35 pairs, and a classifier comparison

**Which label is the system's.** The v2.1 handoff rebuilt the 36 pairs from the corrected extraction but did not classify them again. Their only label came from GLM-5.3 Flash low on the extraction before its correction. Before any oracle output and before any student label is inspected, the 35 live pairs are classified twice from their v2.1 claims, with the frozen classification prompt and the registered settings:
- once at **low reasoning**, which is the system as registered;
- once at **high reasoning**, a variant proposed by Antonio.

The system's label in every primary analysis is the v2.1 low one. The earlier v2 label is kept as a recorded secondary.

**Secondary comparison, low against high.** It is descriptive only. For each setting, report its binary-flag and category agreement with:
- the student consensus on covered pairs;
- the oracle reference;
- each judge.

Report bootstrap intervals, and the pairs where the two settings differ, with the side each reference takes. All 35 pairs were flagged by the earlier low classifier, so the comparison is conditional on that selection and says nothing about pairs only the high setting would flag.

If high reasoning is adopted, it belongs to the revised system and is declared as such. It never changes the evaluation of the system.

## 7. Unchanged

Everything else stays as registered, in particular:
- the system: GLM-5.3 Flash, high effort for extraction and low for classification;
- the student collection and its rules;
- the instrument split rule for student answers;
- the missing-reference bounds;
- the bootstrap seed.

Regenerating the corpus with the corrected extraction is registered separately, in addendum 08A.
