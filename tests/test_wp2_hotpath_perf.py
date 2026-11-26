"""Focused unit tests for WP2 hot-path middleware, rate-limiter, and accounting."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anyio
import pytest
from fastapi import FastAPI, Request
from starlette.routing import Match, Mount, Route

from llm_gateway_core.config.settings import settings
from llm_gateway_core.middleware.accounting_admission import (
    EFFECTIVE_HTTP_ROUTE_CACHE_STATE_KEY,
    EffectiveHttpRoute,
    resolve_effective_http_route,
)
from llm_gateway_core.middleware.auth import _matches_registered_route
from llm_gateway_core.middleware.chat_logging import (
    ChunkProcessor,
    _ChatResponseLifecycle,
    _has_known_prompt_tokens,
    prepare_chat_response_observation,
)
from llm_gateway_core.middleware.content_size import ContentSizeLimitMiddleware
from llm_gateway_core.middleware.request_logging import (
    RequestLoggingASGIMiddleware,
    _new_request_id,
)
from llm_gateway_core.middleware.response_observation import (
    _SSE_FRAME_SEPARATOR_RE,
    _strip_unsettled_sse_cost,
)
from llm_gateway_core.middleware.response_observation_contracts import (
    TerminalReason,
    TransportResult,
)
from llm_gateway_core.services.accounting import (
    AccountingEvent,
    AccountingEventKind,
    AccountingProjectionMark,
    AccountingUsage,
    AccountingValidationError,
    CostSource,
    ProjectionStatus,
    StoredAccountingEvent,
)
from llm_gateway_core.services.accounting_service import (
    AccountingService,
    _AsyncRWLock,
    _PerKeyLocks,
)
from llm_gateway_core.services.rate_limiter import RateLimiter
from llm_gateway_core.services.stream_observation import StreamObservationCapacity


# ---------------------------------------------------------------------------
# 1. ContentSizeLimitMiddleware
# ---------------------------------------------------------------------------


def test_content_size_append_body_blocks_preserves_bytes_and_skips_empty() -> None:
    blocks: deque[bytes] = deque()
    chunk1 = b"hello "
    chunk2 = bytearray(b"world")

    ContentSizeLimitMiddleware._append_body_blocks(blocks, b"", 4)
    assert len(blocks) == 0

    ContentSizeLimitMiddleware._append_body_blocks(blocks, chunk1, 4)
    ContentSizeLimitMiddleware._append_body_blocks(blocks, chunk2, 4)

    assert len(blocks) == 2
    assert blocks[0] is chunk1
    assert isinstance(blocks[1], bytes)
    assert blocks[1] == b"world"

    # Tiny chunks within block_size are coalesced
    tiny_blocks: deque[bytes] = deque()
    for _ in range(10):
        ContentSizeLimitMiddleware._append_body_blocks(tiny_blocks, b"x", 64)
    assert len(tiny_blocks) == 1
    assert bytes(tiny_blocks[0]) == b"x" * 10


def test_content_size_middleware_replays_bytes_chunks() -> None:
    received_messages: list[dict] = []

    async def inner_app(scope, receive, send):
        while True:
            msg = await receive()
            received_messages.append(msg)
            if not msg.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = ContentSizeLimitMiddleware(inner_app, max_body_size=1024)
    incoming = iter(
        [
            {"type": "http.request", "body": b"part1-", "more_body": True},
            {"type": "http.request", "body": bytearray(b"part2"), "more_body": False},
        ]
    )

    async def receive():
        return next(incoming)

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}
    asyncio.run(middleware(scope, receive, send))

    assert len(received_messages) == 1
    assert isinstance(received_messages[0]["body"], bytes)
    assert received_messages[0] == {"type": "http.request", "body": b"part1-part2", "more_body": False}


# ---------------------------------------------------------------------------
# 2. resolve_effective_http_route caching & _matches_registered_route
# ---------------------------------------------------------------------------


def test_resolve_effective_http_route_caches_hit_and_none_and_invalidates() -> None:
    call_count = 0

    class CountingRoute:
        path = "/v1/chat/completions"
        path_format = "/v1/chat/completions"
        methods = {"POST"}

        def matches(self, scope):
            nonlocal call_count
            call_count += 1
            if scope.get("path") == self.path and scope.get("method") == "POST":
                return Match.FULL, {}
            return Match.NONE, {}

    routes = [CountingRoute()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "root_path": "",
        "state": {},
    }

    first = resolve_effective_http_route(scope, routes)
    second = resolve_effective_http_route(scope, routes)
    assert first == EffectiveHttpRoute(method="POST", route_template="/v1/chat/completions")
    assert second == first
    assert call_count == 1
    assert EFFECTIVE_HTTP_ROUTE_CACHE_STATE_KEY in scope["state"]

    # Cache hit for None result
    scope["path"] = "/unknown"
    miss1 = resolve_effective_http_route(scope, routes)
    miss2 = resolve_effective_http_route(scope, routes)
    assert miss1 is None
    assert miss2 is None
    assert call_count == 2

    # Invalidation on different routes list identity
    other_routes = list(routes)
    assert resolve_effective_http_route(scope, other_routes) is None
    assert call_count == 3

    # Invalidation on root_path change
    scope["root_path"] = "/prefix"
    assert resolve_effective_http_route(scope, other_routes) is None
    assert call_count == 4


def test_resolve_effective_http_route_does_not_mutate_scope_without_state_or_cache_errors() -> None:
    scope_no_state = {"type": "http", "method": "POST", "path": "/x"}
    assert resolve_effective_http_route(scope_no_state, []) is None
    assert "state" not in scope_no_state

    class BrokenRoute:
        def matches(self, scope):
            return "not-a-tuple"

    scope_with_state = {"type": "http", "method": "POST", "path": "/x", "state": {}}
    with pytest.raises(AccountingValidationError):
        resolve_effective_http_route(scope_with_state, [BrokenRoute()])
    assert EFFECTIVE_HTTP_ROUTE_CACHE_STATE_KEY not in scope_with_state["state"]


def test_matches_registered_route_fast_path_and_fallbacks() -> None:
    app = FastAPI()

    async def dummy(request):
        return None

    app.router.routes.append(Route("/v1/chat/completions", dummy, methods=["POST"]))
    app.router.routes.append(Mount("/static", app=dummy))

    # Full match populates cache and returns True
    scope_full = {
        "type": "http",
        "app": app,
        "method": "POST",
        "path": "/v1/chat/completions",
        "root_path": "",
        "headers": [],
        "state": {},
    }
    assert _matches_registered_route(Request(scope_full)) is True
    assert EFFECTIVE_HTTP_ROUTE_CACHE_STATE_KEY in scope_full["state"]

    # Partial match (405 wrong method) still returns True via fallback loop
    scope_405 = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/v1/chat/completions",
        "root_path": "",
        "headers": [],
        "state": {},
    }
    assert _matches_registered_route(Request(scope_405)) is True

    # Mount("/static") has methods=None -> resolve_effective_http_route returns None -> fallback returns True
    scope_static = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/static/app.js",
        "root_path": "",
        "headers": [],
        "state": {},
    }
    assert _matches_registered_route(Request(scope_static)) is True

    # Completely unknown route returns False
    scope_404 = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/does-not-exist",
        "root_path": "",
        "headers": [],
        "state": {},
    }
    assert _matches_registered_route(Request(scope_404)) is False


def test_prepare_chat_response_observation_uses_app_router_routes() -> None:
    app = FastAPI()

    async def dummy(request):
        return None

    app.router.routes.append(Route("/health", dummy, methods=["GET"]))
    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/health",
        "root_path": "",
        "headers": [],
        "state": {},
    }
    # Pre-populate cache via app.router.routes
    resolve_effective_http_route(scope, app.router.routes)

    with patch(
        "llm_gateway_core.middleware.chat_logging.resolve_effective_http_route",
        wraps=resolve_effective_http_route,
    ) as spy:
        asyncio.run(prepare_chat_response_observation(Request(scope)))
        spy.assert_called_once()
        assert spy.call_args.args[1] is app.router.routes


# ---------------------------------------------------------------------------
# 3. ChunkProcessor, finish_transport, and response_observation regex
# ---------------------------------------------------------------------------


def _make_processor(**kwargs) -> ChunkProcessor:
    services = SimpleNamespace(
        stream_event_max_bytes=64 * 1024,
        json_response_max_bytes=64 * 1024,
        stream_observation_capacity=StreamObservationCapacity(max_bytes=1024 * 1024, max_items=100),
        upstream_routing_state=MagicMock(),
        task_supervisor=SimpleNamespace(create_task=lambda coro, name=None: asyncio.create_task(coro)),
    )
    return ChunkProcessor(
        req_headers={},
        req_body_str=kwargs.pop("req_body_str", ""),
        is_real_streaming=kwargs.pop("is_real_streaming", False),
        services=services,
        config_loader=None,
        cost_rate_registry={},
        **kwargs,
    )


def test_chunk_processor_response_accum_property_and_endswith() -> None:
    proc = _make_processor()
    assert proc.llm_response_accum == ""
    assert proc._accum_endswith("") is True
    assert proc._accum_endswith("abc") is False

    proc._append_log_text("hello ")
    proc._append_log_text("world")
    assert proc._accum_endswith("world") is True
    assert proc._accum_endswith("lo world") is True
    assert proc._accum_endswith("nope") is False
    assert proc.llm_response_accum == "hello world"

    # Setter compatibility
    proc.llm_response_accum = "\n\n[Reasoning]:\nthought"
    assert proc.llm_response_accum == "\n\n[Reasoning]:\nthought"
    assert proc._reasoning_header_written is True


def test_chunk_processor_needs_thread_for_write_log_skips_thread_when_usage_known(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "log_chat_messages", False)
    proc = _make_processor(req_body_str='{"messages":[]}')
    proc.tokens_usage["prompt_tokens"] = 10
    proc.tokens_usage["completion_tokens"] = 5
    proc._append_log_text("answer")
    assert proc._needs_thread_for_write_log() is False

    # If reasoning header was written and reasoning_tokens == 0 -> needs thread
    proc._append_reasoning_piece("step 1")
    assert proc._needs_thread_for_write_log() is True
    proc.tokens_usage["reasoning_tokens"] = 3
    assert proc._needs_thread_for_write_log() is False

    # If log_chat_messages is True -> needs thread
    monkeypatch.setattr(settings, "log_chat_messages", True)
    assert proc._needs_thread_for_write_log() is True


def test_chunk_processor_run_skips_to_thread_on_fast_path(monkeypatch) -> None:
    monkeypatch.setattr(settings, "log_chat_messages", False)

    async def _exercise() -> None:
        proc = _make_processor()
        proc.tokens_usage["prompt_tokens"] = 12
        proc.tokens_usage["completion_tokens"] = 8
        proc._append_log_text("ok")

        with (
            patch.object(anyio.to_thread, "run_sync", side_effect=AssertionError("should not offload")),
            patch.object(proc, "_write_log_once") as write_mock,
        ):
            proc.start()
            await proc.finish()
            assert await proc.wait(2.0) is True
            write_mock.assert_called_once()

    asyncio.run(_exercise())


def test_finish_transport_cancels_prompt_estimate_task_when_prompt_tokens_known(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "log_chat_messages", False)

    async def _exercise() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow_estimate() -> None:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        proc = _make_processor()
        proc.tokens_usage["prompt_tokens"] = 42
        assert _has_known_prompt_tokens(proc) is True
        proc.start()

        lifecycle = object.__new__(_ChatResponseLifecycle)
        lifecycle._transport_finished = False
        lifecycle._chunk_processor = proc
        lifecycle._prompt_estimate_task = asyncio.create_task(slow_estimate())
        lifecycle._request_started_at = time.monotonic()
        lifecycle._request = SimpleNamespace(state=SimpleNamespace())
        lifecycle._active_registry = MagicMock()
        lifecycle._request_id = "req-1"

        await started.wait()
        with patch.object(proc, "_write_log_once"):
            await lifecycle.finish_transport(
                TransportResult(
                    reason=TerminalReason.COMPLETE,
                    response_started=True,
                    response_completed=True,
                )
            )
        await asyncio.wait_for(cancelled.wait(), timeout=2.0)
        assert lifecycle._prompt_estimate_task.cancelled()

    asyncio.run(_exercise())


def test_strip_unsettled_sse_cost_uses_precompiled_regex() -> None:
    assert _SSE_FRAME_SEPARATOR_RE.pattern == rb"(\r?\n\r?\n)"
    raw = b'data: {"usage":{"prompt_tokens":1,"cost":0.5}}\r\n\r\ndata: [DONE]\n\n'
    stripped = _strip_unsettled_sse_cost(raw)
    assert b'"cost"' not in stripped
    assert b'"prompt_tokens":1' in stripped
    assert stripped.endswith(b"data: [DONE]\n\n")


# ---------------------------------------------------------------------------
# 4. RequestLoggingASGIMiddleware
# ---------------------------------------------------------------------------


def test_new_request_id_fast_path_and_mock_validation() -> None:
    with patch(
        "llm_gateway_core.middleware.request_logging.is_valid_request_id",
        side_effect=AssertionError("regex should not run for real UUID"),
    ):
        rid = _new_request_id()
    assert isinstance(rid, str)
    uuid.UUID(rid)

    with patch("llm_gateway_core.middleware.request_logging.uuid.uuid4", return_value="bad id with spaces"):
        assert _new_request_id() is None

    with patch("llm_gateway_core.middleware.request_logging.uuid.uuid4", return_value="valid-id_1"):
        assert _new_request_id() == "valid-id_1"


def test_request_logging_middleware_uses_monotonic_and_skips_revalidation() -> None:
    async def inner_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestLoggingASGIMiddleware(inner_app)
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "GET", "path": "/v1/models", "headers": []}
    monotonic_calls = 0
    real_monotonic = time.monotonic

    def fake_monotonic():
        nonlocal monotonic_calls
        monotonic_calls += 1
        return real_monotonic()

    with (
        patch("llm_gateway_core.middleware.request_logging.time.monotonic", side_effect=fake_monotonic),
        patch(
            "llm_gateway_core.middleware.request_logging.is_valid_request_id",
            side_effect=AssertionError("should not revalidate request_id"),
        ),
    ):
        asyncio.run(middleware(scope, receive, send))

    assert monotonic_calls >= 2
    headers = dict(sent[0]["headers"])
    assert b"x-request-id" in headers


# ---------------------------------------------------------------------------
# 5. RateLimiter._purge early exit
# ---------------------------------------------------------------------------


def test_rate_limiter_purge_early_exit_on_in_flight_since() -> None:
    now = [100.0]
    limiter = RateLimiter(window_seconds=60.0, time_func=lambda: now[0], in_flight_ttl_seconds=10.0)

    assert limiter.admit_request("req-1", 1, rpm_limit=100, tpm_limit=None) is None
    now[0] = 105.0
    assert limiter.admit_request("req-2", 1, rpm_limit=100, tpm_limit=None) is None
    now[0] = 112.0  # req-1 (100 < 102) expired; req-2 (105 >= 102) still valid
    assert limiter.admit_request("req-3", 1, rpm_limit=100, tpm_limit=None) is None

    state = limiter._by_key[1]
    assert "req-1" not in state.in_flight_since
    assert "req-1" not in state.request_events
    assert list(state.in_flight_since.keys()) == ["req-2", "req-3"]


# ---------------------------------------------------------------------------
# 6. _AsyncRWLock, _PerKeyLocks, and AccountingService._project_stored
# ---------------------------------------------------------------------------


def test_async_rwlock_writer_preferring_and_cancellation_safety() -> None:
    async def _exercise() -> None:
        rw = _AsyncRWLock()
        events: list[str] = []
        r1_in = asyncio.Event()
        r1_release = asyncio.Event()
        w1_waiting = asyncio.Event()

        async def reader1() -> None:
            async with rw.shared():
                events.append("r1_enter")
                r1_in.set()
                await r1_release.wait()
                events.append("r1_exit")

        async def writer1() -> None:
            w1_waiting.set()
            async with rw.exclusive():
                events.append("w1_enter")
                await asyncio.sleep(0)
                events.append("w1_exit")

        async def reader2() -> None:
            async with rw.shared():
                events.append("r2_enter")

        t_r1 = asyncio.create_task(reader1())
        await r1_in.wait()

        t_w1 = asyncio.create_task(writer1())
        await w1_waiting.wait()
        await asyncio.sleep(0)  # let writer1 register as waiting writer

        t_r2 = asyncio.create_task(reader2())
        await asyncio.sleep(0)  # reader2 must block behind waiting writer1
        assert "r2_enter" not in events

        r1_release.set()
        await asyncio.gather(t_r1, t_w1, t_r2)
        assert events == ["r1_enter", "r1_exit", "w1_enter", "w1_exit", "r2_enter"]

        # Cancelling a waiting writer must wake blocked readers
        r3_in = asyncio.Event()
        r3_release = asyncio.Event()
        r4_entered = asyncio.Event()

        async def reader3() -> None:
            async with rw.shared():
                r3_in.set()
                await r3_release.wait()

        async def cancelled_writer() -> None:
            async with rw.exclusive():
                pass

        async def reader4() -> None:
            async with rw.shared():
                r4_entered.set()

        t_r3 = asyncio.create_task(reader3())
        await r3_in.wait()
        t_wc = asyncio.create_task(cancelled_writer())
        await asyncio.sleep(0)
        t_r4 = asyncio.create_task(reader4())
        await asyncio.sleep(0)
        assert not r4_entered.is_set()

        t_wc.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t_wc
        await asyncio.wait_for(r4_entered.wait(), timeout=2.0)
        r3_release.set()
        await asyncio.gather(t_r3, t_r4)

    asyncio.run(_exercise())


def test_async_rwlock_handoff_then_cancel_same_tick_releases_ownership() -> None:
    async def _exercise() -> None:
        rw = _AsyncRWLock()

        # 1) Writer receives ownership synchronously from release_shared(),
        #    then is cancelled on the same event-loop tick before resuming.
        await rw.acquire_shared()
        r_after_entered = asyncio.Event()

        async def waiting_writer() -> None:
            async with rw.exclusive():
                pass

        async def reader_after_writer() -> None:
            async with rw.shared():
                r_after_entered.set()

        t_w = asyncio.create_task(waiting_writer())
        await asyncio.sleep(0)
        t_r_after = asyncio.create_task(reader_after_writer())
        await asyncio.sleep(0)

        # Same-tick handoff + cancel (no await between release and cancel)
        rw.release_shared()
        assert rw._writer is True
        t_w.cancel()

        with pytest.raises(asyncio.CancelledError):
            await t_w
        await asyncio.wait_for(r_after_entered.wait(), timeout=2.0)
        await t_r_after
        assert rw._readers == 0
        assert rw._writer is False

        # 2) Reader receives ownership synchronously from release_exclusive(),
        #    then is cancelled on the same event-loop tick before resuming.
        await rw.acquire_exclusive()
        w_after_entered = asyncio.Event()

        async def waiting_reader() -> None:
            async with rw.shared():
                pass

        async def writer_after_reader() -> None:
            async with rw.exclusive():
                w_after_entered.set()

        t_r = asyncio.create_task(waiting_reader())
        await asyncio.sleep(0)

        # Same-tick handoff to waiting reader + cancel (no await between)
        rw.release_exclusive()
        assert rw._readers == 1
        t_r.cancel()

        t_w_after = asyncio.create_task(writer_after_reader())
        with pytest.raises(asyncio.CancelledError):
            await t_r
        await asyncio.wait_for(w_after_entered.wait(), timeout=2.0)
        await t_w_after
        assert rw._readers == 0
        assert rw._writer is False

    asyncio.run(_exercise())


def test_per_key_locks_cleanup_and_cancellation() -> None:
    async def _exercise() -> None:
        locks = _PerKeyLocks()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with locks.acquire(7):
                entered.set()
                await release.wait()

        async def waiter() -> None:
            async with locks.acquire(7):
                pass

        t1 = asyncio.create_task(holder())
        await entered.wait()
        t2 = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        assert locks._entries[7].waiters == 2

        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2
        assert locks._entries[7].waiters == 1

        release.set()
        await t1
        assert 7 not in locks._entries

    asyncio.run(_exercise())


def _make_stored_charge(event_id: str, api_key_id: int) -> StoredAccountingEvent:
    now = datetime.now(timezone.utc)
    event = AccountingEvent(
        version=1,
        event_id=event_id,
        request_id=f"req-{event_id}",
        kind=AccountingEventKind.CHARGE,
        method="POST",
        route_template="/v1/chat/completions",
        operation="chat",
        provider="openai",
        model="gpt-4o",
        gateway_model="gpt-4o",
        api_key_id=api_key_id,
        usage=AccountingUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost=0.01),
        cost_source=CostSource.UPSTREAM,
        occurred_at=now,
    )
    return StoredAccountingEvent(
        event=event,
        usage_row_id=1,
        created_at=now,
    )


def test_accounting_service_projects_different_keys_in_parallel_and_same_key_serially() -> None:
    async def _exercise() -> None:
        barrier = threading.Barrier(2, timeout=5.0)

        class ParallelSink:
            def apply_spend_event(self, stored, attempted_at):
                barrier.wait()
                return ProjectionStatus.APPLIED

        class DummySource:
            def mark_accounting_event_projected(self, event_id, fingerprint, attempted_at):
                return AccountingProjectionMark(
                    event_id=event_id,
                    billing_fingerprint=fingerprint,
                    projected_at=attempted_at,
                    projection_attempts=1,
                    last_attempt_at=attempted_at,
                    last_error_code=None,
                )

        svc = AccountingService(
            source=DummySource(),  # type: ignore[arg-type]
            sink=ParallelSink(),  # type: ignore[arg-type]
            budget_ledger=MagicMock(),
            rate_limiter=RateLimiter(),
        )
        svc._bind_loop()

        statuses = await asyncio.gather(
            svc._project_stored(_make_stored_charge("e1", 101)),
            svc._project_stored(_make_stored_charge("e2", 202)),
        )
        assert statuses == [ProjectionStatus.APPLIED, ProjectionStatus.APPLIED]
        assert svc._key_projection_locks is not None
        assert len(svc._key_projection_locks._entries) == 0

        # Same key must serialize
        active_same_key = 0
        max_same_key = 0
        lock = threading.Lock()

        class SerialCheckSink:
            def apply_spend_event(self, stored, attempted_at):
                nonlocal active_same_key, max_same_key
                with lock:
                    active_same_key += 1
                    max_same_key = max(max_same_key, active_same_key)
                time.sleep(0.02)
                with lock:
                    active_same_key -= 1
                return ProjectionStatus.APPLIED

        svc._sink = SerialCheckSink()  # type: ignore[assignment]
        statuses_same = await asyncio.gather(
            svc._project_stored(_make_stored_charge("e3", 303)),
            svc._project_stored(_make_stored_charge("e4", 303)),
        )
        assert statuses_same == [ProjectionStatus.APPLIED, ProjectionStatus.APPLIED]
        assert max_same_key == 1
        assert len(svc._key_projection_locks._entries) == 0

    asyncio.run(_exercise())
