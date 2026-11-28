import asyncio

import httpx
import pytest

from llm_gateway_core.api.v1.web_engine_errors import (
    AuthError,
    BotWallError,
    FreeEngineError,
    InvalidResponseError,
    QuotaError,
    TransientError,
)
from llm_gateway_core.services.upstream_routing_state import (
    DEFAULT_COOLDOWN_SECONDS,
    KEYLESS_FINGERPRINT,
    RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS,
    UpstreamRoutingState,
)
from llm_gateway_core.services.web_engine_routing import (
    BOT_WALL_COOLDOWN_SECONDS,
    DEFAULT_ENGINE_CONCURRENCY,
    ENGINE_CONCURRENCY,
    WEB_ENGINE_PROVIDER,
    classify_engine_failure,
    reset_engine_semaphores,
    run_engine,
)
from tests._async_compat import run_async


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def time(self) -> float:
        return self.value

    def monotonic(self) -> float:
        return self.value


@pytest.fixture(autouse=True)
def _fresh_semaphores():
    reset_engine_semaphores()
    yield
    reset_engine_semaphores()


def _state(clock: _Clock) -> UpstreamRoutingState:
    return UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)


def _cooldown(state: UpstreamRoutingState, engine: str) -> float | None:
    return state.cooldown_remaining_seconds(WEB_ENGINE_PROVIDER, engine, KEYLESS_FINGERPRINT)


def _http_status_error(status_code: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=httpx.Request("GET", "https://engine.example/search"),
        response=httpx.Response(status_code, headers=headers),
    )


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (FreeEngineError("base"), "unknown"),
        (BotWallError("challenge"), "bot-wall"),
        (QuotaError("limit"), "quota"),
        (AuthError("denied"), "auth"),
        (TransientError("network"), "transient"),
        (InvalidResponseError("bad payload"), "invalid-response"),
        (_http_status_error(401), "auth"),
        (_http_status_error(403), "auth"),
        (_http_status_error(402), "quota"),
        (_http_status_error(429), "quota"),
        (_http_status_error(500), "transient"),
        (_http_status_error(503), "transient"),
        (_http_status_error(404), "unknown"),
        (httpx.ReadTimeout("timed out"), "transient"),
        (httpx.ConnectError("connection refused"), "transient"),
        (ValueError("something else"), "unknown"),
    ],
)
def test_classify_engine_failure(exc: BaseException, expected: str) -> None:
    assert classify_engine_failure(exc) == expected


def test_engine_concurrency_table_matches_contract() -> None:
    assert ENGINE_CONCURRENCY == {
        "bing": 1,
        "ddg_html": 1,
        "ddg_lite": 1,
        "searxng": 1,
        "anysearch": 2,
        "youcom": 2,
        "firecrawl": 2,
        "keenable": 2,
    }
    assert DEFAULT_ENGINE_CONCURRENCY == 2


def test_cooldown_skip_returns_none_without_recording_failure() -> None:
    clock = _Clock()
    state = _state(clock)

    async def failing() -> list[dict]:
        raise BotWallError("challenge")

    async def scenario() -> None:
        with pytest.raises(BotWallError):
            await run_engine(state, "bing", failing)
        calls: list[int] = []

        async def factory() -> list[dict]:
            calls.append(1)
            return []

        assert await run_engine(state, "bing", factory) is None
        assert calls == []

    run_async(scenario())
    # The skip must not extend the cooldown with a fresh failure record.
    assert _cooldown(state, "bing") == BOT_WALL_COOLDOWN_SECONDS


def test_transient_failure_retried_once_then_succeeds() -> None:
    clock = _Clock()
    state = _state(clock)
    attempts = 0
    rows = [{"url": "https://example.com", "title": "t", "snippet": "s"}]

    async def factory() -> list[dict]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientError("network fault")
        return rows

    result = run_async(run_engine(state, "bing", factory))
    assert result == rows
    assert attempts == 2
    assert _cooldown(state, "bing") is None


