"""Guarded v2 development-pair references and final oracle runs.

No model call, draw or package read occurs on import. Classifier development and
final evaluation have distinct signed authorization gates and accounting phases.
Participant answers are never inputs. Provider batches (OpenAI Batch, Anthropic Message
Batches) are used only under the signed two-judge policy, by reference_batches.
"""
from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import json
import math
import random
import re
import statistics
import time
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator, ValidationError

from .io import digest, file_sha, read_jsonl, utcnow, write_json, write_jsonl
from .annotation_names_study_start import model_input_view, restore_model_input
from .spend_budget import PhaseLedger, money
from .model_runs import ExperimentBlocked, _signed_file
from .instrument_study_start import annotation_from_response, oracle_request
from .judges import (
    FABLE_ENDPOINT,
    FABLE_MAX_TOKENS,
    FABLE_MODEL,
    _isolation_module,
    _read_key,
    _safe_http_error,
    parse_stream,
    require_active_judge,
    usage_cost,
    wire_schema,
    worst_case_cost,
)
from .reference_revision import (
    effective_reference_config,
    reference_provenance,
    reference_revision_files,
    require_reference_provenance,
)
from .claim_schema import PACKAGE_ITEM
from .two_judges import (
    BATCH_TRANSPORTS,
    TwoJudgePolicyError,
    is_two_judge,
    pilot_pair_ids,
    two_judge_settings,
)

ROOT = Path(__file__).resolve().parents[2]
DEV_CONFIG = Path('configs/classifier_development.json')
FINAL_CONFIG = Path('configs/evaluation_system.json')
FINAL_PACKAGE = Path('annotation/package/items.jsonl')
FINAL_SUBSET = Path('annotation/human_subset/items.jsonl')
FINAL_MANIFEST = Path('annotation/package/manifest.json')
FUNDING = Path('configs/funding.json')
MODELS = {'astra': 'gpt-6-astra', 'fable': FABLE_MODEL}
STAGES = {'classifier_development', 'final_evaluation'}
_AUTHORIZATION_TOKEN = object()
PRIVATE_KEYS = {'session_pseudonym', 'participant', 'participant_id', 'first_name', 'last_name',
                'full_name', 'payment_code', 'paid', 'quality_failed', 'read_id', 'human_labels',
                'student_answers', 'antonio_labels', 'annotator', 'annotator_type', 'path', 'rationale'}
RUNTIME = ('src/chl_rag/reference_oracles.py', 'src/chl_rag/instrument_original.py',
           'src/chl_rag/pair_instrument_original.py', 'src/chl_rag/judges.py', 'scripts/oracle_isolation.py',
           'src/chl_rag/extraction.py', 'src/chl_rag/source_text.py', 'src/chl_rag/claim_schema.py',
           'configs/instrument_original.json', 'configs/oracle_prompt_original.json',
           'configs/source_policy.json',
           'configs/pair_annotation_schema_original.json', 'configs/extraction_annotation_schema_original.json',
           'configs/site_instrument_first_site.json', 'configs/site_guidelines_en_first_site.md',
           'src/chl_rag/extraction_comparison_original.py', 'configs/extraction_comparison_prompt_original.json',
           'src/chl_rag/prototype_baseline.py', 'configs/prototype_extraction_prompt.json',
           'src/chl_rag/frozen_compat.py', 'src/chl_rag/reference_revision.py',
           'src/chl_rag/annotation_names_study_start.py', 'src/chl_rag/pair_instrument_study_start.py',
           'src/chl_rag/instrument_study_start.py', 'src/chl_rag/extraction_comparison_study_start.py',
           'configs/instrument_study_start.json', 'configs/oracle_prompt_study_start.json',
           'configs/pair_annotation_schema_study_start.json', 'configs/extraction_annotation_schema_study_start.json',
           'configs/site_instrument_study_start.json', 'configs/site_guidelines_en_study_start.md',
           'configs/extraction_comparison_prompt_study_start.json',
           'src/chl_rag/pair_instrument_first_site.py',
           'src/chl_rag/instrument_first_site.py',
           'src/chl_rag/extraction_comparison_first_site.py',
           'configs/instrument_first_site.json', 'configs/oracle_prompt_first_site.json',
           'configs/pair_annotation_schema_first_site.json',
           'configs/extraction_annotation_schema_first_site.json',
           'configs/extraction_comparison_prompt_first_site.json',
           # Final two-judge run: pair binding to site 61c08a1 and the provider transports.
           'src/chl_rag/site_instrument.py', 'src/chl_rag/pair_instrument.py',
           'src/chl_rag/site_presentation.py', 'configs/instrument.json',
           'configs/oracle_prompt.json', 'configs/site_instrument.json',
           'configs/site_guidelines_en.md', 'src/chl_rag/two_judges.py',
           'src/chl_rag/reference_batches.py')
PAIR_BINDINGS = ('5208dfa', '61c08a1')
WIRE_61C08A1 = 'v2-pair-required-null-61c08a1'


def _json(path):
    return json.loads(Path(path).read_text())


