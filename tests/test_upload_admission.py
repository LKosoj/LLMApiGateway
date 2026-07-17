from __future__ import annotations

import asyncio

import pytest

from llm_gateway_core.services.upload_admission import (
    UploadAdmission,
    UploadAdmissionClosed,
    UploadAdmissionTimeout,
    UploadAdmissionTooLarge,
)
from tests._async_compat import run_async


def test_exact_capacity_and_oversized_weight() -> None:
    async def scenario() -> None:
        admission = UploadAdmission(max_bytes=10)
        lease = await admission.acquire(10)
        assert admission.snapshot.active_bytes == 10
        lease.release()

        with pytest.raises(UploadAdmissionTooLarge) as error:
            await admission.acquire(11)
        assert error.value.reason_code == "weight_too_large"
        assert admission.snapshot.active_bytes == 0

    run_async(scenario())


def test_waiters_are_strict_fifo() -> None:
    async def scenario() -> None:
        admission = UploadAdmission(max_bytes=10)
        active = await admission.acquire(6)
        order: list[str] = []

        async def acquire(name: str, weight: int):
            lease = await admission.acquire(weight)
            order.append(name)
            return lease

        head = asyncio.create_task(acquire("head", 7))
        small = asyncio.create_task(acquire("small", 4))
        await asyncio.sleep(0)
        assert admission.snapshot.waiters == 2

        active.release()
        head_lease = await head
        await asyncio.sleep(0)
        assert order == ["head"]
        assert not small.done()

        head_lease.release()
        small_lease = await small
        assert order == ["head", "small"]
        small_lease.release()

    run_async(scenario())


def test_timeout_removes_waiter_without_leak() -> None:
    async def scenario() -> None:
        admission = UploadAdmission(max_bytes=4)
        active = await admission.acquire(4)

        with pytest.raises(UploadAdmissionTimeout):
            await admission.acquire(1, timeout_seconds=0.01)

        assert admission.snapshot.waiters == 0
        assert admission.snapshot.active_bytes == 4
        active.release()
        assert admission.snapshot.active_bytes == 0

    run_async(scenario())


def test_cancellation_before_and_after_grant_releases_capacity() -> None:
    async def scenario() -> None:
        admission = UploadAdmission(max_bytes=4)
        active = await admission.acquire(4)

        waiting = asyncio.create_task(admission.acquire(2))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert admission.snapshot.waiters == 0

        granted_then_cancelled = asyncio.create_task(admission.acquire(4))
        await asyncio.sleep(0)
        active.release()
        granted_then_cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await granted_then_cancelled
        assert admission.snapshot.active_bytes == 0
        assert admission.snapshot.active_leases == 0

    run_async(scenario())


def test_release_is_idempotent() -> None:
    async def scenario() -> None:
        admission = UploadAdmission(max_bytes=8)
        lease = await admission.acquire(5)
        lease.release()
        lease.release()
        assert lease.released is True
        assert admission.snapshot.active_bytes == 0
        assert admission.snapshot.active_leases == 0

    run_async(scenario())


def test_close_wakes_waiters_and_rejects_new_admission() -> None:
    async def scenario() -> None:
        admission = UploadAdmission(max_bytes=4)
        active = await admission.acquire(4)
        waiting = asyncio.create_task(admission.acquire(1))
        await asyncio.sleep(0)

        admission.close()

        with pytest.raises(UploadAdmissionClosed):
            await waiting
        with pytest.raises(UploadAdmissionClosed):
            await admission.acquire(1)
        active.release()
        assert admission.snapshot.closed is True
        assert admission.snapshot.active_bytes == 0
        assert admission.snapshot.waiters == 0

    run_async(scenario())


def test_snapshot_tracks_high_water_and_finishes_zero() -> None:
    async def scenario() -> None:
        admission = UploadAdmission(max_bytes=10)
        first = await admission.acquire(3)
        second = await admission.acquire(5)
        assert admission.snapshot.high_water_bytes == 8
        assert admission.snapshot.active_leases == 2
        first.release()
        second.release()
        snapshot = admission.snapshot
        assert snapshot.active_bytes == 0
        assert snapshot.active_leases == 0
        assert snapshot.waiters == 0
        assert snapshot.high_water_bytes <= snapshot.max_bytes

    run_async(scenario())
