# Prospective clarification of the six-category classifier metric

13 November2026, before any metabolic classifier-development reference label or classifier prompt-smoke result. This corrects the raw-five-versus-six comparison specified in the staged expansion addendum; no observed classifier score motivated the change. Independent review approved the mapping as a direct interpretation of the classifier's own explicit output.

The raw classifier still emits the registered five labels and a separate `diagnostics.material_extraction_error` boolean. For the six-category accuracy and macro-F1 tie-breaks in decisions§14, derive:

- `extraction_error` if and only if raw label is `not_comparable` and `material_extraction_error` is true;
- the unchanged raw label otherwise.

Validation continues to reject material-error=true combined with any other raw label. The reference label never enters this transformation. No diagnosis is invented, no rationale is rewritten and no oracle information fills a missing prediction. Keep the raw five-label output, diagnostic and derived six-label prediction in every record.

Binary true-flag status, its F0.5 and recall gate are unchanged because both `not_comparable` and `extraction_error` are negative. Report the system as a five-label head plus an explicit material-error diagnostic evaluated through a declared six-category mapping, not a native six-label classifier. Preserve raw-five-label and collapsed-reference-five-label sensitivity results. The original staged raw-five-versus-six policy remains in Git history but is superseded prospectively by this clarification.
