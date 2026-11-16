# Student validation methods — implementation draft

This is preparation for amendment 05, not a registered amendment. The ordered
entries in `INBOX_FROM_CLAUDE.md` govern the design. The entry headed 14:27 requires
Claude's confirmation of the final site configuration before registration; the
later 14:45 entry replaces constructed controls with live quality flags. The
heading times were estimates: entry order controls. No student data or oracle
labels were inspected while implementing this module. No active oracle setting
or run was changed.

The reference comes from paid students in eligible biomedical programmes,
recruited through group chats. It is student consensus, not clinical truth or an
annotation attributed to the author. The author retains the six extraction
abstracts. If any of the frozen 40 pairs lacks two valid student reads at Sunday
15 November 2026, 12:00 CET, the author fallback applies to all 40 pairs. This
pure statistics module reports pairs below two reads. The integration module
requires a timestamped, evidence-bound coordinator decision to activate the
all-author fallback; absence of an export never activates it. Author pair labels
are excluded from the student reference unless that fallback is explicitly
selected. A later export may document coverage as of the deadline, and later
submissions do not cancel an activated fallback.

## Inputs and integration boundary

`student_validation(rows, expected_pair_ids, oracle_labels=None, ...)` accepts
raw in-memory JSONL rows and returns an auditable report. `adapt_site_export`
validates the currently documented answer fields in the read-only sibling site's
`README.md` and the exporter in `app/cli.py`. The adapter requires the caller's
frozen real-pair whitelist. Synthetic/foreign IDs, duplicate session/pair reads,
unknown fields, invalid labels, invalid flags and inconsistent session snapshots
fail closed. Rationale and factor may be absent or blank; they are never invented.
Provided factors must match the project keys. The source row is retained, with
its original one-based export ordinal added as `export_row`.

The six categories and factors already match `annotation/GUIDELINES.md` and
`annotation.py`. In particular, direct contradiction is `contradictory`; labels
are not remapped through numeric button order. Factors and rationale do not alter
the category vote. Unpaid surplus is retained for accounting and omitted from
both statistical populations.

The current export has `paid`, `replacement`, `surplus`, `excluded`, `quality_failed`,
`counted` and `preview`. Production rows carry `preview=0`; all `preview=1` rows
are rejected, including the site's optional timing-calibration export. Earlier
14-field exports without the preview marker remain supported without adding a
fabricated marker. Here `paid` means an accepted answer credited by the assignment engine,
not whether the participant reaches the minimum payout or has received a bank
transfer. Short valid sessions remain eligible. `counted` identifies the site's
currently eligible answer per assignment slot. The primary population requires
`paid=1`, `surplus=0`, `counted=1`, `excluded=0`, and `quality_failed=0`.

The sensitivity population includes all accepted nonsurplus real reads, including
flagged, excluded and displaced reads. It can contain more than four reads per
pair because replacements remain available for audit. Its majority is recalculated
from those reads. This is an explicit sensitivity population, not a reconstruction
of a collection that never excluded anyone: exclusion changed assignments and
escalation while collection ran.

Submission timestamps are canonical UTC seconds. Chronology uses submission time,
then original JSONL order. The current exporter orders by answer ID but omits that
ID; its ordering must be preserved. The integration hashes the original input
bytes before analysis and archives those exact bytes with its result. The pure
statistics module does no file I/O.

`student_evaluate.run(...)` returns `pending_configuration` without reading an
export or oracle labels until the confirmed, registered amendment-05 configuration
exists. Site defaults are not confirmation. A missing or omitted export then
returns `pending_student_export`, preserving oracle/secondary metrics and genuine
author extraction annotations without importing early author pair labels. Temporary
base-evaluation output stays inside `data/raw/student_analysis_scratch/`.

Finality requires closed-collection metadata or an explicit bound attestation.
A closed collection with unresolved categories is reported as completed with
missing references and bounds, rather than waiting indefinitely for adjudication.
Pair and extraction validation have separate completion states and descriptions.

The current contract is incomplete for final integration:

- Answer rows do not carry package/configuration hashes or a schema version. The
  frozen-ID whitelist rejects foreign items but cannot prove content binding.
  Preserve the site provenance and confirmed configuration beside the export.
- The exporter omits programme/year, read IDs, assignment-slot IDs, flag-time
  metrics, reasons and manual-override history. Obtain a documented deidentified
  or aggregate export for the missing reporting fields. Do not open payment or
  identity exports to fill them implicitly. Demographic values remain explicitly
  unavailable unless a validated aggregate companion is supplied.
- The site's README now documents sections 10–18, the real-pair rule and disabled
  hidden-control defaults. These implementation defaults do not replace Claude's
  still-pending confirmation of the final deployed configuration.

## Consensus and reliability

