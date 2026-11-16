from datetime import UTC

import pytest

from chl_rag.llm import AccountingError, BudgetError
from chl_rag.spend_budget import PhaseLedger, ReservationCapacityWait


def test_phase_limit_includes_uncertain_and_persists_across_instances(tmp_path):
    ledger = PhaseLedger('openrouter', 'extraction_development', tmp_path)
    token = ledger.reserve(19, 'a', 'model')
    ledger.uncertain(token, 'timeout')
    other = PhaseLedger('openrouter', 'extraction_development', tmp_path)
    with pytest.raises(BudgetError):
        other.reserve(2, 'b', 'model')
    assert other.snapshot()['liability'] == 19


def test_combined_projection_alert_does_not_shrink_science(tmp_path):
    extraction = PhaseLedger('openrouter', 'production_extraction', tmp_path)
    classification = PhaseLedger('openrouter', 'production_classification', tmp_path)
    token = extraction.reserve(44, 'a', 'model')
    extraction.settle(token, 44)
    with pytest.raises(BudgetError):
        classification.check_projection(42)
    assert classification.snapshot()['funding']['available_credit'] == 96


def test_charge_above_reservation_blocks_later_calls(tmp_path):
    ledger = PhaseLedger('anthropic', 'development_judging', tmp_path)
    token = ledger.reserve(1, 'a', 'model')
    with pytest.raises(AccountingError):
        ledger.settle(token, 2)
    with pytest.raises(AccountingError):
        ledger.reserve(1, 'b', 'model')
    assert ledger.snapshot()['total_cost'] == 2


def test_release_and_accounted(tmp_path):
    ledger = PhaseLedger('openrouter', 'extraction_development', tmp_path)
    token = ledger.reserve(3, 'a', 'model')
    ledger.release(token, '429 without generation')
    token = ledger.reserve(3, 'b', 'model')
    response = {'id': 'generation', 'usage': {'cost': .1}, 'provider': 'fp8-provider'}
    ledger.finish(token, response)
    assert ledger.accounted('b', response)
    assert ledger.snapshot()['total_cost'] == .1
    assert not (tmp_path / 'results/spend.json').exists()


def test_secondary_fable_requires_later_authorization(tmp_path):
    with pytest.raises(ValueError):
        PhaseLedger('anthropic', 'final_judging', tmp_path)
    ledger = PhaseLedger('anthropic', 'final_reference', tmp_path)
    with pytest.raises(BudgetError, match='Phase funding alert'):
        ledger.reserve(.01, 'future-final', 'claude-fable-5-2')
    assert not ledger.snapshot()['pending']


def test_external_screen_and_merged_copies_are_counted_once(tmp_path):
    import json
    root = tmp_path/'project'
    external = tmp_path/'project-clf/results'
    external.mkdir(parents=True)
    first = {'calls': [{'cost': 2, 'reservation_id': 'a', 'generation_id': 'ga'}],
             'pending': {'p': {'reserved': 5}}, 'total_cost': 2}
    screen = {'calls': [{'cost': 3, 'reservation_id': 'b', 'generation_id': 'gb'}],
              'pending': {}, 'total_cost': 3}
    (external/'spend_classifier_selection.json').write_text(json.dumps(first))
    (external/'spend_mancon_screen.json').write_text(json.dumps(screen))
    ledger = PhaseLedger('openrouter', 'extraction_development', root)
    (root/'results/spend_classifier_selection.json').write_text(json.dumps(first))
    assert ledger.external_liability() == 10
    first['pending'] = {}
    first['calls'].append({'cost': 1, 'reservation_id': 'p', 'generation_id': 'gp'})
    (root/'results/spend_classifier_selection.json').write_text(json.dumps(first))
    assert ledger.external_liability() == 6


def test_live_balance_uses_only_own_inflight_and_settled_delta(tmp_path):
    import json
    from datetime import datetime
    root = tmp_path/'project'
    (root/'configs').mkdir(parents=True)
    (root/'configs/funding.json').write_text(json.dumps({'status':'confirmed','authority':'synthetic explicit accounting confirmation',
        'vendors':{'openrouter':{'available_credit':96,'alert':85,'phase_alerts':{'extraction_development':20},
            'headroom_policy':'real_balance_own_inflight_floor','floor_usd':15,'new_calls_paused':False}}}))
    external = tmp_path/'project-clf/results'
    external.mkdir(parents=True)
    (external/'spend_mancon_screen.json').write_text(json.dumps({'calls':[], 'pending':{},
        'reconciled_reserves':{'external':{'reserved':1000}}}))
    ledger = PhaseLedger('openrouter','extraction_development',root)
    with ledger.locked() as value:
        value['calls'] = [{'phase':'extraction_development','cost':20}]
        value['total_cost'] = 20
        value['pending']['old'] = {'phase':'extraction_development','reserved':60,'uncertain':True}
    assert ledger.needs_credit_refresh()
    ledger.record_credit_snapshot(total_credits=100,total_usage=20,own_cost_before_request=20,
                                   checked_at=datetime.now(UTC).isoformat())
    assert not ledger.needs_credit_refresh()
    token = ledger.reserve(60,'first','model')
    with pytest.raises(ReservationCapacityWait,match='reservations'):
        ledger.reserve(6,'second','model')
    ledger.settle(token,5)
    #80was already net of old charges; only the new5 is subtracted.
    token = ledger.reserve(60,'third','model')
    assert ledger.snapshot()['pending']['old']['reserved']==60  # audit preserved
    ledger.release(token,'synthetic no-generation rejection')
    with pytest.raises(BudgetError,match='less than15'):
        ledger.check_projection(61)


def test_comparison_pause_allows_only_fixed_model_without_bypassing_funding(tmp_path):
    import json
    ledger = PhaseLedger('openrouter', 'extraction_development', tmp_path)
    funding = ledger.funding()
    funding.update(new_calls_paused=True, comparison_stop_allow_models=['z-ai/glm-5.3-flash'])
    (tmp_path/'configs').mkdir()
    (tmp_path/'configs/funding.json').write_text(json.dumps({
        'status':'confirmed', 'authority':'synthetic fixed-model go with comparison stop',
        'vendors':{'openrouter':funding}}))
    with pytest.raises(BudgetError, match='explicitly paused'):
        ledger.reserve(1, 'retired-retry', 'qwen/qwen3.6-35b-a3b')
    token = ledger.reserve(1, 'authorized', 'z-ai/glm-5.3-flash')
    ledger.settle(token, .1)
    with pytest.raises(BudgetError, match='Phase funding alert'):
        ledger.reserve(21, 'beyond-phase-headroom', 'z-ai/glm-5.3-flash')
