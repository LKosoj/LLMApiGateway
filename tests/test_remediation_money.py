"""Focused regression tests for the money-safety remediation pass.

Each test targets exactly one behavioral fix from the remediation task list
(see the final report for the corresponding production file:line). These are
new, additive tests; they intentionally do not touch any pre-existing test.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import StreamingResponse

from llm_gateway_core.api.v1 import chat as chat_api
from llm_gateway_core.api.v1.chat_accounting import ChatStreamDialect, _is_error_sse_event
from llm_gateway_core.api.v1.chat_streaming import (
    _anthropic_stream_to_openai,
    _DirectChatStreamObservationBuilder,
    _is_openai_stream_error_chunk,
    _openai_stream_to_anthropic,
    _openai_stream_to_responses,
)
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.db.write_batcher import (
    WriteBatcher,
    WriteBatcherOverloaded,
    WriteBatcherState,
)
from llm_gateway_core.middleware.response_observation import (
    TerminalReason,
    TerminalSignal,
    WireMode,
)
from llm_gateway_core.services.accounting import (
    ACCOUNTING_EVENT_VERSION,
    DEFAULT_OPERATION_COST_USD,
    AccountingError,
    AccountingErrorCode,
    AccountingEvent,
    AccountingEventKind,
    AccountingHealthState,
    AccountingReceipt,
    AccountingUsage,
    AccountingValidationError,
    CostSource,
    ProjectionStatus,
    SourceStatus,
)
from llm_gateway_core.services.error_classifier import classify_error
from llm_gateway_core.services.operation_accounting import build_token_model_component
from llm_gateway_core.services.stream_observation import SSEEvent
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from llm_gateway_core.utils.usage_tracking import ModelCostRates
from tests._async_compat import run_async
from tests.test_accounting_service import (
    FakeSink,
    FakeSource,
    _charge as _service_charge,
    _key_record,
    _service,
    _wait_until,
)
from tests.test_chat_accounting_integration import _direct_request, _provider_config
from tests.test_chat_accounting_terminal import _build_observation, _start


# --- Task 1a: streaming OpenAI-compatible requests must force include_usage,
#              otherwise the accounting engine may never see real usage. ----


def test_streaming_request_forces_include_usage_stream_option() -> None:
    async def scenario() -> None:
        request = _direct_request()
        auth_material = SimpleNamespace(
            headers={"Authorization": "Bearer upstream-secret"},
            upstream_key_fingerprint=None,
        )
        make_llm_request_mock = AsyncMock(return_value=({"ok": True}, None))
        with (
            patch.object(
                chat_api,
                "resolve_provider_auth_material",
                new=AsyncMock(return_value=auth_material),
            ),
            patch.object(chat_api, "make_llm_request", new=make_llm_request_mock),
        ):
            result, error, _attempt_number = await chat_api._attempt_model_fallback_rule(
                request,
                object(),
                {"provider-a": _provider_config(provider_type="openai")},
                "gateway-model",
                {
                    "model": "gateway-model",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
                {"provider": "provider-a", "model": "model-a"},
                True,
                proxy_http_clients={},
                upstream_routing_state=UpstreamRoutingState(),
            )

        assert error is None
        assert result == {"ok": True}
        make_llm_request_mock.assert_awaited_once()
        attempt_payload = make_llm_request_mock.await_args.args[3]
        assert attempt_payload.get("stream_options") == {"include_usage": True}

    run_async(scenario())


# --- Task 1b: a stream that ends without an upstream usage chunk must be ---
#              estimated instead of failing the whole request. -------------


def test_direct_stream_builder_estimates_when_terminal_has_no_usage() -> None:
    builder = _DirectChatStreamObservationBuilder(
        dialect=ChatStreamDialect.OPENAI,
        provider="provider-a",
        model="model-a",
        cost_rate_registry={("provider-a", "model-a"): ModelCostRates(1.0, 2.0)},
        estimated_prompt_tokens=10,
    )
    delta_event = SSEEvent('{"choices":[{"delta":{"content":"hello there"}}]}')
    done_event = SSEEvent("[DONE]", done=True)

    assert builder.observe(delta_event) is None
    observation = builder.observe(done_event)

    assert observation is not None
    assert observation.usage.is_estimated is True
    assert observation.usage.prompt_tokens == 10
    assert observation.usage.completion_tokens > 0


# --- Task 2: a client disconnect/cancel mid-stream must commit a ----------
#             best-effort estimate instead of releasing the reservation
#             (which silently drops the already-spent tokens). -------------


def test_client_disconnect_mid_stream_commits_estimated_partial_instead_of_releasing() -> None:
    async def scenario() -> None:
        _, services, snapshot, _owner, handoff, observation = _build_observation(
            streaming=True,
            dialect=ChatStreamDialect.OPENAI,
        )
        builder = _DirectChatStreamObservationBuilder(
            dialect=ChatStreamDialect.OPENAI,
            provider="provider-a",
            model="model-a",
            cost_rate_registry=snapshot.cost_rate_registry,
            estimated_prompt_tokens=8,
        )
        handoff.publish_stream_observer(builder.observe, build_partial=builder.build_partial)

        captured_events = []

        async def commit(_reservation, event):
            captured_events.append(event)
            return AccountingReceipt(
                source_status=SourceStatus.ACCEPTED,
                projection_status=ProjectionStatus.APPLIED,
                event_id=event.event_id,
                billing_fingerprint=event.billing_fingerprint,
                usage_row_id=1,
            )

        services.accounting_service.commit.side_effect = commit

        start = _start(200, WireMode.SSE)
        delta_event = SSEEvent('{"choices":[{"delta":{"content":"partial answer chunk"}}]}')
        assert observation.classify_sse(start, delta_event) is None

        await observation.finalizer.finalize(
            TerminalSignal(TerminalReason.CLIENT_DISCONNECT, start)
        )

        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()
        assert len(captured_events) == 1
        assert captured_events[0].usage.is_estimated is True
        assert captured_events[0].usage.completion_tokens > 0

    run_async(scenario())


# --- Task 3a: a missing/unconfigured cost rate uses the default cost. -------


def test_missing_token_rate_uses_default_cost() -> None:
    component = build_token_model_component(
        {"usage": {"prompt_tokens": 2, "completion_tokens": 1}},
        provider="trusted-provider",
        model="missing-model",
        cost_rate_registry={},
    )
    assert component.usage.cost_unavailable is False
    assert component.usage.cost == DEFAULT_OPERATION_COST_USD
    assert component.cost_source is CostSource.OPERATION_DEFAULT


# --- Task 3b: a total_tokens mismatch must be tolerated (logged) and the ---
#              derived total used, instead of rejecting the response. ------


def test_total_tokens_mismatch_is_tolerated_and_uses_derived_total() -> None:
    component = build_token_model_component(
        {
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 999,
            }
        },
        provider="provider-a",
        model="model-a",
        cost_rate_registry={("provider-a", "model-a"): ModelCostRates(1.0, 2.0)},
    )
    assert component.usage.total_tokens == 5


# --- Task 4: "context overflow" must be recognized as a context-overflow ---
#             signal for streaming upstream error messages. ----------------


def test_classify_error_recognizes_context_overflow_phrase() -> None:
    assert classify_error("Error: context overflow while building prompt") == "context_overflow"


# --- Task 5: a trailing SSE event without a terminating blank line must ----
#             still be flushed instead of silently dropped. ----------------


def test_anthropic_to_openai_stream_flushes_trailing_event_without_blank_line() -> None:
    async def scenario() -> None:
        async def body():
            yield (
                b"event: content_block_delta\n"
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"text_delta","text":"trailing chunk"}}'
            )

        response = StreamingResponse(body(), media_type="text/event-stream")
        converted = _anthropic_stream_to_openai(None, response, "requested-model")
        chunks = [chunk async for chunk in converted.body_iterator]
        combined = b"".join(chunks)
        assert b"trailing chunk" in combined

    run_async(scenario())


# --- Task 6: a permanent sink failure (ORPHAN_SINK) must not close --------
#             admission for every key. The offending event is logged and ---
#             left unsettled for operator follow-up, but the key and the ---
#             service both keep accepting requests. ------------------------


def test_orphan_sink_quarantines_only_the_offending_key() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        sink.errors.append(AccountingError(AccountingErrorCode.ORPHAN_SINK))
        service = _service(source, sink)
        await service.start()

        receipt = await service.record(_service_charge("event-orphan", api_key_id=7))
        assert receipt.projection_status is ProjectionStatus.PENDING

        snapshot = service.health_snapshot()
        assert snapshot.state is AccountingHealthState.RUNNING
        assert snapshot.accepting is True

        other_receipt = await service.record(
            _service_charge("event-other-key", api_key_id=8)
        )
        assert other_receipt.projection_status is ProjectionStatus.APPLIED

    run_async(scenario())


# --- Task 6b: a permanently broken ledger row must not re-trip the service -
#              across repeated background projector drain cycles. -----------
#              FakeSink.errors is a one-shot queue (pop(0)), so it can't -----
#              reproduce a key whose sink row is durably broken; this test --
#              uses a sink that raises ORPHAN_SINK for that key on every ----
#              single call. -------------------------------------------------


class _PersistentOrphanSink(FakeSink):
    """Always fails apply_spend_event for one api_key_id, forever.

    Unlike FakeSink.errors (a one-shot queue), this reproduces a ledger row
    that is durably broken, so the background projector drain re-discovers
    the same failure on every retry instead of succeeding the second time.
    """

    def __init__(self, *, quarantined_key_id: int) -> None:
        super().__init__()
        self._quarantined_key_id = quarantined_key_id

    def apply_spend_event(self, stored_event, applied_at):
        if stored_event.event.api_key_id == self._quarantined_key_id:
            raise AccountingError(AccountingErrorCode.ORPHAN_SINK)
        return super().apply_spend_event(stored_event, applied_at)


def test_background_drain_does_not_retrip_service_for_already_quarantined_key() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = _PersistentOrphanSink(quarantined_key_id=7)
        service = _service(source, sink, projector_backoff_seconds=0.01)
        await service.start()

        receipt = await service.record(_service_charge("event-quarantine-seed", api_key_id=7))
        assert receipt.projection_status is ProjectionStatus.PENDING

        snapshot = service.health_snapshot()
        assert snapshot.state is AccountingHealthState.RUNNING
        assert snapshot.accepting is True

        # Force several more background projector-loop drain cycles over the
        # same still-pending, already-quarantined event. Without the fix, the
        # very first background retry re-raises ORPHAN_SINK outside the
        # allow_key_quarantine path and trips the whole service to FAILED.
        for iteration in range(1, 6):
            before = source.list_calls
            service._projector_wake.set()
            await _wait_until(lambda: source.list_calls > before)

            snapshot = service.health_snapshot()
            assert snapshot.state is AccountingHealthState.RUNNING, iteration
            assert snapshot.accepting is True, iteration

        # A different, healthy key must keep projecting normally throughout.
        # (APPLIED or ALREADY_APPLIED are both valid: since the background
        # drain no longer skips the permanently-failing key, a concurrent
        # drain cycle may race ahead and project this event first -- either
        # way it settles exactly once, never staying PENDING.)
        other_receipt = await service.record(
            _service_charge("event-other-key-after-quarantine", api_key_id=8)
        )
        assert other_receipt.projection_status in (
            ProjectionStatus.APPLIED,
            ProjectionStatus.ALREADY_APPLIED,
        )

    run_async(scenario())


class _BlockingUpdateSink(FakeSink):
    """Blocks `update_accounting_key` until released.

    Used to hold the transient ACCOUNTING_IN_FLIGHT fence open long enough to
    race a second admin mutation against the same key, so the pre-existing
    short-lived workflow-lock contract (as opposed to a sticky quarantine)
    stays regression-tested.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered_update = threading.Event()
        self.release_update: threading.Event | None = None

    def update_accounting_key(self, key_id, *, changes, reset_spent=False):
        self.entered_update.set()
        if self.release_update is not None:
            self.release_update.wait(timeout=2)
        return super().update_accounting_key(key_id, changes=changes, reset_spent=reset_spent)


