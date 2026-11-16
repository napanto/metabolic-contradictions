# Protocol amendment 04 — isolated frontier judge and human validation

Antonio's decision, recorded in INBOX_FROM_CLAUDE.md on 13 November 2026 at 12:16 CET, supersedes the original full-human-reference evaluation. The corpus, pipeline, candidate frame, predictions and original 145-item sample remain frozen. This amendment changes the reference annotations and validation design only.

## Timing and previous exposure

Commit and sign this amendment and its selection specification before creating the new human package, before any oracle output, and before inspecting existing LLM annotation judgments for this change. Antonio has not annotated and has seen no LLM labels. Claude previously saw one line, extraction-001, while checking provenance. Record that exposure; do not condition selection on its content. The existing `Independent Codex LLM` annotations remain byte-for-byte unchanged as a secondary in-run xhigh pass. Its blinding was instruction-based, not technically enforced. Prediction-aware control/source audits are not annotation gold.

## Frozen selection

Parent package logical SHA-256: `0eb68a4d5fe57a05b2ef23cf0546d2e19b4dea4972c9810790e4d6731a8f68ec`.

Guidelines SHA-256: `a71775f353bc3973986a4113646366c504a35d76ef82400a050cf1ff78a1e73f`.

Use Python `Random(20261113)`. Allocate 40 pairs proportionally across the existing arm/predicted-class sampling strata by largest remainder, minimum one per nonempty stratum, with lexicographic ties. For the observed stratum sizes this also equals unconstrained largest-remainder allocation. Draw without replacement from sorted IDs in sorted stratum order. Then use the same RNG to draw two of the 15 abstracts per arm, in order biomarker, drug, diet. Keep every claim from each chosen abstract. This gives 46 items and 31 extraction claims. Retain original IDs and item contents; preserve the full package unchanged and give the separate human UI package a new hash.

| Stratum | Original | Human |
|---|---:|---:|
| biomarker/apparent_contradiction | 29 | 12 |
| biomarker/contradictory | 3 | 1 |
| biomarker/not_replicated | 2 | 1 |
| diet/apparent_contradiction | 24 | 10 |
| diet/contradictory | 2 | 1 |
| diet/not_replicated | 7 | 3 |
| drug/apparent_contradiction | 24 | 9 |
| drug/contradictory | 3 | 1 |
| drug/not_replicated | 6 | 2 |

Selected human IDs, fixed before oracle output:

- `extraction-005`
- `extraction-015`
- `extraction-017`
- `extraction-018`
- `extraction-028`
- `extraction-035`
- `pair-001`
- `pair-004`
- `pair-006`
- `pair-007`
- `pair-008`
- `pair-010`
- `pair-012`
- `pair-013`
- `pair-014`
- `pair-018`
- `pair-019`
- `pair-020`
- `pair-026`
- `pair-029`
- `pair-035`
- `pair-036`
- `pair-038`
- `pair-039`
- `pair-043`
- `pair-044`
- `pair-046`
- `pair-053`
- `pair-056`
- `pair-060`
- `pair-061`
- `pair-063`
- `pair-064`
- `pair-076`
- `pair-078`
- `pair-083`
- `pair-084`
- `pair-085`
- `pair-086`
- `pair-087`
- `pair-089`
- `pair-091`
- `pair-092`
- `pair-093`
- `pair-095`
- `pair-099`

The oracle order is a shuffle of all 145 sorted IDs using `Random(20261114)`, stored in `configs/evaluation_amendment04.json`. Its first five pilot IDs are `pair-067`, `extraction-036`, `pair-088`, `pair-098`, `extraction-042`. No labels are consulted in either draw.

## Oracle execution and isolation

Use GPT-6 Astra, maximum reasoning effort, through the Codex CLI on the ChatGPT subscription. Do not use OpenAI API keys/credit, gopass, or OpenRouter for these oracle calls. One item per fresh process, concurrency at most two:

```text
codex exec -m gpt-6-astra -c 'model_reasoning_effort="max"' --ephemeral -s read-only --skip-git-repo-check --output-schema <schema> -C <new empty temporary directory outside the repository>
```

Each user prompt contains only the exact frozen GUIDELINES.md text and that one public item. A task-specific JSON schema constrains judgments; annotation metadata is attached by the runner. No other items, system verdicts, labels, repository instructions or private sampling information are supplied. Use a fresh minimal configuration without inherited plugins, MCP connectors, history or unnecessary tools. Subscription authentication is separate from study inputs; API-key authentication is prohibited.

