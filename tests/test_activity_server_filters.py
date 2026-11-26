"""Tests for Activity server-side filters, auth isolation, and CSV/JSON export."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import Page, expect

from llm_gateway_core.api.v1 import stats as stats_module
from llm_gateway_core.db.tokens_usage_db import (
    TokensUsageDB,
    _escape_like,
    _normalize_since_timestamp,
)
from llm_gateway_core.middleware.auth import (
    ROLE_USER,
    SESSION_COOKIE_NAME,
    ApiKeyAuthMiddleware,
    create_authenticated_session,
)
from llm_gateway_core.middleware.runtime_snapshot import RuntimeSnapshotMiddleware
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from tests.runtime_test_support import installed_runtime
from tests.test_r5_2_ui_acceptance_browser import (
    MASTER_KEY,
    _login,
    r5_2_server,
)

__all__ = ["r5_2_server"]


def _seed_usage_row(
    db_path: Path,
    *,
    timestamp: str,
    provider: str | None,
    model: str | None,
    gateway_model: str | None,
    operation: str | None,
    request_id: str | None,
    api_key_id: int | None,
    cost: float = 0.01,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO tokens_usage (
                timestamp, duration_ms, prompt_tokens, completion_tokens, total_tokens,
                reasoning_tokens, cached_tokens, cost, gateway_model, operation,
                model, provider, request_id, is_estimated, cost_saved, api_key_id
            ) VALUES (?, 120, 10, 20, 30, 0, 0, ?, ?, ?, ?, ?, ?, 0, 0.0, ?)
            """,
            (
                timestamp,
                cost,
                gateway_model,
                operation,
                model,
                provider,
                request_id,
                api_key_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class _FakeApiKeysDB:
    def __init__(self, valid_key_id: int):
        self.valid_key_id = valid_key_id

    def get_by_id(self, key_id: int):
        if key_id == self.valid_key_id:
            return SimpleNamespace(
                id=key_id,
                name=f"key-{key_id}",
                disabled=False,
                budget_usd=None,
                spent_usd=0.0,
                budget_period="none",
                last_used_at=None,
            )
        return None

    def list_all(self):
        record = self.get_by_id(self.valid_key_id)
        return [record] if record is not None else []


class TokensUsageDBServerFilterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_dir = Path(self._tmp.name)
        with patch("llm_gateway_core.db.tokens_usage_db.resolve_db_dir", return_value=db_dir):
            self.db = TokensUsageDB("test_usage_filters.db")

        _seed_usage_row(
            self.db.db_path,
            timestamp="2026-07-01T08:00:00+00:00",
            provider="OpenRouter",
            model="qwen/qwen3-coder",
            gateway_model="llmgateway/fast",
            operation="chat",
            request_id="req-alpha-1",
            api_key_id=1,
        )
        _seed_usage_row(
            self.db.db_path,
            timestamp="2026-07-02T12:30:00+00:00",
            provider="openai_100%",
            model="gpt-4o-mini",
            gateway_model="llmgateway/smart_v2",
            operation="embeddings",
            request_id="req-beta\\wild_2",
            api_key_id=2,
        )
        _seed_usage_row(
            self.db.db_path,
            timestamp="2026-07-03T18:45:00+00:00",
            provider="anthropic",
            model="claude-3-7-sonnet",
            gateway_model="llmgateway/reasoner",
            operation="messages",
            request_id="req-gamma-3",
            api_key_id=1,
        )

    def test_normalize_since_timestamp_handles_utc_variants_and_rejects_invalid(self):
        self.assertIsNone(_normalize_since_timestamp(None))
        self.assertIsNone(_normalize_since_timestamp("   "))
        self.assertEqual(
            _normalize_since_timestamp("2026-07-02T12:30Z"),
            "2026-07-02T12:30:00+00:00",
        )
        self.assertEqual(
            _normalize_since_timestamp("2026-07-02T12:30"),
            "2026-07-02T12:30:00+00:00",
        )
        self.assertEqual(
            _normalize_since_timestamp("2026-07-02T14:30:00+02:00"),
            "2026-07-02T12:30:00+00:00",
        )
        with self.assertRaisesRegex(ValueError, "since must be a valid ISO 8601 datetime string"):
            _normalize_since_timestamp("not-a-date")

    def test_escape_like_escapes_wildcards_and_backslash(self):
        self.assertEqual(_escape_like(r"a%b_c\d"), r"a\%b\_c\\d")

    def test_db_filters_provider_model_gateway_model_operation_request_id_and_since(self):
        async def _run():
            by_provider = await self.db.get_latest_usage_records(provider="openrouter")
            self.assertEqual([r["request_id"] for r in by_provider], ["req-alpha-1"])
            self.assertEqual(await self.db.get_total_records_count(provider="openrouter"), 1)

            # model filter matches both resolved model and gateway_model
            by_resolved = await self.db.get_latest_usage_records(model="SONNET")
            self.assertEqual([r["request_id"] for r in by_resolved], ["req-gamma-3"])
            by_gateway = await self.db.get_latest_usage_records(model="FAST")
            self.assertEqual([r["request_id"] for r in by_gateway], ["req-alpha-1"])

            by_op = await self.db.get_latest_usage_records(operation="embed")
            self.assertEqual([r["request_id"] for r in by_op], ["req-beta\\wild_2"])

            by_since = await self.db.get_latest_usage_records(since="2026-07-02T12:30Z")
            self.assertEqual(
                [r["request_id"] for r in by_since],
                ["req-gamma-3", "req-beta\\wild_2"],
            )
            self.assertEqual(await self.db.get_total_records_count(since="2026-07-02T12:30Z"), 2)

            # Combined filters
            combined = await self.db.get_latest_usage_records(
                api_key_id=1, since="2026-07-02T00:00"
            )
            self.assertEqual([r["request_id"] for r in combined], ["req-gamma-3"])

        asyncio.run(_run())

    def test_db_filters_escape_like_special_characters(self):
        async def _run():
            # Literal '%' should match only 'openai_100%', not every row
            pct_rows = await self.db.get_latest_usage_records(provider="100%")
            self.assertEqual([r["request_id"] for r in pct_rows], ["req-beta\\wild_2"])

            # Literal '_' should not match arbitrary character
            no_match = await self.db.get_latest_usage_records(provider="open_outer")
            self.assertEqual(no_match, [])

            # Literal '\' should match only req-beta\wild_2
            slash_rows = await self.db.get_latest_usage_records(request_id="beta\\wild_")
            self.assertEqual([r["request_id"] for r in slash_rows], ["req-beta\\wild_2"])

        asyncio.run(_run())

    def test_db_invalid_since_raises_value_error(self):
        async def _run():
            with self.assertRaises(ValueError):
                await self.db.get_latest_usage_records(since="bad-ts")
            with self.assertRaises(ValueError):
                await self.db.get_total_records_count(since="bad-ts")

        asyncio.run(_run())

    def test_active_requests_registry_filters_consistently(self):
        registry = ActiveRequestsRegistry()
        registry.start(
            request_id="req-live-1",
            path="/v1/chat/completions",
            api_key_id=5,
            gateway_model="llmgateway/live-gw",
            operation="chat",
        )
        registry.update("req-live-1", provider="OpenRouter", model="qwen/qwen3")
        registry.start(
            request_id="req-live-2",
            path="/v1/embeddings",
            api_key_id=9,
            gateway_model="llmgateway/emb",
            operation="embeddings",
        )
        registry.update("req-live-2", provider="openai", model="text-embedding-3")

        self.assertEqual(
            [r["request_id"] for r in registry.list_records(provider="router")],
            ["req-live-1"],
        )
        self.assertEqual(
            [r["request_id"] for r in registry.list_records(model="live-gw")],
            ["req-live-1"],
        )
        self.assertEqual(
            [r["request_id"] for r in registry.list_records(operation="embed")],
            ["req-live-2"],
        )
        self.assertEqual(
            [r["request_id"] for r in registry.list_records(request_id="LIVE-2")],
            ["req-live-2"],
        )
        future_since = datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat()
        self.assertEqual(registry.list_records(since=future_since), [])
        with self.assertRaises(ValueError):
            registry.list_records(since="invalid-iso")


class UsageRecordsApiFilterAndAuthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_dir = Path(self._tmp.name)
        with patch("llm_gateway_core.db.tokens_usage_db.resolve_db_dir", return_value=db_dir):
            self.db = TokensUsageDB("api_filter_test.db")

        _seed_usage_row(
            self.db.db_path,
            timestamp="2026-07-01T10:00:00+00:00",
            provider="openai",
            model="gpt-4o-mini",
            gateway_model="llmgateway/light",
            operation="chat",
            request_id="req-k7-1",
            api_key_id=7,
        )
        _seed_usage_row(
            self.db.db_path,
            timestamp="2026-07-02T10:00:00+00:00",
            provider="anthropic",
            model="claude-3-7-sonnet",
            gateway_model="llmgateway/pro",
            operation="messages",
            request_id="req-k11-1",
            api_key_id=11,
        )

    @contextmanager
    def _client(self, *, active_requests: ActiveRequestsRegistry | None = None, valid_key_id: int = 11):
        overrides = {
            "tokens_usage_db": self.db,
            "api_keys_db": _FakeApiKeysDB(valid_key_id=valid_key_id),
        }
        if active_requests is not None:
            overrides["active_requests_registry"] = active_requests

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            async with installed_runtime(app, **overrides):
                yield

        app = FastAPI(lifespan=lifespan)
        app.include_router(stats_module.stats_router, prefix="/v1")
        app.add_middleware(RuntimeSnapshotMiddleware)
        app.add_middleware(ApiKeyAuthMiddleware)

        with patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "test-gateway-key"):
            with TestClient(app) as client:
                yield client

    def test_master_can_filter_usage_records_and_invalid_since_returns_400(self):
        active = ActiveRequestsRegistry()
        active.start(
            request_id="req-k7-live",
            path="/v1/chat/completions",
            api_key_id=7,
            gateway_model="llmgateway/light",
            operation="chat",
        )
        active.update("req-k7-live", provider="openai", model="gpt-4o-mini")

        with self._client(active_requests=active) as client:
            headers = {"Authorization": "Bearer test-gateway-key"}
            resp = client.get(
                "/v1/api/usage-records?api_key_id=7&provider=open&model=light&operation=chat&since=2026-07-01T09:00Z",
                headers=headers,
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertEqual(payload["total_records"], 2)
            self.assertEqual([r["request_id"] for r in payload["records"]], ["req-k7-live", "req-k7-1"])

            bad = client.get("/v1/api/usage-records?since=not-valid", headers=headers)
            self.assertEqual(bad.status_code, 400)

    def test_virtual_key_cannot_override_api_key_id_filter(self):
        active = ActiveRequestsRegistry()
        active.start(
            request_id="req-k7-live",
            path="/v1/chat/completions",
            api_key_id=7,
            gateway_model="llmgateway/light",
            operation="chat",
        )
        active.update("req-k7-live", provider="openai", model="gpt-4o-mini")

        with self._client(active_requests=active, valid_key_id=11) as client:
            client.cookies.set(
                SESSION_COOKIE_NAME,
                create_authenticated_session(role=ROLE_USER, key_id=11),
            )
            resp = client.get("/v1/api/usage-records?api_key_id=7")
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertEqual(payload["total_records"], 1)
            self.assertEqual([r["request_id"] for r in payload["records"]], ["req-k11-1"])


@pytest.mark.browser
def test_activity_browser_server_filters_and_url_sync(page: Page, r5_2_server: str) -> None:
    _login(page.context, r5_2_server, MASTER_KEY)
    seen_urls: list[str] = []

    def _handle_usage(route):
        seen_urls.append(route.request.url)
        route.fulfill(
            json={
                "records": [
                    {
                        "request_id": "req-filtered-1",
                        "timestamp": "2026-07-19T10:00:00Z",
                        "api_key_id": 7,
                        "operation": "chat",
                        "gateway_model": "llmgateway/fast",
                        "model": "gpt-4o-mini",
                        "provider": "openai",
                        "status": "completed",
                        "duration_ms": 250,
                        "cost": 0.005,
                    }
                ],
                "total_records": 1,
            }
        )

    page.route("**/v1/api/usage-records*", _handle_usage)
    page.goto(f"{r5_2_server}/v1/ui/activity")
    expect(page.locator("#actTableBody tr")).to_contain_text("req-filtered-1")

    # Verify limit 200 option was removed and server_error option remains
    assert page.locator('#filterLimit option[value="200"]').count() == 0
    assert page.locator('#filterStatus option[value="server_error"]').count() == 1

    page.locator("#filterProvider").fill("openai")
    page.locator("#filterModel").fill("gpt-4o")
    page.locator("#filterOperation").fill("chat")
    page.locator("#filterKeyId").fill("7")
    page.locator("#filterRequestId").fill("req-filtered")
    page.locator("#filterSince").fill("2026-07-01T10:30")

    with page.expect_request("**/v1/api/usage-records*"):
        page.locator("#applyFiltersBtn").click()

    last_qs = urllib.parse.parse_qs(urllib.parse.urlparse(seen_urls[-1]).query)
    assert last_qs["provider"] == ["openai"]
    assert last_qs["model"] == ["gpt-4o"]
    assert last_qs["operation"] == ["chat"]
    assert last_qs["api_key_id"] == ["7"]
    assert last_qs["request_id"] == ["req-filtered"]
    assert last_qs["since"] == ["2026-07-01T10:30:00Z"]

    page_qs = urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)
    assert page_qs["model"] == ["gpt-4o"]
    assert page_qs["operation"] == ["chat"]

    page.reload()
    expect(page.locator("#filterModel")).to_have_value("gpt-4o")
    expect(page.locator("#filterOperation")).to_have_value("chat")

    with page.expect_request("**/v1/api/usage-records*"):
        page.locator("#resetFiltersBtn").click()
    expect(page.locator("#filterModel")).to_have_value("")
    expect(page.locator("#filterOperation")).to_have_value("")