def _sha_text(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('Reference timestamps need an explicit timezone')
    return result


def _reject_participant_data(value):
    if isinstance(value, dict):
        if PRIVATE_KEYS & set(value):
            raise ValueError('Participant/annotation data are prohibited as model inputs')
        for child in value.values():
            _reject_participant_data(child)
    elif isinstance(value, list):
        for child in value:
            _reject_participant_data(child)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Nonfinite values cannot enter a public reference item')


def public_item(item):
    """Strict public schema: private predictions cannot be silently transmitted."""
    _reject_participant_data(item)
    Draft202012Validator(PACKAGE_ITEM).validate(item)
    if item['task'] == 'pair' and item['a']['article']['pmid'] == item['b']['article']['pmid']:
        raise ValueError('A pair reference requires two distinct source articles')
    return deepcopy(item)


def _source_ids(item):
    return ({item['a']['article']['pmid'], item['b']['article']['pmid']} if item['task'] == 'pair'
            else {item['article']['pmid']})


def prepare_reference(item, *, baseline=None, binding=None):
    """binding '61c08a1' is the final pair binding; None/'5208dfa' serves historical items."""
    if binding == '61c08a1':
        if baseline is not None:
            raise ValueError('The 61c08a1 binding is pair-only and takes no baseline')
        from .site_instrument import prepare_reference as prepare_61c08a1
        return prepare_61c08a1(item)
    if binding not in (None, '5208dfa'):
        raise ValueError('Unknown pair binding')
    item = public_item(item)
    if baseline is not None:
        if item['task'] != 'extraction':
            raise ValueError('The private baseline is only for a sampled extraction abstract')
        _reject_participant_data(baseline)
        from .extraction_comparison_study_start import prepare
        request = prepare(item, baseline)
    else:
        request = oracle_request(item)
        if restore_model_input(json.loads(request['prompt']).get('item')) != item:
            raise ValueError('Oracle presentation changed the frozen public item')
    return {**request, 'item': item, 'baseline': deepcopy(baseline),
            'wire_schema': wire_schema(request['schema']),
            'raw_public_item_sha256': digest(item), 'displayed_public_item_sha256': digest(model_input_view(item)),
            'model_payload_sha256': _sha_text(request['prompt']),
            'instructions_sha256': _sha_text(request['instructions']),
            'canonical_schema_sha256': digest(request['schema']),
            'wire_schema_sha256': digest(wire_schema(request['schema'])),
            'classifier_outputs_in_prompt': False, 'peer_outputs_in_prompt': False,
            'human_answers_in_prompt': False}


def annotation_from_reference_response(response, item, baseline=None, *, binding=None):
    if binding == '61c08a1':
        if baseline is not None:
            raise ValueError('The 61c08a1 binding is pair-only and takes no baseline')
        from .site_instrument import annotation_from_reference_response as annotate_61c08a1
        return annotate_61c08a1(response, item)
    if binding not in (None, '5208dfa'):
        raise ValueError('Unknown pair binding')
    if baseline is None:
        return annotation_from_response(response, item)
    from .extraction_comparison_study_start import normalize
    return normalize(response, item, baseline)


def prepare_final_baseline_bundle(config_path=FINAL_CONFIG):
    """Freeze a private exact-C companion after sampling, before any oracle output."""
    from .prototype_baseline import load_final_d0
    config_path = Path(config_path)
    _signed_file(config_path)
    config = effective_reference_config(config_path)
    _signed_file(FINAL_PACKAGE); _signed_file(FINAL_MANIFEST)
    manifest = _json(FINAL_MANIFEST)
    require_reference_provenance(manifest, config)
    items = list(read_jsonl(FINAL_PACKAGE))
    pmids = [item['article']['pmid'] for item in items if item['task'] == 'extraction']
    path = Path(config.get('extraction_comparison', {}).get('bundle_path',
                'annotation/private/d0_baselines.json'))
    if path.exists():
        existing = _json(path)
        require_reference_provenance(existing,config)
        if existing.get('package_sha256') != digest(items) or existing.get('package_manifest_sha256') != file_sha(FINAL_MANIFEST) or existing.get('evaluation_config_sha256') != file_sha(config_path):
            raise ExperimentBlocked('Existing D0 companion belongs to another frozen package')
        return path
    if any(Path('annotation/oracles').glob('**/items/*/result.json')):
        raise ExperimentBlocked('Private D0 companion must be frozen before oracle outputs')
    baselines = load_final_d0(pmids, evaluation_config_path=str(config_path))
    write_json(path, {'version': 'final-d0-companion-1', 'baseline': 'saved_v1_retained',
                     'package_sha256': digest(items), 'package_manifest_sha256': file_sha(FINAL_MANIFEST),
                     'evaluation_config_sha256': file_sha(config_path),
                     'frozen_before_oracle_outputs': True, 'baselines': baselines,
                     'reference_revision': reference_provenance(config)})
    return path


def load_final_baselines(config, package, manifest, *, config_path=FINAL_CONFIG, manifest_path=FINAL_MANIFEST):
    """Validate the signed private companion without changing the public package."""
    if config.get('extraction_comparison', {}).get('baseline') != 'saved_v1_retained':
        raise ExperimentBlocked('Final extraction calls require the registered retained v1 comparison')
    path = Path(config.get('extraction_comparison', {}).get('bundle_path',
                'annotation/private/d0_baselines.json'))
    _signed_file(path)
    companion = _json(path)
    require_reference_provenance(manifest,config)
    require_reference_provenance(companion,config)
    expected = {row['article']['pmid'] for row in package if row['task'] == 'extraction'}
    if (companion.get('version') != 'final-d0-companion-1'
            or companion.get('baseline') != 'saved_v1_retained'
            or companion.get('package_sha256') != manifest['package_sha256']
            or companion.get('package_sha256') != digest(package)
            or companion.get('package_manifest_sha256') != file_sha(manifest_path)
            or companion.get('evaluation_config_sha256') != file_sha(config_path)
            or companion.get('frozen_before_oracle_outputs') is not True
            or set(companion.get('baselines', {})) != expected):
        raise ExperimentBlocked('D0 companion is not bound to all frozen C abstracts before outputs')
    baselines = companion['baselines']
    for row in package:
        if row['task'] == 'extraction':
            prepare_reference(row, baseline=baselines[row['article']['pmid']])
    return deepcopy(baselines)


def final_order(items, seed=20261123):
    """Apply the registered order only to an already frozen sample; no sample draw."""
    if seed != 20261123:
        raise ValueError('Final oracle order seed differs from registration')
    pairs = [row['item_id'] for row in items if row['task'] == 'pair']
    abstracts = [row['item_id'] for row in items if row['task'] == 'extraction']
    if len(pairs) < 3 or len(abstracts) < 2:
        raise ExperimentBlocked('A five-item pilot requires three sampled pairs and two sampled abstracts')
    rng = random.Random(seed)
    rng.shuffle(pairs); rng.shuffle(abstracts)
    pilot = pairs[:3] + abstracts[:2]
    remaining = pairs[3:] + abstracts[2:]
    rng.shuffle(remaining)
    return pilot, pilot + remaining


def _runtime_binding(config):
    declared = config.get('reference_runtime_sha256', config.get('source_sha256', {}))
    result = {}
    for path in RUNTIME:
        if declared.get(path) != file_sha(ROOT / path):
            raise ExperimentBlocked('Reference runtime/instrument was not frozen: ' + path)
        _signed_file(ROOT / path)
        result[str(ROOT / path)] = declared[path]
    return result


def _declared_policy(config_path):
    """Unsigned early read, only to let Fable reach the signed checks under a two-judge config."""
    try:
        value = _json(config_path)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _reference_settings(config, judge, stage, effort):
    if is_two_judge(config):
        require_active_judge(judge, config)
        try:
            return two_judge_settings(config, judge, effort)
        except TwoJudgePolicyError as exc:
            raise ExperimentBlocked(str(exc)) from exc
    require_active_judge(judge)
    if (config.get('active_judges') != ['astra'] or config.get('judge_policy') != 'astra_only'
            or config.get('reference_design') != 'single_astra_max'):
        raise ExperimentBlocked('Signed Astra-only reference policy is required')
    refs = config.get('references')
    if not isinstance(refs, dict) or refs.get('transport', 'isolated_codex') != 'isolated_codex':
        raise ExperimentBlocked('Registered isolated Codex reference settings are required; Message Batches is disabled')
    settings = refs.get(judge, {})
    if settings.get('model') != MODELS[judge] or settings.get('effort') != 'max':
        raise ExperimentBlocked('Reference model/maximum effort differs from registration')
    if refs.get('max_attempts') != 2 or refs.get('attempt_timeout_seconds') != 1800:
        raise ExperimentBlocked('References require one initial attempt and at most one identical retry, timeout1800s')
    limit = 4
    concurrency = settings.get('concurrency', limit if judge == 'astra' else 4)
    if type(concurrency) is not int or concurrency < 1 or concurrency > (limit if judge == 'astra' else 16):
        raise ExperimentBlocked('Reference concurrency exceeds the registered phase ceiling')
    max_tokens = settings.get('max_tokens', FABLE_MAX_TOKENS)
    if type(max_tokens) is not int or not 1 <= max_tokens <= FABLE_MAX_TOKENS:
        raise ExperimentBlocked('Invalid registered Fable output cap')
    if effort != 'max' or refs.get('pilot', {}).get('allow_whole_reference_xhigh_fallback') is True:
        raise ExperimentBlocked('Astra max is mandatory; no lower-effort fallback is authorized')
    return {**settings, 'concurrency': concurrency, 'max_tokens': max_tokens,
            'max_attempts': 2, 'attempt_timeout_seconds': 1800, 'effort': effort}


def _final_openai_gate(config):
    """Astra over the OpenAI API spends real money: a confirmed OpenAI allocation is required."""
    if not is_two_judge(config):
        raise ExperimentBlocked('Astra over the OpenAI API requires the signed two-judge policy')
    if not FUNDING.exists():
        raise ExperimentBlocked('Final Astra API waits for a confirmed OpenAI funding entry')
    funding = _json(FUNDING)
    if (funding.get('status') != 'confirmed' or not funding.get('authority')
            or funding.get('final_astra_openai_funding_confirmed') is not True):
        raise ExperimentBlocked('Final Astra API waits for a confirmed OpenAI funding entry')
    try:
        phase_capacity = money(funding.get('vendors', {}).get('openai', {}).get('phase_alerts', {}).get('final_reference', 0))
    except (ValueError, TypeError) as exc:
        raise ExperimentBlocked('Final Astra API requires a valid positive final_reference allocation') from exc
    if phase_capacity <= 0:
        raise ExperimentBlocked('Final Astra API requires a positive final_reference allocation')
    _signed_file(FUNDING)
    return file_sha(FUNDING)


def _final_fable_gate(config):
    require_active_judge("fable", config)
    if not FUNDING.exists():
        raise ExperimentBlocked('Final Fable waits for a separate confirmed Anthropic top-up')
    funding = _json(FUNDING)
    if (funding.get('status') != 'confirmed' or not funding.get('authority')
            or funding.get('final_fable_topup_confirmed') is not True):
        raise ExperimentBlocked('Final Fable waits for a separate confirmed Anthropic top-up')
    try:
        phase_capacity = money(funding.get('vendors', {}).get('anthropic', {}).get('phase_alerts', {}).get('final_reference', 0))
    except (ValueError, TypeError) as exc:
        raise ExperimentBlocked('Final Fable requires a valid positive final_reference phase allocation') from exc
    if phase_capacity <= 0:
        raise ExperimentBlocked('Final Fable requires a positive final_reference phase allocation')
    _signed_file(FUNDING)
    return file_sha(FUNDING)


@dataclass(frozen=True)
class ReferencePlan:
    stage: str
    judge: str
    effort: str
    config_path: str
    items: tuple
    expected_ids: tuple
    order: tuple
    pilot_ids: tuple
    output_dir: str
    package_sha256: str
    frame_manifest_sha256: str
    binding: dict
    files: dict
    settings: dict
    pilot_policy: dict
    _authorization_token: object = field(default=None, repr=False, compare=False)
    baselines: dict = field(default_factory=dict, repr=False)


def load_plan(stage, judge, *, config_path=None, effort='max'):
    """Authorize before reading any future dev/final item package."""
    require_active_judge(judge, _declared_policy(Path(config_path or FINAL_CONFIG)) if judge == 'fable' else None)
    if stage != 'final_evaluation':
        raise ExperimentBlocked('The development pair experiment is retired under the final plan')
    if stage not in STAGES or judge not in MODELS:
        raise ValueError('Use the distinct classifier_development or final_evaluation stage, not P/S/H')
    config_path = Path(config_path or (DEV_CONFIG if stage == 'classifier_development' else FINAL_CONFIG))
    config = effective_reference_config(config_path)
    flag = 'development_references_authorized' if stage == 'classifier_development' else 'final_references_authorized'
    if config.get('status') != 'registered' or config.get(flag) is not True:
        raise ExperimentBlocked('This reference phase has no signed explicit authorization')
    _signed_file(config_path)
    require_active_judge(judge, config if is_two_judge(config) else None)
    settings = _reference_settings(config, judge, stage, effort)
    files = {str(config_path.resolve()): file_sha(config_path), **_runtime_binding(config),
             **reference_revision_files(config)}
    baselines = {}
    if judge == 'fable' and stage == 'final_evaluation':
        _final_fable_gate(config)
    if judge == 'astra' and stage == 'final_evaluation' and is_two_judge(config):
        _final_openai_gate(config)
    if stage == 'classifier_development':
        from .classifier_development import BASE, frozen_manifest
        manifest, items, _spec, _evaluation, _registry = frozen_manifest(config_path)
        manifest_path = BASE / 'manifest.json'
        items_path = Path(manifest['files']['items.jsonl']['path'])
        for path in (manifest_path, items_path):
            _signed_file(path); files[str(path.resolve())] = file_sha(path)
        if manifest.get('stage') != stage or manifest.get('labels_read') is not False or manifest.get('classifier_predictions_read') is not False:
            raise ExperimentBlocked('Development pair frame was not frozen before labels/predictions')
        allowed = set(manifest['development_pmids'])
        if any(row['task'] != 'pair' or not re.fullmatch(r'v2-pair-7\d{5}', row['item_id'])
               or not _source_ids(row) <= allowed for row in items):
            raise ExperimentBlocked('Development reference item is outside the frozen development source frame')
        expected = manifest['item_ids']; order = expected; pilot = []
        package_hash = manifest['package_sha256']
        frame_hash = file_sha(manifest_path)
        output = BASE / 'references' / judge
        binding = {'development_config_sha256': file_sha(config_path),
                   'frame_manifest_sha256': frame_hash, 'package_sha256': package_hash,
                   'selected_pipeline': manifest['binding']}
        policy = {}
    else:
        from .production import production_guard
        _production, pipeline = production_guard(config_path, require_classifier=True)
        for path in (FINAL_MANIFEST, FINAL_PACKAGE, FINAL_SUBSET):
            _signed_file(path); files[str(path.resolve())] = file_sha(path)
        manifest = _json(FINAL_MANIFEST)
        require_reference_provenance(manifest,config)
        if (manifest.get('pipeline_version') != '2.0' or manifest.get('instrument_version') != '2.0'
                or manifest.get('evaluation_config_sha256') != file_sha(config_path)
                or manifest.get('human_subset_frozen_before_oracle') is not True
                or manifest.get('labels_read') is not False):
            raise ExperimentBlocked('Final sample/subset is not frozen under amendment06')
        frame_path = Path(config['reference_frame_manifest_path'])
        _signed_file(frame_path); files[str(frame_path.resolve())] = file_sha(frame_path)
        frame = _json(frame_path)
        if (file_sha(frame_path) != manifest['frame_manifest_sha256'] or frame.get('status') != 'complete_frozen'
                or frame.get('binding') != pipeline
                or frame.get('classifier_binding') != _production.get('_classifier_binding')):
            raise ExperimentBlocked('Final package does not bind the complete production frame')
        items = list(read_jsonl(FINAL_PACKAGE)); human = list(read_jsonl(FINAL_SUBSET))
        if (digest(items) != manifest['package_sha256'] or digest(human) != manifest['human_package_sha256']
                or len(items) != manifest['full_items']):
            raise ExperimentBlocked('Final package/subset bytes or counts changed')
        full_index = {row['item_id']: row for row in items}
        if len({row['item_id'] for row in human}) != len(human) or any(full_index.get(row['item_id']) != row for row in human):
            raise ExperimentBlocked('Human subset is not an unchanged subset of the final public package')
        if any(not _source_ids(row) <= set(frame['eligible_reference_pmids']) for row in items):
            raise ExperimentBlocked('Final reference sources leave the frozen eligible frame')
        if any(not re.fullmatch(r'v2-(?:pair|extraction)-\d{3}', row['item_id']) for row in items):
            raise ExperimentBlocked('Development or old-version IDs cannot masquerade as final items')
        counts = Counter(row['task'] for row in items)
        if counts['pair'] != manifest['pairs'] or counts['extraction'] != manifest['abstracts']:
            raise ExperimentBlocked('Final task counts do not match the frozen package')
        human_counts = Counter(row['task'] for row in human)
        abstract_arms = Counter(row['arm'] for row in items if row['task'] == 'extraction')
        human_abstract_arms = Counter(row['arm'] for row in human if row['task'] == 'extraction')
        if (human_counts['pair'] != manifest.get('human_pairs')
                or human_counts['extraction'] != manifest.get('human_abstracts')
                or human_counts['pair'] != min(40, counts['pair'])
                or dict(human_abstract_arms) != {arm: min(2, count) for arm, count in abstract_arms.items()}):
            raise ExperimentBlocked('Human subset does not retain the registered 40-pair/two-abstracts-per-arm targets')
        if (len(items) != 145 and not (manifest.get('scarcity', {}).get('pairs_reduced') or
                                      manifest.get('scarcity', {}).get('abstracts_reduced'))):
            raise ExperimentBlocked('Reduced final sample lacks its registered scarcity record')
        pilot, order = final_order(items, manifest.get('seeds', {}).get('oracle_order'))
        if pilot != manifest['oracle_pilot_ids'] or order != manifest['oracle_order']:
            raise ExperimentBlocked('Pilot/order differs from the frozen seed20261123 rule')
        companion_path = Path(config.get('extraction_comparison', {}).get('bundle_path',
                              'annotation/private/d0_baselines.json'))
        if config.get('extraction_comparison', {}).get('baseline') != 'saved_v1_retained':
            raise ExperimentBlocked('Final extraction calls require the registered retained v1 comparison')
        _signed_file(companion_path); files[str(companion_path.resolve())] = file_sha(companion_path)
        baselines = load_final_baselines(config, items, manifest, config_path=config_path, manifest_path=FINAL_MANIFEST)
        expected = [row['item_id'] for row in items]
        package_hash = manifest['package_sha256']; frame_hash = manifest['frame_manifest_sha256']
        output = Path('annotation/oracles') / judge / effort
        binding = {'evaluation_config_sha256': file_sha(config_path), 'package_manifest_sha256': file_sha(FINAL_MANIFEST),
            'frame_manifest_sha256': frame_hash, 'package_sha256': package_hash,
            'human_package_sha256': manifest['human_package_sha256'], 'selected_pipeline': pipeline,
            'baseline_companion_path': str(companion_path), 'baseline_companion_sha256': file_sha(companion_path),
            'baselines_sha256': digest(baselines)}
        policy = config.get('references', {}).get('pilot', {})
        if is_two_judge(config):
            # Registered seeded two-pair pilot, identical for both judges.
            pilot = list(pilot_pair_ids(items, salt=settings['pilot_salt'], count=settings['pilot_pairs']))
        else:
            if (policy.get('seed') != 20261123 or policy.get('pairs') != 3 or policy.get('abstracts') != 2
                    or policy.get('minimum_valid') != 4 or policy.get('projection_margin') != 1.5):
                raise ExperimentBlocked('Final pilot policy is not the registered three-pair/two-abstract design')
            _timestamp(policy['deadline'])
        if effort == 'xhigh':
            maximum = Path('annotation/oracles/astra/max/pilot_decision.json')
            decision = _json(maximum)
            if (policy.get('allow_whole_reference_xhigh_fallback') is not True
                    or decision.get('decision') != 'restart_whole_reference_xhigh'
                    or decision.get('package_sha256') != package_hash):
                raise ExperimentBlocked('xhigh requires the recorded registered max-pilot fallback decision')
            files[str(maximum.resolve())] = file_sha(maximum)
    binding['reference_revision'] = reference_provenance(config)
    for item in items:
        public_item(item)
    ids = [row['item_id'] for row in items]
    if len(ids) != len(set(ids)) or ids != list(expected) or set(order) != set(ids) or len(order) != len(ids):
        raise ExperimentBlocked('Reference identities/order are not unique and complete')
    return ReferencePlan(stage, judge, effort, str(config_path.resolve()), tuple(deepcopy(items)),
        tuple(expected), tuple(order), tuple(pilot), str(output.resolve()), package_hash, frame_hash,
        binding, files, settings, policy, _AUTHORIZATION_TOKEN, baselines)


def revalidate(plan):
    if plan.effort != 'max':
        raise ExperimentBlocked('Astra max is mandatory')
    if plan._authorization_token is not _AUTHORIZATION_TOKEN:
        raise ExperimentBlocked('Reference plans must come from the signed authorization loader')
    if plan.stage not in STAGES or plan.judge not in MODELS:
        raise ExperimentBlocked('Unknown reference phase or judge')
    for path, expected in plan.files.items():
        if file_sha(path) != expected:
            raise ExperimentBlocked('Frozen reference authorization/input changed: ' + path)
    config = effective_reference_config(plan.config_path)
    require_active_judge(plan.judge, config if is_two_judge(config) else None)
    require_reference_provenance(plan.binding,config)
    flag = 'development_references_authorized' if plan.stage == 'classifier_development' else 'final_references_authorized'
    if config.get('status') != 'registered' or config.get(flag) is not True:
        raise ExperimentBlocked('Reference phase authorization is no longer active')
    if is_two_judge(config):
        try:
            current = two_judge_settings(config, plan.judge, plan.effort)
        except TwoJudgePolicyError as exc:
            raise ExperimentBlocked(str(exc)) from exc
        if current != plan.settings:
            raise ExperimentBlocked('Two-judge settings changed since the plan was authorized')
        if tuple(pilot_pair_ids(plan.items, salt=current['pilot_salt'], count=current['pilot_pairs'])) != tuple(plan.pilot_ids):
            raise ExperimentBlocked('Pilot pairs differ from the registered seeded rule')
    elif (config.get('active_judges') != ['astra'] or config.get('judge_policy') != 'astra_only'
            or config.get('reference_design') != 'single_astra_max'):
        raise ExperimentBlocked('Astra-only reference policy changed')
    if plan.stage == 'classifier_development' and config.get('stage') != plan.stage:
        raise ExperimentBlocked('S or final sources cannot masquerade as classifier development')
    if plan.baselines and digest(plan.baselines) != plan.binding.get('baselines_sha256'):
        raise ExperimentBlocked('In-memory D0 baseline companion changed')
    if digest(list(plan.items)) != plan.package_sha256:
        raise ExperimentBlocked('In-memory public package changed')
    if plan.judge == 'fable' and plan.stage == 'final_evaluation':
        _final_fable_gate(config)
    if plan.judge == 'astra' and plan.stage == 'final_evaluation' and is_two_judge(config):
        _final_openai_gate(config)


def _registered_pilot_ids(plan):
    """Recheck the exact signed seeded three-pair/two-abstract IDs at dispatch."""
    manifest_path = FINAL_MANIFEST.resolve()
    expected = plan.binding.get('package_manifest_sha256')
    if (not expected or plan.files.get(str(manifest_path)) != expected
            or file_sha(manifest_path) != expected):
        raise ExperimentBlocked('Pilot scope requires the unchanged signed package manifest')
    manifest = _json(manifest_path)
    pilot, order = final_order(plan.items)
    if (tuple(pilot) != plan.pilot_ids or tuple(order) != plan.order
            or manifest.get('oracle_pilot_ids') != pilot or manifest.get('oracle_order') != order
            or manifest.get('package_sha256') != plan.package_sha256):
        raise ExperimentBlocked('Pilot scope differs from the five registered seeded items')
    return set(pilot)


def oracle_release_guard(plan, *, item_id=None, phase=None):
    """Operational lock, separate from immutable science; never infer Antonio's go."""
    if plan.stage != 'final_evaluation':
        return None
    config = effective_reference_config(plan.config_path)
    configured = config.get('oracle_release_lock_path')
    if not isinstance(configured, str) or not configured:
        raise ExperimentBlocked('Final Astra is on HOLD: no registered oracle release lock path')
    path = Path(configured)
    if not path.is_file():
        raise ExperimentBlocked('Final Astra is on HOLD until Antonio explicitly releases it')
    before = file_sha(path)
    release = _json(path)
    authority = release.get('authority')
    if (release.get('status') != 'released'
            or release.get('evaluation_config_sha256') != file_sha(plan.config_path)
            or release.get('reference_revision_sha256') != reference_provenance(config)['reference_revision_sha256']
            or not isinstance(authority, dict) or authority.get('actor') != 'Antonio'
            or authority.get('kind') != 'explicit_go'
            or not isinstance(authority.get('verbatim'), str) or not authority['verbatim'].strip()
            or not isinstance(authority.get('source'), str) or not authority['source'].strip()):
        raise ExperimentBlocked('Final Astra release needs Antonio explicit go bound to this evaluation configuration')
    try:
        _timestamp(release['released_at'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExperimentBlocked('Final Astra release needs a timezone-aware released_at') from exc
    scope = release.get('scope')
    if scope not in {'pilot_only', 'full_run'}:
        raise ExperimentBlocked('Oracle release requires an explicit pilot_only or full_run scope')
    if phase is not None and phase not in {'pilot', 'full'}:
        raise ExperimentBlocked('Unknown reference execution phase')
    if scope == 'pilot_only':
        if is_two_judge(config):
            pilot_ids = set(pilot_pair_ids(plan.items))
            if pilot_ids != set(plan.pilot_ids):
                raise ExperimentBlocked('Pilot pairs differ from the registered seeded rule')
        else:
            pilot_ids = _registered_pilot_ids(plan)
        if phase == 'full' or (item_id is not None and item_id not in pilot_ids):
            raise ExperimentBlocked('Antonio authorized the registered pilot items only; full run remains on HOLD')
    if item_id is not None and item_id not in plan.expected_ids:
        raise ExperimentBlocked('Reference item is outside the signed package')
    commit = _signed_file(path)
    if file_sha(path) != before:
        raise ExperimentBlocked('Oracle release lock changed during signature verification')
    return {'path': str(path), 'sha256': before, 'signed_commit': commit,
            'evaluation_config_sha256': release['evaluation_config_sha256'],
            'reference_revision_sha256': release['reference_revision_sha256'],
            'authority': deepcopy(authority), 'released_at': release['released_at'], 'scope': scope}


def _ledger(plan, supplied=None):
    if plan.judge == 'astra':
        if plan.settings.get('transport') != 'openai_batch':
            return None
        ledger = supplied or PhaseLedger('openai', 'final_reference')
        if getattr(ledger, 'vendor', None) != 'openai' or getattr(ledger, 'phase', None) != 'final_reference':
            raise ExperimentBlocked('Astra API ledger must be the OpenAI final_reference phase')
        return ledger
    if plan.judge != 'fable':
        return None
    expected_phase = 'development_judging' if plan.stage == 'classifier_development' else 'final_reference'
    ledger = supplied or PhaseLedger('anthropic', expected_phase)
    if getattr(ledger, 'vendor', None) != 'anthropic' or getattr(ledger, 'phase', None) != expected_phase:
        raise ExperimentBlocked('Reference ledger vendor/phase mismatch; final Fable cannot use development funds')
    return ledger


def fable_request(reference, max_tokens):
    return {'model': FABLE_MODEL, 'max_tokens': max_tokens, 'stream': True,
        'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'max',
            'format': {'type': 'json_schema', 'schema': reference['wire_schema']}},
        'system': [{'type': 'text', 'text': reference['instructions'],
                    'cache_control': {'type': 'ephemeral', 'ttl': '5m'}}],
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': reference['prompt'],
                      'cache_control': {'type': 'ephemeral', 'ttl': '5m'}}]}]}