def test_transient_failure_twice_records_cooldown_and_reraises() -> None:
    clock = _Clock()
    state = _state(clock)
    attempts = 0

    async def factory() -> list[dict]:
        nonlocal attempts
        attempts += 1
        raise TransientError(f"network fault {attempts}")

    async def scenario() -> None:
        with pytest.raises(TransientError, match="network fault 2"):
            await run_engine(state, "bing", factory)

    run_async(scenario())
    assert attempts == 2
    assert _cooldown(state, "bing") == DEFAULT_COOLDOWN_SECONDS


def test_bot_wall_sets_sixty_second_cooldown_without_retry() -> None:
    clock = _Clock()
    state = _state(clock)
    attempts = 0

    async def factory() -> list[dict]:
        nonlocal attempts
        attempts += 1
        raise BotWallError("anomaly detected")

    async def scenario() -> None:
        with pytest.raises(BotWallError):
            await run_engine(state, "ddg_html", factory)

    run_async(scenario())
    assert attempts == 1
    assert _cooldown(state, "ddg_html") == 60.0


def test_auth_error_sets_access_denied_cooldown() -> None:
    clock = _Clock()
    state = _state(clock)

    async def factory() -> list[dict]:
        raise AuthError("credentials required")

    async def scenario() -> None:
        with pytest.raises(AuthError):
            await run_engine(state, "anysearch", factory)

    run_async(scenario())
    assert _cooldown(state, "anysearch") == DEFAULT_COOLDOWN_SECONDS
    rows = state.get_status_rows()
    assert rows[0]["health_status"] == "invalid"


def test_httpx_403_sets_access_denied_cooldown() -> None:
    clock = _Clock()
    state = _state(clock)

    async def factory() -> list[dict]:
        raise _http_status_error(403)

    async def scenario() -> None:
        with pytest.raises(httpx.HTTPStatusError):
            await run_engine(state, "firecrawl", factory)

    run_async(scenario())
    assert _cooldown(state, "firecrawl") == DEFAULT_COOLDOWN_SECONDS


def test_invalid_response_reraises_without_cooldown() -> None:
    clock = _Clock()
    state = _state(clock)

    async def factory() -> list[dict]:
        raise InvalidResponseError("unexpected payload")

    async def scenario() -> None:
        with pytest.raises(InvalidResponseError):
            await run_engine(state, "keenable", factory)

    run_async(scenario())
    assert _cooldown(state, "keenable") is None


def test_quota_uses_existing_429_ladder() -> None:
    clock = _Clock()
    state = _state(clock)

    async def factory() -> list[dict]:
        raise QuotaError("rate limit exceeded")

    async def failing() -> None:
        with pytest.raises(QuotaError):
            await run_engine(state, "searxng", factory)

    run_async(failing())
    assert _cooldown(state, "searxng") == RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS

    clock.value += RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS + 1
    run_async(failing())
    # Second hit within the hour climbs the escalation ladder.
    assert _cooldown(state, "searxng") == 120.0


def test_quota_retry_after_header_is_a_floor() -> None:
    clock = _Clock()
    state = _state(clock)

    async def factory() -> list[dict]:
        raise _http_status_error(429, headers={"retry-after": "120"})

    async def scenario() -> None:
        with pytest.raises(httpx.HTTPStatusError):
            await run_engine(state, "youcom", factory)

    run_async(scenario())
    assert _cooldown(state, "youcom") == 120.0


def test_success_after_cooldown_expiry_resets_state() -> None:
    clock = _Clock()
    state = _state(clock)
    rows = [{"url": "https://example.com", "title": "t", "snippet": "s"}]

    async def failing() -> list[dict]:
        raise BotWallError("challenge")

    async def ok() -> list[dict]:
        return rows

    async def fail_first() -> None:
        with pytest.raises(BotWallError):
            await run_engine(state, "bing", failing)

    run_async(fail_first())
    clock.value += 61.0
    assert run_async(run_engine(state, "bing", ok)) == rows
    assert _cooldown(state, "bing") is None
    assert state.get_status_rows()[0]["health_status"] == "healthy"


