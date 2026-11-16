"""Pure invented label fixtures; no clinical/model/human judgments are loaded."""
from copy import deepcopy

import pytest

from chl_rag.classifier_metrics import (
    LABELS,
    astra_references,
    derived_six_label,
    evaluate_candidate,
    select_classifier,
)


def fixture():
    expected = [{'item_id': str(i), 'arm': ['biomarker', 'drug', 'diet'][i % 3]} for i in range(6)]
    reference = [{'item_id': str(i), 'label': label, 'factor': 'none'} for i, label in enumerate(LABELS)]
    prediction = [{'item_id': str(i), 'status': 'valid', 'first_attempt_valid': True,
                   'verdict': {'label': 'not_comparable' if label == 'extraction_error' else label,
                               'diagnostics': {'material_extraction_error': label == 'extraction_error'}}}
                  for i, label in enumerate(LABELS)]
    return expected, prediction, reference


def score(expected, prediction, astra, cid='candidate', cost=1):
    return evaluate_candidate(expected, prediction, astra, configuration_id=cid,
                              cost_usd=cost, latency_seconds=1, replicates=40)


def test_six_adapter_requires_explicit_diagnosis_and_preserves_five_label():
    verdict = {'label': 'not_comparable', 'diagnostics': {'material_extraction_error': True}}
    before = deepcopy(verdict)
    assert derived_six_label(verdict) == 'extraction_error' and verdict == before
    assert derived_six_label({'label': 'not_comparable', 'diagnostics': {'material_extraction_error': False}}) == 'not_comparable'
    with pytest.raises(TypeError):
        derived_six_label({'label': 'not_comparable'})
    with pytest.raises(TypeError):
        derived_six_label({'label': 'not_comparable', 'diagnostics': {'material_extraction_error': None}})
    with pytest.raises(ValueError):
        derived_six_label({'label': 'contradictory', 'diagnostics': {'material_extraction_error': True}})
    with pytest.raises(ValueError):
        derived_six_label({'label': 'extraction_error'})
    with pytest.raises(TypeError):
        derived_six_label({'label': 'not_comparable', 'diagnostics': {'material_extraction_error': 'true'}})


def test_single_reference_retains_six_categories_without_consensus_or_interjudge_statistics():
    reference = astra_references(['x'], [{'item_id': 'x', 'label': 'contradictory'}])
    assert reference['pairs']['x'] == {'status': 'resolved', 'label': 'contradictory'}
    assert reference['judge_policy'] == 'astra_only'
    assert not {'agreement_six', 'agreement_binary', 'kappa', 'fable'} & reference.keys()
    with pytest.raises(TypeError):
        astra_references(['x'], [], [])


def test_perfect_six_predictions_keep_raw_and_collapsed_sensitivities():
    expected, prediction, reference = fixture()
    result = score(expected, prediction, reference)
    assert result['primary']['binary']['F0.5'] == 1
    assert result['primary']['six_category']['accuracy'] == result['primary']['six_category']['macro_F1'] == 1
    assert result['primary']['raw_five_against_six_sensitivity']['accuracy'] == pytest.approx(5/6)
    assert result['primary']['collapsed_five_sensitivity']['accuracy'] == 1
    assert result['eligibility']['passed']
    assert result['judge_policy'] == 'astra_only'
    assert not {'per_judge', 'reference_consensus'} & result.keys()


def test_final_invalids_are_conservative_fn_or_fp_not_silent_omissions():
    expected, prediction, reference = fixture()
    prediction[0] = {'item_id': '0', 'status': 'invalid', 'first_attempt_valid': False}
    prediction[5] = {'item_id': '5', 'status': 'invalid', 'first_attempt_valid': False}
    result = score(expected, prediction, reference)
    assert result['primary']['binary']['FN'] == result['primary']['binary']['FP'] == 1
    assert result['primary']['binary']['invalid_predictions'] == 2
    assert result['primary']['six_category']['n'] == 6
    assert not result['eligibility']['passed']


def test_critical_direct_error_uses_actual_astra_reference_flag_denominator():
    expected, prediction, reference = fixture()
    prediction[3]['verdict'] = {'label': 'contradictory', 'diagnostics': {'material_extraction_error': False}}
    result = score(expected, prediction, reference)
    critical = result['primary']['critical_errors']
    assert critical['direct_on_noncomparable_or_extraction_error'] == 1
    assert critical['denominator_actual_valid_reference_flags'] == 4
    assert critical['fraction'] == .25
    assert not result['eligibility']['passed']


