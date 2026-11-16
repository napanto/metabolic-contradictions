"""Two-judge final references: per-item provider batches, synchronous pilot and fallback.

Astra (gpt-6-astra) uses the OpenAI Responses API: one single-request OpenAI
Batch per item before the registered cutoff, and the synchronous streamed
Responses endpoint for the pilot and for items still without a result at the
cutoff. Fable 5.2 uses one single-request Anthropic Message Batch per item, and
the existing streamed Messages path (_fable_once) for the pilot and fallback.

Nothing here runs on import. Every network action needs a ReferencePlan from
the signed loader, the signed two-judge policy, Antonio's signed release lock,
an explicit allow flag and, for Fable, the signed lift of the Anthropic stop.
Credentials are read inside the program and never written to disk or errors.
Sources for every API fact: papers/inbox-2026-11-16/acquisition.json.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import stat
import threading
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import httpx

from . import reference_oracles as refs
from .io import canonical, digest, file_sha, utcnow, write_json, write_jsonl
from .llm import BudgetError
from .adaptive_transport import POLICY as ADAPTIVE_POLICY
from .spend_budget import ReservationCapacityWait
from .model_runs import ExperimentBlocked
from .judges import FABLE_PRICES, _safe_http_error, usage_cost, worst_case_cost
from .two_judges import MODELS, TRANSPORTS, cutoff, is_two_judge

OPENAI_BASE = 'https://api.openai.com/v1'
ANTHROPIC_BASE = 'https://api.anthropic.com/v1'
OPENAI_KEY = Path.home() / '.config/chl-openai.env'
ANTHROPIC_KEY = Path.home() / '.config/chl-anthropic.env'
SCHEMA_NAME = 'reference_answer'
CUSTOM_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
# USD per million tokens, official GPT-6 Astra model and pricing pages (16 November 2026).
ASTRA_PRICES = {
    'batch': {'input': 5.0, 'cached': 0.5, 'write_30m': 6.25, 'write_12h': 12.5, 'output': 25.0},
    'standard': {'input': 10.0, 'cached': 1.0, 'write_30m': 12.5, 'write_12h': 25.0, 'output': 50.0}}
ASTRA_LONG_PRICES = {
    'batch': {'input': 10.0, 'cached': 1.0, 'write_30m': 12.5, 'write_12h': 25.0, 'output': 37.5},
    'standard': {'input': 20.0, 'cached': 2.0, 'write_30m': 25.0, 'write_12h': 50.0, 'output': 75.0}}
ASTRA_SHORT_CONTEXT_TOKENS = 272000
# Message Batches: 50 percent of standard prices on all usage (batch-processing guide).
FABLE_BATCH_PRICES = {key: value / 2 for key, value in FABLE_PRICES.items()}
OPENAI_TERMINAL = {'completed', 'expired', 'cancelled', 'failed'}
SYNC_STATUS_FREE = {400, 401, 403, 404, 413, 422, 429}
# Antonio, 16 November: rejections before generation are retried with backoff until accepted
# and never count as an attempt (429, 529/overloaded, 5xx without generation, provider-side
# batch validation failure, connection errors before any output).
PRE_GENERATION_HTTP = frozenset({429, 529})
PRE_GENERATION_ERROR_TYPES = frozenset({'api_error', 'overloaded_error', 'rate_limit_error', 'timeout_error'})
BACKOFF_BASE_SECONDS, BACKOFF_MAX_SECONDS = 15, 300


def pre_generation_status(status):
    return status is not None and (status in PRE_GENERATION_HTTP or status >= 500)


class StopRun(Exception):
    """A configuration-level failure that must stop new admissions."""


def _read_env_key(path, name):
    """Owner-only mode-600 file with exactly one NAME=value line; never echoed."""
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError(f'{name} file must be a regular owner-only mode-600 file')
    lines = path.read_text().strip().splitlines()
    if len(lines) != 1 or not lines[0].startswith(name + '='):
        raise RuntimeError(f'Expected exactly one {name} assignment')
    key = lines[0].split('=', 1)[1].strip()
    if not key or key[0] in "\"'" or any(char.isspace() for char in key):
        raise RuntimeError(f'Malformed {name} assignment')
    return key


def _money_up(value):
    return float(Decimal(str(value)).quantize(Decimal('0.000001'), rounding=ROUND_CEILING))


# ----------------------------------------------------------------------------- requests

def astra_body(reference, settings):
    """Responses request; identical inside a batch line and on the synchronous path."""
    return {'model': MODELS['astra'], 'reasoning': {'effort': settings['effort']},
            'max_output_tokens': settings['max_output_tokens'], 'store': False,
            'prompt_cache_options': {'mode': settings['prompt_cache']['mode'], 'ttl': settings['prompt_cache']['ttl']},
            'input': [
                {'role': 'developer', 'content': [{'type': 'input_text', 'text': reference['instructions'],
                                                   'prompt_cache_breakpoint': {'mode': 'explicit'}}]},
                {'role': 'user', 'content': [{'type': 'input_text', 'text': reference['prompt']}]}],
            'text': {'format': {'type': 'json_schema', 'name': SCHEMA_NAME,
                                'schema': reference['wire_schema'], 'strict': True}}}


def fable_params(reference, settings):
    """Message Batches params: the synchronous fable_request minus stream, with the registered TTL."""
    request = refs.fable_request(reference, settings['max_tokens'])
    request.pop('stream')
    for block in request['system'] + request['messages'][0]['content']:
        if 'cache_control' in block:
            block['cache_control'] = {'type': 'ephemeral', 'ttl': settings['cache_ttl']}
    return request


def params_fingerprint(judge, reference, settings):
    """Model-visible parameters only; transport fields (batch wrapper, stream, TTL) are excluded."""
    return digest({'judge': judge, 'model': MODELS[judge], 'effort': settings['effort'],
                   'output_cap': settings['max_tokens'], 'instructions': reference['instructions'],
                   'prompt': reference['prompt'], 'wire_schema': reference['wire_schema']})


def _input_bound(request):
    # One token per serialized UTF-8 byte plus 4096 framing tokens: a bound, not an estimate.
    return len(canonical(request)) + 4096


def astra_worst_case(body, tier):
    bound = _input_bound(body)
    prices = (ASTRA_LONG_PRICES if bound > ASTRA_SHORT_CONTEXT_TOKENS else ASTRA_PRICES)[tier]
    rate = max(prices['input'], prices['write_30m'], prices['write_12h'] if body['prompt_cache_options']['ttl'] == '12h' else 0)
    return _money_up((bound * rate + body['max_output_tokens'] * prices['output']) / 1_000_000)


def fable_batch_worst_case(params):
    bound = _input_bound(params)
    rate = max(FABLE_BATCH_PRICES['input'], FABLE_BATCH_PRICES['cache_write_1h'], FABLE_BATCH_PRICES['cache_write_5m'])
    return _money_up((bound * rate + params['max_tokens'] * FABLE_BATCH_PRICES['output']) / 1_000_000)


def astra_cost(usage, tier):
    def count(value):
        if type(value) is not int or value < 0:
            raise ValueError('Invalid Astra usage')
        return value
    total = count(usage.get('input_tokens'))
    details = usage.get('input_tokens_details') or {}
    cached = count(details.get('cached_tokens', 0))
    writes = count(details.get('cache_write_tokens', 0))
    split = details.get('cache_write_tokens_details')
    if split is None:
        w30, w12 = 0, writes  # unknown split: price every write at the dearer lifetime
    else:
        w30, w12 = count(split.get('ttl_30m_tokens', 0)), count(split.get('ttl_12h_tokens', 0))
        if w30 + w12 != writes:
            raise ValueError('Astra cache-write split does not sum')
    ordinary = total - cached - writes
    if ordinary < 0:
        raise ValueError('Astra cached and written tokens exceed input')
    output = count(usage.get('output_tokens'))
    prices = (ASTRA_LONG_PRICES if total > ASTRA_SHORT_CONTEXT_TOKENS else ASTRA_PRICES)[tier]
    return (ordinary * prices['input'] + cached * prices['cached'] + w30 * prices['write_30m']
            + w12 * prices['write_12h'] + output * prices['output']) / 1_000_000


def fable_batch_cost(usage):
    return usage_cost(usage) / 2


# ----------------------------------------------------------------------------- outcomes

def _openai_text(body):
    texts, unexpected, refusal = [], False, None
    for entry in body.get('output') or []:
        kind = entry.get('type')
        if kind == 'reasoning':
            continue
        if kind != 'message':
            unexpected = True
            continue
        for part in entry.get('content') or []:
            if part.get('type') == 'output_text':
                texts.append(part.get('text', ''))
            elif part.get('type') == 'refusal':
                refusal = part.get('refusal', '')
            else:
                unexpected = True
    return ''.join(texts), unexpected, refusal


def openai_body_outcome(body):
    """Map one Responses object to the shared outcome shape (no ledger action)."""
    text, unexpected, refusal = _openai_text(body)
    out = {'text': text, 'model': body.get('model'), 'usage': body.get('usage'),
           'response_status': body.get('status'), 'response_id': body.get('id'), 'counts_as_attempt': True}
    status = body.get('status')
    if unexpected:
        return {**out, 'status': 'unexpected_events', 'retryable': False}
    if refusal is not None:
        return {**out, 'status': 'refusal', 'retryable': False}
    if status == 'completed':
        return {**out, 'status': 'completed', 'retryable': True}
    if status == 'incomplete':
        reason = (body.get('incomplete_details') or {}).get('reason')
        return {**out, 'status': 'truncated' if reason == 'max_output_tokens' else 'incomplete_' + str(reason),
                'retryable': reason == 'max_output_tokens'}
    if status == 'failed':
        code = (body.get('error') or {}).get('code')
        return {**out, 'status': 'response_failed', 'error_code': code,
                'retryable': code in {'server_error', 'rate_limit_exceeded'}}
    return {**out, 'status': 'invalid_response_status', 'retryable': False}


def anthropic_message_outcome(message):
    texts, unexpected = [], False
    for block in message.get('content') or []:
        if block.get('type') == 'text':
            texts.append(block.get('text', ''))
        elif block.get('type') not in {'thinking', 'redacted_thinking'}:
            unexpected = True
    out = {'text': ''.join(texts), 'model': message.get('model'), 'usage': message.get('usage'),
           'stop_reason': message.get('stop_reason'), 'response_id': message.get('id'), 'counts_as_attempt': True}
    stop = message.get('stop_reason')
    if unexpected:
        return {**out, 'status': 'unexpected_events', 'retryable': False}
    if stop == 'end_turn':
        return {**out, 'status': 'completed', 'retryable': True}
    if stop == 'max_tokens':
        return {**out, 'status': 'truncated', 'retryable': True}
    if stop == 'refusal':
        return {**out, 'status': 'refusal', 'retryable': False}
    return {**out, 'status': 'invalid_stop', 'retryable': False}


# ----------------------------------------------------------------------------- adaptive cap

class AdaptiveCap:
    """In-process admission cap for synchronous calls: halve on HTTP 429 or a 5xx burst,
    restore gradually after a clean interval (same constants as adaptive_transport)."""

    def __init__(self, maximum, clock=time.monotonic, policy=ADAPTIVE_POLICY):
        self.policy, self.clock, self.maximum = policy, clock, maximum
        self.cap, self.last_change, self.last_bad = maximum, clock(), None
        self.errors, self.clean, self.transitions = [], 0, []
        self.lock = threading.Lock()

    def allowed(self):
        with self.lock:
            now = self.clock()
            p = self.policy
            if (self.cap < self.maximum and now - self.last_change >= p['clean_seconds']
                    and (self.last_bad is None or now - self.last_bad >= p['clean_seconds'])
                    and self.clean >= p['clean_successes']):
                before, self.cap = self.cap, min(self.maximum, self.cap + p['restore_step'])
                self.last_change, self.clean = now, 0
                self.transitions.append({'from': before, 'to': self.cap, 'reason': 'clean_interval'})
            return self.cap

    def observe(self, status):
        with self.lock:
            now, p = self.clock(), self.policy
            server = status is not None and 500 <= status < 600
            self.errors = [at for at in self.errors if now - at <= p['window_seconds']] + ([now] if server else [])
            reason = 'HTTP429' if status == 429 else 'HTTP5xx_burst' if server and len(self.errors) >= p['server_error_burst'] else None
            if status == 429 or server:
                self.last_bad, self.clean = now, 0
            elif status is not None and 200 <= status < 300:
                self.clean += 1
            if reason and (not self.transitions or now - self.last_change >= p['halve_cooldown_seconds']):
                before, self.cap = self.cap, max(p['minimum'], self.cap // 2)
                if before != self.cap:
                    self.last_change, self.clean = now, 0
                    self.transitions.append({'from': before, 'to': self.cap, 'reason': reason})


# ----------------------------------------------------------------------------- runner

class TwoJudgeRun:
    """Resumable per-judge run. All state lives under plan.output_dir/items/<id>/dispatch-<n>/."""

    def __init__(self, plan, *, ledger=None, client=None, key_path=None, clock=None, sleeper=time.sleep,
                 monotonic=time.monotonic, sync_maximum=64, tick_seconds=2.0):
        from . import two_judge_plan as amendment08
        if plan.binding.get('loader') == amendment08.LOADER:
            self._revalidate, self._release = amendment08.revalidate, amendment08.release_guard
            config = amendment08.revalidate(plan)
            ledger = ledger or amendment08.ledger(plan)
            vendor = 'openai' if plan.judge == 'astra' else 'anthropic'
            if getattr(ledger, 'vendor', None) != vendor or getattr(ledger, 'phase', None) != 'final_reference':
                raise ExperimentBlocked('Two-judge ledger must be the judge vendor final_reference phase')
        else:
            self._revalidate, self._release = refs.revalidate, refs.oracle_release_guard
            refs.revalidate(plan)
            config = refs.effective_reference_config(plan.config_path)
            ledger = refs._ledger(plan, ledger)
        if not is_two_judge(config) or plan.settings.get('transport') != TRANSPORTS[plan.judge]:
            raise ExperimentBlocked('The provider batch runner requires the signed two-judge policy')
        self.plan, self.config, self.settings = plan, config, plan.settings
        self.judge = plan.judge
        self.ledger = ledger
        self.client = client
        self.key_path = Path(key_path) if key_path else (OPENAI_KEY if self.judge == 'astra' else ANTHROPIC_KEY)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleeper, self.tick = sleeper, tick_seconds
        self.monotonic = monotonic
        self.cap = AdaptiveCap(sync_maximum, clock=monotonic)
        self.root = Path(plan.output_dir)
        self.items = {row['item_id']: row for row in plan.items}
        self.lock = threading.RLock()
        self.fatal = None
        self.last_poll = {}

    # -- helpers ---------------------------------------------------------------
    def _cutoff(self):
        value = cutoff(self.settings)
        if value is None:
            raise ExperimentBlocked('No registered cutoff: Antonio sets references.cutoff_at before any run')
        return value

    def _after_cutoff(self):
        return self.clock() >= self._cutoff()

    def _key(self):
        if self.judge == 'fable':
            from .judges import require_active_judge
            require_active_judge('fable', self.config)
            return _read_env_key(self.key_path, 'ANTHROPIC_API_KEY')
        return _read_env_key(self.key_path, 'OPENAI_API_KEY')

    def _http(self):
        if self.client is None:
            self.client = httpx.Client(timeout=httpx.Timeout(120, connect=30), follow_redirects=False)
        return self.client

    def _headers(self, key, json_body=True):
        if self.judge == 'astra':
            headers = {'Authorization': 'Bearer ' + key}
        else:
            headers = {'x-api-key': key, 'anthropic-version': self.settings['anthropic_version']}
        if json_body:
            headers['content-type'] = 'application/json'
        return headers

    def reference(self, item_id):
        item = self.items[item_id]
        if item['task'] not in self.settings['tasks']:
            raise ExperimentBlocked('This run accepts pair items only')
        return refs.prepare_reference(item, binding=self.settings['pair_binding'])

    def identity(self, item_id, reference):
        plan = self.plan
        return {'stage': plan.stage, 'judge': self.judge, 'requested_model': MODELS[self.judge], 'effort': plan.effort,
                'reference_revision': deepcopy(plan.binding['reference_revision']), 'item_id': item_id,
                'task': self.items[item_id]['task'], 'package_sha256': plan.package_sha256, 'binding': plan.binding,
                'item_sha256': reference['raw_public_item_sha256'],
                'displayed_public_item_sha256': reference['displayed_public_item_sha256'],
                'prompt_sha256': reference['model_payload_sha256'], 'instructions_sha256': reference['instructions_sha256'],
                'instrument_sha256': reference.get('instrument_sha256'),
                'pair_instrument_sha256': reference.get('pair_instrument_sha256'),
                'canonical_schema_sha256': reference['canonical_schema_sha256'],
                'wire_schema_sha256': reference['wire_schema_sha256'],
                'pair_binding': self.settings['pair_binding'], 'pair_wire_version': reference.get('pair_wire_version'),
                'params_fingerprint': params_fingerprint(self.judge, reference, self.settings),
                'max_attempts': 2, 'output_cap': self.settings['max_tokens'],
                'transport_policy': 'per_item_batch_then_full_price_after_cutoff; pilot full price',
                'classifier_outputs_in_prompt': False, 'peer_outputs_in_prompt': False, 'human_answers_in_prompt': False}

    def item_dir(self, item_id):
        return self.root / 'items' / item_id

    def dispatches(self, item_id):
        rows = []
        for path in sorted(self.item_dir(item_id).glob('dispatch-*'), key=lambda p: int(p.name.split('-')[1])):
            record = json.loads((path / 'dispatch.json').read_text()) if (path / 'dispatch.json').exists() else None
            outcome = json.loads((path / 'metadata.json').read_text()) if (path / 'metadata.json').exists() else None
            rows.append({'path': path, 'dispatch': record, 'outcome': outcome})
        return rows

    def state(self, item_id):
        rows = self.dispatches(item_id)
        final = self.item_dir(item_id) / 'result.json'
        counted = [row['outcome'] for row in rows if row['outcome'] and row['outcome'].get('counts_as_attempt')]
        open_rows = [row for row in rows if row['outcome'] is None]
        return {'final': final.exists(), 'rows': rows, 'counted': counted, 'open': open_rows}

    def in_backoff(self, item_id, current=None):
        current = current or self.state(item_id)
        if current['final'] or current['open']:
            return False
        outcomes = [row['outcome'] for row in current['rows'] if row['outcome']]
        return bool(outcomes and outcomes[-1].get('pre_generation') and outcomes[-1].get('retry_not_before')
                    and self.clock() < datetime.fromisoformat(outcomes[-1]['retry_not_before']))

    def eligible(self, item_id):
        current = self.state(item_id)
        if current['final'] or current['open'] or self.in_backoff(item_id, current):
            return None  # backoff after a rejection before generation
        counted = current['counted']
        if len(counted) >= 2 or (counted and not counted[-1].get('retryable')):
            return None
        return len(counted) + 1

    # -- final records -----------------------------------------------------------
    def _backoff(self, path):
        """Consecutive rejections before generation for this item, including this one."""
        rows = self.dispatches(path.parent.name)
        streak = 1
        for row in reversed([r for r in rows if r['outcome'] and r['path'] != path]):
            if not row['outcome'].get('pre_generation'):
                break
            streak += 1
        delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (streak - 1))
        return streak, (self.clock() + timedelta(seconds=delay)).isoformat()

    def _write_outcome(self, path, identity, attempt, value):
        if value.get('pre_generation'):
            value = {**value, 'counts_as_attempt': False, 'retryable': True}
            value['pre_generation_streak'], value['retry_not_before'] = self._backoff(path)
        raw = path / 'raw_output.txt'
        raw.write_text(value.get('text') or '')
        value = {**value, 'identity': identity, 'raw_output_sha256': file_sha(raw), 'ended_at': utcnow(),
                 'attempt': attempt if value.get('counts_as_attempt') else None}
        value.pop('text', None)
        write_json(path / 'metadata.json', value)
        return value

    def _finish_item(self, item_id, phase, *, missing_reason=None):
        current = self.state(item_id)
        if current['final']:
            return json.loads((self.item_dir(item_id) / 'result.json').read_text())
        reference = self.reference(item_id)
        identity = self.identity(item_id, reference)
        counted = current['counted']
        last = counted[-1] if counted else None
        status = 'valid' if last and last['status'] == 'valid' else (missing_reason or (last['status'] if last else 'not_run'))
        costs = [row['outcome'].get('cost_usd') for row in current['rows'] if row['outcome']]
        result = {**identity, 'status': status, 'schema_valid': bool(last and last.get('schema_valid')),
                  'answer': last.get('answer') if last else None, 'raw_response': last.get('raw_response') if last else None,
                  'attempt_count': len(counted), 'attempt_statuses': [row['status'] for row in counted],
                  'dispatches': [{'dispatch': row['dispatch']['dispatch'], 'route': row['dispatch']['route'],
                                  'phase': row['dispatch']['phase'], 'status': row['outcome']['status'],
                                  'counts_as_attempt': row['outcome'].get('counts_as_attempt'),
                                  'attempt': row['outcome'].get('attempt'), 'batch_id': row['dispatch'].get('batch_id')}
                                 for row in current['rows'] if row['outcome']],
                  'finalized_in_phase': phase, 'missing_reason': missing_reason,
                  'cost_usd': sum(costs) if all(cost is not None for cost in costs) else None,
                  'annotator_type': 'LLM', 'reference_role': 'final_two_judge_oracle', 'finalized_at': utcnow()}
        write_json(self.item_dir(item_id) / 'result.json', result)
        return result

    def _after_outcome(self, item_id, phase, value):
        if value.get('status') == 'valid':
            return self._finish_item(item_id, phase)
        counted = self.state(item_id)['counted']
        if counted and (len(counted) >= 2 or not counted[-1].get('retryable')):
            return self._finish_item(item_id, phase)
        return None

    def _decode(self, value, reference):
        if value.get('status') == 'completed' and value.get('model') != MODELS[self.judge]:
            value = {**value, 'status': 'model_mismatch', 'retryable': False}
        decoded = refs._decode(value, reference)
        decoded.setdefault('schema_valid', False)
        return decoded

    # -- dispatch creation -------------------------------------------------------
    def _new_dispatch(self, item_id, phase, route, attempt, request, reservation, reference, release):
        directory = self.item_dir(item_id)
        directory.mkdir(parents=True, exist_ok=True)
        number = len(list(directory.glob('dispatch-*'))) + 1
        path = directory / f'dispatch-{number}'
        path.mkdir()
        if not (directory / 'item.json').exists():
            write_json(directory / 'item.json', reference['item'])
            (directory / 'prompt.json').write_text(reference['prompt'])
            (directory / 'instructions.txt').write_text(reference['instructions'])
            write_json(directory / 'wire_schema.json', reference['wire_schema'])
        custom = f'{item_id}-d{number}'
        if not CUSTOM_ID.match(custom):
            raise ExperimentBlocked('Item identifier cannot form a provider custom_id')
        record = {'identity': self.identity(item_id, reference), 'dispatch': number, 'phase': phase,
                  'route': route, 'candidate_attempt': attempt, 'custom_id': custom,
                  'request_sha256': digest(request), 'reservation_id': reservation,
                  'oracle_release': release, 'started_at': utcnow()}
        write_json(path / 'request.json', request)
        write_json(path / 'dispatch.json', record)
        return path, record

    def _reserve(self, amount, request, item_id, route):
        return self.ledger.reserve(amount, digest(request), MODELS[self.judge],
                                   metadata={'stage': self.plan.stage, 'item_id': item_id, 'route': route})

    # -- batch route ---------------------------------------------------------------
    def submit_batch(self, item_id, phase, attempt, release):
        reference = self.reference(item_id)
        if self.judge == 'astra':
            body = astra_body(reference, self.settings)
            worst = astra_worst_case(body, 'batch')
        else:
            body = fable_params(reference, self.settings)
            worst = fable_batch_worst_case(body)
        reservation = self._reserve(worst, body, item_id, 'batch')
        path, record = self._new_dispatch(item_id, phase, 'batch', attempt, body, reservation, reference, release)
        key = self._key()
        try:
            client = self._http()
            if self.judge == 'astra':
                line = {'custom_id': record['custom_id'], 'method': 'POST', 'url': '/v1/responses', 'body': body}
                payload = (json.dumps(line, ensure_ascii=False) + '\n').encode()
                (path / 'batch_input.jsonl').write_bytes(payload)
                upload = client.post(OPENAI_BASE + '/files', headers=self._headers(key, json_body=False),
                                     data={'purpose': 'batch'}, files={'file': ('batch_input.jsonl', payload, 'application/jsonl')})
                if upload.status_code != 200:
                    return self._submission_rejected(item_id, phase, path, record, upload, key, 'upload')
                file_id = upload.json()['id']
                write_json(path / 'upload.json', {'input_file_id': file_id})
                created = client.post(OPENAI_BASE + '/batches', headers=self._headers(key),
                                      json={'input_file_id': file_id, 'endpoint': '/v1/responses', 'completion_window': '24h',
                                            'metadata': {'custom_id': record['custom_id']}})
            else:
                created = client.post(ANTHROPIC_BASE + '/messages/batches', headers=self._headers(key),
                                      json={'requests': [{'custom_id': record['custom_id'], 'params': body}]})
            if created.status_code != 200:
                return self._submission_rejected(item_id, phase, path, record, created, key, 'create')
            batch = created.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            write_json(path / 'submission_uncertain.json', {'error_type': type(exc).__name__, 'at': utcnow(),
                       'reason': 'Batch creation outcome unknown: reconcile before any resubmission'})
            raise ExperimentBlocked('Batch creation outcome unknown for ' + item_id) from None
        finally:
            key = None
        write_json(path / 'batch.json', {'batch_id': batch['id'], 'created_at': utcnow(), 'object': _sanitize_batch(batch)})
        record['batch_id'] = batch['id']
        write_json(path / 'dispatch.json', record)
        return record

    def _submission_rejected(self, item_id, phase, path, record, response, key, step):
        detail = _safe_http_error(response, key)
        status = response.status_code
        self.cap.observe(status)
        self.ledger.release(record['reservation_id'], f'Batch {step} rejected with HTTP{status}; no batch exists')
        value = {'status': 'batch_' + step + '_rejected', 'http_status': status, **detail,
                 'counts_as_attempt': False, 'retryable': pre_generation_status(status), 'cost_usd': 0,
                 'pre_generation': pre_generation_status(status)}
        self._write_outcome(path, record['identity'], None, value)
        if not value['retryable']:
            self.fatal = f'Batch {step} rejected with HTTP{status} ({detail.get("error_type")}); configuration review needed'

    def poll_batch(self, item_id, phase):
        rows = self.state(item_id)['open']
        if not rows or rows[0]['dispatch']['route'] != 'batch' or 'batch_id' not in rows[0]['dispatch']:
            return None
        path, record = rows[0]['path'], rows[0]['dispatch']
        key = self._key()
        try:
            client = self._http()
            base = (OPENAI_BASE + '/batches/' if self.judge == 'astra' else ANTHROPIC_BASE + '/messages/batches/') + record['batch_id']
            response = client.get(base, headers=self._headers(key, json_body=False))
            if response.status_code != 200:
                self.cap.observe(response.status_code)
                return None
            batch = response.json()
            write_json(path / 'status.json', {'at': utcnow(), 'object': _sanitize_batch(batch)})
            terminal = (batch.get('status') in OPENAI_TERMINAL if self.judge == 'astra'
                        else batch.get('processing_status') == 'ended')
            if not terminal:
                if self._after_cutoff() and not (path / 'cancel.json').exists():
                    cancel = client.post(base + '/cancel', headers=self._headers(key, json_body=False))
                    write_json(path / 'cancel.json', {'requested_at': utcnow(), 'http_status': cancel.status_code,
                                                      'reason': 'registered cutoff reached'})
                return None
            return self._collect(item_id, phase, path, record, batch, client, key)
        finally:
            key = None

    def _collect(self, item_id, phase, path, record, batch, client, key):
        reference = self.reference(item_id)
        identity = record['identity']
        reservation = record['reservation_id']
        cancelled = (path / 'cancel.json').exists()
        if self.judge == 'astra':
            line = None
            if batch.get('status') == 'failed':
                # Batch-level validation failure: no request ran; retried with backoff, not an attempt.
                self.ledger.release(reservation, 'OpenAI batch failed validation; no request ran')
                value = {'status': 'batch_validation_failed', 'errors': _sanitize_batch(batch).get('errors'),
                         'counts_as_attempt': False, 'retryable': True, 'cost_usd': 0, 'pre_generation': True}
                return self._record(item_id, phase, path, identity, record, value, reference)
            for field, name in (('output_file_id', 'output.jsonl'), ('error_file_id', 'errors.jsonl')):
                if batch.get(field):
                    content = client.get(OPENAI_BASE + '/files/' + batch[field] + '/content',
                                         headers=self._headers(key, json_body=False))
                    if content.status_code != 200:
                        return None
                    (path / name).write_bytes(content.content)
                    for raw in content.text.splitlines():
                        if raw.strip():
                            row = json.loads(raw)
                            if row.get('custom_id') == record['custom_id']:
                                line = row
            value = self._openai_line_outcome(line, batch, reservation, cancelled)
        else:
            url = batch.get('results_url') or ANTHROPIC_BASE + '/messages/batches/' + record['batch_id'] + '/results'
            if not url.startswith(ANTHROPIC_BASE + '/'):
                raise ExperimentBlocked('Unexpected Anthropic results URL host')
            content = client.get(url, headers=self._headers(key, json_body=False))
            if content.status_code != 200:
                return None
            (path / 'results.jsonl').write_bytes(content.content)
            line = None
            for raw in content.text.splitlines():
                if raw.strip():
                    row = json.loads(raw)
                    if row.get('custom_id') == record['custom_id']:
                        line = row
            value = self._anthropic_line_outcome(line, reservation, cancelled)
        if value.get('status') == 'completed':
            value = self._decode(value, reference)
        return self._record(item_id, phase, path, identity, record, value, reference)

    def _openai_line_outcome(self, line, batch, reservation, cancelled):
        if line is None:
            if batch.get('status') in {'cancelled', 'expired'}:
                # Batch guide: unfinished requests are cancelled; only completed requests are charged.
                self.ledger.release(reservation, 'Request not executed before batch ' + batch['status'])
                return {'status': 'not_processed_' + batch['status'], 'counts_as_attempt': False,
                        'retryable': True, 'cost_usd': 0, 'cutoff_cancel': cancelled}
            self.ledger.uncertain(reservation, 'Completed OpenAI batch lacks this request line')
            return {'status': 'missing_from_results', 'counts_as_attempt': False, 'retryable': False, 'cost_usd': None}
        error = line.get('error')
        response = line.get('response')
        if error and error.get('code') == 'batch_expired':
            self.ledger.release(reservation, 'batch_expired: not executed, not charged')
            return {'status': 'not_processed_expired', 'counts_as_attempt': False, 'retryable': True, 'cost_usd': 0}
        if not response or response.get('status_code') != 200 or not isinstance(response.get('body'), dict):
            code = response.get('status_code') if response else None
            self.ledger.uncertain(reservation, f'OpenAI batch request error HTTP{code}; billing undocumented')
            before = pre_generation_status(code)
            return {'status': 'request_error', 'http_status': code,
                    'error_code': (error or {}).get('code') if isinstance(error, dict) else None,
                    'counts_as_attempt': not before, 'retryable': before, 'pre_generation': before, 'cost_usd': None}
        value = openai_body_outcome(response['body'])
        return self._settle_openai(value, reservation, 'batch')

    def _settle_openai(self, value, reservation, tier):
        try:
            cost = astra_cost(value.get('usage') or {}, tier)
        except ValueError:
            self.ledger.uncertain(reservation, 'Astra response lacks valid usage')
            return {**value, 'cost_usd': None}
        self.ledger.settle(reservation, cost, usage=value.get('usage'),
                           metadata={'response_id': value.get('response_id'), 'tier': tier})
        return {**value, 'cost_usd': cost}

    def _anthropic_line_outcome(self, line, reservation, cancelled):
        if line is None:
            self.ledger.uncertain(reservation, 'Ended Message Batch lacks this request result')
            return {'status': 'missing_from_results', 'counts_as_attempt': False, 'retryable': False, 'cost_usd': None}
        result = line.get('result') or {}
        kind = result.get('type')
        if kind in {'canceled', 'expired'}:
            self.ledger.release(reservation, f'Message Batch result {kind}: not sent to the model, not billed')
            return {'status': 'not_processed_' + kind, 'counts_as_attempt': False, 'retryable': True,
                    'cost_usd': 0, 'cutoff_cancel': cancelled}
        if kind == 'errored':
            self.ledger.release(reservation, 'Message Batch result errored: not billed')
            error = ((result.get('error') or {}).get('error') or {})
            before = error.get('type') in PRE_GENERATION_ERROR_TYPES
            return {'status': 'errored', 'error_type': error.get('type'), 'counts_as_attempt': not before,
                    'retryable': before, 'pre_generation': before, 'cost_usd': 0}
        if kind != 'succeeded':
            self.ledger.uncertain(reservation, 'Unknown Message Batch result type')
            return {'status': 'unknown_result_type', 'counts_as_attempt': False, 'retryable': False, 'cost_usd': None}
        value = anthropic_message_outcome(result.get('message') or {})
        try:
            cost = fable_batch_cost(value.get('usage') or {})
        except ValueError:
            self.ledger.uncertain(reservation, 'Fable batch result lacks valid usage')
            return {**value, 'cost_usd': None}
        self.ledger.settle(reservation, cost, usage=value.get('usage'),
                           metadata={'message_id': value.get('response_id'), 'tier': 'batch'})
        return {**value, 'cost_usd': cost}

    def _record(self, item_id, phase, path, identity, record, value, reference):
        attempt = record['candidate_attempt']
        value = self._write_outcome(path, identity, attempt, {**value, 'route': record['route'],
                                                              'batch_id': record.get('batch_id')})
        self._after_outcome(item_id, phase, value)
        return value

    # -- synchronous route -------------------------------------------------------
    def sync_dispatch(self, item_id, phase, attempt, release):
        reference = self.reference(item_id)
        if self.judge == 'astra':
            body = {**astra_body(reference, self.settings), 'stream': True}
            worst = astra_worst_case(body, 'standard')
            reservation = self._reserve(worst, body, item_id, 'sync')
            path, record = self._new_dispatch(item_id, phase, 'sync', attempt, body, reservation, reference, release)
            value = self._astra_stream(path, body, reservation)
        else:
            request = refs.fable_request(reference, self.settings['max_tokens'])
            reservation = self._reserve(worst_case_cost(request), request, item_id, 'sync')
            path, record = self._new_dispatch(item_id, phase, 'sync', attempt, request, reservation, reference, release)
            value = refs._fable_once(self.plan, reference, path, self.ledger, client=self._http(), key_path=self.key_path,
                                     reservation=reservation, policy_config=self.config)
            if value.get('status') == 'transport_incomplete' and not value.get('text') and not value.get('usage'):
                value = {**value, 'pre_generation': True}  # connection error before any output
            value = self._sync_http_accounting(value)
        if value.get('status') == 'http_error' and not value.get('pre_generation') and value.get('counts_as_attempt') is False:
            self.fatal = f'Synchronous request rejected with HTTP{value.get("http_status")}; configuration review needed'
        self.cap.observe(value.get('http_status', 200 if value.get('status') == 'completed' else None))
        if value.get('status') == 'completed':
            value = self._decode(value, reference)
        return self._record(item_id, phase, path, record['identity'], record, value, reference)

    @staticmethod
    def _sync_http_accounting(value):
        """429, 529 and 5xx before any output are retried with backoff and are not attempts;
        other 4xx rejections are configuration errors that stop admissions."""
        status = value.get('http_status')
        if value.get('pre_generation'):
            return {**value, 'counts_as_attempt': False, 'retryable': True}
        if value.get('status') not in {'http_error', 'rate_limited'} or status is None:
            return {**value, 'counts_as_attempt': True}
        if pre_generation_status(status):
            return {**value, 'counts_as_attempt': False, 'retryable': True, 'pre_generation': True}
        return {**value, 'counts_as_attempt': False, 'retryable': False}

    def _astra_stream(self, path, body, reservation):
        key = self._key()
        started = time.monotonic()
        events, final, error = [], None, None
        try:
            with self._http().stream('POST', OPENAI_BASE + '/responses', json=body, headers=self._headers(key),
                                     timeout=httpx.Timeout(60, connect=30)) as response:
                if response.status_code != 200:
                    detail = _safe_http_error(response, key)
                    free = response.status_code in SYNC_STATUS_FREE | PRE_GENERATION_HTTP
                    if free:
                        self.ledger.release(reservation, f'HTTP{response.status_code} rejected before generation')
                    else:
                        self.ledger.uncertain(reservation, f'HTTP{response.status_code}; final usage unknown')
                    return self._sync_http_accounting({'status': 'rate_limited' if response.status_code == 429 else 'http_error',
                            'http_status': response.status_code, **detail, 'cost_usd': 0 if free else None})
                with (path / 'events.jsonl').open('w') as stream:
                    for raw in response.iter_lines():
                        if time.monotonic() - started > self.settings['attempt_timeout_seconds']:
                            raise TimeoutError('Reference stream exceeded registered wall time')
                        if not raw.startswith('data:'):
                            continue
                        data = raw[5:].strip()
                        if not data or data == '[DONE]':
                            continue
                        event = json.loads(data)
                        events.append(event)
                        stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                        if event.get('type') in {'response.completed', 'response.incomplete', 'response.failed'}:
                            final = event.get('response')
                        elif event.get('type') == 'error':
                            error = 'stream_error'
        except (httpx.HTTPError, ValueError, OSError, TimeoutError) as exc:
            error = type(exc).__name__
            if not events:
                # Connection error before any output: not an attempt, retried with backoff.
                if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                    self.ledger.release(reservation, 'Connection failed before the request was sent')
                else:
                    self.ledger.uncertain(reservation, 'Transport error before any output; billing unknown')
                return {'status': 'transport_before_output', 'transport_error_type': error, 'pre_generation': True,
                        'counts_as_attempt': False, 'retryable': True, 'cost_usd': None, 'http_status': None}
        finally:
            key = None
        if final is None or error:
            self.ledger.uncertain(reservation, 'Incomplete Astra stream; retain worst-case liability')
            return {'status': 'transport_incomplete', 'transport_error_type': error, 'counts_as_attempt': True,
                    'retryable': False, 'cost_usd': None, 'http_status': None}
        value = openai_body_outcome(final)
        return {**self._settle_openai(value, reservation, 'standard'), 'http_status': 200}

    # -- orchestration -----------------------------------------------------------
    def phase_ids(self, phase):
        if phase == 'pilot':
            return list(self.plan.pilot_ids)
        return [item_id for item_id in self.plan.order if self.items[item_id]['task'] in self.settings['tasks']]

    def check_pilot_reuse(self):
        """A valid pilot answer is final only when its model-visible parameters equal the final ones."""
        for item_id in self.plan.pilot_ids:
            path = self.item_dir(item_id) / 'result.json'
            if path.exists():
                result = json.loads(path.read_text())
                current = params_fingerprint(self.judge, self.reference(item_id), self.settings)
                if result.get('params_fingerprint') != current:
                    raise ExperimentBlocked('Pilot parameters differ from the final configuration: ' + item_id)

    def _pollable(self, item_id):
        rows = self.state(item_id)['open']
        return bool(rows) and rows[0]['dispatch']['route'] == 'batch' and 'batch_id' in rows[0]['dispatch']

    def drive(self, phase, *, allow_inference=False, max_seconds=None):
        """Per-item fan-out: every admissible item is dispatched at once (one single-request
        batch each before the cutoff; synchronous calls for the pilot and after the cutoff),
        every open batch is polled concurrently, and each result is collected, settled and,
        when needed, retried as soon as its own batch ends."""
        if not allow_inference:
            raise ExperimentBlocked('Reference inference is disabled unless explicitly requested')
        if phase not in {'pilot', 'full'}:
            raise ValueError('Phase must be pilot or full')
        self._cutoff()
        if phase == 'full':
            self.check_pilot_reuse()
        ids = self.phase_ids(phase)
        began = time.monotonic()
        last_check = None
        futures = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
            while True:
                if last_check is None or time.monotonic() - last_check >= 30:
                    self._revalidate(self.plan)
                    last_check = time.monotonic()
                after = self._after_cutoff()
                for future in [f for f in futures if f.done()]:
                    futures.pop(future)
                    try:
                        future.result()
                    except (BudgetError, ReservationCapacityWait):
                        pass  # allocation wave: the item stays admissible and is retried after settlements
                    except Exception as exc:  # noqa: BLE001 -- stop admissions, never hide a blocked item
                        self.fatal = self.fatal or f'{type(exc).__name__}: {str(exc)[:300]}'
                busy = set(futures.values())
                for item_id in ids:
                    if item_id in busy or not self._pollable(item_id):
                        continue
                    due = self.monotonic() - self.last_poll.get(item_id, -1e9) >= self.settings['poll_interval_seconds']
                    if due:
                        self.last_poll[item_id] = self.monotonic()
                        futures[pool.submit(self.poll_batch, item_id, phase)] = item_id
                        busy.add(item_id)
                if not self.fatal:
                    sync_running = sum(1 for future in futures if getattr(future, 'route', None) == 'sync')
                    for item_id in ids:
                        if item_id in busy:
                            continue
                        attempt = self.eligible(item_id)
                        if attempt is None:
                            continue
                        route = 'sync' if phase == 'pilot' or after else 'batch'
                        if route == 'sync' and sync_running >= self.cap.allowed():
                            break
                        release = self._release(self.plan, item_id=item_id, phase=phase)
                        task = self.submit_batch if route == 'batch' else self.sync_dispatch
                        future = pool.submit(task, item_id, phase, attempt, release)
                        future.route = route
                        futures[future] = item_id
                        busy.add(item_id)
                        sync_running += route == 'sync'
                if not futures and not any(self._pollable(i) for i in ids):
                    admissible = [i for i in ids if self.eligible(i) is not None]
                    waiting = not self.fatal and any(self.in_backoff(i) for i in ids)
                    if not waiting and (self.fatal or not admissible
                                        or self._allocation_exhausted(admissible, phase, after)):
                        break
                if max_seconds is not None and time.monotonic() - began > max_seconds:
                    break
                self.sleeper(self.tick)
        return self.finalize(phase)

    def _allocation_exhausted(self, admissible, phase, after):
        """Nothing in flight and the next admissible item cannot be reserved: stop the wave loop."""
        item_id = admissible[0]
        reference = self.reference(item_id)
        route = 'sync' if phase == 'pilot' or after else 'batch'
        if self.judge == 'astra':
            body = astra_body(reference, self.settings)
            amount = astra_worst_case({**body, 'stream': True} if route == 'sync' else body,
                                      'standard' if route == 'sync' else 'batch')
        else:
            amount = (worst_case_cost(refs.fable_request(reference, self.settings['max_tokens'])) if route == 'sync'
                      else fable_batch_worst_case(fable_params(reference, self.settings)))
        try:
            token = self.ledger.reserve(amount, 'allocation-probe', MODELS[self.judge], metadata={'probe': True})
        except (BudgetError, ReservationCapacityWait):
            self.fatal = self.fatal or 'Allocation cannot admit the next item with nothing in flight'
            return True
        self.ledger.release(token, 'allocation probe only')
        return False

    def finalize(self, phase):
        ids = self.phase_ids(phase)
        after = self._after_cutoff()
        for item_id in ids:
            current = self.state(item_id)
            if current['final'] or current['open']:
                continue
            if phase == 'full' and (after or self.fatal) and self.eligible(item_id) is not None:
                reason = ('missing_allocation' if self.fatal and self.fatal.startswith('Allocation')
                          else 'missing_run_stopped' if self.fatal else 'missing_after_cutoff')
                self._finish_item(item_id, phase, missing_reason=reason)
            elif self.eligible(item_id) is None and current['counted']:
                self._finish_item(item_id, phase)
        rows = [json.loads((self.item_dir(i) / 'result.json').read_text())
                for i in ids if (self.item_dir(i) / 'result.json').exists()]
        name = 'pilot' if phase == 'pilot' else 'run'
        answers = self.root / f'two_judge_{name}_answers.jsonl'
        self.root.mkdir(parents=True, exist_ok=True)
        write_jsonl(answers, rows)
        open_ids = [i for i in ids if self.state(i)['open']]
        manifest = {'stage': self.plan.stage, 'judge_policy': 'two_judges', 'judge': self.judge,
                    'reference_design': 'astra_api_max_plus_fable_max', 'requested_model': MODELS[self.judge],
                    'effort': self.plan.effort, 'phase': phase, 'transport': self.settings['transport'],
                    'cutoff_at': self.settings['cutoff_at'], 'binding': self.plan.binding,
                    'package_sha256': self.plan.package_sha256, 'requested_item_ids': ids,
                    'finalized': len(rows), 'valid': sum(row['status'] == 'valid' for row in rows),
                    'open_item_ids': open_ids, 'fatal': self.fatal,
                    'status': 'complete' if len(rows) == len(ids) else 'partial',
                    'answers': {'path': answers.name, 'sha256': file_sha(answers)},
                    'adaptive_cap_transitions': list(self.cap.transitions),
                    'classifier_outputs_in_prompt': False, 'peer_outputs_in_prompt': False,
                    'human_answers_in_prompt': False, 'generated_at': utcnow()}
        write_json(self.root / f'two_judge_{name}_manifest.json', manifest)
        return manifest


def _sanitize_batch(batch):
    keep = ('id', 'object', 'status', 'processing_status', 'endpoint', 'completion_window', 'created_at',
            'in_progress_at', 'finalizing_at', 'completed_at', 'failed_at', 'expired_at', 'cancelling_at',
            'cancelled_at', 'expires_at', 'ended_at', 'cancel_initiated_at', 'archived_at', 'request_counts',
            'output_file_id', 'error_file_id', 'input_file_id', 'results_url', 'errors', 'model', 'type')
    return {key: batch.get(key) for key in keep if key in batch}


def allocation_for(judge, worst_case_per_pair, pairs=35, step=5):
    """35 x per-pair batch worst case, rounded up to the next multiple of ``step`` USD."""
    total = Decimal(str(worst_case_per_pair)) * pairs
    return int((total / step).to_integral_value(rounding=ROUND_CEILING) * step)
