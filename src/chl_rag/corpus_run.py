"""Addendum 08A: the corpus of "the system" (v2.1 extraction) on the complete F.

The frozen v2 production code runs unchanged except for what the addendum registers:
- the extraction request receives the v2.1 prompt addition and the wire schema with
  result.assertion last, byte-identical to the 14 November re-extraction of the
  human-pair abstracts (scripts/system/extraction_correction_test.py, arm order_prompt3);
- outputs go to new directories (data/derived/production, results/production);
- routes are every eligible fp8 endpoint of the 16 November public catalogue snapshot;
- the adaptive per-provider cap starts at 128 (Antonio), with its own controller state;
- rejections before generation (429, 5xx, provider refusal, connection failure before
  sending) are retried with backoff and are never counted as attempts. An invalid or
  truncated output keeps the single identical retry on the same provider.

Run from the main project root (ledger, data and caches), with CHL_V21_ROOT pointing to
the checkout that holds the signed 08A files and PYTHONPATH to its src.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import os
import random
import subprocess
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import httpx

from . import model_runs as E
from . import frozen_compat as FC
from . import production as P
from . import retrieval_prefetch as R
from .io import digest, file_sha, utcnow, write_json
from .llm import AccountingError, OutputError, RateLimitError, RequestConfigurationError
from .adaptive_transport import POLICY, AdaptiveV2Client, ProviderControl
from .extraction import WIRE_MODEL_EXTRACTION
from .extraction import extraction_request as frozen_extraction_request

CONFIG_REL = 'configs/evaluation_corpus.json'
BASE = Path('data/derived/production')
RESULTS = Path('results/production')
STATUS = RESULTS / 'status.json'
SIGNED_FILES = (CONFIG_REL, 'PROTOCOL_ADDENDUM_08A_V21_CORPUS.md', 'PROTOCOL_ADDENDUM_08A_IMPLEMENTATION_NOTES.md',
                'configs/corpus_routes.json',
                'src/chl_rag/corpus_run.py', 'src/chl_rag/v21_uncapped.py', 'scripts/system/run_corpus.py',
                'scripts/system/snapshot_routes.py')
_route_holder = contextvars.ContextVar('v21_route_holder', default=None)
STATE = {}


def root():
    return Path(os.environ['CHL_V21_ROOT']).resolve()


V21_CODE_ROOT = root


def _git(*args):
    return subprocess.run(['git', '-C', str(root()), *args], capture_output=True, text=True, check=False)


def verify_registration():
    """The 08A files must be committed, unmodified and signed (G) in the 08A checkout."""
    proofs = {}
    for relative in SIGNED_FILES:
        if _git('status', '--porcelain', '--', relative).stdout.strip():
            raise P.ExperimentBlocked('08A file has uncommitted changes: ' + relative)
        line = _git('log', '-1', '--format=%H %G?', '--', relative).stdout.split()
        if len(line) != 2 or line[1] != 'G':
            raise P.ExperimentBlocked('08A file is not in a good signed commit: ' + relative)
        proofs[relative] = {'commit': line[0], 'sha256': file_sha(root() / relative)}
    return proofs


def reordered_schema():
    schema = deepcopy(WIRE_MODEL_EXTRACTION)
    props = schema['properties']['claims']['items']['properties']['result']['properties']
    props['assertion'] = props.pop('assertion')
    return schema


def load_config():
    config = json.loads((root() / CONFIG_REL).read_text())
    if config.get('status') != 'registered' or config.get('addendum') != 'PROTOCOL_ADDENDUM_08A_V21_CORPUS.md':
        raise P.ExperimentBlocked('08A configuration is not registered')
    ext = config['extraction_revision']
    if digest_text(ext['prompt_addition']) != ext['prompt_addition_sha256']:
        raise P.ExperimentBlocked('v2.1 prompt addition changed')
    if canonical_sha(reordered_schema()) != ext['wire_schema_sha256']:
        raise P.ExperimentBlocked('v2.1 wire schema changed')
    if file_sha(root() / config['routing_config']) != config['routing_config_sha256']:
        raise P.ExperimentBlocked('v2.1 routing configuration changed')
    if file_sha(root() / 'PROTOCOL_ADDENDUM_08A_V21_CORPUS.md') != config['addendum_sha256']:
        raise P.ExperimentBlocked('08A addendum changed')
    return config


def digest_text(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_sha(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()


def install():
    """Redirect the frozen production module to the 08A run; call once per process."""
    if STATE.get('installed'):
        return STATE
    main = Path.cwd().resolve()
    import chl_rag
    from . import sqlite_ledger as L
    if not Path(chl_rag.__file__).resolve().is_relative_to(root()):
        raise P.ExperimentBlocked('08A code must be imported from the 08A checkout')
    frozen_verify = FC.verify_frozen_input

    def verify_frozen_input(path, expected_sha, *, config_path=FC.BASE_CONFIG, root=None):
        # Tracked inputs are verified, as one reviewed set, in the 08A checkout (whose code is
        # the code that runs); a tracked configuration read at runtime from the main tree must be
        # byte-identical to the verified copy. Untracked data is verified in the main tree. Note 1.
        checkout = V21_CODE_ROOT()
        tracked = (checkout / path).exists()
        frozen_verify(path, expected_sha, config_path=config_path, root=checkout if tracked else main)
        if tracked and not str(path).startswith(('src/', 'scripts/')) and (main / path).exists():
            if file_sha(main / path) != file_sha(checkout / path):
                raise FC.FrozenCompatibilityError('Runtime copy differs from the verified copy: ' + str(path))
    FC.verify_frozen_input = verify_frozen_input
    frozen_authority = L.authority

    def authority_of_running_code(_root):
        # The reviewed accounting runtime is checked where the running code is imported from
        # (the 08A checkout); the ledger database and export stay in the main tree (note 1).
        return frozen_authority(root())
    L.authority = authority_of_running_code
    config = load_config()
    proofs = verify_registration()
    addition = config['extraction_revision']['prompt_addition']
    v21_binding = {'addendum': 'PROTOCOL_ADDENDUM_08A_V21_CORPUS.md',
                   'config_sha256': file_sha(root() / CONFIG_REL),
                   'prompt_addition_sha256': config['extraction_revision']['prompt_addition_sha256'],
                   'wire_schema_sha256': config['extraction_revision']['wire_schema_sha256'],
                   'routing_config_sha256': config['routing_config_sha256']}
    frozen_guard = P.production_guard

    def guard(config_path=P.CONFIG, *, require_classifier=False):
        base, binding = frozen_guard(config_path, require_classifier=require_classifier)
        base = deepcopy(base)
        base['routing_config'] = str(root() / config['routing_config'])
        base['_operational_decision'] = {'scope': 'full', 'authority': config['authority'],
                                         'basis': 'addendum_08A_v21_corpus', 'runtime_time_fallback_authorized': False}
        decision = STATE.get('classifier_effort')
        if decision:
            # Addendum 08C: the corpus classifier effort comes from the signed decision; caps per effort.
            classification = deepcopy(base['classification'])
            classification['reasoning'] = decision['effort']
            classification['configuration_id'] = 'glm-5.3-flash__' + decision['effort']
            classification['max_tokens'] = classification['max_tokens'] if decision['effort'] == 'low' else 131072
            base['classification'] = classification
            if '_classifier_binding' in base:
                base['_classifier_binding'] = {**base['_classifier_binding'],
                                               'fixed_configuration_sha256': digest(classification),
                                               'addendum_08C': {k: decision[k] for k in ('effort', 'decision_sha256', 'commit')}}
        return base, {**binding, 'corpus_run': v21_binding}

    def v21_request(article, **kwargs):
        request = frozen_extraction_request(article, **kwargs)
        messages = deepcopy(request['messages'])
        messages[0]['content'] += addition
        schema = reordered_schema()
        request.update(messages=messages, schema=schema, corpus_run=True,
                       identity=digest({'frozen_identity': request['identity'], **v21_binding}))
        return request

    P.BASE, P.RESULTS = BASE, RESULTS
    P.production_guard = guard
    R.production_guard = guard
    R.PROGRESS = RESULTS / 'retrieval_prefetch.json'
    E.extraction_request = v21_request
    RESULTS.mkdir(parents=True, exist_ok=True)
    BASE.mkdir(parents=True, exist_ok=True)
    STATE.update(installed=True, config=config, proofs=proofs, v21_binding=v21_binding)
    return STATE


POLICY_V21 = {**POLICY, 'maximum': 128, 'restore_step': 32, 'clean_seconds': 30}
RETRYABLE_CONNECT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class ConnectRetryHTTP:
    """A connection that never opened sent nothing: retry it with backoff, without limit.

    Outermost wrapper, so a connection failure never reaches the request accounting as an
    attempt. GET (credit reads) is retried the same way."""
    def __init__(self, client, counters):
        self.client, self.counters = client, counters

    async def _retry(self, method, *args, **kwargs):
        number = 0
        while True:
            try:
                return await getattr(self.client, method)(*args, **kwargs)
            except RETRYABLE_CONNECT:
                self.counters['connect_retry_' + method] += 1
                await asyncio.sleep(min(60, 2 ** min(number, 6)) * (0.5 + random.random()))
                number += 1

    async def post(self, *args, **kwargs):
        return await self._retry('post', *args, **kwargs)

    async def get(self, *args, **kwargs):
        return await self._retry('get', *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.client, name)


class TotalDeadlineHTTP:
    def __init__(self, client, seconds=480):
        self.client, self.seconds = client, seconds

    async def post(self, *args, **kwargs):
        try:
            return await asyncio.wait_for(self.client.post(*args, **kwargs), timeout=self.seconds)
        except TimeoutError:
            raise httpx.ReadTimeout(f'Total model POST wall deadline exceeded ({self.seconds:g}s)') from None

    def __getattr__(self, name):
        return getattr(self.client, name)


class V21Client(AdaptiveV2Client):
    """Never stops on a rejection before generation: routes cool down and are re-probed;
    a key-level limit pauses every admission and is re-probed once a minute."""
    COOLDOWN_START, COOLDOWN_MAX, KEY_PROBE_SECONDS = 60, 600, 60

    def __init__(self, phase, *, routes_path, ledger, concurrency):
        super().__init__(phase, config=routes_path, ledger=ledger, concurrency=concurrency,
                         adaptive_policy=POLICY_V21, route_health_holds=[])
        if not self.models or any(not m.get('routes') for m in self.models.values()):
            raise RequestConfigurationError('The registered route set is empty')
        slots = Path('data/raw/v21/provider_slots').resolve()
        slots.mkdir(parents=True, exist_ok=True)
        self.provider_slot_root = slots
        self.control = ProviderControl(slots, state_root='data/raw/v21/adaptive_provider_state', policy=POLICY_V21)
        self.counters = Counter()
        self.route_rejections = defaultdict(int)
        self.cooldowns = {}       # tag -> {'until': epoch, 'seconds': n, 'reason': ...}
        self.run_holds = {}       # permanent only for unsupported parameters (404)
        self.key_blocked_until = 0.0
        self.key_block_events = []
        self.http.control = self.control
        self.http = ConnectRetryHTTP(TotalDeadlineHTTP(self.http), self.counters)

    async def ensure_credit_snapshot(self):
        """The credit read is a GET with no generation: a network failure is retried, never fatal."""
        delay = 2
        while True:
            try:
                return await super().ensure_credit_snapshot()
            except (httpx.HTTPError, AccountingError) as exc:
                if isinstance(exc, AccountingError) and not str(exc).startswith(('Cannot refresh', 'Invalid authoritative credit')):
                    raise
                self.counters['credit_refresh_retry'] += 1
                await asyncio.sleep(min(60, delay) * (0.5 + random.random()))
                delay *= 2

    def _cool(self, tag, reason):
        import time
        previous = self.cooldowns.get(tag, {}).get('seconds', 0)
        seconds = min(self.COOLDOWN_MAX, max(self.COOLDOWN_START, previous * 2))
        self.cooldowns[tag] = {'until': time.time() + seconds, 'seconds': seconds, 'reason': reason, 'at': utcnow()}

    @asynccontextmanager
    async def route(self, model, max_tokens, forced_provider=None):
        import time
        if self.models[model].get('execution_hold'):
            raise RequestConfigurationError('Model remains on the explicit user execution hold')
        registered = [r for r in self.models[model]['routes'] if r['max_completion_tokens'] >= max_tokens
                      and (forced_provider is None or r['tag'] == forced_provider)]
        if not registered:
            raise RequestConfigurationError('No registered fp8 endpoint supports this request')
        while True:
            now = time.time()
            if now < self.key_blocked_until:
                await asyncio.sleep(min(5, self.key_blocked_until - now) + random.random())
                continue
            if self.key_blocked_until:
                # The first request after the pause is the probe; the others keep waiting for its result.
                self.key_blocked_until = now + self.KEY_PROBE_SECONDS
            usable = [r for r in registered if r['tag'] not in self.run_holds or r['tag'] == forced_provider]
            ready = [r for r in usable if self.cooldowns.get(r['tag'], {}).get('until', 0) <= now]
            if not usable:
                # Every route is permanently unsupported for these parameters: wait, never raise.
                self.counters['waiting_no_usable_route'] += 1
                await asyncio.sleep(30)
                continue
            if not ready:
                wake = min(self.cooldowns[r['tag']]['until'] for r in usable)
                await asyncio.sleep(max(.5, min(10, wake - now)) + random.random())
                continue
            for option in random.sample(ready, len(ready)):
                fd = self._try_provider_slot(option['provider_name'])
                if fd is None:
                    continue
                holder = _route_holder.get()
                if holder is not None:
                    holder['tag'] = option['tag']
                try:
                    yield option
                finally:
                    os.close(fd)
                return
            await asyncio.sleep(.1)

    async def _generate(self, model, messages, schema, reasoning, max_tokens, forced_provider):
        import time
        rejections = 0
        while True:
            holder = {}
            token = _route_holder.set(holder)
            try:
                result = await super()._generate(model, messages, schema, reasoning, max_tokens, forced_provider)
                tag = holder.get('tag')
                if tag:
                    self.route_rejections[tag] = 0
                    self.cooldowns.pop(tag, None)
                if self.key_blocked_until:
                    self.key_block_events.append({'cleared_at': utcnow()})
                    self.key_blocked_until = 0.0
                self.counters['accepted'] += 1
                return result
            except (RateLimitError, RequestConfigurationError, OutputError) as exc:
                kind = rejection_kind(exc)
                if kind is None:
                    raise
                tag = holder.get('tag')
                self.counters['rejection_' + kind] += 1
                if tag:
                    self.counters['rejection_' + kind + '@' + tag] += 1
                self.pause_reason = None
                rejections += 1
                if kind == 'key_limit':
                    if not self.key_blocked_until:
                        self.key_block_events.append({'blocked_at': utcnow()})
                    self.key_blocked_until = time.time() + self.KEY_PROBE_SECONDS
                elif kind == 'unsupported' and tag and tag != forced_provider:
                    self.run_holds[tag] = {'at': utcnow(), 'reason': 'HTTP 404: no endpoint supports the request parameters'}
                elif kind in ('refusal', '429', '5xx', 'unsupported') and tag:
                    self.route_rejections[tag] += 1
                    if self.route_rejections[tag] >= 5:
                        self._cool(tag, 'five consecutive rejections before generation (' + kind + ')')
                        self.route_rejections[tag] = 0
            finally:
                _route_holder.reset(token)
            await asyncio.sleep(min(60, 2 ** min(rejections, 6)) * (0.5 + random.random()))


def rejection_kind(exc):
    """Classify failures that happened before any generation; None means a real attempt outcome."""
    message = str(exc)
    status = getattr(exc, 'status_code', None)
    if 'Key limit exceeded' in message:
        return 'key_limit'
    if isinstance(exc, RateLimitError) or status == 429:
        return '429'
    if message.startswith('Scheduling paused'):
        return 'paused'
    if message.startswith('Generation failed: HTTP 5') or (status is not None and 500 <= status < 600):
        return '5xx'
    if message.startswith('OpenRouter HTTP 401') or message.startswith('OpenRouter HTTP 402'):
        return None
    if message.startswith('OpenRouter HTTP 404') and 'requested parameters' in message:
        return 'unsupported'
    if message.startswith('OpenRouter HTTP 4'):
        return 'refusal'
    return None


EFFORT_DECISION = Path('configs/corpus_classifier_effort.json')


def read_effort_decision(path=EFFORT_DECISION):
    """08C gate: a committed, good-signature decision in the main repository, or nothing."""
    if not path.exists():
        return None, 'missing'
    run = lambda *a: subprocess.run(['git', *a], capture_output=True, text=True, check=False).stdout
    if run('status', '--porcelain', '--', str(path)).strip():
        return None, 'uncommitted'
    line = run('log', '-1', '--format=%H %G?', '--', str(path)).split()
    if len(line) != 2 or line[1] != 'G':
        return None, 'not in a good signed commit'
    try:
        value = json.loads(path.read_text())
    except ValueError:
        return None, 'not JSON'
    if (value.get('effort') not in {'low', 'high', 'max'} or value.get('addendum') != '08C'
            or not isinstance(value.get('decision_sha256'), str) or len(value['decision_sha256']) != 64):
        return None, 'invalid content'
    return {**value, 'commit': line[0], 'file_sha256': file_sha(path)}, 'ok'


def reconcile_started_classifications(directory):
    """Attempts left 'started' by a stopped classification stage (no running process) are archived.

    A crash before the POST (for example a failed credit read) generated nothing. An interrupted
    POST keeps its unknown-billing liability in the ledger, and a cached response is reused on retry."""
    report = {'at': utcnow(), 'directory': str(directory), 'archived': 0}
    for path in sorted(Path(directory).glob('*/attempt-*.json')):
        if (Path(directory) / (path.parent.name + '.json')).exists():
            continue
        attempt = json.loads(path.read_text())
        if attempt.get('status') != 'started':
            continue
        history = path.parent / 'interrupted_history'
        history.mkdir(exist_ok=True)
        write_json(history / (digest(attempt) + '.json'), {**attempt, 'archived_at': utcnow(),
                   'archived_by': 'addendum 08A implementation note 4: stage stopped; attempt restarted'})
        path.unlink()
        report['archived'] += 1
    write_json(RESULTS / ('reconciliation_classification_' + report['at'].replace(':', '').replace('-', '')[:15] + '.json'), report)
    return report


PREFLIGHT_ERRORS = ('No eligible fp8 endpoint remains for this request', 'OpenRouter HTTP 403: Key limit exceeded')


def reconcile_preflight_blocks():
    """Attempts blocked by a rejection before any generation are not attempts (08A).

    Archive them exactly as the frozen _one archives budget preflight blocks, so the
    source restarts that attempt with the identical request. Anything else stays blocked."""
    base = BASE / 'extraction' / 'glm-5.3-flash__high'
    report = {'at': utcnow(), 'archived': 0, 'by_error': Counter(), 'left_blocked': 0}
    for path in sorted(base.glob('*/attempt-*.json')):
        if (base / (path.parent.name + '.json')).exists():
            continue
        attempt = json.loads(path.read_text())
        if attempt.get('status') != 'blocked':
            continue
        error = attempt.get('error') or ''
        if (attempt.get('error_type') == 'RequestConfigurationError' and 'response' not in attempt
                and any(error.startswith(p) for p in PREFLIGHT_ERRORS)):
            history = path.parent / 'preflight_history'
            history.mkdir(exist_ok=True)
            write_json(history / (digest(attempt) + '.json'), {**attempt, 'archived_at': utcnow(),
                       'archived_by': 'addendum 08A implementation note 1: rejection before generation'})
            path.unlink()
            report['archived'] += 1
            report['by_error'][error[:60]] += 1
        else:
            report['left_blocked'] += 1
    report['by_error'] = dict(report['by_error'])
    write_json(RESULTS / ('reconciliation_' + report['at'].replace(':', '').replace('-', '')[:15] + '.json'), report)
    return report


def status(**fields):
    current = {}
    if STATUS.exists():
        try:
            current = json.loads(STATUS.read_text())
        except ValueError:
            current = {}
    current.update(fields, updated_at=utcnow())
    write_json(STATUS, current)
    return current


async def fresh_balance(client):
    for number in range(10):
        try:
            response = await client.http.get('https://openrouter.ai/api/v1/credits',
                                             headers={'Authorization': 'Bearer ' + client.secret})
            if response.status_code == 200:
                break
        except httpx.HTTPError:
            pass
        await asyncio.sleep(min(60, 2 ** number))
    else:
        raise P.ExperimentBlocked('Cannot read the OpenRouter balance')
    data = response.json()['data']
    snapshot = {'checked_at': utcnow(), 'total_credits': data['total_credits'], 'total_usage': data['total_usage'],
                'balance_usd': float(Decimal(str(data['total_credits'])) - Decimal(str(data['total_usage']))),
                'source': 'OpenRouter /api/v1/credits', 'model_API_calls': 0}
    return snapshot
