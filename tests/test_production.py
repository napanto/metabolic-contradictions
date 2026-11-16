"""Invented fixed-plan production tests; no paid calls or real source draws."""
import asyncio
import gzip
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from chl_rag import production as prod
from chl_rag.io import digest, file_sha, write_json, write_jsonl
from chl_rag.sampling import SEEDS

FIXTURE = json.loads(Path('configs/synthetic.json').read_text())['cases'][0]
PROMPT = json.loads(Path('configs/extraction_prompt.json').read_text())
CLASSIFIER_PROMPT = json.loads(Path('configs/classification_prompt.json').read_text())
MODEL = 'z-ai/glm-5.3-flash'


class FakeClient:
    def __init__(self):
        self.pmids = []
        self.classifier_calls = 0

    async def generate(self, model, messages, schema, reasoning, max_tokens, **kwargs):
        assert model == MODEL
        payload = json.loads(messages[-1]['content'])
        if 'a' in payload:
            assert reasoning == 'low'
            self.classifier_calls += 1
            evidence = [{'side': side, 'sentence_id': payload[side]['article']['sentences'][1]['id'],
                         'quote': payload[side]['article']['sentences'][1]['text']} for side in ['a', 'b']]
            output = {'label': 'not_replicated', 'factor': 'none', 'rationale': 'Synthetic transport only',
                      'diagnostics': {'material_extraction_error': False, 'error_sides': [], 'error_fields': [],
                                      'uncertain_extraction': False, 'comparison_sufficient': True,
                                      'source_evidence': evidence, 'notes': ''}}
        else:
            assert reasoning == 'high'
            self.pmids.append(payload['pmid'])
            output = FIXTURE['expected_model_output']
        return {'id': 'synthetic', 'provider': 'Synthetic',
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}],
                'usage': {'cost': .001, 'prompt_tokens': 100, 'completion_tokens': 100}}, {
                    'requested_provider': 'synthetic/fp8', 'provider': 'Synthetic',
                    'request_hash': digest(messages), 'cache_hit': False}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # The compatibility verifier intentionally defaults to its installed project
    # root. Bind its real checks to this isolated fixture, just as its unit tests
    # do; changing CWD alone does not change a default argument captured at import.
    from chl_rag import frozen_compat as compat
    verify_frozen_input = compat.verify_frozen_input
    def verify_fixture_input(path, expected_sha, *, config_path=prod.CONFIG):
        return verify_frozen_input(path, expected_sha, config_path=config_path, root=tmp_path)
    monkeypatch.setattr(compat, 'verify_frozen_input', verify_fixture_input)
    monkeypatch.setattr(prod, '_signed_file', lambda path: 'signed-synthetic')
    monkeypatch.setattr(prod.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    a = deepcopy(FIXTURE['article']); b = deepcopy(a); b['pmid'] = '899999999'
    write_jsonl('data/derived/corpus.jsonl', [a, b])
    write_json('data/derived/partitions.json', {
        'full_production_pmids': [a['pmid'], b['pmid'], 'unused-P', 'unused-S', 'unused-H'],
        'reserve_first_production_order': [b['pmid'], 'unused-P', a['pmid'], 'unused-S', 'unused-H'],
        'partition_pmids': {'F': [a['pmid'], b['pmid']], 'P': ['unused-P'], 'S': ['unused-S'], 'H': ['unused-H']},
        'draws': {'reserve': [{'pmid': b['pmid'], 'arm': 'diet'}]}})
    registry = {'configurations': [
        {'id': 'glm-5.3-flash__high', 'model': MODEL, 'reasoning': 'high', 'available': True, 'execution_hold': False},
        {'id': 'glm-5.3-flash__low', 'model': MODEL, 'reasoning': 'low', 'available': True, 'classifier_execution_hold': False}],
        'models': {MODEL: {'execution_hold': False, 'routes': [
            {'tag': 'synthetic/fp8', 'provider_name': 'Synthetic', 'quantization': 'fp8', 'max_completion_tokens': 16384}]}}}
    write_json('configs/routing_registry.json', registry)
    write_json('configs/extraction_prompt.json', PROMPT)
    write_json('configs/classification_prompt.json', CLASSIFIER_PROMPT)
    write_json('configs/oracle_prompt_original.json', {'synthetic': 'oracle'})
    write_json('configs/instrument_original.json', {'synthetic': 'instrument'})
    protocols = ['PROTOCOL_AMENDMENT_06_V2_EVALUATION.md', prod.ASTRA_ONLY_PROTOCOL,
                 prod.PRESPECIFIED_PROTOCOL, 'PROTOCOL_ADDENDUM_05a_PROMPT_FREEZE.md']
    for path in protocols:Path(path).write_text('Synthetic signed authority')
    prompts = {'extraction': 'configs/extraction_prompt.json', 'metabolic_classifier': 'configs/classification_prompt.json',
               'oracle': 'configs/oracle_prompt_original.json', 'instrument': 'configs/instrument_original.json'}
    freeze = {'status': 'frozen', 'protocol': protocols[-1], 'protocol_sha256': file_sha(protocols[-1]),
              'prompt_files': prompts, 'reviewed_prompt_sha256': {p:file_sha(p) for p in prompts.values()},
              'production_caps': {'glm-5.3-flash__high': 8192, 'glm-5.3-flash__low': 4096}}
    write_json('results/prompt_freeze.json', freeze)
    write_json('configs/vocabularies.json', {'concepts': []})
    write_json('results/index.json', {'synthetic': True})
    path = Path('data/derived/pubtator.jsonl.gz'); path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, 'wt') as handle:
        for article in [a, b]:handle.write(json.dumps({'pmid': article['pmid'], 'entities': [], 'relations': []})+'\n')
    write_json('results/v2_classifier_selection.json', {'status': 'selected', 'selection_split': 'dev',
        'selected': {'model': 'external-benchmark-only', 'reasoning': 'off'}})
    sources = protocols + list(prompts.values()) + ['configs/routing_registry.json', 'results/prompt_freeze.json',
               'data/derived/partitions.json', 'results/index.json']
    config = {'status': 'registered', 'pipeline_version': '2.0', 'protocol': protocols[0],
              'production_design': 'prespecified_fixed',
              'comparison_status': {'S': 'not_run', 'H': 'not_run', 'metabolic_classifier_experiment': 'not_run'},
              'active_judges': ['astra'], 'judge_policy': 'astra_only', 'reference_design': 'single_astra_max',
              'perimeter_policy': 'full_F_operational_failure_only', 'production_source_count': 2,
              'sampling_seeds': SEEDS, 'instrument_version': 'synthetic-v2',
              'source_sha256': {p:file_sha(p) for p in sources},
              'extraction': {'configuration_id': 'glm-5.3-flash__high', 'model': MODEL, 'reasoning': 'high',
                             'max_tokens': 8192, 'verify': False},
              'classification': {'configuration_id': 'glm-5.3-flash__low', 'model': MODEL, 'reasoning': 'low', 'max_tokens': 4096},
              'external_benchmark': {'selection_path': 'results/v2_classifier_selection.json',
                  'selection_sha256': file_sha('results/v2_classifier_selection.json'), 'merge_commit': 'signed-benchmark'},
              # These deliberately expired deadlines cannot shrink F or stop work.
              'timing': {'projection_margin': 1.5, 'full_extraction_deadline': '2000-01-01T01:00:00+01:00',
                         'classification_deadline': '2000-01-01T02:00:00+01:00', 'package_deadline': '2000-01-01T03:00:00+01:00'}}
    write_json(prod.CONFIG, config)
    # Sampling now records a separately registered reference view. This test is
    # about the production frame/package boundary, not the reference amendment;
    # supply an explicit synthetic view without consulting the live repository.
    # Its provenance validator still runs; authority checks have their own tests.
    from chl_rag import reference_revision as reference
    fixture_config_sha = file_sha(prod.CONFIG)
    monkeypatch.setattr(reference, 'BASE_SHA256', fixture_config_sha)
    def fixture_reference_config(config_path):
        value = json.loads(Path(config_path).read_text())
        value['evaluation'] = {'instrument_sha256': file_sha('configs/instrument_original.json'),
                               'site_instrument_sha256': digest({'synthetic_site_only': True})}
        value['_reference_revision'] = {
            'base_evaluation_config_sha256': fixture_config_sha,
            'reference_revision_sha256': digest({'synthetic_revision_only': True}),
            'reference_effective_sha256': digest(value['evaluation']),
            'synthetic_only': True}
        return value
    monkeypatch.setattr(reference, 'effective_reference_config', fixture_reference_config)
    return config, a, b


