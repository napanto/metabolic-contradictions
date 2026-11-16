"""Fresh v2 annotation samples from a complete frozen frame, never a running tail."""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

from .annotation import allocate_strata
from .io import canonical, digest, file_sha, read_jsonl, utcnow, write_json, write_jsonl
from .extraction import extraction_item, pair_item
from .production import CONFIG, production_guard

ARMS = ('biomarker', 'drug', 'diet')
FLAGGED = {'contradictory', 'apparent_contradiction', 'not_replicated'}
SEEDS = {'extraction': 20261120, 'pairs': 20261121, 'human': 20261122, 'oracle_order': 20261123}
PAIR_QUOTAS = {'biomarker': 34, 'drug': 33, 'diet': 33}


def bounded_allocation(capacities, quotas):
    """Keep original arm quotas; redistribute shortfalls proportionally, bounded."""
    capacities = {key: int(capacities.get(key, 0)) for key in quotas}
    if any(value < 0 for value in capacities.values()):
        raise ValueError('Negative sampling capacity')
    target = min(sum(quotas.values()), sum(capacities.values()))
    result = {key: min(capacities[key], quotas[key]) for key in quotas}
    while sum(result.values()) < target:
        remaining = target - sum(result.values())
        available = [key for key in quotas if result[key] < capacities[key]]
        total_weight = sum(quotas[key] for key in available)
        shares = {key: remaining * quotas[key] / total_weight for key in available}
        floors = {key: min(capacities[key]-result[key], int(shares[key])) for key in available}
        if any(floors.values()):
            for key, value in floors.items():
                result[key] += value
        else:
            for key in sorted(available, key=lambda k: (-shares[k], k))[:remaining]:
                result[key] += 1
    return result


def _validate_frame(frame, config):
    if frame.get('status') != 'complete_frozen' or frame.get('pipeline_version') != '2.0':
        raise ValueError('Sampling requires a complete frozen v2 frame')
    if config.get('sampling_seeds') != SEEDS:
        raise ValueError('Sampling seeds differ from preregistration')
    if frame['extraction']['terminal_sources'] != frame['extraction']['requested_sources']:
        raise ValueError('Unfinished extraction sources cannot define a final frame')
    if frame['classification']['terminal_pairs'] != frame['classification']['requested_pairs']:
        raise ValueError('Unfinished classifications cannot define a final frame')