def test_update_key_still_reports_accounting_in_flight_for_transient_fence() -> None:
    async def scenario() -> None:
        sink = _BlockingUpdateSink()
        sink.records[7] = _key_record()
        sink.release_update = threading.Event()
        service = _service(FakeSource(), sink)
        await service.start()

        first = asyncio.create_task(service.update_key(7, changes={}, reset_spent=False))
        await asyncio.to_thread(sink.entered_update.wait, 1)

        with pytest.raises(AccountingError) as error:
            await service.update_key(7, changes={}, reset_spent=False)
        assert error.value.code is AccountingErrorCode.ACCOUNTING_IN_FLIGHT

        sink.release_update.set()
        await first
        await service.stop()

    run_async(scenario())


def test_delete_key_still_reports_accounting_in_flight_for_transient_fence() -> None:
    async def scenario() -> None:
        sink = _BlockingUpdateSink()
        sink.records[7] = _key_record()
        sink.release_update = threading.Event()
        service = _service(FakeSource(), sink)
        await service.start()

        update_task = asyncio.create_task(service.update_key(7, changes={}, reset_spent=False))
        await asyncio.to_thread(sink.entered_update.wait, 1)

        with pytest.raises(AccountingError) as error:
            await service.delete_key(7)
        assert error.value.code is AccountingErrorCode.ACCOUNTING_IN_FLIGHT

        sink.release_update.set()
        await update_task
        await service.stop()

    run_async(scenario())


