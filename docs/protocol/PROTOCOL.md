# Preregistered scientific protocol

Version 1 — 2026-11-12. Freeze by signed Git commit before test predictions or inspection of test performance. Subsequent deviations are dated in STATUS.md and disclosed in the report. Public gold labels may be read solely to construct the group-stratified split; no test results guide choices.

## Questions and scope

Can a retrieval-augmented LLM pipeline over PubMed abstracts find and explain inconsistent claims about biomarkers, diet and drugs in metabolic disease, and how precise is it?

- RQ1: inconsistent-class classification on ManConCorpus.
- RQ2: correctness of extracted exposure, outcome, sign, kind, comparator and context.
- RQ3: precision of flagged inconsistencies, overall and by arm.
- RQ4: recovery and explanation of documented control cases.
- RQ5: contribution of retrieval and context over entity matching and sign comparison.

Use the exact three-arm queries and design filter in the specification, humans, 2016–2025, abstracts. Freeze PMID membership, query translation, retrieval dates and checksums. Publication-date partitions are permitted to avoid PubMed's 9,999-result ceiling; validate their union against total counts. Count overlaps explicitly. Index the full union; extraction covers design-eligible studies in control-case, RCT/meta-analysis, then remaining-study order. Clinical validity and whole-corpus recall are outside the estimand.

## Splits and leakage prevention

ManConCorpus: 24 question groups; 8 development, 16 test. Sort stable question IDs, use Python Random(42), draw 10,000 eight-question subsets, and minimize
`abs(dev_pair_share - 1/3) + abs(dev_inconsistent_fraction - overall_fraction) + abs(test_inconsistent_fraction - overall_fraction)`
on all within-question pairs. Reject subsets that separate questions linked by an identical PMID or normalized exact claim text; connected groups must stay together. Break ties lexicographically by sorted IDs. Save IDs and source checksum. Claims from a question stay in one split. All-pair setting has 1,775 pairs; question-aware setting restricts to equal claim type and has 1,040 pairs. Validate totals before evaluation.

Metabolic development: randomly select 10 distinct design-eligible PMIDs per arm with seed 42, assigning overlaps once in arm order biomarker, drug, diet. Save the 30 IDs before prompt development. Exclude these PMIDs from extraction evaluation and exclude every pair involving them from pair evaluation. Controls are purposive case studies, reported separately, not used as random evaluation evidence. All extracted claims from a sampled abstract are evaluated; no selection of only easy claims.

## Models, prompts and selection

Candidate IDs: `z-ai/glm-5.3-flash`, `deepseek/deepseek-v4.1-flash-1017`, `qwen/qwen3.8-flash`, `qwen/qwen3.6-35b-a3b`. Verify availability, prices, supported seed and JSON schema parameters, providers and quantization from catalogue and endpoint listings. Never silently substitute unavailable models. Pin one provider per model with no fallback and required parameters. Temperature 0, seed 42. Extraction reasoning off/minimal; compare off and low reasoning on development for pair classification. If a provider does not implement an option, record unavailable instead of pretending the setting was used.

Use development only to revise prompts and schemas and annotate 30 development abstracts (LLM-assisted development labels, explicitly not human test gold). Archive final prompts, dev labels and selection outputs before test calls. Rank by inconsistent F1 in the without-question development setting; ties use extraction field agreement, then schema validity, then lower measured cost. Select Qwen3.6 if within absolute 0.03 F1 of best and schema validity at least 0.98. Report all four candidate models on both ManConCorpus test settings, subject to documented availability and budget. Selection is done once; no test-directed prompt edits.

Question-aware LLM task uses each claim's yes/no/neutral answer to its PICO question, then pairs equal-type claims: opposite yes/no is inconsistent, otherwise consistent; report neutral and invalid rates. Question-free LLM task classifies each pair directly. Prediction failures remain in denominators and are reported; compute conservative metrics with abstentions counted as errors, plus conditional metrics on valid outputs.

## Baselines and metrics

ManConCorpus primary metric: F1 of inconsistent class; secondary precision, recall, specificity, accuracy, macro F1, and output validity. Majority class determined from dev only. Fixed lexical polarity/negation heuristic, vocabulary frozen in code before test. NLI: `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli`, CPU, read-only existing cache, contradiction score averaged over both input orders. Add question prefix in the question-aware setting. Threshold selected separately per setting on dev from 0.05, 0.10, …, 0.95 by F1; ties nearest 0.5, then lower threshold. No NLI fitting. Report model revision and truncation count.

Confidence intervals: 2,000 bootstrap resamples of **question groups**, seed 42, percentile 2.5/97.5, for ManCon metrics and paired F1 differences (same sampled groups for both models). Questions linked by duplicate PMID/text form a single resampling component. Group dependence precludes treating pairs as independent. Report effective cluster counts and undefined resamples. Comparisons are exploratory; no uncorrected claims of superiority from multiple tests. Xie GPT-4 numbers are literature references over all 1,040 pairs, not directly comparable held-out scores.

## Claims and candidate pairs

Claim schema follows the specification: PMID, arm, exposure/class, outcome, comparator, kind (level/association/effect/risk), sign (+/−/0), significance, hedging, context (population, region, design, n, duration, dose, sample), evidence sentence IDs. A null effect means an explicitly reported absence of association/difference; a nonsignificant directional estimate remains documented as nonsignificant. Null results alone are not direct contradictions. For levels, condition is exposure and biomarker is outcome. No invented context; unknown remains null.