@pytest.mark.browser
def test_activity_browser_csv_and_json_export_with_pagination_and_formula_protection(
    page: Page, r5_2_server: str
) -> None:
    _login(page.context, r5_2_server, MASTER_KEY)

    all_records = [
        {
            "request_id": f"req-exp-{i}",
            "timestamp": "2026-07-19T10:00:00Z",
            "api_key_id": 7,
            "operation": "chat",
            "gateway_model": "llmgateway/fast",
            "model": (
                "=CMD|' /C calc'!A0"
                if i == 0
                else ("+danger" if i == 1 else (" =1+1" if i == 2 else "gpt-4o-mini"))
            ),
            "provider": "openai",
            "status": "completed",
            "duration_ms": 100 + i,
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "cost": 0.001,
            "x_title": "@inject" if i == 0 else "-neg",
        }
        for i in range(105)
    ]

    def _handle_usage(route):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query)
        limit = int(qs.get("limit", ["50"])[0])
        offset = int(qs.get("offset", ["0"])[0])
        route.fulfill(
            json={
                "records": all_records[offset : offset + limit],
                "total_records": len(all_records),
            }
        )

    page.route("**/v1/api/usage-records*", _handle_usage)
    page.goto(f"{r5_2_server}/v1/ui/activity")
    expect(page.locator("#actTableBody tr")).to_have_count(50)

    with page.expect_download() as csv_download_info:
        page.locator("#exportCsvBtn").click()
    csv_path = csv_download_info.value.path()
    assert csv_path is not None
    raw_bytes = Path(csv_path).read_bytes()
    assert raw_bytes.startswith(b"\xef\xbb\xbf")
    csv_text = raw_bytes.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 105
    assert "api_key_id" in rows[0]
    assert rows[0]["model"].startswith("'=")
    assert rows[1]["model"].startswith("'+")
    assert rows[2]["model"] == "' =1+1"
    assert rows[0]["x_title"].startswith("'@")
    assert rows[1]["x_title"].startswith("'-")

    with page.expect_download() as json_download_info:
        page.locator("#exportJsonBtn").click()
    json_path = json_download_info.value.path()
    assert json_path is not None
    exported_json = json.loads(Path(json_path).read_text(encoding="utf-8"))
    assert len(exported_json) == 105
    assert exported_json[0]["api_key_id"] == 7


