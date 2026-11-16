"""Prospective metabolic classifier development frame and prediction grid.

No work runs on import. Frame construction is source/linkage/count driven; it
never reads classifier or judge answers. All API work is explicit and guarded.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
from collections import Counter, defaultdict
from pathlib import Path

from .io import canonical, digest, file_sha, read_jsonl, utcnow, write_json, write_jsonl
from .entity_normalize import Normalizer
from .classifier_metrics import evaluate_candidate, select_classifier
from .model_client import V2Client
from .model_runs import ExperimentBlocked, _lock, _signed_file, configuration_descriptor
from .extraction import flatten_extraction, pair_item
from .pair_development import fixture_inputs
from .candidate_pairs import generate_candidates, normalize_record
from .partitions import duplicate_groups, primary_arm
from .production import (
    ASTRA_ONLY_PROTOCOL,
    _classify_one,
    _workers,
    production_guard,
    readonly_retriever,
    reuse_development_checkpoint,
)
from .selected_extraction import materialize

CONFIG = Path('configs/classifier_development.json')
BASE = Path('data/derived/classifier_development')
ARMS = ('biomarker', 'drug', 'diet')
EXPANSION_SEED = 20261128
SAMPLE_SEED = 20261127
TARGET = 120
FINAL_GRID_PROTOCOL = 'PROTOCOL_ADDENDUM_05_REDUCED_GRID.md'


def _json(path):
    return json.loads(Path(path).read_text())


def _immutable_json(path, value):
    path = Path(path)
    if path.exists():
        if canonical(_json(path)) != canonical(value):
            raise ExperimentBlocked('Refusing to replace frozen classifier-development artifact: ' + str(path))
        return
    write_json(path, value)


def _immutable_jsonl(path, rows):
    path = Path(path)
    if path.exists():
        if canonical(list(read_jsonl(path))) != canonical(rows):
            raise ExperimentBlocked('Refusing to replace frozen classifier-development rows: ' + str(path))
        return
    write_jsonl(path, rows)


def guard(config_path=CONFIG, *, prediction=False):
    spec = _json(config_path)
    if spec.get('status') != 'registered' or spec.get('stage') != 'classifier_development':
        raise ExperimentBlocked('Classifier development needs its signed registered configuration')
    _signed_file(config_path)
    if (ASTRA_ONLY_PROTOCOL not in spec.get('protocols', [])
            or spec.get('active_judges') != ['astra'] or spec.get('judge_policy') != 'astra_only'
            or spec.get('reference_design') != 'single_astra_max'):
        raise ExperimentBlocked('Classifier development requires the signed single-Astra reference policy')
    for protocol in spec['protocols']:
        _signed_file(protocol)
    if (spec.get('target_pairs') != TARGET or spec.get('expansion_seed') != EXPANSION_SEED
            or spec.get('sample_seed') != SAMPLE_SEED):
        raise ExperimentBlocked('Classifier-development sample rules differ from registration')
    required_paths = {spec[k] for k in ('evaluation_config', 'registry_path', 'corpus_path', 'pubtator_path',
                                      'legacy_key_path', 'vocabulary_path', 'index_manifest')}
    required_paths.update(spec['protocols'])
    required_paths.update({'data/derived/partitions.json', 'configs/classification_prompt.json',
                           'configs/classification_wire_schema.json', 'configs/instrument_original.json',
                           'configs/source_policy.json'})
    if required_paths - spec.get('source_sha256', {}).keys():
        raise ExperimentBlocked('Required classifier-development source hashes are missing')
    for path, expected in spec['source_sha256'].items():
        if file_sha(path) != expected:
            raise ExperimentBlocked('Frozen classifier-development input changed: ' + path)
    evaluation, pipeline_binding = production_guard(spec['evaluation_config'])
    if spec.get('evaluation_config_sha256') != pipeline_binding['config_sha256']:
        raise ExperimentBlocked('Classifier-development config does not bind amendment06')
    registry = _json(spec['registry_path'])
    _signed_file(spec['registry_path'])
    if (registry.get('active_judges') != ['astra'] or registry.get('judge_policy') != 'astra_only'
            or registry.get('reference_design') != 'single_astra_max'):
        raise ExperimentBlocked('Classifier registry does not authorize the single-Astra reference policy')
    classifier_candidates(spec, registry)
    if prediction and registry.get('candidate_list_final') is not True:
        raise ExperimentBlocked('Metabolic classifier prediction grid waits for the final candidate list')
    binding = {'classifier_development_config_sha256': file_sha(config_path),
               'evaluation_config_sha256': pipeline_binding['config_sha256'],
               'pipeline': pipeline_binding,
               'judge_policy': 'astra_only', 'reference_design': 'single_astra_max',
               'prompt_sha256': file_sha('configs/classification_prompt.json'),
               'wire_schema_sha256': file_sha('configs/classification_wire_schema.json')}
    return spec, evaluation, registry, binding


def classifier_candidates(spec, registry):
    """Read the signed classifier role, independently of extraction membership."""
    if (spec.get('classifier_grid_protocol') != FINAL_GRID_PROTOCOL
            or FINAL_GRID_PROTOCOL not in spec.get('protocols', [])):
        raise ExperimentBlocked('Classifier grid requires the signed final-grid amendment authority')
    approved = registry.get('classifier_configuration_ids')
    count = registry.get('classifier_configuration_count')
    if (type(count) is not int or count <= 0
            or not isinstance(approved, list) or len(approved) != count
            or any(not isinstance(cid, str) or not cid for cid in approved)
            or len(set(approved)) != len(approved)):
        raise ExperimentBlocked('Classifier list must match its registered positive configuration count and contain unique IDs')
    if spec.get('classifier_configuration_ids') != approved:
        raise ExperimentBlocked('Development classifier list differs from the signed registry screen')
    rows = registry['configurations']
    by_id = {row['id']: row for row in rows}
    if len(by_id) != len(rows):
        raise ExperimentBlocked('Duplicate registry configuration identity')
    if set(approved) - set(by_id):
        raise ExperimentBlocked('A screened classifier configuration is absent from the registry')
    for cid in approved:
        row = by_id[cid]
        # execution_hold/held/included_in_final_grid are extraction-role flags.
        # An explicitly authorized classifier may be excluded from extraction.
        if (row.get('classifier_execution_hold') is not False
                or row.get('included_in_classifier_grid') is not True):
            raise ExperimentBlocked('A registered classifier configuration is held or excluded: ' + cid)
        model = registry.get('models', {}).get(row.get('model'))
        if model is None or model.get('execution_hold'):
            raise ExperimentBlocked('A registered classifier model is missing or held: ' + cid)
    return {cid: by_id[cid] for cid in approved}


def closure(pmids, duplicate_components):
    result = set(pmids)
    changed = True
    while changed:
        changed = False
        for component in duplicate_components:
            if result & set(component) and not set(component) <= result:
                result.update(component); changed = True
    return result


def linked_pool(initial_normalized, legacy_normalized, eligible_f, articles, retrieval_hits, *, duplicate_components=(), initial_source_ids=None):
    """Fixed linkage only: v1 keys are not judgments or model quality scores."""
    eligible_f = set(eligible_f)
    initial_pmids = set(initial_source_ids) if initial_source_ids is not None else {row['pmid'] for row in initial_normalized}
    excluded_closure = closure(initial_pmids, duplicate_components)
    candidates = eligible_f - excluded_closure
    keys = {(row['arm'], row['exposure_key'], row['outcome_key']) for row in initial_normalized}
    reasons = defaultdict(set)
    for row in legacy_normalized:
        if row['pmid'] in candidates and (row['arm'], row['exposure_key'], row['outcome_key']) in keys:
            reasons[row['pmid']].add('frozen_same_normalized_key')
    for hit in retrieval_hits:
        if hit['pmid'] in candidates:
            reasons[hit['pmid']].add('frozen_retrieval_link_to_initial_development')
    by_arm = {arm: sorted(p for p in reasons if primary_arm(articles[p]) == arm) for arm in ARMS}
    rng = random.Random(EXPANSION_SEED)
    for arm in ARMS:
        rng.shuffle(by_arm[arm])
    order = []
    for index in range(max(map(len, by_arm.values()), default=0)):
        for arm in ARMS:
            if index < len(by_arm[arm]):
                order.append(by_arm[arm][index])
    return {'pool_pmids': sorted(reasons), 'ordered_pmids': order,
            'reasons': {p: sorted(reasons[p]) for p in sorted(reasons)},
            'by_arm': {arm: len(by_arm[arm]) for arm in ARMS},
            'seed': EXPANSION_SEED, 'initial_development_pmids': sorted(initial_pmids),
            'selection_uses_judgments_or_completion_time': False}


def sample_allocation(capacities, target=TARGET):
    """40/arm first; redistribute scarcity by remaining availability and arm ties."""
    if target != TARGET or any(capacities.get(a, 0) < 0 for a in ARMS):
        raise ValueError('Invalid classifier-development target/capacity')
    allocation = {arm: min(40, capacities.get(arm, 0)) for arm in ARMS}
    wanted = min(target, sum(capacities.get(a, 0) for a in ARMS))
    left = wanted - sum(allocation.values())
    if left:
        remaining = {arm: capacities.get(arm, 0)-allocation[arm] for arm in ARMS}
        total = sum(remaining.values())
        quotas = {arm: left*remaining[arm]/total for arm in ARMS}
        for arm in ARMS:
            allocation[arm] += int(quotas[arm])
        residual = wanted-sum(allocation.values())
        for arm in sorted(ARMS, key=lambda a: (-(quotas[a]-int(quotas[a])), ARMS.index(a))):
            if residual and allocation[arm] < capacities.get(arm, 0):
                allocation[arm] += 1; residual -= 1
    return allocation


def draw_pairs(frame):
    if len({row['pair_id'] for row in frame}) != len(frame):
        raise ValueError('Duplicate development candidate pair')
    groups = {arm: sorted((row for row in frame if row['arm'] == arm), key=lambda row: row['pair_id']) for arm in ARMS}
    if sum(map(len, groups.values())) != len(frame):
        raise ValueError('Unknown development-pair arm')
    allocation = sample_allocation({a: len(groups[a]) for a in ARMS})
    rng = random.Random(SAMPLE_SEED)
    selected = []
    for arm in ARMS:
        selected.extend(rng.sample(groups[arm], allocation[arm]))
    return selected, {'seed': SAMPLE_SEED, 'requested': TARGET, 'actual': len(selected),
                      'frame_by_arm': {a: len(groups[a]) for a in ARMS}, 'selected_by_arm': allocation,
                      'scarcity': len(selected) < TARGET, 'predicted_or_reference_labels_used': False}


class FrameBuilder:
    def __init__(self, articles, pubtator, retriever, *, index_sha256, vocabulary, cache_dir):
        self.articles, self.retriever, self.index_sha256 = articles, retriever, index_sha256
        self.normalizer = Normalizer(vocabulary=vocabulary, pubtator=pubtator)
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.hits = {}

    def search(self, query):
        if query not in self.hits:
            key = digest({'query': query, 'index_sha256': self.index_sha256, 'k': 21})
            path = self.cache / (key + '.json')
            if path.exists():
                value = _json(path)
                if value['query'] != query or value['index_sha256'] != self.index_sha256:
                    raise ExperimentBlocked('Development retrieval cache changed identity')
                self.hits[query] = value['hits']
            else:
                self.hits[query] = self.retriever.search(query, k=21)
                write_json(path, {'query': query, 'index_sha256': self.index_sha256, 'hits': self.hits[query]})
        return self.hits[query]

    def build(self, extractions, pairing):
        normalized = [normalize_record(row, self.normalizer) for extraction in extractions.values()
                      for row in flatten_extraction(extraction, source_query_arms=self.articles[extraction['pmid']]['arms'])]
        by_query = defaultdict(set)
        for row in normalized:
            if not row['hedged'] and row['comparison_identifiable']:
                query = row['normalization']['exposure']['label'] + ' ' + row['normalization']['outcome']['label']
                by_query[query].add(row['pmid'])
        links, all_hits = {}, []
        for query, pmids in sorted(by_query.items()):
            hits = self.search(query)
            all_hits.extend({'query': query, **hit} for hit in hits)
            for pmid in sorted(pmids):
                for hit in [h for h in hits if h['pmid'] != pmid][:20]:
                    if hit['pmid'] in extractions:
                        key = tuple(sorted((pmid, hit['pmid'])))
                        links[key] = max(links.get(key, 0), hit['rrf_score'])
        frame, stats = generate_candidates(normalized,
            retrieved_document_pairs=[(*key, score) for key, score in sorted(links.items())], **pairing)
        return normalized, frame, stats, all_hits


def _events(path):
    rows = list(read_jsonl(path)) if Path(path).exists() else []
    previous = None
    for ordinal, row in enumerate(rows, 1):
        if row.get('ordinal') != ordinal or row.get('previous_sha256') != previous:
            raise ExperimentBlocked('Development expansion journal chain is inconsistent')
        value = {k: v for k, v in row.items() if k != 'event_sha256'}
        if digest(value) != row.get('event_sha256'):
            raise ExperimentBlocked('Development expansion journal event changed')
        previous = row['event_sha256']
    return rows


def _append(path, rows, event):
    row = {**event, 'ordinal': len(rows)+1, 'previous_sha256': rows[-1]['event_sha256'] if rows else None}
    row['event_sha256'] = digest(row)
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False)+'\n'); handle.flush(); os.fsync(handle.fileno())
    rows.append(row)


def _exclusion_document(events, original_f, *, final=False):
    reserved = [e for e in events if e['event'] == 'source_reserved_before_extraction']
    all_excluded = set().union(*(set(e['source_closure']) for e in reserved)) if reserved else set()
    return {'status': 'frozen_before_pair_labels' if final else 'expanding_before_pair_labels',
            'excluded_from_F': sorted(all_excluded & set(original_f)),
            'all_source_closure_exclusions': sorted(all_excluded),
            'drawn_source_pmids': [e['pmid'] for e in reserved],
            'journal_tip': events[-1]['event_sha256'] if events else None,
            'labels_read': False, 'original_partition_manifest_unchanged': True}


def _no_labels_or_predictions_yet():
    for subdir in (BASE / 'references', BASE / 'predictions'):
        if subdir.exists() and any(path.is_file() and path.suffix in {'.json', '.jsonl'} for path in subdir.rglob('*')):
            raise ExperimentBlocked('Development sources/draw cannot change after references or predictions exist')


async def prepare_frame(*, config_path=CONFIG, allow_paid=False, client=None, retriever=None, extraction_loader=None):
    spec, evaluation, _registry, binding = guard(config_path)
    BASE.mkdir(parents=True, exist_ok=True)
    with _lock(BASE / 'prepare.lock'):
        if (BASE / 'manifest.json').exists():
            manifest = frozen_manifest(config_path)[0]
            return manifest
        _no_labels_or_predictions_yet()
        partitions = _json('data/derived/partitions.json')
        initial = sorted(set().union(*(set(partitions['partition_pmids'][p]) for p in ('P', 'S', 'H'))))
        if not set(partitions['partition_pmids']['H']) <= set(initial):
            raise ExperimentBlocked('Held-out source membership changed')
        articles = {a['pmid']: a for a in read_jsonl(spec['corpus_path'])}
        if not set(initial) <= set(articles):
            raise ExperimentBlocked('Initial development source missing')
        if extraction_loader is None:
            for pmid in partitions['partition_pmids']['H']:
                reuse_development_checkpoint(articles[pmid], evaluation, binding['pipeline'])
        duplicate_components, closure_reasons = duplicate_groups(list(articles.values()))
        pubtator = {a['pmid']: a for a in read_jsonl(spec['pubtator_path'])}
        builder = FrameBuilder(articles, pubtator, retriever or readonly_retriever(),
            index_sha256=file_sha(spec['index_manifest']), vocabulary=spec['vocabulary_path'],
            cache_dir='data/raw/v2/classifier_development_retrieval')
        own_client = client is None
        if client is None and allow_paid:
            client = V2Client('metabolic_classifier_development', config=spec['registry_path'])
        async def extract(pmid):
            if extraction_loader is not None:
                record = await extraction_loader(articles[pmid], evaluation, binding['pipeline'])
            else:
                record = await materialize(articles[pmid], evaluation, binding['pipeline'], client=client,
                                           allow_paid=allow_paid, requested_phase='classifier_development')
            if record.get('status') not in {'valid', 'invalid'}:
                raise ExperimentBlocked('Development extraction has not reached a terminal state')
            return record
        outputs, records = {}, {}
        try:
            # Every initial source is materialized under the one selected pipeline;
            # H uses its frozen checkpoint and can never trigger new inference.
            for pmid in initial:
                record = await extract(pmid); records[pmid] = record
                if record['status'] == 'valid':
                    outputs[pmid] = record['output']['extraction']
            normalized, frame, stats, initial_hits = builder.build(outputs, spec['pairing'])
            initial_count = len(frame)
            pool_path = BASE / 'linked_pool.json'
            if pool_path.exists():
                pool = _json(pool_path)
                if pool['binding'] != binding:
                    raise ExperimentBlocked('Fixed linked-source pool belongs to another freeze')
                if pool.get('initial_extraction_sha256') != {p: digest(r) for p, r in sorted(outputs.items())}:
                    raise ExperimentBlocked('Initial selected extraction changed after fixing the linked pool')
            else:
                if initial_count < TARGET:
                    pool = linked_pool(normalized, read_jsonl(spec['legacy_key_path']),
                        partitions['partition_pmids']['F'], articles, initial_hits, duplicate_components=duplicate_components,
                        initial_source_ids=initial)
                else:
                    pool = {'pool_pmids': [], 'ordered_pmids': [], 'reasons': {}, 'by_arm': {a: 0 for a in ARMS},
                            'seed': EXPANSION_SEED, 'initial_development_pmids': initial,
                            'selection_uses_judgments_or_completion_time': False}
                pool.update(binding=binding, initial_frame_count=initial_count,
                            initial_source_set=initial, initial_extraction_sha256={p: digest(r) for p, r in sorted(outputs.items())},
                            source_closure_rules_sha256=digest(closure_reasons),
                            initial_retrieval_links=initial_hits,
                            legacy_keys_sha256=file_sha(spec['legacy_key_path']))
                _immutable_json(pool_path, pool)
            events = _events(BASE / 'expansion_journal.jsonl')
            reserved = [e for e in events if e['event'] == 'source_reserved_before_extraction']
            completed = {e['pmid'] for e in events if e['event'] == 'source_completed'}
            eligible_f = set(partitions['partition_pmids']['F'])
            reserved_closure = set()
            for event in reserved:
                next_id = next((p for p in pool['ordered_pmids'] if p not in reserved_closure), None)
                if event['pmid'] != next_id or event.get('pool_sha256') != file_sha(pool_path):
                    raise ExperimentBlocked('Expansion journal departed from the seeded source order')
                reserved_closure.update(event['source_closure'])
            # Resume every already reserved addition before considering another.
            for event in reserved:
                pmid = event['pmid']
                if pmid not in pool['ordered_pmids'] or event['source_closure'] != sorted(closure({pmid}, duplicate_components)):
                    raise ExperimentBlocked('Reserved development source differs from the fixed pool/closure')
                record = await extract(pmid); records[pmid] = record
                previous_completed = next((e for e in events if e['event'] == 'source_completed' and e['pmid'] == pmid), None)
                if previous_completed and previous_completed['record_sha256'] != digest(record):
                    raise ExperimentBlocked('A completed selected extraction changed during development expansion')
                if record['status'] == 'valid':outputs[pmid] = record['output']['extraction']
                normalized, frame, stats, _ = builder.build(outputs, spec['pairing'])
                if pmid not in completed:
                    _append(BASE / 'expansion_journal.jsonl', events, {'event': 'source_completed', 'pmid': pmid,
                            'record_sha256': digest(record), 'terminal_status': record['status'],
                            'frame_pairs_after': len(frame), 'completed_at': utcnow()})
            excluded = set(_exclusion_document(events, eligible_f)['all_source_closure_exclusions'])
            for pmid in pool['ordered_pmids']:
                if len(frame) >= TARGET:break
                if pmid in excluded:continue
                _no_labels_or_predictions_yet()
                component = closure({pmid}, duplicate_components)
                _append(BASE / 'expansion_journal.jsonl', events, {'event': 'source_reserved_before_extraction',
                        'pmid': pmid, 'source_closure': sorted(component), 'pool_sha256': file_sha(pool_path),
                        'frame_pairs_before': len(frame), 'reserved_at': utcnow()})
                excluded.update(component)
                write_json(BASE / 'extra_F_exclusions.json', _exclusion_document(events, eligible_f))
                record = await extract(pmid); records[pmid] = record
                if record['status'] == 'valid':outputs[pmid] = record['output']['extraction']
                normalized, frame, stats, _ = builder.build(outputs, spec['pairing'])
                _append(BASE / 'expansion_journal.jsonl', events, {'event': 'source_completed', 'pmid': pmid,
                        'record_sha256': digest(record), 'terminal_status': record['status'],
                        'frame_pairs_after': len(frame), 'completed_at': utcnow()})
            development_sources = set(initial) | {e['pmid'] for e in events if e['event'] == 'source_reserved_before_extraction'}
            if any(r['pmid_a'] not in development_sources or r['pmid_b'] not in development_sources for r in frame):
                raise ExperimentBlocked('Development frame contains an unpromoted source')
            selected, sampling = draw_pairs(frame)
            items, links = [], []
            for index, row in enumerate(selected, 1):
                item_id = f'v2-pair-{700000+index:06d}'
                items.append(pair_item(item_id, row['arm'], articles[row['pmid_a']], outputs[row['pmid_a']], row['claim_a'],
                                       articles[row['pmid_b']], outputs[row['pmid_b']], row['claim_b']))
                links.append({'item_id': item_id, 'source_pair_id': row['pair_id'], 'arm': row['arm'],
                              'pmids': [row['pmid_a'], row['pmid_b']],
                              'inclusion_probability': sampling['selected_by_arm'][row['arm']]/sampling['frame_by_arm'][row['arm']]})
            exclusions = _exclusion_document(events, eligible_f, final=True)
            _immutable_jsonl(BASE / 'frame.jsonl', frame)
            _immutable_jsonl(BASE / 'items.jsonl', items)
            _immutable_jsonl(BASE / 'selected_links.jsonl', links)
            _immutable_jsonl(BASE / 'selected_extractions.jsonl', list(outputs.values()))
            # The final exclusion file seals the same monotonically grown set.
            write_json(BASE / 'extra_F_exclusions.json', exclusions)
            paths = [BASE / p for p in ['frame.jsonl', 'items.jsonl', 'selected_links.jsonl',
                                        'selected_extractions.jsonl', 'linked_pool.json', 'extra_F_exclusions.json']]
            manifest = {'status': 'frozen_before_pair_labels', 'stage': 'classifier_development', 'binding': binding,
                'created_at': utcnow(), 'initial_development_pmids': initial,
                'development_pmids': sorted(development_sources), 'extra_development_pmids': exclusions['drawn_source_pmids'],
                'excluded_from_F': exclusions['excluded_from_F'], 'source_closure_exclusions': exclusions['all_source_closure_exclusions'],
                'initial_frame_pairs': initial_count, 'frame_pairs': len(frame), 'selected_pairs': len(items),
                'linked_pool_exhausted': len(frame) < TARGET,
                'extraction_terminal_counts': dict(Counter(r['status'] for r in records.values())),
                'package_sha256': digest(items), 'item_ids': [i['item_id'] for i in items], 'sampling': sampling,
                'frame_statistics': stats, 'labels_read': False, 'classifier_predictions_read': False,
                'files': {p.name: {'path': str(p), 'sha256': file_sha(p)} for p in paths}}
            _immutable_json(BASE / 'manifest.json', manifest)
            return manifest
        finally:
            if own_client and client is not None:await client.close()


def frozen_manifest(config_path=CONFIG):
    spec, evaluation, registry, binding = guard(config_path)
    manifest = _json(BASE / 'manifest.json')
    if manifest.get('status') != 'frozen_before_pair_labels' or manifest.get('binding') != binding:
        raise ExperimentBlocked('Classifier-development manifest is not frozen under this configuration')
    for info in manifest['files'].values():
        if file_sha(info['path']) != info['sha256']:
            raise ExperimentBlocked('Classifier-development frozen artifact changed')
    items = list(read_jsonl(manifest['files']['items.jsonl']['path']))
    if digest(items) != manifest['package_sha256'] or [i['item_id'] for i in items] != manifest['item_ids']:
        raise ExperimentBlocked('Classifier-development public package hash/IDs changed')
    return manifest, items, spec, evaluation, registry


async def run_grid(*, config_path=CONFIG, allow_paid=False, client=None, concurrency=128, config_ids=None):
    spec, _evaluation, registry, auth = guard(config_path, prediction=True)
    _manifest, items, _, _, _ = frozen_manifest(config_path)
    if not allow_paid:
        raise ExperimentBlocked('Paid classifier execution must be explicit')
    candidates = classifier_candidates(spec, registry)
    chosen = list(config_ids) if config_ids is not None else list(candidates)
    if not chosen or not set(chosen) <= set(candidates) or len(set(chosen)) != len(chosen):
        raise ExperimentBlocked('Classifier resume subset must come only from the registered classifier configurations')
    own = client is None
    base_binding = {**auth, 'frame_manifest_sha256': file_sha(BASE / 'manifest.json'),
                    'public_items_sha256': file_sha(BASE / 'items.jsonl'), 'oracle_answers_read': False}
    work = [(cid, item) for item in items for cid in chosen if candidates[cid]['available']]
    for cid in chosen:
        candidate = candidates[cid]
        if not candidate['available']:
            _immutable_json(BASE / 'predictions' / cid / 'availability.json',
                {'status': 'unavailable', 'reason': candidate['unavailable_reason'], 'configuration_id': cid})
            continue
        cap = spec['prediction_caps'].get(cid)
        if type(cap) is not int or cap <= 0:
            raise ExperimentBlocked('Classifier cap needs prospective P calibration: ' + cid)
        if not any(route['max_completion_tokens'] >= cap for route in registry['models'][candidate['model']]['routes']):
            raise ExperimentBlocked('Classifier cap exceeds every registered endpoint: ' + cid)
        descriptor = configuration_descriptor(candidate, registry, cap)
        _immutable_json(BASE / 'predictions' / cid / 'binding.json', {**base_binding, 'configuration': descriptor})
    client = client or V2Client('metabolic_classifier_development', config=spec['registry_path'], concurrency=concurrency)
    async def classify(job):
        cid, item = job
        candidate = candidates[cid]
        pair, articles, extractions = fixture_inputs({'item': item})
        config = {'classification': {'model': candidate['model'], 'reasoning': candidate['reasoning'],
                                    'max_tokens': spec['prediction_caps'][cid]}}
        binding = {**base_binding, 'configuration': configuration_descriptor(candidate, registry, spec['prediction_caps'][cid])}
        return await _classify_one(client, pair, articles, extractions, config, binding,
                                   BASE / 'predictions' / cid / 'items')
    try:
        with _lock(BASE / 'predict.lock'):
            records = await _workers(work, classify, concurrency=concurrency, progress_path=BASE / 'prediction_progress.json')
    finally:
        if own:await client.close()
    summary = {'status': 'complete_requested_grid', 'configuration_ids': chosen,
               'full_classifier_configuration_ids': list(candidates),
               'requested_subset_only': set(chosen) != set(candidates),
               'predictions': len(records), 'manifest_sha256': file_sha(BASE / 'manifest.json'),
               'completed_at': utcnow(), 'oracle_answers_read': False}
    write_json(BASE / 'prediction_run.json', summary)
    return summary


def _final_references(path, manifest):
    value = _json(path)
    if (value.get('judge') != 'astra' or value.get('judge_policy') != 'astra_only'
            or value.get('reference_design') != 'single_astra_max'
            or value.get('status') not in {'complete', 'complete_with_missing'}
            or value.get('requested_model') != 'gpt-6-astra' or value.get('effort') != 'max'
            or value.get('classifier_outputs_in_prompt') is not False or value.get('peer_outputs_in_prompt') is not False
            or value.get('frame_manifest_sha256') != file_sha(BASE / 'manifest.json')
            or value.get('package_sha256') != manifest['package_sha256']
            or value.get('expected_item_ids') != manifest['item_ids']):
        raise ExperimentBlocked('Judge references are not final or bind another development package')
    artifact = value['answers']
    if file_sha(artifact['path']) != artifact['sha256']:
        raise ExperimentBlocked('Reference answer bytes changed')
    rows = list(read_jsonl(artifact['path']))
    if len(rows) != len(manifest['item_ids']) or {r.get('item_id', r.get('pair_id')) for r in rows} != set(manifest['item_ids']):
        raise ExperimentBlocked('Final reference manifest must account for valid and missing terminal items')
    return rows


def evaluate_grid(astra_manifest, *, config_path=CONFIG,
                  output='results/metabolic_classifier_selection.json'):
    spec, _, registry, auth = guard(config_path, prediction=True)
    manifest, items, _, _, _ = frozen_manifest(config_path)
    references = _final_references(astra_manifest, manifest)
    expected = [{'item_id': i['item_id'], 'arm': i['arm']} for i in items]
    rows, descriptors, prediction_hashes = [], {}, {}
    candidates = classifier_candidates(spec, registry)
    for candidate in candidates.values():
        cid = candidate['id']; descriptors[cid] = candidate
        if not candidate['available']:
            rows.append({'configuration_id': cid, 'status': 'unavailable', 'reason': candidate['unavailable_reason']})
            continue
        predictions = []
        expected_binding = {**auth, 'frame_manifest_sha256': file_sha(BASE / 'manifest.json'),
                            'public_items_sha256': file_sha(BASE / 'items.jsonl'), 'oracle_answers_read': False,
                            'configuration': configuration_descriptor(candidate, registry, spec['prediction_caps'][cid])}
        prediction_binding_path = BASE / 'predictions' / cid / 'binding.json'
        if not prediction_binding_path.exists():
            raise ExperimentBlocked('Classifier grid has no frozen prediction binding: ' + cid)
        if _json(prediction_binding_path) != expected_binding:
            raise ExperimentBlocked('Classifier prediction settings differ from the frozen grid')
        for item_id in manifest['item_ids']:
            path = BASE / 'predictions' / cid / 'items' / f'{item_id}.json'
            if not path.exists():raise ExperimentBlocked('Classifier grid has unfinished items: ' + cid)
            prediction = _json(path)
            if prediction.get('pair_id') != item_id or prediction.get('binding') != expected_binding:
                raise ExperimentBlocked('Classifier prediction identity/frame/configuration mismatch')
            predictions.append(prediction)
            prediction_hashes[str(path)] = file_sha(path)
        attempts = [a for p in predictions for a in p.get('attempts', [])]
        costs = [a.get('usage', {}).get('cost') for a in attempts]
        times = [a.get('elapsed_seconds') for a in attempts]
        if any(v is None for v in costs + times):
            raise ExperimentBlocked('Measured cost/time missing before classifier tie-breaks')
        rows.append(evaluate_candidate(expected, predictions, references,
            configuration_id=cid, cost_usd=sum(costs), latency_seconds=sum(times)/len(predictions) if predictions else 0))
    selection = select_classifier(rows, candidate_list_final=registry.get('candidate_list_final'), expected_configuration_ids=list(candidates))
    result = {**selection, 'grid': rows, 'selection_domain': 'metabolic_development_pairs',
              'judge_policy': 'astra_only', 'reference_design': 'single_astra_max', 'active_judges': ['astra'],
              'source_sha256': {'config': file_sha(config_path), 'frame_manifest': file_sha(BASE / 'manifest.json'),
                               'astra_references': file_sha(astra_manifest)},
              'prediction_sha256': prediction_hashes,
              'classifier_configuration_ids': list(candidates),
              'reported_at': utcnow(), 'external_mancon_does_not_select': True}
    if selection['status'] == 'selected':
        cid = selection['selected_configuration_id']; candidate = descriptors[cid]
        result['selected'] = {**candidate, 'max_tokens': spec['prediction_caps'][cid],
                              'providers': registry['models'][candidate['model']]['routes']}
    if Path(output).exists():
        prior = _json(output)
        comparable = lambda value: {k: v for k, v in value.items() if k != 'reported_at'}
        if canonical(comparable(prior)) != canonical(comparable(result)):
            raise ExperimentBlocked('Frozen classifier selection inputs or result changed')
        return prior
    _immutable_json(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'predict', 'evaluate'])
    parser.add_argument('--config', default=str(CONFIG))
    parser.add_argument('--allow-paid', action='store_true')
    parser.add_argument('--concurrency', type=int, default=128)
    parser.add_argument('--astra-references')
    args = parser.parse_args()
    if args.action == 'prepare':
        result = asyncio.run(prepare_frame(config_path=args.config, allow_paid=args.allow_paid))
    elif args.action == 'predict':
        result = asyncio.run(run_grid(config_path=args.config, allow_paid=args.allow_paid, concurrency=args.concurrency))
    else:
        if not args.astra_references:
            parser.error('evaluate requires --astra-references')
        result = evaluate_grid(args.astra_references, config_path=args.config)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
