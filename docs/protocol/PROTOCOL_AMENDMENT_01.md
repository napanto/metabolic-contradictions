# Amendment 01 — before any LLM evaluation

2026-11-12. The baseline agent read Xie et al. 2024, pp. 1554–1555, and identified that their ternary question-answer rule merges **neutral with no** before deriving pair labels. The original protocol's yes/no-only opposition rule would therefore not reproduce that method.

For the question-aware LLM setting, use yes versus (no or neutral): a pair is inconsistent exactly when one claim answers yes and the other answers no/neutral. Report the neutral rate. Preserve the binary question-free task and all non-LLM baselines unchanged. This is a prospective correction before model selection or any paid LLM calls, and will be disclosed as an amendment in the report. The original signed protocol is retained unchanged; no test performance motivated this correction.

Corpus scope check: Du 2016 PMID 27300472 is absent from the exact query because its disease MeSH indexing does not include the three BASE diseases. Keep it as an external reference; evaluate eligible primary irisin studies, with the missing review explicitly reported. Do not expand the corpus post hoc.
