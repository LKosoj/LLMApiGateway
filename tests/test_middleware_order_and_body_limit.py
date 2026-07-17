import asyncio
import gc
import unittest
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import Mock, create_autospec, patch

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import main
from llm_gateway_core.db.api_keys_db import ApiKeyRecord, ApiKeysDB
from llm_gateway_core.middleware.content_size import (
    ContentLengthLimitMiddleware,
    ContentSizeLimitMiddleware,
    LOGIN_MAX_REQUEST_BODY_BYTES,
    PRE_ROUTE_REJECTED_STATE_KEY,
    UNMATCHED_ROUTE_STATE_KEY,
)
from llm_gateway_core.services.runtime_config import (
    AppServices,
    RuntimeManagerStateError,
)
from tests._async_compat import run_async
from tests.runtime_test_support import installed_runtime


class _BodyProbe:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def __len__(self) -> int:
        return len(self.value)

    def __getitem__(self, item):
        return self.value[item]


class _LengthOnlyBodyProbe:
    def __init__(self, size: int) -> None:
        self.size = size

    def __len__(self) -> int:
        return self.size


class _WeakBytearray(bytearray):
    pass


@asynccontextmanager
async def _test_runtime_lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with installed_runtime(app):
        yield


def build_gateway_middleware_test_app(
    *,
    cors_allow_origins: list[str] | None = None,
    max_request_body_bytes: int = 1024,
) -> FastAPI:
    app = FastAPI(lifespan=_test_runtime_lifespan)
    app.state.test_route_calls = 0

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        app.state.test_route_calls += 1
        return {"bytes": len(await request.body())}

    main.configure_gateway_middleware(
        app,
        cors_allow_origins=cors_allow_origins,
        max_request_body_bytes=max_request_body_bytes,
    )
    return app


@asynccontextmanager
async def _install_budget_virtual_key(
    app: FastAPI,
) -> AsyncIterator[tuple[ApiKeyRecord, AppServices]]:
    record = ApiKeyRecord(
        id=17,
        name="body-admission-key",
        api_key="budget-virtual-key",
        budget_usd=100.0,
        spent_usd=0.0,
        rpm=None,
        tpm=None,
        disabled=False,
    )
    api_keys_db = create_autospec(ApiKeysDB, instance=True, spec_set=True)
    api_keys_db.get_by_key.side_effect = (
        lambda token: record if token == record.api_key else None
    )
    api_keys_db.get_by_id.side_effect = (
        lambda key_id: record if key_id == record.id else None
    )
    async with installed_runtime(app, api_keys_db=api_keys_db):
        services: AppServices = app.state.services
        yield record, services