def test_fixed_guard_requires_no_selection_h_or_120_artifact(project, monkeypatch):
    original = prod._json
    def guarded_read(path):
        assert not any(token in str(path) for token in ('H_check', '/selection.json', '/references/', 'classifier_development', 'v1_extraction'))
        return original(path)
    monkeypatch.setattr(prod, '_json', guarded_read)
    config, binding = prod.production_guard(require_classifier=True)
    assert binding['comparison_status'] == {'S': 'not_run', 'H': 'not_run', 'metabolic_classifier_experiment': 'not_run'}
    assert config['_extra_development_pmids'] == []
    assert config['classification']['model'] == MODEL


@pytest.mark.parametrize('change', [
    {'production_design': 'comparative'}, {'comparison_status': {'H': 'passed'}},
    {'perimeter_policy': 'time_only'}, {'production_source_count': 1}, {'active_judges': ['astra', 'fable']}])
def test_obsolete_design_and_false_pass_markers_fail_closed(project, change):
    config, _, _ = project; config.update(change); write_json(prod.CONFIG, config)
    with pytest.raises(prod.ExperimentBlocked):prod.production_guard()


@pytest.mark.parametrize('role,key,value', [
    ('extraction', 'model', 'other'), ('extraction', 'reasoning', 'low'), ('extraction', 'verify', True),
    ('extraction', 'H_check_path', 'forbidden.json'), ('classification', 'reasoning', 'high'),
    ('classification', 'selection_path', 'forbidden.json'), ('classification', 'max_tokens', 8192)])
