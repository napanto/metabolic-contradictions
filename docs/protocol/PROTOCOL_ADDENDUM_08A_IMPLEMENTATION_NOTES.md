# Addendum 08A: implementation notes

## Note 1: stop at 02:33 CET and restart from the checkpoints (16 November 2026)

**What happened.** The run started at 01:51 CET. It stopped at 02:33 with `RequestConfigurationError: No eligible fp8 endpoint remains for this request`, after 5,354 of the 14,218 sources had finished.

- **Cause.** From about 02:15, every request was refused before generation with HTTP 403 "Key limit exceeded (total limit)". This is the OpenRouter API key's own total spend limit; it is not a provider failure. The refusals by route were:

  | Route | Refusals |
  |---|---|
  | Morph | 607 |
  | Novita | 283 |
  | Near AI | 269 |
  | Inceptron | 223 |

  These refusals cost nothing and generated nothing.
- **The router bug.** The first router counted these refusals as provider refusals, held every route after five in a row, and then raised. That broke the rule of the addendum: a rejection before generation is retried and never stops the run.
- **Other route events:**
  - GMICloud returned HTTP 404 "No endpoints found that can handle the requested parameters" 128 times. It lists `response_format` but not `structured_outputs`, and the request uses a strict JSON schema with `require_parameters`.
  - Inceptron received upstream 429 rate limits, and the adaptive control lowered its cap.

**Router fix** (`src/chl_rag/v21_corpus.py`):
- **Key limit.** A key-level 403 pauses every admission. One probe is sent every 60 seconds, and admission resumes on the first accepted request.
- **Route cooldown.** A route with five consecutive rejections before generation (429, 5xx or a provider refusal) cools down for 60 seconds. The cooldown doubles each time, up to 600 seconds, and the route is then re-probed.
- **Waiting.** A request waits for a route instead of raising.
- **Permanent hold.** Only a 404 for unsupported request parameters holds a route for the rest of the run.
- **Raising.** The router raises only when the registered route set is empty.
- **Unchanged:** fp8 only, pinning with no fallback, `require_parameters`, the per-request worst-case reservation and the single identical retry for invalid or truncated output.

**Blocked attempts reconciled before the restart.** 480 attempts were blocked by a rejection before any generation:
- 408 first attempts that stopped in the router before any request was sent;
- 72 identical retries refused with the key-limit 403.

They are archived in each source's `preflight_history`, the same mechanism the frozen code uses for budget preflight blocks, so they are not attempts. Each source restarts that attempt with the identical request. A retry stays pinned to the provider of the first, invalid attempt. Completed sources are never sent again. Any other blocked attempt is left blocked and stops the run, so that it stays visible.

**Catalogue re-snapshot at 01:40:47Z:** `configs/provider_snapshots/glm-5.3-flash_endpoints_20261116T014047Z.json` (sha256 d106007ef69b…). No new fp8 endpoint qualifies once `structured_outputs` is required, which the 404s above show is needed. The fp8 endpoints that remain excluded:

| Endpoint | Reason |
|---|---|
| SiliconFlow | no seed, no `structured_outputs` |
| AtlasCloud | no `response_format` or `structured_outputs` |
| BaseTen | no seed |
| Z.AI | no seed |
| StreamLake | cap 128,000, no seed |
| Tenstorrent | cap 131,000 |
| GMICloud | no `structured_outputs`; held on its first 404 |

The registered route file is unchanged, because its hash is part of every checkpoint's binding. The routes in use are therefore Morph, Novita, Near AI and Inceptron.

**Funding.** Antonio must raise the key's total limit. Until then the restarted run waits and probes once a minute; it does not stop.

**Ledger authority.** Project master now carries a change to `src/chl_rag/v2_budget.py`, from the two-judge oracle work (7f819c7, a493f9d). Checked against the main working tree, it fails the runtime hash of the signed SQLite authority. This run imports its code only from the 08A checkout, where the four reviewed accounting files are byte-identical to the reviewed ones. The authority check therefore runs against that checkout, which is the code that actually executes. The ledger database and its JSON export stay in the main tree. Any other OpenRouter run from master will fail the authority check until that change is reviewed and registered.

**Frozen-input check.** The same master change also affects `src/chl_rag/v2_judges.py` and `src/chl_rag/v2_reference_oracles.py`, which the v2 configuration binds as frozen code. The check therefore works as follows:
- **Tracked inputs** are verified as one reviewed set in the 08A checkout. Its code is the code that runs, and it equals the reviewed bytes.
- **Tracked configurations** that are read at runtime from the main tree must also be byte-identical to the verified copy.
- **Untracked data**, such as the corpus and PubTator, is verified in the main tree.

