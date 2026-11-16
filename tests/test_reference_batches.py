"""Two-judge provider batches with fake HTTP only; no credential, network or model call."""
import itertools
import json
import threading
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import test_reference_oracles as fixtures

from chl_rag import site_instrument as instrument
from chl_rag import reference_batches as batches
from chl_rag import reference_oracles as refs
from chl_rag import reference_revision as revision
from chl_rag.io import digest, file_sha, write_json
from chl_rag.spend_budget import PhaseLedger
from chl_rag.judges import require_active_judge
from chl_rag.two_judges import TwoJudgePolicyError, pilot_pair_ids, two_judge_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DRAFT = json.loads((PROJECT_ROOT / 'configs/two_judges_template.json').read_text())['evaluation_config_fields']
SECRETS = {'astra': 'sk-proj-TESTSECRETastra0123456789abcdef', 'fable': 'sk-ant-TESTSECRETfable0123456789abcdef'}
CUTOFF = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)  # 11:00 CET
SITE = json.loads((PROJECT_ROOT / 'configs/site_instrument.json').read_text())


def answer(item):
    """A valid strict-wire answer for the 61c08a1 binding (offline fixture encoding)."""
    canonical = {'instrument_version': '2.0', 'pipeline_version': '2.0', 'item_id': item['item_id'], 'task': 'pair',
                 'path': deepcopy(SITE['example_paths']['concordant']), 'rationale': ''}
    return instrument.encode_response(canonical, item)


def policy(lifted=True):
    value = deepcopy(DRAFT)
    if lifted:
        value['anthropic_stop'] = {'status': 'lifted', 'lifted_at': '2026-11-16T01:30:00+01:00',
            'authority': {'actor': 'Antonio', 'kind': 'explicit_lift', 'verbatim': 'Synthetic lift', 'source': 'synthetic test'}}
    return value


def _items():
    case = json.loads((PROJECT_ROOT / 'configs/synthetic.json').read_text())['cases'][0]
    from chl_rag.extraction import extraction_item, pair_item, validate_extraction
    a = deepcopy(case['article']); b = deepcopy(a); b['pmid'] = '899999997'
    ext_a = validate_extraction(a, case['expected_model_output'])['extraction']
    ext_b = validate_extraction(b, case['expected_model_output'])['extraction']
    arm = ext_a['claims'][0]['arm']
    pair = pair_item('v2-pair-700001', arm, a, ext_a, ext_a['claims'][0]['claim_id'], b, ext_b, ext_b['claims'][0]['claim_id'])
    return pair, extraction_item('v2-extraction-001', arm, a, ext_a)


