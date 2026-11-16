"""Two-judge final references: pilot, per-item batch run with post-cutoff fallback, local status.

    plan    [--judges astra,fable]                     offline: load, validate every request, no network
    status  [--judges astra,fable]                     local files only, no network
    pilot   --allow-inference [--judges astra,fable]   synchronous full price, registered 2 pairs
    run     --allow-inference [--judges astra,fable]   per-item batches until the cutoff, then fallback

Every command is resumable: state lives under each judge's output directory and
a restart never resubmits an item with a result or an open dispatch. Nothing is
sent without the signed two-judge configuration, the release lock and, for
Fable, the signed lift of the Anthropic stop.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys

from chl_rag import two_judge_plan as amendment08
from chl_rag.reference_batches import TwoJudgeRun


def status(plan):
    run = TwoJudgeRun(plan)
    rows = {}
    for item_id in run.phase_ids('full'):
        current = run.state(item_id)
        rows[item_id] = ('final:' + json.loads((run.item_dir(item_id) / 'result.json').read_text())['status']
                         if current['final'] else 'open' if current['open'] else
                         f"attempts={len(current['counted'])}")
    return {'judge': plan.judge, 'pilot_ids': list(plan.pilot_ids), 'items': rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('status', 'plan', 'pilot', 'run'))
    parser.add_argument('--judges', default='astra,fable')
    parser.add_argument('--config', default=None)
    parser.add_argument('--allow-inference', action='store_true')
    args = parser.parse_args(argv)
    judges = [name for name in args.judges.split(',') if name]
    if not judges or any(name not in ('astra', 'fable') for name in judges):
        parser.error('judges must be astra and/or fable')
    plans = {name: amendment08.load_two_judge_plan(name, config_path=args.config or amendment08.CONFIG) for name in judges}
    if args.command == 'plan':
        # Offline verification: every live request is built and validated; no key is read, nothing is sent.
        from chl_rag.reference_batches import (
            TwoJudgeRun,
            astra_body,
            fable_params,
            params_fingerprint,
        )
        report = {}
        for name, plan in plans.items():
            run = TwoJudgeRun(plan)
            rows = []
            for item_id in run.phase_ids('full'):
                reference = run.reference(item_id)
                body = astra_body(reference, plan.settings) if name == 'astra' else fable_params(reference, plan.settings)
                rows.append({'item_id': item_id, 'params_fingerprint': params_fingerprint(name, reference, plan.settings)[:16],
                             'request_bytes': len(json.dumps(body, ensure_ascii=False).encode())})
            report[name] = {'items': len(rows), 'pilot_ids': list(plan.pilot_ids), 'cutoff_at': plan.settings['cutoff_at'],
                            'effort': plan.settings['effort'], 'output_cap': plan.settings['max_tokens'],
                            'binding': plan.binding, 'requests': rows}
        print(json.dumps(report, indent=1))
        return 0
    if args.command == 'status':
        print(json.dumps([status(plan) for plan in plans.values()], indent=1))
        return 0
    if not args.allow_inference:
        parser.error('pilot and run send paid requests: pass --allow-inference explicitly')
    phase = 'pilot' if args.command == 'pilot' else 'full'
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(plans)) as pool:
        futures = {pool.submit(TwoJudgeRun(plan).drive, phase, allow_inference=True): name for name, plan in plans.items()}
        manifests = {futures[f]: f.result() for f in concurrent.futures.as_completed(futures)}
    print(json.dumps({name: {key: manifest[key] for key in ('phase', 'status', 'finalized', 'valid', 'fatal', 'open_item_ids')}
                      for name, manifest in manifests.items()}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
