# Financial authorization update, 14 November2026

Authority: Antonio’s INBOX03:36 top-up confirmation and03:38 clarification. This supersedes the15USD floor and all lower phase/account alert thresholds in the earlier financial policy. The confirmed top-up is50USD; a fresh read-only account check recorded150USD total credit and101.524444914USD remaining before resumed dispatch.

Antonio explicitly authorizes the entire real available OpenRouter balance for the fixed production. There is **no fixed floor, unused account margin, phase cap or account alert below that balance**. Only the per-request worst-case reservation remains to prevent requests beyond available credit. Actual usage continues to settle against that reservation. Before classification, project its cost on the actual completed candidate frame; if that cannot fit the then-current real balance, pause and report, without reducing scientific scope.

`configs/v2_funding.json` is the mutable operational funding authority and records this update. The frozen scientific configuration, model/effort, schemas, prompts, source F, seeds, output caps and completed checkpoints are unchanged. Resumption reuses completed outputs and preserves blocked preflight attempts without treating them as extra model generations.

The Astra hold is unchanged. A funding confirmation is not permission for an Astra pilot or oracle; that still requires Antonio’s explicit go and the separate signed release lock. No Anthropic call is reauthorized.
