# Addendum 08E: claim-level semantic pairing

Status: registered on Monday 16 November 2026 at 07:35 CET, after 08D froze (06:31:53 UTC) and before any 08E classification. The draft was revised after Fable's review (section 6 of that review, all changes applied); the draft file stays as it was. The counts per τ (volumes, costs, overlap) were known when these rules were written. B, c and τ* were computed by the rule of §5 at 07:32 CET. No classification of any new pair was known. Antonio was asleep and had delegated the decisions («D'ora in poi prendi tu le scelte perché vado a dormire»). Offline counts: `results/v2/claim_pairing/counts_08E.json`. Literature: `chl/papers/inbox-2026-11-16/pairing-methods/NOTE_metodi.md`, cited as [key, page].

Authority, quoted:
- «dato che sembra facile, vai anche con i metodi nuovi, e se le coppie aumentano di un ordine di grandezza anche dopo il filtro, non fare la classificazione con GLM e lo mandiamo ai lavori futuri, però il resto ci sta» (about 04:20);
- «puoi classificare le nuove coppie se rimani dentro il budget di 75 dollari disponibili su openrouter» (about 04:30);
- «Va bene il campione» (about 04:50), approving step (3) of §4.

## 1. Why

The registered routes find pairs only when the normalised keys match, or when the abstracts are near retrieval neighbours:
- 08A: key matching plus hybrid retrieval;
- 08D: the same routes without caps, 42,972 pairs, of which 11,391 are opposite or effect versus null.

Synonyms that the lexicon and PubTator do not map, and measures outside PubTator's six entity types, can stay unpaired.

Route (a) is added. **The registered routes stay**, because a union of dense and non-neural blocking gains 8.5–14.5 recall points over either alone [thirumuruganathan2021, p. 2470].

## 2. Route (a): semantic matching on strings, joined on claims

- **Eligible claims.** As in 08A: not hedged, identifiable comparison type (C), and a sign in {+, −, 0}.
- **Texts.** Each claim gives its extracted exposure string (E) and outcome string (O), lower-cased, whitespace collapsed. Direction and significance are not embedded [sahoo2026, PDF p. 1; sparsecl2024, PDF p. 2].
- **Encoders.** Two encoders are run:
  - the MedCPT query encoder;
  - SapBERT (cambridgeltl/SapBERT-from-PubMedBERT-fulltext, revision 090663c3ae57bf35ffe4d0d468a2a88d03051a4d, file sha256s pinned in `chl_rag.claim_pairing.SAPBERT`, stored in the project model cache).

  Both use the CLS vector, L2-normalised, max length 64.
- **Choice of the primary encoder.** Each encoder proposes its K most similar new T1+T2 pairs, ranked by the smaller of the two cosines, with K = ⌊(B − 1,500c)/c⌋. The primary encoder is the one that, at that volume, recovers more key-route T1+T2 pairs with at least one non-identical string.
  - If the difference is below 2 points, SapBERT.
  - The other encoder is reported with counts only.
  - No third encoder tonight.
  - Declared bias: the criterion favours SapBERT, because the keys are MeSH-based IDs.
  - Thresholds do not transfer between encoders. The budget rule of §5 recomputes τ* for the primary encoder.
- **Search, exact.** Within each (domain, C) partition, the unique (E, O) string pairs are indexed in FAISS `IndexFlatIP` on the concatenated unit vectors. `range_search` runs at radius 2τ − 1e-4, which returns a superset of the string pairs whose two cosines both reach τ; each is then checked exactly. There is no top-k cut-off. Claim pairs follow by a join: E ~ E′, O ~ O′, C compatible under the registered 08A rules, different PMID.
- **Stoplist,** for this route only: placebo, control, controls, control group, usual care, patients, participants, intervention, treatment, therapy, diet, standard care [marshall2020, p. 1907; rosemblat2019]. A claim is excluded only if its whole normalised E string equals an entry.
- **Classifier output.** The frozen classifier has a "not comparable" output, which is where a fuzzy pair that does not ask the same question should land. The classifier has not been validated on fuzzy pairs.

## 3. Route (b): specified, NOT registered, not run tonight (deferred to 08F)

1. **Linking.** Residual E and O strings without a PubTator ID are linked to MeSH by SapBERT nearest neighbour after abbreviation expansion [kartchner2023, pp. 14467, 14470].
2. **τ_link.** The smallest value at which at least 95% of accepted top-1 links equal the PubTator ID. Coverage is reported. 100 accepted residual links are checked by hand before use.
3. **Pairing.** Same MeSH E and O. The MeSH parent/child relation forms a lower-priority route [nye2020; marshall2020].
4. **Drug classes.** MeSH Pharmacological Action and RxClass ATC level 4.

