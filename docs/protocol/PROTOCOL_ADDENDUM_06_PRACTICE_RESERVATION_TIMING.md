# Operational allocation note: reserve real reads after practice

Authority: INBOX_FROM_CLAUDE.md2026-11-14 12:40, Antonio's operational decision, deployed by Claude as site8600c76 with545 tests.

With practice enabled, Start now reserves only the practice item. Admission still checks that ADMISSION_MIN_READS distinct real pairs are available. The real CONTINUE_BATCH of5 is reserved when practice feedback is acknowledged. Sessions admitted earlier retain their existing reservations. The purpose is to prevent visitors who only inspect the site from holding real-read slots.

This changes reservation timing only. It does not change C, the sampled human subset, reads per pair, slot eligibility, third/fourth-read rules, QC, payment or sampling/analysis rules. The nonsecret configuration snapshot hash remains65c3f4255f61d46697c484733c5adec1c99008b46b2f90d90057f5f09c91023a, so the signed collection lock remains valid. The change is not a new reference sample or a model intervention. No participant records were read by this agent to implement this note.
