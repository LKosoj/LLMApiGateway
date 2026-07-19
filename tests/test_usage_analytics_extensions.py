"""Focused tests for the analytics extensions.

Covers the write path (client metadata on usage rows, durable hourly/lifetime
aggregates), the dashboard read path (throughput, pin-honored counters,
per-provider latency, hourly-backed series, lifetime totals) and the
request-extraction helpers feeding the chat commit site.
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.services.accounting import (
    ACCOUNTING_EVENT_VERSION,
    AccountingError,
    AccountingEvent,
    AccountingEventKind,
    AccountingUsage,
    CostSource,
    SourceStatus,
)
from llm_gateway_core.utils.usage_tracking import (
    MAX_CLIENT_USER_AGENT_CHARS,
    extract_request_client_ip,
    extract_request_fallback_depth,
    extract_request_user_agent,
)
from tests._async_compat import run_async


@pytest.fixture
def source_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TokensUsageDB:
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    return TokensUsageDB(db_filename="analytics-extensions.db")


def _usage(
    *,
    prompt_tokens: int = 30,
    completion_tokens: int = 20,
    cost: float = 0.25,
    cost_saved: float = 0.05,
    duration_ms: int | None = 125,
    ttft_ms: int | None = None,
) -> AccountingUsage:
    return AccountingUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost=cost,
        cost_saved=cost_saved,
        duration_ms=duration_ms,
        ttft_ms=ttft_ms,
    )


def _charge(
    event_id: str,
    *,
    usage: AccountingUsage | None = None,
    occurred_at: datetime | None = None,
    provider: str = "provider-a",
    client_ip: str | None = None,
    client_user_agent: str | None = None,
    fallback_depth: int | None = None,
) -> AccountingEvent:
    return AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION,
        event_id=event_id,
        kind=AccountingEventKind.CHARGE,
        api_key_id=7,
        method="POST",
        route_template="/v1/chat/completions",
        operation="chat",
        gateway_model="gateway/chat",
        provider=provider,
        model="model-a",
        usage=usage or _usage(),
        cost_source=CostSource.UPSTREAM,
        occurred_at=occurred_at or datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
        client_ip=client_ip,
        client_user_agent=client_user_agent,
        fallback_depth=fallback_depth,
    )


def _dashboard(db: TokensUsageDB, **kwargs) -> dict:
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = datetime(2100, 1, 1, tzinfo=timezone.utc)
    return run_async(db.get_dashboard_usage("day", start, end, **kwargs))


def _usage_row(db: TokensUsageDB, event_id: str) -> sqlite3.Row:
    with sqlite3.connect(db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM tokens_usage WHERE accounting_event_id = ?",
            (event_id,),
        ).fetchone()


def _single_row(db: TokensUsageDB, sql: str) -> sqlite3.Row | None:
    with sqlite3.connect(db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql).fetchone()


# ── client metadata on usage rows ────────────────────────────────────────────


def test_accept_event_persists_client_metadata(source_db: TokensUsageDB) -> None:
    event = _charge(
        "evt-meta-1",
        client_ip="203.0.113.7",
        client_user_agent="python-requests/2.32.3",
        fallback_depth=2,
    )
    acceptance = source_db.accept_accounting_event(event)
    assert acceptance.status is SourceStatus.ACCEPTED

    row = _usage_row(source_db, "evt-meta-1")
    assert row["client_ip"] == "203.0.113.7"
    assert row["client_user_agent"] == "python-requests/2.32.3"
    assert row["fallback_depth"] == 2


def test_client_metadata_defaults_to_null(source_db: TokensUsageDB) -> None:
    source_db.accept_accounting_event(_charge("evt-meta-2"))
    row = _usage_row(source_db, "evt-meta-2")
    assert row["client_ip"] is None
    assert row["client_user_agent"] is None
    assert row["fallback_depth"] is None


def test_client_metadata_is_outside_billing_fingerprint(
    source_db: TokensUsageDB,
) -> None:
    base = _charge("evt-meta-3")
    enriched = replace(
        base,
        client_ip="198.51.100.9",
        client_user_agent="curl/8.6.0",
        fallback_depth=1,
    )
    assert base.billing_fingerprint == enriched.billing_fingerprint

    assert source_db.accept_accounting_event(enriched).status is SourceStatus.ACCEPTED
    # A re-delivery without the metadata still deduplicates as the same event.
    assert source_db.accept_accounting_event(base).status is SourceStatus.DUPLICATE
    with sqlite3.connect(source_db.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tokens_usage").fetchone()[0] == 1


def test_negative_fallback_depth_is_rejected() -> None:
    with pytest.raises(AccountingError):
        _charge("evt-meta-4", fallback_depth=-1)


# ── durable hourly/lifetime aggregates ───────────────────────────────────────


def test_accepted_events_fold_into_hourly_and_lifetime(
    source_db: TokensUsageDB,
) -> None:
    first = _charge(
        "evt-agg-1",
        usage=_usage(prompt_tokens=10, completion_tokens=5, cost=0.1, cost_saved=0.2, duration_ms=100),
        occurred_at=datetime(2026, 7, 13, 10, 15, tzinfo=timezone.utc),
    )
    same_hour = _charge(
        "evt-agg-2",
        usage=_usage(prompt_tokens=1, completion_tokens=2, cost=0.3, cost_saved=0.0, duration_ms=None),
        occurred_at=datetime(2026, 7, 13, 10, 45, tzinfo=timezone.utc),
    )
    other_hour = _charge(
        "evt-agg-3",
        usage=_usage(prompt_tokens=7, completion_tokens=3, cost=0.05, cost_saved=0.5, duration_ms=40),
        occurred_at=datetime(2026, 7, 13, 11, 5, tzinfo=timezone.utc),
    )
    for event in (first, same_hour, other_hour):
        assert source_db.accept_accounting_event(event).status is SourceStatus.ACCEPTED

    with sqlite3.connect(source_db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        hourly = {
            row["hour"]: row
            for row in conn.execute("SELECT * FROM usage_hourly ORDER BY hour")
        }
    assert set(hourly) == {"2026-07-13T10:00:00", "2026-07-13T11:00:00"}
    ten = hourly["2026-07-13T10:00:00"]
    assert ten["requests"] == 2
    assert ten["prompt_tokens"] == 11
    assert ten["completion_tokens"] == 7
    assert ten["total_tokens"] == 18
    assert ten["cost"] == pytest.approx(0.4)
    assert ten["cost_saved"] == pytest.approx(0.2)
    assert ten["duration_ms_sum"] == 100
    assert ten["duration_count"] == 1

    lifetime = _single_row(source_db, "SELECT * FROM usage_lifetime")
    assert lifetime["requests"] == 3
    assert lifetime["total_tokens"] == 15 + 3 + 10
    assert lifetime["cost"] == pytest.approx(0.45)
    assert lifetime["cost_saved"] == pytest.approx(0.7)
    assert lifetime["first_event_at"] == "2026-07-13T10:15:00+00:00"


def test_duplicate_event_does_not_double_aggregates(
    source_db: TokensUsageDB,
) -> None:
    event = _charge("evt-agg-dup")
    assert source_db.accept_accounting_event(event).status is SourceStatus.ACCEPTED
    assert source_db.accept_accounting_event(event).status is SourceStatus.DUPLICATE

    lifetime = _single_row(source_db, "SELECT * FROM usage_lifetime")
    assert lifetime["requests"] == 1
    hourly = _single_row(source_db, "SELECT * FROM usage_hourly")
    assert hourly["requests"] == 1


def test_cleanup_old_records_preserves_aggregates(
    source_db: TokensUsageDB,
) -> None:
    stale = _charge(
        "evt-agg-old",
        occurred_at=datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc),
    )
    assert source_db.accept_accounting_event(stale).status is SourceStatus.ACCEPTED
    # Retention is pending-safe: only projected rows are prunable.
    source_db.mark_accounting_event_projected(
        stale.event_id,
        stale.billing_fingerprint,
        datetime(2020, 1, 1, 0, 5, tzinfo=timezone.utc),
    )

    source_db.cleanup_old_records(retention_days=180)

    with sqlite3.connect(source_db.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tokens_usage").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM usage_hourly").fetchone()[0] == 1
        assert conn.execute("SELECT requests FROM usage_lifetime").fetchone()[0] == 1


# ── dashboard read path ──────────────────────────────────────────────────────


def test_dashboard_summary_reports_throughput_and_pin_honored(
    source_db: TokensUsageDB,
) -> None:
    pinned = _charge(
        "evt-read-1",
        usage=_usage(completion_tokens=20, duration_ms=1000),
        fallback_depth=0,
    )
    degraded = _charge(
        "evt-read-2",
        usage=_usage(completion_tokens=30, duration_ms=500),
        fallback_depth=2,
    )
    untracked = _charge("evt-read-3", usage=_usage(duration_ms=None))
    for event in (pinned, degraded, untracked):
        assert source_db.accept_accounting_event(event).status is SourceStatus.ACCEPTED

    summary = _dashboard(source_db)["summary"]
    assert summary["requests"] == 3
    assert summary["tokens_per_second"] == pytest.approx((20 + 30) * 1000 / 1500)
    assert summary["first_attempt_requests"] == 1
    assert summary["fallback_tracked_requests"] == 2


def test_dashboard_providers_breakdown_reports_latency_and_throughput(
    source_db: TokensUsageDB,
) -> None:
    for index, (duration, ttft) in enumerate([(100, 50), (200, 60), (300, 70)]):
        event = _charge(
            f"evt-prov-a-{index}",
            usage=_usage(completion_tokens=20, duration_ms=duration, ttft_ms=ttft),
        )
        assert source_db.accept_accounting_event(event).status is SourceStatus.ACCEPTED
    other = _charge(
        "evt-prov-b",
        provider="provider-b",
        usage=_usage(duration_ms=400, ttft_ms=None),
    )
    assert source_db.accept_accounting_event(other).status is SourceStatus.ACCEPTED

    providers = {
        row["label"]: row for row in _dashboard(source_db)["breakdowns"]["providers"]
    }
    assert providers["provider-a"]["ttft_avg_ms"] == 60
    assert providers["provider-a"]["duration_p95_ms"] == 300
    assert providers["provider-a"]["tokens_per_second"] == pytest.approx(100.0)
    assert providers["provider-b"]["ttft_avg_ms"] is None
    assert providers["provider-b"]["duration_p95_ms"] == 400


def test_dashboard_unfiltered_series_survives_raw_pruning(
    source_db: TokensUsageDB,
) -> None:
    early = _charge(
        "evt-series-1", occurred_at=datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)
    )
    late = _charge(
        "evt-series-2", occurred_at=datetime(2026, 7, 12, 9, 30, tzinfo=timezone.utc)
    )
    for event in (early, late):
        assert source_db.accept_accounting_event(event).status is SourceStatus.ACCEPTED
    # Simulate the retention loop having pruned every raw row.
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute("DELETE FROM tokens_usage")

    series = _dashboard(source_db)["series"]
    assert [(row["time_period"], row["requests"]) for row in series] == [
        ("2026-07-10", 1),
        ("2026-07-12", 1),
    ]
    # Filtered views still read raw rows only.
    assert _dashboard(source_db, provider="provider-a")["series"] == []


def test_dashboard_recent_records_include_client_metadata(
    source_db: TokensUsageDB,
) -> None:
    event = _charge(
        "evt-recent-1",
        client_ip="203.0.113.7",
        client_user_agent="curl/8.6.0",
        fallback_depth=1,
    )
    assert source_db.accept_accounting_event(event).status is SourceStatus.ACCEPTED

    record = _dashboard(source_db)["recent_records"][0]
    assert record["client_ip"] == "203.0.113.7"
    assert record["client_user_agent"] == "curl/8.6.0"
    assert record["fallback_depth"] == 1


def test_get_lifetime_totals_returns_counters(source_db: TokensUsageDB) -> None:
    assert run_async(source_db.get_lifetime_totals()) is None

    event = _charge(
        "evt-life-1",
        usage=_usage(prompt_tokens=10, completion_tokens=5, cost=0.1, cost_saved=0.2),
        occurred_at=datetime(2026, 7, 13, 10, 15, tzinfo=timezone.utc),
    )
    assert source_db.accept_accounting_event(event).status is SourceStatus.ACCEPTED

    totals = run_async(source_db.get_lifetime_totals())
    assert totals == {
        "requests": 1,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cost": pytest.approx(0.1),
        "cost_saved": pytest.approx(0.2),
        "first_event_at": "2026-07-13T10:15:00+00:00",
    }


def test_init_backfills_aggregates_from_existing_rows(
    source_db: TokensUsageDB,
) -> None:
    first = _charge(
        "evt-backfill-1", occurred_at=datetime(2026, 7, 13, 10, 15, tzinfo=timezone.utc)
    )
    second = _charge(
        "evt-backfill-2", occurred_at=datetime(2026, 7, 13, 11, 5, tzinfo=timezone.utc)
    )
    for event in (first, second):
        assert source_db.accept_accounting_event(event).status is SourceStatus.ACCEPTED
    # Simulate a pre-aggregates database: raw rows exist, aggregates do not.
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute("DELETE FROM usage_hourly")
        conn.execute("DELETE FROM usage_lifetime")

    reopened = TokensUsageDB(db_filename="analytics-extensions.db")

    with sqlite3.connect(reopened.db_path) as conn:
        hours = [row[0] for row in conn.execute("SELECT hour FROM usage_hourly ORDER BY hour")]
    assert hours == ["2026-07-13T10:00:00", "2026-07-13T11:00:00"]
    totals = run_async(reopened.get_lifetime_totals())
    assert totals is not None
    assert totals["requests"] == 2
    assert totals["first_event_at"] == "2026-07-13T10:15:00+00:00"


# ── request extraction helpers ───────────────────────────────────────────────


def _fake_request(
    *,
    host: str | None = "127.0.0.1",
    headers: dict[str, str] | None = None,
    state: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host=host) if host is not None else None,
        headers=headers or {},
        state=state or SimpleNamespace(),
    )


def test_extract_request_client_ip_returns_peer_host() -> None:
    assert extract_request_client_ip(_fake_request(host="203.0.113.7")) == "203.0.113.7"
    assert extract_request_client_ip(_fake_request(host=None)) is None
    assert extract_request_client_ip(None) is None


def test_extract_request_user_agent_is_trimmed_and_bounded() -> None:
    assert (
        extract_request_user_agent(_fake_request(headers={"user-agent": "  curl/8.6.0  "}))
        == "curl/8.6.0"
    )
    oversized = "x" * (MAX_CLIENT_USER_AGENT_CHARS + 50)
    extracted = extract_request_user_agent(_fake_request(headers={"user-agent": oversized}))
    assert extracted == "x" * MAX_CLIENT_USER_AGENT_CHARS
    assert extract_request_user_agent(_fake_request(headers={})) is None
    assert extract_request_user_agent(None) is None


def test_extract_request_fallback_depth_counts_failed_attempts() -> None:
    trail = [
        {"provider": "a", "model": "m", "error_class": "rate_limit"},
        {"provider": "b", "model": "m", "error_class": "server_error"},
        {"provider": "c", "model": "m", "error_class": None},
    ]
    request = _fake_request(state=SimpleNamespace(llmgateway_fallback_attempts=trail))
    assert extract_request_fallback_depth(request) == 2

    clean = _fake_request(
        state=SimpleNamespace(
            llmgateway_fallback_attempts=[{"provider": "a", "model": "m", "error_class": None}]
        )
    )
    assert extract_request_fallback_depth(clean) == 0
    assert extract_request_fallback_depth(_fake_request()) is None
    assert extract_request_fallback_depth(None) is None