# --- Task 7: a transient enqueue overflow must stay transient (recorded ---
#             as overflow), not permanently fail the whole write batcher. ---


def test_write_batcher_overflow_stays_transient_not_permanently_failed(tmp_path) -> None:
    db_path = tmp_path / "writer.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
        conn.commit()
    batcher = WriteBatcher(
        db_path,
        queue_maxsize=2,
        dead_letter_path=tmp_path / "writer.dead-letter.jsonl",
    )
    batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("one",))
    batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("two",))
    with pytest.raises(WriteBatcherOverloaded):
        batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("three",))

    saturated = batcher.health_snapshot()
    assert saturated.state == WriteBatcherState.NEW
    assert saturated.overflow == 1
    assert saturated.last_error_code == "overloaded"
    assert saturated.accepting is True


# --- Task 9: usage_source / upstream_key_fingerprint / x_title must round- -
#             trip through the accounting-event insert path (they had -----
#             regressed / been silently dropped by the INSERT statement). --


def test_accounting_event_persists_usage_source_fingerprint_and_title(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db = TokensUsageDB(db_filename="remediation-source.db")
    event = AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION,
        event_id="event-title",
        kind=AccountingEventKind.CHARGE,
        api_key_id=7,
        method="POST",
        route_template="/v1/chat/completions",
        operation="chat",
        gateway_model="gateway/chat",
        provider="provider-a",
        model="model-a",
        usage=AccountingUsage(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cost=0.0,
            is_estimated=True,
        ),
        cost_source=CostSource.UPSTREAM,
        occurred_at=datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
        upstream_key_fingerprint="fingerprint-abc",
        x_title="My App",
    )

    db.accept_accounting_event(event)

    with sqlite3.connect(db.db_path) as conn:
        row = conn.execute(
            "SELECT usage_source, upstream_key_fingerprint, x_title FROM tokens_usage"
        ).fetchone()
    assert row == ("estimate_fallback", "fingerprint-abc", "My App")


