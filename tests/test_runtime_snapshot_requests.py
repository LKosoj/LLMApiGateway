from __future__ import annotations

import asyncio
import json
import unittest
from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from starlette.requests import Request

from llm_gateway_core.middleware.content_size import PRE_ROUTE_REJECTED_STATE_KEY
from llm_gateway_core.middleware.runtime_snapshot import (
    RUNTIME_INDEPENDENT_PATHS,
    RuntimeAvailabilityMiddleware,
    RuntimeSnapshotMiddleware,
    retain_request_runtime,
)
from llm_gateway_core.services.runtime_config import (
    AppServices,
    RuntimeGenerationManager,
    RuntimeLease,
    RuntimeManagerStateError,
    RuntimeManagerStatus,
    RuntimeSnapshot,
)
from llm_gateway_core.services.stream_observation import StreamObservationCapacity
from llm_gateway_core.services.upload_admission import UploadAdmission
from tests._async_compat import run_async
from tests.runtime_test_support import (
    install_test_runtime_snapshot,
    publish_test_runtime_snapshot,
)


def _snapshot(
    generation: int,
    *,
    clients: dict[str, object] | None = None,
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        generation=generation,
        config_loader=SimpleNamespace(
            providers_config={},
            fallback_rules={},
            operation_rules={},
            fusion_rules={},
            model_rules={},
            router_rules={},
        ),
        operation_dispatcher=Mock(name=f"dispatcher-{generation}"),
        fusion_service=Mock(name=f"fusion-{generation}"),
        router_model_service=Mock(name=f"router-{generation}"),
        provider_models_service=Mock(name=f"models-{generation}"),
        proxy_http_clients=clients or {},
        cost_rate_registry={},
    )


def _http_scope(
    *,
    manager: object | None = None,
    state: object = None,
    include_state: bool = True,
    include_app: bool = True,
) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/models",
        "raw_path": b"/v1/models",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    if include_app:
        dependencies = {
            name: Mock(name=name)
            for name in AppServices.__dataclass_fields__
        }
        dependencies["runtime_manager"] = manager
        dependencies["stream_observation_capacity"] = StreamObservationCapacity()
        dependencies["stream_event_max_bytes"] = 1_048_576
        dependencies["json_response_capacity"] = StreamObservationCapacity()
        dependencies["json_response_max_bytes"] = 8_388_608
        dependencies["upload_admission"] = UploadAdmission(max_bytes=1_048_576)
        dependencies["upload_admission_timeout_seconds"] = 5.0
        services = AppServices(**dependencies)
        scope["app"] = SimpleNamespace(
            state=SimpleNamespace(services=services)
        )
    if include_state:
        scope["state"] = {} if state is None else state
    return scope


async def _disconnect_receive() -> dict[str, str]:
    return {"type": "http.disconnect"}


async def _unused_receive() -> dict[str, str]:
    raise AssertionError("receive must not be called")