Open resources only; UMLS, RxNorm in full, LOINC and SNOMED CT stay future work. None of this enters the 08E order.

## 4. Order of classification

1. **1,500 new T3 (concordant) pairs at τ*,** a seeded simple random sample (ascending sha256(pair_id)), classified first.
2. **All new T1 pairs, then all new T2 pairs at τ*,** in ascending sha256(pair_id).
3. **T1+T2 pairs of the next lower grid band,** in ascending sha256(pair_id), until B is reached.
   - Ascending sha256 makes any truncation a seeded random sample of a declared stratum.
   - Approved by Antonio: «Va bene il campione» (16 November 2026, about 04:50 CET).

No route tier, no NLI and no similarity ordering. NLI may be computed afterwards as a covariate only.

## 5. τ*: a budget parameter

- **Rule.** τ* is the smallest grid value, in {0.80, 0.85, 0.88, 0.90, 0.92, 0.94, 0.96, 0.98}, at which all new T1+T2 pairs plus the 1,500 controls cost at most B at the recomputed cost per pair c.
  - It is a budget parameter, not a calibrated threshold.
  - The counts per τ were known when it was written. No classification was known.
- **B,** registered as a formula: B = min(fresh OpenRouter balance after 08D finishes, 75 USD − the OpenRouter spend since about 04:30 CET on the ledger) − 3 USD.
- **c.** The mean cost per classification call at high since the 08A corpus classification began (03:15 UTC), on the ledger, at the moment 08D finishes. It was 0.00107 per call at 04:45.
- **Where the values come from.** The τ* table per B and c is in the counts file. τ*, B and c are recorded together before any 08E call.
- **Overlap with the key route** is a diagnostic only. It is reported per τ and split by cause: stoplist, class↔member, C mismatch, failure on E only, on O only, on both. It is not recall.

**Offline results** (counts_08E.json, known before registration; no classification known):
- **Primary encoder: MedCPT.** At budget-matched volume it recovers 48–60% of the 6,099 key-route T1+T2 pairs with a non-identical string, against 21–24% for SapBERT. The gap is well over 2 points at every B and c. SapBERT's cosines are lower, so at B ≥ 25 even its 0.80 floor gives fewer than K pairs.
- **τ\* per B,** MedCPT, at c = 0.00108 / 0.00111. The band below τ\* is sampled per §4 step 3.

| B (USD) | τ* | New T1+T2 at τ* | Cost incl. 1,500 controls | Band sampled |
|---|---|---|---|---|
| 15 | 0.92 | 10,560 | 13.0 / 13.4 | [0.90, 0.92): 1,828 / 1,453 of 8,425 |
| 20 | 0.92 | 10,560 | 13.0 / 13.4 | [0.90, 0.92): 6,458 / 5,958 of 8,425 |
| 25 | 0.90 | 18,985 | 22.1 / 22.7 | [0.88, 0.90): 2,663 / 2,037 of 11,113 |
| 30 | 0.90 | 18,985 | 22.1 / 22.7 | [0.88, 0.90): 7,292 / 6,542 of 11,113 |

- **10× checks at 0.90:** T1+T2 1.67× and all signs 1.48× (at 0.92: 0.93× and 0.84×). Both pass. At 0.80 T1+T2 would be 17.2×, which fails.
- **Overlap at 0.90,** out of 7,541 key-route T1+T2 pairs:
  - recovered: 4,706;
  - not recovered, by cause: stoplist 57, class↔member 1,448, C mismatch 0, failure on E only 883, on O only 369, on both 78.

- **Sanity print,** reported descriptively and never changing τ*: 30 pairs at ≥ τ* and 30 from the sampled band, showing E and O strings only.

### Values at registration (computed at 07:32 CET, `results/v2/production_v21_08e/budget_tau_star.json`, sha256 e2ad90c1…)

- **Fresh balance** after 08D: 33.4377 USD (one `GET /api/v1/credits`, no model call).
- **Spend since 04:30 CET** on the ledger: 37.0317 USD, so 75 − spend = 37.97 USD. The balance binds.
- **B = 30.4377 USD** (33.4377 − 3).
- **c = 0.000938 USD per call:** 42.4039 USD over 45,224 classification calls started since 03:15 UTC, as defined above.
  - The rule speaks of a cost per pair. The definition, written earlier with «0.00107 per call», counts calls. Retries and zero-cost rejections make them differ: the same spend over the 42,972 pairs of 08A and 08D gives 0.000987 per pair.
  - At registration both values were known. With 0.000987, τ* would be 0.90 (0.88 would cost 31.18 > B).
  - **The definition is applied as written.** Changing it after seeing the result would be a choice made on the result.
  - The practical difference is small. The stop on actual spend governs both readings: under 0.90, the band [0.88, 0.90) would have been classified almost entirely as a sample.
