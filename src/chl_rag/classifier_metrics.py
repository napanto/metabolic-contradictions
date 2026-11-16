"""Pure metabolic-development classifier metrics; no inference or data mutation.

Six-way predictions use only an explicit material-error diagnostic. The sole
selection reference is isolated GPT-6 Astra max; old Fable P records are excluded.
"""
from __future__ import annotations

import math
import random
from collections import Counter

import numpy as np

LABELS = ('contradictory', 'apparent_contradiction', 'not_replicated',
          'extraction_error', 'not_comparable', 'concordant')
RAW_LABELS = set(LABELS) - {'extraction_error'}
FLAGS = set(LABELS[:3])
BOOTSTRAP_SEED = 20261129
BOOTSTRAP_REPLICATES = 2000


def derived_six_label(verdict):
    raw = verdict['label']
    if raw not in RAW_LABELS:
        raise ValueError('Classifier output must retain the registered five raw labels')
    diagnostic = (verdict.get('diagnostics') or {}).get('material_extraction_error')
    if type(diagnostic) is not bool:
        raise TypeError('Material-error diagnostic must be an explicit boolean')
    if diagnostic is True and raw != 'not_comparable':
        raise ValueError('An explicit material error requires raw not_comparable')
    return 'extraction_error' if raw == 'not_comparable' and diagnostic is True else raw


def _index(rows):
    if isinstance(rows, dict):
        return rows
    indexed = {}
    for row in rows:
        key = row.get('item_id', row.get('pair_id'))
        if not isinstance(key, str) or key in indexed:
            raise ValueError('Every pair needs one unique string ID')
        indexed[key] = row
    return indexed


def _reference(row):
    if row is None or ('status' in row and row['status'] not in {'valid', 'ok'}):
        return None
    value = row.get('answer', row)
    if not isinstance(value, dict):
        raise TypeError('A valid reference must contain its original answer object')
    label = value.get('label')
    if label not in LABELS:
        raise ValueError('Unexpected six-category reference')
    return label


def _prediction(row):
    if row is None or row.get('status') not in {'valid', 'ok'}:
        return None, None
    if row.get('truncated'):
        raise ValueError('A final truncated prediction cannot be marked valid')
    verdict = row.get('verdict', row.get('answer'))
    if not isinstance(verdict, dict):
        raise TypeError('Valid prediction lacks its original verdict')
    return derived_six_label(verdict), verdict['label']


def _ratio(a, b):
    return a / b if b else None


def _binary(truth, prediction):
    counts = Counter()
    for actual, predicted in zip(truth, prediction, strict=True):
        if predicted is None:
            counts['FN' if actual else 'FP'] += 1
            counts['invalid_predictions'] += 1
        else:
            counts['TP' if actual and predicted else 'FN' if actual else 'FP' if predicted else 'TN'] += 1
    tp, fp, fn = (counts[k] for k in ('TP', 'FP', 'FN'))
    return {**{k: counts[k] for k in ('TP', 'FP', 'FN', 'TN', 'invalid_predictions')},
            'n': len(truth), 'reference_positive': sum(truth),
            'precision': _ratio(tp, tp + fp), 'recall': _ratio(tp, tp + fn),
            'F0.5': _ratio(1.25*tp, 1.25*tp + fp + .25*fn),
            'invalid_policy': 'FN for reference positive; FP for reference negative, without inventing a categorical output'}


def _categorical(truth, prediction, labels=LABELS):
    per_class = {}
    for label in labels:
        tp = sum(a == b == label for a, b in zip(truth, prediction, strict=True))
        fp = sum(a != label and b == label for a, b in zip(truth, prediction, strict=True))
        fn = sum(a == label and b != label for a, b in zip(truth, prediction, strict=True))
        per_class[label] = {'TP': tp, 'FP': fp, 'FN': fn, 'F1': 2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0.0}
    return {'n': len(truth), 'accuracy': _ratio(sum(a == b for a, b in zip(truth, prediction, strict=True)), len(truth)),
            'macro_F1': sum(row['F1'] for row in per_class.values())/len(labels),
            'labels': list(labels), 'zero_division': 0, 'by_class': per_class}


def astra_references(expected_ids, astra):
    """Validate one blinded reference per pair, without consensus or imputation."""
    indexed = _index(astra)
    if set(indexed) - set(expected_ids):
        raise ValueError('Reference contains foreign pair IDs')
    rows = {}
    for item_id in expected_ids:
        source = indexed.get(item_id)
        if source is not None and (source.get('judge', 'astra') != 'astra'
                                   or source.get('requested_model', 'gpt-6-astra') != 'gpt-6-astra'
                                   or source.get('effort', 'max') != 'max'):
            raise ValueError('Non-Astra-max reference cannot enter single-Astra selection')
        value = _reference(source)
        rows[item_id] = {'status': 'missing' if value is None else 'resolved', 'label': value}
    return {'pairs': rows, 'counts': dict(Counter(row['status'] for row in rows.values())),
            'judge_policy': 'astra_only', 'reference_design': 'single_astra_max',
            'judge': 'astra', 'requested_model': 'gpt-6-astra', 'effort': 'max',
            'factor_policy': 'Instrument factors retained in input artifacts; no independent factor inferred'}


