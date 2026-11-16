# Amendment 05 — prospective development of pipeline v2

13 November 2026. This development plan is registered before any new paid P run, S output or S/H judgment. Its signed Git commit and the bound machine-readable configuration identify the registration. It supersedes the **unregistered** v1 student-study draft, not the signed v1 protocols or results. Amendment 06 and production require a separate decision after the single H check.

Authority: the ordered inbox entries headed19:10,18:43,18:56,18:58 and19:02, and `../v2-decisions-2026-11-13.md`, including sections11–12. Those decisions override the conceptual memo. The v1 oracle's provisional aggregate assessments and **a formative pilot of the annotation interface** motivate a post-hoc revision; they do not demonstrate that v2 improves accuracy. No individual v1 judge labels or pilot interactions become development gold. V1 and its ManCon benchmark remain intact.

## Sources and separation

P is the original30 development abstracts (10 per arm) and six invented abstracts containing12 atomic acceptance cases. Prompt/schema development is restricted to P and these synthetics, with up to four documented revisions per prompt. S contains36 new abstracts (12 per arm), sampled with `Random(20261118)`, arms biomarker→drug→diet, sorted PMID pools and unique overlap assignment. H contains24 new abstracts (8 per arm), seed20261125, separated and sealed before selection. H source bodies and judgments are not opened for choosing the model.

The deterministic partition manifest is `data/derived/v2/partitions.json`: P30, prior-evaluation quarantine231 distinct source PMIDs, additional audited sources86, additional duplicate sources2, S36, H24, and F14,218. The remaining14,932 corpus records are outside extraction eligibility. The entire old evaluation package is quarantined, including every old human-subset/preview source. No participant answers, identity or payment data are read for these exclusions. Exact DOI, normalized source-text and explicit study-identifier links are quarantined at source level; unidentified shared cohorts remain a limitation. Every selected pair must have both sources in its eligible frame.

A3,000-source reserve,1,000 per arm, seed20261119, is fixed from F. It can be used **only under the timing rule**, never to save money. The full operational corpus has14,627 eligible sources, processed reserve-first; only F is eligible for final evaluation. Later evaluation seeds remain extraction20261120, pairs20261121, human subset20261122, oracle order20261123, analysis20261124. Final evaluation is not authorized by this development amendment.

## Fixed extractor grid and provider availability

The13 requested rows are retained in `configs/dev_selection_v2.json`:

- Qwen3.6-35B-A3B: off/on; on is enabled reasoning, not a fabricated discrete effort.
- Qwen3.8-Flash: off/low/medium/xhigh through the gateway, on the single Alibaba provider with quantization explicitly recorded as **unknown**.
- DeepSeek-V4.1-Flash-1017: off/low/high/max.
- GLM-5.3-Flash: low/high/max; off is unsupported.

Live official endpoint snapshots show only Alibaba Qwen3.8 routes with unknown quantization and no currently declared native discrete supported efforts. Antonio’s18:58 correction explicitly authorizes this single-provider exception and all four gateway settings. All13 rows remain in the experiment. P verifies parameter acceptance; the report distinguishes gateway controls from native declared efforts. A technical rejection is reported and escalated, never silently treated as poor quality or a reason to drop Qwen3.8.

For multi-provider models, only enumerated healthy declared-fp8 endpoints with the required schema, seed, temperature and reasoning parameters are allowed. Qwen3.8 uses one pinned Alibaba endpoint only; no quantization claim or multi-provider overflow is made for it. Requests pin one route using `only`, `order`, `allow_fallbacks=false`, `require_parameters=true` and quantizations fp8 where declared; the Alibaba exception omits a quantization filter. PrimeIntellect is first for Qwen3.6. Other routes are ordered by the frozen quote for1,000 input plus4,000 output tokens, then tag, with one route per provider. Overflow goes to the next idle eligible provider. Record the requested route, returned provider, canonical model, quantization, tokens and cost. At most16 in-flight calls per provider name across this runner's models; known-free429 responses have bounded adaptive backoff. The classifier-selection worker has its own separately registered runner and ledger.

All candidates use the same v2 prompt/schema, temperature0 and seed42. Rich fields distinguish result/comparison/subgroup/analysis/time, groups and sample sizes, and authors' significance from estimate direction. Program-owned provenance and deterministic IDs are added locally. Evidence validation checks literal quotes, sentence eligibility and numeric/structural consistency; it is not semantic verification. Original emitted occurrences, including duplicates and later dropped claims, remain available for scoring. No manual claim repair or truncation.

## P calibration and locking S

Initial total completion limits are off32,768; on/low/medium65,536; high131,072; xhigh/max262,144, subject to a registered endpoint's ceiling. P smoke uses the first original P source in each assigned arm plus all six synthetics. Use P solely for technical calibration, the four permitted prompt revisions and checking the stated error families. On truncation or consumption at least90% of the limit, double it up to the endpoint ceiling and repeat the same P input; retain and charge all attempts. Do not label artificial truncation as low semantic accuracy.