The restart reconciliation archived 480 blocked attempts (408 router, 72 key-limit) and left none blocked.

## Note 2: gate for the classifier effort of addendum 08C (16 November 2026, about 03:10 CET)

Addendum 08C (master 07ed274) chooses the corpus classifier effort (low, high or max) from the oracle agreement on the 35 pairs. The orchestrator started at 02:46 was already running with only the low classifier, and it is not interrupted: extraction and the frame continue unchanged. Instead:

- **Second process.** A second detached process, `chl-v21-corpus-classify` (action `classify-gated`), holds the classification lock (`results/v2/production_v21/classification_full.lock`) from its start. When the first orchestrator reaches classification after the frame, it cannot take the lock and ends without classifying a single pair.
- **What it waits for.** The second process waits until all of these are true:
  1. the frame is complete;
  2. the first orchestrator has ended;
  3. a decision file exists at `configs/v21_corpus_classifier_effort.json` in the main repository, with these properties:
     - committed in a commit with a good signature (G), with no uncommitted change;
     - `effort` in low, high or max;
     - `addendum` "08C";
     - a 64-hex `decision_sha256`.

  Any other state keeps it waiting.
- **Classification settings.** It classifies with the decided effort. The cap stays 65,536 for low and is 131,072 for high and max. Everything else is unchanged: prompt, routes, workers, rejection rules, the single identical retry and the ledger. The classifier binding records the 08C decision and its commit.
- **Funding.** Before classifying, it compares a fresh balance with the projection for the decided effort (pairs × measured 35-pair cost / 35 × 1.5). If the balance is short, it waits for a top-up and checks again every two minutes. The effort is never lowered.
- **Freezing.** It then freezes the frame. Status: `results/v2/production_v21/status_classify.json`.

## Note 3: addendum 08D, the uncapped frame (16 November 2026, 04:31 CET)

**The frame** was built offline by `src/chl_rag/v21_uncapped.py` from the same v2.1 normalized claims as the capped frame, with the same retrieval cache (0 misses, 545,700 retrieved document pairs). There are no caps and no within-key sampling.

- **Size.** 42,972 candidate pairs. All 11,092 capped pairs are included, and 31,880 are new.
- **By origin:**

  | Origin | Pairs |
  |---|---|
  | Key route only | 13,564 |
  | Retrieval only | 18,058 |
  | Both | 11,350 |

  The key route contributes 24,914 pairs in all.
- **By arm:** drug 41,347, biomarker 818, diet 807.
- **By sign pattern:** concordant 30,122, effect versus null 8,400, opposite 2,991, unrepresented 1,459. Following the addendum's "every sign combination", the key route includes the combinations with an unrepresented sign on one side.

The frame is at `data/derived/v2/production_v21_uncapped/full/`, with its summary in `results/v2/production_v21_uncapped/frame_full.json`.

**Classification** is gated in the unit `chl-v21-uncapped`:
- it waits for the capped frame to be frozen;
- it reuses every capped record whose classification identity, which covers the request and the binding, is identical;
- it classifies the rest at the 08C effort (high, cap 131,072), with the 08A routes and rules.

**Funding.** The funding check projects the measured corpus cost per pair of the capped high run (0.0011 USD at 04:32) × 1.5. The 35-pair measurement (0.0016 USD per pair) would overstate it.

The final counts, cost and wall time are added to `results/v2/production_v21_uncapped/summary.json`.

## Note 4: stop of the capped classification at 05:00:48 and restart (16 November 2026)

**What happened.** The 08C-gated classification (effort high) stopped with 10,247 of 11,092 pairs done.
- **Cause.** `httpx.ConnectError` on the credit read (GET /api/v1/credits) inside the request path. This is a transient network failure. It escaped as an exception, stopped admission at about 04:44, and failed the stage once the requests in flight had drained.
- **Generation.** The failure happened before any request was sent, so nothing was generated.

**Fix** (`src/chl_rag/v21_corpus.py`):
- **Credit read.** A network failure or non-200 on the credit read is retried with backoff and never stops the run.
- **Connection failures.** A connection that never opened (ConnectError, ConnectTimeout, PoolTimeout) is retried without limit by the outermost HTTP wrapper, for POST and for GET. A model request is therefore never counted as an attempt before it is sent.
- **Balance read.** The fresh-balance read retries too.
- **Unchanged:** a timeout after sending, and any response, keep the existing attempt rules.

**Restart.**
- Attempts left `started` by the stopped stage are archived in `interrupted_history`, and the source restarts that attempt.
- An interrupted request keeps its unknown-billing liability in the ledger, and a cached response is reused.
- Completed pairs are never sent again.
- The 08D unit is restarted with the same code. It had not spent anything yet.