class RuntimeSnapshotMiddlewareTests(unittest.TestCase):
    def test_availability_requires_running_typed_runtime_without_taking_lease(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            scope = _http_scope(manager=manager)
            downstream = AsyncMock()
            send = AsyncMock()

            with patch.object(
                manager,
                "acquire_current",
                side_effect=AssertionError("availability must not take a lease"),
            ) as acquire_current:
                await RuntimeAvailabilityMiddleware(downstream)(
                    scope,
                    _unused_receive,
                    send,
                )

            downstream.assert_awaited_once()
            acquire_current.assert_not_called()
            send.assert_not_awaited()
            await manager.shutdown()

        run_async(scenario())

    def test_availability_rejects_non_running_runtime_before_receive(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            scope = _http_scope(manager=manager)
            scope["state"]["llmgateway_request_id"] = "availability-request"
            downstream = AsyncMock()
            sent = []

            async def send(message) -> None:
                sent.append(message)

            await RuntimeAvailabilityMiddleware(downstream)(
                scope,
                _unused_receive,
                send,
            )

            downstream.assert_not_awaited()
            self.assertEqual(sent[0]["status"], 503)
            self.assertIn(
                (b"x-request-id", b"availability-request"),
                sent[0]["headers"],
            )

        run_async(scenario())

    def test_runtime_independent_paths_bypass_availability_and_snapshot(self) -> None:
        async def scenario() -> None:
            for path in RUNTIME_INDEPENDENT_PATHS:
                with self.subTest(path=path):
                    scope = _http_scope(include_app=False)
                    scope["path"] = path
                    downstream = AsyncMock()
                    send = AsyncMock()
                    receive = AsyncMock()
                    app = RuntimeAvailabilityMiddleware(
                        RuntimeSnapshotMiddleware(downstream)
                    )

                    await app(scope, receive, send)

                    downstream.assert_awaited_once_with(scope, receive, send)
                    send.assert_not_awaited()

        run_async(scenario())

    def test_non_http_scope_is_passed_through_untouched(self) -> None:
        async def scenario() -> None:
            scope: dict[str, Any] = {"type": "lifespan", "sentinel": object()}
            observed: list[object] = []

            async def downstream(received_scope, receive, send) -> None:
                observed.extend((received_scope, receive, send))

            receive = AsyncMock()
            send = AsyncMock()
            await RuntimeSnapshotMiddleware(downstream)(scope, receive, send)

            self.assertEqual(observed, [scope, receive, send])
            self.assertNotIn("state", scope)

        run_async(scenario())

    def test_existing_state_is_preserved_and_one_lease_covers_terminal_body(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            snapshot = _snapshot(1)
            install_test_runtime_snapshot(manager, snapshot)
            original_state = {"existing": object()}
            scope = _http_scope(manager=manager, state=original_state)
            messages: list[dict[str, Any]] = []

            async def send(message) -> None:
                messages.append(message)

            async def downstream(received_scope, receive, downstream_send) -> None:
                self.assertIs(received_scope["state"], original_state)
                self.assertIn("existing", received_scope["state"])
                self.assertIs(received_scope["state"]["runtime_snapshot"], snapshot)
                self.assertEqual(manager.active_leases[1], 1)
                await downstream_send(
                    {"type": "http.response.start", "status": 200, "headers": []}
                )
                await downstream_send(
                    {"type": "http.response.body", "body": b"{}"}
                )
                self.assertEqual(manager.active_leases[1], 1)

            with patch.object(
                manager,
                "acquire_current",
                wraps=manager.acquire_current,
            ) as acquire_current:
                await RuntimeSnapshotMiddleware(downstream)(
                    scope, _unused_receive, send
                )

            acquire_current.assert_called_once_with()
            self.assertEqual(manager.active_leases[1], 0)
            self.assertEqual(messages[-1]["type"], "http.response.body")
            self.assertNotIn("more_body", messages[-1])
            await manager.shutdown()

        run_async(scenario())

    def test_request_retains_exact_runtime_before_first_await_and_owns_children(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            snapshot = _snapshot(1)
            install_test_runtime_snapshot(manager, snapshot)
            scope = _http_scope(manager=manager)

            async def downstream(received_scope, receive, send) -> None:
                request = Request(received_scope, receive=receive)
                originating = next(
                    value
                    for value in received_scope["state"].values()
                    if isinstance(value, RuntimeLease)
                )
                first_child = retain_request_runtime(request)
                second_child = retain_request_runtime(request)

                self.assertIsNot(first_child, originating)
                self.assertIsNot(second_child, originating)
                self.assertIs(first_child.snapshot, snapshot)
                self.assertIs(second_child.snapshot, snapshot)
                self.assertEqual(manager.active_leases[1], 3)

                first_child.release()
                first_child.release()
                self.assertEqual(manager.active_leases[1], 2)
                second_child.release()
                self.assertEqual(manager.active_leases[1], 1)
                await send(
                    {"type": "http.response.body", "body": b"{}"}
                )

            await RuntimeSnapshotMiddleware(downstream)(
                scope, _unused_receive, AsyncMock()
            )

            self.assertEqual(manager.active_leases[1], 0)
            self.assertFalse(
                any(isinstance(value, RuntimeLease) for value in scope["state"].values())
            )
            await manager.shutdown()

        run_async(scenario())

    def test_middleware_removes_originating_lease_before_base_release(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            scope = _http_scope(manager=manager)
            key_present_at_release: list[bool] = []
            original_release = RuntimeLease.release

            def release(lease: RuntimeLease) -> None:
                key_present_at_release.append(
                    any(value is lease for value in scope["state"].values())
                )
                original_release(lease)

            with patch.object(RuntimeLease, "release", new=release):
                await RuntimeSnapshotMiddleware(AsyncMock())(
                    scope, _unused_receive, AsyncMock()
                )

            self.assertEqual(key_present_at_release, [False])
            self.assertEqual(manager.active_leases[1], 0)
            await manager.shutdown()

        run_async(scenario())

    def test_detached_child_keeps_retired_request_generation_alive(self) -> None:
        async def scenario() -> None:
            old_client = Mock()
            old_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            snapshot = _snapshot(1, clients={"old": old_client})
            install_test_runtime_snapshot(manager, snapshot)
            scope = _http_scope(manager=manager)
            finish_child = asyncio.Event()
            detached_tasks: list[asyncio.Task[None]] = []
            retained_snapshots: list[RuntimeSnapshot] = []

            async def detached(child: RuntimeLease) -> None:
                try:
                    await finish_child.wait()
                finally:
                    child.release()

            async def downstream(received_scope, receive, send) -> None:
                publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
                self.assertEqual(manager.active_leases[1], 1)
                old_client.aclose.assert_not_awaited()
                child = retain_request_runtime(
                    Request(received_scope, receive=receive)
                )
                retained_snapshots.append(child.snapshot)
                self.assertEqual(manager.active_leases[1], 2)
                detached_tasks.append(asyncio.create_task(detached(child)))
                await asyncio.sleep(0)

            await RuntimeSnapshotMiddleware(downstream)(
                scope, _unused_receive, AsyncMock()
            )

            self.assertEqual(manager.active_leases[1], 1)
            self.assertIs(retained_snapshots[0], snapshot)
            old_client.aclose.assert_not_awaited()

            finish_child.set()
            await detached_tasks[0]
            for _attempt in range(10):
                if old_client.aclose.await_count:
                    break
                await asyncio.sleep(0)

            old_client.aclose.assert_awaited_once()
            self.assertFalse(any(manager.active_leases.values()))
            self.assertFalse(
                any(isinstance(value, RuntimeLease) for value in scope["state"].values())
            )
            await manager.shutdown()

        run_async(scenario())

    def test_retain_request_runtime_fails_closed_for_missing_and_wrong_values(self) -> None:
        async def scenario() -> None:
            safe_message = "Request runtime lease is unavailable."
            missing_request = Request(_http_scope(manager=None), receive=_unused_receive)

            with self.assertRaises(RuntimeManagerStateError) as missing:
                retain_request_runtime(missing_request)
            self.assertEqual(str(missing.exception), safe_message)

            with self.assertRaises(RuntimeManagerStateError) as wrong_request:
                retain_request_runtime(SimpleNamespace(state={}))  # type: ignore[arg-type]
            self.assertEqual(str(wrong_request.exception), safe_message)

            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            scope = _http_scope(manager=manager)
            secret = "https://user:credential@proxy.invalid"

            async def downstream(received_scope, receive, send) -> None:
                request = Request(received_scope, receive=receive)
                lease_entries = [
                    (key, value)
                    for key, value in received_scope["state"].items()
                    if isinstance(value, RuntimeLease)
                ]
                self.assertEqual(len(lease_entries), 1)
                key, lease = lease_entries[0]
                received_scope["state"][key] = secret

                with self.assertRaises(RuntimeManagerStateError) as wrong_value:
                    retain_request_runtime(request)
                self.assertEqual(str(wrong_value.exception), safe_message)
                self.assertNotIn("credential", str(wrong_value.exception))
                received_scope["state"][key] = lease

            await RuntimeSnapshotMiddleware(downstream)(
                scope, _unused_receive, AsyncMock()
            )
            self.assertEqual(manager.active_leases[1], 0)
            await manager.shutdown()

        run_async(scenario())

    def test_retain_request_runtime_rejects_released_originating_lease(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))

            async def downstream(scope, receive, send) -> None:
                request = Request(scope, receive=receive)
                originating = next(
                    value
                    for value in scope["state"].values()
                    if isinstance(value, RuntimeLease)
                )
                originating.release()

                with self.assertRaises(RuntimeManagerStateError) as released:
                    retain_request_runtime(request)
                self.assertEqual(
                    str(released.exception),
                    "Request runtime lease is unavailable.",
                )

            await RuntimeSnapshotMiddleware(downstream)(
                _http_scope(manager=manager), _unused_receive, AsyncMock()
            )
            self.assertEqual(manager.active_leases[1], 0)
            await manager.shutdown()

        run_async(scenario())

    def test_retain_request_runtime_rejects_foreign_event_loop(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))

            async def downstream(scope, receive, send) -> None:
                request = Request(scope, receive=receive)

                def retain_on_foreign_loop() -> str:
                    async def attempt() -> str:
                        try:
                            retain_request_runtime(request)
                        except RuntimeManagerStateError as exc:
                            return str(exc)
                        raise AssertionError("Foreign-loop retain unexpectedly succeeded")

                    return run_async(attempt())

                error = await asyncio.to_thread(retain_on_foreign_loop)
                self.assertEqual(error, "Request runtime lease is unavailable.")
                self.assertEqual(manager.active_leases[1], 1)

            await RuntimeSnapshotMiddleware(downstream)(
                _http_scope(manager=manager), _unused_receive, AsyncMock()
            )
            self.assertEqual(manager.active_leases[1], 0)
            await manager.shutdown()

        run_async(scenario())

    def test_shutdown_rejects_new_request_children_and_drains_base_lease(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            scope = _http_scope(manager=manager)
            shutdown_tasks: list[asyncio.Task[None]] = []

            async def downstream(received_scope, receive, send) -> None:
                request = Request(received_scope, receive=receive)
                shutdown_task = asyncio.create_task(manager.shutdown())
                shutdown_tasks.append(shutdown_task)
                await asyncio.sleep(0)

                self.assertEqual(manager.status, RuntimeManagerStatus.STOPPING)
                with self.assertRaises(RuntimeManagerStateError) as stopped:
                    retain_request_runtime(request)
                self.assertEqual(
                    str(stopped.exception),
                    "Request runtime lease is unavailable.",
                )

            await RuntimeSnapshotMiddleware(downstream)(
                scope, _unused_receive, AsyncMock()
            )
            await shutdown_tasks[0]

            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPED)
            self.assertEqual(manager.cleanup_task_count, 0)
            self.assertFalse(
                any(isinstance(value, RuntimeLease) for value in scope["state"].values())
            )

        run_async(scenario())

    def test_blocked_sse_keeps_retired_generation_alive_until_terminal_body(self) -> None:
        async def scenario() -> None:
            old_client = Mock()
            old_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            first = _snapshot(1, clients={"old": old_client})
            install_test_runtime_snapshot(manager, first)
            entered_stream = asyncio.Event()
            finish_stream = asyncio.Event()
            messages: list[dict[str, Any]] = []
            seen_snapshots: list[RuntimeSnapshot] = []
            scope = _http_scope(manager=manager)

            async def send(message) -> None:
                messages.append(message)

            async def downstream(scope, receive, downstream_send) -> None:
                seen_snapshots.append(scope["state"]["runtime_snapshot"])
                await downstream_send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")],
                    }
                )
                await downstream_send(
                    {
                        "type": "http.response.body",
                        "body": b"data: first\n\n",
                        "more_body": True,
                    }
                )
                entered_stream.set()
                await finish_stream.wait()
                await downstream_send(
                    {"type": "http.response.body", "body": b"", "more_body": False}
                )

            task = asyncio.create_task(
                RuntimeSnapshotMiddleware(downstream)(
                    scope, _unused_receive, send
                )
            )
            await entered_stream.wait()
            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await asyncio.sleep(0)

            self.assertEqual(manager.active_leases[1], 1)
            old_client.aclose.assert_not_awaited()
            self.assertEqual(seen_snapshots, [first])

            finish_stream.set()
            await task
            await asyncio.sleep(0)

            old_client.aclose.assert_awaited_once()
            self.assertEqual(messages[-1].get("more_body"), False)
            self.assertFalse(
                any(isinstance(value, RuntimeLease) for value in scope["state"].values())
            )
            await manager.shutdown()

        run_async(scenario())

    def test_downstream_exception_is_preserved_and_releases_lease(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            failure = RuntimeError("downstream sentinel")
            scope = _http_scope(manager=manager)

            async def downstream(scope, receive, send) -> None:
                raise failure

            with self.assertRaises(RuntimeError) as caught:
                await RuntimeSnapshotMiddleware(downstream)(
                    scope, _unused_receive, AsyncMock()
                )

            self.assertIs(caught.exception, failure)
            self.assertEqual(manager.active_leases[1], 0)
            self.assertFalse(
                any(isinstance(value, RuntimeLease) for value in scope["state"].values())
            )
            await manager.shutdown()

        run_async(scenario())

    def test_cancellation_is_preserved_and_releases_lease(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            entered = asyncio.Event()
            scope = _http_scope(manager=manager)

            async def downstream(scope, receive, send) -> None:
                entered.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(
                RuntimeSnapshotMiddleware(downstream)(
                    scope, _unused_receive, AsyncMock()
                )
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertEqual(manager.active_leases[1], 0)
            self.assertFalse(
                any(isinstance(value, RuntimeLease) for value in scope["state"].values())
            )
            await manager.shutdown()

        run_async(scenario())

    def test_disconnect_return_releases_lease(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            scope = _http_scope(manager=manager)

            async def downstream(scope, receive, send) -> None:
                self.assertEqual(await receive(), {"type": "http.disconnect"})

            await RuntimeSnapshotMiddleware(downstream)(
                scope, _disconnect_receive, AsyncMock()
            )

            self.assertEqual(manager.active_leases[1], 0)
            self.assertFalse(
                any(isinstance(value, RuntimeLease) for value in scope["state"].values())
            )
            await manager.shutdown()

        run_async(scenario())

    def test_send_failure_is_preserved_and_releases_lease(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            failure = ConnectionError("send sentinel")
            scope = _http_scope(manager=manager)

            async def downstream(scope, receive, send) -> None:
                await send(
                    {"type": "http.response.start", "status": 200, "headers": []}
                )

            async def failing_send(message) -> None:
                raise failure

            with self.assertRaises(ConnectionError) as caught:
                await RuntimeSnapshotMiddleware(downstream)(
                    scope, _unused_receive, failing_send
                )

            self.assertIs(caught.exception, failure)
            self.assertEqual(manager.active_leases[1], 0)
            self.assertFalse(
                any(isinstance(value, RuntimeLease) for value in scope["state"].values())
            )
            await manager.shutdown()

        run_async(scenario())

    def test_missing_or_wrong_manager_chain_returns_safe_terminal_503(self) -> None:
        async def scenario() -> None:
            downstream = AsyncMock()
            scopes = [
                _http_scope(include_app=False),
                {**_http_scope(), "app": SimpleNamespace()},
                {
                    **_http_scope(),
                    "app": SimpleNamespace(state=SimpleNamespace()),
                },
                {
                    **_http_scope(),
                    "app": SimpleNamespace(
                        state=SimpleNamespace(services=SimpleNamespace())
                    ),
                },
                _http_scope(manager="https://user:secret@proxy.invalid"),
            ]

            for scope in scopes:
                messages: list[dict[str, Any]] = []

                async def send(message) -> None:
                    messages.append(message)

                await RuntimeSnapshotMiddleware(downstream)(
                    scope, _unused_receive, send
                )
                self._assert_safe_503(messages)

            downstream.assert_not_awaited()

        run_async(scenario())

    def test_new_stopping_and_stopped_managers_return_safe_503(self) -> None:
        async def scenario() -> None:
            downstream = AsyncMock()

            new_manager = RuntimeGenerationManager()
            await self._assert_manager_unavailable(new_manager, downstream)
            await new_manager.shutdown()

            stopping_manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(stopping_manager, _snapshot(1))
            blocker = stopping_manager.acquire_current()
            shutdown_task = asyncio.create_task(stopping_manager.shutdown())
            await asyncio.sleep(0)
            self.assertEqual(stopping_manager.status, RuntimeManagerStatus.STOPPING)
            await self._assert_manager_unavailable(stopping_manager, downstream)
            blocker.release()
            await shutdown_task

            stopped_manager = RuntimeGenerationManager()
            await stopped_manager.shutdown()
            self.assertEqual(stopped_manager.status, RuntimeManagerStatus.STOPPED)
            await self._assert_manager_unavailable(stopped_manager, downstream)

            downstream.assert_not_awaited()

        run_async(scenario())

    def test_503_passes_through_existing_request_id_without_generating_one(self) -> None:
        async def scenario() -> None:
            state = {"llmgateway_request_id": "req-existing-123"}
            scope = _http_scope(manager=None, state=state)
            messages: list[dict[str, Any]] = []

            async def send(message) -> None:
                messages.append(message)

            await RuntimeSnapshotMiddleware(AsyncMock())(
                scope, _unused_receive, send
            )

            headers = dict(messages[0]["headers"])
            payload = json.loads(messages[1]["body"])
            self.assertEqual(headers[b"x-request-id"], b"req-existing-123")
            self.assertEqual(payload["request_id"], "req-existing-123")
            self.assertEqual(payload["error"]["request_id"], "req-existing-123")
            self.assertEqual(
                state,
                {
                    "llmgateway_request_id": "req-existing-123",
                    PRE_ROUTE_REJECTED_STATE_KEY: True,
                },
            )

        run_async(scenario())

    def test_bad_scope_state_write_does_not_leak_acquired_lease(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            immutable_state = MappingProxyType({"existing": object()})
            scope = _http_scope(manager=manager, state=immutable_state)
            downstream = AsyncMock()

            with self.assertRaises(TypeError):
                await RuntimeSnapshotMiddleware(downstream)(
                    scope, _unused_receive, AsyncMock()
                )

            downstream.assert_not_awaited()
            self.assertEqual(manager.active_leases[1], 0)
            self.assertIs(scope["state"], immutable_state)
            await manager.shutdown()

        run_async(scenario())

    async def _assert_manager_unavailable(
        self,
        manager: RuntimeGenerationManager,
        downstream: AsyncMock,
    ) -> None:
        messages: list[dict[str, Any]] = []

        async def send(message) -> None:
            messages.append(message)

        await RuntimeSnapshotMiddleware(downstream)(
            _http_scope(manager=manager), _unused_receive, send
        )
        self._assert_safe_503(messages)

    def _assert_safe_503(self, messages: list[dict[str, Any]]) -> None:
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["type"], "http.response.start")
        self.assertEqual(messages[0]["status"], 503)
        self.assertEqual(messages[1]["type"], "http.response.body")
        self.assertNotIn("more_body", messages[1])
        payload = json.loads(messages[1]["body"])
        self.assertEqual(payload["error"]["code"], "runtime_unavailable")
        self.assertNotIn("secret", messages[1]["body"].decode("utf-8").lower())
