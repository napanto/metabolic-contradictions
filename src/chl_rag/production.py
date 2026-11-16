"""Guarded v2 production, immutable source frames and separate v2 artifacts.

Nothing runs on import. Production requires signed amendment06, a bound selected
a pre-specified GLM high extractor and GLM low classifier; no S/H selection is claimed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from jsonschema import ValidationError

from .io import digest, file_sha, read_jsonl, utcnow, write_json, write_jsonl
from .llm import AccountingError, BudgetError, OutputError, RequestConfigurationError
from .entity_normalize import Normalizer
from .model_client import V2Client
from .model_runs import (
    ExperimentBlocked,
    _immutable,
    _lock,
    _parse_response,
    _signed_file,
    _usage,
)
from .extraction import flatten_extraction
from .candidate_pairs import (
    classification_request,
    generate_candidates,
    normalize_record,
    validate_classification,
)

CONFIG = Path('configs/evaluation_system.json')
BASE = Path('data/derived/production_uncorrected')
RESULTS = Path('results/production_uncorrected')
ARMS = ('biomarker', 'drug', 'diet')
ASTRA_ONLY_PROTOCOL = 'PROTOCOL_ADDENDUM_05_ASTRA_ONLY.md'
PRESPECIFIED_PROTOCOL = 'PROTOCOL_ADDENDUM_05_PRESPECIFIED_GLM.md'


def _json(path):
    return json.loads(Path(path).read_text())


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('Production timestamps need a timezone')
    return result


def _bound(path, sources):
    if str(path) not in sources or file_sha(path) != sources[str(path)]:
        raise ExperimentBlocked('Unbound production input: ' + str(path))
    return _json(path)


def production_guard(config_path=CONFIG, *, require_classifier=False):
    """Authorize the signed, a-priori GLM pipeline without inventing S/H results."""
    from .frozen_compat import verify_frozen_input
    config = _json(config_path)
    if config.get('status') != 'registered' or config.get('pipeline_version') != '2.0':
        raise ExperimentBlocked('Production requires registered v2 amendment06')
    if (config.get('production_design') != 'prespecified_fixed'
            or config.get('comparison_status') != {
                'S': 'not_run', 'H': 'not_run', 'metabolic_classifier_experiment': 'not_run'}):
        raise ExperimentBlocked('Production requires the explicit pre-specified choice and unused S/H/120 stages')
    if (config.get('active_judges') != ['astra'] or config.get('judge_policy') != 'astra_only'
            or config.get('reference_design') != 'single_astra_max'):
        raise ExperimentBlocked('Production requires the registered single-Astra reference policy')
    if config.get('perimeter_policy') != 'full_F_operational_failure_only':
        raise ExperimentBlocked('Full F may be reduced only for a documented operational failure')
    protocol = config.get('protocol', 'PROTOCOL_AMENDMENT_06_V2_EVALUATION.md')
    protocol_commit = _signed_file(protocol)
    _signed_file(config_path)
    sources = config.get('source_sha256', {})
    authority_commits = {}
    for authority in (ASTRA_ONLY_PROTOCOL, PRESPECIFIED_PROTOCOL):
        if sources.get(authority) != file_sha(authority):
            raise ExperimentBlocked('Production must bind the signed fixed-plan and Astra-only amendments')
        authority_commits[authority] = _signed_file(authority)
    for path, sha in sources.items():
        verify_frozen_input(path, sha, config_path=config_path)
    ext, classification = config['extraction'], config['classification']
    expected = [('extraction', ext, 'glm-5.3-flash__high', 'high'),
                ('classification', classification, 'glm-5.3-flash__low', 'low')]
    for role, row, cid, effort in expected:
        if (row.get('configuration_id') != cid or row.get('model') != 'z-ai/glm-5.3-flash'
                or row.get('reasoning') != effort or type(row.get('max_tokens')) is not int
                or row['max_tokens'] <= 0):
            raise ExperimentBlocked('Pre-specified model/effort/cap changed for ' + role)
    if ext.get('verify') is not False:
        raise ExperimentBlocked('Pre-specified extraction is one pass, with verification disabled')
    if any(key in ext for key in ('selection_path', 'H_check_path')) or 'selection_path' in classification:
        raise ExperimentBlocked('Pre-specified production must not present comparison selections or H checks')
    routing_path = config.get('routing_config', 'configs/routing_registry.json')
    routes = _bound(routing_path, sources)
    for role, row, cid, _effort in expected:
        candidates = [c for c in routes.get('configurations', []) if c['id'] == cid]
        if (len(candidates) != 1 or any(candidates[0][k] != row[k] for k in ('model', 'reasoning'))
                or candidates[0].get('available') is not True):
            raise ExperimentBlocked('Pre-specified role differs from the registered routing configuration: ' + role)
        hold = 'execution_hold' if role == 'extraction' else 'classifier_execution_hold'
        if candidates[0].get(hold) is not False:
            raise ExperimentBlocked('Pre-specified production role remains on execution hold: ' + role)
    model = routes['models'][ext['model']]
    if model.get('execution_hold'):
        raise ExperimentBlocked('Pre-specified production model remains on execution hold')
    if not model.get('routes') or any(r.get('quantization') != 'fp8' for r in model['routes']):
        raise ExperimentBlocked('Pre-specified GLM production requires declared fp8 routes')
    for role, row, _cid, _effort in expected:
        if not any(r['max_completion_tokens'] >= row['max_tokens'] for r in model['routes']):
            raise ExperimentBlocked('Frozen output cap exceeds all registered endpoints: ' + role)
    freeze_path = config.get('prompt_freeze_path', 'results/prompt_freeze.json')
    freeze = _bound(freeze_path, sources)
    _signed_file(freeze_path)
    if (freeze.get('status') != 'frozen' or not {'extraction', 'metabolic_classifier', 'oracle', 'instrument'}
            <= set(freeze.get('prompt_files', {}))):
        raise ExperimentBlocked('Production requires the complete reviewed prompt freeze')
    freeze_protocol = freeze.get('protocol', 'PROTOCOL_ADDENDUM_05a_PROMPT_FREEZE.md')
    if (freeze.get('protocol_sha256') != file_sha(freeze_protocol)
            or sources.get(freeze_protocol) != file_sha(freeze_protocol)):
        raise ExperimentBlocked('Prompt-freeze protocol is not hash-bound')
    _signed_file(freeze_protocol)
    for path in freeze['prompt_files'].values():
        if freeze.get('reviewed_prompt_sha256', {}).get(path) != sources.get(path):
            raise ExperimentBlocked('Reviewed prompt differs from the frozen production input: ' + path)
        verify_frozen_input(path, sources[path], config_path=config_path)
        _signed_file(path)
    for path, sha in freeze.get('runtime_sha256', {}).items():
        if sources.get(path) != sha:
            raise ExperimentBlocked('Frozen production runtime changed: ' + path)
        verify_frozen_input(path, sha, config_path=config_path)
    for role, row, cid, _effort in expected:
        if freeze.get('production_caps', {}).get(cid) != row['max_tokens']:
            raise ExperimentBlocked('Production output cap was not frozen from P calibration: ' + role)
    partitions = _bound('data/derived/partitions.json', sources)
    f_pmids = partitions['partition_pmids']['F']
    if (type(config.get('production_source_count')) is not int
            or config['production_source_count'] != len(f_pmids) or len(set(f_pmids)) != len(f_pmids)):
        raise ExperimentBlocked('Fixed production count must equal the complete original F partition')
    excluded = set().union(*(set(partitions['partition_pmids'].get(key, []))
                             for key in ('P', 'S', 'H', 'V1', 'audit', 'preview_additional', 'duplicates_additional')))
    if set(f_pmids) & excluded:
        raise ExperimentBlocked('Original F must remain disjoint from development and exposed sources')
    for name in ('full_extraction_deadline', 'classification_deadline', 'package_deadline'):
        _time(config['timing'][name])
    binding = {'config_sha256': file_sha(config_path), 'protocol_commit': protocol_commit,
               'authority_commits': authority_commits, 'production_design': 'prespecified_fixed',
               'judge_policy': 'astra_only', 'reference_design': 'single_astra_max',
               'comparison_status': config['comparison_status'], 'prompt_freeze_sha256': file_sha(freeze_path),
               'prespecified_configuration_sha256': digest({'extraction': ext, 'classification': classification}),
               'production_F_sha256': digest(sorted(f_pmids)),
               'operational_failure_path': config.get('operational_failure_path', 'configs/v2_operational_failure.json')}
    config = deepcopy(config)
    config['_extra_development_pmids'] = []
    config['_operational_decision'] = {'scope': 'full', 'authority': 'Antonio INBOX 2026-11-14 00:58',
                                       'basis': 'prespecified_fixed', 'runtime_time_fallback_authorized': False}
    if require_classifier:
        benchmark = config.get('external_benchmark', {})
        path = benchmark.get('selection_path')
        if not path:
            raise ExperimentBlocked('Classification requires the signed external ManCon benchmark integration')
        _signed_file(path)
        if file_sha(path) != benchmark.get('selection_sha256'):
            raise ExperimentBlocked('External benchmark selection hash mismatch')
        if _json(path).get('selection_split') != 'dev':
            raise ExperimentBlocked('External benchmark is not the frozen ManCon-dev selection')
        merge_commit = benchmark['merge_commit']
        if subprocess.run(['git', 'merge-base', '--is-ancestor', merge_commit, 'HEAD'], capture_output=True, check=False).returncode:
            raise ExperimentBlocked('External benchmark branch has not been integrated')
        if subprocess.run(['git', 'verify-commit', merge_commit], capture_output=True, check=False).returncode:
            raise ExperimentBlocked('External benchmark integration commit is not signed')
        config['_classifier_binding'] = {'selection_basis': 'prespecified_before_F_C_results',
            'fixed_configuration_sha256': digest(classification), 'benchmark_selection_sha256': file_sha(path),
            'benchmark_merge_commit': merge_commit, 'metabolic_classifier_experiment': 'not_run'}
    return config, binding


def choose_scope(now, full_extraction_seconds, full_classification_seconds, timing):
    """Report timing while retaining full F; clock/cost cannot reduce scope."""
    now = _time(now)
    if full_extraction_seconds < 0 or full_classification_seconds < 0:
        raise ValueError('Projection durations must be nonnegative')
    margin = timing.get('projection_margin', 1.5)
    if margin != 1.5:
        raise ValueError('Registered planning margin is 50 percent')
    extraction_end = now + timedelta(seconds=margin * full_extraction_seconds)
    classification_end = extraction_end + timedelta(seconds=margin * full_classification_seconds)
    full = (extraction_end <= _time(timing['full_extraction_deadline'])
            and classification_end <= _time(timing['classification_deadline']))
    return {'scope': 'full', 'decided_at': now.isoformat(),
            'rule': 'full_F_operational_failure_only', 'margin': margin, 'within_target_times': full,
            'projected_extraction_end': extraction_end.isoformat(),
            'projected_classification_end': classification_end.isoformat(),
            'cost_used': False, 'judgment_values_used': False, 'recommendation_only': True}


def plan_scope(projection_path=None, config_path=CONFIG):
    config, binding = production_guard(config_path)
    if projection_path:
        projection = _json(projection_path)
        if projection.get('config_sha256') != binding['config_sha256']:
            raise ExperimentBlocked('Timing projection does not bind the pre-specified configuration')
        report = choose_scope(utcnow(), projection['full_extraction_seconds'],
                              projection['full_classification_seconds'], config['timing'])
        write_json(RESULTS / 'timing_projection.json', {**report, 'projection_sha256': file_sha(projection_path)})
    decision = {'scope': 'full', 'decided_at': utcnow(),
                'authority': config['_operational_decision']['authority'],
                'scope_policy': 'full_F_operational_failure_only', 'binding': binding}
    path = RESULTS / 'scope_decision.json'
    if path.exists():
        prior = _json(path)
        if prior.get('binding') != binding or prior.get('scope') != 'full':
            raise ExperimentBlocked('Scope decision binds another fixed plan')
        return prior
    return _immutable(path, decision)


def _scope(scope, binding):
    if scope not in {'full', 'reserve'} or binding.get('production_design') != 'prespecified_fixed':
        raise ExperimentBlocked('Production requires the pre-specified full-F scope')
    if (RESULTS / 'time_fallback.json').exists():
        raise ExperimentBlocked('An obsolete time fallback cannot authorize fixed-plan production')
    partitions = _json('data/derived/partitions.json')
    f_pmids = set(partitions['partition_pmids']['F'])
    reserve = [r['pmid'] for r in partitions['draws']['reserve']]
    if len(set(reserve)) != len(reserve) or not set(reserve) <= f_pmids:
        raise ExperimentBlocked('Reserve must be a unique subset of the original F partition')
    order = [p for p in partitions['reserve_first_production_order'] if p in f_pmids]
    if set(order) != f_pmids or len(order) != len(f_pmids):
        raise ExperimentBlocked('Production order must cover every original F source once')
    if scope == 'reserve':
        failure_path = Path(binding.get('operational_failure_path', 'configs/v2_operational_failure.json'))
        if not failure_path.exists():
            raise ExperimentBlocked('Reserve requires a signed documented operational failure')
        _signed_file(failure_path)
        failure = _json(failure_path)
        if (failure.get('config_sha256') != binding['config_sha256']
                or failure.get('status') != 'confirmed' or failure.get('scope') != 'reserve'
                or failure.get('basis') != 'operational_failure' or failure.get('full_F_impossible') is not True
                or failure.get('time_only') is not False or failure.get('cost_used') is not False
                or not str(failure.get('description', '')).strip() or not failure.get('evidence')):
            raise ExperimentBlocked('Reserve failure record must document why full F is operationally impossible')
        for evidence in failure['evidence']:
            if file_sha(evidence['path']) != evidence['sha256']:
                raise ExperimentBlocked('Operational-failure evidence changed')
        audit = {'failure_path': str(failure_path), 'failure_sha256': file_sha(failure_path),
                 'config_sha256': binding['config_sha256'], 'scope': 'reserve'}
        _immutable(RESULTS / 'operational_fallback.json', audit)
        order = reserve
    return order, partitions


async def _workers(items, function, *, concurrency, progress_path):
    if concurrency < 1:
        raise ValueError('Production concurrency must be positive')
    queue, stop, errors, results = asyncio.Queue(), asyncio.Event(), [], []
    for item in items:
        queue.put_nowait(item)
    async def worker():
        while not stop.is_set():
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                result = await function(item)
                if result is not None:
                    results.append(result)
                    write_json(progress_path, {'updated_at': utcnow(), 'completed_this_invocation': len(results),
                        'requested': len(items), 'status_counts': dict(Counter(r['status'] for r in results))})
            except Exception as exc:  # noqa: BLE001 -- drain submitted calls before re-raising.
                errors.append(exc); stop.set()
    tasks = [asyncio.create_task(worker()) for _ in range(min(concurrency, len(items)))]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    if errors:
        raise errors[0]
    return results


def _extract_path(config, pmid):
    return BASE / 'extraction' / config['extraction']['configuration_id'] / f'{pmid}.json'


def reuse_development_checkpoint(article, config, binding):
    """Fixed production uses only F; never open development or saved v1 bodies."""
    if config.get('production_design') != 'prespecified_fixed':
        raise ExperimentBlocked('Comparative production and H checkpoint reuse are retired')
    partitions = _json('data/derived/partitions.json')['partition_pmids']
    if str(article['pmid']) not in set(partitions['F']):
        raise ExperimentBlocked('Fixed production cannot infer on P/S/H or promote other sources')


async def run_extraction(scope, *, config_path=CONFIG, client=None, concurrency=128):
    config, binding = production_guard(config_path)
    plan_scope(config_path=config_path)
    order, _partitions = _scope(scope, binding)
    articles = {a['pmid']: a for a in read_jsonl('data/derived/corpus.jsonl') if a['pmid'] in set(order)}
    if set(articles) != set(order):
        raise ExperimentBlocked('Production source manifest is incomplete')
    own = client is None
    client = client or V2Client('production_extraction', config=config.get('routing_config', 'configs/routing_registry.json'), concurrency=concurrency)
    async def work(pmid):
        from .selected_extraction import materialize
        return await materialize(articles[pmid], config, binding, client=client, allow_paid=True,
                                 requested_phase='production')
    try:
        with _lock(RESULTS / 'extraction.lock'):
            await _workers(order, work, concurrency=concurrency, progress_path=RESULTS / f'extraction_{scope}_progress.json')
    finally:
        if own:
            await client.close()
    return assemble_extraction(scope, config_path=config_path)


def assemble_extraction(scope, *, config_path=CONFIG):
    config, binding = production_guard(config_path)
    order, _ = _scope(scope, binding)
    missing = [p for p in order if not _extract_path(config, p).exists()]
    if missing:
        raise ExperimentBlocked(f'Extraction frame has {len(missing)} unfinished sources')
    rows = [_json(_extract_path(config, p)) for p in order]
    if any(r.get('status') not in {'valid', 'invalid'} or r['binding']['registration'] != binding for r in rows):
        raise ExperimentBlocked('Nonterminal or mismatched extraction checkpoint')
    for pmid, row in zip(order, rows, strict=True):
        output = row.get('output') or {}
        if (str(row.get('pmid')) != pmid
                or (row['status'] == 'valid' and str(output.get('extraction', {}).get('pmid')) != pmid)):
            raise ExperimentBlocked('Extraction checkpoint source PMID differs from its expected path: ' + pmid)
    extractions, dropped, verified_drops = [], [], []
    raw_claims = 0
    for row in rows:
        base = row.get('base_record', row)
        base_output = base.get('output') or {}
        raw_claims += base_output.get('raw_claim_count', 0)
        dropped.extend({'pmid': row['pmid'], **drop} for drop in base_output.get('dropped', []))
        if row['status'] == 'valid':
            extractions.append(row['output']['extraction'])
            output = row['output']
            if output.get('verification'):
                rejected = set(output['discarded_claim_ids'])
                by_id = {v['claim_id']: v for v in output['verification']['claims']}
                verified_drops.extend({'pmid': row['pmid'], 'reason': 'same_model_unsupported',
                                       'claim': claim, 'verification': by_id[claim['claim_id']]}
                                      for claim in output['original_extraction']['claims']
                                      if claim['claim_id'] in rejected)
    destination = BASE / scope
    write_jsonl(destination / 'extractions.jsonl', extractions)
    write_jsonl(destination / 'claims.jsonl', (r for e in extractions for r in flatten_extraction(e)))
    write_jsonl(destination / 'deterministic_drops.jsonl', dropped)
    write_jsonl(destination / 'verification_drops.jsonl', verified_drops)
    summary = {'status': 'complete', 'scope': scope, 'binding': binding, 'source_pmids': order,
               'requested_sources': len(order), 'terminal_sources': len(rows),
               'valid_sources': len(extractions), 'invalid_sources': sum(r['status'] == 'invalid' for r in rows),
               'zero_claim_sources': sum(not e['claims'] for e in extractions),
               'retained_claims': sum(len(e['claims']) for e in extractions),
               'raw_emitted_claims_in_valid_base_outputs': raw_claims,
               'deterministic_dropped_claims': len(dropped), 'verification_dropped_claims': len(verified_drops),
               'reused_development_sources': sum(bool(r.get('reuse_provenance')) for r in rows),
               'reused_by_stage': dict(Counter(r['reuse_provenance']['source_stage'] for r in rows if r.get('reuse_provenance'))),
               'completed_at': utcnow(), 'extractions_sha256': file_sha(destination / 'extractions.jsonl'),
               'claims_sha256': file_sha(destination / 'claims.jsonl'), 'terminal_failures_count_as_complete': True}
    write_json(RESULTS / f'extraction_{scope}.json', summary)
    return summary


def readonly_retriever():
    """Load existing index/cache only; never call load_or_build."""
    import faiss

    from .index import Retriever, load_encoder
    from .sparse_bm25 import SparseBM25
    retriever = object.__new__(Retriever)
    retriever.articles = list(read_jsonl('data/index/articles.jsonl'))
    retriever.sentences = list(read_jsonl('data/index/sentences.jsonl'))
    retriever.bm25 = SparseBM25.load('data/index/bm25')
    retriever.index = faiss.read_index('data/index/medcpt.faiss')
    retriever.tokenizer, retriever.model, _ = load_encoder('Query')
    return retriever


def build_frame(scope, *, config_path=CONFIG, retriever=None):
    config, binding = production_guard(config_path)
    extraction = assemble_extraction(scope, config_path=config_path)
    source = BASE / scope
    claims = list(read_jsonl(source / 'claims.jsonl'))
    wanted = set(extraction['source_pmids'])
    pubtator = {a['pmid']: a for a in read_jsonl('data/derived/pubtator.jsonl.gz') if a['pmid'] in wanted}
    normalizer = Normalizer(pubtator=pubtator)
    normalized = [normalize_record(row, normalizer) for row in claims]
    write_jsonl(source / 'claims_normalized.jsonl', normalized)
    retriever = retriever or readonly_retriever()
    cache = Path('data/raw/v2/production_retrieval')
    cache.mkdir(parents=True, exist_ok=True)
    index_identity = config['source_sha256'].get('results/index.json')
    if not index_identity:
        raise ExperimentBlocked('Frozen index identity must be bound in amendment06')
    by_query = {}
    for row in normalized:
        if row['hedged'] or not row['comparison_identifiable']:
            continue
        query = row['normalization']['exposure']['label'] + ' ' + row['normalization']['outcome']['label']
        by_query.setdefault(query, set()).add(row['pmid'])
    document_pairs = {}
    for query, pmids in sorted(by_query.items()):
        key = digest({'index': index_identity, 'query': query, 'k': 21})
        path = cache / (key + '.json')
        if path.exists():
            hits = _json(path)['hits']
        else:
            hits = retriever.search(query, k=21)
            write_json(path, {'index_sha256': index_identity, 'query': query, 'hits': hits})
        for pmid in pmids:
            selected = [hit for hit in hits if hit['pmid'] != pmid][:20]
            for hit in selected:
                if hit['pmid'] in wanted:
                    pair = tuple(sorted([pmid, hit['pmid']]))
                    document_pairs[pair] = max(document_pairs.get(pair, 0), hit['rrf_score'])
    parameters = config.get('pairing', {'inconsistent_cap': 30, 'concordant_cap': 5})
    key_pairs, key_stats = generate_candidates(normalized, **parameters)
    pairs, stats = generate_candidates(normalized,
        retrieved_document_pairs=[(*key, score) for key, score in sorted(document_pairs.items())], **parameters)
    write_jsonl(source / 'pairs_key.jsonl', key_pairs)
    write_jsonl(source / 'pairs_candidates.jsonl', pairs)
    summary = {'status': 'complete', 'scope': scope, 'binding': binding,
               'extraction_sha256': file_sha(source / 'extractions.jsonl'),
               'claims_sha256': file_sha(source / 'claims_normalized.jsonl'),
               'pairs_sha256': file_sha(source / 'pairs_candidates.jsonl'),
               'pairs': len(pairs), 'key_only_pairs': len(key_pairs),
               'retrieval_queries': len(by_query), 'key_stats': key_stats, 'hybrid_stats': stats,
               'completed_at': utcnow(), 'existing_indexes_read_only': True}
    write_json(RESULTS / f'frame_{scope}.json', summary)
    return summary


def classifier_routes(config, development_routes):
    """Fixed classifier identity; use only the signed GLM fp8 routing registry."""
    model = config['classification']['model']
    output = deepcopy(development_routes)
    routes = output['models'][model]['routes']
    if not routes or any(r.get('quantization') != 'fp8' for r in routes):
        raise ExperimentBlocked('Fixed classifier requires the registered fp8 provider routes')
    quantizations = {r.get('quantization', 'unknown') for r in routes}
    if len(routes) > 1 and quantizations != {'fp8'}:
        raise ExperimentBlocked('Multi-provider classifier routes must share declared fp8')
    output['models'][model]['routes'] = routes
    return output


async def _classify_one(client, pair, articles, extractions, config, binding, destination):
    spec = config['classification']
    request = classification_request(pair, articles, extractions, model=spec['model'],
                                     reasoning=spec['reasoning'], max_output_tokens=spec['max_tokens'])
    identity = digest({'request': request['identity'], 'binding': binding})
    path = destination / f"{pair['pair_id']}.json"
    if path.exists():
        saved = _json(path)
        if saved['identity'] != identity:
            raise ExperimentBlocked('Classification checkpoint changed identity')
        return saved
    provider, verdict, attempts = None, None, []
    for number in (1, 2):
        attempt_path = destination / pair['pair_id'] / f'attempt-{number}.json'
        if attempt_path.exists():
            attempt = _json(attempt_path)
            if attempt['identity'] != identity:
                raise ExperimentBlocked('Classification attempt identity mismatch')
            if attempt['status'] == 'started':
                raise ExperimentBlocked('Interrupted classification needs billing/cache reconciliation')
            if attempt['status'] in {'valid', 'invalid'}:
                attempts.append(attempt); provider = attempt.get('meta', {}).get('requested_provider') or provider
                if attempt['status'] == 'valid':
                    verdict = attempt['verdict']; break
                continue
        attempt = {'identity': identity, 'status': 'started', 'started_at': utcnow(), 'number': number}
        write_json(attempt_path, attempt)
        start = time.monotonic()
        try:
            response, meta = await client.generate(spec['model'], request['messages'], request['schema'],
                spec['reasoning'], spec['max_tokens'], forced_provider=provider,
                cache_namespace=f'production-classification/{identity}/attempt-{number}')
            provider = meta['requested_provider']
            attempt.update(response=response, meta=meta, usage=_usage(response))
            verdict = validate_classification(request['payload'], _parse_response(response))
            attempt.update(status='valid', verdict=verdict)
        except (BudgetError, AccountingError, RequestConfigurationError) as exc:
            attempt.update(status='blocked', error_type=type(exc).__name__, error=str(exc), ended_at=utcnow())
            write_json(attempt_path, attempt); raise
        except (KeyError, IndexError, TypeError, ValueError, ValidationError, OutputError) as exc:
            attempt.update(status='invalid', error_type=type(exc).__name__, error=str(exc))
            if getattr(exc, 'failure_category', None) == 'transport_missing_output':
                provider = exc.requested_provider
                attempt.update(failure_category='transport_missing_output', schema_validation='not_evaluated_no_usable_response',
                               meta={'request_hash': exc.request_hash, 'requested_provider': provider,
                                     'billing_status': 'unknown', 'transport_response_received': exc.response_received,
                                     'missing_output_cause': exc.missing_output_cause})
        attempt.update(ended_at=utcnow(), elapsed_seconds=time.monotonic()-start)
        write_json(attempt_path, attempt); attempts.append(attempt)
        if verdict is not None:
            break
    record = {'identity': identity, 'status': 'valid' if verdict is not None else 'invalid',
              'pair_id': pair['pair_id'], 'verdict': verdict, 'first_attempt_valid': attempts[0]['status'] == 'valid',
              'attempt_count': len(attempts), 'attempts': attempts, 'binding': binding, 'completed_at': utcnow()}
    write_json(path, record)
    return record


async def run_classification(scope, *, config_path=CONFIG, client=None, concurrency=128):
    config, binding = production_guard(config_path, require_classifier=True)
    _scope(scope, binding)
    source = BASE / scope
    frame = _json(RESULTS / f'frame_{scope}.json')
    if (frame['status'] != 'complete' or frame['binding'] != binding
            or file_sha(source / 'pairs_candidates.jsonl') != frame['pairs_sha256']
            or file_sha(source / 'extractions.jsonl') != frame['extraction_sha256']
            or file_sha(source / 'claims_normalized.jsonl') != frame['claims_sha256']):
        raise ExperimentBlocked('Candidate frame is incomplete or changed')
    pairs = list(read_jsonl(source / 'pairs_candidates.jsonl'))
    extractions = {e['pmid']: e for e in read_jsonl(source / 'extractions.jsonl')}
    articles = {a['pmid']: a for a in read_jsonl('data/derived/corpus.jsonl') if a['pmid'] in extractions}
    adapted = classifier_routes(config, _json(config.get('routing_config', 'configs/routing_registry.json')))
    route_path = RESULTS / 'classifier_routes.json'
    if route_path.exists() and _json(route_path) != adapted:
        raise ExperimentBlocked('Classifier routing adapter changed during production')
    write_json(route_path, adapted)
    own = client is None
    client = client or V2Client('production_classification', config=route_path, concurrency=concurrency)
    try:
        with _lock(RESULTS / f'classification_{scope}.lock'):
            records = await _workers(pairs, lambda pair: _classify_one(client, pair, articles, extractions, config,
                {**binding, **config['_classifier_binding']}, BASE / 'classification'), concurrency=concurrency,
                progress_path=RESULTS / f'classification_{scope}_progress.json')
    finally:
        if own:
            await client.close()
    by_id = {r['pair_id']: r for r in records}
    output = [{**pair, 'classification_status': by_id[pair['pair_id']]['status'],
               'verdict': by_id[pair['pair_id']]['verdict']} for pair in pairs]
    write_jsonl(source / 'pairs_classified.jsonl', output)
    summary = {'status': 'complete', 'scope': scope, 'binding': binding, 'requested_pairs': len(pairs),
               'terminal_pairs': len(records), 'valid_pairs': sum(r['status'] == 'valid' for r in records),
               'invalid_pairs': sum(r['status'] == 'invalid' for r in records),
               'output_sha256': file_sha(source / 'pairs_classified.jsonl'), 'completed_at': utcnow(),
               'classifier_binding': config['_classifier_binding']}
    write_json(RESULTS / f'classification_{scope}.json', summary)
    return summary


def freeze_frame(scope, *, config_path=CONFIG):
    config, binding = production_guard(config_path, require_classifier=True)
    order, partitions = _scope(scope, binding)
    extraction = _json(RESULTS / f'extraction_{scope}.json')
    classification = _json(RESULTS / f'classification_{scope}.json')
    frame = _json(RESULTS / f'frame_{scope}.json')
    if (extraction.get('status') != 'complete' or extraction['terminal_sources'] != len(order)
            or classification.get('status') != 'complete'
            or classification['terminal_pairs'] != classification['requested_pairs']
            or extraction['binding'] != binding or classification['binding'] != binding
            or frame['binding'] != binding or frame['pairs'] != classification['requested_pairs']
            or classification.get('classifier_binding') != config['_classifier_binding']):
        raise ExperimentBlocked('Cannot freeze unfinished or mismatched production frame')
    source = BASE / scope
    paths = [source / name for name in ['extractions.jsonl', 'claims_normalized.jsonl', 'pairs_candidates.jsonl', 'pairs_classified.jsonl']]
    if (file_sha(source / 'extractions.jsonl') != extraction['extractions_sha256']
            or file_sha(source / 'pairs_classified.jsonl') != classification['output_sha256']
            or file_sha(source / 'pairs_candidates.jsonl') != frame['pairs_sha256']
            or file_sha(source / 'claims_normalized.jsonl') != frame['claims_sha256']):
        raise ExperimentBlocked('Production source bytes changed before freeze')
    manifest = {'status': 'complete_frozen', 'pipeline_version': '2.0', 'scope': scope,
                'binding': binding, 'frozen_at': utcnow(), 'source_pmids': order,
                'eligible_reference_pmids': sorted((set(partitions['partition_pmids']['F']) - set(config['_extra_development_pmids'])) & set(order)),
                'extra_development_pmids': config['_extra_development_pmids'],
                'classifier_binding': config['_classifier_binding'],
                'extraction': extraction, 'classification': classification,
                'files': {p.name: {'path': str(p), 'sha256': file_sha(p)} for p in paths},
                'config_path': str(config_path), 'terminal_failures_preserved': True}
    path = RESULTS / f'frozen_frame_{scope}.json'
    if path.exists():
        prior = _json(path)
        if any(prior[k] != manifest[k] for k in ('binding', 'source_pmids', 'eligible_reference_pmids', 'classifier_binding', 'files')):
            raise ExperimentBlocked('Refusing to replace frozen v2 frame')
        return prior
    write_json(path, manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'extract', 'assemble', 'frame', 'classify', 'freeze'])
    parser.add_argument('--scope', choices=['full', 'reserve'])
    parser.add_argument('--config', default=str(CONFIG))
    parser.add_argument('--projection')
    parser.add_argument('--concurrency', type=int, default=128)
    args = parser.parse_args()
    if args.action == 'plan':
        result = plan_scope(args.projection, args.config)
    elif args.action in {'extract', 'classify'}:
        result = asyncio.run((run_extraction if args.action == 'extract' else run_classification)(args.scope, config_path=args.config, concurrency=args.concurrency))
    else:
        result = {'assemble': assemble_extraction, 'frame': build_frame, 'freeze': freeze_frame}[args.action](args.scope, config_path=args.config)
    print(json.dumps(result, indent=2))
