# Final human analysis of the pair study, with the two-judge oracle comparison

Module `chl_rag.human_analysis`. It is offline only and never calls a model or the network. It was built and tested on SYNTHETIC data only. The fixture in `tests/fixtures/synthetic_study/` comes from the site's own code at the tag of the final instrument, run by `scripts/build_synthetic_fixture.py` on a temporary database with invented sessions. No production database, export or participant answer was read.

## Command at the freeze

```bash
PYTHONPATH=src uv run python -m chl_rag.human_analysis \
  --export <private export directory> \
  --oracle astra=<astra run directory or rows.jsonl> --oracle fable=<fable run directory or rows.jsonl> \
  --assignment-started <started.jsonl> \
  --classification low=data/derived/study_pair_classification/low \
  --classification high=data/derived/study_pair_classification/high \
  --sampling data/derived/human_subset_sampling_private.json \
  --output results/human_analysis.json \
  --private-dir data/raw/human_private \
  --macros report/human_macros.tex
```

The `--oracle` and `--classification` options are optional. Without them, those sections report as pending. The run fails closed with a field name and no row content if any of these does not hold:
- the export bytes, hashes or derivations;
- the package;
- the configuration hash in `configs/human_analysis_binding.json`;
- an oracle or classification record.

## Oracle input, one JSONL row per item

Each row is `{"judge", "item_id", "status": "valid"|"invalid", "annotation"}`. For a valid row, `annotation` is the record of `site_instrument.annotation_from_reference_response`. For an invalid row it is null. An item absent from the file is missing.

The analysis re-derives every label from the path, using the site validator. That validator is AST-identical in sites 5208dfa and 61c08a1.

`oracle_rows_from_run` adapts the two-judge runner's output directory, `<root>/items/<item_id>/result.json` from `chl_rag.reference_batches.TwoJudgeRun`, into these rows. A directory passed to `--oracle` goes through this adapter.
- A valid result gives its decoded `answer`.
- Any other final status gives an invalid row.
- An item without a final result is missing.

The runner's status counts are reported. The adapter is tested on the runner's fake-transport outputs, in `tests/fixtures/synthetic_oracle_run/`.

## What it computes

**Populations:**
- **Primary:** counted, paid, not surplus, not excluded, not quality-flagged, and the session is eligible.
- **Sensitivities:**
  - the original answers, without participant corrections;
  - all accepted reads: paid, not surplus, eligible.
- **Corrections:** the primary population applies the applied entry of each chain, revalidated against the instrument.

**Pairs (amendment 07, addendum A):**
- covered means at least 2 reads;
- consensus is a unique majority of at least 2;
- states are resolved, pending third, pending fourth, unresolved after four, or uncovered;
- there is no fifth read: the primary population is capped at 4 and the cap is counted;
- it also reports the reads each resolved pair needed, and the minimum of 5 covered pairs per arm, below which results are descriptive only.

**Students:**
- human–human raw agreement and alpha on the first two reads;
- consensus categories;
- precision of system-flagged pairs, with missing-reference bounds, overall and per arm (per arm always descriptive);
- agreement of the system with the consensus;
- strict Q1 per-check correctness on the 70 sides, declared non-representative.

**System label:**
- **Primary:** the system's label (low reasoning).
- **System failures:** an invalid label counts as a system failure. It is reported, never imputed, and left out of precision denominators. Analyses that do not use the system label keep the pair.
- **Secondary:** the label from before the extraction correction (the stratum of the superseded pair), with design-weighted precision.

**Judges (draft amendment 08, section 5):**
- validity counts;
- per-judge kappa against the consensus, with the 0.40 rule, "inconclusive" when undefined, and "imprecise" below 30 covered;
- agreement with individual reads;
- inter-judge agreement on pairs and per Q1 check;
- the oracle reference, only when both judges pass;
- each judge alone, as a secondary analysis.

**Low/high:**
- agreement of each setting with every reference;
- pairs that differ, and the side each reference takes;
- the note that the comparison is conditional on the selection.

