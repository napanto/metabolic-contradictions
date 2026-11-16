"""Synthetic metadata policy tests; no actual requests or provider state."""
import asyncio

import httpx
import pytest

from chl_rag import adaptive_transport as adaptive


@pytest.fixture
def control(tmp_path):
    now=[1000.0]
    value=adaptive.ProviderControl(tmp_path/'slots',tmp_path/'state',clock=lambda:now[0])
    return value,now


def state(control):return control.report()[0]


def test_429_halves_cooldown_and_restart_preserves_cap(control):
    value,now=control;value.observe('Synthetic',429,False)
    assert state(value)['cap']==32 and state(value)['unknown_posts']==0
    now[0]+=1;value.observe('Synthetic',429,False);assert state(value)['cap']==32
    now[0]+=15;value.observe('Synthetic',429,False);assert state(value)['cap']==16
    resumed=adaptive.ProviderControl(value.slots,value.root,clock=lambda:now[0])
    assert state(resumed)['cap']==16


def test_5xx_full60s_window_and_clean_posts_do_not_repeat_old_burst(control):
    value,now=control
    value.observe('Synthetic',500,False);now[0]+=1;value.observe('Synthetic',502,False)
    for _ in range(25):value.observe('Synthetic',200,False)
    now[0]+=1;value.observe('Synthetic',503,False)
    assert state(value)['cap']==32 and len(state(value)['server_errors_60s'])==3
    now[0]+=16;value.observe('Synthetic',200,False)
    assert state(value)['cap']==32
    now[0]+=61
    for _ in range(4):value.observe('Synthetic',200,False)
    assert state(value)['cap']==40


def test_unknown_last20_retains_old_events_and_requires_minimum(control):
    value,now=control
    value.observe('Synthetic',200,True)
    for _ in range(9):
        now[0]+=70;value.observe('Synthetic',200,False)
    assert state(value)['cap']==64
    now[0]+=70;value.observe('Synthetic',200,True)
    assert state(value)['recent_posts']==11
    assert state(value)['recent_unknown_fraction']==pytest.approx(2/11)
    assert state(value)['cap']==32


def test_known_free_429_is_not_unknown_and_response_is_unchanged(control):
    value,_now=control
    async def run():
        raw=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(429,json={'error':'synthetic'})))
        observed=adaptive.ObservedHTTP(raw,value,{'synthetic/fp8':'Synthetic'})
        try:
            response=await observed.post('https://synthetic.invalid',json={'provider':{'order':['synthetic/fp8']}})
            assert response.status_code==429 and response.json()=={'error':'synthetic'}
        finally:await observed.aclose()
    asyncio.run(run())
    assert state(value)['http_429']==1 and state(value)['unknown_posts']==0


def test_state_write_failure_before_admission_releases_slot(control,monkeypatch):
    value,_now=control
    monkeypatch.setattr(adaptive,'write_json',lambda *a,**k:(_ for _ in ()).throw(OSError('synthetic disk failure')))
    with pytest.raises(adaptive.RequestConfigurationError):value.try_slot('Synthetic')
    assert value.fault and not value.report()
