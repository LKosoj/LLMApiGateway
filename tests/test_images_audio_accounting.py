from __future__ import annotations

import asyncio
import io
import json
from contextlib import ExitStack, asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from llm_gateway_core.api.v1 import audio, images
from llm_gateway_core.api.v1.audio_adapters import AudioAdapterResponse
from llm_gateway_core.api.v1.image_adapters import ImageDownstreamResponseError
from llm_gateway_core.api.v1.operation_runtime import ValidatedUpload
from llm_gateway_core.config.loader import (
    OperationRoute,
    REQUEST_FORMAT_NVIDIA_RIVA_GRPC,
)
from llm_gateway_core.middleware.accounting_admission import (
    ACCOUNTING_REQUEST_CONTEXT_STATE_KEY,
    AccountingRequestContext,
)
from llm_gateway_core.middleware.response_observation import (
    ResponseObservationMiddleware,
)
from llm_gateway_core.services.accounting import (
    DEFAULT_OPERATION_COST_USD,
    AccountingReceipt,
    AccountingReservation,
    CostSource,
    OperationCostCalculator,
    ProjectionStatus,
    SourceStatus,
    classify_billing_policy,
)
from llm_gateway_core.services.request_handler import OperationDispatcher
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


def _route(
    provider: str = "provider-a",
    model: str = "model-a",
    *,
    request_format: str | None = None,
) -> OperationRoute:
    return OperationRoute(
        provider=provider,
        model=model,
        target_path=f"/{model}",
        request_format=request_format,
    )


def _request(
    path: str,
    *,
    calculators: dict[tuple[str, str], OperationCostCalculator] | None = None,
) -> tuple[Request, object]:
    policy = classify_billing_policy("POST", path)
    assert policy is not None
    context = AccountingRequestContext(
        method="POST",
        route_template=path,
        policy=policy,
        request_id="request-1",
        reservation=AccountingReservation(
            reservation_id="reservation-1",
            request_id="request-1",
            api_key_id=7,
            reserved_usd=1.0,
        ),
    )
    services = make_app_services()
    services.accounting_service.release.return_value = True
    snapshot = make_runtime_snapshot(
        operation_cost_calculator_registry=calculators or {},
    )
    app = FastAPI()
    app.state.services = services
    state = {
        ACCOUNTING_REQUEST_CONTEXT_STATE_KEY: context,
        "llmgateway_request_id": "request-1",
        "runtime_snapshot": snapshot,
    }
    return (
        Request(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "root_path": "",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 1234),
                "server": ("testserver", 80),
                "app": app,
                "state": state,
            }
        ),
        services,
    )


def _install_successful_commit(service, *, before_return=None) -> None:
    async def commit(_reservation, event):
        if before_return is not None:
            before_return(event)
        return AccountingReceipt(
            source_status=SourceStatus.ACCEPTED,
            projection_status=ProjectionStatus.APPLIED,
            event_id=event.event_id,
            billing_fingerprint=event.billing_fingerprint,
            usage_row_id=1,
        )

    service.commit.side_effect = commit


def _response_json(response) -> object:
    return json.loads(response.body)