def environment(tmp_path, monkeypatch, judge, *, config=None, allocation=500):
    # The shared fixture predates site b5e77f9: bind the companion check to the fixture's own
    # site export so the real revision gates still run (pre-existing HEAD failures otherwise).
    monkeypatch.setattr(revision, 'SITE_SHA256', file_sha(PROJECT_ROOT / 'configs/site_instrument_study_start.json'))
    env = fixtures.reference_environment(tmp_path, monkeypatch, overrides=config if config is not None else policy())
    env.release()
    funding = {'status': 'confirmed', 'authority': 'synthetic', 'final_fable_topup_confirmed': True,
               'final_astra_openai_funding_confirmed': True,
               'vendors': {vendor: {'available_credit': allocation, 'alert': allocation + 1,
                                    'phase_alerts': {'final_reference': allocation}} for vendor in ('openai', 'anthropic')}}
    write_json(env.root / 'configs/funding.json', funding)
    env.sign(env.root / 'configs/funding.json')
    monkeypatch.setattr(refs, 'FUNDING', Path('configs/funding.json'))
    pair, extraction = _items()
    data = [{**deepcopy(pair), 'item_id': f'v2-pair-{i:03d}'} for i in range(1, 5)]
    data.append({**deepcopy(extraction), 'item_id': 'v2-extraction-001'})
    settings = two_judge_settings(env.view, judge, 'max')
    files = {str(env.config_path): file_sha(env.config_path), **refs._runtime_binding(env.view),
             **refs.reference_revision_files(env.view)}
    plan = refs.ReferencePlan('final_evaluation', judge, 'max', str(env.config_path), tuple(data),
        tuple(row['item_id'] for row in data), tuple(row['item_id'] for row in data), pilot_pair_ids(data),
        str(env.root / 'output' / judge), digest(data), 'synthetic-frame', {'reference_revision': env.provenance},
        files, settings, {}, refs._AUTHORIZATION_TOKEN)
    key = env.root / f'{judge}.env'
    key.write_text(('OPENAI_API_KEY=' if judge == 'astra' else 'ANTHROPIC_API_KEY=') + SECRETS[judge] + '\n')
    key.chmod(0o600)
    ledger = PhaseLedger('openai' if judge == 'astra' else 'anthropic', 'final_reference', root=env.root)
    return env, plan, key, ledger


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class FakeProviders:
    """OpenAI files/batches/responses and Anthropic message batches/messages, in memory."""

    def __init__(self, plan, *, script=None, finish_after_polls=1):
        self.plan, self.items = plan, {row['item_id']: row for row in plan.items}
        self.script = script or {}
        self.finish_after = finish_after_polls
        self.files, self.batches, self.log = {}, {}, []
        self.lock = threading.Lock()
        self.sync_live = self.sync_peak = 0
        self.cancel_requests = []
        self.never_finish = set()
        self.sync_barrier = self.create_barrier = None

    def text_for(self, item_id):
        queue = self.script.get(item_id)
        if queue:
            return queue.pop(0)
        return json.dumps(answer(self.items[item_id]))

    @staticmethod
    def item_of(custom_id):
        return custom_id.rsplit('-d', 1)[0]

    def openai_body(self, item_id, text):
        if text == 'TRUNCATE':
            return {'id': 'resp_x', 'object': 'response', 'status': 'incomplete', 'model': 'gpt-6-astra',
                    'incomplete_details': {'reason': 'max_output_tokens'}, 'output': [],
                    'usage': {'input_tokens': 9000, 'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0,
                              'cache_write_tokens_details': {'ttl_30m_tokens': 0, 'ttl_12h_tokens': 0}},
                              'output_tokens': 128000, 'output_tokens_details': {'reasoning_tokens': 128000}}}
        return {'id': 'resp_' + item_id, 'object': 'response', 'status': 'completed', 'model': 'gpt-6-astra',
                'output': [{'type': 'reasoning', 'summary': []},
                           {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}],
                'usage': {'input_tokens': 12000, 'input_tokens_details': {'cached_tokens': 9000, 'cache_write_tokens': 0,
                          'cache_write_tokens_details': {'ttl_30m_tokens': 0, 'ttl_12h_tokens': 0}},
                          'output_tokens': 6000, 'output_tokens_details': {'reasoning_tokens': 5000}}}

    def anthropic_result(self, custom_id, text):
        if text in {'ERRORED', 'CANCELED'}:
            kind = 'errored' if text == 'ERRORED' else 'canceled'
            result = {'type': kind}
            if kind == 'errored':
                result['error'] = {'type': 'error', 'error': {'type': 'api_error', 'message': 'synthetic'}}
            return {'custom_id': custom_id, 'result': result}
        return {'custom_id': custom_id, 'result': {'type': 'succeeded', 'message': {
            'id': 'msg_x', 'type': 'message', 'role': 'assistant', 'model': 'claude-fable-5-2',
            'content': [{'type': 'thinking', 'thinking': '...'}, {'type': 'text', 'text': text}],
            'stop_reason': 'max_tokens' if text == 'TRUNCATE' else 'end_turn',
            'usage': {'input_tokens': 3000, 'output_tokens': 7000, 'cache_read_input_tokens': 9000,
                      'cache_creation_input_tokens': 0}}}}

    def handler(self, request):
        with self.lock:
            self.log.append((request.method, request.url.path))
        path, method = request.url.path, request.method
        auth = request.headers.get('authorization', '') + request.headers.get('x-api-key', '')
        assert auth.endswith(SECRETS[self.plan.judge])
        if path == '/v1/files' and method == 'POST':
            if self.create_barrier:
                self.create_barrier.wait()  # every item's batch is being created at the same time
            body = request.content
            start = body.index(b'{"custom_id"')
            line = json.loads(body[start:body.index(b'\n', start)])
            assert line['url'] == '/v1/responses' and 'stream' not in line['body']
            file_id = f'file-{len(self.files) + 1}'
            self.files[file_id] = line
            return httpx.Response(200, json={'id': file_id, 'object': 'file', 'purpose': 'batch'})
        if path == '/v1/batches' and method == 'POST':
            payload = json.loads(request.content)
            assert payload['endpoint'] == '/v1/responses' and payload['completion_window'] == '24h'
            line = self.files[payload['input_file_id']]
            batch_id = f'batch_{len(self.batches) + 1}'
            self.batches[batch_id] = {'custom_id': line['custom_id'], 'polls': 0, 'cancel': False}
            return httpx.Response(200, json={'id': batch_id, 'object': 'batch', 'status': 'validating'})
        if path == '/v1/messages/batches' and method == 'POST':
            payload = json.loads(request.content)
            assert len(payload['requests']) == 1 and 'stream' not in payload['requests'][0]['params']
            batch_id = f'msgbatch_{len(self.batches) + 1}'
            self.batches[batch_id] = {'custom_id': payload['requests'][0]['custom_id'], 'polls': 0, 'cancel': False}
            return httpx.Response(200, json={'id': batch_id, 'type': 'message_batch', 'processing_status': 'in_progress'})
        for prefix in ('/v1/batches/', '/v1/messages/batches/'):
            if path.startswith(prefix):
                rest = path[len(prefix):]
                batch_id, _, action = rest.partition('/')
                state = self.batches[batch_id]
                item_id = self.item_of(state['custom_id'])
                if action == 'cancel':
                    state['cancel'] = True
                    self.cancel_requests.append(batch_id)
                    return httpx.Response(200, json={'id': batch_id})
                if action == 'results':
                    text = state['text']
                    return httpx.Response(200, text=json.dumps(self.anthropic_result(state['custom_id'], text)) + '\n')
                state['polls'] += 1
                done = state['polls'] >= self.finish_after and item_id not in self.never_finish
                if prefix == '/v1/batches/':
                    if state['cancel']:
                        return httpx.Response(200, json={'id': batch_id, 'status': 'cancelled', 'output_file_id': None,
                                                         'error_file_id': None})
                    if not done:
                        return httpx.Response(200, json={'id': batch_id, 'status': 'in_progress'})
                    if 'out' not in state:
                        state['out'] = f'file-out-{batch_id}'
                        text = self.text_for(item_id)
                        self.files[state['out']] = {'id': 'batch_req_1', 'custom_id': state['custom_id'], 'response': {
                            'status_code': 200, 'request_id': 'req_1', 'body': self.openai_body(item_id, text)}, 'error': None}
                    return httpx.Response(200, json={'id': batch_id, 'status': 'completed', 'output_file_id': state['out'],
                                                     'error_file_id': None, 'request_counts': {'total': 1, 'completed': 1, 'failed': 0}})
                if state['cancel'] and not done:
                    state['text'] = 'CANCELED'
                    return httpx.Response(200, json={'id': batch_id, 'processing_status': 'ended', 'results_url': None})
                if not done:
                    return httpx.Response(200, json={'id': batch_id, 'processing_status': 'in_progress', 'results_url': None})
                state.setdefault('text', self.text_for(item_id))
                return httpx.Response(200, json={'id': batch_id, 'processing_status': 'ended',
                    'results_url': 'https://api.anthropic.com/v1/messages/batches/' + batch_id + '/results'})
        if path.startswith('/v1/files/') and path.endswith('/content'):
            file_id = path.split('/')[3]
            return httpx.Response(200, text=json.dumps(self.files[file_id]) + '\n')
        if path in {'/v1/responses', '/v1/messages'} and method == 'POST':
            payload = json.loads(request.content)
            assert payload['stream'] is True
            with self.lock:
                self.sync_live += 1
                self.sync_peak = max(self.sync_peak, self.sync_live)
            try:
                if self.sync_barrier:
                    self.sync_barrier.wait()
                if path == '/v1/responses':
                    prompt = payload['input'][1]['content'][0]['text']
                else:
                    prompt = payload['messages'][0]['content'][0]['text']
                item_id = next(i for i, row in self.items.items() if row['task'] == 'pair'
                               and refs.prepare_reference(row, binding='61c08a1')['prompt'] == prompt)
                text = self.text_for(item_id)
                if path == '/v1/responses':
                    body = self.openai_body(item_id, text)
                    events = [{'type': 'response.created', 'response': {'id': body['id']}},
                              {'type': 'response.output_text.delta', 'delta': text},
                              {'type': 'response.completed', 'response': body}]
                else:
                    events = [{'type': 'message_start', 'message': {'model': 'claude-fable-5-2',
                                'usage': {'input_tokens': 3000, 'output_tokens': 1, 'cache_read_input_tokens': 0,
                                          'cache_creation_input_tokens': 0}}},
                              {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                              {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}},
                              {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 5000}},
                              {'type': 'message_stop'}]
                stream = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
                return httpx.Response(200, text=stream, headers={'content-type': 'text/event-stream'})
            finally:
                with self.lock:
                    self.sync_live -= 1
        raise AssertionError(f'Unexpected request {method} {path}')

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def runner(plan, ledger, key, fake, clock, sleeper=lambda _s: None):
    ticks = itertools.count(0, 100)  # every poll interval has elapsed at each tick
    return batches.TwoJudgeRun(plan, ledger=ledger, client=fake.client(), key_path=key, clock=clock,
                               sleeper=sleeper, tick_seconds=0, monotonic=lambda: next(ticks))


