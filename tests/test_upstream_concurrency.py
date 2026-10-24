from concurrent.futures import ThreadPoolExecutor
import asyncio

from fastapi.responses import StreamingResponse

from llm_gateway_core.services.upstream_routing_state import (
    UpstreamQuotaLimits,
    UpstreamRoutingState,
    fingerprint_api_key,
)
from llm_gateway_core.services.request_handler import (
    _OwnedStreamingResponse,
    _StreamResourceOwner,
    attach_streaming_response_close_callback,
)


def test_concurrency_admission_is_atomic_and_shared_between_models():
    state = UpstreamRoutingState()

    def acquire(index: int):
        return state.acquire_concurrency("provider", "shared-key", max_concurrency=1)

    with ThreadPoolExecutor(max_workers=8) as executor:
        leases = list(executor.map(acquire, range(16)))

    acquired = [lease for lease in leases if lease is not None]
    assert len(acquired) == 1
    acquired[0].release()


def test_concurrency_selection_scopes_and_hot_enable():
    state = UpstreamRoutingState()
    fingerprint = fingerprint_api_key("key-a")
    first = state.acquire_concurrency("provider", fingerprint, max_concurrency=None)
    second = state.acquire_concurrency("provider", fingerprint, max_concurrency=None)
    assert first is not None
    assert second is not None

    limits = UpstreamQuotaLimits(max_concurrency=1)
    assert not state.select_key("provider", "model-b", ["key-a"], key_limits=limits).available
    assert state.select_key("provider", "model-b", ["key-b"], key_limits=limits).available
    assert state.select_key("other-provider", "model-b", ["key-a"], key_limits=limits).available

    first.release()
    assert not state.select_key("provider", "model-b", ["key-a"], key_limits=limits).available
    second.release()
    assert state.select_key("provider", "model-b", ["key-a"], key_limits=limits).available


def test_concurrency_lease_releases_only_its_own_slot_once():
    state = UpstreamRoutingState()
    first = state.acquire_concurrency("provider", "key", max_concurrency=2)
    second = state.acquire_concurrency("provider", "key", max_concurrency=2)
    assert first is not None
    assert second is not None

    first.release()
    first.release()
    third = state.acquire_concurrency("provider", "key", max_concurrency=2)
    assert third is not None
    assert state.acquire_concurrency("provider", "key", max_concurrency=2) is None

    second.release()
    assert state.acquire_concurrency("provider", "key", max_concurrency=2) is not None


def test_stream_close_before_first_item_releases_concurrency_once():
    released = 0

    async def source():
        yield b"data: first\n\n"

    async def release() -> None:
        nonlocal released
        released += 1

    async def close() -> None:
        response = attach_streaming_response_close_callback(StreamingResponse(source()), release)
        await response.aclose()
        await response.aclose()

    asyncio.run(close())
    assert released == 1


def test_stream_close_callback_runs_when_owner_close_fails():
    released = 0

    async def source():
        yield b"data: first\n\n"

    async def failing_owner_close() -> None:
        raise RuntimeError("owner failed")

    async def release() -> None:
        nonlocal released
        released += 1

    async def close() -> None:
        response = _OwnedStreamingResponse(
            source(), stream_owner=_StreamResourceOwner(failing_owner_close)
        )
        response.add_close_callback(release)
        try:
            await response.aclose()
        except RuntimeError as exc:
            assert str(exc) == "owner failed"
        else:
            raise AssertionError("owner failure was swallowed")

    asyncio.run(close())
    assert released == 1