def _fable_once(plan, reference, directory, ledger, *, client=None, key_path=None, reservation=None,
                policy_config=None):
    require_active_judge("fable", policy_config)
    request = fable_request(reference, plan.settings['max_tokens'])
    write_json(directory / 'request.json', request)
    if reservation is None:
        reservation = ledger.reserve(worst_case_cost(request), digest(request), FABLE_MODEL,
            metadata={'stage': plan.stage, 'item_id': reference['item']['item_id']})
    try:
        key = _read_key(key_path or Path.home() / '.config/chl-anthropic.env', policy_config)
    except Exception:
        ledger.release(reservation, 'Credential validation failed before reference dispatch')
        raise
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60, connect=30), follow_redirects=False)
    events, error = [], None
    started = time.monotonic()
    try:
        with client.stream('POST', FABLE_ENDPOINT, json=request,
                headers={'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'}) as response:
            if response.status_code != 200:
                detail = _safe_http_error(response, key)
                free = response.status_code in {400, 401, 403, 404, 413, 422, 429}
                if free: ledger.release(reservation, f'HTTP{response.status_code} rejected before generation')
                else: ledger.uncertain(reservation, f'HTTP{response.status_code}; final usage unknown')
                return {'status': 'rate_limited' if response.status_code == 429 else 'http_error',
                    'text': '', 'schema_valid': False, 'http_status': response.status_code, **detail,
                    'cost_usd': 0 if free else None, 'retryable': response.status_code == 429,
                    'retry_after_seconds': 60 if response.status_code == 429 else 0,
                    'reservation_id': reservation}
            with (directory / 'events.jsonl').open('w') as stream:
                for line in response.iter_lines():
                    if time.monotonic() - started > plan.settings['attempt_timeout_seconds']:
                        raise TimeoutError('Reference stream exceeded registered wall time')
                    if not line.startswith('data:'): continue
                    raw = line[5:].strip()
                    if not raw or raw == '[DONE]': continue
                    event = json.loads(raw); events.append(event)
                    stream.write(json.dumps(event, ensure_ascii=False) + '\n'); stream.flush()
    except (httpx.HTTPError, ValueError, OSError) as exc:
        error = type(exc).__name__
    finally:
        key = None
        if own: client.close()
    parsed = parse_stream(events)
    (directory / 'raw_output.txt').write_text(parsed['text'])
    result = {**parsed, 'transport_error_type': error, 'reservation_id': reservation}
    if not parsed['complete'] or parsed['errors'] or error:
        ledger.uncertain(reservation, 'Incomplete reference stream; retain worst-case liability')
        return {**result, 'status': 'transport_incomplete', 'schema_valid': False, 'retryable': False, 'cost_usd': None}
    try:
        cost = usage_cost(parsed['usage'])
    except ValueError:
        ledger.uncertain(reservation, 'Complete reference stream lacks final usage')
        return {**result, 'status': 'usage_unknown', 'schema_valid': False, 'retryable': False, 'cost_usd': None}
    ledger.settle(reservation, cost, usage=parsed['usage'], metadata={'stage': plan.stage, 'stop_reason': parsed['stop_reason']})
    result['cost_usd'] = cost
    if parsed['model'] != FABLE_MODEL:
        return {**result, 'status': 'model_mismatch', 'schema_valid': False, 'retryable': False}
    if parsed['stop_reason'] == 'refusal':
        return {**result, 'status': 'refusal', 'schema_valid': False, 'retryable': False}
    if parsed['stop_reason'] != 'end_turn':
        return {**result, 'status': 'truncated' if parsed['stop_reason'] == 'max_tokens' else 'invalid_stop',
                'schema_valid': False, 'retryable': parsed['stop_reason'] == 'max_tokens'}
    return {**result, 'status': 'completed', 'retryable': True}


