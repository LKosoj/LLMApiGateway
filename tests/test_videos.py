import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from llm_gateway_core.api.v1 import videos
from llm_gateway_core.api.v1.models import _add_gateway_operation_models
from llm_gateway_core.api.v1.rules_editor import (
    _build_structured_operation_rules_response,
    _serialize_structured_operation_rules,
)
from llm_gateway_core.api.v1.video_adapters import VideoRequest, build_video_payload, video_cost_usd
from llm_gateway_core.config.loader import ConfigLoader, ProviderDetails
from llm_gateway_core.config.schemas import ModelsOperationConfig
from llm_gateway_core.db.video_jobs_db import VideoJobsDB
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.middleware.accounting_admission import (
    ACCOUNTING_REQUEST_CONTEXT_STATE_KEY,
    AccountingRequestContext,
)
from llm_gateway_core.middleware.response_observation import ResponseObservationMiddleware
from llm_gateway_core.services.accounting import (
    AccountingReceipt, AccountingReservation, CostSource, ProjectionStatus, SourceStatus,
    classify_billing_policy,
)
from llm_gateway_core.services.request_handler import OperationDispatcher
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


MODEL = 'llmgateway/wan3.0'
ROUTES = [
    dict(provider='bettertoken', model='wan3.0-video', target_path='/videos', request_format='bettertoken_videos'),
    dict(provider='routerai', model='alibaba/wan-3.0', target_path='/videos', request_format='routerai_videos'),
]
RULES = {'videos_generations': {MODEL: {'routes': ROUTES}}}
BODY = dict(model=MODEL, prompt='A bird flies', duration=5, resolution='720p')


@asynccontextmanager
async def video_client(tmp_path, handler, *, key_id=7):
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    providers = {
        name: ProviderDetails(baseUrl=f'https://{name}.example/v1', apikey='test-key')
        for name in ('bettertoken', 'routerai')
    }
    loader = SimpleNamespace(providers_config=providers, operation_rules=RULES)
    dispatcher = OperationDispatcher(providers, RULES, upstream)
    snapshot = make_runtime_snapshot(config_loader=loader, operation_dispatcher=dispatcher)
    store = VideoJobsDB(tmp_path / 'videos.db')
    services = make_app_services(http_client=upstream, video_jobs_db=store)
    events = []

    async def commit(_reservation, event):
        events.append(event)
        return AccountingReceipt(
            source_status=SourceStatus.ACCEPTED, projection_status=ProjectionStatus.APPLIED,
            event_id=event.event_id, billing_fingerprint=event.billing_fingerprint, usage_row_id=1,
        )
    services.accounting_service.commit.side_effect = commit
    app = FastAPI()
    app.state.services = services
    app.include_router(videos.router, prefix='/v1')
    app.add_middleware(ResponseObservationMiddleware)

    class StateMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            request_id = uuid4().hex
            state = scope.setdefault('state', {})
            state.update(runtime_snapshot=snapshot, api_key_id=key_id, llmgateway_request_id=request_id)
            route = '/v1/videos' if scope['method'] == 'POST' else '/v1/videos/{job_id}'
            policy = classify_billing_policy(scope['method'], route)
            if policy is not None:
                state[ACCOUNTING_REQUEST_CONTEXT_STATE_KEY] = AccountingRequestContext(
                    method=scope['method'], route_template=route, policy=policy, request_id=request_id,
                    reservation=AccountingReservation(
                        reservation_id=request_id, request_id=request_id, api_key_id=key_id, reserved_usd=5,
                    ),
                )
            await self.app(scope, receive, send)
    app.add_middleware(StateMiddleware)
    async with upstream, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
        yield client, services, events


def test_bettertoken_reference_translation():
    body = VideoRequest(**BODY, input_references=[
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}},
        {'type': 'video_url', 'video_url': {'url': 'https://example.com/a.mp4'}},
        {'type': 'audio_url', 'audio_url': {'url': 'https://example.com/a.mp3'}},
    ], generate_audio=False, seed=42)
    payload = build_video_payload(body, 'wan3.0-video', 'bettertoken_videos')
    assert payload['operation'] == 'reference_to_video'
    assert payload['duration'] == {'mode': 'fixed', 'seconds': 5}
    assert payload['output'] == {'format': 'mp4', 'resolution': '720p'}
    assert payload['audio'] == {'mode': 'off'}
    assert payload['seed'] == 42
    assert [item['role'] for item in payload['media']] == ['reference_image', 'reference_video', 'reference_audio']
    assert payload['media'][0]['source'].startswith('data:')
    direct = build_video_payload(body, 'alibaba/wan-3.0', 'routerai_videos')
    assert direct['input_references'][1]['video_url']['url'] == 'https://example.com/a.mp4'