def build_packages(frame, articles, extractions, pairs, config):
    """Pure deterministic draw; intended for synthetic tests or guarded prepare."""
    _validate_frame(frame, config)
    whitelist = set(frame['eligible_reference_pmids'])
    if not whitelist <= set(frame['source_pmids']):
        raise ValueError('Reference source whitelist leaves the completed production scope')
    if len({row['pair_id'] for row in pairs}) != len(pairs):
        raise ValueError('Duplicate pair source IDs')
    source_pairs = [row for row in pairs if row.get('classification_status') == 'valid'
                    and (row.get('verdict') or {}).get('label') in FLAGGED
                    and row['pmid_a'] in whitelist and row['pmid_b'] in whitelist]
    strata = defaultdict(list)
    for row in source_pairs:
        if row['arm'] not in ARMS:
            raise ValueError('Unknown semantic arm in classified pair')
        strata[(row['arm'], row['verdict']['label'])].append(row)
    pair_capacity = {arm: sum(len(v) for (a, _), v in strata.items() if a == arm) for arm in ARMS}
    pair_allocation = bounded_allocation(pair_capacity, PAIR_QUOTAS)
    pair_rng = random.Random(SEEDS['pairs'])
    selected_pairs, pair_frames = [], {}
    for arm in ARMS:
        counts = {label: len(rows) for (a, label), rows in strata.items() if a == arm}
        allocation = allocate_strata(counts, pair_allocation[arm])
        for label in sorted(counts):
            pool = sorted(strata[(arm, label)], key=lambda row: row['pair_id'])
            chosen = pair_rng.sample(pool, allocation[label])
            key = arm + '/' + label
            pair_frames[key] = {'arm': arm, 'predicted_class': label,
                                'frame_count': len(pool), 'selected': len(chosen)}
            selected_pairs.extend((row, key, len(chosen)/len(pool)) for row in chosen)
    extraction_pools = defaultdict(list)
    for pmid, extraction in extractions.items():
        if pmid in whitelist and extraction['claims']:
            if pmid not in articles:
                raise ValueError('Eligible extraction source missing')
            arm = next((a for a in ARMS if a in articles[pmid]['arms']), None)
            if arm is None:
                raise ValueError('Extraction source has no registered query arm')
            extraction_pools[arm].append(pmid)
    extraction_capacity = {a: len(extraction_pools[a]) for a in ARMS}
    extraction_allocation = bounded_allocation(extraction_capacity, {a: 15 for a in ARMS})
    extraction_rng = random.Random(SEEDS['extraction'])
    selected_abstracts, extraction_frames = [], {}
    for arm in ARMS:
        chosen = extraction_rng.sample(sorted(extraction_pools[arm]), extraction_allocation[arm])
        extraction_frames[arm] = {'frame_count': extraction_capacity[arm], 'selected': len(chosen)}
        selected_abstracts.extend((pmid, arm, len(chosen)/extraction_capacity[arm]) for pmid in chosen)
    items, private_items = [], {}
    for number, (pmid, arm, probability) in enumerate(selected_abstracts, 1):
        item_id = f'v2-extraction-{number:03d}'
        items.append(extraction_item(item_id, arm, articles[pmid], extractions[pmid]))
        private_items[item_id] = {'item_id': item_id, 'task': 'extraction', 'arm': arm, 'pmid': pmid,
                                 'inclusion_probability': probability, 'first_stage_probability': probability,
                                 'claim_ids': [c['claim_id'] for c in extractions[pmid]['claims']]}
    for number, (row, stratum, probability) in enumerate(selected_pairs, 1):
        item_id = f'v2-pair-{number:03d}'
        items.append(pair_item(item_id, row['arm'], articles[row['pmid_a']], extractions[row['pmid_a']], row['claim_a'],
                               articles[row['pmid_b']], extractions[row['pmid_b']], row['claim_b']))
        private_items[item_id] = {'item_id': item_id, 'task': 'pair', 'arm': row['arm'],
                                 'stratum': stratum, 'source_pair_id': row['pair_id'],
                                 'pmids': [row['pmid_a'], row['pmid_b']],
                                 'inclusion_probability': probability, 'first_stage_probability': probability}
    full_hash = digest(items)
    human_rng = random.Random(SEEDS['human'])
    full_pair_groups = defaultdict(list)
    for item in items:
        if item['task'] == 'pair':
            full_pair_groups[private_items[item['item_id']]['stratum']].append(item)
    human_allocation = allocate_strata({k: len(v) for k, v in full_pair_groups.items()}, min(40, len(selected_pairs)))
    human, human_private, human_frames = [], {}, {}
    for stratum in sorted(full_pair_groups):
        pool = sorted(full_pair_groups[stratum], key=lambda row: row['item_id'])
        chosen = human_rng.sample(pool, human_allocation[stratum])
        conditional = len(chosen)/len(pool)
        human_frames[stratum] = {**pair_frames[stratum], 'original_selected': len(pool),
                                 'selected': len(chosen), 'second_stage_probability': conditional}
        for item in chosen:
            original = private_items[item['item_id']]
            probability = original['inclusion_probability'] * conditional
            human.append(item)
            human_private[item['item_id']] = {**original, 'second_stage_probability': conditional,
                'total_inclusion_probability': probability, 'inclusion_probability': probability}
    for arm in ARMS:
        pool = sorted((item for item in items if item['task'] == 'extraction' and item['arm'] == arm),
                      key=lambda item: item['item_id'])
        chosen = human_rng.sample(pool, min(2, len(pool)))
        conditional = len(chosen)/len(pool) if pool else 0
        for item in chosen:
            original = private_items[item['item_id']]
            probability = original['inclusion_probability'] * conditional
            human.append(item)
            human_private[item['item_id']] = {**original, 'second_stage_probability': conditional,
                'total_inclusion_probability': probability, 'inclusion_probability': probability}
    human.sort(key=lambda row: row['item_id'])
    human_hash = digest(human)
    oracle_rng = random.Random(SEEDS['oracle_order'])
    pair_ids = [item['item_id'] for item in items if item['task'] == 'pair']
    extraction_ids = [item['item_id'] for item in items if item['task'] == 'extraction']
    oracle_rng.shuffle(pair_ids); oracle_rng.shuffle(extraction_ids)
    pilot = pair_ids[:3] + extraction_ids[:2]
    remainder = pair_ids[3:] + extraction_ids[2:]
    oracle_rng.shuffle(remainder)
    private = {'schema_version': 2, 'pipeline_version': '2.0', 'scope': frame['scope'],
               'blind_items_sha256': full_hash, 'items': list(private_items.values()),
               'pair_frames': pair_frames, 'extraction_frames': extraction_frames,
               'seeds': SEEDS, 'source_whitelist_sha256': digest(sorted(whitelist))}
    human_manifest = {'schema_version': 2, 'pipeline_version': '2.0', 'scope': frame['scope'],
                      'parent_package_sha256': full_hash, 'blind_items_sha256': human_hash,
                      'items': list(human_private.values()), 'pair_frames': human_frames,
                      'seeds': SEEDS, 'sampling': 'frozen full-sample inclusion probability times conditional human-subset probability'}
    manifest = {'pipeline_version': '2.0', 'instrument_version': config['instrument_version'],
                'scope': frame['scope'], 'package_sha256': full_hash, 'human_package_sha256': human_hash,
                'full_items': len(items), 'pairs': len(selected_pairs), 'abstracts': len(selected_abstracts),
                'claims': sum(len(item['claims']) for item in items if item['task'] == 'extraction'),
                'human_pairs': sum(item['task'] == 'pair' for item in human),
                'human_abstracts': sum(item['task'] == 'extraction' for item in human),
                'human_claims': sum(len(item['claims']) for item in human if item['task'] == 'extraction'),
                'pair_capacity': pair_capacity, 'pair_requested_by_arm': PAIR_QUOTAS, 'pair_selected_by_arm': pair_allocation,
                'extraction_capacity': extraction_capacity, 'extraction_selected_by_arm': extraction_allocation,
                'scarcity': {'pairs_reduced': len(selected_pairs) < 100, 'abstracts_reduced': len(selected_abstracts) < 45,
                            'pair_arm_redistribution': pair_allocation != PAIR_QUOTAS,
                            'extraction_arm_redistribution': extraction_allocation != {a: 15 for a in ARMS}},
                'oracle_pilot_ids': pilot, 'oracle_order': pilot + remainder, 'seeds': SEEDS,
                'unique_source_pmids': len({p for row in private_items.values() for p in row.get('pmids', [row.get('pmid')])}),
                'human_subset_frozen_before_oracle': True, 'labels_read': False}
    return items, human, private, human_manifest, manifest


