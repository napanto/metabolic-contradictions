"""Skip tests that need material not distributed in the delivery repository."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "annotation/study_pairs/items.jsonl"
NEEDS_PACKAGE = (
    "tests/test_human_analysis.py",
    "tests/test_site_instrument.py::test_every_frozen_handoff_pair_renders_and_prepares",
)


# Test modules that read the study-pair package at import time.
collect_ignore = [] if PACKAGE.exists() else ["test_oracle_revalidation.py"]

# Kept as a fixture module for the two-judge tests; its own tests exercise the
# single-judge guard chain, which is bound to the private development record.
DEVELOPMENT_RECORD = ("tests/test_reference_oracles.py",)


def pytest_collection_modifyitems(config, items):
    record = pytest.mark.skip(reason="exercises the private development record (see OMITTED.md)")
    for item in items:
        if any(item.nodeid.startswith(n + "::") for n in DEVELOPMENT_RECORD):
            item.add_marker(record)
    if PACKAGE.exists():
        return
    marker = pytest.mark.skip(reason="needs the study-pair package with abstract text, not distributed "
                                     "(see OMITTED.md); restore it to run these tests")
    for item in items:
        if any(item.nodeid == n or item.nodeid.startswith(n + "::") or item.nodeid.startswith(n + "[")
               for n in NEEDS_PACKAGE):
            item.add_marker(marker)