# --- Task 10: cost_unavailable must round-trip through the accounting-event -
#              insert path, otherwise a successful request with an --------
#              unconfigured price is indistinguishable in the database from -
#              an honestly free one (silent revenue loss with no trace). ----


def test_accounting_event_persists_and_rereads_cost_unavailable_flag(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db = TokensUsageDB(db_filename="remediation-cost-unavailable.db")
    event = AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION,
        event_id="event-cost-unavailable",
        kind=AccountingEventKind.CHARGE,
        api_key_id=7,
        method="POST",
        route_template="/v1/chat/completions",
        operation="chat",
        gateway_model="gateway/chat",
        provider="provider-a",
        model="unpriced-model",
        usage=AccountingUsage(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cost=0.0,
            cost_unavailable=True,
        ),
        cost_source=CostSource.TOKEN_REGISTRY,
        occurred_at=datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
    )

    db.accept_accounting_event(event)

    with sqlite3.connect(db.db_path) as conn:
        row = conn.execute(
            "SELECT cost_unavailable FROM tokens_usage WHERE accounting_event_id = ?",
            (event.event_id,),
        ).fetchone()
    assert row == (1,)

    # A duplicate accept must re-read the stored row and reconstruct the
    # same cost_unavailable flag, not silently default it back to False.
    duplicate = db.accept_accounting_event(event)
    assert duplicate.status is SourceStatus.DUPLICATE
    assert duplicate.stored_event.event.usage.cost_unavailable is True


