# Addendum06 operational note: controller concurrency

At Antonio’s07:35 instruction, after an admission pause and bounded drain, restart full-F extraction with controller concurrency256 instead of128. The30-minute preflight records1086 known calls and27 unknown events (conservative problem fraction2.43%), no provider error response or recorded429; see results/v2/production/concurrency_256_preflight.json. Return to128 if provider/429 errors rise above approximately5%. Model, request bytes/hash, retry limits and actual route constraints stay unchanged.

The registered hard cap16 simultaneous requests per provider is retained. Four frozen endpoints support131072 output tokens, so extraction can have at most64 real model POSTs, regardless of controller256. The fifth endpoint supports131000 and cannot take extraction requests. GLM-low classification may use all five endpoints, at most80 model POSTs under the same cap. STATUS asks Antonio to clarify any desired increase of the provider cap rather than silently changing that registered limit.

The funding pause is maintenance only, cleared after the old controller exits; floor0 and full-credit use remain. The independent Astra hold remains. No completed checkpoint is repeated.

This companion leaves the signed SQLite migration authority and its original protocol bytes immutable. The migration database binds their exact original identity. Initial attempts to attach this note inside that authority were rejected before model dispatch; the unchanged authority is restored in the signed follow-up. No request or checkpoint was changed.
