"""Signed loader, revalidation, release lock and funding for the amendment-08 two-judge run.

Scope: the 35 pairs live on the site (the v2.1 handoff minus the retired duplicate),
site instrument 61c08a1, both judges at max effort. Independent of the amendment-06
reference-revision machinery: every input is bound by path and SHA-256 in
configs/evaluation_two_judges.json, which must be identical to a verifiably signed
commit. No network call happens here.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from . import reference_oracles as refs
from .io import digest, file_sha
from .spend_budget import PhaseLedger, money
from .model_runs import ExperimentBlocked, _signed_file
from .judges import require_active_judge
from .two_judges import TwoJudgePolicyError, is_two_judge, pilot_pair_ids, two_judge_settings

CONFIG = Path('configs/evaluation_two_judges.json')
LOADER = 'amendment08_two_judge_plan'
# Every file that shapes a request, its validation or its accounting.
RUNTIME = ('src/chl_rag/two_judge_plan.py', 'src/chl_rag/two_judges.py', 'src/chl_rag/reference_batches.py',
           'src/chl_rag/reference_oracles.py', 'src/chl_rag/judges.py', 'src/chl_rag/spend_budget.py',
           'src/chl_rag/site_instrument.py', 'src/chl_rag/pair_instrument.py',
           'src/chl_rag/site_presentation.py', 'src/chl_rag/annotation_names_study_start.py',
           'src/chl_rag/claim_schema.py', 'src/chl_rag/extraction.py', 'src/chl_rag/source_text.py', 'src/chl_rag/io.py',
           'configs/instrument.json', 'configs/oracle_prompt.json',
           'configs/site_instrument.json', 'configs/site_guidelines_en.md',
           'scripts/run_reference_batches.py')


def _json(path):
    return json.loads(Path(path).read_text())


def _aware(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _bound(path, expected, what):
    if not isinstance(path, str) or not isinstance(expected, str) or file_sha(path) != expected:
        raise ExperimentBlocked(f'{what} is not the registered file: {path}')
    _signed_file(path)
    if file_sha(path) != expected:
        raise ExperimentBlocked(f'{what} changed during signature verification')


def _lift(config, config_sha):
    """Antonio's signed lift of the 00:50 stop, for this run only (exact config hash)."""
    path = config.get('anthropic_stop_lift_path')
    if not isinstance(path, str) or not Path(path).is_file():
        return None
    record = _json(path)
    authority = record.get('authority')
    if (record.get('status') != 'lifted' or record.get('scope') != 'amendment_08_two_judge_run_only'
            or record.get('evaluation_config_sha256') != config_sha or _aware(record.get('lifted_at')) is None
            or not isinstance(authority, dict) or authority.get('actor') != 'Antonio'
            or authority.get('kind') != 'explicit_lift'
            or not all(isinstance(authority.get(key), str) and authority[key].strip() for key in ('verbatim', 'source'))):
        return None
    before = file_sha(path)
    _signed_file(path)
    if file_sha(path) != before:
        raise ExperimentBlocked('Anthropic lift changed during signature verification')
    return {'status': 'lifted', 'lifted_at': record['lifted_at'], 'authority': deepcopy(authority),
            'path': path, 'sha256': before}


def config_view(config_path=CONFIG):
    """The signed configuration, with a verified lift record placed under ``anthropic_stop``."""
    config_path = Path(config_path)
    _signed_file(config_path)
    config = _json(config_path)
    config_sha = file_sha(config_path)
    if (config.get('status') != 'registered' or config.get('loader') != LOADER
            or config.get('final_references_authorized') is not True or not is_two_judge(config)):
        raise ExperimentBlocked('Amendment-08 two-judge configuration is not registered')
    config.pop('anthropic_stop', None)  # only the separate signed lift can enable Fable
    lift = _lift(config, config_sha)
    if lift:
        config['anthropic_stop'] = lift
    return config, config_sha