def _astra_once(plan, reference, directory):
    revalidate(plan)
    item_id = reference.get('item', {}).get('item_id')
    release = oracle_release_guard(plan, item_id=item_id)
    if not isinstance(item_id, str):
        raise ExperimentBlocked('Low-level reference dispatch requires a frozen item identity')
    item = next((row for row in plan.items if row['item_id'] == item_id), None)
    if item is None:
        raise ExperimentBlocked('Low-level reference item is outside the signed package')
    baseline = plan.baselines.get(item['article']['pmid']) if item['task'] == 'extraction' else None
    if reference != prepare_reference(item, baseline=baseline):
        raise ExperimentBlocked('Low-level reference request differs from its frozen item and instrument')
    limit = 4
    response = _isolation_module().run_isolated_codex(reference['prompt'], reference['wire_schema'],
        instructions_text=reference['instructions'], effort=plan.effort,
        timeout=plan.settings['attempt_timeout_seconds'], max_concurrency=limit, retain_timeout_output=True)
    (directory / 'events.jsonl').write_text(response.stdout)
    (directory / 'stderr.txt').write_text(response.stderr)
    (directory / 'raw_output.txt').write_text(response.output)
    tools, errors = [], 0
    for line in response.stdout.splitlines():
        if not line.strip(): continue
        try: event = json.loads(line)
        except ValueError: errors += 1; continue
        if (any(token in event.get('type', '') for token in ('tool_call', 'command_execution', 'mcp_tool'))
                or event.get('item', {}).get('type') not in {None, 'agent_message', 'reasoning'}):
            tools.append(event)
    result = {'text': response.output, 'oracle_release': release, 'model': response.metadata['model'], 'returncode': response.returncode,
              'isolation': response.metadata, 'codex_version': response.metadata['codex_version'],
              'tool_event_count': len(tools), 'event_parse_errors': errors, 'cost_usd': None}
    if tools or (errors and not response.returncode):
        return {**result, 'status': 'unexpected_events', 'schema_valid': False, 'retryable': False}
    if response.returncode:
        limited = 'rate limit' in (response.stdout + response.stderr).lower()
        return {**result, 'status': 'rate_limited' if limited else 'process_failed', 'schema_valid': False,
                'retryable': True, 'retry_after_seconds': 60 if limited else 0}
    if response.metadata['model'] != MODELS['astra'] or response.metadata['effort'] != plan.effort:
        return {**result, 'status': 'model_or_effort_mismatch', 'schema_valid': False, 'retryable': False}
    return {**result, 'status': 'completed', 'retryable': True}