A category wins only with at least two votes and strictly more votes than every
other category. No winner yields `unresolved` when at least two reads exist;
fewer reads yield `insufficient_reads`. Both have a missing reference label.
Binary mapping occurs only after a category resolves. Direct contradiction,
apparent contradiction and non-replication map to a true flag. A tie among those
categories remains unresolved even though their binary mapping agrees.

Human–human agreement uses the first two eligible chronological reads on each
pair, before looking at the eventual consensus. Later escalation cannot improve
this statistic. Report raw agreement and nominal Krippendorff alpha separately
for six categories and binary flags, together with counts of pairs with zero,
one and two eligible reads.

Nominal alpha is `1 - observed_disagreement / expected_disagreement`. For a unit
with `m` ratings, ordered within-unit coincidences are weighted by `1/(m-1)`;
singleton ratings contribute to neither term. Expected disagreement uses the
pooled pairable-value counts with the finite-sample `n*(n-1)` denominator. This
supports varying reader identities and missing observations. There is no ordinal
distance between categories. These details and the two numerical fixtures were
checked against Krippendorff's primary methods document, sections A–C and the
nominal metric in section D, pages 1–5. The archived source and honest reading
coverage are in `../../papers/inbox-2026-11-13/krippendorff-alpha/`.

Alpha and kappa are undefined when the relevant chance disagreement vanishes.
The report uses null with a reason or estimability status; it never substitutes
zero or one. Negative defined values are retained. Empty populations produce
missing estimates and full missing-resample accounting.

Compare the oracle with resolved consensus using raw agreement and Cohen kappa
at both category and binary levels. Amendment 04's binary kappa `<0.40` rule is
retained. An undefined kappa never passes that rule. The module returns the rule
status; the integration layer determines primary/secondary report presentation.
Oracle comparisons with individual reads are reported pooled and by pseudonymous
session, including explicit disagreement rows. They preserve the repeated reads
and never adjudicate them. The pooled individual-read estimate is read-weighted;
the consensus comparison has one observation per resolved pair.

Student human–human, oracle–consensus and oracle–individual intervals use 2,000
percentile bootstrap replicates, seed **20261117**, resampling whole comparable
pairs without stratification. All individual comparisons within a sampled pair stay
together. Human–human intervals use pairs with two eligible reads; unmatched and
unresolved references are counted separately. Undefined resamples, effective
replicate counts and degenerate intervals are explicit. Intervals condition on
observed eligibility and do not model dependence between pairs sharing readers
or abstracts, informative missingness, or the adaptive collection process.
Student-referenced design-weighted precision retains the original sampling strata
and uses seed 20261117. Oracle-only amendment-04 metrics and secondary-LLM
comparisons retain seed 20261115. Author extraction intervals retain amendment-04
abstract-cluster resampling within arm and seed 20261115.

## Quality and descriptive reporting

The latest ordered instruction flags a session with at least six shared pairs
and at most one category agreement, or at least five real pairs and a median
time below 30 seconds. Constructed controls and retest pairs are not used.
Three synthetic practice examples are training only and excluded from every
metric. Exact deployed flags, thresholds, caps and pay still require the
configuration confirmation; this module does not freeze them or replay flags.

The current site's `quality_report` counts agreement with **any** currently
eligible other reader on a pair, at most once per shared pair. It is not a
leave-one-out majority. Its own-session denominator contains accepted paid real
reads. The draft recomputes these diagnostic counts and elapsed-time summaries
from the final exported snapshot, then reports distributions across sessions.
That snapshot can differ from the moment of flagging. Stored flags govern the
primary exclusion, even when current diagnostic values would no longer trigger
it. Final flag reasons and override history require the additional export above.
The latest site contract says a manual include clears the active quality flag
and records an override; this analysis follows those final exported flags and
does not attempt to recreate historical exclusions.

Report flagged/excluded session counts, unresolved and insufficient-read counts,
accepted/displaced/surplus accounting, time per read, and distributions of
per-session shared pairs, matches, match rates and median time. Rates omit
sessions with no shared pair instead of assigning them zero. Programme and year
must ultimately be aggregate counts, with missingness stated. Server timing starts
at assignment activation during the preceding POST, is bounded at 24 hours and
survives refresh/restart. It includes interruptions and is not a measure of
attentive reading. Agreement-based filtering can inflate measured
agreement, so primary and all-accepted-read sensitivity results must both appear.

The current student interface presents deterministic sentences constructed from
the extracted claim fields, with the original fields in collapsed technical
details. The oracle and author fallback interface use raw fields. Disclose this
presentation difference when comparing judgments; these sentences are generated
by templates, not by a model. Unpaid preview data are for review/calibration and
never enter either analysis population.

## Verification

`tests/test_student_validation.py` contains only invented fixtures and the
published methodological examples. Tests cover both source alpha examples,
variable raters/missing values, degeneracy and negative alpha, category ties,
chronology, exclusion and replacement accounting, immutable raw reads, strict
export validation, the binary reliability rule and clustering every read of a
pair during resampling. No real student or oracle labels are test fixtures.