def test_init_db_migrates_legacy_tokens_usage_table_without_cost_unavailable_column(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db_path = tmp_path / "remediation-legacy-cost-unavailable.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE tokens_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                duration_ms INTEGER,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                reasoning_tokens INTEGER DEFAULT 0,
                cached_tokens INTEGER DEFAULT 0,
                cost REAL DEFAULT 0.0,
                gateway_model TEXT,
                operation TEXT,
                model TEXT,
                provider TEXT,
                request_id TEXT,
                is_estimated INTEGER NOT NULL DEFAULT 0,
                usage_source TEXT,
                cost_saved REAL NOT NULL DEFAULT 0.0,
                api_key_id INTEGER,
                upstream_key_fingerprint TEXT,
                x_title TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tokens_usage
            (timestamp, prompt_tokens, completion_tokens, total_tokens, cost,
             gateway_model, operation, model, provider)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-07-13T10:00:00+00:00",
                10,
                5,
                15,
                0.02,
                "gateway/chat",
                "chat",
                "model-a",
                "provider-a",
            ),
        )
        conn.commit()

    TokensUsageDB(db_filename=db_path.name)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        column_names = {row[1] for row in conn.execute("PRAGMA table_info(tokens_usage)")}
        assert "cost_unavailable" in column_names

        rows = conn.execute("SELECT * FROM tokens_usage").fetchall()
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 10
    assert rows[0]["cost_unavailable"] == 0


# --- Task 1a: a "usage": null delta chunk (forced by include_usage=True on ---
#              every OpenAI-compatible stream) must not poison the OpenAI ----
#              usage latch before the terminal event is seen. ---------------


def test_direct_stream_builder_ignores_null_usage_before_terminal() -> None:
    builder = _DirectChatStreamObservationBuilder(
        dialect=ChatStreamDialect.OPENAI,
        provider="provider-a",
        model="model-a",
        cost_rate_registry={("provider-a", "model-a"): ModelCostRates(1.0, 2.0)},
        estimated_prompt_tokens=10,
    )
    delta_event = SSEEvent(
        '{"choices":[{"delta":{"content":"hello"}}],"usage":null}'
    )

    assert builder.observe(delta_event) is None
    observation = builder.build_partial()

    assert observation is not None
    assert observation.usage.is_estimated is True
    assert observation.usage.completion_tokens > 0


def test_direct_stream_builder_accepts_openai_usage_with_zero_anthropic_aliases() -> None:
    """AgentRouter Claude's include_usage chunk mixes both token schemas.

    `prompt_tokens`/`completion_tokens` are real; `input_tokens`/`output_tokens`
    are dummy zeros. Building the terminal observation used to raise, and the
    response observer turned that into a 502 protocol error after a valid
    stream.
    """
    builder = _DirectChatStreamObservationBuilder(
        dialect=ChatStreamDialect.OPENAI,
        provider="agentrouter",
        model="claude-opus-5",
        cost_rate_registry={("agentrouter", "claude-opus-5"): ModelCostRates(1.0, 2.0)},
        estimated_prompt_tokens=10,
    )
    assert (
        builder.observe(
            SSEEvent('{"choices":[{"delta":{"content":"Hi"}}],"usage":null}')
        )
        is None
    )
    assert builder.observe(SSEEvent("null")) is None
    assert (
        builder.observe(
            SSEEvent(
                json.dumps(
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 8,
                            "total_tokens": 18,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "input_tokens_details": None,
                        },
                    }
                )
            )
        )
        is None
    )
    observation = builder.observe(SSEEvent("[DONE]", done=True))

    assert observation is not None
    assert observation.usage.is_estimated is False
    assert observation.usage.prompt_tokens == 10
    assert observation.usage.completion_tokens == 8
    assert observation.usage.total_tokens == 18


