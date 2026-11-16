"""Registered two-judge reference policy (Astra and Fable through provider batches).

Pure validation, no I/O and no network. The policy is accepted only when the
signed evaluation configuration declares every field below; a DRAFT file has no
effect until its fields are registered in that configuration.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

POLICY = {'active_judges': ['astra', 'fable'], 'judge_policy': 'two_judges',
          'reference_design': 'astra_api_max_plus_fable_max'}
MODELS = {'astra': 'gpt-6-astra', 'fable': 'claude-fable-5-2'}
TRANSPORTS = {'astra': 'openai_batch', 'fable': 'anthropic_message_batches'}
BATCH_TRANSPORTS = frozenset(TRANSPORTS.values())
# Model maxima from the official model pages (papers/inbox-2026-11-16).
OUTPUT_CAPS = {'astra': 128000, 'fable': 128000}
CAP_FIELDS = {'astra': 'max_output_tokens', 'fable': 'max_tokens'}
EFFORT = 'max'
ANTHROPIC_VERSION = '2023-06-01'
PILOT_RULE = 'ascending_sha256_of_salt_plus_item_id'
PILOT_SALT = 'oracle-pilot-20261116:'
PILOT_PAIRS = 2
PAIR_BINDING = '61c08a1'
PRE_GENERATION_RULE = {
    'policy': 'retry_with_backoff_until_accepted_not_an_attempt',
    'covers': ['http_429', 'http_529_overloaded', 'http_5xx_without_generation',
               'provider_batch_validation_failure', 'connection_error_before_any_output'],
    'backoff_seconds': {'base': 15, 'factor': 2, 'max': 300},
    'invalid_or_truncated_output': 'single_identical_retry',
    'after_cutoff': 'not yet accepted items follow the cutoff rule (synchronous full price)'}


class TwoJudgePolicyError(Exception):
    """Raised for an unregistered or inconsistent two-judge configuration."""


def is_two_judge(config):
    return isinstance(config, dict) and all(config.get(key) == value for key, value in POLICY.items())


def _aware(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def anthropic_stop_lifted(config):
    """Fable may run only after Antonio's recorded, signed lift of the 14 November 00:50 stop.

    The loader of the two-judge run verifies the separate signed lift file against the
    evaluation configuration hash and only then places it under ``anthropic_stop``."""
    if not is_two_judge(config):
        return False
    stop = config.get('anthropic_stop')
    if not isinstance(stop, dict) or stop.get('status') != 'lifted':
        return False
    authority = stop.get('authority')
    return (isinstance(authority, dict) and authority.get('actor') == 'Antonio'
            and authority.get('kind') == 'explicit_lift'
            and all(isinstance(authority.get(key), str) and authority[key].strip() for key in ('verbatim', 'source'))
            and _aware(stop.get('lifted_at')) is not None)


def pilot_pair_ids(items, *, salt=PILOT_SALT, count=PILOT_PAIRS):
    """Registered seed rule: pair items ordered by sha256(salt + item_id), first ``count``."""
    pairs = sorted((row['item_id'] for row in items if row.get('task') == 'pair'),
                   key=lambda item_id: hashlib.sha256((salt + item_id).encode()).hexdigest())
    if len(pairs) < count:
        raise TwoJudgePolicyError('Too few pair items for the registered pilot')
    return tuple(pairs[:count])


def cutoff(settings):
    """The registered cutoff instant, or None while Antonio has not set it."""
    return _aware(settings.get('cutoff_at'))


def two_judge_settings(config, judge, effort):
    if not is_two_judge(config):
        raise TwoJudgePolicyError('Signed two-judge policy is required')
    if judge not in MODELS:
        raise TwoJudgePolicyError('Unknown judge')
    refs = config.get('references')
    if not isinstance(refs, dict) or refs.get('transport') != 'provider_batch':
        raise TwoJudgePolicyError('Two-judge references run only through provider batches')
    level = refs.get('effort')
    if level != EFFORT or effort != level:
        raise TwoJudgePolicyError('One registered effort level, max, applies to both judges and every task')
    for name, model in MODELS.items():
        own = refs.get(name)
        if not isinstance(own, dict):
            raise TwoJudgePolicyError('Both judges must be registered')
        if any(key != 'effort' and 'effort' in key for key in own):
            raise TwoJudgePolicyError('Per-judge or per-task effort overrides are not allowed')
        if (own.get('model') != model or own.get('transport') != TRANSPORTS[name]
                or own.get('effort', level) != level):
            raise TwoJudgePolicyError('Judge model, transport or effort differs from registration')
        if own.get(CAP_FIELDS[name]) != OUTPUT_CAPS[name]:
            raise TwoJudgePolicyError('Output cap must be the registered model maximum')
    astra, fable = refs['astra'], refs['fable']
    if astra.get('store') is not False or astra.get('prompt_cache') != {'mode': 'explicit', 'ttl': '30m'}:
        raise TwoJudgePolicyError('Astra requires store=false and the registered explicit 30m cache')
    if fable.get('cache_ttl') != '1h' or fable.get('anthropic_version') != ANTHROPIC_VERSION:
        raise TwoJudgePolicyError('Fable requires the registered 1h cache TTL and API version')
    if refs.get('tasks') != ['pair']:
        raise TwoJudgePolicyError('This run is registered for pair items only')
    if refs.get('pair_binding') != PAIR_BINDING:
        raise TwoJudgePolicyError('The final run accepts only the 61c08a1 pair binding')
    if refs.get('max_attempts') != 2 or refs.get('retry') != 'second_batch_of_failed_items_only':
        raise TwoJudgePolicyError('One initial attempt and at most one identical retry in a second batch')
    if refs.get('truncation') != 'invalid_with_single_identical_retry':
        raise TwoJudgePolicyError('An output-cap stop must count as invalid with the single retry')
    pilot = refs.get('pilot')
    if (not isinstance(pilot, dict) or pilot.get('rule') != PILOT_RULE or pilot.get('salt') != PILOT_SALT
            or pilot.get('pairs') != PILOT_PAIRS or pilot.get('same_for_both_judges') is not True
            or pilot.get('valid_pilot_answer_is_final_when_request_identical') is not True):
        raise TwoJudgePolicyError('Pilot must be the registered seeded two-pair rule shared by both judges')
    if refs.get('pre_generation_rejections') != PRE_GENERATION_RULE:
        raise TwoJudgePolicyError('Rejections before generation must be retried with backoff and not counted')
    if refs.get('sync_attempt_timeout_seconds') != 1800:
        raise TwoJudgePolicyError('Synchronous calls keep the registered 1800-second wall time')
    poll = refs.get('poll_interval_seconds')
    if type(poll) is not int or not 30 <= poll <= 3600:
        raise TwoJudgePolicyError('Registered poll interval must be 30-3600 seconds')
    if refs.get('cutoff_at') is not None and _aware(refs['cutoff_at']) is None:
        raise TwoJudgePolicyError('Cutoff must be a timezone-aware ISO instant or unset')
    own = refs[judge]
    return {'judge': judge, 'model': MODELS[judge], 'effort': level, 'transport': TRANSPORTS[judge],
            CAP_FIELDS[judge]: OUTPUT_CAPS[judge], 'max_tokens': OUTPUT_CAPS[judge],
            'max_attempts': 2, 'attempt_timeout_seconds': 1800, 'concurrency': 1,
            'tasks': ('pair',), 'pair_binding': PAIR_BINDING, 'cutoff_at': refs.get('cutoff_at'), 'poll_interval_seconds': poll,
            'pilot_salt': PILOT_SALT, 'pilot_pairs': PILOT_PAIRS,
            'store': astra.get('store') if judge == 'astra' else None,
            'prompt_cache': dict(astra['prompt_cache']) if judge == 'astra' else None,
            'cache_ttl': fable['cache_ttl'] if judge == 'fable' else None,
            'anthropic_version': fable['anthropic_version'] if judge == 'fable' else None,
            'policy': dict(POLICY), 'own': dict(own)}