def test_success_resets_rate_limit_ladder() -> None:
    clock = _Clock()
    state = _state(clock)

    async def quota() -> list[dict]:
        raise QuotaError("rate limit exceeded")

    async def ok() -> list[dict]:
        return []

    async def failing() -> None:
        with pytest.raises(QuotaError):
            await run_engine(state, "searxng", quota)

    run_async(failing())
    clock.value += RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS + 1
    run_async(run_engine(state, "searxng", ok))
    run_async(failing())
    # A served request ends the rate-limit episode: no escalation to 120s.
    assert _cooldown(state, "searxng") == RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS


def _concurrency_probe(engine: str, concurrency_limit: int | None = None) -> int:
    state = UpstreamRoutingState()

    async def scenario() -> int:
        active = 0
        max_active = 0

        async def factory() -> list[dict]:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return []

        await asyncio.gather(
            *(run_engine(state, engine, factory, concurrency_limit=concurrency_limit) for _ in range(4))
        )
        return max_active

    return run_async(scenario())


def test_semaphore_limits_html_engine_to_one() -> None:
    assert _concurrency_probe("bing") == 1


def test_semaphore_override_raises_limit() -> None:
    assert _concurrency_probe("bing", concurrency_limit=2) == 2


def test_unknown_engine_defaults_to_two() -> None:
    assert _concurrency_probe("custom_engine") == 2


def test_engines_do_not_share_cooldowns() -> None:
    clock = _Clock()
    state = _state(clock)
    rows = [{"url": "https://example.com", "title": "t", "snippet": "s"}]

    async def failing() -> list[dict]:
        raise BotWallError("challenge")

    async def ok() -> list[dict]:
        return rows

    async def scenario() -> None:
        with pytest.raises(BotWallError):
            await run_engine(state, "bing", failing)
        assert await run_engine(state, "anysearch", ok) == rows

    run_async(scenario())
    assert _cooldown(state, "bing") == 60.0
    assert _cooldown(state, "anysearch") is None


def test_httpx_402_retry_after_is_honored() -> None:
    clock = _Clock()
    state = _state(clock)

    async def factory() -> list[dict]:
        raise _http_status_error(402, headers={"retry-after": "3600"})

    async def scenario() -> None:
        with pytest.raises(httpx.HTTPStatusError):
            await run_engine(state, "youcom", factory)

    run_async(scenario())
    assert _cooldown(state, "youcom") == 3600.0


def test_httpx_402_without_retry_after_gets_default_cooldown() -> None:
    clock = _Clock()
    state = _state(clock)

    async def factory() -> list[dict]:
        raise _http_status_error(402)

    async def scenario() -> None:
        with pytest.raises(httpx.HTTPStatusError):
            await run_engine(state, "youcom", factory)

    run_async(scenario())
    assert _cooldown(state, "youcom") == DEFAULT_COOLDOWN_SECONDS


def test_queued_request_skips_after_predecessor_sets_cooldown() -> None:
    clock = _Clock()
    state = _state(clock)
    queued_calls: list[int] = []

    async def failing() -> list[dict]:
        await asyncio.sleep(0.01)
        raise BotWallError("challenge")

    async def queued() -> list[dict]:
        queued_calls.append(1)
        return []

    async def first() -> None:
        with pytest.raises(BotWallError):
            await run_engine(state, "bing", failing)

    async def scenario() -> None:
        results = await asyncio.gather(first(), run_engine(state, "bing", queued))
        assert results[1] is None

    run_async(scenario())
    assert queued_calls == []
    assert _cooldown(state, "bing") == BOT_WALL_COOLDOWN_SECONDS


def test_mixed_default_and_override_share_one_semaphore() -> None:
    state = UpstreamRoutingState()

    async def scenario() -> int:
        active = 0
        max_active = 0

        async def factory() -> list[dict]:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return []

        await asyncio.gather(
            run_engine(state, "bing", factory),
            run_engine(state, "bing", factory, concurrency_limit=5),
            run_engine(state, "bing", factory),
            run_engine(state, "bing", factory, concurrency_limit=5),
        )
        return max_active

    # First call (default limit 1) fixes the semaphore; overrides reuse it.
    assert run_async(scenario()) == 1


