"""V2-only persistent funding alerts and reservations. V1 ledger is untouched."""
from __future__ import annotations

import fcntl
import json
import math
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .io import digest, utcnow
from .llm import AccountingError, BudgetError

PHASE_CAPS = {'openrouter': {'extraction_development': 20, 'production_extraction': 45,
                             'production_classification': 40, 'retries_reserve': 15, 'metabolic_classifier_development': 30},
              'anthropic': {'development_judging': 110, 'final_reference': 0},
              # OpenAI Batch for Astra: no default allowance; spending needs a confirmed funding entry.
              'openai': {'final_reference': 0}}
VENDOR_ALERTS = {'openrouter': 85, 'anthropic': 105, 'openai': 0}
AVAILABLE_CREDIT = {'openrouter': 96, 'anthropic': 119, 'openai': 0}


class ReservationCapacityWait(BudgetError):
    """Credit covers this call, but earlier live reservations must settle first."""


def money(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError('Cost must be numeric')
    value = Decimal(str(value))
    if not value.is_finite() or value < 0:
        raise ValueError('Cost must be finite and nonnegative')
    return value


class PhaseLedger:
    def __init__(self, vendor, phase, root=None, funding_path='configs/funding.json'):
        if vendor not in PHASE_CAPS or phase not in PHASE_CAPS[vendor]:
            raise ValueError('Unregistered vendor/phase')
        self.vendor, self.phase = vendor, phase
        self.root = Path(root or Path.cwd())
        self.funding_path = funding_path
        if vendor == 'openrouter':
            from .sqlite_ledger import reject_legacy_json_writer
            reject_legacy_json_writer(self.root)
        self.path = self.root / 'results' / f'{vendor}_spend.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def funding(self):
        path = self.root / self.funding_path
        if path.exists():
            document = json.loads(path.read_text())
            if document.get('status') != 'confirmed' or not document.get('authority'):
                raise BudgetError('Funding update requires explicit coordinator confirmation')
            vendors = document.get('vendors')
            if not isinstance(vendors, dict) or self.vendor not in vendors:
                raise BudgetError('Confirmed funding has no entry for this vendor')
            return vendors[self.vendor]
        return {'available_credit': AVAILABLE_CREDIT[self.vendor],
                'alert': VENDOR_ALERTS[self.vendor], 'phase_alerts': PHASE_CAPS[self.vendor]}

    def external_liability(self):
        if self.vendor != 'openrouter':
            return Decimal(0)
        # Same logical ledger may exist in both worktrees after merging. Count
        # generation/reservation identities once, including the later screen.
        files = []
        for directory in (self.root.parent / 'project-clf/results', self.root / 'results'):
            for pattern in ('spend_classifier*.json', 'spend_mancon*.json'):
                files.extend(directory.glob(pattern))
        calls, pending, retained, settled_ids = {}, {}, {}, set()
        for path in files:
            document = json.loads(path.read_text())
            if not isinstance(document.get('calls'), list) or not isinstance(document.get('pending'), dict):
                continue
            for row in document['calls']:
                reservation = row.get('reservation_id')
                if reservation:
                    settled_ids.add(reservation)
                generation = row.get('generation_id') or row.get('response_metadata', {}).get('generation_id')
                identity = ('generation', generation) if generation else ('reservation', reservation or row['request_hash'])
                cost = money(row['cost'])
                if identity in calls and calls[identity] != cost:
                    raise AccountingError('External ledger copies disagree on a generation cost')
                calls[identity] = cost
            for token, row in document['pending'].items():
                pending[token] = max(pending.get(token, Decimal(0)), money(row['reserved']))
            for token, row in document.get('reconciled_reserves', {}).items():
                retained[token] = max(retained.get(token, Decimal(0)), money(row['reserved']))
        return (sum(calls.values(), Decimal(0))
                + sum((cost for token, cost in pending.items() if token not in settled_ids and token not in retained), Decimal(0))
                + sum(retained.values(), Decimal(0)))

    @contextmanager
    def locked(self):
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = json.loads(self.path.read_text()) if self.path.exists() else {
                'schema_version': 2, 'vendor': self.vendor, 'currency': 'USD',
                'policy': 'pause at funding alert; resume only after confirmed top-up',
                'funding': self.funding(),
                'total_cost': 0, 'calls': [], 'pending': {}, 'unknown_cost_calls': [],
                'status': 'idle'}
            if value['vendor'] != self.vendor:
                raise BudgetError('Ledger vendor mismatch')
            value['funding'] = self.funding()
            yield value
            value['updated_at'] = utcnow()
            temporary = self.path.with_suffix(f'.{os.getpid()}.{uuid.uuid4().hex}.tmp')
            temporary.write_text(json.dumps(value, indent=2) + '\n')
            temporary.replace(self.path)

    @staticmethod
    def liability(value, phase=None):
        settled = sum((money(x['cost']) for x in value['calls']
                       if phase is None or x['phase'] == phase), Decimal(0))
        pending = sum((money(x['reserved']) for x in value['pending'].values()
                       if phase is None or x['phase'] == phase), Decimal(0))
        return settled + pending

    @staticmethod
    def live_reservations(value):
        return sum((money(row['reserved']) for row in value['pending'].values()
                    if not row.get('uncertain')), Decimal(0))

    def needs_credit_refresh(self, max_age_seconds=60):
        with self.locked() as value:
            if value['funding'].get('headroom_policy') != 'real_balance_own_inflight_floor':
                return False
            snapshot = value.get('account_credit_snapshot')
            return (snapshot is None or (datetime.now(UTC) - datetime.fromisoformat(snapshot['checked_at'])).total_seconds() > max_age_seconds)

    def record_credit_snapshot(self, *, total_credits, total_usage, own_cost_before_request, checked_at):
        credits, usage, own = money(total_credits), money(total_usage), money(own_cost_before_request)
        if usage > credits:
            raise BudgetError('Account balance is exhausted')
        with self.locked() as value:
            old = value.get('account_credit_snapshot')
            if old and (money(old['total_credits']) > credits or
                        (money(old['total_credits']) == credits and money(old['total_usage']) > usage)):
                return  # Ignore an older/cached account response.
            value['account_credit_snapshot'] = {'checked_at': checked_at,
                'total_credits': float(credits), 'total_usage': float(usage),
                'real_balance': float(credits-usage), 'own_cost_before_request': float(own),
                'authority': 'INBOX21:52: external historical liability is audit-only; floor15USD'}
            value.setdefault('account_credit_snapshot_history', []).append(dict(value['account_credit_snapshot']))

    @staticmethod
    def bank_headroom(value):
        snap = value.get('account_credit_snapshot')
        if snap is None:
            raise BudgetError('Fresh read-only credit snapshot required')
        # Account balance already includes earlier charges, including the other
        # worktree. Subtract only our settled delta since snapshot acquisition
        # began and our still-live reservations. Historical unknown liabilities
        # remain in the audit; they are not subtracted a second time.
        settled_delta = max(Decimal(0), money(value['total_cost']) - money(snap['own_cost_before_request']))
        return money(snap['real_balance']) - settled_delta - PhaseLedger.live_reservations(value)

    def reserve(self, amount, request_hash, model, metadata=None):
        amount = money(amount)
        if amount <= 0:
            raise ValueError('Reservation must be positive')
        with self.locked() as value:
            if value.get('price_mismatch'):
                raise AccountingError('Actual cost exceeded reservation; review prices before spending')
            funding = value['funding']
            if (funding.get('new_calls_paused') and
                    model not in funding.get('comparison_stop_allow_models', [])):
                raise BudgetError('New calls explicitly paused pending coordinator confirmation')
            if funding.get('headroom_policy') == 'real_balance_own_inflight_floor':
                if self.bank_headroom(value) - amount < money(funding['floor_usd']):
                    live = self.live_reservations(value)
                    if live and self.bank_headroom(value) + live - amount >= money(funding['floor_usd']):
                        raise ReservationCapacityWait('Waiting for own in-flight reservations to settle')
                    raise BudgetError('Real account headroom after own live reservations would fall below15USD')
            else:
                if self.liability(value) + self.external_liability() + amount > money(funding['available_credit']):
                    raise BudgetError('Top-up required: reserved worst-case spend exceeds confirmed available credit')
                if self.liability(value, self.phase) + amount > money(funding['phase_alerts'].get(self.phase, 0)):
                    raise BudgetError('Phase funding alert: pause and request top-up confirmation; do not reduce science')
                if money(value['total_cost']) >= money(funding['alert']):
                    raise BudgetError('Account funding alert: pause and request top-up confirmation')
            token = uuid.uuid4().hex
            value['pending'][token] = {'phase': self.phase, 'reserved': float(amount),
                'request_hash': request_hash, 'model': model, 'metadata': metadata or {},
                'started_at': utcnow(), 'owner_pid': os.getpid()}
            value['status'] = 'active'
            return token

    def settle(self, token, actual_cost, usage=None, metadata=None):
        cost = money(actual_cost)
        exceeded = False
        with self.locked() as value:
            row = value['pending'].pop(token)
            exceeded = cost > money(row['reserved']) + Decimal('0.000000001')
            value['calls'].append({**row, 'reservation_id': token, 'finished_at': utcnow(),
                                   'cost': float(cost), 'usage': usage, 'response_metadata': metadata or {}})
            value['total_cost'] = float(sum((money(c['cost']) for c in value['calls']), Decimal(0)))
            value['price_mismatch'] = bool(value.get('price_mismatch') or exceeded)
            value['status'] = 'blocked_price_mismatch' if value['price_mismatch'] else 'active' if value['pending'] else 'idle'
        if exceeded:
            raise AccountingError('Actual cost exceeded reserved upper bound')

    def release(self, token, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('Known-unbilled release requires a reason')
        self.settle(token, 0, metadata={'known_unbilled': True, 'reason': reason})

    def uncertain(self, token, reason, *, response=None):
        with self.locked() as value:
            if token not in value['pending']:
                raise ValueError('Unknown reservation')
            evidence = {'reservation_id': token, 'reason': reason, 'recorded_at': utcnow()}
            pending = value['pending'][token]
            pending.update(uncertain=True, billing_status='unknown', http_in_flight=False,
                           record_type='uncertain_request_liability')
            if isinstance(response, dict) and response:
                # Observed bytes establish cache identity, never a zero/estimated cost.
                pending['observed_response_sha256'] = digest(response)
                evidence['observed_response_sha256'] = pending['observed_response_sha256']
                evidence['response_metadata'] = {'generation_id': response.get('id'),
                                                 'provider': response.get('provider')}
            value['unknown_cost_calls'].append(evidence)
            phase = pending['phase']
            settled = {r['reservation_id'] for r in value['calls'] if r['phase'] == phase}
            unknown = {key for key, row in value['pending'].items()
                       if row['phase'] == phase and row.get('uncertain')}
            denominator = len(settled | unknown)
            fraction = len(unknown) / denominator if denominator else None
            value.setdefault('unknown_billing_audit', {})[phase] = {
                'unknown_requests': len(unknown), 'completed_accounted_or_unknown_requests': denominator,
                'fraction': fraction, 'report_in_STATUS_above_five_percent': fraction is not None and fraction > .05,
                'continue_run': True, 'authority': 'INBOX2026-11-14 04:15'}
            value['status'] = 'active_with_worst_case_liability'

    def check_projection(self, remaining_cost):
        with self.locked() as value:
            if value['funding'].get('headroom_policy') == 'real_balance_own_inflight_floor':
                if self.bank_headroom(value) - money(remaining_cost) < money(value['funding']['floor_usd']):
                    raise BudgetError('Projected new work would leave less than15USD real headroom')
                return float(money(remaining_cost))
            projected = self.liability(value) + self.external_liability() + money(remaining_cost)
            threshold = value['funding']['alert'] if self.vendor == 'openrouter' else value['funding']['available_credit']
            if projected > money(threshold):
                raise BudgetError(f'Top-up required: projected {self.vendor} spend USD {projected:.2f}')
            return float(projected)

    def snapshot(self):
        with self.locked() as value:
            return {**value, 'liability': float(self.liability(value)),
                    'phase_liability': float(self.liability(value, self.phase)),
                    'external_classifier_liability': float(self.external_liability())}

    # Compatibility boundary for the sanitized OpenRouter transport.
    def finish(self, token, response=None, *, known_free=False, error=None):
        if known_free:
            return self.release(token, error or 'explicit no-generation rejection')
        response = response or {}
        usage = response.get('usage')
        usage = usage if isinstance(usage, dict) else {}
        cost = usage.get('cost')
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
            self.uncertain(token, error or 'missing provider usage cost', response=response)
            return None  #04:15: retain uncertainty, never invent zero, continue per request.
        return self.settle(token, cost, usage, {'generation_id': response.get('id'),
                                               'provider': response.get('provider'), 'error': error})

    def accounted(self, request_hash, response):
        with self.locked() as value:
            return any(c['request_hash'] == request_hash
                       and c.get('response_metadata', {}).get('generation_id') == response.get('id')
                       for c in value['calls']) or any(
                           r.get('uncertain') and r.get('request_hash') == request_hash
                           and r.get('observed_response_sha256') == digest(response)
                           for r in value['pending'].values())