def _decode(result, reference):
    if result.get('status') != 'completed':
        return result
    try:
        raw = json.loads(result['text'])
        binding = '61c08a1' if reference.get('pair_wire_version') == WIRE_61C08A1 else None
        answer = annotation_from_reference_response(raw, reference['item'], reference.get('baseline'), binding=binding)
    except (ValueError, KeyError, TypeError, ValidationError) as exc:
        message = ('Schema validation failed at ' + '/'.join(map(str, exc.absolute_path))
                   if isinstance(exc, ValidationError) else str(exc)[:400])
        return {**result, 'status': 'invalid', 'schema_valid': False, 'retryable': True,
                'error_type': type(exc).__name__, 'validation_error': message}
    return {**result, 'status': 'valid', 'schema_valid': True, 'retryable': False,
            'raw_response': raw, 'answer': answer}


def run_item(plan, item_id, *, allow_inference=False, ledger=None, client=None, key_path=None,
             transport=None, sleeper=time.sleep):
    revalidate(plan)
    if plan.settings.get('transport') in BATCH_TRANSPORTS:
        raise ExperimentBlocked('Two-judge references run only through the provider batch runner')
    if not allow_inference:
        raise ExperimentBlocked('Reference inference is disabled unless explicitly requested')
    oracle_release_guard(plan, item_id=item_id)
    items = {row['item_id']: row for row in plan.items}
    if item_id not in items:
        raise ExperimentBlocked('Item is outside the signed immutable reference package')
    item = items[item_id]
    baseline = plan.baselines.get(item['article']['pmid']) if item['task'] == 'extraction' else None
    reference = prepare_reference(item, baseline=baseline)
    budget = _ledger(plan, ledger)
    identity = {'stage': plan.stage, 'judge': plan.judge, 'requested_model': MODELS[plan.judge],
        'effort': plan.effort, 'reference_revision': deepcopy(plan.binding['reference_revision']),
        'item_id': item_id, 'task': items[item_id]['task'],
        'package_sha256': plan.package_sha256, 'binding': plan.binding,
        'item_sha256': reference['raw_public_item_sha256'],
        'displayed_public_item_sha256': reference['displayed_public_item_sha256'],
        'prompt_sha256': reference['model_payload_sha256'], 'instructions_sha256': reference['instructions_sha256'],
        'instrument_sha256': reference['instrument_sha256'], 'pair_instrument_sha256': reference['pair_instrument_sha256'],
        'canonical_schema_sha256': reference['canonical_schema_sha256'], 'wire_schema_sha256': reference['wire_schema_sha256'],
        'comparison_map_sha256': reference.get('comparison_map_sha256'),
        'baseline_sha256': reference.get('baseline_sha256'),
        'model_view_sha256': reference.get('model_view_sha256'),
        'input_projection_sha256': reference.get('input_projection_sha256'),
        'max_attempts': 2, 'attempt_timeout_seconds': 1800,
        'max_tokens': plan.settings['max_tokens'] if plan.judge == 'fable' else None,
        'classifier_outputs_in_prompt': False, 'peer_outputs_in_prompt': False, 'human_answers_in_prompt': False}
    directory = Path(plan.output_dir) / 'items' / item_id
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        final_path = directory / 'result.json'
        if final_path.exists():
            result = _json(final_path)
            if any(result.get(key) != value for key, value in identity.items()):
                raise ExperimentBlocked('An existing reference belongs to different inputs, instrument or effort')
            count = result.get('attempt_count')
            if count not in {1, 2}:
                raise ExperimentBlocked('Cached reference has an invalid attempt count')
            last = _json(directory / f'attempt-{count}' / 'metadata.json')
            raw_path = directory / f'attempt-{count}' / 'raw_output.txt'
            if (last.get('identity') != identity or last.get('status') != result.get('status')
                    or not raw_path.exists() or file_sha(raw_path) != last.get('raw_output_sha256')):
                raise ExperimentBlocked('Cached reference provenance/raw output changed')
            if result['status'] == 'valid':
                rebuilt = _decode({**last, 'status': 'completed', 'text': raw_path.read_text()}, reference)
                if rebuilt.get('status') != 'valid' or rebuilt.get('answer') != result.get('answer') or rebuilt.get('raw_response') != result.get('raw_response'):
                    raise ExperimentBlocked('Cached annotation was changed or no longer matches its raw response')
            return result
        write_json(directory / 'item.json', reference['item'])
        (directory / 'prompt.json').write_text(reference['prompt'])
        (directory / 'instructions.txt').write_text(reference['instructions'])
        write_json(directory / 'schema.json', reference['schema'])
        write_json(directory / 'wire_schema.json', reference['wire_schema'])
        write_json(directory / 'input_projection.json', reference['input_projection'])
        if baseline is not None:
            write_json(directory / 'private_baseline.json', baseline)
            write_json(directory / 'comparison_map.json', reference['comparison_map'])
            write_json(directory / 'source_provenance.json', reference['source_provenance'])
        attempts = []
        for ordinal in (1, 2):
            revalidate(plan)
            release = oracle_release_guard(plan, item_id=item_id)
            attempt = directory / f'attempt-{ordinal}'
            metadata = attempt / 'metadata.json'
            if metadata.exists():
                value = _json(metadata)
                if value.get('identity') != identity:
                    raise ExperimentBlocked('Attempt identity changed')
            else:
                if attempt.exists():
                    raise ExperimentBlocked('Interrupted reference attempt requires accounting review, not another call')
                reservation = None
                if plan.judge == 'fable' and transport is None:
                    # A funding pause occurs before an attempt exists and cannot
                    # consume a model attempt or create an ambiguous resubmission.
                    request = fable_request(reference, plan.settings['max_tokens'])
                    reservation = budget.reserve(worst_case_cost(request), digest(request), FABLE_MODEL,
                        metadata={'stage': plan.stage, 'item_id': item_id})
                attempt.mkdir(); started = utcnow(); clock = time.monotonic()
                write_json(attempt / 'started.json', {'identity': identity, 'attempt': ordinal, 'started_at': started,
                                                     'reservation_id': reservation, 'oracle_release': release})
                try:
                    if transport is not None:
                        value = transport(plan, reference, attempt, budget)
                    elif plan.judge == 'astra':
                        value = _astra_once(plan, reference, attempt)
                    else:
                        value = _fable_once(plan, reference, attempt, budget, client=client, key_path=key_path,
                                            reservation=reservation)
                    raw_path = attempt / 'raw_output.txt'
                    text = value.get('text', '')
                    if raw_path.exists() and raw_path.read_text() != text:
                        raise ExperimentBlocked('Transport raw output differs from its returned text')
                    raw_path.write_text(text)
                    value['raw_output_sha256'] = file_sha(raw_path)
                    if value.get('status') == 'completed' and value.get('model') != MODELS[plan.judge]:
                        value = {**value, 'status': 'model_mismatch', 'schema_valid': False, 'retryable': False}
                    value = _decode(value, reference)
                except Exception as exc:
                    write_json(attempt / 'blocked.json', {'identity': identity, 'attempt': ordinal,
                        'started_at': started, 'ended_at': utcnow(), 'error_type': type(exc).__name__,
                        'reason': 'No silent retry; coordinator must inspect dispatch/accounting state'})
                    raise
                value.setdefault('oracle_release', release)
                value.update(identity=identity, attempt=ordinal, started_at=started, ended_at=utcnow(),
                             elapsed_seconds=time.monotonic()-clock)
                write_json(metadata, value)
            attempts.append(value)
            if value['status'] == 'valid' or not value.get('retryable'): break
            if ordinal < 2 and value.get('retry_after_seconds'):
                sleeper(value['retry_after_seconds'])
        last = attempts[-1]
        result = {**identity, 'status': 'valid' if last['status'] == 'valid' else last['status'],
            'schema_valid': last.get('schema_valid', False), 'answer': last.get('answer'),
            'raw_response': last.get('raw_response'), 'attempt_count': len(attempts),
            'attempt_statuses': [row['status'] for row in attempts],
            'started_at': attempts[0]['started_at'], 'ended_at': last['ended_at'],
            'elapsed_seconds': sum(row['elapsed_seconds'] for row in attempts),
            'cost_usd': sum(row['cost_usd'] for row in attempts) if all(row.get('cost_usd') is not None for row in attempts) else None,
            'annotator_type': 'LLM', 'reference_role': 'development_pair_reference' if plan.stage == 'classifier_development' else
                'final_primary_oracle' if plan.judge == 'astra' else 'final_secondary_oracle'}
        write_json(final_path, result)
        return result