def _bootstrap(rows, *, replicates, seed):
    if replicates < 1:
        raise ValueError('Positive bootstrap replicate count required')
    strata = {}
    for index, row in enumerate(rows):
        strata.setdefault(row['arm'], []).append(index)
    rng = random.Random(seed)
    values = {key: [] for key in ('precision', 'recall', 'F0.5')}
    for _ in range(replicates):
        selected = [rng.choice(indices) for _, indices in sorted(strata.items()) for _ in indices]
        result = _binary([rows[i]['reference'] in FLAGS for i in selected],
                         [None if rows[i]['prediction'] is None else rows[i]['prediction'] in FLAGS for i in selected])
        for key, collected in values.items():
            if result[key] is not None:
                collected.append(result[key])
    return {key: {'ci95': [float(x) for x in np.quantile(v, [.025, .975])] if v else None,
                  'defined': len(v), 'undefined': replicates-len(v),
                  'degenerate': bool(v and np.quantile(v, .025) == np.quantile(v, .975))}
            for key, v in values.items()} | {
        'method': 'pair bootstrap within arm on resolved reference observations', 'seed': seed,
        'replicates': replicates, 'repeated_source_dependence_not_fully_modelled': True}


def _score(rows, all_rows, *, replicates, seed):
    truth = [r['reference'] for r in rows]
    predictions = [r['prediction'] for r in rows]
    raw = [r['raw_prediction'] for r in rows]
    binary = _binary([label in FLAGS for label in truth], [None if label is None else label in FLAGS for label in predictions])
    valid_flagged = [r for r in rows if r['prediction'] in FLAGS]
    critical = sum(r['raw_prediction'] == 'contradictory' and r['reference'] in {'not_comparable', 'extraction_error'} for r in rows)
    missing = [r for r in all_rows if r['reference'] is None]
    best, worst = [], []
    for row in missing:
        if row['prediction'] is None:
            best.append(True); worst.append(False)
        else:
            best.append(row['prediction'] in FLAGS); worst.append(row['prediction'] not in FLAGS)
    known_truth = [x in FLAGS for x in truth]
    all_prediction = [None if r['prediction'] is None else r['prediction'] in FLAGS for r in rows + missing]
    bounds = [_binary(known_truth + values, all_prediction)['F0.5'] for values in (worst, best)]
    collapsed = lambda label: 'not_comparable' if label == 'extraction_error' else label
    result = {'binary': binary, 'six_category': _categorical(truth, predictions),
              'raw_five_against_six_sensitivity': _categorical(truth, raw),
              'collapsed_five_sensitivity': _categorical([collapsed(x) for x in truth], [None if x is None else collapsed(x) for x in raw],
                                                      tuple(x for x in LABELS if x != 'extraction_error')),
              'reference_coverage': {'valid': len(rows), 'expected': len(all_rows), 'missing': len(missing)},
              'critical_errors': {'direct_on_noncomparable_or_extraction_error': critical,
                  'denominator_actual_valid_reference_flags': len(valid_flagged), 'fraction': _ratio(critical, len(valid_flagged))},
              'missing_reference_F0.5_bounds': bounds,
              'missing_reference_bounds_status': 'contains_undefined_endpoint' if any(x is None for x in bounds) else 'defined',
              'missing_reference_undefined_bound_possible': any(x is None for x in bounds),
              'undefined_bound_interpretation': 'None denotes an admissible assignment with undefined F0.5, never a numeric optimistic bound',
              'bounds_scope': 'Model-specific pessimistic/optimistic assignments for missing Astra references; not confidence intervals or substituted labels',
              'bootstrap': _bootstrap(rows, replicates=replicates, seed=seed)}
    return result


