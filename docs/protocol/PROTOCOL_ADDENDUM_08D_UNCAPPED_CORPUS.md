# Addendum 08D: all candidate pairs of the registered rules, without caps

Registered on Monday 16 November 2026 at 04:24 CET, by Antonio's decision: «parti con il primo gruppo di miglioramenti all'algoritmo che tu ritieni di fare subito e fai classificare anche le nuove coppie». It extends addendum 08A and keeps the classifier effort chosen under 08C. Antonio is topping up the OpenRouter credit by 50 USD for it.

## Change

The corpus frame of addendum 08A samples candidate pairs per key:
- at most 30 opposite or effect-versus-null pairs and 5 concordant or unrepresented pairs per key;
- within a key, a seeded sample when the population is large.

From this addendum on, nothing is sampled and nothing is capped:
- **Key route.** Every pair of compatible claims from different abstracts that shares a key, for every sign combination. Compatibility follows the registered rules unchanged: domain, comparison type, exposure (the same key, or class and member) and outcome.
- **Retrieval route.** Every claim pair proposed from the 20 retrieved neighbours per query, with the registered looser outcome match, before any final cap.
- **Deduplication** of pairs found by both routes, keeping both origins.

Normalisation, retrieval, neighbours, exclusions (hedged claims, unclear comparison type) and the extraction stay exactly as in 08A.

## Classification

- **Classifier.** GLM-5.3 Flash at the effort chosen under 08C (high), with the frozen classification prompt, the fp8 routes, the retry rule and the rejection handling of 08A.
- **Reuse.** A pair already classified in the capped 08A run with a byte-identical request is reused, never re-sent.
- **Funding.** If the projected cost exceeds the fresh balance, the run waits for a top-up. The effort is never lowered.

## Outputs and wording

- The uncapped frame becomes the primary corpus output. The capped frame stays recorded as a subset of it.
- The study pairs and their validation are unaffected.
- **Wording in the report:** these are all the candidate pairs that the registered rules can see. They are not all potentially contradictory pairs in the literature: pairs missed by normalisation, by retrieval or by extraction stay outside, and recall remains unmeasured.