def _write_immutable(path, value, *, jsonl=False):
    path = Path(path)
    if path.exists():
        old = list(read_jsonl(path)) if jsonl else json.loads(path.read_text())
        if canonical(old) != canonical(value):
            raise ValueError('Refusing to replace a frozen v2 package or sampling manifest: ' + str(path))
        return
    (write_jsonl if jsonl else write_json)(path, value)


def prepare(frame_manifest, *, config_path=CONFIG):
    config, binding = production_guard(config_path, require_classifier=True)
    from .reference_revision import effective_reference_config, reference_provenance
    reference_config = effective_reference_config(config_path)
    frame = json.loads(Path(frame_manifest).read_text())
    if frame.get('binding') != binding:
        raise ValueError('Frozen production frame binds another evaluation protocol')
    _validate_frame(frame, config)
    partitions = json.loads(Path('data/derived/partitions.json').read_text())
    expected_sources = (partitions['partition_pmids']['F'] if frame['scope'] == 'full' else
                        [row['pmid'] for row in partitions['draws']['reserve']])
    if set(frame['source_pmids']) != set(expected_sources):
        raise ValueError('Frozen frame is not the complete prespecified production scope')
    expected_eligible = (set(partitions['partition_pmids']['F']) - set(config['_extra_development_pmids'])) & set(expected_sources)
    if frame.get('classifier_binding') != config['_classifier_binding']:
        raise ValueError('Frozen classifier selection/exclusion lock changed')
    if set(frame['eligible_reference_pmids']) != expected_eligible:
        raise ValueError('Reference whitelist differs from source-level F exclusions')
    for row in frame['files'].values():
        if file_sha(row['path']) != row['sha256']:
            raise ValueError('Frozen production frame source changed')
    extractions = {row['pmid']: row for row in read_jsonl(frame['files']['extractions.jsonl']['path'])}
    pairs = list(read_jsonl(frame['files']['pairs_classified.jsonl']['path']))
    articles = {row['pmid']: row for row in read_jsonl('data/derived/corpus.jsonl') if row['pmid'] in extractions}
    full, human, private, human_private, manifest = build_packages(frame, articles, extractions, pairs, config)
    manifest.update({'frame_manifest_sha256': file_sha(frame_manifest), 'evaluation_config_sha256': file_sha(config_path),
        'reference_revision': reference_provenance(reference_config),
        'reference_instrument_sha256': reference_config['evaluation']['instrument_sha256'],
        'site_instrument_sha256': reference_config['evaluation']['site_instrument_sha256']})
    _write_immutable('annotation/package/items.jsonl', full, jsonl=True)
    _write_immutable('annotation/package/second_annotator_items.jsonl', full, jsonl=True)
    _write_immutable('annotation/human_subset/items.jsonl', human, jsonl=True)
    _write_immutable('data/derived/annotation_sampling_private.json', private)
    _write_immutable('data/derived/human_subset_sampling_private.json', human_private)
    _write_immutable('annotation/package/manifest.json', manifest)
    write_json('results/package_ready.json', {'checked_at': utcnow(), 'status': 'frozen', **manifest})
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('frame_manifest')
    parser.add_argument('--config', default=str(CONFIG))
    args = parser.parse_args()
    print(json.dumps(prepare(args.frame_manifest, config_path=args.config), indent=2))


if __name__ == '__main__':
    main()
