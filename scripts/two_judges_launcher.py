"""Launcher equivalent to `scripts/run_reference_batches.py pilot|run --allow-inference`.

The registered script has a dispatch bug: a function-local import of TwoJudgeRun in its
`plan` branch makes the name local to main(), so `pilot` and `run` crash with
UnboundLocalError before any request. The script is hash-bound by the signed
configuration, so it is left unchanged; this launcher calls the same bound
functions (load_two_judge_plan, TwoJudgeRun.drive) exactly as main() intends.
"""
import concurrent.futures, json, sys
from chl_rag import two_judge_plan as amendment08
from chl_rag.reference_batches import TwoJudgeRun

phase = {'pilot': 'pilot', 'run': 'full'}[sys.argv[1]]
plans = {name: amendment08.load_two_judge_plan(name, config_path=amendment08.CONFIG) for name in ('astra', 'fable')}
with concurrent.futures.ThreadPoolExecutor(max_workers=len(plans)) as pool:
    futures = {pool.submit(TwoJudgeRun(plan).drive, phase, allow_inference=True): name for name, plan in plans.items()}
    manifests = {futures[f]: f.result() for f in concurrent.futures.as_completed(futures)}
print(json.dumps({name: {key: manifest[key] for key in ('phase', 'status', 'finalized', 'valid', 'fatal', 'open_item_ids')}
                  for name, manifest in manifests.items()}, indent=1))
