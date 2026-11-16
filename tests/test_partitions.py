from copy import deepcopy

import pytest

from chl_rag.partitions import construct, duplicate_groups, identifier_pmids


def test_exact_source_duplicates_quarantine_all_linked_members():
    rows = [{"pmid": "1", "doi": "https://doi.org/10.1/ABC", "abstract": "First"},
            {"pmid": "2", "doi": "doi:10.1/abc", "abstract": "Second"},
            {"pmid": "3", "doi": None, "abstract": " SECOND "},
            {"pmid": "4", "doi": None, "abstract": "Distinct"}]
    groups, evidence = duplicate_groups(rows)
    assert groups == [["1", "2", "3"]]
    assert {e["kind"] for e in evidence} == {"doi", "abstract"}
    assert all("First" not in str(e) for e in evidence)


def fixture():
    articles, dev = [], []
    for arm_index, arm in enumerate(("biomarker", "drug", "diet")):
        for i in range(70):
            pmid = str(10000 + arm_index * 100 + i)
            articles.append({"pmid": pmid, "arms": [arm], "design_eligible": True,
                             "doi": None, "abstract": "Unique synthetic source " + pmid})
            if i < 10:
                dev.append({"pmid": pmid, "arm": arm})
    return articles, dev


def test_partitions_reproducible_disjoint_exclude_sources_and_adjust_counts():
    articles, dev = fixture()
    old = [{"task": "pair", "a": {"article": {"pmid": "10010"}},
            "b": {"article": {"pmid": "10110"}}}]
    audit = {"pmids": ["10210"], "rationale": "10011 should not be interpreted as an ID"}
    result = construct(articles, dev, old, audit, preview_pmids=["10011"])
    assert result == construct(list(reversed(articles)), dev, old, audit, preview_pmids=["10011"])
    assert result["counts"]["S"] == 36
    assert result["counts"]["H"] == 24
    assert sum(result["counts"].values()) == len(articles)
    partitions = list(map(set, result["partition_pmids"].values()))
    for i, left in enumerate(partitions):
        assert all(not left & right for right in partitions[i+1:])
    assert result["partition_pmids"]["preview_additional"] == ["10011"]
    assert result["draws"]["reserve"]  # Short arms are retained without quota transfer.
    assert result["reserve_count"] == result["counts"]["F"]
    assert result["labels_read"] is False


def test_audit_reads_only_explicit_identifiers():
    assert identifier_pmids({"pmids": [123, "456"], "label": "789", "notes": "111"}) == {"123", "456"}


def test_partitions_reject_changed_original_dev_count():
    articles, dev = fixture()
    with pytest.raises(ValueError, match="30-item"):
        construct(articles, dev[:-1], [], {})


def test_overlap_arm_assigned_once_in_fixed_order():
    articles, dev = fixture()
    articles = deepcopy(articles)
    for row in articles:
        if row["pmid"] == "10169":
            row["arms"] = ["drug", "biomarker"]
    result = construct(articles, dev, [], {})
    source = next(x for x in result["draws"]["reserve"] + result["draws"]["S"] + result["draws"]["H"] if x["pmid"] == "10169")
    assert source["arm"] == "biomarker"