Number source sentences once and retain offsets and sections. Keep only evidence IDs present in eligible results/conclusions. For unstructured abstracts, a deterministic documented sentence-section rule identifies candidate results/conclusions and its coverage is reported. Invalid evidence drops the claim with a reason; schema-invalid responses get one repair attempt. Report pre/post-filter denominators. Link to same-document PubTator identifiers, curated diet vocabulary, drug-to-class map and conservative curated outcome synonyms; embedding suggestions require conservative similarity and keep original labels. Never equate HOMA-IR and all insulin resistance measurements without retaining measurement context.

Use exact normalized exposure/outcome keys and a hybrid retrieval augmentation (sentence BM25 plus abstract-level MedCPT, CPU FAISS, reciprocal rank fusion constant 60). Retrieval top 20 abstracts per claim, excluding same PMID. Retain opposite-sign and effect/null pairs first with deterministic per-key cap 30 plus up to 5 concordant pairs per key for specificity diagnostics. Classify both source evidence and context, output concordant, contradictory, apparent_contradiction, not_replicated, not_comparable, explaining factor and rationale. Factors: population, region, disease subtype, design, comparator, molecule/class, dose, duration, outcome definition, sample, unknown. Descriptive concordant diagnostics are separate from flagged-pair precision.

Metabolic baselines: PubTator opposite positive/negative relations for the same normalized entities (biomarker/drug eligible items only), plain sign comparison, key-only versus key-plus-retrieval candidate generation. Record sample support and coverage of every baseline; absence of PubTator annotations is missing coverage, not evidence of concordance.

## Human evaluation and sampling

Extraction: seed 43, 45 distinct random PMIDs among extracted non-dev abstracts, 15 per arm (same overlap-assignment rule). Score every extracted claim's six fields as correct/incorrect/uncertain with evidence support; report uncertain separately and count it as incorrect for primary strict correctness. Aggregate field correctness with abstract-cluster bootstrap 95% CIs; report per-arm denominators and evidence-check drop rate over the full extraction. This estimates extracted-claim precision, not completeness/recall of extraction.

Flagged pairs: seed 44, 100 unique pairs from predicted contradictory/apparent/not_replicated classes, arm allocation 34 biomarker / 33 drug / 33 diet. Within arm allocate approximately in proportion to predicted class prevalence with minimum 1 for each nonempty class, largest remainder and deterministic ties. Save frame counts, inclusion probabilities and selected IDs. Disjointness is by item, not necessarily by PMID; document dependence. Scarcity is reported, with no invented replacement examples. Annotate against evidence: real contradiction, context-explained, not replicated, extraction error, not comparable (plus concordant false positives). True flag = first three. Show requested Wilson 95% intervals for observed sample precision overall and by arm, explicitly descriptive of the stratified sample. Also report design-weighted corpus-flag precision using known arm/class population counts, with stratified bootstrap interval; discuss repeated PMID dependence and modest sample size. Do not call a Wilson interval on an oversampled stratum population-representative.

Antonio's blind labels are the reference. A fresh second-annotator LLM agent sees only guidelines, source texts and extracted fields, never predictions or Antonio labels. Report raw agreement and Cohen's kappa for pair categories and binary true flag; report kappa's undefined cases honestly. Development labels are separate. No silent adjudication; human revisions are versioned with notes. Prepare Italian guidelines, resumable loopback-only HTML/JSONL interface, keyboard shortcuts, and estimated ~3 hours. Missing human labels produce pending results, never substitute LLM gold.

## Controls, network, retractions and errors

Fixed controls: Du 2016 irisin PMID from inbox; egg PMIDs 26739035 and 32453379; amputation PMIDs 28605608, 32744411, 29938883; heart-failure negative control 29938883 plus matching studies in corpus. Measure staged recovery: documents present, claims extracted, pair generated by each candidate method, classification and explaining factor. A review's opposite subgroup estimates are not automatically between-study contradictions. T1D-only or pre-2016 primary studies outside the query cannot count as missed in-scope retrieval; report scope limitations and never add them silently.

Cross-reference PubMed retraction tags and frozen Retraction Watch CSV by PMID/DOI; distinguish original articles from retraction notices. Evidence graph nodes are exposures/outcomes, edges carry sign-specific unique PMID counts and conflict status. Report node/edge counts, conflict concentration, degree and control subnetwork; this graph represents textual evidence, not biological causality. Error taxonomy: extraction/entity error, reversed relation/sign, incompatible comparator/outcome, missing context, significance/null confusion, review/primary overlap, unsupported explanation, other. Report counts and representative source-backed examples.

## Reproducibility, resource limits and reporting

All report numbers come from versioned results files via a table-building script. Freeze code/prompts before final metabolic annotation scoring. Record source/model revisions, provider quantization, request hashes, costs, seed, runtime, truncation, normalization coverage and corpus scope. Secrets stay in the specified external env file, loaded in memory only. Cache canonical requests and sanitized responses, never authorization headers. Reserve worst-case cost before calls under a locked ledger; hard cumulative cap USD40; missing usage/cost suspends further spending until reconciled. Account retries and failed billable generations. No GPU inference; retrieval/NLI CPU unless a documented schedule threat requires brief non-LLM GPU work.

Prefer full planned samples and extraction. If throughput threatens Saturday evening, apply specification's declared cuts only with dated justification: extraction annotation 45→30, PubTator baseline, third extraction tier. Pair minimum 60 (20/arm) only as explicit contingency, not an unreported convenience. Disclose unavailable models/services. Report at most five pages, English; 10-minute slides start with context and question. Explicit limitations: one human, LLM second annotator, abstracts, cardiovascular benchmark transfer, quantization, query scope, selection dependence, missing full-corpus recall and no claim of clinical truth.