def test_first_frame_translation_and_validation():
    frame = {'type': 'image_url', 'image_url': {'url': 'https://example.com/frame.jpg'}, 'frame_type': 'first_frame'}
    payload = build_video_payload(VideoRequest(**BODY, frame_images=[frame]), 'wan3.0-video', 'bettertoken_videos')
    assert payload['operation'] == 'image_to_video'
    assert payload['media'][0]['role'] == 'first_frame'
    mixed = VideoRequest(**BODY, frame_images=[frame], input_references=[{**frame, 'frame_type': None}])
    translated = build_video_payload(mixed, 'wan3.0-video', 'bettertoken_videos')
    assert [media['role'] for media in translated['media']] == ['first_frame', 'reference_image']
    with pytest.raises(ValidationError):
        VideoRequest(**BODY, frame_images=[frame, frame])
    with pytest.raises(ValidationError):
        VideoRequest(**{**BODY, 'duration': True})


@pytest.mark.parametrize('refusal', [400, 401, 402, 403, 404, 413, 415, 422])
def test_fallback_pins_provider_across_restart(tmp_path, refusal):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            if request.method == 'POST' and request.url.host == 'bettertoken.example':
                return httpx.Response(refusal, json={'error': 'refused'})
            if request.method == 'POST':
                assert json.loads(request.content)['model'] == 'alibaba/wan-3.0'
                return httpx.Response(200, json={'id': 'upstream-1', 'status': 'queued', 'polling_url': 'upstream'})
            if request.url.path.endswith('/content'):
                return httpx.Response(200, content=b'video-bytes', headers={'content-type': 'video/mp4'})
            return httpx.Response(200, json={'id': 'upstream-1', 'status': 'failed', 'error': {'message': 'generation failed'}})
        async with video_client(tmp_path, handler) as (client, _, events):
            created = await client.post('/v1/videos', json=BODY)
            assert created.status_code == 200, created.text
            task = created.json()
            assert task['polling_url'] == f"/v1/videos/{task['id']}"
            assert task['model'] == MODEL
            assert events == []
        async with video_client(tmp_path, handler) as (client, _, events):
            status = await client.get(task['polling_url'])
            assert status.json()['status'] == 'failed'
            content = await client.get(task['content_url'])
            assert content.content == b'video-bytes'
            assert events == []
        assert [r.url.host for r in calls] == ['bettertoken.example', 'routerai.example', 'routerai.example', 'routerai.example']
    asyncio.run(scenario())



def test_status_hides_provider_download_links(tmp_path):
    # Ссылки провайдера требуют его ключа: клиент шлюза качает только через content_url.
    async def scenario():
        def handler(request):
            if request.method == 'POST':
                return httpx.Response(200, json={'id': 'upstream-1', 'status': 'queued'})
            return httpx.Response(200, json={'id': 'upstream-1', 'status': 'completed',
                                             'unsigned_urls': ['https://routerai.example/v1/videos/upstream-1/content']})
        async with video_client(tmp_path, handler) as (client, _, _):
            task = (await client.post('/v1/videos', json=BODY)).json()
            status = (await client.get(task['polling_url'])).json()
            assert 'unsigned_urls' not in status
            assert status['content_url'] == f"/v1/videos/{task['id']}/content"
    asyncio.run(scenario())

@pytest.mark.parametrize('failure', [409, 429, 500, 502, 503, 'timeout', 'bad_json', 'no_id', 'error_with_id'])
def test_uncertain_create_never_falls_back(tmp_path, failure):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            if failure == 'timeout':
                raise httpx.ReadTimeout('timeout', request=request)
            if failure == 'bad_json':
                return httpx.Response(200, content=b'not-json')
            if failure == 'no_id':
                return httpx.Response(200, json={'status': 'queued'})
            if failure == 'error_with_id':
                return httpx.Response(400, json={'id': 'accepted', 'status': 'failed'})
            return httpx.Response(failure, json={'error': 'uncertain'})
        async with video_client(tmp_path, handler) as (client, services, events):
            result = await client.post('/v1/videos', json=BODY)
            assert result.status_code >= 400
            assert len(calls) == 1
            assert events == []
            services.accounting_service.release.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('shape', ['nested', 'top_level', 'string', 'text', 'long'])
def test_creation_errors_record_provider_diagnostics(tmp_path, shape):
    async def scenario():
        def handler(request):
            provider = request.url.host.split('.')[0]
            code = f'{provider}_invalid_media'
            message = f'{provider}: reference image is unavailable'
            if shape in ('nested', 'long'):
                if shape == 'long':
                    message += 'x' * 1000
                return httpx.Response(422, json={'error': {'code': code, 'message': message}})
            if shape == 'top_level':
                return httpx.Response(422, json={'code': code, 'message': message})
            if shape == 'string':
                return httpx.Response(422, json={'error': message})
            return httpx.Response(422, text=message)

        async with video_client(tmp_path, handler) as (client, services, _events):
            insert = services.fallback_events_db.insert_event
            response = await client.post('/v1/videos', json=BODY)
            assert response.status_code == 422
            assert response.json()['detail'] == 'Video provider could not create the task'
            assert insert.call_count == 2
            for call, provider in zip(insert.call_args_list, ('bettertoken', 'routerai')):
                event = call.kwargs
                assert event['provider'] == provider
                assert event['success'] is False
                assert 'HTTP 422' in event['error_message']
                assert f'{provider}: reference image is unavailable' in event['error_message']
                if shape in ('nested', 'top_level', 'long'):
                    assert f'code={provider}_invalid_media' in event['error_message']
                if shape == 'long':
                    assert len(event['error_message']) == 500
    asyncio.run(scenario())