def test_unknown_class_records_temporary_cooldown() -> None:
    clock = _Clock()
    state = _state(clock)
    attempts = 0

    async def factory() -> list[dict]:
        nonlocal attempts
        attempts += 1
        raise ValueError("unexpected engine bug")

    async def scenario() -> None:
        with pytest.raises(ValueError):
            await run_engine(state, "bing", factory)

    run_async(scenario())
    assert attempts == 1
    assert _cooldown(state, "bing") == DEFAULT_COOLDOWN_SECONDS


def test_transient_retry_ending_in_bot_wall_uses_bot_wall_cooldown() -> None:
    clock = _Clock()
    state = _state(clock)
    attempts = 0

    async def factory() -> list[dict]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientError("network fault")
        raise BotWallError("challenge")

    async def scenario() -> None:
        with pytest.raises(BotWallError):
            await run_engine(state, "ddg_html", factory)

    run_async(scenario())
    assert attempts == 2
    assert _cooldown(state, "ddg_html") == BOT_WALL_COOLDOWN_SECONDS


def test_paid_namespace_cooldown_is_isolated_from_free() -> None:
    clock = _Clock()
    state = _state(clock)
    rows = [{"url": "https://example.com", "title": "t", "snippet": "s"}]

    async def failing() -> list[dict]:
        raise BotWallError("challenge")

    async def ok() -> list[dict]:
        return rows

    async def scenario() -> None:
        with pytest.raises(BotWallError):
            await run_engine(state, "bing", failing, provider="web_search_paid", key_fingerprint="default")
        # Same engine in the free namespace is unaffected.
        assert await run_engine(state, "bing", ok) == rows

    run_async(scenario())
    assert state.cooldown_remaining_seconds("web_search_paid", "bing", "default") == BOT_WALL_COOLDOWN_SECONDS
    assert _cooldown(state, "bing") is None


def test_paid_namespace_skip_and_success_stay_in_their_namespace() -> None:
    clock = _Clock()
    state = _state(clock)
    calls: list[int] = []

    async def failing() -> list[dict]:
        raise BotWallError("challenge")

    async def factory() -> list[dict]:
        calls.append(1)
        return []

    async def scenario() -> None:
        with pytest.raises(BotWallError):
            await run_engine(state, "bing", failing, provider="web_search_paid", key_fingerprint="default")
        # Paid namespace is cooling down: skip without calling the factory.
        assert await run_engine(
            state, "bing", factory, provider="web_search_paid", key_fingerprint="default"
        ) is None
        assert calls == []
        clock.value += 61.0
        assert await run_engine(
            state, "bing", factory, provider="web_search_paid", key_fingerprint="default"
        ) == []

    run_async(scenario())
    assert calls == [1]
    rows = state.get_status_rows()
    assert [(row["provider"], row["upstream_key_fingerprint"]) for row in rows] == [("web_search_paid", "default")]
    assert rows[0]["health_status"] == "healthy"


def test_zero_concurrency_limit_disables_semaphore_but_keeps_cooldown() -> None:
    clock = _Clock()
    state = _state(clock)
    skip_calls: list[int] = []

    async def scenario() -> None:
        active = 0
        max_active = 0

        async def factory() -> list[dict]:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return []

        await asyncio.gather(*(run_engine(state, "bing", factory, concurrency_limit=0) for _ in range(5)))
        assert max_active == 5

        async def failing() -> list[dict]:
            raise BotWallError("challenge")

        with pytest.raises(BotWallError):
            await run_engine(state, "bing", failing, concurrency_limit=0)

        async def skipped() -> list[dict]:
            skip_calls.append(1)
            return []

        assert await run_engine(state, "bing", skipped, concurrency_limit=0) is None

    run_async(scenario())
    assert skip_calls == []
    assert _cooldown(state, "bing") == BOT_WALL_COOLDOWN_SECONDS


def test_same_engine_works_across_separate_event_loops() -> None:
    state = UpstreamRoutingState()

    async def ok() -> list[dict]:
        return []

    assert run_async(run_engine(state, "bing", ok)) == []
    assert run_async(run_engine(state, "bing", ok)) == []