# ----------------------------------------------------------------------------- policy

def test_draft_registers_one_max_effort_pairs_only_and_model_maxima():
    for judge in ('astra', 'fable'):
        settings = two_judge_settings(policy(), judge, 'max')
        assert settings['effort'] == 'max' and settings['max_tokens'] == 128000 and settings['tasks'] == ('pair',)
    for change in ({'effort': 'xhigh'}, {'tasks': ['pair', 'extraction']}, {'max_attempts': 3}, {'pair_binding': '5208dfa'}):
        bad = policy(); bad['references'].update(change)
        with pytest.raises(TwoJudgePolicyError):
            two_judge_settings(bad, 'astra', 'max')
    bad = policy(); bad['references']['fable']['pair_effort'] = 'high'
    with pytest.raises(TwoJudgePolicyError, match='override'):
        two_judge_settings(bad, 'fable', 'max')
    bad = policy(); bad['references']['fable']['max_tokens'] = 64000
    with pytest.raises(TwoJudgePolicyError, match='maximum'):
        two_judge_settings(bad, 'fable', 'max')
    with pytest.raises(TwoJudgePolicyError):
        two_judge_settings(policy(), 'astra', 'xhigh')


def test_fable_stays_disabled_until_signed_lift():
    with pytest.raises(RuntimeError, match='permanently disabled'):
        require_active_judge('fable')
    with pytest.raises(RuntimeError, match='permanently disabled'):
        require_active_judge('fable', policy(lifted=False))
    astra_only = {**policy(), 'active_judges': ['astra'], 'judge_policy': 'astra_only'}
    with pytest.raises(RuntimeError, match='permanently disabled'):
        require_active_judge('fable', astra_only)
    require_active_judge('fable', policy())


