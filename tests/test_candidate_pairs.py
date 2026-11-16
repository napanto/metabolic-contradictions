import json
from copy import deepcopy
from pathlib import Path

from chl_rag.extraction import flatten_extraction, validate_extraction
from chl_rag.candidate_pairs import (
    baseline_sign,
    classifier_payload,
    compatible_key,
    generate_candidates,
    make_pair,
    normalize_record,
)

FIXTURES = json.loads(Path("configs/synthetic.json").read_text())["cases"]


def records(case=0):
    source = deepcopy(FIXTURES[case]); other = deepcopy(source)
    other["article"]["pmid"] = "899999997"
    articles, extractions, rows = {}, {}, []
    for item in [source, other]:
        article = item["article"]
        extraction = validate_extraction(article, item["expected_model_output"])["extraction"]
        articles[article["pmid"]] = article; extractions[article["pmid"]] = extraction
        rows.extend(normalize_record(r) for r in flatten_extraction(extraction))
    return articles, extractions, rows


def test_baseline_preserves_assertion_vs_estimate_and_unclear_is_missing():
    claim = deepcopy(FIXTURES[4]["expected_model_output"]["claims"][0])
    assert claim["result"]["estimate_direction"] == "-"
    assert baseline_sign(claim) == "0"
    claim["result"]["assertion"] = "directional"
    assert baseline_sign(claim) == "-"
    claim["result"]["assertion"] = "unclear"
    assert baseline_sign(claim) is None


def test_normalization_preserves_every_canonical_field_and_comparison_type():
    _, extractions, rows = records()
    for row in rows:
        assert row["claim"] in extractions[row["pmid"]]["claims"]
        assert row["pair_key"].endswith("|comparison:within_group_change")


def test_candidates_reject_within_vs_between_but_keep_context_differences():
    _, _, rows = records()
    a, b = rows[0], rows[2]
    assert compatible_key(a, b)
    modified = deepcopy(b); modified["comparison_type"] = "between_group"
    assert compatible_key(a, modified) is None
    modified = deepcopy(b); modified["claim"]["context"]["dose"] = "different dose"
    pair = make_pair(a, modified)
    assert any(row["field"] == "context" for row in pair["context_differences"])


def test_no_self_source_pairs_and_no_hedged_or_unclear_comparison():
    _, _, rows = records()
    a, b = rows[0], rows[2]
    same = deepcopy(b); same["pmid"] = a["pmid"]
    assert compatible_key(a, same) is None
    hedged = deepcopy(b); hedged["hedged"] = True
    assert compatible_key(a, hedged) is None
    unclear = deepcopy(b); unclear["comparison_identifiable"] = False
    assert compatible_key(a, unclear) is None


def test_generator_is_deterministic_capped_and_classifier_sees_full_source():
    articles, extractions, rows = records()
    pairs, summary = generate_candidates(rows)
    again, _ = generate_candidates(list(reversed(rows)))
    assert pairs == again
    assert summary["candidate_pairs"] == 2
    payload = classifier_payload(pairs[0], articles, extractions)
    assert set(payload) == {"pipeline_version", "a", "b"}
    assert len(payload["a"]["article"]["sentences"]) == 2
    assert "comparison" in payload["a"]["claim"]
    assert "sign_pattern" not in json.dumps(payload)


def test_retrieval_proposals_do_not_bypass_comparison_checks():
    _, _, rows = records()
    changed = deepcopy(rows[2]); changed["comparison_type"] = "between_group"
    changed["pair_key"] = changed["pair_key"].replace("within_group_change", "between_group")
    pairs, _ = generate_candidates([rows[0], changed], retrieved_document_pairs=[(rows[0]["pmid"], changed["pmid"], .8)])
    assert not pairs


def test_classifier_request_has_no_pair_proposal_leakage_and_preserves_source():
    from chl_rag.candidate_pairs import classification_request
    articles, extractions, rows = records()
    pair = make_pair(rows[0], rows[2])
    pair['label'] = 'contradictory'
    request = classification_request(pair, articles, extractions, model='synthetic', reasoning='max')
    payload = json.loads(request['messages'][1]['content'])
    assert 'label' not in payload and 'sign_pattern' not in payload
    assert payload['a']['article']['abstract']
    assert payload['a']['claim']['comparison']['type'] == 'within_group_change'


def test_classifier_requires_material_errors_and_opposition_to_remain_distinct():
    import pytest

    from chl_rag.candidate_pairs import validate_classification
    articles, extractions, rows = records()
    payload = classifier_payload(make_pair(rows[0], rows[2]), articles, extractions)
    sentence = payload['a']['article']['sentences'][1]
    verdict = {'label': 'not_comparable', 'factor': 'none', 'rationale': 'Synthetic material-error fixture',
               'diagnostics': {'material_extraction_error': True, 'error_sides': ['a'],
                               'error_fields': ['comparison'], 'uncertain_extraction': False,
                               'comparison_sufficient': False,
                               'source_evidence': [{'side': 'a', 'sentence_id': sentence['id'], 'quote': sentence['text']}],
                               'notes': ''}}
    assert validate_classification(payload, verdict) == verdict
    verdict['label'] = 'contradictory'
    with pytest.raises(ValueError, match='Material extraction error'):
        validate_classification(payload, verdict)
    verdict['diagnostics']['material_extraction_error'] = False
    with pytest.raises(ValueError, match='sufficient'):
        validate_classification(payload, verdict)
    verdict['diagnostics']['comparison_sufficient'] = True
    verdict['label'] = 'apparent_contradiction'; verdict['factor'] = 'population'
    with pytest.raises(ValueError, match='both sources'):
        validate_classification(payload, verdict)