Freeze final caps before S: at least the initial limit, twice the maximum observed completion rounded up to1,024, and sufficient visible-output room. The visible margin is max(4,096, twice observed visible tokens); use UTF8 output bytes conservatively if token decomposition is missing. Where gateway fractions apply, reserve at least V/(1−f), f=.2low,.5medium,.8high,.95xhigh/max. Binary on has no invented fraction. A configuration is technically unavailable if its supported endpoint ceilings cannot accommodate the frozen requirements. All caps, schemas, prompts and source hashes are bound in a signed S-calibration record before any S call.

The full available S grid runs interleaved, with fixed seed20261126, without quality-based early elimination. Each item has at most one identical retry after invalid output; no repair prompt. Terminal failures remain recorded. Retrying uses a separate cache attempt namespace while retaining the identical model, request body, provider, schema and reasoning. Caches and outputs are v2-only.

## All-prompt development and the S freeze

The19:02 instruction expands prompt work to extraction, same-model verification, the metabolic pair classifier, both judges' source-inventory/scoring rubrics and the final oracle instrument. The ManCon grid keeps its existing prompt unchanged. Every prompt receives an independent sub-agent review for ambiguity, expected-answer leakage and pressure toward contradiction labels. Its author cannot supply that review. Changes use only P, pairs built from P and synthetic cases, never S/H or prior-evaluation/preview sources. Record each of up to four revisions, rationale, reviewed hashes and P evidence.

This initial signed amendment permits P calibration and prompt development. **S remains technically locked** until a signed `PROTOCOL_ADDENDUM_05a_PROMPT_FREEZE.md` and `results/v2/prompt_freeze.json` bind every final prompt/instrument and independent review, plus the signed P calibration. Later P changes do not rewrite this registration or earlier outputs; each run retains its exact request and revision hashes. The final instrument and classifier prompt are developed now and frozen before S, even though their production use requires06.

## Independent development references

Two judges are used independently: Claude Fable5.2 (`claude-fable-5-2`), adaptive thinking enabled and `output_config.effort=max`, through the Anthropic API; and GPT-6 Astra at max through the OS-isolated subscription wrapper. Astra uses at most four concurrent fresh processes. The wrapper retains annotation-only instructions, no tools/repository access and ChatGPT-only egress. No OpenAI API key or credit is used. Neither judge receives the other's inventory, output or model identity. Participant answers are never sent to either judge.

Each judge first constructs and seals its **own source-only inventory** for every source in a partition, before seeing candidate outputs. Candidates have opaque aliases and seeded shuffled order; source text, fields and emitted occurrences are preserved. For efficiency, scoring uses source-scoped batches of at most three anonymous variants, with each variant judged against the source and own inventory independently. Duplicate detection never crosses variant boundaries. P checks output/context feasibility; a smaller batch may be fixed for technical reasons before S, with a signed calibration record. No batch-size or semantic rubric adjustment follows S quality results. Standard per-variant artifacts are deterministically split from the validated parent response.

Fable uses streaming, explicit five-minute prompt caching for shared rubric/source where applicable, maximum output128,000 initially, and complete usage accounting including reasoning/cache tokens. Refusals are terminal, not blindly retried. Each judge call permits one identical retry for invalid output, at most30minutes per attempt. The frozen rubrics/schemas specify exact output units. Backend-added Astra instructions remain uninspectable; same-family development/final judging and batching effects are limitations.

## Metrics and inventory correspondence

Each judge marks every original emitted claim core supported/unsupported/unclear, the five field groups, and critical invented-comparison/reversed-direction errors. A core is correct only if exposure, outcome, direction/conclusion and comparison are supported. Duplicates earn no additional recall.

Consensus: supported+supported=correct; unsupported+unsupported=incorrect; every other combination=contested. Strict P_core counts contested as not correct. Lenient P_core is the optimistic bound counting contested as correct. A critical error counts if **either** judge flags it.

The consensus inventory is the exact intersection of normalized source-anchor tuples: result sentence IDs plus literal exposure, outcome, comparison, subgroup, analysis and time spans. Normalize only NFKC/casefold/whitespace. No third adjudicator or semantic matching tuned to outcomes is introduced. Report alignment counts and unmatched inventories: conservative lexical mismatch is a limitation. Union-inventory recall is a sensitivity analysis. Recovery requires a consensus-correct claim and maximum one-to-one cardinality matching; ties maximize multi-result recovery. Shared-inventory matches must agree under both judges. A generic claim cannot cover several distinct subgroup results.

R_atomic is recovered atomic results/inventory results. R_multi uses results in source sentences marked multi-result/exception **before** candidate outputs. Field support precision and recovery cover comparison, significance basis, population/subgroup, n/unit/scope, and analysis/adjustment. Canonical emitted JSON-path details and independent expected inventory details prevent omission from earning credit. Consensus details require both support judgments; detail correspondence uses normalized text, evidence and source-result anchor. Groups with genuinely no evaluable details are omitted and disclosed; expected-but-omitted details yield zero F1.