def test_pilot_rule_is_seeded_and_pairs_only():
    rows = [{'item_id': f'v2-pair-{i:03d}', 'task': 'pair'} for i in range(1, 36)] + [{'item_id': 'v2-extraction-001', 'task': 'extraction'}]
    pilot = pilot_pair_ids(rows)
    assert len(pilot) == 2 and all(item.startswith('v2-pair-') for item in pilot)
    assert pilot == pilot_pair_ids(list(reversed(rows)))


# ----------------------------------------------------------------------------- requests and money

def test_request_shapes_and_identical_model_parameters(tmp_path, monkeypatch):
    _env, plan, _key, _ledger = environment(tmp_path, monkeypatch, 'astra')
    reference = refs.prepare_reference(plan.items[0], binding='61c08a1')
    assert reference['pair_wire_version'] == 'v2-pair-required-null-61c08a1'
    body = batches.astra_body(reference, plan.settings)
    assert body['model'] == 'gpt-6-astra' and body['reasoning'] == {'effort': 'max'}
    assert body['store'] is False and body['max_output_tokens'] == 128000 and 'tools' not in body and 'stream' not in body
    assert body['text']['format'] == {'type': 'json_schema', 'name': 'reference_answer', 'schema': reference['wire_schema'], 'strict': True}
    assert body['input'][0]['role'] == 'developer' and body['input'][0]['content'][0]['text'] == reference['instructions']
    assert body['input'][1]['content'][0]['text'] == reference['prompt']
    fable = two_judge_settings(policy(), 'fable', 'max')
    params = batches.fable_params(reference, fable)
    sync = refs.fable_request(reference, 128000)
    assert 'stream' not in params and sync['stream'] is True
    assert params['output_config'] == sync['output_config'] == {'effort': 'max', 'format': {'type': 'json_schema', 'schema': reference['wire_schema']}}
    assert all(block['cache_control']['ttl'] == '1h' for block in params['system'] + params['messages'][0]['content'] if 'cache_control' in block)
    assert params['system'][0]['text'] == sync['system'][0]['text'] and params['messages'][0]['content'][0]['text'] == sync['messages'][0]['content'][0]['text']