# --- Task 1b: take_partial() must be covered by the same release-on-failure -
#              guard as take()/commit(), otherwise a raising partial builder -
#              leaks the reservation on client disconnect. -------------------


def test_stream_partial_estimate_failure_still_releases_reservation_on_disconnect() -> None:
    async def scenario() -> None:
        _, services, _, _, handoff, observation = _build_observation(
            streaming=True,
            dialect=ChatStreamDialect.OPENAI,
        )

        def observer(_event: SSEEvent) -> None:
            return None

        def raising_build_partial() -> None:
            raise AccountingValidationError

        handoff.publish_stream_observer(observer, build_partial=raising_build_partial)

        start = _start(200, WireMode.SSE)
        delta_event = SSEEvent('{"choices":[{"delta":{"content":"partial"}}]}')
        assert observation.classify_sse(start, delta_event) is None

        with pytest.raises(AccountingValidationError):
            await observation.finalizer.finalize(
                TerminalSignal(TerminalReason.CLIENT_DISCONNECT, start)
            )

        services.accounting_service.release.assert_awaited_once()
        services.accounting_service.commit.assert_not_awaited()

    run_async(scenario())


# --- Task 2: build_partial() for the RESPONSES dialect must estimate from ---
#             streamed response.output_text.delta text instead of always ----
#             returning None. -------------------------------------------------


def test_responses_stream_builder_estimates_partial_from_streamed_text() -> None:
    builder = _DirectChatStreamObservationBuilder(
        dialect=ChatStreamDialect.RESPONSES,
        provider="provider-a",
        model="model-a",
        cost_rate_registry={("provider-a", "model-a"): ModelCostRates(1.0, 2.0)},
        estimated_prompt_tokens=10,
    )
    delta_event = SSEEvent(
        '{"type":"response.output_text.delta","delta":"partial response text"}'
    )

    assert builder.observe(delta_event) is None
    observation = builder.build_partial()

    assert observation is not None
    assert observation.usage.is_estimated is True
    assert observation.usage.prompt_tokens == 10
    assert observation.usage.completion_tokens > 0


# --- Task 3: an OpenAI-compatible SSE chunk carrying a null "error" key -----
#             alongside real "choices" must not be misclassified as an ------
#             upstream error. -------------------------------------------------


def test_openai_error_key_alongside_choices_is_not_misclassified_as_upstream_error() -> None:
    content_event = SSEEvent('{"choices":[{"delta":{"content":"Hello"}}],"error":null}')
    assert _is_error_sse_event(content_event, ChatStreamDialect.OPENAI) is False

    real_error_event = SSEEvent('{"error":{"message":"failed"}}')
    assert _is_error_sse_event(real_error_event, ChatStreamDialect.OPENAI) is True


# --- Review fix: a terminal usage-only chunk ("choices": []) that also -----
#             carries a null "error" key (the same provider quirk Task 3 -----
#             fixed for content chunks) must not be misclassified as an -----
#             upstream error, otherwise the only carrier of real usage is ---
#             dropped and the request is silently released unbilled. -------
#             A real error reported with an empty "choices" list must still -
#             be recognized as an error.------------------------------------


def test_openai_terminal_usage_chunk_with_null_error_is_not_misclassified() -> None:
    terminal_event = SSEEvent(
        '{"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,'
        '"total_tokens":15},"error":null}'
    )
    assert _is_error_sse_event(terminal_event, ChatStreamDialect.OPENAI) is False

    real_error_event = SSEEvent('{"choices":[],"error":{"message":"failed"}}')
    assert _is_error_sse_event(real_error_event, ChatStreamDialect.OPENAI) is True


# --- Review fix: tool-call argument tokens streamed via ---------------------
#             "response.function_call_arguments.delta" are real model ------
#             output (billed by the upstream) and must count toward the ----
#             partial-estimate completion tokens, otherwise a stream --------
#             aborted mid-tool-call bills zero completion tokens. ----------


