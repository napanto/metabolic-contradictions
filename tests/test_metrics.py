import pytest

from chl_rag.metrics import (
    binary_metrics,
    clustered_bootstrap,
    cohen_kappa,
    paired_cluster_bootstrap,
    wilson,
)


def test_metrics_known_confusion():
    m = binary_metrics([1, 1, 0, 0], [1, 0, 1, 0])
    assert m["confusion"] == {"tp": 1, "tn": 1, "fp": 1, "fn": 1}
    assert m["f1"] == m["accuracy"] == m["precision"] == .5
    with pytest.raises(ValueError):
        binary_metrics([1], [1, 0])


def test_wilson_empty_and_extremes():
    assert wilson(0, 0) is None
    assert wilson(0, 10)[0] == 0
    assert wilson(10, 10)[1] == 1
    assert wilson(5, 10) == pytest.approx([0.2365930905, 0.7634069095])


def test_cluster_bootstrap_preserves_clusters_and_is_repeatable():
    result = clustered_bootstrap([1, 0, 1, 0], [1, 0, 1, 0], ["a", "a", "b", "b"], 200)
    assert result["n_clusters"] == 2
    assert result["intervals"]["f1"] == [1, 1]
    assert result == clustered_bootstrap([1, 0, 1, 0], [1, 0, 1, 0], ["a", "a", "b", "b"], 200)


def test_paired_bootstrap_identical_systems():
    result = paired_cluster_bootstrap([1, 0, 1, 0], [1, 0, 0, 1], [1, 0, 0, 1], ["a", "a", "b", "b"], 200)
    assert result["ci95"] == [0, 0]


def test_kappa_chance_and_undefined():
    assert cohen_kappa(["a", "b", "a", "b"], ["a", "a", "b", "b"])["kappa"] == 0
    assert cohen_kappa(["a", "a"], ["a", "a"])["kappa"] is None
