# What this repository leaves out, and why

This is a clean, single-commit export of the project's private working repository. It was built with an explicit allowlist.

**Development history.**
- The working repository has several hundred commits. Its development paths were abandoned before the final design and are not part of the delivered method:
  - extractor and classifier selection experiments;
  - calibration rounds;
  - reduced model grids;
  - withdrawn configurations;
  - staging copies;
  - development-era design notes: the production contract, the package format and the original instrument notes.
- The protocol documents that registered those decisions are kept in `docs/protocol/`, because the final system's deviations are declared against them.
- A few kept modules still contain code paths from that period. They are imported by the final components.

**Tests.**
- **Not included:** two test files of the earlier single-judge oracle guards. They exercise the private development record.
- **Kept but skipped:** `test_reference_oracles` stays only as a fixture module for the two-judge tests.
- **Skipped:** `tests/conftest.py` skips the tests that need the study-pair package, because it contains abstract text.

**Names.**
- The working repository labels its iterations with internal version tags and site-commit suffixes. Here files, modules, configurations and directories use simplified names; the mapping to the original paths and their hashes is kept in the private archive.
- Recorded file hashes inside the configurations and results are rebound to the renamed files, so the tree verifies against itself.
- The registered protocol record in `docs/protocol/` keeps the internal labels verbatim.
- Item identifiers, such as `v2-pair-1043`, and schema version fields are data and keep their original values.

**The prototype.** Its pipeline and data are omitted. Only its frozen oracle evaluation summary is kept, as a baseline.

**Report and slides.** They are not distributed here.

**Abstract text.**
- **Omitted:** corpus snapshots with full abstracts, the study-pair packages and the oracle request files, all of which embed both abstracts.
- **Kept instead:** PMID lists, hashes, the fetch script and per-pair outputs.

**Private participant data.** None of the following are included:
- answers, sessions, participation codes or payments from the study site;
- the private analysis directory;
- private sampling files;
- the private prototype-baseline companion;
- the author's own annotations.

Only aggregated human results are included.

**Accounting and caches.**
- Raw ledger rows and model request caches are omitted.
- Logs are omitted.
- Credentials were never part of either repository.
- Cost totals per vendor and phase are in `results/cost_totals.json`.

**Deployment.** Hosts, addresses and the operational configuration of the study site are omitted. In a few kept records, the site's service name and server data paths are replaced by neutral placeholders; those files therefore hash differently from their private originals (they are marked `sanitized` in `EXPORT_MANIFEST.json`). The site's code is a separate repository.