- **τ\* = 0.88.**
  - New T1+T2 at 0.88: 30,098 (T1 9,800, T2 20,298). With the 1,500 controls, 31,598 pairs cost 29.63 USD ≤ B.
  - At 0.85 they would cost 59.46 USD.
- **10× checks at 0.88:** new T1+T2 30,098 against 11,391 (2.64×), new pairs of all signs 97,073 against 42,972 (2.26×). Both pass.
- **Ordered list:** `data/derived/v2/production_v21_08e/pairs_08e_tau088.jsonl`, sha256 87e2dd5c5b9ad057803966a9c2843ad2e604fcac508252001cfc8df085baf1a5, 63,400 pairs. Manifest: `results/v2/claim_pairing/pair_lists_manifest_tau088.json`.
  - (1) 1,500 T3 controls; (2a) 9,800 T1; (2b) 20,298 T2; (3) band [0.85, 0.88): 31,802 T1+T2.
  - Built against the frozen 08D frame (sha256 ff181a61…) with the registered builder (`scripts/build_08e_pair_lists.py --candidates 0.88:0.85`).
  - The band has 10 pairs fewer than the counts file (31,812): the builder's 1e-6 tolerance at the band edges. This is noted, not corrected.

## 6. Recall: future work

Tonight only the overlap between routes is reported. The proper estimate:
- declare a universe U;
- take the "comparable pair" as the positive class;
- stratify the non-proposed pairs of U by similarity bands, plus a random stratum [marchant2017];
- allocate 20:80 between proposed and non-proposed [webber2013, p. 27];
- report a beta-binomial interval [webber2013, p. 3];
- run the diagnostic of the 200 most similar non-proposed pairs first [thirumuruganathan2021, p. 2469].

It needs annotators.

## 7. Decision rules (Antonio), to be registered

1. **Order of magnitude, checked like for like at τ*.**
   - New T1+T2 against 11,391: the ceiling is 113,910.
   - New pairs of all signs against 42,972: the ceiling is 429,720.
   - If either count reaches its ceiling, there is no 08E classification, and the route is reported with counts and goes to future work.
   - Both counts are recorded before classification.
2. **Budget.** The 75 USD of Antonio's message covers the rest of the 08A classification, 08D and 08E.
   - B already includes the 3 USD margin.
   - 08E follows §4. A new pair is admitted only if: actual ledger spend of 08E + pairs in flight × the 95th-percentile cost per pair of 08D (retries included) + the mean cost per pair of 08D ≤ B.
   - The ledger's worst-case reservations (about 0.10 USD per call) are not used for this gate. Account safety stays with the ledger's own real-balance check (floor 0).
   - The actual spend is re-read from the ledger at least every 500 admitted pairs.
   - After the first refusal no later pair is admitted, and calls in flight finish. The classified set is therefore a prefix of the ordered list.
   - **Time cutoff.** Admission also stops at **11:00 CET** on 16 November, whichever comes first.
     - The real-balance policy limits calls in flight to about balance/0.10. Spend therefore decays roughly exponentially, and reaching B would take until about 13:00 CET, too late for the delivery that afternoon.
     - With ascending sha256 order inside each step, a cut by time leaves a seeded random sample of the step it falls in.
   - **Disk guard.** Admission also stops if free space on /var falls below 800 MB.
3. **Classification settings.** As in 08D: GLM-5.3 Flash high (08C), the frozen prompt, fp8 routes, the retry and rejection rules, and the effort is never lowered.

## 8. What the report must say

> The new routes propose additional candidate pairs, not established contradictions. At τ* = … the semantic route adds … opposite or effect-versus-null pairs absent from the 08D frame. All those at ≥ τ* were classified, plus a random sample of … out of … in the band [ … ) and 1,500 concordant controls; the remaining … are unclassified. τ* was set by the budget, with the counts known and before any classification; it is not calibrated on human judgements, and "same question" precision is not measured. The recovery of key-route pairs is an overlap between routes, not a recall. Recall remains unmeasured: unextracted claims, below-threshold pairs and unidentifiable comparisons stay out. The labels are the classifier's, which is not validated on fuzzy pairs.

Exact shares are allowed in the censused stratum, and shares with intervals in the sampled band. No extrapolation below the bands.

If B or the time cutoff falls before the end of step 2, the wording changes accordingly. The report then states:
- which steps were complete;
- the step that was cut, with how many of its pairs were classified (a seeded random sample of that step);
- that the pairs after the cut are unclassified.

Shares in the cut step carry intervals.