def test_missing_astra_reference_bounds_are_explicit_without_other_judge_substitution():
    expected, prediction, reference = fixture()
    reference[0] = {'item_id': '0', 'status': 'refused', 'answer': None}
    result = score(expected, prediction, reference)
    assert result['reference']['counts']['missing'] == 1
    assert result['primary']['reference_coverage']['valid'] == 5
    assert result['primary']['reference_coverage']['missing'] == 1
    lower, upper = result['primary']['missing_reference_F0.5_bounds']
    assert lower < upper == 1


def test_zero_reference_positive_cannot_pass_and_truncation_cannot_be_success():
    expected, prediction, reference = fixture()
    for row in reference:row['label'] = 'concordant'
    result = score(expected, prediction, reference)
    assert result['primary']['binary']['recall'] is None
    assert not result['eligibility']['passed']
    prediction[0]['truncated'] = True
    with pytest.raises(ValueError, match='truncated'):
        score(expected, prediction, reference)


def test_selector_waits_for_final_complete_grid_and_applies_prespecified_ties():
    expected, prediction, reference = fixture()
    a = score(expected, prediction, reference, cid='a', cost=5)
    b = deepcopy(a); b['configuration_id'] = 'b'; b['cost_usd'] = 1
    assert select_classifier([a, b], candidate_list_final=False, expected_configuration_ids=['a', 'b'])['status'] == 'candidate_list_not_final'
    assert select_classifier([a], candidate_list_final=True, expected_configuration_ids=['a', 'b'])['status'] == 'incomplete_grid'
    assert select_classifier([a, b], candidate_list_final=True, expected_configuration_ids=['a', 'b'])['selected_configuration_id'] == 'b'
    b['primary']['six_category']['accuracy'] -= .01
    assert select_classifier([a, b], candidate_list_final=True, expected_configuration_ids=['a', 'b'])['selected_configuration_id'] == 'a'
    b['status'] = 'incomplete'
    assert select_classifier([a, b], candidate_list_final=True, expected_configuration_ids=['a', 'b'])['status'] == 'incomplete_grid'


def test_unavailable_configuration_is_not_given_a_weak_quality_score():
    expected, prediction, reference = fixture()
    a = score(expected, prediction, reference, cid='a')
    missing = {'configuration_id': 'b', 'status': 'unavailable', 'reason': 'Provider rejects required structured output'}
    result = select_classifier([a, missing], candidate_list_final=True, expected_configuration_ids=['a', 'b'])
    assert result['selected_configuration_id'] == 'a'
    assert 'primary' not in result['unavailable_configurations'][0]


def test_nonvalid_reference_statuses_are_missing_not_parsed_as_answers():
    for status in ['refused', 'refusal', 'process_failed', 'rate_limited', 'blocked', 'started']:
        result = astra_references(['x'], [{'item_id': 'x', 'status': status, 'answer': None}])
        assert result['pairs']['x']['status'] == 'missing'


def test_undefined_optimistic_missing_bound_is_explicit():
    expected = [{'item_id': 'x', 'arm': 'diet'}]
    prediction = [{'item_id': 'x', 'status': 'valid', 'first_attempt_valid': True,
                   'verdict': {'label': 'concordant', 'diagnostics': {'material_extraction_error': False}}}]
    result = score(expected, prediction, [])
    assert result['status'] == 'complete'
    assert 'classifier_predictions_only' in result['completion_scope']
    assert result['primary']['missing_reference_F0.5_bounds'] == [0, None]
    assert result['primary']['missing_reference_undefined_bound_possible'] is True
    assert result['primary']['missing_reference_bounds_status'] == 'contains_undefined_endpoint'


def test_old_dual_judge_scores_cannot_enter_single_astra_selection():
    expected, prediction, reference = fixture()
    old = score(expected, prediction, reference)
    old.pop('judge_policy')
    with pytest.raises(ValueError, match='Astra-only'):
        select_classifier([old], candidate_list_final=True, expected_configuration_ids=['candidate'])


def test_six_category_reference_cannot_be_replaced_with_a_foreign_pair():
    with pytest.raises(ValueError, match='foreign'):
        astra_references(['expected'], [{'item_id': 'foreign', 'label': 'concordant'}])


def test_explicit_fable_or_lower_effort_provenance_cannot_enter_astra_metric_input():
    for provenance in [{'judge': 'fable'}, {'requested_model': 'claude-fable-5-2'}, {'effort': 'xhigh'}]:
        with pytest.raises(ValueError, match='Non-Astra-max reference'):
            astra_references(['x'], [{'item_id': 'x', 'label': 'concordant', **provenance}])