async def _send_response(
    request: Request,
    response,
    *,
    before_send=None,
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []

    async def app(scope, receive, send) -> None:
        await response(scope, receive, send)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if before_send is not None:
            before_send(message)
        messages.append(message)

    await ResponseObservationMiddleware(app)(request.scope, receive, send)
    return messages


def _wire_body(messages: list[dict[str, object]]) -> bytes:
    return b"".join(
        bytes(message.get("body", b""))
        for message in messages
        if message.get("type") == "http.response.body"
    )


def _assert_no_legacy_writes(services) -> None:
    services.tokens_usage_db.insert_usage.assert_not_called()
    services.tokens_usage_db.insert_usage_once.assert_not_called()
    services.api_keys_db.record_spent.assert_not_called()


@contextmanager
def _image_dependencies(
    request_body: dict[str, object],
    routes: list[OperationRoute],
    downstream_result: object,
    normalized_response: object,
):
    dispatcher = Mock(spec=OperationDispatcher)
    dispatcher.lookup_routes.return_value = routes
    config_loader = SimpleNamespace(
        providers_config={route.provider: object() for route in routes}
    )
    execute = AsyncMock()
    if isinstance(downstream_result, list):
        execute.side_effect = downstream_result
    elif isinstance(downstream_result, BaseException):
        execute.side_effect = downstream_result
    else:
        execute.return_value = downstream_result
    normalize = Mock()
    if isinstance(normalized_response, BaseException):
        normalize.side_effect = normalized_response
    else:
        normalize.return_value = normalized_response
    prepare = AsyncMock(return_value=("https://provider.example/operation", {}, (0, 0.0)))
    prepared_request = SimpleNamespace(
        transport="json",
        json_payload={},
        multipart_data=None,
        multipart_files=None,
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                images,
                "read_json_request_body",
                AsyncMock(return_value=request_body),
            )
        )
        stack.enter_context(patch.object(images, "enforce_virtual_key_access"))
        stack.enter_context(
            patch.object(
                images,
                "_get_operation_runtime",
                return_value=(dispatcher, Mock(), config_loader, {}),
            )
        )
        stack.enter_context(patch.object(images, "_prepare_route_request", prepare))
        stack.enter_context(
            patch.object(
                images,
                "build_downstream_image_request",
                return_value=prepared_request,
            )
        )
        stack.enter_context(
            patch.object(images, "_execute_prepared_request", execute)
        )
        stack.enter_context(
            patch.object(images, "normalize_downstream_image_response", normalize)
        )
        yield execute, normalize, prepare


async def _call_image_endpoint(path: str, request: Request):
    if path == "/v1/images":
        return await images.create_images(request)
    if path == "/v1/images/generations":
        return await images.create_images_generation(request)
    if path == "/v1/images/edits":
        return await images.create_images_edit(request)
    raise AssertionError(path)