def pilot_decision(plan, results, *, as_of=None):
    if plan.stage != 'final_evaluation' or plan.effort != 'max':
        raise ValueError('Only a registered final-reference pilot determines feasibility')
    index = {row['item_id']: row for row in results}
    if len(index) != 5 or set(index) != set(plan.pilot_ids):
        raise ValueError('Pilot decision requires all five fixed terminal items, never selected successes')
    now = _timestamp(as_of or utcnow())
    by_task = {task: [row['elapsed_seconds'] for row in results if row['task'] == task] for task in ('pair','extraction')}
    medians = {task: statistics.median(values) for task, values in by_task.items()}
    remaining = Counter(row['task'] for row in plan.items if row['item_id'] not in index)
    seconds = 1.5 * sum(remaining[task]*medians[task] for task in medians) / plan.settings['concurrency']
    valid = sum(row['status'] == 'valid' for row in results)
    feasible = valid >= 4 and now + timedelta(seconds=seconds) <= _timestamp(plan.pilot_policy['deadline'])
    decision = 'continue_max' if feasible else 'human_priority_oracle_incomplete'
    return {'decided_at': now.isoformat(), 'stage': plan.stage, 'judge': plan.judge,
        'effort': plan.effort, 'package_sha256': plan.package_sha256, 'pilot_ids': list(plan.pilot_ids),
        'valid': valid, 'requested': 5, 'median_seconds': statistics.median(row['elapsed_seconds'] for row in results),
        'task_median_seconds': medians, 'projection_seconds_remaining_with_margin': seconds,
        'projected_completion': (now+timedelta(seconds=seconds)).isoformat(),
        'decision': decision, 'no_labels_used_for_feasibility': True,
        'failure_times_included': True, 'no_effort_mixing': True}