def test_responses_stream_builder_estimates_partial_from_function_call_arguments() -> None:
    builder = _DirectChatStreamObservationBuilder(
        dialect=ChatStreamDialect.RESPONSES,
        provider="provider-a",
        model="model-a",
        cost_rate_registry={("provider-a", "model-a"): ModelCostRates(1.0, 2.0)},
        estimated_prompt_tokens=10,
    )
    delta_event = SSEEvent(
        '{"type":"response.function_call_arguments.delta",'
        '"delta":"{\\"city\\": \\"Berlin\\", \\"units\\": \\"metric\\"}"}'
    )

    assert builder.observe(delta_event) is None
    observation = builder.build_partial()

    assert observation is not None
    assert observation.usage.is_estimated is True
    assert observation.usage.completion_tokens > 0


# --- Task 5: the client-facing SSE converters shared the same "error": null
#             quirk as the accounting boundary. A terminal usage-only chunk
#             carrying "error": null must not be turned into a client-facing
#             failure that hides real usage from both the client and the
#             accounting engine (which then releases the reservation unbilled).


def _sse_line(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")


def _collect_responses_events(result: StreamingResponse) -> list[dict]:
    async def consume() -> list[bytes]:
        return [chunk async for chunk in result.body_iterator]

    events: list[dict] = []
    text = b"".join(run_async(consume())).decode("utf-8")
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data == "[DONE]":
                continue
            events.append(json.loads(data))
    return events


def _collect_anthropic_events(result: StreamingResponse) -> list[dict]:
    async def consume() -> list[bytes]:
        return [chunk async for chunk in result.body_iterator]

    events: list[dict] = []
    text = b"".join(run_async(consume())).decode("utf-8")
    for block in text.strip().split("\n\n"):
        name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if name is not None and data is not None:
            events.append({"event": name, "data": data})
    return events


def test_openai_stream_error_chunk_ignores_null_error_key() -> None:
    # A null "error" key on the terminal usage-only chunk is not an error.
    assert (
        _is_openai_stream_error_chunk(
            {"choices": [], "usage": {"prompt_tokens": 10}, "error": None}
        )
        is False
    )
    # A real upstream error (non-null "error", no choices) is still an error.
    assert _is_openai_stream_error_chunk({"error": {"message": "boom"}}) is True
    # A real error carried alongside actual content is not treated as fatal.
    assert (
        _is_openai_stream_error_chunk(
            {"error": {"message": "boom"}, "choices": [{"delta": {"content": "x"}}]}
        )
        is False
    )
    # An ordinary content chunk is not an error.
    assert (
        _is_openai_stream_error_chunk({"choices": [{"delta": {"content": "Hi"}}]})
        is False
    )


def test_responses_converter_null_error_terminal_usage_completes_with_real_usage() -> None:
    async def body():
        yield _sse_line({"id": "r1", "choices": [{"delta": {"content": "Hello"}}]})
        yield _sse_line(
            {
                "id": "r1",
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
                "error": None,
            }
        )
        yield b"data: [DONE]\n\n"

    response = StreamingResponse(body(), media_type="text/event-stream")
    result = _openai_stream_to_responses(response, "gateway-model")
    events = _collect_responses_events(result)

    assert not any(event.get("type") == "response.failed" for event in events)
    completed = [event for event in events if event.get("type") == "response.completed"]
    assert completed, "terminal null-error usage chunk must not suppress response.completed"
    usage = completed[-1]["response"]["usage"]
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["total_tokens"] == 15


def test_anthropic_converter_null_error_terminal_usage_completes_with_real_usage() -> None:
    async def body():
        yield _sse_line({"id": "a1", "choices": [{"delta": {"content": "Hello"}}]})
        yield _sse_line(
            {
                "id": "a1",
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
                "error": None,
            }
        )
        yield b"data: [DONE]\n\n"

    response = StreamingResponse(body(), media_type="text/event-stream")
    result = _openai_stream_to_anthropic(None, response, "gateway-model")
    events = _collect_anthropic_events(result)

    names = [event["event"] for event in events]
    assert "error" not in names, (
        "terminal null-error usage chunk must not emit an anthropic error event"
    )
    assert names[-1] == "message_stop"
    message_delta = [event for event in events if event["event"] == "message_delta"][-1]
    assert message_delta["data"]["usage"]["input_tokens"] == 10
    assert message_delta["data"]["usage"]["output_tokens"] == 5
