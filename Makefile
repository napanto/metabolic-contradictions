.PHONY: setup test corpus models index

setup:
	uv sync --frozen

test:
	PYTHONPATH=src uv run pytest -q

# Re-fetch the frozen PubMed records from NCBI (network; refuses query or PMID drift).
corpus:
	PYTHONPATH=src uv run python -m chl_rag.corpus fetch

# Pinned public MedCPT weights, then the CPU dense + sparse index.
models:
	PYTHONPATH=src uv run python scripts/download_medcpt.py

index:
	PYTHONPATH=src uv run python -m chl_rag.index