An empty CWD and read-only mode do not establish read isolation. Enforce an OS-level filesystem boundary with no repository or home-data mounts, and verify that the repository, results, data and private sampling manifest cannot be read. Record configuration, mounts and adversarial sentinel-read probes before the pilot. Pure isolation probes use no study labels/items.

Log under `annotation/oracle_max/`, for each item and attempt: ID, prompt SHA-256, model, effort, Codex version, start/end time, elapsed time, raw output/events and schema validity. Keep credentials out of logs. Limit each attempt to 30 minutes. Invalid or absent output receives one retry with identical prompt/schema; after that retain an invalid result. No manual label fixing or changed repair prompt.

First run only the five pilot items. Record median time, task-specific times and validity in STATUS.md. Max is unusable if unsupported/repeatedly limited, fewer than four of five items validate after permitted retries, or projected completion exceeds Saturday 14 November at 20:00 CET. Project task-specific pilot medians over 100 pairs and 45 abstracts at concurrency at most two, with a 50% time buffer and 90 minutes for final validation. If necessary, declare xhigh fallback; preserve the max pilot separately and run the same pilot plus all 145 primary-reference items at xhigh. Do not mix efforts in the primary reference or substitute another model.

## Prespecified analyses

Primary metabolic estimates use the isolated oracle over all 100 pairs and 45 abstracts: observed flag precision overall/per arm with descriptive Wilson 95% intervals; design-weighted flag-frame precision using original inclusion probabilities and arm/class-stratified bootstrap intervals; and strict extraction field correctness with abstract-cluster bootstrap intervals. Uncertain field judgments count as incorrect. Report evidence support and reference validity separately. These are judge-referenced estimates, not human gold or clinical truth; whole-corpus recall remains unmeasurable.

Human validation uses the 46-item subset. Report raw agreement and Cohen's kappa for the six-way pair label, binary true flag, and each extraction field, with 2,000 percentile-bootstrap replicates using seed 20261115. Resample pair items within the original sampling strata; resample abstracts within arm, retaining all claims. Report undefined resamples and degenerate intervals. Six abstract clusters and repeated PMIDs limit precision. Also report evidence-support agreement.

Report human-subset flag precision overall/per arm with Wilson intervals and design-weighted precision using two-stage inclusion probabilities: original sample inclusion multiplied by within-stratum validation inclusion. Report extraction correctness on the 31 selected claims with small-cluster caveats. Human estimates are initially secondary.

**Reliability rule:** if oracle–human binary true-flag kappa is below 0.40, report oracle-based metrics as unreliable and make human-subset estimates primary. Apply this threshold to the point estimate, not a confidence bound. Before human labels exist, reliability is pending and oracle results are provisional. Undefined kappa is not a pass: report inconclusive validation without replacing it by zero or 0.40.

Also report raw agreement and kappa, with analogous bootstrap intervals, between the isolated oracle and the preserved in-run xhigh pass on all 145 items. Do not describe the secondary pass as technically isolated or as independent human validation.

Invalid oracle items are missing references, never invented labels. Report valid-reference estimates, coverage, and pessimistic/optimistic bounds over missing references. If a whole stratum has no valid reference, its weighted point estimate is not estimable. Human/oracle comparisons use matched valid items and disclose missing matches. List and discuss disagreements qualitatively; do not adjudicate silently. Retain original artifacts and revisions.

## Reporting and remaining human gate

Describe the reference as “a frontier LLM judge (GPT-6 Astra, maximum reasoning effort, isolated, run through the Codex CLI on a ChatGPT subscription: an agent wrapper rather than a raw API call; closed weights), validated on a random human subset”. Before validation say “to be validated”; disclose xhigh if fallback occurs. Note that this is the same model family as the agent that built the pipeline.

Restart the local UI on the new 46-item package at 127.0.0.1:8765 and document the restart. Rewrite ANNOTATION_READY.md in Italian with an approximately one-hour estimate. Preserve original public items and existing secondary labels. Keep STATUS.md current through isolation checks, pilot and full run. The existing OpenRouter ledger/cap remain unchanged; do not invent a USD subscription cost. The requested narrative/style revision follows human annotation. Stop only when the 46 human items are the sole remaining input dependency.
