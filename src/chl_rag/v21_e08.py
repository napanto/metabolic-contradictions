"""Addendum 08E: classification of the ordered semantic-route pair list, stopped at budget B.

Classifier settings are exactly those of 08D (08A routes and retry rules, 08C effort, frozen
prompt); only the input list, the output directories and the admission rule differ.

Admission rule (08E section 7.2, as registered):
- pairs are admitted strictly in list order (`order_08e.position`);
- the next pair is admitted only while
      actual 08E ledger spend + (pairs in flight + unknown-billing 08E calls) x p95 + mean <= B,
  where p95 and mean are the 95th percentile and the mean of the cost per pair (retries
  included) of the pairs newly classified in 08D;
- the ledger spend is re-read at least every 500 admissions and at most a few seconds apart;
- admission also stops at the registered time cutoff and below a free-space floor on the
  output file system;
- after the first refusal nothing more is admitted, in this or any later invocation (the
  classified set is a prefix of the list); calls in flight finish.

The ledger phase stays `production_classification` (the reviewed SQLite ledger accepts only
the two production phases); 08E spend is every classification call started at or after the
08E start time, which is recorded on the first launch and kept across restarts.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from . import production as P
from . import corpus_run as V
from .io import file_sha, read_jsonl, utcnow, write_json

BASE = Path('data/derived/production_v21_08e')
RESULTS = Path('results/production_v21_08e')
OUT = BASE / 'classification'
LEDGER_DB = Path('data/raw/v2/accounting/openrouter.sqlite3')
PHASE = 'production_classification'
SOURCE = Path('data/derived/production/full')
UNCAPPED_CLASSIFICATION = Path('data/derived/production_v21_uncapped/classification')
REFRESH_EVERY = 500
MAX_STALENESS_SECONDS = 5.0
FREE_SPACE_FLOOR_BYTES = 800 * 1024 * 1024


def use_paths():
    V.STATUS = RESULTS / 'status.json'
    RESULTS.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)


def verify_protocol(path):
    """The registered 08E addendum: committed, unmodified, in a good signed commit of the main tree."""
    run = lambda *a: subprocess.run(['git', *a], capture_output=True, text=True, check=False).stdout
    if not Path(path).is_file():
        raise P.ExperimentBlocked('08E addendum is missing: ' + str(path))
    if run('status', '--porcelain', '--', str(path)).strip():
        raise P.ExperimentBlocked('08E addendum has uncommitted changes')
    line = run('log', '-1', '--format=%H %G?', '--', str(path)).split()
    if len(line) != 2 or line[1] != 'G':
        raise P.ExperimentBlocked('08E addendum is not in a good signed commit')
    return {'path': str(path), 'commit': line[0], 'sha256': file_sha(path)}


def pair_cost_stats(directory=UNCAPPED_CLASSIFICATION):
    """Cost per pair (all attempts) of the pairs newly classified in 08D (reused records excluded)."""
    costs = []
    for path in Path(directory).glob('*.json'):
        record = json.loads(path.read_text())
        if record.get('reused_from'):
            continue
        costs.append(sum(float((a.get('usage') or {}).get('cost') or 0) for a in record.get('attempts', [])))
    if not costs:
        raise P.ExperimentBlocked('No 08D classification records for the cost statistics')
    costs.sort()
    quantile = lambda q: costs[int(q * (len(costs) - 1))]
    return {'source': str(directory), 'pairs': len(costs), 'mean': sum(costs) / len(costs),
            'p50': quantile(0.5), 'p95': quantile(0.95), 'p99': quantile(0.99), 'max': costs[-1]}


class LedgerSpend:
    """Read-only, incremental reader of the 08E spend on the SQLite ledger."""

    def __init__(self, db, since):
        self.uri = f'file:{Path(db).resolve()}?mode=ro'
        self.since = since
        self.last_seq = 0
        self.spent = Decimal(0)
        self.calls = 0
        self.unknown_billing = 0
        self.read_at = None

    def refresh(self):
        connection = sqlite3.connect(self.uri, uri=True, timeout=30)
        try:
            rows = connection.execute('SELECT seq, cost_decimal, row_json FROM calls WHERE seq > ? AND phase = ? '
                                      'ORDER BY seq', (self.last_seq, PHASE)).fetchall()
            for seq, cost, row_json in rows:
                self.last_seq = max(self.last_seq, seq)
                if (json.loads(row_json).get('started_at') or '') >= self.since:
                    self.spent += Decimal(cost)
                    self.calls += 1
            unknown = 0
            for (row_json,) in connection.execute('SELECT row_json FROM pending WHERE phase = ? AND uncertain = 1',
                                                  (PHASE,)):
                if (json.loads(row_json).get('started_at') or '') >= self.since:
                    unknown += 1
            self.unknown_billing = unknown
        finally:
            connection.close()
        self.read_at = time.monotonic()
        return self


def free_bytes(path='.'):
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize


class BudgetGate:
    """Admission in list order; once closed, never reopened."""

    def __init__(self, *, budget, inflight_cost, next_cost, spend, stop_at=None, refused_from=None,
                 refresh_every=REFRESH_EVERY, max_staleness=MAX_STALENESS_SECONDS,
                 free_space=free_bytes, free_floor=FREE_SPACE_FLOOR_BYTES, clock=time.time):
        self.budget = Decimal(str(budget))
        self.inflight_cost = Decimal(str(inflight_cost))
        self.next_cost = Decimal(str(next_cost))
        self.spend = spend
        self.stop_at = stop_at
        self.refused_from = refused_from
        self.refresh_every, self.max_staleness = refresh_every, max_staleness
        self.free_space, self.free_floor, self.clock = free_space, free_floor, clock
        self.inflight = 0
        self.admitted = 0
        self.since_refresh = 0
        self.closed = None          # {'position', 'reason', 'at', 'projected_usd'}
        self.last_projection = None

    def projection(self):
        return (self.spend.spent + (self.inflight + self.spend.unknown_billing) * self.inflight_cost
                + self.next_cost)

    def _close(self, position, reason):
        self.closed = {'position': position, 'reason': reason, 'at': utcnow(),
                       'projected_usd': float(self.last_projection) if self.last_projection is not None else None,
                       'spent_usd': float(self.spend.spent), 'inflight': self.inflight}
        return False

    def admit(self, position):
        """Synchronous decision; the caller serialises calls (one admission at a time, in order)."""
        if self.closed:
            return False
        if self.refused_from is not None and position >= self.refused_from:
            return self._close(position, 'refused in an earlier invocation')
        if (self.since_refresh >= self.refresh_every or self.spend.read_at is None
                or time.monotonic() - self.spend.read_at > self.max_staleness):
            self.spend.refresh()
            self.since_refresh = 0
        if self.stop_at is not None and self.clock() >= self.stop_at:
            return self._close(position, 'time cutoff')
        if self.free_space() < self.free_floor:
            return self._close(position, 'free space below floor')
        self.last_projection = self.projection()
        if self.last_projection > self.budget:
            return self._close(position, 'budget')
        self.inflight += 1
        self.admitted += 1
        self.since_refresh += 1
        return True

    def finished(self):
        self.inflight -= 1


def load_run_state(path, *, list_path, budget, stop_at_text):
    """First launch fixes the start time and the cost statistics; restarts must match them."""
    if path.exists():
        state = json.loads(path.read_text())
        if (state['list_path'] != str(list_path) or state['list_sha256'] != file_sha(list_path)
                or Decimal(state['budget_usd']) != Decimal(str(budget)) or state['stop_admitting_at'] != stop_at_text):
            raise P.ExperimentBlocked('08E restart with a different list, budget or cutoff')
        return state
    state = {'started_at': utcnow(), 'list_path': str(list_path), 'list_sha256': file_sha(list_path),
             'budget_usd': str(budget), 'stop_admitting_at': stop_at_text, 'cost_per_pair_08D': pair_cost_stats(),
             'refused_from_position': None}
    write_json(path, state)
    return state


async def classify_list(*, list_path, budget, stop_at_text, protocol, concurrency, client):
    """Run the 08E admission loop with a built V21Client (closed here); returns the summary."""
    use_paths()
    started = time.time()
    config, binding = P.production_guard(P.CONFIG, require_classifier=True)
    state_path = RESULTS / 'run_state.json'
    state = load_run_state(state_path, list_path=list_path, budget=budget, stop_at_text=stop_at_text)
    stop_at = None
    if stop_at_text:
        moment = datetime.fromisoformat(stop_at_text)
        if moment.tzinfo is None:
            raise P.ExperimentBlocked('The 08E time cutoff needs an explicit UTC offset')
        stop_at = moment.timestamp()
    pairs = list(read_jsonl(list_path))
    positions = [p['order_08e']['position'] for p in pairs]
    if positions != sorted(positions) or len(set(p['pair_id'] for p in pairs)) != len(pairs):
        raise P.ExperimentBlocked('08E list is not in position order or has duplicate pairs')
    extractions = {e['pmid']: e for e in read_jsonl(SOURCE / 'extractions.jsonl')}
    wanted = {p[k] for p in pairs for k in ('pmid_a', 'pmid_b')}
    articles = {a['pmid']: a for a in read_jsonl('data/derived/corpus.jsonl') if a['pmid'] in wanted}
    classifier_binding = {**binding, **config['_classifier_binding'],
                          'addendum_08E': {'list_sha256': state['list_sha256']}}
    V.status(state='reconciling', pid=os.getpid(), protocol=protocol, run_state=state,
             reconciliation=V.reconcile_started_classifications(OUT))
    terminal = {p.stem for p in OUT.glob('*.json')}
    todo = [p for p in pairs if p['pair_id'] not in terminal]
    stats = state['cost_per_pair_08D']
    spend = LedgerSpend(LEDGER_DB, state['started_at']).refresh()
    gate = BudgetGate(budget=budget, inflight_cost=stats['p95'], next_cost=stats['mean'], spend=spend,
                      stop_at=stop_at, refused_from=state.get('refused_from_position'),
                      refresh_every=REFRESH_EVERY, max_staleness=MAX_STALENESS_SECONDS)
    results, errors = [], []
    order_lock = asyncio.Lock()
    iterator = iter(todo)
    by_step = Counter()

    async def worker():
        while True:
            async with order_lock:
                if gate.closed or errors:
                    return
                pair = next(iterator, None)
                if pair is None:
                    return
                admitted = await asyncio.to_thread(gate.admit, pair['order_08e']['position'])
                if not admitted:
                    return
            try:
                record = await P._classify_one(client, pair, articles, extractions, config, classifier_binding, OUT)
                results.append(record)
                by_step[(pair['order_08e']['step'], record['status'])] += 1
            except Exception as exc:  # noqa: BLE001 -- stop admitting, let calls in flight finish
                errors.append(exc)
            finally:
                gate.finished()

    async def monitor():
        while True:
            await asyncio.sleep(30)
            V.status(state='classifying', admitted=gate.admitted, inflight=gate.inflight,
                     terminal_this_invocation=len(results),
                     status_counts=dict(Counter(r['status'] for r in results)),
                     spent_08E_usd=float(spend.spent), ledger_calls_08E=spend.calls,
                     unknown_billing_08E=spend.unknown_billing,
                     projected_usd=float(gate.last_projection) if gate.last_projection is not None else None,
                     budget_usd=float(gate.budget), gate_closed=gate.closed,
                     free_bytes=free_bytes(), counters=dict(client.counters), run_holds=client.run_holds,
                     cooldowns=client.cooldowns, key_limit_waiting=client.key_blocked_until > time.time())

    V.status(state='classifying', list_pairs=len(pairs), already_terminal=len(pairs) - len(todo),
             budget_usd=float(budget), stop_admitting_at=stop_at_text, concurrency=concurrency,
             cost_per_pair_08D=stats, started_at_08E=state['started_at'])
    watcher = asyncio.create_task(monitor())
    try:
        await asyncio.gather(*(worker() for _ in range(min(concurrency, max(1, len(todo))))))
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        if gate.closed and state.get('refused_from_position') is None:
            state['refused_from_position'] = gate.closed['position']
            state['refusal'] = gate.closed
            write_json(state_path, state)
        await client.close()
    spend.refresh()
    records = {}
    for pair in pairs:
        path = OUT / f"{pair['pair_id']}.json"
        if path.exists():
            records[pair['pair_id']] = json.loads(path.read_text())
    steps = Counter()
    for pair in pairs:
        record = records.get(pair['pair_id'])
        steps[(pair['order_08e']['step'], record['status'] if record else 'not_classified')] += 1
    classified = [p['order_08e']['position'] for p in pairs if p['pair_id'] in records]
    summary = {'addendum': '08E', 'protocol': protocol, 'list_path': str(list_path), 'list_sha256': state['list_sha256'],
               'list_pairs': len(pairs), 'budget_usd': float(budget), 'stop_admitting_at': stop_at_text,
               'started_at_08E': state['started_at'], 'finished_at': utcnow(),
               'admitted_this_invocation': gate.admitted, 'terminal_pairs': len(records),
               'valid_pairs': sum(r['status'] == 'valid' for r in records.values()),
               'invalid_pairs': sum(r['status'] == 'invalid' for r in records.values()),
               'by_step': {f'{step}/{status}': n for (step, status), n in sorted(steps.items())},
               'first_unadmitted_position': state.get('refused_from_position'),
               'refusal': state.get('refusal'),
               'prefix_property': classified == list(range(1, len(classified) + 1)),
               'last_classified_position': max(classified) if classified else None,
               'spent_08E_usd': float(spend.spent), 'ledger_calls_08E': spend.calls,
               'unknown_billing_08E': spend.unknown_billing, 'cost_per_pair_08D': stats,
               'effort': config['classification']['reasoning'], 'model': config['classification']['model'],
               'wall_seconds_this_invocation': round(time.time() - started, 1),
               'errors': [f'{type(e).__name__}: {str(e)[:200]}' for e in errors[:5]]}
    write_json(RESULTS / 'summary.json', summary)
    if errors:
        V.status(state='stopped', error_type=type(errors[0]).__name__, error=str(errors[0])[:300], summary=summary)
        raise errors[0]
    V.status(state='complete', summary=summary)
    return summary