def test_fixed_model_onepass_and_p_caps_cannot_change(project, role, key, value):
    config, _, _ = project; config[role][key] = value; write_json(prod.CONFIG, config)
    with pytest.raises(prod.ExperimentBlocked):prod.production_guard()


def test_prompt_freeze_and_signed_fixed_authority_are_mandatory(project):
    config, _, _ = project
    config['source_sha256'].pop(prod.PRESPECIFIED_PROTOCOL); write_json(prod.CONFIG, config)
    with pytest.raises(prod.ExperimentBlocked, match='signed fixed-plan'):prod.production_guard()
    config['source_sha256'][prod.PRESPECIFIED_PROTOCOL] = file_sha(prod.PRESPECIFIED_PROTOCOL)
    freeze = json.loads(Path('results/prompt_freeze.json').read_text()); freeze['status'] = 'draft'
    write_json('results/prompt_freeze.json', freeze)
    config['source_sha256']['results/prompt_freeze.json'] = file_sha('results/prompt_freeze.json')
    write_json(prod.CONFIG, config)
    with pytest.raises(prod.ExperimentBlocked, match='reviewed prompt freeze'):prod.production_guard()


def test_full_F_runs_after_deadline_and_resumes_without_inference(project):
    _, a, b = project; client = FakeClient()
    summary = asyncio.run(prod.run_extraction('full', client=client, concurrency=1))
    assert client.pmids == [b['pmid'], a['pmid']]
    assert summary['source_pmids'] == [b['pmid'], a['pmid']]
    assert summary['terminal_sources'] == 2
    asyncio.run(prod.run_extraction('full', client=client, concurrency=1))
    assert len(client.pmids) == 2
    assert not (prod.RESULTS/'time_fallback.json').exists()


def test_complete_fixed_pipeline_preserves_all_F_and_checkpoint_bytes(project):
    _, a, b = project; client = FakeClient()
    asyncio.run(prod.run_extraction('full', client=client, concurrency=1))
    frame = prod.build_frame('full', retriever=SimpleNamespace(search=lambda query, k: []))
    assert frame['pairs'] == 2
    result = asyncio.run(prod.run_classification('full', client=client, concurrency=1))
    assert result['terminal_pairs'] == 2
    paths = sorted((prod.BASE/'classification').glob('*.json'))
    original = {p:p.read_bytes() for p in paths}
    asyncio.run(prod.run_classification('full', client=client, concurrency=1))
    assert client.classifier_calls == 2 and {p:p.read_bytes() for p in paths} == original
    frozen = prod.freeze_frame('full')
    assert set(frozen['eligible_reference_pmids']) == {a['pmid'], b['pmid']}
    assert frozen['extra_development_pmids'] == []
    assert prod.freeze_frame('full')['files'] == frozen['files']
    from chl_rag.sampling import prepare
    package = prepare(prod.RESULTS/'frozen_frame_full.json')
    assert package['human_subset_frozen_before_oracle'] is True
    assert package['full_items'] > 0
    Path(frozen['files']['pairs_candidates.jsonl']['path']).write_text('tampered\n')
    with pytest.raises(prod.ExperimentBlocked, match='changed'):prod.freeze_frame('full')