def live_items(config):
    package = config['package']
    _bound(package['items_path'], package['items_sha256'], 'Handoff package')
    _bound(package['manifest_path'], package['manifest_sha256'], 'Handoff manifest')
    manifest = _json(package['manifest_path'])
    items = [json.loads(line) for line in Path(package['items_path']).read_text().splitlines() if line.strip()]
    if (manifest.get('package_sha256') != package['package_sha256'] or digest(items) != package['package_sha256']
            or manifest.get('item_count') != len(items)):
        raise ExperimentBlocked('Handoff package bytes or count differ from its manifest')
    retired = set(package['retired_item_ids'])
    live = [row for row in items if row['item_id'] not in retired]
    ids = [row['item_id'] for row in live]
    if (not retired <= {row['item_id'] for row in items} or ids != package['live_item_ids']
            or digest(live) != package['live_package_sha256'] or len(set(ids)) != len(ids)
            or any(row.get('task') != 'pair' for row in live)):
        raise ExperimentBlocked('Live pair set differs from registration')
    for row in live:
        refs.public_item(row)
    return live


def load_two_judge_plan(judge, *, config_path=CONFIG, effort='max'):
    if judge == 'fable':
        try:
            declared = _json(config_path)
        except (OSError, ValueError):
            declared = None
        lift_path = declared.get('anthropic_stop_lift_path') if isinstance(declared, dict) else None
        if not (isinstance(lift_path, str) and Path(lift_path).is_file()):
            require_active_judge('fable')  # raises: no lift record, no key read, no package read
    config, config_sha = config_view(config_path)
    require_active_judge(judge, config)
    try:
        settings = two_judge_settings(config, judge, effort)
    except TwoJudgePolicyError as exc:
        raise ExperimentBlocked(str(exc)) from exc
    files = {str(Path(config_path).resolve()): config_sha}
    _bound(config['protocol_path'], config['protocol_sha256'], 'Amendment 08')
    _bound(config['pair_binding_record_path'], config['pair_binding_record_sha256'], 'Pair binding record')
    files[str(Path(config['protocol_path']).resolve())] = config['protocol_sha256']
    files[str(Path(config['pair_binding_record_path']).resolve())] = config['pair_binding_record_sha256']
    runtime = config.get('reference_runtime_sha256', {})
    if set(runtime) != set(RUNTIME):
        raise ExperimentBlocked('Registered runtime list differs from the loader')
    for path in RUNTIME:
        _bound(path, runtime[path], 'Runtime file')
        files[str(Path(path).resolve())] = runtime[path]
    record = _json(config['pair_binding_record_path'])
    for path, expected in record.get('files_sha256', {}).items():
        if path.startswith(('configs/', 'src/')) and file_sha(path) != expected:
            raise ExperimentBlocked('61c08a1 binding record does not match its files: ' + path)
    items = live_items(config)
    for path_key, sha_key in (('items_path', 'items_sha256'), ('manifest_path', 'manifest_sha256')):
        files[str(Path(config['package'][path_key]).resolve())] = config['package'][sha_key]
    order = sorted(row['item_id'] for row in items)
    pilot = pilot_pair_ids(items, salt=settings['pilot_salt'], count=settings['pilot_pairs'])
    if list(pilot) != config['pilot_item_ids']:
        raise ExperimentBlocked('Seeded pilot differs from the registered pilot pairs')
    _funding(config, judge)
    binding = {'loader': LOADER, 'evaluation_config_sha256': config_sha,
               'protocol_sha256': config['protocol_sha256'],
               'pair_binding_record_sha256': config['pair_binding_record_sha256'],
               'package_sha256': config['package']['package_sha256'],
               'live_package_sha256': config['package']['live_package_sha256'],
               'retired_item_ids': list(config['package']['retired_item_ids']),
               'reference_revision': {'pair_binding': settings['pair_binding']}}
    output = Path(config['output_root']) / judge / effort
    return refs.ReferencePlan('final_evaluation', judge, effort, str(Path(config_path).resolve()), tuple(deepcopy(items)),
                              tuple(row['item_id'] for row in items), tuple(order), tuple(pilot), str(output.resolve()),
                              config['package']['live_package_sha256'], config['package']['package_sha256'],
                              binding, files, settings, {}, refs._AUTHORIZATION_TOKEN, {})


