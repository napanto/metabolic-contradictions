"""Addendum 08E admission rule: offline, with a temporary SQLite ledger and a fake classifier."""
import asyncio
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from chl_rag import corpus_run as V
from chl_rag import v21_e08 as E8
from chl_rag import production as P
from chl_rag.io import utcnow, write_json, write_jsonl

SCHEMA = '''
CREATE TABLE calls (seq INTEGER PRIMARY KEY, reservation_id TEXT UNIQUE NOT NULL,
 phase TEXT NOT NULL, request_hash TEXT NOT NULL, generation_identity TEXT NOT NULL,
 cost_decimal TEXT NOT NULL, row_json TEXT NOT NULL);
CREATE TABLE pending (reservation_id TEXT PRIMARY KEY, phase TEXT NOT NULL,
 request_hash TEXT NOT NULL, reserved_decimal TEXT NOT NULL, uncertain INTEGER NOT NULL,
 response_sha256 TEXT, row_json TEXT NOT NULL);
'''


def make_db(path):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()


def add_call(path, cost, started_at, phase=E8.PHASE):
    connection = sqlite3.connect(path)
    seq = connection.execute('SELECT coalesce(max(seq), 0) + 1 FROM calls').fetchone()[0]
    connection.execute('INSERT INTO calls VALUES (?,?,?,?,?,?,?)',
                       (seq, f'r{seq}', phase, 'h', '"g"', str(cost),
                        json.dumps({'started_at': started_at, 'phase': phase})))
    connection.commit()
    connection.close()


def add_uncertain(path, started_at):
    connection = sqlite3.connect(path)
    n = connection.execute('SELECT count(*) FROM pending').fetchone()[0]
    connection.execute('INSERT INTO pending VALUES (?,?,?,?,1,NULL,?)',
                       (f'u{n}', E8.PHASE, 'h', '0.1', json.dumps({'started_at': started_at})))
    connection.commit()
    connection.close()


def test_ledger_spend_counts_only_08e_calls_and_unknown_billing(tmp_path):
    db = tmp_path / 'ledger.sqlite3'
    make_db(db)
    add_call(db, '0.5', '2026-11-16T06:00:00+00:00')                       # 08D, before the start
    add_call(db, '0.25', '2026-11-16T07:40:00+00:00')
    add_call(db, '9', '2026-11-16T07:41:00+00:00', phase='production_extraction')
    add_uncertain(db, '2026-11-16T07:42:00+00:00')
    add_uncertain(db, '2026-11-16T05:00:00+00:00')                          # older liability
    spend = E8.LedgerSpend(db, '2026-11-16T07:35:00+00:00').refresh()
    assert spend.spent == Decimal('0.25') and spend.calls == 1 and spend.unknown_billing == 1
    add_call(db, '0.125', '2026-11-16T07:43:00+00:00')
    spend.refresh()
    assert spend.spent == Decimal('0.375') and spend.calls == 2


class FakeSpend:
    def __init__(self):
        self.spent, self.calls, self.unknown_billing, self.read_at, self.refreshes = Decimal(0), 0, 0, None, 0

    def refresh(self):
        self.refreshes += 1
        self.read_at = __import__('time').monotonic()
        return self


def gate(spend, **kw):
    options = dict(budget='1.0', inflight_cost='0.1', next_cost='0.05', spend=spend, max_staleness=1e9,
                   free_space=lambda: 10 ** 12)
    options.update(kw)
    return E8.BudgetGate(**options)


def test_gate_counts_inflight_at_p95_and_never_reopens():
    spend = FakeSpend()
    g = gate(spend)
    # 0 + k*0.1 + 0.05 <= 1.0 admits k = 0..9 -> 10 pairs in flight
    admitted = [g.admit(i) for i in range(1, 12)]
    assert admitted == [True] * 10 + [False]
    assert g.closed['position'] == 11 and g.closed['reason'] == 'budget'
    for _ in range(10):
        g.finished()
    assert g.admit(12) is False                     # closed: a prefix only


def test_gate_uses_actual_spend_and_unknown_billing():
    spend = FakeSpend()
    spend.spent, spend.unknown_billing = Decimal('0.8'), 1
    g = gate(spend)
    # 0.8 + (0 + 1)*0.1 + 0.05 = 0.95 <= 1 ; next: 0.8 + 2*0.1 + 0.05 = 1.05 > 1
    assert g.admit(1) is True
    assert g.admit(2) is False


def test_gate_refreshes_at_least_every_500_admissions():
    spend = FakeSpend()
    g = gate(spend, budget='1000', inflight_cost='0', next_cost='0')
    for i in range(1, 1202):
        assert g.admit(i)
    assert spend.refreshes == 3                     # first admission, after 500, after 1000