def evaluate_candidate(expected_items, predictions, astra, *, configuration_id, cost_usd, latency_seconds,
                       replicates=BOOTSTRAP_REPLICATES, seed=BOOTSTRAP_SEED):
    expected = _index(expected_items)
    if any(row.get('arm') not in {'biomarker', 'drug', 'diet'} for row in expected.values()):
        raise ValueError('Every development pair needs its frozen arm')
    predicted = _index(predictions)
    if set(predicted) - set(expected):
        raise ValueError('Foreign classifier pair IDs')
    if any(not math.isfinite(float(x)) or x < 0 for x in (cost_usd, latency_seconds)):
        raise ValueError('Measured cost and latency must be finite and nonnegative')
    reference = astra_references(list(expected), astra)
    rows = []
    for item_id, item in expected.items():
        derived, raw = _prediction(predicted.get(item_id))
        rows.append({'item_id': item_id, 'arm': item['arm'], 'prediction': derived, 'raw_prediction': raw,
                     'reference': reference['pairs'][item_id]['label']})
    resolved = [row for row in rows if row['reference'] is not None]
    primary = _score(resolved, rows, replicates=replicates, seed=seed)
    first = sum(predicted.get(item_id, {}).get('first_attempt_valid') is True for item_id in expected)
    final = sum(row['prediction'] is not None for row in rows)
    schema_validity = _ratio(first, len(expected))
    reasons = []
    if primary['binary']['recall'] is None or primary['binary']['recall'] < .40:
        reasons.append('recall_below_0.40_or_undefined')
    if schema_validity is None or schema_validity < .98:
        reasons.append('first_attempt_schema_validity_below_0.98')
    critical_rate = primary['critical_errors']['fraction']
    if critical_rate is None or critical_rate > .10:
        reasons.append('critical_flag_error_rate_above_0.10_or_undefined')
    arm_results = {}
    for arm in ('biomarker', 'drug', 'diet'):
        arm_all = [r for r in rows if r['arm'] == arm]
        arm_results[arm] = _score([r for r in arm_all if r['reference'] is not None], arm_all,
                                replicates=replicates, seed=seed)
    return {'configuration_id': configuration_id, 'primary': primary, 'judge_policy': 'astra_only',
            'reference_design': 'single_astra_max',
            'status': 'complete' if set(predicted) == set(expected) and all(r.get('status') in {'valid', 'ok', 'invalid'} for r in predicted.values()) else 'incomplete',
            'completion_scope': 'classifier_predictions_only; missing Astra reference coverage is separate',
            'per_arm': arm_results, 'reference': reference,
            'validity': {'expected': len(expected), 'first_attempt_valid': first, 'final_valid': final,
                         'first_attempt_fraction': schema_validity, 'final_fraction': _ratio(final, len(expected))},
            'eligibility': {'passed': not reasons, 'reasons': reasons},
            'cost_usd': float(cost_usd), 'latency_seconds': float(latency_seconds),
            'prediction_adapter': 'extraction_error iff raw not_comparable and explicit material_extraction_error is true; otherwise raw five-way label',
            'raw_outputs_preserved': True, 'development_only': True}


def select_classifier(rows, *, candidate_list_final, expected_configuration_ids):
    if candidate_list_final is not True:
        return {'status': 'candidate_list_not_final', 'selected': None}
    if len({r['configuration_id'] for r in rows}) != len(rows):
        raise ValueError('Duplicate classifier configurations')
    if len(set(expected_configuration_ids)) != len(expected_configuration_ids):
        raise ValueError('Duplicate planned classifier configurations')
    if {r['configuration_id'] for r in rows} != set(expected_configuration_ids):
        return {'status': 'incomplete_grid', 'selected': None}
    unavailable = [r for r in rows if r.get('status') == 'unavailable']
    if any(not str(r.get('reason', '')).strip() for r in unavailable):
        raise ValueError('Unavailable configurations need their technical reason')
    if any(r.get('status') not in {'complete', 'unavailable'} for r in rows):
        return {'status': 'incomplete_grid', 'selected': None}
    if any(r.get('judge_policy') != 'astra_only' for r in rows if r.get('status') == 'complete'):
        raise ValueError('Classifier selection requires Astra-only scores')
    eligible = [r for r in rows if r.get('status') == 'complete' and r['eligibility']['passed']]
    if not eligible:
        return {'status': 'no_eligible_classifier', 'selected': None, 'decision_required': True}
    best = max(row['primary']['binary']['F0.5'] for row in eligible)
    tied = [r for r in eligible if best-r['primary']['binary']['F0.5'] <= .02+1e-12]
    chosen = min(tied, key=lambda r: (-r['primary']['six_category']['accuracy'], -r['primary']['six_category']['macro_F1'],
                                    r['cost_usd'], r['latency_seconds'], r['configuration_id']))
    return {'status': 'selected', 'selection_domain': 'metabolic_development_pairs', 'judge_policy': 'astra_only',
            'reference_design': 'single_astra_max',
            'selected_configuration_id': chosen['configuration_id'], 'selected_metrics': chosen,
            'eligible_configuration_ids': [r['configuration_id'] for r in eligible], 'best_F0.5': best,
            'unavailable_configurations': unavailable,
            'tie_tolerance': .02, 'cost_used_only_after_quality_ties': True}