`S = .7 × F0.5(P_core(strict), R_atomic) + .3 × F_fields`, with `F0.5=1.25PR/(.25P+R)` and F_fields the mean support/recovery F1 across the five groups.

D0 is the saved v1 extraction on these sources, never regenerated. It supplies core/atomic/multi-result baselines. Rich fields absent from D0 are nonrepresented, not fabricated false positives; its composite is not directly comparable. The no-regression gate uses only faithfully preserved common paths: comparison.comparator, result.authors_significance, study.design. Unanchored legacy population/n remain descriptive and are never silently mapped to group sizes or study totals. Every estimable registered common path is checked separately; a newly missing metric does not pass.

Report each judge's metrics separately, consensus strict/lenient and union sensitivity, critical counts, duplicates/drops, first/final validity, truncations, input/visible/reasoning tokens, costs, latency mean/p90 and measured throughput. Report claim-level Cohen kappa and raw agreement overall/by field group and Kendall tau-b between complete per-judge rankings. Undefined quantities remain explicit.

## Selection and the single H check

Technical S gate: at least35/36 first-attempt valid outputs,36/36 after the one retry, no final truncation, and no unresolved critical error in the synthetic acceptance cases. This scales the memo's17/18 gate to the approved36 sources.

**Each judge separately** must find P_core≥.90; R_atomic≥max(.70,R_atomic(D0)−.05); R_multi≥R_multi(D0)−.05 when D0 is estimable; and no common-path support regression over.05. Undefined P_core/R_atomic fails. Only jointly eligible configurations can be selected.

The fixed extract–verify expansion takes the top two technically complete single-pass configurations from different models, ranked by the rule below even if semantic eligibility fails. This permits the prespecified verification comparison without adding variants after results. With fewer than two technically complete models, disclose the unavailable expansion. Reuse extraction, add exactly one same-model/same-configuration verification pass; drop only unsupported claims without rewriting, retain unclear flagged, count drops as lost recovery. Apply full eligibility again to the final fixed candidate pool.

Rank by fewest critical errors, then consensus S (a .02 near-tie band), then higher Fable-only S, active reasoning, lower measured time and lexical ID. **Cost is reported and never used for selection** under the later budget decision. Publish all rows. The best time-feasible configuration is operational; report any difference from the quality winner. Before selecting a lower time-feasible configuration, apply the prespecified full-versus-reserve time rule. Never lower effort or reduce the corpus to save money.

If none passes jointly but at least one passes one judge, **stop for Antonio through STATUS.md**. If none passes either, report no winner; do not invent a successful v2. Freeze one selected configuration before H. H runs once:24/24 final valid outputs, no truncation, zero invented-comparison/reversed-direction errors, P_core≥.85 and R_atomic≥.70 **under both judges**. A failure cannot trigger another candidate on the same H or tuning followed by a renewed claim of independence.

The winner also receives a provider-consistency check on all S sources using two different eligible fp8 providers with the same frozen settings when two authorized providers exist. If the single-provider Qwen3.8 configuration wins, report that this cross-provider check is unavailable rather than introducing an unauthorized second provider. Both outputs are judged using the same inventories/rubric and reported separately. This diagnostic does not reselect the model or permit semantic changes.

## Funding, timing and stop points

The later budget instruction replaces fixed ceilings with **replenishable alert points**. Keep separate v2 ledgers and worst-case reservations before each attempt, without cache discounts in the reservation. Unknown billing retains the full liability. Actual cost above the reservation stops new calls until reviewed. Model refusal/invalid output cannot create an unbounded loop.

Initial phase alert points remain extraction experiment20 USD, production extraction45, classification40, reserve15; the separate classifier worker has its own30 alert. Account-level alerts: OpenRouter projected remaining spend above approximately85 USD against96 available; Anthropic105 spent against119 available. A phase pauses at an alert or insufficient confirmed credit for reserved/projected spending. Write the service and approximate top-up in STATUS.md, and resume only after Claude confirms. No candidate, reasoning level, sample or scientific scope is cut for cost. Final Fable evaluation requires a separate confirmed top-up and amendment06; it is not enabled here. Sum distinct ledgers once in the final audit.

Target S/judges by23:00, H/selection by23:30. The original bounded development inference window is120minutes, tracked separately from implementation/calibration time; at its exhaustion report incomplete comparisons and stop for a declared scheduling decision, never claim a complete best-model search. Production full scope is allowed only when measured throughput plus50% margin permits extraction by Saturday01:00 and classified frame by05:00. Otherwise use the prefixed reserve **for time only**. At01:00 apply the registered time fallback; package deadline06:00, site09:00. No selection from the first or easiest completed sources.

**Mandatory stop before amendment06:** Antonio may change the Sunday fallback. Prepare the concrete selected pipeline and evaluation plan, then write the decision request in STATUS.md. Do not register06 or create production predictions before that response. The production site is `https://metabolic-evidence.it`; the separate site worker implements the bilingual instrument and current privacy text. No deployment, recruitment, contact, payment or push is performed by this runner.