@pytest.mark.parametrize('swap', ['whole_checkpoint', 'output_only'])
def test_swapped_source_checkpoint_is_rejected_before_export(project, swap):
    config, a, b = project
    asyncio.run(prod.run_extraction('full', client=FakeClient(), concurrency=1))
    apath = prod._extract_path(config, a['pmid'])
    left = json.loads(apath.read_text())
    right = json.loads(prod._extract_path(config, b['pmid']).read_text())
    write_json(apath, right if swap == 'whole_checkpoint' else {**left, 'output': right['output']})
    with pytest.raises(prod.ExperimentBlocked, match='source PMID differs'):
        prod.assemble_extraction('full')


def test_unfinished_sources_cannot_define_C(project):
    with pytest.raises(prod.ExperimentBlocked, match='unfinished'):prod.assemble_extraction('full')


def test_extraction_does_not_wait_for_benchmark_merge_but_classification_does(project, monkeypatch):
    monkeypatch.setattr(prod.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1))
    assert prod.production_guard()[0]['extraction']['model'] == MODEL
    with pytest.raises(prod.ExperimentBlocked, match='has not been integrated'):prod.production_guard(require_classifier=True)


def test_scope_projection_cannot_replace_full_with_time_or_cost_reserve(project):
    config, _, _ = project
    decision = prod.choose_scope('2099-01-01T00:00:00+00:00', 1000000, 1000000, config['timing'])
    assert decision['scope'] == 'full' and decision['within_target_times'] is False
    _, binding = prod.production_guard()
    with pytest.raises(prod.ExperimentBlocked, match='signed documented operational failure'):prod._scope('reserve', binding)


def test_reserve_requires_signed_failure_with_unchanged_evidence(project):
    _, _, b = project; _, binding = prod.production_guard()
    Path('outage.txt').write_text('Synthetic provider outage trace')
    failure = {'status': 'confirmed', 'scope': 'reserve', 'basis': 'operational_failure', 'full_F_impossible': True,
               'time_only': False, 'cost_used': False, 'config_sha256': binding['config_sha256'],
               'description': 'Synthetic provider outage prevents completion',
               'evidence': [{'path': 'outage.txt', 'sha256': file_sha('outage.txt')}]}
    write_json('configs/v2_operational_failure.json', failure)
    assert prod._scope('reserve', binding)[0] == [b['pmid']]
    failure['cost_used'] = True; write_json('configs/v2_operational_failure.json', failure)
    with pytest.raises(prod.ExperimentBlocked, match='operationally impossible'):prod._scope('reserve', binding)


def test_never_open_development_reference_or_D0_for_fixed_production(project, monkeypatch):
    config, a, _ = project; _, binding = prod.production_guard()
    original = prod._json
    def only_partitions(path):
        assert str(path) == 'data/derived/partitions.json'
        return original(path)
    monkeypatch.setattr(prod, '_json', only_partitions)
    assert prod.reuse_development_checkpoint(a, config, binding) is None
    with pytest.raises(prod.ExperimentBlocked, match='P/S/H'):
        prod.reuse_development_checkpoint({**a, 'pmid': 'unused-H'}, config, binding)


def test_concurrent_production_controllers_share_one_immutable_source(project):
    from chl_rag.selected_extraction import materialize
    config, a, _ = project; _, binding = prod.production_guard()
    class SlowClient(FakeClient):
        async def generate(self, *args, **kwargs):
            await asyncio.sleep(.05)
            return await super().generate(*args, **kwargs)
    first, second = SlowClient(), SlowClient()
    async def both():
        return await asyncio.gather(
            materialize(a, config, binding, client=first, allow_paid=True, requested_phase='production'),
            materialize(a, config, binding, client=second, allow_paid=True, requested_phase='production'))
    left, right = asyncio.run(both())
    assert len(first.pmids)+len(second.pmids) == 1 and left == right
