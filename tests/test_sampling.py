"""Sampling tests use only duplicated invented sources with new synthetic IDs."""
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from chl_rag.io import digest
from chl_rag.extraction import validate_extraction
from chl_rag.sampling import SEEDS, bounded_allocation, build_packages

FIXTURE = json.loads(Path('configs/synthetic.json').read_text())['cases'][0]


def frame_fixture(per_arm=20, flags_per_arm=45):
    articles, extractions, pairs = {}, {}, []
    order = []
    for arm_index, arm in enumerate(['biomarker', 'drug', 'diet']):
        pmids = []
        for index in range(per_arm):
            pmid = str(988000000 + arm_index*1000+index)
            article = deepcopy(FIXTURE['article']); article['pmid'] = pmid; article['arms'] = [arm]
            raw = deepcopy(FIXTURE['expected_model_output'])
            for claim in raw['claims']:claim['arm'] = arm
            articles[pmid] = article; extractions[pmid] = validate_extraction(article, raw)['extraction']
            pmids.append(pmid); order.append(pmid)
        for index in range(flags_per_arm):
            a, b = pmids[index % per_arm], pmids[(index + 1 + index//per_arm) % per_arm]
            if a == b:continue
            pairs.append({'pair_id': f'synthetic-{arm}-{index}', 'arm': arm, 'pmid_a': a, 'pmid_b': b,
                          'claim_a': extractions[a]['claims'][0]['claim_id'], 'claim_b': extractions[b]['claims'][1]['claim_id'],
                          'classification_status': 'valid', 'verdict': {'label': ['contradictory', 'apparent_contradiction', 'not_replicated'][index%3]}})
    frame = {'status': 'complete_frozen', 'pipeline_version': '2.0', 'scope': 'full', 'source_pmids': order,
             'eligible_reference_pmids': order,
             'extraction': {'terminal_sources': len(order), 'requested_sources': len(order)},
             'classification': {'terminal_pairs': len(pairs), 'requested_pairs': len(pairs)}}
    return frame, articles, extractions, pairs, {'sampling_seeds': SEEDS, 'instrument_version': 'synthetic-instrument'}


def test_full_target_nested_sample_probabilities_and_oracle_order():
    args = frame_fixture()
    full, human, private, human_private, manifest = build_packages(*args)
    assert len(full) == 145 and len(human) == 46
    assert manifest['pairs'] == 100 and manifest['abstracts'] == 45
    assert manifest['human_pairs'] == 40 and manifest['human_abstracts'] == 6
    assert Counter(row['arm'] for row in full if row['task'] == 'pair') == {'biomarker': 34, 'drug': 33, 'diet': 33}
    assert Counter(row['arm'] for row in human if row['task'] == 'extraction') == {'biomarker': 2, 'drug': 2, 'diet': 2}
    assert digest(full) == private['blind_items_sha256'] == manifest['package_sha256']
    assert digest(human) == human_private['blind_items_sha256']
    assert human_private['parent_package_sha256'] == digest(full)
    for row in human_private['items']:
        assert row['inclusion_probability'] == row['first_stage_probability'] * row['second_stage_probability']
    assert all(r['selected'] >= 1 for r in private['pair_frames'].values())
    assert all(r['selected'] >= 1 for r in human_private['pair_frames'].values())
    assert len(manifest['oracle_order']) == len(set(manifest['oracle_order'])) == 145
    assert sum(x.startswith('v2-pair') for x in manifest['oracle_pilot_ids']) == 3
    assert sum(x.startswith('v2-extraction') for x in manifest['oracle_pilot_ids']) == 2
    assert build_packages(*args) == (full, human, private, human_private, manifest)


def test_excluded_source_on_either_side_is_never_sampled():
    frame, articles, extracts, pairs, config = frame_fixture()
    banned = frame['eligible_reference_pmids'][0]
    frame['eligible_reference_pmids'] = [p for p in frame['eligible_reference_pmids'] if p != banned]
    full, _, private, _, _ = build_packages(frame, articles, extracts, pairs, config)
    for row in full:
        used = [row['article']['pmid']] if row['task'] == 'extraction' else [row['a']['article']['pmid'], row['b']['article']['pmid']]
        assert banned not in used
    assert private['source_whitelist_sha256'] == digest(sorted(frame['eligible_reference_pmids']))


def test_scarcity_is_declared_and_no_outputs_are_fabricated():
    args = frame_fixture(per_arm=3, flags_per_arm=3)
    full, human, _, _, manifest = build_packages(*args)
    assert manifest['pairs'] == manifest['abstracts'] == 9
    assert manifest['scarcity']['pairs_reduced'] and manifest['scarcity']['abstracts_reduced']
    assert len(full) == 18 and len(human) == 15
    assert manifest['claims'] == 18


def test_bounded_arm_shortfall_redistribution_is_deterministic():
    assert bounded_allocation({'biomarker': 10, 'drug': 100, 'diet': 100},
                              {'biomarker': 34, 'drug': 33, 'diet': 33}) == {'biomarker': 10, 'drug': 45, 'diet': 45}


def test_unfinished_frame_or_wrong_seed_is_rejected():
    frame, articles, extracts, pairs, config = frame_fixture()
    frame['classification']['terminal_pairs'] -= 1
    with pytest.raises(ValueError, match='Unfinished'):
        build_packages(frame, articles, extracts, pairs, config)
    frame['classification']['terminal_pairs'] += 1
    config['sampling_seeds'] = {**SEEDS, 'pairs': 42}
    with pytest.raises(ValueError, match='seeds'):
        build_packages(frame, articles, extracts, pairs, config)


def test_public_package_never_contains_private_prediction_or_probability_fields():
    full, human, _, _, _ = build_packages(*frame_fixture())
    forbidden = {'verdict', 'classification_status', 'inclusion_probability', 'stratum', 'pair_key', 'source_pair_id'}
    def walk(value):
        if isinstance(value, dict):
            assert not forbidden & set(value)
            for child in value.values():walk(child)
        if isinstance(value, list):
            for child in value:walk(child)
    walk(full);walk(human)