def test_worst_case_bounds_actual_costs_and_allocation_rounding():
    body = {'max_output_tokens': 128000, 'prompt_cache_options': {'ttl': '30m'}, 'x': 'a' * 60000}
    worst = batches.astra_worst_case(body, 'batch')
    usage = {'input_tokens': 64000, 'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 64000,
             'cache_write_tokens_details': {'ttl_30m_tokens': 64000, 'ttl_12h_tokens': 0}}, 'output_tokens': 128000}
    assert batches.astra_cost(usage, 'batch') <= worst
    assert batches.astra_worst_case(body, 'standard') == pytest.approx(2 * worst, rel=1e-4)
    assert batches.astra_cost({**usage, 'input_tokens_details': {'cached_tokens': 60000, 'cache_write_tokens': 0,
        'cache_write_tokens_details': {'ttl_30m_tokens': 0, 'ttl_12h_tokens': 0}}}, 'batch') == pytest.approx(
        (4000 * 5 + 60000 * .5 + 128000 * 25) / 1e6)
    assert batches.allocation_for('fable', 3.01) == 110 and batches.allocation_for('astra', 3.43) == 125


def test_adaptive_cap_halves_on_429_and_recovers():
    now = [0.0]
    cap = batches.AdaptiveCap(64, clock=lambda: now[0])
    cap.observe(429)
    assert cap.allowed() == 32
    for _ in range(5):
        cap.observe(200)
    now[0] = 61
    assert cap.allowed() == 40


# ----------------------------------------------------------------------------- runs

def test_per_item_batch_fanout_concurrent_poll_and_settlement(tmp_path, monkeypatch):
    _env, plan, key, ledger = environment(tmp_path, monkeypatch, 'astra')
    fake = FakeProviders(plan, finish_after_polls=2)
    fake.create_barrier = threading.Barrier(4, timeout=10)
    manifest = runner(plan, ledger, key, fake, Clock(CUTOFF - timedelta(hours=8))).drive('full', allow_inference=True)
    pairs = [i for i in plan.order if i.startswith('v2-pair')]
    assert manifest['valid'] == 4 and manifest['status'] == 'complete' and manifest['requested_item_ids'] == pairs
    assert len(fake.batches) == 4 and len({state['custom_id'] for state in fake.batches.values()}) == 4
    assert all(not row['custom_id'].startswith('v2-extraction') for row in fake.batches.values())
    snapshot = ledger.snapshot()
    assert len(snapshot['calls']) == 4 and not snapshot['pending']
    assert all(call['cost'] == pytest.approx((3000 * 5 + 9000 * .5 + 6000 * 25) / 1e6) for call in snapshot['calls'])
    result = json.loads((Path(plan.output_dir) / 'items' / pairs[0] / 'result.json').read_text())
    assert result['status'] == 'valid' and result['attempt_count'] == 1 and result['dispatches'][0]['route'] == 'batch'


