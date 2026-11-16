import asyncio
import json

import httpx

from chl_rag.model_client import V2Client


def test_identical_retry_gets_new_call_but_resume_uses_same_attempt_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    route = {'tag': 'fixture/fp8', 'provider_name': 'Fixture', 'max_completion_tokens': 10000,
             'pricing': {'prompt': '0.0000001', 'completion': '0.0000001'}}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'models': {'model': {'canonical_slug': 'model-fixed', 'routes': [route]}}}))
    key = tmp_path / 'credential.env'
    key.write_text('OPENROUTER_API_KEY=synthetic-test-value\n')
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body['provider']['only'] == ['fixture/fp8']
        assert body['provider']['quantizations'] == ['fp8']
        return httpx.Response(200, json={'id': f'gen{len(requests)}', 'provider': 'Fixture',
            'usage': {'cost': .00001}, 'choices': [{'finish_reason': 'stop',
            'message': {'content': '{}'}}]})

    async def execute():
        client = V2Client(config=config, credential_path=key, transport=httpx.MockTransport(handler))
        args = ('model', [{'role': 'user', 'content': 'fixture'}], {'type': 'object'}, 'on', 1000)
        try:
            first, _ = await client.generate(*args, cache_namespace='attempt1')
            second, _ = await client.generate(*args, cache_namespace='attempt2')
            resumed, provenance = await client.generate(*args, cache_namespace='attempt2')
            assert first['id'] != second['id'] == resumed['id']
            assert provenance['cache_hit'] is True
        finally:
            await client.close()
    asyncio.run(execute())
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert requests[0]['reasoning'] == {'enabled': True}
    assert 'synthetic-test-value' not in ''.join(p.read_text() for p in (tmp_path/'results').rglob('*.json'))


def test_shared_provider_tag_uses_the_requested_models_price(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cheap = {'tag': 'same/fp8', 'provider_name': 'Same', 'max_completion_tokens': 10000,
             'pricing': {'prompt': '0.0000001', 'completion': '0.0000001'}}
    expensive = {**cheap, 'pricing': {'prompt': '0.000001', 'completion': '0.000001'}}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'models': {
        'expensive': {'canonical_slug': 'expensive', 'routes': [expensive]},
        'cheap': {'canonical_slug': 'cheap', 'routes': [cheap]}}}))
    key = tmp_path / 'credential.env'
    key.write_text('OPENROUTER_API_KEY=synthetic-test-value\n')
    async def execute():
        client = V2Client(config=config, credential_path=key, transport=httpx.MockTransport(lambda r: None))
        try:
            request = client.make_request('expensive', [], {'type': 'object'}, 'off', 1000, expensive)
            assert client.reserve_cost(request) > .005
            assert client.routes['same/fp8']['pricing'] == cheap['pricing']
        finally:
            await client.close()
    asyncio.run(execute())


def test_global_provider_slots_are_shared_across_client_instances(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    route = {'tag': 'fixture/fp8', 'provider_name': 'Fixture', 'max_completion_tokens': 10000,
             'pricing': {'prompt': '0.0000001', 'completion': '0.0000001'}}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'models': {'model': {'canonical_slug': 'model-fixed', 'routes': [route]}}}))
    key = tmp_path / 'credential.env'
    key.write_text('OPENROUTER_API_KEY=synthetic-test-value\n')

    async def execute():
        active = peak = calls = 0
        full, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            nonlocal active, peak, calls
            active += 1
            calls += 1
            generation = calls
            peak = max(peak, active)
            if active == 16:
                full.set()
            await release.wait()
            await asyncio.sleep(.001)
            active -= 1
            return httpx.Response(200, json={'id': f'gen{generation}', 'provider': 'Fixture',
                'usage': {'cost': .00001}, 'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]})
        clients = [V2Client(config=config, credential_path=key, transport=httpx.MockTransport(handler)) for _ in range(2)]
        args = ('model', [{'role': 'user', 'content': 'fixture'}], {'type': 'object'}, 'off', 1000)
        jobs = [asyncio.create_task(clients[i % 2].generate(*args, cache_namespace=f'parallel-{i}')) for i in range(20)]
        try:
            await asyncio.wait_for(full.wait(), 5)
            await asyncio.sleep(.05)
            assert active == 16
            release.set()
            await asyncio.gather(*jobs)
            assert calls == 20 and peak == 16
        finally:
            release.set()
            await asyncio.gather(*jobs, return_exceptions=True)
            for client in clients:
                await client.close()
    asyncio.run(execute())


def test_direct_generation_cannot_bypass_model_execution_hold(tmp_path, monkeypatch):
    import pytest

    from chl_rag.llm import RequestConfigurationError
    monkeypatch.chdir(tmp_path)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'models': {'held': {'routes': [], 'execution_hold': True}}}))
    key = tmp_path / 'credential.env'
    key.write_text('OPENROUTER_API_KEY=synthetic-test-value\n')
    async def execute():
        client = V2Client(config=config, credential_path=key, transport=httpx.MockTransport(lambda r: pytest.fail('held call sent')))
        try:
            with pytest.raises(RequestConfigurationError, match='execution hold'):
                await client.generate('held', [], {'type': 'object'}, 'high', 1000)
        finally:
            await client.close()
    asyncio.run(execute())