@pytest.mark.browser
def test_activity_browser_non_master_omits_api_key_id_from_query_and_exports(
    page: Page, r5_2_server: str
) -> None:
    create_resp = page.context.request.post(
        f"{r5_2_server}/v1/admin/api-keys",
        headers={"Authorization": f"Bearer {MASTER_KEY}"},
        data={"name": "activity-filter-browser-user"},
    )
    assert create_resp.status == 201, create_resp.text()
    virtual_key = str(create_resp.json()["api_key"])
    _login(page.context, r5_2_server, virtual_key)
    seen_urls: list[str] = []

    def _handle_usage(route):
        seen_urls.append(route.request.url)
        route.fulfill(
            json={
                "records": [
                    {
                        "request_id": "req-user-exp-1",
                        "timestamp": "2026-07-19T10:00:00Z",
                        "api_key_id": 3,
                        "operation": "chat",
                        "gateway_model": "llmgateway/fast",
                        "model": "gpt-4o-mini",
                        "provider": "openai",
                        "status": "completed",
                        "duration_ms": 150,
                        "prompt_tokens": 10,
                        "completion_tokens": 20,
                        "total_tokens": 30,
                        "cost": 0.002,
                        "x_title": "UserApp",
                    }
                ],
                "total_records": 1,
            }
        )

    page.route("**/v1/api/usage-records*", _handle_usage)
    page.goto(f"{r5_2_server}/v1/ui/activity?key_id=999")
    page.wait_for_function("window.gatewayAuth && window.gatewayAuth.state === 'user'")
    expect(page.locator("#actTableBody tr")).to_contain_text("req-user-exp-1")

    for url in seen_urls:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert "api_key_id" not in qs

    with page.expect_download() as csv_download_info:
        page.locator("#exportCsvBtn").click()
    csv_path = csv_download_info.value.path()
    assert csv_path is not None
    csv_text = Path(csv_path).read_bytes().decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == 1
    assert "api_key_id" not in rows[0]

    with page.expect_download() as json_download_info:
        page.locator("#exportJsonBtn").click()
    json_path = json_download_info.value.path()
    assert json_path is not None
    exported_json = json.loads(Path(json_path).read_text(encoding="utf-8"))
    assert len(exported_json) == 1
    assert "api_key_id" not in exported_json[0]