def inspect_first_input(plan, item_id):
    """Actual credential-free local Astra input inspection; never an inference."""
    revalidate(plan)
    if plan.judge != 'astra':
        raise ValueError('Local Codex inspection applies only to Astra')
    item = next((row for row in plan.items if row['item_id'] == item_id), None)
    if item is None:
        raise ExperimentBlocked('Inspection item is outside the frozen package')
    baseline = plan.baselines.get(item['article']['pmid']) if item['task'] == 'extraction' else None
    request = prepare_reference(item, baseline=baseline)
    identity = {'item_id': item_id, 'package_sha256': plan.package_sha256,
                'prompt_sha256': request['model_payload_sha256'],
                'instructions_sha256': request['instructions_sha256'], 'effort': plan.effort}
    path = Path(plan.output_dir) / 'first_item_local_input.json'
    if path.exists():
        report = _json(path)
        if report.get('reference_identity') != identity or report.get('passed') is not True:
            raise ExperimentBlocked('Prior input inspection belongs to another reference')
        return report
    report = _isolation_module().inspect_model_input(request['prompt'], request['wire_schema'],
        instructions_text=request['instructions'], effort=plan.effort)
    report = {**report, 'reference_identity': identity}
    write_json(path, report)
    if report.get('passed') is not True or report.get('credential_free') is not True or report.get('inference_calls') != 0:
        raise ExperimentBlocked('Actual local model input failed the annotation-only isolation check')
    return report


