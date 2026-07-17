import asyncio

import pytest

from llm_gateway_core.services.stream_observation import (
    STREAM_OBSERVATION_BUFFER_MAX_BYTES,
    STREAM_OBSERVATION_BUFFER_MAX_ITEMS,
    StreamObservationCapacity,
    StreamObservationCapacityExhausted,
    StreamObservationStateError,
    StreamObservationTooLarge,
)
from tests._async_compat import run_async


def test_capacity_defaults_and_exact_partial_release() -> None:
    async def scenario() -> None:
        capacity = StreamObservationCapacity()
        initial = capacity.snapshot
        assert initial.max_items == STREAM_OBSERVATION_BUFFER_MAX_ITEMS
        assert initial.max_bytes == STREAM_OBSERVATION_BUFFER_MAX_BYTES
        assert initial.active_items == 0
        assert initial.active_bytes == 0

        lease = await capacity.acquire(6)
        assert lease.remaining_bytes == 6
        lease.consume(2)
        assert lease.remaining_bytes == 4
        assert capacity.snapshot.active_items == 1
        assert capacity.snapshot.active_bytes == 4

        lease.consume(4)
        assert lease.released is True
        assert capacity.snapshot.active_items == 0
        assert capacity.snapshot.active_bytes == 0
        assert capacity.snapshot.high_water_items == 1
        assert capacity.snapshot.high_water_bytes == 6

        lease.release_all()
        assert capacity.snapshot.active_items == 0

    run_async(scenario())


def test_capacity_waiters_are_strict_fifo_for_item_and_byte_capacity() -> None:
    async def scenario() -> None:
        capacity = StreamObservationCapacity(max_items=2, max_bytes=5)
        initial = await capacity.acquire(4)
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()

        async def reserve(byte_count: int, entered: asyncio.Event):
            entered.set()
            return await capacity.acquire(byte_count)

        first_waiter = asyncio.create_task(reserve(4, first_entered))
        await first_entered.wait()
        assert capacity.snapshot.waiters == 1

        second_waiter = asyncio.create_task(reserve(1, second_entered))
        await second_entered.wait()
        assert capacity.snapshot.waiters == 2
        assert not second_waiter.done()

        initial.release_all()
        first_lease = await first_waiter
        second_lease = await second_waiter
        assert first_lease.remaining_bytes == 4
        assert second_lease.remaining_bytes == 1
        assert capacity.snapshot.active_items == 2
        assert capacity.snapshot.active_bytes == 5

        first_lease.release_all()
        second_lease.release_all()

    run_async(scenario())


def test_try_acquire_fails_fast_without_bypassing_fifo_waiters() -> None:
    async def scenario() -> None:
        capacity = StreamObservationCapacity(max_items=3, max_bytes=8)
        active = await capacity.acquire(6)
        blocked = asyncio.create_task(capacity.acquire(3))
        await asyncio.sleep(0)
        assert capacity.snapshot.waiters == 1

        with pytest.raises(StreamObservationCapacityExhausted) as exhausted:
            capacity.try_acquire(1)
        assert exhausted.value.reason_code == "capacity_exhausted"
        assert exhausted.value.requested_bytes == 1
        assert exhausted.value.active_items == 1
        assert exhausted.value.active_bytes == 6
        assert exhausted.value.waiters == 1
        assert capacity.snapshot.active_items == 1
        assert capacity.snapshot.active_bytes == 6
        assert capacity.snapshot.waiters == 1
        assert capacity.snapshot.failures == 1
        assert capacity.snapshot.last_reason_code == "capacity_exhausted"

        active.release_all()
        blocked_lease = await blocked
        immediate = capacity.try_acquire(5)
        assert capacity.snapshot.active_items == 2
        assert capacity.snapshot.active_bytes == 8
        blocked_lease.release_all()
        immediate.release_all()

    run_async(scenario())


def test_try_acquire_preserves_too_large_and_closed_errors() -> None:
    async def scenario() -> None:
        capacity = StreamObservationCapacity(max_items=1, max_bytes=4)

        with pytest.raises(StreamObservationTooLarge) as too_large:
            capacity.try_acquire(5)
        assert too_large.value.reason_code == "acquire_too_large"

        lease = capacity.try_acquire(4)
        capacity.close()
        with pytest.raises(StreamObservationStateError) as closed:
            capacity.try_acquire(1)
        assert closed.value.reason_code == "capacity_closed"
        lease.release_all()

    run_async(scenario())