def _funding(config, judge):
    path = config['funding_path']
    _bound(path, config['funding_sha256'], 'Two-judge funding')
    funding = _json(path)
    vendor = 'openai' if judge == 'astra' else 'anthropic'
    flag = 'final_astra_openai_funding_confirmed' if judge == 'astra' else 'final_fable_topup_confirmed'
    try:
        allocation = money(funding['vendors'][vendor]['phase_alerts']['final_reference'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExperimentBlocked('Two-judge funding lacks a final_reference allocation') from exc
    if funding.get('status') != 'confirmed' or not funding.get('authority') or funding.get(flag) is not True or allocation <= 0:
        raise ExperimentBlocked('Two-judge funding is not confirmed for ' + judge)
    return funding


def revalidate(plan):
    if plan._authorization_token is not refs._AUTHORIZATION_TOKEN or plan.binding.get('loader') != LOADER:
        raise ExperimentBlocked('Plans must come from the amendment-08 loader')
    for path, expected in plan.files.items():
        if file_sha(path) != expected:
            raise ExperimentBlocked('Frozen two-judge input changed: ' + path)
    config, config_sha = config_view(plan.config_path)
    if config_sha != plan.binding['evaluation_config_sha256']:
        raise ExperimentBlocked('Two-judge configuration changed')
    require_active_judge(plan.judge, config)
    if two_judge_settings(config, plan.judge, plan.effort) != plan.settings:
        raise ExperimentBlocked('Two-judge settings changed since the plan was authorized')
    if digest(list(plan.items)) != plan.package_sha256:
        raise ExperimentBlocked('In-memory live package changed')
    _funding(config, plan.judge)
    return config


def release_guard(plan, *, item_id=None, phase=None):
    """Antonio's explicit go, bound to the exact two-judge configuration hash."""
    config, config_sha = config_view(plan.config_path)
    path = config.get('oracle_release_lock_path')
    if not isinstance(path, str) or not Path(path).is_file():
        raise ExperimentBlocked('Two-judge run is on HOLD until Antonio explicitly releases it')
    before = file_sha(path)
    release = _json(path)
    authority = release.get('authority')
    if (release.get('status') != 'released' or release.get('evaluation_config_sha256') != config_sha
            or release.get('protocol_sha256') != config['protocol_sha256']
            or not isinstance(authority, dict) or authority.get('actor') != 'Antonio'
            or authority.get('kind') != 'explicit_go'
            or not all(isinstance(authority.get(key), str) and authority[key].strip() for key in ('verbatim', 'source'))
            or _aware(release.get('released_at')) is None):
        raise ExperimentBlocked('Two-judge release needs Antonio explicit go bound to this exact configuration')
    scope = release.get('scope')
    if scope not in {'pilot_only', 'full_run'}:
        raise ExperimentBlocked('Release requires an explicit pilot_only or full_run scope')
    if scope == 'pilot_only' and (phase == 'full' or (item_id is not None and item_id not in plan.pilot_ids)):
        raise ExperimentBlocked('Antonio released the registered pilot pairs only')
    if item_id is not None and item_id not in plan.expected_ids:
        raise ExperimentBlocked('Item is outside the live package')
    commit = _signed_file(path)
    if file_sha(path) != before:
        raise ExperimentBlocked('Release lock changed during signature verification')
    return {'path': path, 'sha256': before, 'signed_commit': commit, 'scope': scope,
            'evaluation_config_sha256': config_sha, 'authority': deepcopy(authority), 'released_at': release['released_at']}


def ledger(plan):
    config = _json(plan.config_path)
    vendor = 'openai' if plan.judge == 'astra' else 'anthropic'
    return PhaseLedger(vendor, 'final_reference', funding_path=config['funding_path'])