def test_bettertoken_job_billing_and_idempotent_creation(tmp_path, monkeypatch):
    monkeypatch.setenv('GATEWAY_DB_DIR', str(tmp_path))
    usage_db = TokensUsageDB('video-usage.db')
    async def scenario():
        calls = []
        settled = {'id': 'bt-1', 'status': 'completed', 'delivery_status': 'ready',
                   'billing': {'currency': 'USD', 'net_amount': '0.47'}}
        def handler(request):
            calls.append(request)
            if request.method == 'POST':
                assert request.headers['idempotency-key'] == 'same-request'
                payload = json.loads(request.content)
                assert payload['operation'] == 'text_to_video'
                return httpx.Response(202, json={'id': 'bt-1', 'status': 'queued'})
            return httpx.Response(200, json=settled)
        async with video_client(tmp_path, handler) as (client, _, events):
            first = await client.post('/v1/videos', json=BODY, headers={'Idempotency-Key': 'same-request'})
            second = await client.post('/v1/videos', json=BODY, headers={'Idempotency-Key': 'same-request'})
            assert first.json()['id'] == second.json()['id']
            poll = first.json()['polling_url']
            assert (await client.get(poll)).status_code == 200
            assert (await client.get(poll)).status_code == 200
            assert len(events) == 2
            assert events[0].event_id == events[1].event_id
            assert events[0].billing_fingerprint == events[1].billing_fingerprint
            assert events[0].usage.cost == 0.47
            assert events[0].provider == 'bettertoken'
            assert events[0].cost_source == CostSource.UPSTREAM
            assert usage_db.accept_accounting_event(events[0]).status == SourceStatus.ACCEPTED
            assert usage_db.accept_accounting_event(events[1]).status == SourceStatus.DUPLICATE
        # Reopening storage keeps both the provider and immutable billing identity.
        async with video_client(tmp_path, handler) as (client, _, events):
            assert (await client.get(poll)).status_code == 200
            assert events[0].usage.cost == 0.47
            assert usage_db.accept_accounting_event(events[0]).status == SourceStatus.DUPLICATE
        async with video_client(tmp_path, handler, key_id=8) as (client, _, _):
            before = len(calls)
            assert (await client.get(poll)).status_code == 404
            assert (await client.get(first.json()['content_url'])).status_code == 404
            assert len(calls) == before
    asyncio.run(scenario())


@pytest.mark.parametrize('usage', [None, {'currency': 'RUB', 'net_amount': '100'}, {'currency': 'USD', 'net_amount': None}])
def test_missing_usd_settlement_has_no_invented_charge(tmp_path, usage):
    async def scenario():
        def handler(request):
            return httpx.Response(200, json={'id': 'done', 'status': 'completed', 'billing': usage})
        async with video_client(tmp_path, handler) as (client, services, events):
            assert (await client.post('/v1/videos', json=BODY)).status_code == 200
            assert events == []
            services.accounting_service.release.assert_awaited_once()
    asyncio.run(scenario())


def test_cost_resolver_zero_priority_and_invalid_values():
    assert video_cost_usd({'usage': {'cost': 0}, 'billing': {'currency': 'USD', 'net_amount': '10'}}) == 0
    for value in (-1, float('nan'), True, 'broken'):
        with pytest.raises((ValueError, TypeError)):
            video_cost_usd({'usage': {'cost': value}})


def test_video_config_roundtrip_and_model_metadata():
    raw = {'videos_generations': [{'gateway_model_name': MODEL, 'routes': ROUTES}]}
    loader = ConfigLoader()
    loader.operation_rules = loader._build_operation_config(raw)
    assert loader.operation_rules['videos_generations'][MODEL]['routes'][0]['provider'] == 'bettertoken'
    structured = _build_structured_operation_rules_response(loader)
    assert structured['videos_generations'][0]['gateway_model_name'] == MODEL
    serialized = json.loads(_serialize_structured_operation_rules(ModelsOperationConfig.model_validate(structured)))
    assert [r['provider'] for r in serialized['videos_generations'][0]['routes']] == ['bettertoken', 'routerai']
    models = {}
    _add_gateway_operation_models(models, RULES)
    assert models[MODEL]['type'] == 'video'
    assert models[MODEL]['architecture']['output_modalities'] == ['video']