def test_item_slot_can_be_released_while_retained_bytes_stay_accounted() -> None:
    async def scenario() -> None:
        capacity = StreamObservationCapacity(max_items=1, max_bytes=8)
        retained = await capacity.acquire(4)
        retained.release_item()

        snapshot = capacity.snapshot
        assert snapshot.active_items == 0
        assert snapshot.active_bytes == 4

        next_lease = await capacity.acquire(4)
        retained.absorb(next_lease)
        assert next_lease.released is True
        assert capacity.snapshot.active_items == 0
        assert capacity.snapshot.active_bytes == 8
        assert retained.remaining_bytes == 8

        retained.consume(3)
        assert capacity.snapshot.active_bytes == 5
        retained.release_all()
        assert capacity.snapshot.active_items == 0
        assert capacity.snapshot.active_bytes == 0

    run_async(scenario())


def test_cancelled_waiter_is_removed_without_capacity_leak() -> None:
    async def scenario() -> None:
        capacity = StreamObservationCapacity(max_items=1, max_bytes=4)
        active = await capacity.acquire(4)
        entered = asyncio.Event()

        async def wait_for_capacity() -> None:
            entered.set()
            await capacity.acquire(1)

        waiter = asyncio.create_task(wait_for_capacity())
        await entered.wait()
        assert capacity.snapshot.waiters == 1

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert capacity.snapshot.waiters == 0
        assert capacity.snapshot.active_items == 1
        assert capacity.snapshot.active_bytes == 4

        active.release_all()
        replacement = await capacity.acquire(4)
        replacement.release_all()

    run_async(scenario())


def test_cancellation_after_grant_releases_the_unobserved_lease() -> None:
    async def scenario() -> None:
        capacity = StreamObservationCapacity(max_items=1, max_bytes=4)
        active = await capacity.acquire(4)
        entered = asyncio.Event()

        async def wait_for_capacity() -> None:
            entered.set()
            await capacity.acquire(4)

        waiter = asyncio.create_task(wait_for_capacity())
        await entered.wait()
        active.release_all()
        waiter.cancel()

        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert capacity.snapshot.active_items == 0
        assert capacity.snapshot.active_bytes == 0
        assert capacity.snapshot.waiters == 0

    run_async(scenario())


def test_too_large_and_lease_state_errors_are_safe_and_counted() -> None:
    async def scenario() -> None:
        capacity = StreamObservationCapacity(max_items=1, max_bytes=4)

        with pytest.raises(StreamObservationTooLarge) as too_large:
            await capacity.acquire(5)
        assert too_large.value.reason_code == "acquire_too_large"
        assert too_large.value.observed_bytes == 5
        assert "5" in str(too_large.value)
        assert capacity.snapshot.last_reason_code == "acquire_too_large"

        lease = await capacity.acquire(4)
        with pytest.raises(StreamObservationStateError) as state_error:
            lease.consume(5)
        assert state_error.value.reason_code == "lease_bytes_exceeded"
        assert capacity.snapshot.active_items == 1
        assert capacity.snapshot.active_bytes == 4
        assert capacity.snapshot.failures == 2
        lease.release_all()

    run_async(scenario())


def test_close_wakes_waiters_and_rejects_future_admission() -> None:
    async def scenario() -> None:
        capacity = StreamObservationCapacity(max_items=1, max_bytes=4)
        active = await capacity.acquire(4)
        entered = asyncio.Event()

        async def wait_for_capacity() -> None:
            entered.set()
            await capacity.acquire(1)

        waiter = asyncio.create_task(wait_for_capacity())
        await entered.wait()
        capacity.close(reason="shutdown")

        with pytest.raises(StreamObservationStateError) as waiter_error:
            await waiter
        assert waiter_error.value.reason_code == "shutdown"
        assert capacity.snapshot.waiters == 0
        assert capacity.snapshot.last_reason_code == "shutdown"

        with pytest.raises(StreamObservationStateError) as admission_error:
            await capacity.acquire(1)
        assert admission_error.value.reason_code == "capacity_closed"
        active.release_all()

    run_async(scenario())


def test_record_failure_only_retains_a_safe_reason_code() -> None:
    async def scenario() -> None:
        capacity = StreamObservationCapacity(max_items=1, max_bytes=4)
        capacity.record_failure("parser_failure")
        assert capacity.snapshot.failures == 1
        assert capacity.snapshot.last_reason_code == "parser_failure"

        capacity.record_failure("unsafe reason with payload")
        assert capacity.snapshot.failures == 2
        assert capacity.snapshot.last_reason_code == "state_error"

    run_async(scenario())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_items": 0}, "max_items"),
        ({"max_items": True}, "max_items"),
        ({"max_bytes": -1}, "max_bytes"),
    ],
)
def test_capacity_rejects_invalid_limits(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        StreamObservationCapacity(**kwargs)
