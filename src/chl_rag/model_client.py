"""V2 OpenRouter routing: registered endpoint sets and per-provider concurrency."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path

import httpx

from .io import canonical, digest, utcnow
from .llm import (
    AccountingError,
    BudgetError,
    Client,
    OutputError,
    RequestConfigurationError,
    load_key,
)
from .spend_budget import PhaseLedger, ReservationCapacityWait


class TransportFailure(OutputError):
    """No usable HTTP response; requested route/hash are known, billing is not."""
    def __init__(self, *, requested_provider, request_hash, cause='transport_no_response'):
        super().__init__('Transport failure without usable response; unknown billing retained')
        self.requested_provider = requested_provider
        self.request_hash = request_hash
        self.failure_category = 'transport_missing_output'
        self.missing_output_cause = cause
        self.response_received = cause != 'transport_no_response'


class V2Client(Client):
    def __init__(self, phase='extraction_development', *, config='configs/routing_registry.json',
                 transport=None, credential_path=None, concurrency=128, ledger=None):
        document = json.loads(Path(config).read_text())
        self.models = document['models']
        self.routes = {route['tag']: route for model in self.models.values() for route in model['routes']}
        self.provider_slot_root = Path('data/raw/v2/provider_slots').resolve()
        self.provider_slot_root.mkdir(parents=True, exist_ok=True)
        self.provider_semaphores = {r['provider_name']: asyncio.Semaphore(16) for r in self.routes.values()}
        self.runtime_prices = {}
        self.price_revision_sha256 = None
        self.http_error_dir = Path('data/raw/v2/http_errors')
        self.secret = load_key(credential_path)
        self.ledger = ledger or PhaseLedger('openrouter', phase)
        self._cache_context = ContextVar('v2_cache_namespace', default=None)
        self.cache = Path('data/raw/v2/openrouter_cache')
        self.cache.mkdir(parents=True, exist_ok=True)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(900, connect=30),
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
            transport=transport)
        self.api_attempts = self.cache_hits = 0
        self.pause_reason = None
        self.credit_refresh_lock = asyncio.Lock()

    @property
    def cache(self):
        namespace = self._cache_context.get()
        return self._base_cache / digest(namespace) if namespace else self._base_cache

    @cache.setter
    def cache(self, value):
        self._base_cache = value

    def _try_provider_slot(self, name):
        key = digest(name.casefold().replace(' ', ''))[:20]
        for number in range(16):
            fd = os.open(self.provider_slot_root / f'{key}-{number}.lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                os.close(fd)
        return None

    @asynccontextmanager
    async def route(self, model, max_tokens, forced_provider=None):
        if self.models[model].get('execution_hold'):
            raise RequestConfigurationError('Model remains on the explicit user execution hold')
        options = [r for r in self.models[model]['routes']
                   if r['max_completion_tokens'] >= max_tokens
                   and (forced_provider is None or r['tag'] == forced_provider)]
        if not options:
            raise RequestConfigurationError('No registered fp8 endpoint supports the frozen output cap')
        while True:
            for option in options:
                semaphore = self.provider_semaphores[option['provider_name']]
                if not semaphore.locked():
                    await semaphore.acquire()
                    fd = self._try_provider_slot(option['provider_name'])
                    if fd is None:
                        semaphore.release()
                        continue
                    try:
                        yield option
                    finally:
                        os.close(fd)
                        semaphore.release()
                    return
            await asyncio.sleep(.1)

    def make_request(self, model, messages, schema, reasoning, max_tokens, route):
        price = route.get('price_ceiling', route['pricing'])
        parameters = {'enabled': False} if reasoning == 'off' else (
            {'enabled': True} if reasoning == 'on' else {'effort': reasoning})
        return {'model': model, 'messages': messages, 'temperature': 0, 'seed': 42,
                'max_tokens': max_tokens, 'reasoning': parameters,
                'provider': {'order': [route['tag']], 'only': [route['tag']],
                    'allow_fallbacks': False, 'require_parameters': True, **({'quantizations': ['fp8']} if route.get('quantization', 'fp8') == 'fp8' else {}),
                    'max_price': {'prompt': float(Decimal(price['prompt']) * 1000000),
                                  'completion': float(Decimal(price['completion']) * 1000000),
                                  'request': float(price.get('request', 0))}},
                'response_format': {'type': 'json_schema', 'json_schema': {
                    'name': 'v2_result', 'strict': True, 'schema': schema}}}

    def reserve_cost(self, request):
        route = next(r for r in self.models[request['model']]['routes']
                     if r['tag'] == request['provider']['order'][0])
        price = route.get('price_ceiling', route['pricing'])
        # Conservative uncached UTF8-byte token bound plus schema/chat framing.
        return float((Decimal(len(canonical(request)) + 4096) * Decimal(price['prompt'])
                      + Decimal(request['max_tokens']) * Decimal(price['completion'])
                      + Decimal(price.get('request', 0))) * Decimal('1.05'))

    async def generate(self, model, messages, schema, reasoning, max_tokens, *, forced_provider=None, cache_namespace=None):
        token = self._cache_context.set(cache_namespace)
        self.cache.mkdir(parents=True, exist_ok=True)
        try:
            return await self._generate(model, messages, schema, reasoning, max_tokens, forced_provider)
        finally:
            self._cache_context.reset(token)

    async def ensure_credit_snapshot(self):
        if not self.ledger.needs_credit_refresh():
            return
        async with self.credit_refresh_lock:
            if not self.ledger.needs_credit_refresh():
                return
            before = self.ledger.snapshot()['total_cost']
            response = await self.http.get('https://openrouter.ai/api/v1/credits',
                headers={'Authorization': 'Bearer ' + self.secret})
            if response.status_code != 200:
                raise AccountingError('Cannot refresh authoritative OpenRouter credit balance')
            try:
                data = response.json()['data']
                self.ledger.record_credit_snapshot(total_credits=data['total_credits'],
                    total_usage=data['total_usage'], own_cost_before_request=before, checked_at=utcnow())
            except (KeyError, TypeError, ValueError):
                raise AccountingError('Invalid authoritative credit response; no generation sent') from None

    async def _generate(self, model, messages, schema, reasoning, max_tokens, forced_provider):
        async with self.route(model, max_tokens, forced_provider) as route:
            if self.pause_reason:
                raise RequestConfigurationError('Scheduling paused after transport/accounting failure: ' + self.pause_reason)
            await self.ensure_credit_snapshot()
            request = self.make_request(model, messages, schema, reasoning, max_tokens, route)
            reservation_wait = 0.0
            while True:
                try:
                    response, provenance = await self.raw(request)
                    break
                except ReservationCapacityWait:
                    await asyncio.sleep(.25)
                    reservation_wait += .25
                    await self.ensure_credit_snapshot()
                except (AccountingError, BudgetError, RequestConfigurationError) as exc:
                    missing_response_causes = {
                        'Transport failure: reconcile billing before retry': 'transport_no_response',
                        'Non-JSON response': 'non_json_response',
                        'Expected a JSON object response': 'non_object_response'}
                    if isinstance(exc, AccountingError) and str(exc) in missing_response_causes:
                        raise TransportFailure(requested_provider=route['tag'], request_hash=digest(request),
                                               cause=missing_response_causes[str(exc)]) from None
                    self.pause_reason = type(exc).__name__
                    raise
            if response.get('provider') != route['provider_name']:
                raise RequestConfigurationError('Returned provider differs from explicitly pinned endpoint')
            return response, {**provenance, 'reservation_wait_seconds': reservation_wait, 'requested_provider': route['tag'],
                              'provider': response.get('provider'), 'quantization': route.get('quantization', 'fp8'),
                              'canonical_slug': self.models[model]['canonical_slug']}