def dry_run_plan(plan):
    """Validate every frozen model input/schema locally, even while release is held."""
    revalidate(plan)
    rows = []
    for item in plan.items:
        baseline = plan.baselines.get(item['article']['pmid']) if item['task'] == 'extraction' else None
        request = prepare_reference(item, baseline=baseline)
        Draft202012Validator.check_schema(request['schema'])
        Draft202012Validator.check_schema(request['wire_schema'])
        rows.append({'item_id': item['item_id'], 'task': item['task'],
                     'prompt_sha256': request['model_payload_sha256'],
                     'instructions_sha256': request['instructions_sha256'],
                     'canonical_schema_sha256': request['canonical_schema_sha256'],
                     'wire_schema_sha256': request['wire_schema_sha256'],
                     'baseline_sha256': request.get('baseline_sha256'),
                     'comparison_map_sha256': request.get('comparison_map_sha256')})
    report = {'stage': plan.stage, 'judge': plan.judge, 'effort': plan.effort,
              'status': 'offline_validated', 'inference_calls': 0,
              'package_sha256': plan.package_sha256, 'binding': plan.binding,
              'expected_item_ids': list(plan.expected_ids), 'items': rows,
              'release_required_before_inference': True,
              'reference_revision': deepcopy(plan.binding['reference_revision'])}
    write_json(Path(plan.output_dir) / 'dry_run_manifest.json', report)
    return report


def run_plan(plan, *, phase='full', allow_inference=False, ledger=None, client=None, key_path=None, transport=None):
    revalidate(plan)
    if plan.settings.get('transport') in BATCH_TRANSPORTS:
        raise ExperimentBlocked('Two-judge references run only through the provider batch runner')
    if not allow_inference:
        raise ExperimentBlocked('Reference inference is disabled unless explicitly requested')
    oracle_release_guard(plan, phase=phase)
    if phase not in {'pilot', 'full'} or (phase == 'pilot' and plan.stage != 'final_evaluation'):
        raise ValueError('Pilot is reserved for the final sampled package')
    if plan.stage == 'final_evaluation' and phase == 'full':
        root = Path(plan.output_dir)
        decision = _json(root / 'pilot_decision.json')
        pilot_manifest = _json(root / 'pilot_manifest.json')
        pilot_answers = root / 'pilot_answers.jsonl'
        if (decision.get('package_sha256') != plan.package_sha256 or decision.get('effort') != plan.effort
                or decision.get('judge') != plan.judge or decision.get('decision') != 'continue_' + plan.effort
                or decision.get('pilot_manifest_sha256') != file_sha(root / 'pilot_manifest.json')
                or decision.get('pilot_answers_sha256') != file_sha(pilot_answers)
                or pilot_manifest.get('binding') != plan.binding
                or pilot_manifest.get('requested_item_ids') != list(plan.pilot_ids)):
            raise ExperimentBlocked('A whole final reference waits for its own fixed-effort pilot decision')
        original_decision = pilot_decision(plan, list(read_jsonl(pilot_answers)), as_of=decision['decided_at'])
        if any(decision.get(key) != value for key, value in original_decision.items()):
            raise ExperimentBlocked('Pilot feasibility decision differs from its frozen inputs')
    budget = _ledger(plan, ledger)
    ids = list(plan.pilot_ids if phase == 'pilot' else plan.order)
    if plan.judge == 'astra' and transport is None and ids:
        inspect_first_input(plan, ids[0])
    rows, failures = {}, []
    with concurrent.futures.ThreadPoolExecutor(max_workers=plan.settings['concurrency']) as pool:
        pending = {}; iterator = iter(ids); stopped = False
        def submit():
            try: item_id = next(iterator)
            except StopIteration: return
            pending[pool.submit(run_item, plan, item_id, allow_inference=allow_inference,
                ledger=budget, client=client, key_path=key_path, transport=transport)] = item_id
        for _ in range(plan.settings['concurrency']): submit()
        while pending:
            completed, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in completed:
                item_id = pending.pop(future)
                try: rows[item_id] = future.result()
                except Exception as exc:  # noqa: BLE001 -- drain in-flight work, never hide a blocked item
                    failures.append({'item_id': item_id, 'error_type': type(exc).__name__}); stopped = True
                if not stopped: submit()
    ordered = [rows[key] for key in plan.expected_ids if key in rows]
    root = Path(plan.output_dir); root.mkdir(parents=True, exist_ok=True)
    answer_path = root / ('pilot_answers.jsonl' if phase == 'pilot' else 'answers.jsonl')
    write_jsonl(answer_path, ordered)
    complete = len(ordered) == len(ids) and not failures
    valid = sum(row['status'] == 'valid' for row in ordered)
    status = 'blocked' if failures else 'complete' if complete and valid == len(ids) else 'complete_with_missing' if complete else 'partial'
    manifest = {'stage': plan.stage, 'judge_policy': 'astra_only', 'reference_design': 'single_astra_max', 'active_judges': ['astra'], 'judge': plan.judge, 'requested_model': MODELS[plan.judge],
        'effort': plan.effort, 'phase': phase, 'status': status, 'binding': plan.binding,
        'reference_revision': deepcopy(plan.binding['reference_revision']),
        'package_sha256': plan.package_sha256, 'frame_manifest_sha256': plan.frame_manifest_sha256,
        'expected_item_ids': list(plan.expected_ids), 'requested_item_ids': ids, 'processed': len(ordered), 'valid': valid,
        'invalid_item_ids': [row['item_id'] for row in ordered if row['status'] != 'valid'], 'failures': failures,
        'classifier_outputs_in_prompt': False, 'peer_outputs_in_prompt': False, 'human_answers_in_prompt': False,
        'answers': {'path': str(answer_path.relative_to(ROOT)) if answer_path.is_relative_to(ROOT) else str(answer_path),
                    'sha256': file_sha(answer_path)}, 'generated_at': utcnow()}
    write_json(root / ('pilot_manifest.json' if phase == 'pilot' else 'run_manifest.json'), manifest)
    if phase == 'pilot' and complete:
        write_json(root / 'pilot_decision.json', {**pilot_decision(plan, ordered),
            'pilot_manifest_sha256': file_sha(root / 'pilot_manifest.json'),
            'pilot_answers_sha256': file_sha(answer_path)})
    return manifest