class MiddlewareOrderAndBodyLimitTests(unittest.TestCase):
    def test_registered_middleware_order_matches_admission_contract(self):
        app = build_gateway_middleware_test_app(
            cors_allow_origins=["https://app.example"]
        )

        middleware_names = []
        for middleware in app.user_middleware:
            dispatch = middleware.kwargs.get("dispatch")
            middleware_names.append(
                dispatch.__name__ if dispatch is not None else middleware.cls.__name__
            )

        self.assertEqual(
            middleware_names,
            [
                "CORSMiddleware",
                "RequestLoggingASGIMiddleware",
                "ContentLengthLimitMiddleware",
                "RuntimeAvailabilityMiddleware",
                "ApiKeyAuthMiddleware",
                "ContentSizeLimitMiddleware",
                "RuntimeSnapshotMiddleware",
                "GZipMiddleware",
                "ResponseObservationMiddleware",
                "add_routing_diagnostic_headers",
                "add_anthropic_version_header",
            ],
        )

    def test_content_limit_passes_non_http_scope_through_unchanged(self):
        async def scenario() -> None:
            scope = {"type": "websocket", "path": "/socket"}
            receive = Mock()
            send = Mock()
            observed = []

            async def downstream(actual_scope, actual_receive, actual_send):
                observed.append((actual_scope, actual_receive, actual_send))

            middleware = ContentSizeLimitMiddleware(downstream, max_body_size=5)
            await middleware(scope, receive, send)
            self.assertEqual(observed, [(scope, receive, send)])

        run_async(scenario())

    def test_bodyless_and_public_non_body_routes_do_not_preread_slow_bodies(self):
        app = FastAPI(lifespan=_test_runtime_lifespan)
        route_calls = {"health": 0, "root": 0, "static": 0}

        @app.get("/health")
        async def health():
            route_calls["health"] += 1
            return {"status": "ok"}

        @app.get("/")
        async def root():
            route_calls["root"] += 1
            return {"root": True}

        @app.get("/static/test.js")
        async def static_asset():
            route_calls["static"] += 1
            return {"static": True}

        main.configure_gateway_middleware(
            app,
            max_request_body_bytes=100 * 1024 * 1024,
        )
        slow_body = [
            {
                "type": "http.request",
                "body": b"must-not-be-read",
                "more_body": True,
            }
        ]
        cases = (
            ("POST", "/health", 405),
            ("GET", "/health", 200),
            ("POST", "/", 405),
            ("POST", "/static", 404),
            ("POST", "/static/test.js", 405),
        )

        with patch.object(
            ContentSizeLimitMiddleware,
            "_append_body_blocks",
            side_effect=AssertionError("bodyless/public route must not buffer"),
        ):
            for method, path, expected_status in cases:
                with self.subTest(method=method, path=path):
                    response = run_async(
                        _run_asgi_request_with_runtime(
                            app,
                            slow_body,
                            headers=[],
                            path=path,
                            method=method,
                        )
                    )
                    self.assertEqual(response["status"], expected_status)
                    self.assertEqual(response["receive_count"], 0)

        self.assertEqual(route_calls, {"health": 1, "root": 0, "static": 0})

    def test_unmatched_routes_skip_body_admission_and_runtime_lease(self):
        app = build_gateway_middleware_test_app(
            max_request_body_bytes=100 * 1024 * 1024
        )

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        cases = (
            ("/does-not-exist", 404),
            ("/v1/chat/completions/typo", 404),
            ("/health/", 307),
        )

        for path, expected_status in cases:
            with self.subTest(path=path):
                response = run_async(
                    _run_asgi_request_with_runtime(
                        app,
                        [
                            {
                                "type": "http.request",
                                "body": b"must-not-be-read",
                                "more_body": True,
                            }
                        ],
                        headers=[],
                        path=path,
                    )
                )

                self.assertEqual(response["status"], expected_status)
                self.assertEqual(response["receive_count"], 0)
                self.assertEqual(response["acquire_count"], 0)
                self.assertTrue(response["state"][UNMATCHED_ROUTE_STATE_KEY])
                self.assertNotIn("gateway_authenticated", response["state"])

    def test_login_uses_strict_small_body_admission_limit(self):
        app = FastAPI(lifespan=_test_runtime_lifespan)
        route_bodies = []

        @app.post("/auth/login")
        async def login(request: Request):
            route_bodies.append(await request.body())
            return {"accepted": True}

        main.configure_gateway_middleware(
            app,
            max_request_body_bytes=100 * 1024 * 1024,
        )
        exact_body = b"x" * LOGIN_MAX_REQUEST_BODY_BYTES
        accepted = run_async(
            _run_asgi_request_with_runtime(
                app,
                [{"type": "http.request", "body": exact_body}],
                headers=[],
                path="/auth/login",
            )
        )
        self.assertEqual(accepted["status"], 200)
        self.assertEqual(route_bodies, [exact_body])

        max_retained = 0
        append_body_blocks = ContentSizeLimitMiddleware._append_body_blocks

        def tracking_append(blocks, body, block_size):
            nonlocal max_retained
            append_body_blocks(blocks, body, block_size)
            max_retained = max(max_retained, sum(len(block) for block in blocks))

        with patch.object(
            ContentSizeLimitMiddleware,
            "_append_body_blocks",
            side_effect=tracking_append,
        ):
            chunked_overflow = run_async(
                _run_asgi_request_with_runtime(
                    app,
                    [
                        {
                            "type": "http.request",
                            "body": exact_body,
                            "more_body": True,
                        },
                        {"type": "http.request", "body": b"+"},
                    ],
                    headers=[],
                    path="/auth/login",
                )
            )

        self.assertEqual(chunked_overflow["status"], 413)
        self.assertTrue(
            chunked_overflow["state"][PRE_ROUTE_REJECTED_STATE_KEY]
        )
        self.assertLessEqual(max_retained, LOGIN_MAX_REQUEST_BODY_BYTES)
        self.assertEqual(route_bodies, [exact_body])

        declared_overflow = run_async(
            _run_asgi_request_with_runtime(
                app,
                [
                    {
                        "type": "http.request",
                        "body": b"must-not-be-read",
                        "more_body": True,
                    }
                ],
                headers=[
                    (
                        b"content-length",
                        str(LOGIN_MAX_REQUEST_BODY_BYTES + 1).encode("ascii"),
                    )
                ],
                path="/auth/login",
            )
        )
        self.assertEqual(declared_overflow["status"], 413)
        self.assertEqual(declared_overflow["receive_count"], 0)
        self.assertEqual(route_bodies, [exact_body])

        with patch.object(
            ContentSizeLimitMiddleware,
            "_append_body_blocks",
            side_effect=AssertionError("100 MiB login body must not be copied"),
        ):
            virtual_huge = run_async(
                _run_asgi_request_with_runtime(
                    app,
                    [
                        {
                            "type": "http.request",
                            "body": _LengthOnlyBodyProbe(100 * 1024 * 1024),
                        }
                    ],
                    headers=[],
                    path="/auth/login",
                )
            )
        self.assertEqual(virtual_huge["status"], 413)
        self.assertEqual(route_bodies, [exact_body])

    def test_cors_preflight_is_handled_before_auth(self):
        app = build_gateway_middleware_test_app(cors_allow_origins=["https://app.example"])

        with TestClient(app) as client:
            manager = client.app.state.services.runtime_manager
            with patch.object(
                manager,
                "acquire_current",
                wraps=manager.acquire_current,
            ) as acquire_current:
                response = client.options(
                    "/v1/chat/completions",
                    headers={
                        "Origin": "https://app.example",
                        "Access-Control-Request-Method": "POST",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "https://app.example")
        acquire_current.assert_not_called()

    def test_unauthorized_chat_request_is_rejected_before_chat_logging_reads_body(self):
        calls = []
        original_preparer = main.prepare_chat_response_observation

        async def fail_if_called(request):
            calls.append(request.url.path)
            raise AssertionError(
                "chat response preparation must not run before auth rejects the request"
            )

        main.prepare_chat_response_observation = fail_if_called
        try:
            app = build_gateway_middleware_test_app(max_request_body_bytes=1024)
        finally:
            main.prepare_chat_response_observation = original_preparer

        with TestClient(app) as client:
            manager = client.app.state.services.runtime_manager
            with patch.object(
                manager,
                "acquire_current",
                wraps=manager.acquire_current,
            ) as acquire_current:
                response = client.post(
                    "/v1/chat/completions",
                    content=b'{"model":"demo"}',
                )
                self.assertEqual(manager.active_leases[1], 0)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(calls, [])
        acquire_current.assert_not_called()

    def test_auth_rejection_keeps_request_id_header(self):
        app = build_gateway_middleware_test_app(max_request_body_bytes=1024)

        with TestClient(app) as client:
            response = client.post("/v1/chat/completions", content=b'{"model":"demo"}')

        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.headers.get("X-Request-ID"))

    def test_content_length_over_limit_returns_413_before_body_read(self):
        app = build_gateway_middleware_test_app(max_request_body_bytes=4)

        with TestClient(app) as client:
            manager = client.app.state.services.runtime_manager
            with patch.object(
                manager,
                "acquire_current",
                wraps=manager.acquire_current,
            ) as acquire_current:
                response = client.post(
                    "/v1/chat/completions",
                    content=b"12345",
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": "Request body too large"})
        acquire_current.assert_not_called()

    def test_missing_services_returns_safe_503_with_request_id_before_auth(self):
        route_calls = []
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def protected():
            route_calls.append("route")
            return {"ok": True}

        main.configure_gateway_middleware(
            app,
            max_request_body_bytes=1024,
        )

        response = run_async(
            _run_asgi_request(
                app,
                [
                    {
                        "type": "http.request",
                        "body": b"slow-body-must-not-be-read",
                        "more_body": True,
                    }
                ],
                headers=[],
            )
        )

        self.assertEqual(response["status"], 503)
        self.assertIn(b'"code":"runtime_unavailable"', response["body"])
        self.assertTrue(response["headers"].get("x-request-id"))
        self.assertEqual(response["receive_count"], 0)
        self.assertNotIn("gateway_authenticated", response["state"])
        self.assertEqual(route_calls, [])

    def test_invalid_auth_rejects_slow_chunked_body_without_receive_or_lease(self):
        app = build_gateway_middleware_test_app(max_request_body_bytes=5)

        auth_cases = (
            ([], 401),
            ([(b"authorization", b"Bearer invalid-key")], 403),
        )
        with patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key",
            "valid-key",
        ):
            for headers, expected_status in auth_cases:
                with self.subTest(expected_status=expected_status):
                    response = run_async(
                        _run_asgi_request_with_runtime(
                            app,
                            [
                                {
                                    "type": "http.request",
                                    "body": b"slow-body-must-not-be-read",
                                    "more_body": True,
                                }
                            ],
                            headers=headers,
                        )
                    )

                    self.assertEqual(response["status"], expected_status)
                    self.assertEqual(response["receive_count"], 0)
                    self.assertEqual(response["acquire_count"], 0)
                    self.assertFalse(response["state"]["gateway_authenticated"])
                    self.assertEqual(app.state.test_route_calls, 0)

    def test_known_content_length_overflow_precedes_runtime_auth_and_receive(self):
        app = build_gateway_middleware_test_app(max_request_body_bytes=5)

        response = run_async(
            _run_asgi_request_with_runtime(
                app,
                [
                    {
                        "type": "http.request",
                        "body": b"must-not-be-read",
                        "more_body": False,
                    }
                ],
                headers=[(b"content-length", b"6")],
            )
        )

        self.assertEqual(response["status"], 413)
        self.assertEqual(response["receive_count"], 0)
        self.assertEqual(response["acquire_count"], 0)
        self.assertNotIn("gateway_authenticated", response["state"])
        self.assertEqual(app.state.test_route_calls, 0)

    def test_authenticated_accepted_body_gets_lease_only_after_admission(self):
        route_observations: list[tuple[int, bytes]] = []
        app = FastAPI(lifespan=_test_runtime_lifespan)

        @app.post("/accepted")
        async def accepted(request: Request):
            manager = app.state.services.runtime_manager
            body = await request.body()
            route_observations.append((manager.active_leases[1], body))
            return {"size": len(body)}

        main.configure_gateway_middleware(
            app,
            max_request_body_bytes=5,
        )

        with patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key",
            "test-key",
        ):
            response = run_async(
                _run_asgi_request_with_runtime(
                    app,
                    [
                        {
                            "type": "http.request",
                            "body": b"12",
                            "more_body": True,
                        },
                        {
                            "type": "http.request",
                            "body": b"345",
                            "more_body": False,
                        },
                        {"type": "http.disconnect"},
                    ],
                    headers=[(b"authorization", b"Bearer test-key")],
                    path="/accepted",
                )
            )

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["acquire_count"], 1)
        self.assertTrue(response["state"]["gateway_authenticated"])
        self.assertEqual(route_observations, [(1, b"12345")])

    def test_streaming_response_holds_runtime_lease_until_final_body(self):
        observations: list[int] = []
        app = FastAPI(lifespan=_test_runtime_lifespan)

        async def stream_body():
            manager = app.state.services.runtime_manager
            observations.append(manager.active_leases[1])
            yield b"data: one\n\n"
            await asyncio.sleep(0)
            observations.append(manager.active_leases[1])
            yield b"data: two\n\n"

        @app.get("/stream")
        async def stream():
            return StreamingResponse(stream_body(), media_type="text/event-stream")

        main.configure_gateway_middleware(
            app,
            max_request_body_bytes=1024,
        )

        with (
            patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "test-key"),
            TestClient(app) as client,
        ):
            response = client.get(
                "/stream",
                headers={"Authorization": "Bearer test-key"},
            )
            manager = client.app.state.services.runtime_manager
            self.assertEqual(manager.active_leases[1], 0)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observations, [1, 1])

    def test_chunked_body_without_content_length_is_counted(self):
        body_messages = [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"45", "more_body": True},
            {"type": "http.request", "body": b"6", "more_body": False},
        ]
        response = run_async(_run_limited_asgi_request(body_messages, max_body_size=5))

        self.assertEqual(response["status"], 413)
        self.assertEqual(response["body"], b'{"detail": "Request body too large"}')

    def test_accepted_chunked_body_replays_same_bytes_then_delegates_disconnect(self):
        request_messages = [
            {"type": "http.request", "body": b"12", "more_body": True},
            {"type": "http.request", "body": b"345", "more_body": False},
        ]
        disconnect = {"type": "http.disconnect"}
        received = run_async(
            _collect_replayed_messages(
                [*request_messages, disconnect],
                max_body_size=5,
            )
        )

        replayed_requests = [
            message for message in received if message["type"] == "http.request"
        ]
        self.assertEqual(
            b"".join(message.get("body", b"") for message in replayed_requests),
            b"12345",
        )
        self.assertFalse(replayed_requests[-1]["more_body"])
        self.assertIs(received[-1], disconnect)

    def test_preread_coalesces_thousands_of_empty_and_tiny_chunks(self):
        expected_body = b"x" * 4_096
        body_messages = [
            {"type": "http.request", "body": b"", "more_body": True}
            for _index in range(5_000)
        ]
        body_messages.extend(
            {"type": "http.request", "body": b"x", "more_body": True}
            for _index in range(len(expected_body) - 1)
        )
        body_messages.append(
            {"type": "http.request", "body": b"x", "more_body": False}
        )
        body_messages.append({"type": "http.disconnect"})

        received = run_async(
            _collect_replayed_messages(body_messages, max_body_size=len(expected_body))
        )
        replayed_requests = [
            message for message in received if message["type"] == "http.request"
        ]

        self.assertLessEqual(len(replayed_requests), 1)
        self.assertEqual(
            b"".join(message.get("body", b"") for message in replayed_requests),
            expected_body,
        )
        self.assertEqual(received[-1]["type"], "http.disconnect")

    def test_preread_releases_original_body_before_downstream(self):
        async def scenario() -> None:
            body_refs = []

            def request_message():
                body = _BodyProbe(b"accepted")
                body_refs.append(weakref.ref(body))
                return {"type": "http.request", "body": body}

            async def receive():
                return request_message()

            async def downstream(_scope, replay_receive, _send):
                gc.collect()
                self.assertIsNone(body_refs[0]())
                replayed = await replay_receive()
                self.assertEqual(replayed["body"], b"accepted")

            middleware = ContentSizeLimitMiddleware(
                downstream,
                max_body_size=64,
            )
            await middleware(
                {"type": "http", "headers": []},
                receive,
                Mock(),
            )

        run_async(scenario())

    def test_preread_releases_oversized_body_before_sending_413(self):
        async def scenario() -> None:
            body_refs = []

            def request_message():
                body = _BodyProbe(b"oversized")
                body_refs.append(weakref.ref(body))
                return {"type": "http.request", "body": body}

            async def receive():
                return request_message()

            async def downstream(_scope, _receive, _send):
                raise AssertionError("oversized body must not reach downstream")

            async def send(_message):
                gc.collect()
                self.assertIsNone(body_refs[0]())

            middleware = ContentSizeLimitMiddleware(
                downstream,
                max_body_size=5,
            )
            await middleware(
                {"type": "http", "headers": []},
                receive,
                send,
            )

        run_async(scenario())

    def test_overflow_releases_buffered_blocks_before_blocking_413_send(self):
        async def scenario() -> None:
            block_refs = []
            send_started = asyncio.Event()
            release_send = asyncio.Event()
            messages = [
                {
                    "type": "http.request",
                    "body": b"12345",
                    "more_body": True,
                },
                {"type": "http.request", "body": b"6"},
            ]
            send_count = 0

            def append_weak_block(blocks, body, _block_size):
                block = _WeakBytearray(body)
                blocks.append(block)
                block_refs.append(weakref.ref(block))

            async def receive():
                return messages.pop(0)

            async def downstream(_scope, _receive, _send):
                raise AssertionError("overflow must not reach downstream")

            async def send(_message):
                nonlocal send_count
                send_count += 1
                if send_count == 1:
                    send_started.set()
                    await release_send.wait()

            middleware = ContentSizeLimitMiddleware(
                downstream,
                max_body_size=5,
            )
            with patch.object(
                ContentSizeLimitMiddleware,
                "_append_body_blocks",
                side_effect=append_weak_block,
            ):
                task = asyncio.create_task(
                    middleware(
                        {
                            "type": "http",
                            "method": "POST",
                            "path": "/v1/chat/completions",
                            "headers": [],
                        },
                        receive,
                        send,
                    )
                )
                await send_started.wait()
                try:
                    gc.collect()
                    self.assertTrue(block_refs)
                    self.assertTrue(all(block_ref() is None for block_ref in block_refs))
                    self.assertEqual(
                        sum(
                            len(block)
                            for block_ref in block_refs
                            if (block := block_ref()) is not None
                        ),
                        0,
                    )
                finally:
                    release_send.set()
                    await task

            self.assertEqual(send_count, 2)

        run_async(scenario())

    def test_malformed_or_lying_content_length_still_uses_actual_body_size(self):
        for raw_length in (b"invalid", b"1"):
            with self.subTest(content_length=raw_length):
                response = run_async(
                    _run_limited_asgi_request(
                        [
                            {
                                "type": "http.request",
                                "body": b"123456",
                                "more_body": False,
                            }
                        ],
                        max_body_size=5,
                        headers=[(b"content-length", raw_length)],
                    )
                )
                self.assertEqual(response["status"], 413)

    def test_exact_limit_missing_fields_and_adversarial_lengths(self):
        exact = run_async(
            _run_limited_asgi_request(
                [{"type": "http.request", "body": b"12345"}],
                max_body_size=5,
            )
        )
        over = run_async(
            _run_limited_asgi_request(
                [{"type": "http.request", "body": b"123456"}],
                max_body_size=5,
            )
        )
        missing_body = run_async(
            _run_limited_asgi_request(
                [{"type": "http.request"}],
                max_body_size=5,
            )
        )
        negative_length = run_async(
            _run_limited_asgi_request(
                [{"type": "http.request", "body": b"123456"}],
                max_body_size=5,
                headers=[(b"content-length", b"-1")],
            )
        )
        conflicting_lengths = run_async(
            _run_header_guard_asgi_request(
                max_body_size=5,
                headers=[
                    (b"content-length", b"0"),
                    (b"content-length", b"6"),
                ],
            )
        )

        self.assertEqual(exact["status"], 200)
        self.assertEqual(over["status"], 413)
        self.assertEqual(missing_body["status"], 200)
        self.assertEqual(negative_length["status"], 413)
        self.assertEqual(conflicting_lengths["status"], 413)

    def test_preread_receive_error_and_cancellation_preserve_original_exception(self):
        async def scenario(primary: BaseException, *, after_partial_body: bool) -> None:
            async def downstream(_scope, _receive, _send):
                raise AssertionError("downstream must not run")

            calls = 0

            async def receive():
                nonlocal calls
                calls += 1
                if after_partial_body and calls == 1:
                    return {
                        "type": "http.request",
                        "body": b"12",
                        "more_body": True,
                    }
                raise primary

            async def send(_message):
                raise AssertionError("response must not start")

            middleware = ContentSizeLimitMiddleware(downstream, max_body_size=5)
            with self.assertRaises(type(primary)) as raised:
                await middleware(
                    {"type": "http", "headers": []},
                    receive,
                    send,
                )
            self.assertIs(raised.exception, primary)

        for after_partial_body in (False, True):
            for primary in (
                RuntimeError("receive failed"),
                asyncio.CancelledError("receive cancelled"),
            ):
                with self.subTest(
                    exception_type=type(primary).__name__,
                    after_partial_body=after_partial_body,
                ):
                    run_async(
                        scenario(
                            primary,
                            after_partial_body=after_partial_body,
                        )
                    )

    def test_disconnect_before_terminal_body_is_replayed_in_order(self):
        first_request = {
            "type": "http.request",
            "body": b"partial",
            "more_body": True,
        }
        disconnect = {"type": "http.disconnect"}

        received = run_async(
            _collect_replayed_messages(
                [first_request, disconnect],
                max_body_size=64,
            )
        )

        self.assertEqual(received[0]["body"], b"partial")
        self.assertTrue(received[0]["more_body"])
        self.assertIs(received[1], disconnect)

        disconnect_only = {"type": "http.disconnect"}
        self.assertEqual(
            run_async(
                _collect_replayed_messages(
                    [disconnect_only],
                    max_body_size=64,
                )
            ),
            [disconnect_only],
        )

    def test_downstream_exception_identity_is_preserved_after_preread(self):
        primary = RuntimeError("downstream failure")

        async def scenario() -> None:
            async def downstream(_scope, _receive, _send):
                raise primary

            messages = [{"type": "http.request", "body": b"ok"}]

            async def receive():
                return messages.pop(0)

            async def send(_message):
                raise AssertionError("response must not start")

            middleware = ContentSizeLimitMiddleware(downstream, max_body_size=5)
            with self.assertRaises(RuntimeError) as raised:
                await middleware(
                    {"type": "http", "headers": []},
                    receive,
                    send,
                )
            self.assertIs(raised.exception, primary)

        run_async(scenario())

    def test_413_send_failures_preserve_original_exception(self):
        async def scenario(failure_index: int, primary: BaseException) -> None:
            send_count = 0

            async def downstream(_scope, _receive, _send):
                raise AssertionError("downstream must not run")

            async def receive():
                raise AssertionError("known oversized body must not be read")

            async def send(_message):
                nonlocal send_count
                send_count += 1
                if send_count == failure_index:
                    raise primary

            middleware = ContentLengthLimitMiddleware(downstream, max_body_size=5)
            with self.assertRaises(type(primary)) as raised:
                await middleware(
                    {
                        "type": "http",
                        "headers": [(b"content-length", b"6")],
                    },
                    receive,
                    send,
                )
            self.assertIs(raised.exception, primary)

        for failure_index in (1, 2):
            with self.subTest(failure_index=failure_index):
                run_async(
                    scenario(
                        failure_index,
                        RuntimeError(f"send failure {failure_index}"),
                    )
                )

    def test_full_stack_chunked_body_without_content_length_returns_413(self):
        app = build_gateway_middleware_test_app(
            max_request_body_bytes=5,
            cors_allow_origins=["https://app.example"],
        )
        body_messages = [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"45", "more_body": True},
            {"type": "http.request", "body": b"6", "more_body": False},
        ]

        with patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "test-gateway-key"):
            response = run_async(
                _run_asgi_request_with_runtime(
                    app,
                    body_messages,
                    headers=[
                        (b"authorization", b"Bearer test-gateway-key"),
                        (b"origin", b"https://app.example"),
                    ],
                )
            )

        self.assertEqual(response["status"], 413)
        self.assertEqual(response["body"], b'{"detail": "Request body too large"}')
        self.assertEqual(response["acquire_count"], 0)
        self.assertEqual(response["receive_count"], 3)
        self.assertEqual(
            response["headers"]["access-control-allow-origin"],
            "https://app.example",
        )
        self.assertTrue(response["headers"].get("x-request-id"))
        self.assertTrue(response["state"]["gateway_authenticated"])
        self.assertEqual(app.state.test_route_calls, 0)

    def test_authenticated_pre_route_rejections_release_budget_and_active_request(self):
        async def scenario() -> None:
            app = build_gateway_middleware_test_app(max_request_body_bytes=5)
            async with _install_budget_virtual_key(app) as (record, services):
                ledger = services.usd_budget_ledger
                registry = services.active_requests_registry
                overflow = await _run_asgi_request(
                    app,
                    [
                        {
                            "type": "http.request",
                            "body": b"12345",
                            "more_body": True,
                        },
                        {"type": "http.request", "body": b"6"},
                    ],
                    headers=[
                        (
                            b"authorization",
                            f"Bearer {record.api_key}".encode("ascii"),
                        )
                    ],
                )

                self.assertEqual(overflow["status"], 413)
                self.assertTrue(overflow["state"][PRE_ROUTE_REJECTED_STATE_KEY])
                self.assertTrue(overflow["state"]["llmgateway_active_request_id"])
                services.accounting_service.reserve.assert_awaited_once()
                services.accounting_service.release.assert_awaited_once()
                self.assertEqual(ledger.reserved_for(record.id), 0.0)
                self.assertEqual(registry.list_records(), [])

                manager = services.runtime_manager
                with patch.object(
                    manager,
                    "acquire_current",
                    side_effect=RuntimeManagerStateError("late runtime race"),
                ):
                    unavailable = await _run_asgi_request(
                        app,
                        [{"type": "http.request", "body": b"ok"}],
                        headers=[
                            (
                                b"authorization",
                                f"Bearer {record.api_key}".encode("ascii"),
                            )
                        ],
                    )

                self.assertEqual(unavailable["status"], 503)
                self.assertTrue(unavailable["state"][PRE_ROUTE_REJECTED_STATE_KEY])
                self.assertTrue(
                    unavailable["state"]["llmgateway_active_request_id"]
                )
                self.assertEqual(
                    services.accounting_service.reserve.await_count,
                    2,
                )
                self.assertEqual(
                    services.accounting_service.release.await_count,
                    2,
                )
                self.assertEqual(ledger.reserved_for(record.id), 0.0)
                self.assertEqual(registry.list_records(), [])

        run_async(scenario())

    def test_functional_auth_adapter_cleans_up_and_preserves_downstream_failure(self):
        async def scenario(stage: str, failure: BaseException) -> None:
            app = FastAPI()

            @app.post("/v1/chat/completions")
            async def chat():
                return {"unused": True}

            async with _install_budget_virtual_key(app) as (record, services):
                async def receive():
                    if stage == "receive":
                        raise failure
                    return {"type": "http.request", "body": b""}

                request = Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/v1/chat/completions",
                        "raw_path": b"/v1/chat/completions",
                        "query_string": b"",
                        "headers": [
                            (
                                b"authorization",
                                f"Bearer {record.api_key}".encode("ascii"),
                            )
                        ],
                        "client": ("testclient", 50000),
                        "server": ("testserver", 80),
                        "scheme": "http",
                        "app": app,
                        "state": {"llmgateway_request_id": "functional-request"},
                    },
                    receive=receive,
                )

                async def call_next(actual_request):
                    if stage == "receive":
                        await actual_request.receive()

                    async def send(_message):
                        raise failure

                    await send({"type": "http.response.start"})
                    raise AssertionError("failing send must not return")

                with self.assertRaises(type(failure)) as raised:
                    await main.api_key_auth(request, call_next)
                self.assertIs(raised.exception, failure)

                self.assertTrue(request.state.llmgateway_active_request_id)
                services.accounting_service.reserve.assert_awaited_once()
                services.accounting_service.release.assert_awaited_once()
                self.assertEqual(
                    services.usd_budget_ledger.reserved_for(record.id),
                    0.0,
                )
                self.assertEqual(
                    services.active_requests_registry.list_records(),
                    [],
                )

        for stage in ("receive", "send"):
            for failure in (
                RuntimeError(f"{stage} failed"),
                asyncio.CancelledError(f"{stage} cancelled"),
            ):
                with self.subTest(stage=stage, failure=type(failure).__name__):
                    run_async(scenario(stage, failure))

    def test_registered_stack_admission_failures_preserve_identity_and_cleanup(self):
        async def scenario(failure: BaseException) -> None:
            app = build_gateway_middleware_test_app(max_request_body_bytes=5)
            async with _install_budget_virtual_key(app) as (record, services):
                with self.assertRaises(type(failure)) as raised:
                    await _run_asgi_request(
                        app,
                        [
                            {
                                "type": "http.request",
                                "body": b"123",
                                "more_body": True,
                            },
                            failure,
                        ],
                        headers=[
                            (
                                b"authorization",
                                f"Bearer {record.api_key}".encode("ascii"),
                            )
                        ],
                    )
                self.assertIs(raised.exception, failure)

                self.assertEqual(
                    services.usd_budget_ledger.reserved_for(record.id),
                    0.0,
                )
                self.assertEqual(
                    services.active_requests_registry.list_records(),
                    [],
                )

        for failure in (
            RuntimeError("admission receive failed"),
            asyncio.CancelledError("admission cancelled"),
        ):
            with self.subTest(failure=type(failure).__name__):
                run_async(scenario(failure))

    def test_authenticated_413_send_failure_cleans_budget_and_active_request(self):
        async def scenario(failure: BaseException) -> None:
            app = build_gateway_middleware_test_app(max_request_body_bytes=5)
            async with _install_budget_virtual_key(app) as (record, services):
                messages = [
                    {
                        "type": "http.request",
                        "body": b"12345",
                        "more_body": True,
                    },
                    {"type": "http.request", "body": b"6"},
                ]
                scope = {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.3"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/v1/chat/completions",
                    "raw_path": b"/v1/chat/completions",
                    "query_string": b"",
                    "headers": [
                        (
                            b"authorization",
                            f"Bearer {record.api_key}".encode("ascii"),
                        )
                    ],
                    "client": ("testclient", 50000),
                    "server": ("testserver", 80),
                }

                async def receive():
                    return messages.pop(0)

                async def send(_message):
                    raise failure

                with self.assertRaises(type(failure)) as raised:
                    await app(scope, receive, send)
                self.assertIs(raised.exception, failure)

                self.assertTrue(scope["state"][PRE_ROUTE_REJECTED_STATE_KEY])
                services.accounting_service.reserve.assert_awaited_once()
                services.accounting_service.release.assert_awaited_once()
                self.assertEqual(
                    services.usd_budget_ledger.reserved_for(record.id),
                    0.0,
                )
                self.assertEqual(
                    services.active_requests_registry.list_records(),
                    [],
                )

        for failure in (
            RuntimeError("413 send failed"),
            asyncio.CancelledError("413 send cancelled"),
        ):
            with self.subTest(failure=type(failure).__name__):
                run_async(scenario(failure))

    def test_registered_stack_keeps_streaming_chat_ownership_until_final_body(self):
        async def scenario() -> None:
            stream_started = asyncio.Event()
            release_stream = asyncio.Event()
            app = FastAPI(lifespan=_test_runtime_lifespan)

            @app.post("/v1/chat/completions")
            async def chat():
                async def stream_body():
                    stream_started.set()
                    yield b"data: first\n\n"
                    await release_stream.wait()
                    yield b"data: done\n\n"

                return StreamingResponse(
                    stream_body(),
                    media_type="text/event-stream",
                )

            original_preparer = main.prepare_chat_response_observation

            async def passthrough_preparer(_request):
                return None

            main.prepare_chat_response_observation = passthrough_preparer
            try:
                main.configure_gateway_middleware(
                    app,
                    max_request_body_bytes=1024,
                )
            finally:
                main.prepare_chat_response_observation = original_preparer

            async with _install_budget_virtual_key(app) as (record, services):
                registry = services.active_requests_registry
                request_task = asyncio.create_task(
                    _run_asgi_request(
                        app,
                        [{"type": "http.request", "body": b"{}"}],
                        headers=[
                            (
                                b"authorization",
                                f"Bearer {record.api_key}".encode("ascii"),
                            )
                        ],
                        asgi_spec_version="2.4",
                    )
                )
                try:
                    await asyncio.wait_for(stream_started.wait(), timeout=2.0)
                    services.accounting_service.reserve.assert_awaited_once()
                    services.accounting_service.release.assert_not_awaited()
                    self.assertEqual(len(registry.list_records()), 1)
                    release_stream.set()
                    response = await asyncio.wait_for(request_task, timeout=2.0)
                finally:
                    release_stream.set()
                    if not request_task.done():
                        request_task.cancel()
                    await asyncio.gather(request_task, return_exceptions=True)

                self.assertEqual(response["status"], 200)
                services.accounting_service.release.assert_awaited_once()
                self.assertEqual(registry.list_records(), [])

        run_async(scenario())

    def test_global_exception_response_is_generic_and_includes_request_id(self):
        app = FastAPI()

        @app.get("/boom")
        async def boom():
            raise RuntimeError("SECRET_BACKEND_TOKEN")

        app.add_exception_handler(Exception, main.global_exception_handler)
        app.middleware("http")(main.log_middleware_functional)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/boom")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Internal server error")
        self.assertTrue(response.json()["request_id"])
        self.assertNotIn("SECRET_BACKEND_TOKEN", response.text)


