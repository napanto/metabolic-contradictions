# Context-aware inconsistency mining in metabolic literature

Computational Health Laboratory project, University of Pisa, 2026.

This project finds claims that disagree across PubMed abstracts on type 2 diabetes, metabolic syndrome and obesity. It then checks whether the study context explains each disagreement. Three areas are covered: biomarkers, drugs and diet.

The repository holds the final system and its validation. It has a single commit. The development history and private participant data are not included; `OMITTED.md` lists what was left out and why.

## What the system does

1. **Extraction.** GLM-5.3 Flash, at high reasoning, turns each abstract into structured findings: exposure, outcome, comparison type, direction, population and groups, design, sample size, statistical evidence, and the cited sentences. The extraction prompt was corrected before the human study, after a systematic check found false «no effect» findings. The test is in `results/extraction_correction/`.
2. **Normalization.** No model is involved. Exposures and outcomes become canonical keys, through PubTator entities where they exist. Directions become signs.
3. **Pairing.** No model proposes pairs. Two findings can pair only if they come from different abstracts and share area and comparison type. Candidate pairs come from three routes:
   - **identical keys:** the same exposure key, or a drug class and its member, and the same outcome key;
   - **hybrid retrieval:** MedCPT dense plus BM25, fused by reciprocal rank, over the 20 nearest abstracts;
   - **semantic matching** (addendum 08E): exposure and outcome strings are embedded separately with the MedCPT query encoder and searched exhaustively with FAISS. Both cosines must reach τ* = 0.88, a budget parameter fixed before any of these pairs was classified.

   A pair found by several routes is kept once.
   - **Key and retrieval routes:** classified in full (42,972 pairs).
   - **Semantic route:** classified in a fixed hash order until its budget ran out. That covers the 1,500 concordant controls, all 9,800 new opposite pairs, and a seeded random sample of 15,229 of the 20,298 new effect-versus-null pairs. The 31,802 pairs of the band 0.85–0.88 are not classified.
4. **Classification.** GLM-5.3 Flash, at high reasoning, reads both full abstracts and both findings. A registered rule chose high reasoning from the judges' answers. The study pairs were also classified at low and maximum reasoning. The classifier labels each pair as concordant, contradictory, apparent contradiction explained by context, not replicated, or not comparable. It also gives the contextual factor and a rationale.
5. **Validation.**
   - **Human study:** students judged 35 flagged pairs on a dedicated site, whose code is published as [evidiff](https://github.com/napanto/evidiff) (AGPL-3.0). Pairs were assigned to maximise coverage, with consensus by majority, and third and fourth reads after disagreements.
   - **Two oracles:** two blind LLM judges from different families, GPT-6 Astra and Claude Fable 5.2, both at maximum effort, answered the same instrument.
   - **Registration:** every rule was registered before outcomes were seen. The record is in `docs/protocol/`.

The earlier pipeline, "the prototype", appears only as a baseline: its frozen oracle evaluation is in `results/prototype/`.

**Version labels.** File names, identifiers and the protocol keep the internal labels of the project: *v1* is the prototype; *v2* is the final system, with its study site and instrument (hence pair identifiers such as `v2-pair-1001`); *v2.1* is the final system with the corrected extraction prompt, whose corpus is the one reported.

## Layout

| Path | Content |
|---|---|
| `src/chl_rag/` | The system: extraction, normalization, pairing, classification, the two-judge oracle, human analysis, descriptive analyses |
| `scripts/` | Entry points: corpus run, study-pair rebuild and classification, oracle run, analysis helpers |
| `configs/` | Frozen prompts, schemas and signed run configurations, the site instrument the oracles answer, the human-study records |
| `docs/protocol/` | The registered protocol, its amendments and addenda, in order |
| `docs/` | Method notes for the final components |
| `results/` | Aggregated results: oracle and classification outputs per study pair, descriptive corpus results, human-analysis results, cost totals |
| `data/` | Frozen PubMed identity (PMID lists, membership, query hashes), the partition, ManCon provenance; the corpus of the system is included in `data/corpus_system/` |
| `tests/` | Tests for the kept components, with synthetic fixtures only |

## Reproducing

```bash
make setup      # Python 3.12 with uv; CPU-only PyTorch
make test
make corpus     # re-fetch the frozen PubMed records (network; refuses drift)
make models     # pinned public MedCPT weights
make index      # CPU dense and sparse index
```

**What runs where:**
- **Free and local.** The analyses run locally on the outputs in this repository.
- **Paid model calls.** Extraction, classification and the oracles need credentials and cost money. The OpenRouter ledger records about 75 USD for corpus extraction and about 81 USD for classification, of which 29.84 USD went to the semantic route. The oracles cost under 50 USD. They read keys only from files outside the repository.
- **Integrity guards.** Before any paid call, the runners check the hashes of the complete private development record. In this repository those guarded stages are provided for reading and audit; re-executing them requires the original archive.
- **Human analysis.** It needs the private export of the study site ([evidiff](https://github.com/napanto/evidiff)), which is not distributed. The tests run it on synthetic data.

## Licensing

- **Code** (`src/`, `scripts/`, `tests/`, build files): MIT, see `LICENSE`.
- **Documentation, the protocol record and our own derived results** (extracted claims, classifications, oracle outputs, aggregated human results, descriptive results): CC BY 4.0, see `LICENSE-CC-BY-4.0`.
- **Third-party material** keeps its own terms, see `THIRD_PARTY.md`. PubMed abstract text is not redistributed.

## Citation

See `CITATION.cff`.