def test_gate_time_cutoff_free_space_and_earlier_refusal():
    now = [100.0]
    g = gate(FakeSpend(), stop_at=150.0, clock=lambda: now[0])
    assert g.admit(1)
    now[0] = 150.0
    assert g.admit(2) is False and g.closed['reason'] == 'time cutoff'
    g = gate(FakeSpend(), free_space=lambda: 100, free_floor=800)
    assert g.admit(1) is False and g.closed['reason'] == 'free space below floor'
    g = gate(FakeSpend(), refused_from=5)
    assert all(g.admit(i) for i in range(1, 5))
    assert g.admit(5) is False and g.closed['reason'] == 'refused in an earlier invocation'


class FakeClient:
    counters, run_holds, cooldowns, key_blocked_until = {}, {}, {}, 0.0

    async def close(self):
        pass


def setup_run(tmp_path, monkeypatch, pairs_n=60):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / 'ledger.sqlite3'
    make_db(db)
    monkeypatch.setattr(E8, 'LEDGER_DB', db)
    monkeypatch.setattr(E8, 'MAX_STALENESS_SECONDS', 0.0)
    monkeypatch.setattr(E8, 'pair_cost_stats', lambda: {'mean': 0.01, 'p95': 0.02, 'pairs': 1})
    monkeypatch.setattr(V, 'reconcile_started_classifications', lambda directory: {'archived': 0})
    config = {'classification': {'reasoning': 'high', 'model': 'z-ai/glm-5.3-flash'}, '_classifier_binding': {}}
    monkeypatch.setattr(P, 'production_guard', lambda *a, **k: (config, {'config_sha256': 'x'}))
    write_jsonl(E8.SOURCE / 'extractions.jsonl', [{'pmid': '1'}, {'pmid': '2'}])
    write_jsonl(Path('data/derived/corpus.jsonl'), [{'pmid': '1'}, {'pmid': '2'}])
    pairs = [{'pair_id': f'p{i:03d}', 'pmid_a': '1', 'pmid_b': '2',
              'order_08e': {'position': i, 'step': '2a_T1' if i <= 30 else '3_band_T1T2'}} for i in range(1, pairs_n + 1)]
    write_jsonl(tmp_path / 'list.jsonl', pairs)
    return db, pairs


def test_classify_list_stops_at_budget_and_keeps_a_prefix(tmp_path, monkeypatch):
    db, pairs = setup_run(tmp_path, monkeypatch)
    started = []

    async def fake_classify(client, pair, articles, extractions, config, binding, destination):
        started.append(pair['order_08e']['position'])
        await asyncio.sleep(0.001 * (pair['order_08e']['position'] % 5))
        add_call(db, '0.01', utcnow())
        record = {'pair_id': pair['pair_id'], 'status': 'valid', 'verdict': {}}
        write_json(destination / f"{pair['pair_id']}.json", record)
        return record

    monkeypatch.setattr(P, '_classify_one', fake_classify)
    summary = asyncio.run(E8.classify_list(list_path=tmp_path / 'list.jsonl', budget='0.30', stop_at_text=None,
                                           protocol={'path': 'x'}, concurrency=4, client=FakeClient()))
    assert summary['prefix_property'] is True
    assert 0 < summary['terminal_pairs'] < len(pairs)
    assert summary['first_unadmitted_position'] == summary['terminal_pairs'] + 1
    assert summary['spent_08E_usd'] <= 0.30 + 4 * 0.02
    assert started == sorted(started)               # admission strictly in list order
    # A restart admits nothing past the first refusal, even with a larger budget recorded elsewhere.
    state = json.loads((E8.RESULTS / 'run_state.json').read_text())
    assert state['refused_from_position'] == summary['first_unadmitted_position']
    again = asyncio.run(E8.classify_list(list_path=tmp_path / 'list.jsonl', budget='0.30', stop_at_text=None,
                                         protocol={'path': 'x'}, concurrency=4, client=FakeClient()))
    assert again['terminal_pairs'] == summary['terminal_pairs'] and again['admitted_this_invocation'] == 0


def test_classify_list_restart_with_other_budget_is_blocked(tmp_path, monkeypatch):
    setup_run(tmp_path, monkeypatch, pairs_n=3)

    async def fake_classify(client, pair, articles, extractions, config, binding, destination):
        record = {'pair_id': pair['pair_id'], 'status': 'valid', 'verdict': {}}
        write_json(destination / f"{pair['pair_id']}.json", record)
        return record

    monkeypatch.setattr(P, '_classify_one', fake_classify)
    asyncio.run(E8.classify_list(list_path=tmp_path / 'list.jsonl', budget='1', stop_at_text=None,
                                 protocol={}, concurrency=2, client=FakeClient()))
    with pytest.raises(P.ExperimentBlocked):
        asyncio.run(E8.classify_list(list_path=tmp_path / 'list.jsonl', budget='2', stop_at_text=None,
                                     protocol={}, concurrency=2, client=FakeClient()))
    with pytest.raises(P.ExperimentBlocked):
        asyncio.run(E8.classify_list(list_path=tmp_path / 'list.jsonl', budget='1',
                                     stop_at_text='2026-11-16T11:00:00', protocol={}, concurrency=2,
                                     client=FakeClient()))