@pytest.mark.parametrize(
    (
        "path",
        "request_body",
        "upstream_payload",
        "calculators",
        "expected_cost",
        "expected_source",
    ),
    [
        (
            "/v1/images",
            {"model": "gateway-image", "prompt": "draw"},
            {"data": [], "usage": {"cost": 0}},
            {},
            0.0,
            CostSource.UPSTREAM,
        ),
        (
            "/v1/images/generations",
            {"model": "gateway-image", "prompt": "draw"},
            {"data": []},
            {
                ("images_generation", "gateway-image"): OperationCostCalculator(
                    unit="operation",
                    rate_usd=0.25,
                )
            },
            0.25,
            CostSource.OPERATION_CONFIGURED,
        ),
        (
            "/v1/images/edits",
            {
                "model": "gateway-image",
                "prompt": "edit",
                "images": ["data:image/png;base64,AA=="],
            },
            {"data": []},
            {},
            DEFAULT_OPERATION_COST_USD,
            CostSource.OPERATION_DEFAULT,
        ),
    ],
)
def test_image_endpoints_commit_normalized_success_with_flat_cost_precedence(
    path: str,
    request_body: dict[str, object],
    upstream_payload: dict[str, object],
    calculators: dict[tuple[str, str], OperationCostCalculator],
    expected_cost: float,
    expected_source: CostSource,
) -> None:
    async def scenario() -> None:
        request, services = _request(path, calculators=calculators)
        normalized = {"created": 1, "data": [{"b64_json": "AA=="}]}
        response_started = False
        with _image_dependencies(
            request_body,
            [_route()],
            (upstream_payload, 201),
            normalized,
        ) as (_, normalize, _):
            def before_commit(_event) -> None:
                assert response_started is False
                normalize.assert_called_once()

            _install_successful_commit(
                services.accounting_service,
                before_return=before_commit,
            )
            response = await _call_image_endpoint(path, request)

        assert isinstance(response, JSONResponse)
        assert response.status_code == 201
        assert _response_json(response) == normalized
        services.accounting_service.commit.assert_not_awaited()

        def before_send(message: dict[str, object]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True

        messages = await _send_response(
            request,
            response,
            before_send=before_send,
        )

        assert _wire_body(messages) == response.body
        event = services.accounting_service.commit.await_args.args[1]
        assert event.usage.cost == expected_cost
        assert event.cost_source is expected_source
        assert event.usage.duration_ms is not None
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_image_fallback_commits_once_from_final_success() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/images/generations")
        _install_successful_commit(services.accounting_service)
        routes = [
            _route("provider-a", "model-a"),
            _route("provider-b", "model-b"),
        ]
        final_payload = {"data": [], "usage": {"cost": 0.7}}
        with _image_dependencies(
            {"model": "gateway-image", "prompt": "draw"},
            routes,
            [HTTPException(status_code=503, detail="primary failed"), (final_payload, 202)],
            {"data": [{"url": "https://example.test/image.png"}]},
        ) as (execute, _, prepare):
            response = await images.create_images_generation(request)

        assert response.status_code == 202
        assert execute.await_count == 2
        assert prepare.await_args_list[-1].args[2] is routes[-1]
        services.accounting_service.commit.assert_not_awaited()
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        event = services.accounting_service.commit.await_args.args[1]
        assert event.usage.cost == 0.7
        assert event.cost_source is CostSource.UPSTREAM
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


@pytest.mark.parametrize(
    ("downstream_result", "normalized_response", "expected_exception"),
    [
        (
            ({"data": []}, 200),
            ImageDownstreamResponseError("bad response"),
            HTTPException,
        ),
        (asyncio.CancelledError(), {}, asyncio.CancelledError),
    ],
)
def test_image_error_or_cancellation_releases_without_commit(
    downstream_result: object,
    normalized_response: object,
    expected_exception: type[BaseException],
) -> None:
    async def scenario() -> None:
        request, services = _request("/v1/images/generations")
        with _image_dependencies(
            {"model": "gateway-image", "prompt": "draw"},
            [_route()],
            downstream_result,
            normalized_response,
        ):
            with pytest.raises(expected_exception):
                await images.create_images_generation(request)

        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_image_invalid_upstream_cost_fails_closed_and_releases() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/images/generations")
        with _image_dependencies(
            {"model": "gateway-image", "prompt": "draw"},
            [_route()],
            ({"data": [], "usage": {"cost": "invalid"}}, 200),
            {"data": []},
        ):
            response = await images.create_images_generation(request)

        assert response.status_code == 503
        assert _response_json(response)["error"]["code"] == "accounting_unavailable"
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()
        _assert_no_legacy_writes(services)

    run_async(scenario())


@pytest.mark.parametrize("surface", ["image", "audio"])
def test_image_audio_missing_accounting_context_fails_before_upstream(
    surface: str,
) -> None:
    async def scenario() -> None:
        path = "/v1/images/generations" if surface == "image" else "/v1/audio/speech"
        request, services = _request(path)
        request.scope["state"].pop(ACCOUNTING_REQUEST_CONTEXT_STATE_KEY)

        if surface == "image":
            with _image_dependencies(
                {"model": "gateway-image", "prompt": "draw"},
                [_route()],
                ({"data": []}, 200),
                {"data": []},
            ) as (execute, _normalize, _prepare):
                response = await images.create_images_generation(request)
            execute.assert_not_awaited()
        else:
            route = _route(model="speech-model")
            with _audio_runtime([route]) as (stack, _dispatcher):
                stack.enter_context(
                    patch.object(
                        audio,
                        "_parse_audio_speech_request",
                        AsyncMock(
                            return_value=(
                                "gateway-speech",
                                {"model": "gateway-speech", "input": "hello"},
                            )
                        ),
                    )
                )
                prepare = stack.enter_context(
                    patch.object(
                        audio,
                        "_prepare_audio_json_http_request",
                        AsyncMock(return_value=("https://provider.example/speech", {})),
                    )
                )
                proxy = stack.enter_context(
                    patch.object(
                        audio,
                        "proxy_json_raw_to_downstream",
                        AsyncMock(return_value=(b"audio", 200, "audio/mpeg")),
                    )
                )
                response = await audio.create_audio_speech(request)
            prepare.assert_not_awaited()
            proxy.assert_not_awaited()

        assert response.status_code == 503
        assert _response_json(response)["error"]["code"] == "accounting_unavailable"
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


@contextmanager
def _audio_runtime(routes: list[OperationRoute]):
    dispatcher = Mock(spec=OperationDispatcher)
    dispatcher.lookup_route.return_value = routes[0] if routes else None
    dispatcher.lookup_routes.return_value = routes
    config_loader = SimpleNamespace(
        providers_config={
            route.provider: SimpleNamespace(baseUrl="https://provider.example", apikey=None)
            for route in routes
        }
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(audio, "enforce_virtual_key_access"))
        stack.enter_context(
            patch.object(
                audio,
                "_get_operation_runtime",
                return_value=(dispatcher, Mock(), config_loader, {}),
            )
        )
        stack.enter_context(
            patch.object(audio, "_prepare_audio_request_state", return_value=(0, 0.0))
        )
        yield stack, dispatcher


def test_audio_speech_binary_response_commits_configured_cost_before_return() -> None:
    async def scenario() -> None:
        calculators = {
            ("audio_speech", "gateway-speech"): OperationCostCalculator(
                unit="operation",
                rate_usd=0.33,
            )
        }
        request, services = _request("/v1/audio/speech", calculators=calculators)
        route = _route(model="speech-model")
        response_started = False
        with _audio_runtime([route]) as (stack, _):
            stack.enter_context(
                patch.object(
                    audio,
                    "_parse_audio_speech_request",
                    AsyncMock(
                        return_value=(
                            "gateway-speech",
                            {"model": "gateway-speech", "input": "hello"},
                        )
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    audio,
                    "_prepare_audio_json_http_request",
                    AsyncMock(return_value=("https://provider.example/speech", {})),
                )
            )
            proxy = stack.enter_context(
                patch.object(
                    audio,
                    "proxy_json_raw_to_downstream",
                    AsyncMock(return_value=(b"audio-bytes", 201, "audio/wav")),
                )
            )

            def before_commit(_event) -> None:
                proxy.assert_awaited_once()
                assert response_started is False

            _install_successful_commit(
                services.accounting_service,
                before_return=before_commit,
            )
            response = await audio.create_audio_speech(request)

        assert response.body == b"audio-bytes"
        assert response.status_code == 201
        assert response.headers["content-type"] == "audio/wav"
        services.accounting_service.commit.assert_not_awaited()

        def before_send(message: dict[str, object]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True

        messages = await _send_response(
            request,
            response,
            before_send=before_send,
        )
        assert _wire_body(messages) == response.body
        event = services.accounting_service.commit.await_args.args[1]
        assert event.usage.cost == 0.33
        assert event.cost_source is CostSource.OPERATION_CONFIGURED
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


@contextmanager
def _transcription_dependencies(
    routes: list[OperationRoute],
    downstream_result: object,
):
    @asynccontextmanager
    async def parsed_request(_request):
        yield (
            "gateway-transcribe",
            {"model": "gateway-transcribe"},
            ValidatedUpload(
                "sample.wav",
                "audio/wav",
                3,
                io.BytesIO(b"wav"),
            ),
        )

    with _audio_runtime(routes) as (stack, dispatcher):
        stack.enter_context(
            patch.object(
                audio,
                "_parse_audio_transcription_request",
                parsed_request,
            )
        )
        stack.enter_context(
            patch.object(
                audio,
                "_prepare_audio_http_request",
                AsyncMock(return_value=("https://provider.example/transcribe", {})),
            )
        )
        proxy = AsyncMock()
        if isinstance(downstream_result, list):
            proxy.side_effect = downstream_result
        elif isinstance(downstream_result, BaseException):
            proxy.side_effect = downstream_result
        else:
            proxy.return_value = downstream_result
        stack.enter_context(
            patch.object(audio, "proxy_multipart_raw_to_downstream", proxy)
        )
        yield stack, dispatcher, proxy


def test_audio_transcription_json_uses_upstream_zero_cost() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/audio/transcriptions")
        _install_successful_commit(services.accounting_service)
        payload = {"text": "hello", "usage": {"cost": 0}}
        body = json.dumps(payload).encode("utf-8")
        with _transcription_dependencies(
            [_route(model="transcribe-model")],
            (body, 202, "application/json"),
        ):
            response = await audio.create_audio_transcription(request)

        assert response.status_code == 202
        assert _response_json(response) == payload
        services.accounting_service.commit.assert_not_awaited()
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        event = services.accounting_service.commit.await_args.args[1]
        assert event.usage.cost == 0.0
        assert event.cost_source is CostSource.UPSTREAM
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_audio_transcription_binary_uses_synthesized_default_and_preserves_headers() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/audio/transcriptions")
        _install_successful_commit(services.accounting_service)
        with _transcription_dependencies(
            [_route(model="transcribe-model")],
            (b"plain transcript", 206, "text/plain; charset=utf-8"),
        ):
            response = await audio.create_audio_transcription(request)

        assert response.body == b"plain transcript"
        assert response.status_code == 206
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        services.accounting_service.commit.assert_not_awaited()
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        event = services.accounting_service.commit.await_args.args[1]
        assert event.usage.cost == DEFAULT_OPERATION_COST_USD
        assert event.cost_source is CostSource.OPERATION_DEFAULT
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_audio_grpc_response_uses_synthesized_configured_cost() -> None:
    async def scenario() -> None:
        calculators = {
            ("audio_transcription", "gateway-transcribe"): OperationCostCalculator(
                unit="operation",
                rate_usd=0.45,
            )
        }
        request, services = _request(
            "/v1/audio/transcriptions",
            calculators=calculators,
        )
        route = _route(
            provider="nvidia",
            model="riva-model",
            request_format=REQUEST_FORMAT_NVIDIA_RIVA_GRPC,
        )
        _install_successful_commit(services.accounting_service)
        with _transcription_dependencies([route], (b"unused", 200, "text/plain")) as (
            stack,
            _,
            proxy,
        ):
            adapter = AudioAdapterResponse(
                body={"text": "hello", "usage": {"cost": 99}},
                content_type="application/json",
                status_code=207,
            )
            stack.enter_context(
                patch.object(
                    audio,
                    "transcribe_with_nvidia_riva_grpc",
                    AsyncMock(return_value=adapter),
                )
            )
            stack.enter_context(
                patch.object(audio, "resolve_provider_config_api_key", return_value=None)
            )
            response = await audio.create_audio_transcription(request)

        proxy.assert_not_awaited()
        assert response.status_code == 207
        assert _response_json(response) == adapter.body
        services.accounting_service.commit.assert_not_awaited()
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        event = services.accounting_service.commit.await_args.args[1]
        assert event.usage.cost == 0.45
        assert event.cost_source is CostSource.OPERATION_CONFIGURED
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_audio_transcription_fallback_commits_only_final_success() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/audio/transcriptions")
        _install_successful_commit(services.accounting_service)
        routes = [
            _route("provider-a", "model-a"),
            _route("provider-b", "model-b"),
        ]
        payload = {"text": "ok", "usage": {"cost": 0.6}}
        with _transcription_dependencies(
            routes,
            [
                HTTPException(status_code=503, detail="primary failed"),
                (json.dumps(payload).encode(), 200, "application/json"),
            ],
        ) as (_, dispatcher, proxy):
            response = await audio.create_audio_transcription(request)

        assert response.status_code == 200
        assert proxy.await_count == 2
        assert dispatcher.lookup_routes.return_value[-1] is routes[-1]
        services.accounting_service.commit.assert_not_awaited()
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        event = services.accounting_service.commit.await_args.args[1]
        assert event.usage.cost == 0.6
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_audio_cancellation_releases_without_commit() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/audio/transcriptions")
        with _transcription_dependencies(
            [_route(model="transcribe-model")],
            asyncio.CancelledError(),
        ):
            with pytest.raises(asyncio.CancelledError):
                await audio.create_audio_transcription(request)

        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()
        _assert_no_legacy_writes(services)

    run_async(scenario())