def test_invalid_item_gets_one_immediate_identical_retry_never_a_third(tmp_path, monkeypatch):
    _env, plan, key, ledger = environment(tmp_path, monkeypatch, 'fable')
    fake = FakeProviders(plan, script={'v2-pair-001': ['not json'], 'v2-pair-002': ['TRUNCATE', 'still not json']})
    run = runner(plan, ledger, key, fake, Clock(CUTOFF - timedelta(hours=8)))
    run.drive('full', allow_inference=True)
    root = Path(plan.output_dir) / 'items'
    first = json.loads((root / 'v2-pair-001' / 'result.json').read_text())
    second = json.loads((root / 'v2-pair-002' / 'result.json').read_text())
    assert first['status'] == 'valid' and first['attempt_statuses'] == ['invalid', 'valid']
    assert second['status'] == 'invalid' and second['attempt_statuses'] == ['truncated', 'invalid'] and second['attempt_count'] == 2
    customs = sorted(state['custom_id'] for state in fake.batches.values())
    assert sum(c.startswith('v2-pair-002') for c in customs) == 2 and len(customs) == 6
    retry = json.loads((root / 'v2-pair-001' / 'dispatch-2' / 'request.json').read_text())
    assert retry == json.loads((root / 'v2-pair-001' / 'dispatch-1' / 'request.json').read_text())


def test_cutoff_cancels_keeps_completed_and_sends_rest_at_full_price(tmp_path, monkeypatch):
    _env, plan, key, ledger = environment(tmp_path, monkeypatch, 'fable')
    fake = FakeProviders(plan, script={'v2-pair-003': ['ERRORED']})
    fake.never_finish = {'v2-pair-004'}
    clock = Clock(CUTOFF - timedelta(minutes=5))
    run = runner(plan, ledger, key, fake, clock)
    run.drive('full', allow_inference=True, max_seconds=0.5)
    fake.script['v2-pair-003'] = []
    clock.value = CUTOFF + timedelta(seconds=1)
    manifest = run.drive('full', allow_inference=True)
    root = Path(plan.output_dir) / 'items'
    assert manifest['valid'] == 4 and fake.cancel_requests
    canceled = json.loads((root / 'v2-pair-004' / 'result.json').read_text())
    assert [d['route'] for d in canceled['dispatches']] == ['batch', 'sync']
    assert canceled['dispatches'][0]['counts_as_attempt'] is False and canceled['attempt_count'] == 1
    errored = json.loads((root / 'v2-pair-003' / 'result.json').read_text())
    # api_error is a rejection before generation: not an attempt, so the full-price send is attempt 1
    assert errored['attempt_statuses'] == ['valid'] and [d['attempt'] for d in errored['dispatches']] == [None, 1]
    assert [d['route'] for d in errored['dispatches']] == ['batch', 'sync']
    snapshot = ledger.snapshot()
    assert not snapshot['pending']
    released = [c for c in snapshot['calls'] if c['response_metadata'].get('known_unbilled')]
    assert len(released) == 2  # errored and canceled are not billed


def test_pilot_is_synchronous_and_counts_in_full_when_parameters_match(tmp_path, monkeypatch):
    _env, plan, key, ledger = environment(tmp_path, monkeypatch, 'astra')
    fake = FakeProviders(plan)
    fake.sync_barrier = threading.Barrier(2, timeout=10)  # both pilot pairs are in flight together
    run = runner(plan, ledger, key, fake, Clock(CUTOFF - timedelta(hours=8)))
    pilot = run.drive('pilot', allow_inference=True)
    fake.sync_barrier = None
    assert pilot['valid'] == 2 and not fake.batches and fake.sync_peak == 2
    run.drive('full', allow_inference=True)
    batched = {fake.item_of(state['custom_id']) for state in fake.batches.values()}
    assert batched.isdisjoint(plan.pilot_ids) and len(batched) == 2
    path = Path(plan.output_dir) / 'items' / plan.pilot_ids[0] / 'result.json'
    result = json.loads(path.read_text())
    assert result['dispatches'][0]['route'] == 'sync' and result['finalized_in_phase'] == 'pilot'
    result['params_fingerprint'] = 'changed'
    write_json(path, result)
    with pytest.raises(refs.ExperimentBlocked, match='Pilot parameters differ'):
        run.drive('full', allow_inference=True)