async def _run_limited_asgi_request(
    body_messages,
    *,
    max_body_size: int,
    headers=None,
):
    async def inner_app(scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = ContentSizeLimitMiddleware(inner_app, max_body_size=max_body_size)
    messages = list(body_messages)
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": headers or [],
        },
        receive,
        send,
    )
    return {
        "status": sent[0]["status"],
        "body": b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body"),
    }


async def _run_header_guard_asgi_request(*, max_body_size: int, headers):
    sent = []

    async def inner_app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        raise AssertionError("header guard must not consume the body")

    async def send(message):
        sent.append(message)

    app = ContentLengthLimitMiddleware(inner_app, max_body_size=max_body_size)
    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": headers,
        },
        receive,
        send,
    )
    return {
        "status": sent[0]["status"],
        "body": b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        ),
    }


async def _run_asgi_request(
    app,
    body_messages,
    *,
    headers,
    path: str = "/v1/chat/completions",
    method: str = "POST",
    asgi_spec_version: str = "2.3",
):
    messages = list(body_messages)
    sent = []
    receive_count = 0

    async def receive():
        nonlocal receive_count
        receive_count += 1
        message = messages.pop(0)
        if isinstance(message, BaseException):
            raise message
        return message

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": asgi_spec_version},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    response_start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    return {
        "status": sent[0]["status"],
        "body": b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body"),
        "headers": {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in response_start.get("headers", [])
        },
        "receive_count": receive_count,
        "state": scope.get("state", {}),
    }


async def _run_asgi_request_with_runtime(
    app,
    body_messages,
    *,
    headers,
    path: str = "/v1/chat/completions",
    method: str = "POST",
):
    async with _test_runtime_lifespan(app):
        manager = app.state.services.runtime_manager
        with patch.object(
            manager,
            "acquire_current",
            wraps=manager.acquire_current,
        ) as acquire_current:
            response = await _run_asgi_request(
                app,
                body_messages,
                headers=headers,
                path=path,
                method=method,
            )
        response["acquire_count"] = acquire_current.call_count
        return response


async def _collect_replayed_messages(body_messages, *, max_body_size: int):
    received = []

    async def downstream(_scope, receive, send):
        while True:
            message = await receive()
            received.append(message)
            if message["type"] == "http.disconnect":
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    messages = list(body_messages)

    async def receive():
        return messages.pop(0)

    async def send(_message):
        return None

    middleware = ContentSizeLimitMiddleware(downstream, max_body_size=max_body_size)
    await middleware(
        {"type": "http", "headers": []},
        receive,
        send,
    )
    return received
