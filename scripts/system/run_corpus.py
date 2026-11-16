#!/usr/bin/env python3
"""Addendum 08A orchestrator: v2.1 extraction -> frame -> funding check -> classification -> freeze.

Run from the MAIN project root (ledger, data, caches), detached, with
CHL_V21_ROOT=<08A checkout> and PYTHONPATH=<08A checkout>/src. Every stage is resumable:
terminal checkpoints are reused, and a rerun of `all` continues where it stopped.
Status: results/production/status.json; log: results/production/run.log.
Action e08 (addendum 08E): status and summary in results/production_v21_08e/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

from chl_rag import corpus_run as V

STAGE_CONCURRENCY = {'extract': 640, 'classify': 640}
V2_CLASSIFICATION_USD_PER_PAIR = 8.346419 / 11526


def raise_fd_limit():
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 65536), hard))


def terminal_counts(stage):
    from chl_rag import production as P
    folder = (P.BASE / 'extraction' / 'glm-5.3-flash__high') if stage == 'extract' else (P.BASE / 'classification')
    if not folder.exists():
        return 0
    return sum(1 for p in folder.glob('*.json'))


async def monitor(stage, client, ledger, stop, total):
    history = []
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except TimeoutError:
            pass
        try:
            done = await asyncio.to_thread(terminal_counts, stage)
            now = time.time()
            history = [h for h in history if now - h[0] <= 900] + [(now, done)]
            rate = (history[-1][1] - history[0][1]) / max(1, history[-1][0] - history[0][0]) * 60
            eta_min = (total - done) / rate if rate > 0 and total else None
            progress_path = V.RESULTS / f"{'extraction' if stage == 'extract' else 'classification'}_full_progress.json"
            progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
            V.status(stage=stage, terminal=done, total=total, rate_per_min_15m=round(rate, 2),
                     eta_minutes=None if eta_min is None else round(eta_min, 1),
                     this_invocation=progress.get('status_counts'),
                     ledger_phase=ledger.phase, ledger=ledger.totals(),
                     counters=dict(client.counters), run_holds=client.run_holds, cooldowns=client.cooldowns,
                     key_limit_waiting=client.key_blocked_until > time.time(), key_block_events=client.key_block_events[-6:],
                     provider_caps={s['provider']: s['cap'] for s in client.control.report()})
        except Exception as exc:  # noqa: BLE001 -- monitoring must never stop the run
            V.status(monitor_error=type(exc).__name__)


async def export_loop(ledger, stop):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            pass
        try:
            await asyncio.to_thread(ledger.export_json)
        except Exception as exc:  # noqa: BLE001 -- a busy export lock (another process) is not a stop
            V.status(last_export_skip=type(exc).__name__)


async def run_stage(stage):
    from chl_rag import production as P
    from chl_rag.sqlite_ledger import SQLitePhaseLedger
    phase = 'production_extraction' if stage == 'extract' else 'production_classification'
    config, binding = P.production_guard(P.CONFIG, require_classifier=stage == 'classify')
    ledger = SQLitePhaseLedger('openrouter', phase)
    if ledger.approved['evaluation_config_sha256'] != binding['config_sha256']:
        raise P.ExperimentBlocked('SQLite authority and production configuration differ')
    routes = config['routing_config']
    if stage == 'classify':
        adapted = P.classifier_routes(config, json.loads(Path(routes).read_text()))
        target = V.RESULTS / 'classifier_routes.json'
        if target.exists() and json.loads(target.read_text()) != adapted:
            raise P.ExperimentBlocked('Classifier routing adapter changed during the run')
        P.write_json(target, adapted)
        routes = str(target)
    concurrency = STAGE_CONCURRENCY[stage]
    client = V.V21Client(phase, routes_path=routes, ledger=ledger, concurrency=concurrency)
    total = len(json.loads(Path('data/derived/partitions.json').read_text())['partition_pmids']['F']) \
        if stage == 'extract' else sum(1 for _ in open(P.BASE / 'full' / 'pairs_candidates.jsonl'))
    stop = asyncio.Event()
    tasks = [asyncio.create_task(monitor(stage, client, ledger, stop, total)),
             asyncio.create_task(export_loop(ledger, stop))]
    V.status(stage=stage, stage_started_at=P.utcnow(), concurrency=concurrency,
             routes=[r['tag'] for r in client.models[config['extraction']['model']]['routes']])
    try:
        function = P.run_extraction if stage == 'extract' else P.run_classification
        return await function('full', config_path=P.CONFIG, client=client, concurrency=concurrency)
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        V.status(counters=dict(client.counters), run_holds=client.run_holds, cooldowns=client.cooldowns)
        await client.close()
        try:
            ledger.export_json()
        except Exception:  # noqa: BLE001
            pass
        ledger.close()


async def balance_check(stage, projected):
    from chl_rag import production as P
    from chl_rag.sqlite_ledger import SQLitePhaseLedger
    phase = 'production_extraction' if stage == 'extract' else 'production_classification'
    ledger = SQLitePhaseLedger('openrouter', phase)
    config, _binding = P.production_guard(P.CONFIG)
    client = V.V21Client(phase, routes_path=config['routing_config'], ledger=ledger, concurrency=4)
    try:
        snapshot = await V.fresh_balance(client)
    finally:
        await client.close()
        ledger.close()
    snapshot.update(stage=stage, projected_cost_usd=round(projected, 4))
    P.write_json(V.RESULTS / f'credit_snapshot_{stage}.json', snapshot)
    return snapshot


def prefetch():
    """CPU retrieval prefetch over terminal v2.1 checkpoints, until all of F is terminal."""
    import fcntl
    import torch
    torch.set_num_threads(max(4, (os.cpu_count() or 8) // 2))
    V.install()
    from chl_rag import production as P
    from chl_rag import retrieval_prefetch as R
    lock = Path('data/raw/v21/retrieval_prefetch.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = R.Prefetcher(P.CONFIG)
        while True:
            result = worker.once()
            print(json.dumps({k: result.get(k) for k in ('status', 'terminal_sources', 'unique_queries_warmed',
                                                          'searched_queries', 'cache_hit_queries')}), flush=True)
            if result['status'] == 'complete':
                return result
            time.sleep(30)


def run_all():
    from chl_rag import production as P
    V.status(state='running', pid=os.getpid(), started_at=P.utcnow(), error_type=None, error=None,
             registration=V.STATE['proofs'], v21_binding=V.STATE['v21_binding'])
    V.status(reconciliation=V.reconcile_preflight_blocks())
    extraction_projection = float(V.STATE['config']['funding']['extraction_projection_usd'])
    snapshot = asyncio.run(balance_check('extract', extraction_projection))
    V.status(extraction_balance=snapshot)
    if snapshot['balance_usd'] < extraction_projection:
        V.status(state='blocked_funding', blocked_stage='extract')
        return 3
    log = open(V.RESULTS / 'prefetch.log', 'a')
    env = dict(os.environ)
    child = subprocess.Popen([sys.executable, '-u', __file__, 'prefetch'], stdout=log, stderr=subprocess.STDOUT, env=env)
    V.status(prefetch_pid=child.pid)
    try:
        extraction = asyncio.run(run_stage('extract'))
    except BaseException:
        child.terminate()
        raise
    V.status(stage='extract_done', extraction_summary={k: extraction.get(k) for k in (
        'requested_sources', 'terminal_sources', 'valid_sources', 'invalid_sources', 'retained_claims')})
    code = child.wait()
    V.status(prefetch_exit=code)
    frame = P.build_frame('full', config_path=P.CONFIG)
    V.status(stage='frame_done', frame_summary={k: frame.get(k) for k in ('pairs', 'key_only_pairs', 'retrieval_queries')})
    projected = frame['pairs'] * V2_CLASSIFICATION_USD_PER_PAIR * 1.5
    snapshot = asyncio.run(balance_check('classify', projected))
    V.status(classification_balance=snapshot)
    if snapshot['balance_usd'] < projected:
        V.status(state='blocked_funding', blocked_stage='classify')
        return 3
    classification = asyncio.run(run_stage('classify'))
    V.status(stage='classify_done', classification_summary={k: classification.get(k) for k in (
        'requested_pairs', 'terminal_pairs', 'valid_pairs', 'invalid_pairs')})
    frozen = P.freeze_frame('full', config_path=P.CONFIG)
    V.status(state='complete', stage='frozen', frozen_at=frozen['frozen_at'])
    return 0


PER_PAIR_35 = {'low': 0.030 / 35, 'high': 0.057 / 35}


def projection(effort, pairs, decision=None):
    measured = (decision or {}).get('measured_35_pair_cost_usd')
    per_pair = measured / 35 if measured else PER_PAIR_35.get(effort, PER_PAIR_35['high'] * 3)
    return pairs * per_pair * 1.5


def classify_gated():
    """Addendum 08C gate: hold the classification lock, wait for the frame, the end of the first
    orchestrator and a signed effort decision; then wait for funds and classify at that effort."""
    import fcntl
    from chl_rag import production as P
    main_status = V.STATUS
    V.STATUS = V.RESULTS / 'status_classify.json'
    handle = open(V.RESULTS / 'classification_full.lock', 'a')
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    V.status(state='waiting', pid=os.getpid(), holds_classification_lock=True, started_at=P.utcnow())
    while True:
        frame_path = V.RESULTS / 'frame_full.json'
        frame = json.loads(frame_path.read_text()) if frame_path.exists() else {}
        first = subprocess.run(['systemctl', '--user', 'is-active', 'chl-v21-corpus'],
                               capture_output=True, text=True).stdout.strip()
        decision, why = V.read_effort_decision()
        V.status(frame_complete=frame.get('status') == 'complete', first_orchestrator=first,
                 decision_state=why, decision=decision)
        if frame.get('status') == 'complete' and first != 'active' and decision:
            break
        time.sleep(30)
    V.STATE['classifier_effort'] = decision
    V.status(reconciliation=V.reconcile_started_classifications(V.BASE / 'classification'))
    projected = projection(decision['effort'], frame['pairs'], decision)
    while True:
        snapshot = asyncio.run(balance_check('classify', projected))
        V.status(classification_balance=snapshot, projected_usd=round(projected, 2))
        if snapshot['balance_usd'] >= projected:
            break
        V.status(state='waiting_topup')
        time.sleep(120)
    fcntl.flock(handle, fcntl.LOCK_UN)
    handle.close()
    V.status(state='classifying', effort=decision['effort'])
    classification = asyncio.run(run_stage('classify'))
    V.status(stage='classify_done', classification_summary={k: classification.get(k) for k in (
        'requested_pairs', 'terminal_pairs', 'valid_pairs', 'invalid_pairs')})
    frozen = P.freeze_frame('full', config_path=P.CONFIG)
    V.status(state='complete', frozen_at=frozen['frozen_at'])
    V.STATUS = main_status
    V.status(state='complete', stage='frozen', frozen_at=frozen['frozen_at'], classifier_effort=decision['effort'])
    return 0


def uncapped():
    """Addendum 08D: build the uncapped frame now; classify after the capped 08A frame is frozen."""
    from chl_rag import v21_uncapped as U
    from chl_rag import production as P
    started = time.time()
    U.use_uncapped_paths()
    V.status(state='waiting_decision', pid=os.getpid(), started_at=P.utcnow())
    while True:
        decision, why = V.read_effort_decision()
        if decision:
            break
        V.status(decision_state=why)
        time.sleep(30)
    V.STATE['classifier_effort'] = decision
    V.status(state='building_frame', effort=decision['effort'])
    existing = U.RESULTS / 'frame_full.json'
    frame = json.loads(existing.read_text()) if existing.exists() else {}
    if not (frame.get('status') == 'complete'
            and P.file_sha(U.BASE / 'full' / 'pairs_candidates.jsonl') == frame.get('pairs_sha256')):
        frame = U.build_frame()
    new = frame['new_against_capped']
    projected = projection(decision['effort'], new, decision)
    V.status(state='frame_built', frame={k: frame[k] for k in ('pairs', 'key_only_pairs', 'by_origin', 'by_arm',
             'by_sign_pattern', 'capped_pairs', 'new_against_capped', 'retrieval_cache_misses', 'build_seconds')},
             projected_new_classification_usd=round(projected, 2),
             expected_new_classification_usd=round(projected / 1.5, 2))
    capped_frozen = U.CAPPED_RESULTS / 'frozen_frame_full.json'
    while not capped_frozen.exists():
        V.status(state='waiting_capped_freeze')
        time.sleep(30)
    V.status(reconciliation=V.reconcile_started_classifications(U.BASE / 'classification'))
    reuse = U.reuse_capped_classifications()
    V.status(reuse=reuse)
    per_pair = U.capped_mean_cost()
    to_classify = reuse.get('new', 0) + reuse.get('capped_but_different_request', 0)
    remaining = to_classify * per_pair * 1.5 if per_pair else projection(decision['effort'], to_classify, decision)
    V.status(measured_capped_cost_per_pair=per_pair, pairs_to_classify=to_classify)
    while True:
        snapshot = asyncio.run(balance_check('classify', remaining))
        V.status(classification_balance=snapshot, projected_remaining_usd=round(remaining, 2))
        if snapshot['balance_usd'] >= remaining:
            break
        V.status(state='waiting_topup')
        time.sleep(120)
    V.status(state='classifying')
    classification = asyncio.run(run_stage('classify'))
    frozen = P.freeze_frame('full', config_path=P.CONFIG)
    cost = U.classified_cost()
    summary = {'addendum': '08D', 'frozen_at': frozen['frozen_at'], 'effort': decision['effort'],
               'pairs': frame['pairs'], 'by_origin': frame['by_origin'], 'by_arm': frame['by_arm'],
               'by_sign_pattern': frame['by_sign_pattern'], 'capped_pairs': frame['capped_pairs'],
               'new_against_capped': frame['new_against_capped'], 'reuse': reuse,
               'classified': classification['terminal_pairs'], 'valid': classification['valid_pairs'],
               'invalid': classification['invalid_pairs'], **cost,
               'wall_seconds': round(time.time() - started, 1)}
    P.write_json(U.RESULTS / 'summary.json', summary)
    V.status(state='complete', summary=summary)
    return 0


def e08(args):
    """Addendum 08E: classify the registered ordered list in order, stopping admission at B, at the
    registered time cutoff or below the free-space floor; settings exactly as 08D."""
    import fcntl
    from chl_rag import v21_e08 as E8
    from chl_rag import production as P
    from chl_rag.sqlite_ledger import SQLitePhaseLedger
    E8.use_paths()
    protocol = E8.verify_protocol(args.protocol)
    if P.file_sha(args.list) != args.list_sha256:
        raise P.ExperimentBlocked('08E list sha256 differs from the registered value')
    registered = Path(args.protocol).read_text()
    protocol.update(list_sha256_in_addendum=args.list_sha256 in registered,
                    budget_in_addendum=args.budget_usd in registered)
    decision, why = V.read_effort_decision()
    if not decision:
        raise P.ExperimentBlocked('08C effort decision unavailable: ' + why)
    V.STATE['classifier_effort'] = decision
    handle = open(E8.RESULTS / 'classification.lock', 'a')
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    async def go():
        config, binding = P.production_guard(P.CONFIG, require_classifier=True)
        if config['classification']['reasoning'] != decision['effort']:
            raise P.ExperimentBlocked('08E effort differs from the 08C decision')
        ledger = SQLitePhaseLedger('openrouter', 'production_classification')
        if ledger.approved['evaluation_config_sha256'] != binding['config_sha256']:
            raise P.ExperimentBlocked('SQLite authority and production configuration differ')
        adapted = P.classifier_routes(config, json.loads(Path(config['routing_config']).read_text()))
        reference = json.loads(Path('results/production_v21_uncapped/classifier_routes.json').read_text())
        if adapted != reference:
            raise P.ExperimentBlocked('08E classifier routes differ from the 08D routes')
        target = E8.RESULTS / 'classifier_routes.json'
        P.write_json(target, adapted)
        concurrency = STAGE_CONCURRENCY['classify']
        client = V.V21Client('production_classification', routes_path=str(target), ledger=ledger,
                             concurrency=concurrency)
        stop = asyncio.Event()
        exporter = asyncio.create_task(export_loop(ledger, stop))
        try:
            return await E8.classify_list(list_path=Path(args.list), budget=args.budget_usd,
                                          stop_at_text=args.stop_admitting_at, protocol=protocol,
                                          concurrency=concurrency, client=client)
        finally:
            stop.set()
            await asyncio.gather(exporter, return_exceptions=True)
            try:
                ledger.export_json()
            except Exception:  # noqa: BLE001
                pass
            ledger.close()

    asyncio.run(go())
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['all', 'prefetch', 'check', 'classify-gated', 'uncapped', 'e08'])
    parser.add_argument('--list', help='e08: ordered 08E pair list (jsonl, frame format with order_08e)')
    parser.add_argument('--budget-usd', type=str, help='e08: registered budget B in USD')
    parser.add_argument('--stop-admitting-at', help='e08: registered time cutoff, ISO 8601 with UTC offset')
    parser.add_argument('--list-sha256', help='e08: sha256 of the list as registered in the addendum')
    parser.add_argument('--protocol', default='PROTOCOL_ADDENDUM_08E_NEW_PAIRING.md', help='e08: registered addendum')
    args = parser.parse_args()
    raise_fd_limit()
    if args.action == 'prefetch':
        prefetch()
        return 0
    V.install()
    if args.action == 'e08':
        if not args.list or not args.budget_usd or not args.list_sha256:
            parser.error('e08 needs --list, --list-sha256 and --budget-usd')
        try:
            return e08(args)
        except BaseException as exc:
            V.status(state='stopped', error_type=type(exc).__name__, error=str(exc)[:300])
            raise
    if args.action == 'uncapped':
        try:
            return uncapped()
        except BaseException as exc:
            V.status(state='stopped', error_type=type(exc).__name__, error=str(exc)[:300])
            raise
    if args.action == 'classify-gated':
        try:
            return classify_gated()
        except BaseException as exc:
            V.status(state='stopped', error_type=type(exc).__name__, error=str(exc)[:300])
            raise
    if args.action == 'check':
        print(json.dumps({'proofs': V.STATE['proofs'], 'v21_binding': V.STATE['v21_binding']}, indent=1))
        return 0
    try:
        return run_all()
    except BaseException as exc:
        V.status(state='stopped', error_type=type(exc).__name__, error=str(exc)[:300])
        raise


if __name__ == '__main__':
    sys.exit(main())