def test_synchronous_waves_never_exceed_the_allocation(tmp_path, monkeypatch):
    _env, plan, key, ledger = environment(tmp_path, monkeypatch, 'astra', allocation=12)
    fake = FakeProviders(plan)
    manifest = runner(plan, ledger, key, fake, Clock(CUTOFF + timedelta(minutes=1))).drive('full', allow_inference=True)
    assert manifest['valid'] == 4 and fake.sync_peak == 1 and not fake.batches


def test_key_never_written_and_rejection_is_redacted(tmp_path, monkeypatch):
    _env, plan, key, ledger = environment(tmp_path, monkeypatch, 'astra')
    fake = FakeProviders(plan)
    runner(plan, ledger, key, fake, Clock(CUTOFF - timedelta(hours=8))).drive('full', allow_inference=True)

    def reject(request):
        return httpx.Response(400, json={'error': {'type': 'invalid_request_error',
                                                    'message': 'bad key ' + SECRETS['astra']}})
    other = tmp_path / 'second'
    _env2, plan2, key2, ledger2 = environment(other, monkeypatch, 'astra')
    fake2 = FakeProviders(plan2)
    fake2.handler = reject
    run = runner(plan2, ledger2, key2, fake2, Clock(CUTOFF - timedelta(hours=8)))
    manifest = run.drive('full', allow_inference=True)
    assert manifest['fatal'] and manifest['valid'] == 0 and not ledger2.snapshot()['pending']
    for root in (tmp_path / 'output', other / 'output', tmp_path / 'results', other / 'results'):
        for path in root.rglob('*'):
            if path.is_file():
                assert SECRETS['astra'] not in path.read_text(errors='ignore'), path


def test_gates_block_before_any_request(tmp_path, monkeypatch):
    env, plan, key, ledger = environment(tmp_path, monkeypatch, 'astra')
    fake = FakeProviders(plan)
    run = runner(plan, ledger, key, fake, Clock(CUTOFF - timedelta(hours=8)))
    with pytest.raises(refs.ExperimentBlocked, match='disabled unless explicitly'):
        run.drive('full')
    Path(env.config['oracle_release_lock_path']).unlink()
    with pytest.raises(refs.ExperimentBlocked, match='HOLD'):
        run.drive('full', allow_inference=True)
    assert not fake.log
    with pytest.raises(refs.ExperimentBlocked, match='batch runner'):
        refs.run_item(plan, plan.expected_ids[0], allow_inference=True, transport=lambda *a: pytest.fail('sync run_item'))


def test_unset_cutoff_and_astra_only_policy_refuse(tmp_path, monkeypatch):
    unset = policy(); unset['references']['cutoff_at'] = None
    _env, plan, key, ledger = environment(tmp_path, monkeypatch, 'astra', config=unset)
    with pytest.raises(refs.ExperimentBlocked, match='cutoff'):
        runner(plan, ledger, key, FakeProviders(plan), Clock(CUTOFF)).drive('full', allow_inference=True)
    with pytest.raises(refs.ExperimentBlocked, match='Message Batches'):
        refs._reference_settings({'active_judges': ['astra'], 'judge_policy': 'astra_only',
                                  'reference_design': 'single_astra_max', 'references': {'transport': 'provider_batch'}},
                                 'astra', 'final_evaluation', 'max')


def page_rule_violation(item):
    """Strict-wire valid, server-valid, but outside what the page offers (decision b: invalid)."""
    from chl_rag import pair_instrument as pair
    rows = {side: {field: {'answer': 'yes', 'problems': []} for field in pair.FIELDS} for side in ('a', 'b')}
    rows['a']['groups'] = {'answer': 'no', 'problems': ['wrong_value']}
    canonical = {'instrument_version': '2.0', 'pipeline_version': '2.0', 'item_id': item['item_id'], 'task': 'pair',
                 'path': {'q1': rows, 'q1_relevance': 'cannot_establish_comparison'}, 'rationale': ''}
    return instrument._encode(canonical, instrument.pair_response_schema(item['item_id']))


