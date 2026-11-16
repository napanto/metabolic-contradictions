# Addendum 08A: the corpus of the system (v2.1 extraction) on the complete F

Registered on Monday 16 November 2026, before any request of this run. Authority: Antonio's explicit go at about 01:40 CET, relayed by Claude: «hai il mio via libera per far partire estrazione e classificazione su corpus intero». His instructions of about 01:45, also relayed by Claude, set:
- maximum parallelism;
- every eligible fp8 route;
- the retry rule for rejections;
- no OpenRouter batch.

## What runs

This run produces the corpus of «the system»: the v2.1 extraction, which is the one the students and the oracles judge. The v2 corpus outputs of 14 November stay unchanged as historical records. They are the corpus of the extraction prompt before its correction.

- **Sources.** The complete original F partition, 14,218 sources, in the registered production order. The 132 sources that failed on 14 November get a fresh attempt, like every other source.
- **Extraction.**
  - GLM-5.3 Flash (`z-ai/glm-5.3-flash`), reasoning high, cap 131,072 tokens, temperature 0, seed 42.
  - The prompt is the frozen v2 extraction prompt plus the v2.1 addition, with the wire schema that generates `result.assertion` last. This is byte-identical to the 14 November re-extraction of the 66 human-pair abstracts (`scripts/v21/order_test.py`, arm `order_prompt3`). Five of its cached request bodies were compared with this run's construction: system message, user message, schema key order, effort and cap are all identical.
  - One pass, no verifier.
  - The hashes are in `configs/evaluation_v21_corpus.json`.
- **Frame.** Unchanged: normalization with PubTator, entity-key pairing plus the hybrid MedCPT/BM25 retrieval proposals (k = 21, 20 neighbours), caps of 30 inconsistent and 5 concordant per key. The frozen index is used read-only. The retrieval query cache is shared, because its entries depend only on the frozen index and the query text.
- **Classification.** Unchanged: GLM-5.3 Flash reasoning low, cap 65,536, with the frozen classification prompt and both full numbered abstracts. No high-reasoning corpus classification is part of this run.
- **Outputs.** New directories only: `data/derived/v2/production_v21/` and `results/v2/production_v21/`, with their own locks and a frozen manifest at the end. No v2 output, signed configuration, frozen frame or sample C is modified.

## Routes and parallelism (Antonio's instructions)

- **Catalogue snapshot.** The public OpenRouter endpoint catalogue for `z-ai/glm-5.3-flash` was saved right before launch (`configs/provider_snapshots/glm-5.3-flash_endpoints_20261116T004830Z.json`).
- **Eligible routes.** A route must meet every condition:
  - fp8 only;
  - status 0;
  - `max_completion_tokens` of at least 131,072;
  - support for reasoning, `response_format` or `structured_outputs`, and seed;
  - the same model version as the registered routes (`z-ai/glm-5.3-flash-20260826`).

  fp4, nvfp4 and unknown quantizations are excluded. Tenstorrent stays excluded: its cap is 131,000 and it rejected most requests on 14 November. Five routes are eligible: GMICloud, Near AI, Novita, Morph and Inceptron. Every one is used.
- **Request rules.** Each request is pinned to one route, with no fallback and `require_parameters`. The provider is recorded per call. Price ceilings are twice the listed price, as in v2.
- **Concurrency.**
  - The adaptive per-provider cap starts at 128 instead of 64.
  - It halves on a 429, a 5xx burst or rising unknown billing, and recovers by 32 after a 30-second clean interval.
  - 640 controller workers for extraction and 640 for classification.
  - The CPU retrieval prefetch runs at the same time as extraction.
- **Funding.** Per-request worst-case reservation against the real OpenRouter balance, floor 0, as in v2. A fresh balance is read before extraction and again before classification. A shortfall stops the run for a top-up; it never reduces the scope.

## Attempts and rejections

- **Rejections are not attempts.** A rejection before generation is retried with backoff, on the same route or another eligible one, until it is accepted. This covers a 429, a 5xx, a provider refusal (a known-free 4xx) and a connection that never opened. A route with five consecutive provider refusals is held for the rest of the run, and the hold is recorded.
- **Invalid output.** An invalid or truncated output keeps the single identical retry on the same provider. So does a transport failure after the request was sent, whose billing is unknown.

## Not done

There is no OpenRouter batch. `z-ai/glm-5.3-flash:batch` is served only by fp4 or unknown-quantization providers, has no cancellation, and returns results only when the whole batch completes. It would not be the same system.