**Instrument versions:**
- each read is placed in a segment of the binding timeline, using the registered split rule;
- ambiguous reads are settled from the A1/A2 option sets where possible, otherwise flagged;
- Q1 answers are counted before and after the layout deploy.

## Choices where the registered text is ambiguous

1. **Oracle reference.** It uses the category on which both judges agree. Draft amendment 08 says "the label on which both judges agree". A binary-only agreement variant is not computed, so the amendment text should say "category".
2. **Student reference for a Q1 check.** It uses the same rule as for categories, a unique majority of at least 2 reads per side and check. "Cannot tell" counts as not correct (strict).
3. **Split rule.** The export carries `assigned_at_utc` (assignments.created), not `started`.
   - **With the sidecar.** The optional `--assignment-started` sidecar (JSONL `{assignment_id, started_utc}`, metadata only, read-only from the database at the freeze) applies the registered rule exactly. The sidecar must cover every exported read and satisfy assigned ≤ started ≤ submitted.
   - **Without the sidecar.** Assignment is a lower bound of `started`, so ambiguous counts are upper bounds.
   - **Reload instants.** All of them are exact to the second, from the service journal.
   - **Other reloads.** No other reload changed the instrument export. `scripts/verify_instrument_timeline.py` checked all 16 other site tags against the export hash and the site deploy log, and its result is recorded in the binding.
4. **Kappa against the students.** Only resolved pairs have a consensus label, so the kappa uses resolved pairs. The "imprecise" threshold uses the covered count.
5. **Seed.** The bootstrap seed is 20261124, the final bootstrap of amendment 06. The older draft value 20261117 is not used.
6. **Pending eligibility.** Sessions whose eligibility is pending are excluded from both populations.

## Addendum 08B: validity on coded answers

`scripts/apply_oracle_revalidation.py` (module `chl_rag.oracle_revalidation`) re-validates the saved answers of the two-judge run without any new request. It writes `annotation/oracles_two_judges/<judge>/max/two_judge_final_08b.jsonl` and a manifest next to the run, and leaves the existing files untouched.

Each record holds:
- the attempt used and the annotation derived from the coded answers;
- `free_text_as_returned`, the exact original texts;
- the validation changes, which are the removal of text where the page shows no box and the cut to 200 characters in the 'other' box;
- the strict status under the original rule;
- the addendum sha256.

Rule 3 is applied as written, following the addendum's Correction of 03:02 CET: the first attempt valid under 08B is final for every item. Where that differs from the attempt that was final under the original rule (five items: Astra 1, Fable 4), the originally valid attempt is recorded as the alternative in `literal_rule_attempt`, together with its raw-output sha256. Items whose final attempt is unchanged keep their annotation byte for byte.

When `--oracle` points at a run directory, the analysis uses the 08B outputs as primary and the original strict results as the sensitivity analysis, `oracle_sensitivity_strict`. Explicit strict rows can be supplied with `--oracle-strict judge=PATH`.

**Macros, primary under 08B (already defined):**
- `\HAAstraValid`, `\HAFableValid`
- `\HAAstraKappa`, `\HAFableKappa`, `\HAAstraKappaCI`, `\HAFableKappaCI`
- `\HAAstraReliability`, `\HAFableReliability`
- `\HAAstraComparedPairs`, `\HAFableComparedPairs`
- `\HAInterJudgePairs`, `\HAInterJudgeCategoryAgreement`
- `\HAInterJudgeBinaryKappa`, `\HAInterJudgeBinaryKappaCI`
- `\HAInterJudgeCategoryKappa`, `\HAInterJudgeCategoryKappaCI`
- `\HAOracleReference`

**Macros, primary under 08B (new):**
- `\HAAstraBinaryAgreement`, `\HAAstraBinaryAgreementCI`
- `\HAFableBinaryAgreement`, `\HAFableBinaryAgreementCI`
- `\HAInterJudgeBinaryAgreement`

