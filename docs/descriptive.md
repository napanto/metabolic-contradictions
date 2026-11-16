# Descriptive corpus analyses (report and slides)

`src/chl_rag/descriptive.py` describes the classifier's **predictions** on the whole corpus. Only the 35-pair sample has human and oracle validation, so every figure and number here is descriptive. It reads only frozen frames; it never reads a partial run, an oracle or human answer, or sample membership. It makes no model call.

## Commands (from this checkout, data read from the main project tree)

```bash
P=.
PYTHONPATH=src .venv/bin/python -m chl_rag.descriptive --data-root $P analyze --scope uncorrected    # frozen 14 Nov corpus
PYTHONPATH=src .venv/bin/python -m chl_rag.descriptive --data-root $P analyze --scope system   # after the corrected run is frozen
PYTHONPATH=src .venv/bin/python -m chl_rag.descriptive --data-root $P compare --before uncorrected --after system
```

`analyze` refuses a frame whose status is not `complete_frozen` and checks the sha256 of every frozen file.

## Outputs

- `report/generated_descriptive/<scope>/results.json` and `macros.tex` (`\Desc...`), plus `compare/` (`\DescCmp...`).
- `figures/descriptive/<scope>/<figure>_{slide,report}.{pdf,png}`. The slide variant is sized for 16:9 beamer; the report variant fits one column, or both columns for `pipeline` and `network`.

| Figure | What it shows |
|---|---|
| `pipeline` | Hero numbers: abstracts → extracted results → candidate pairs (by key, retrieval, both) → flagged pairs |
| `categories` | Predicted category shares per arm (100% bars, n per arm) |
| `apparent_factors` | Context factor the classifier gives for apparent contradictions |
| `design_mismatch` | Share of pairs whose two studies have different coarse designs, per category |
| `contested_topics` | Top 5 exposure → outcome topics by flagged pairs, per arm |
| `network` | Ten largest topics per arm; edge shade = share flagged, width = candidate pairs |
| `compare/correction` | Before and after the extraction correction: share of "no difference" results and flagged pairs per arm |

## Definitions

- **Flagged:** contradictory, apparent contradiction or not replicated. The denominator is valid classifications.
- **Topic:** the pair key, i.e. arm, exposure key and outcome key. Its display name is the most frequent normalized label; when that label is a gene symbol (for example INS), the most frequent extracted wording is used instead.
- **Design classes:** coarse keyword classes from the extracted design text (`design_class`).
- **Example pair per arm:** taken from the most contested topic, contradictory first, then in digest order (seed 20261116). It is chosen by a fixed rule, not by hand.
- **Cost and time:** taken from the OpenRouter ledger export (`results/openrouter_spend.json`).
  - The earlier corpus uses a fixed window on 14 November. The corrected run uses its status `started_at` up to `frozen_at`.
  - Only known costs are summed. Calls with unknown billing are counted, not imputed.
- **Comparison:** abstract pairs are matched by arm and the two PMIDs, because claim and pair IDs differ between runs.

## Caveats to keep in the text

- The figures show predictions, not validated contradictions or clinical truth.
- Most candidate pairs are in the drug arm, so totals reflect drugs.
- The corpus covers 2016–2026, two thirds of it since 2020. Publication-year gaps within pairs are therefore small by construction and say little.
- In the 14 November corpus, design differences are about as common in flagged pairs as in concordant ones, about 50% in both.
