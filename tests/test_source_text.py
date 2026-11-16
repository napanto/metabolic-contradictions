"""Synthetic prospective P3 source policy; no evaluation sources are fixtures."""
from copy import deepcopy

import pytest

from chl_rag.sentences import sentences as legacy_sentences
from chl_rag.source_text import sentences, source_payload


def test_objective_with_direction_words_remains_context_not_observed_result():
    article = {'pmid': 'synthetic', 'abstract': 'We tested if decreased marker A predicts outcome B. Marker A was lower in group X.',
               'sections': [{'label': 'BACKGROUND & AIMS', 'nlm_category': '', 'text': 'We tested if decreased marker A predicts outcome B.'},
                            {'label': 'RESULTS', 'nlm_category': 'RESULTS', 'text': 'Marker A was lower in group X.'}],
               'publication_types': ['Synthetic fixture']}
    original = deepcopy(article)
    legacy = legacy_sentences(article)
    assert legacy[0]['eligible'] is True
    rows = sentences(article)
    assert rows[0]['eligible'] is False and rows[1]['eligible'] is True
    assert [(r['id'], r['text']) for r in rows] == [(r['id'], r['text']) for r in legacy]
    assert article == original
    assert source_payload(article)['publication_types'] == ['Synthetic fixture']
    assert source_payload(article)['eligible_result_sentence_ids'] == [2]


def test_methods_predicted_change_and_results_observed_change_are_separate():
    article = {'pmid': 'synthetic', 'sentences': [
        {'id': 1, 'section': 'METHODS', 'text': 'We predicted lower fictional marker levels.', 'eligible': True},
        {'id': 2, 'section': 'RESULTS', 'text': 'Fictional marker levels were higher.', 'eligible': True}]}
    assert [s['eligible'] for s in sentences(article)] == [False, True]


def test_explicit_context_cannot_be_forced_into_results_by_external_id_list():
    article = {'pmid': 'synthetic', 'sentences': [
        {'id': 1, 'section': 'OBJECTIVES', 'text': 'To determine whether response increased.'},
        {'id': 2, 'section': 'RESULTS AND DISCUSSION', 'text': 'Response increased.'}],
        'eligible_result_sentence_ids': [1, 2]}
    assert source_payload(article)['eligible_result_sentence_ids'] == [2]


def test_supplied_sentence_only_results_are_supported_without_article_resegmentation():
    article = {'pmid': 'synthetic', 'sentences': [
        {'id': 4, 'section': 'RESULTS', 'text': 'The outcome was lower.'}]}
    assert source_payload(article)['sentences'] == article['sentences']
    assert source_payload(article)['eligible_result_sentence_ids'] == [4]


def test_unbound_unstructured_source_requires_frozen_eligibility():
    with pytest.raises(ValueError, match='Cannot derive'):
        source_payload({'pmid': 'synthetic', 'sentences': [{'id': 1, 'text': 'Changed text.'}]})


def test_quote_equivalence_is_whitespace_only_and_keeps_raw_strings():
    from chl_rag.source_text import quote_match_kind, quote_matches
    source = 'GENE-A\u2009increased\u00a0by 2 µmol/L.'
    quote = 'GENE-A increased by 2 µmol/L'
    assert quote_match_kind(quote, source) == 'whitespace_only'
    assert quote_match_kind('GENE-A', source) == 'exact'
    assert quote_matches(quote, source)
    for wrong in ['gene-A increased by 2 µmol/L', 'GENEA increased by 2 µmol/L',
                  'GENE-A increased by 2 μmol/L', 'GENE-A increased by 3 µmol/L', '   ']:
        assert quote_match_kind(wrong, source) == 'missing'
    assert source == 'GENE-A\u2009increased\u00a0by 2 µmol/L.'


def test_model_display_normalizes_only_whitespace_and_binds_both_views():
    from chl_rag.io import digest
    from chl_rag.source_text import model_source_view
    article = {'pmid': 'synthetic', 'sentences': [
        {'id': 1, 'section': 'RESULTS', 'eligible': True,
         'text': 'GENE-A\u2009increased\u00a0by 2 µmol/L.'}]}
    original = deepcopy(article)
    view = model_source_view(article)
    assert article == original
    assert view['source']['sentences'][0]['text'] == 'GENE-A increased by 2 µmol/L.'
    assert view['provenance']['changed_sentence_ids'] == [1]
    assert view['provenance']['raw_source_sha256'] == digest(source_payload(article))
    assert view['provenance']['display_source_sha256'] == digest(view['source'])
    assert view['provenance']['view_version'] == 'v2-model-source-view-1'
    assert '\u2009' in article['sentences'][0]['text']
    assert 'µ' in view['source']['sentences'][0]['text'] and 'μ' not in view['source']['sentences'][0]['text']