def test_page_rule_violation_is_invalid_retried_once_then_missing(tmp_path, monkeypatch):
    _env, plan, key, ledger = environment(tmp_path, monkeypatch, 'astra')
    item = next(row for row in plan.items if row['item_id'] == 'v2-pair-001')
    bad = json.dumps(page_rule_violation(item))
    fake = FakeProviders(plan, script={'v2-pair-001': [bad, bad]})
    runner(plan, ledger, key, fake, Clock(CUTOFF - timedelta(hours=8))).drive('full', allow_inference=True)
    result = json.loads((Path(plan.output_dir) / 'items' / 'v2-pair-001' / 'result.json').read_text())
    assert result['status'] == 'invalid' and result['attempt_statuses'] == ['invalid', 'invalid'] and result['answer'] is None
    metadata = json.loads((Path(plan.output_dir) / 'items' / 'v2-pair-001' / 'dispatch-1' / 'metadata.json').read_text())
    assert 'offered only after a Finding or Comparison' in metadata['validation_error']


def test_rejections_before_generation_retry_with_backoff_and_never_count(tmp_path, monkeypatch):
    _env, plan, key, ledger = environment(tmp_path, monkeypatch, 'fable')
    fake = FakeProviders(plan)
    clock = Clock(CUTOFF - timedelta(hours=8))
    original = fake.handler
    rejected = {'create': 0}

    def handler(request):
        if request.url.path == '/v1/messages/batches' and request.method == 'POST' and rejected['create'] < 2:
            rejected['create'] += 1
            return httpx.Response(529, json={'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'busy'}})
        return original(request)
    fake.handler = handler
    fake.script = {'v2-pair-002': ['ERRORED', 'not json', 'still not json']}  # api_error result, then two invalid outputs
    run = runner(plan, ledger, key, fake, clock)
    run.drive('full', allow_inference=True, max_seconds=1)  # fixed clock: rejected items stay in backoff
    first = [row['outcome'] for i in plan.pilot_ids + ('v2-pair-001', 'v2-pair-002', 'v2-pair-003', 'v2-pair-004')
             for row in run.dispatches(i) if row['outcome'] and row['outcome'].get('http_status') == 529]
    assert rejected['create'] == 2 and len(first) == 2
    assert all(o['counts_as_attempt'] is False and o['attempt'] is None and o['retry_not_before'] for o in first)
    def advance(_seconds):
        clock.value += timedelta(seconds=20)  # wall time passes while the loop waits for backoffs
    run.sleeper = advance
    run.drive('full', allow_inference=True)
    root = Path(plan.output_dir) / 'items'
    for item_id in ('v2-pair-001', 'v2-pair-003', 'v2-pair-004'):
        assert json.loads((root / item_id / 'result.json').read_text())['status'] == 'valid'
    assert (root / 'v2-pair-002' / 'result.json').exists(), [
        (row['dispatch'], row['outcome'] and {k: row['outcome'].get(k) for k in ('status', 'counts_as_attempt', 'retry_not_before', 'retryable')})
        for row in run.dispatches('v2-pair-002')] + [run.fatal, str(clock.value)]
    errored = json.loads((root / 'v2-pair-002' / 'result.json').read_text())
    assert errored['status'] == 'invalid' and errored['attempt_count'] == 2  # errored/overloaded did not use an attempt
    counted = [d['counts_as_attempt'] for d in errored['dispatches']]
    assert counted[-2:] == [True, True] and not any(counted[:-2]) and len(counted) >= 3
    assert not ledger.snapshot()['pending']
    every = [row['outcome'] for i in ('v2-pair-001', 'v2-pair-002', 'v2-pair-003', 'v2-pair-004')
             for row in run.dispatches(i) if row['outcome'] and row['outcome'].get('pre_generation')]
    assert len(every) == 3  # two 529 creations and one api_error result, none counted