**Macros, strict sensitivity:** the same names with the `Strict` suffix:
- `\HAAstraValidStrict`, `\HAFableValidStrict`
- `\HAAstraKappaStrict`, `\HAFableKappaStrict`, `\HAAstraKappaCIStrict`, `\HAFableKappaCIStrict`
- `\HAAstraReliabilityStrict`, `\HAFableReliabilityStrict`
- `\HAAstraComparedPairsStrict`, `\HAFableComparedPairsStrict`
- `\HAAstraBinaryAgreementStrict`, `\HAAstraBinaryAgreementCIStrict`
- `\HAFableBinaryAgreementStrict`, `\HAFableBinaryAgreementCIStrict`
- `\HAInterJudgePairsStrict`, `\HAInterJudgeCategoryAgreementStrict`, `\HAInterJudgeBinaryAgreementStrict`
- `\HAInterJudgeBinaryKappaStrict`, `\HAInterJudgeBinaryKappaCIStrict`
- `\HAInterJudgeCategoryKappaStrict`, `\HAInterJudgeCategoryKappaCIStrict`
- `\HAOracleReferenceStrict`

## Addendum 08C: reasoning effort of the corpus classifier

`python -m chl_rag.classifier_effort` takes these inputs:
- the low, high and max classifications of the 35 live pairs, each of which must be complete;
- the addendum-08B outputs of both judges.

The reference is the category on which Astra and Fable agree. For each setting:
- **Primary measure:** the number of reference pairs where the setting's binary flag equals the reference flag. An invalid label counts as a mismatch.
- **Secondary measure:** the number of category matches.

The rule chooses the lowest effort, in the order low, high, max, whose primary measure is at most one pair below the best. No student data is read.

The command writes the decision JSON. Its `decision_sha256` is the sha256 of the canonical JSON without that key. It also writes the macros `\HAEffortChosen`, `\HAEffortReferencePairs`, `\HAEffortMatch{Low,High,Max}`, `\HAMaxFlagged` and `\HAMaxBinaryAgree{Students,Astra,Fable}`, where Students stays `\pending{}`.

The human analysis also accepts `--classification max=PATH`. At the freeze it then defines the `\HAMax*` macros, including Students. Input its macros before the effort macros, because `\providecommand` keeps the first definition.

## Additions of 16 November (approved by Antonio after the report review)

**Per-setting results.** For low (the registered system label), high (the corpus classifier chosen by 08C) and max (descriptive), `students.*.settings` gives:
- the pairs the setting flags, with its invalid labels;
- **precision on the study sample**: the denominator is the pairs that setting flags. The 35 pairs are a stratified sample, so the figure is not extrapolated to the corpus;
- binary and category agreement with the student consensus.

**Agreement indices.** Every agreement block reports raw agreement, Cohen's kappa and Gwet's AC1 (Gwet 2008), with the same pair-cluster bootstrap and seed. AC1 uses the full scale: six categories, two binary values, and three answers for the Q1 checks. The kappa estimates and intervals are unchanged.

**Factor agreement** (descriptive, counts only). It covers the pairs whose student consensus, or whose agreed oracle category, is apparent contradiction.
- **The student consensus factor** is the unique majority of at least 2 among the reads in that category.
- **The oracle factor** counts only where both judges agree on it.
- **A mismatch** is counted when the setting's label is invalid or in another category.

**New macros.** `S` is `Low`, `High` or `Max`:
- `\HA<S>FlaggedDenominator`, `\HA<S>Invalid`
- `\HA<S>PrecisionStudents`, with `N`, `CI` and `Bounds`
- `\HA<S>BinaryKappaStudents`, `\HA<S>BinaryACOneStudents`
- `\HA<S>CategoryAgreeStudents`, `\HA<S>CategoryKappaStudents`, `\HA<S>CategoryACOneStudents`
- each agreement macro also has a `CI` version
- `\HASystemBinaryAgreement`, `\HASystemBinaryACOne`, each with `CI`
- `\HAAstraACOne`, `\HAFableACOne`, each with `CI` and `Strict`
- `\HAInterJudgeBinaryACOne`, `\HAInterJudgeCategoryACOne`, each with `CI` and `Strict`
- `\HAFactorPairs`, `\HAFactorStudentsPairs`, `\HAFactorOraclePairs`, `\HAFactor{Students,Oracle}Match<S>`

The macro names use `ACOne` because LaTeX names cannot contain digits.
