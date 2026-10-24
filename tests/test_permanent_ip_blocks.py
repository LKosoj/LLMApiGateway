import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.admin_rejections import admin_rejections_router
from llm_gateway_core.db.rejections_db import RejectionsDB
from llm_gateway_core.middleware.auth import ApiKeyAuthMiddleware
from llm_gateway_core.services.ip_blocklist import PERMANENT_BLOCK, IpBlockGuard
from tests.runtime_test_support import bind_app_services


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    return RejectionsDB()


def record_block(db, ip, when, category="ip_blocked"):
    with closing(sqlite3.connect(db.db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO rejection_events (timestamp, path, method, client_ip, status_code, category) "
            "VALUES (?, '/v1/models', 'GET', ?, 429, ?)",
            (when.isoformat(), ip, category),
        )


def block_ip(db, ip):
    now = datetime.now(timezone.utc)
    record_block(db, ip, now - timedelta(hours=2))
    record_block(db, ip, now - timedelta(hours=1))
    guard = IpBlockGuard(rejections_db=db)
    guard.scan_permanent_blocks()
    return guard


def test_daily_scan_only_counts_two_actual_blocks_within_last_24_hours(db):
    now = datetime.now(timezone.utc)
    record_block(db, "198.51.100.1", now - timedelta(hours=24))
    record_block(db, "198.51.100.1", now)
    record_block(db, "198.51.100.2", now - timedelta(hours=24, seconds=1))
    record_block(db, "198.51.100.2", now)
    record_block(db, "198.51.100.3", now, category="auth_invalid")
    record_block(db, "198.51.100.3", now)
    record_block(db, "198.51.100.4", now + timedelta(seconds=1))
    record_block(db, "198.51.100.4", now)
    assert [item["client_ip"] for item in db.promote_repeat_ip_blocks(now=now)] == ["198.51.100.1"]
    first = db.list_permanent_ip_blocks()
    assert db.promote_repeat_ip_blocks(now=now) == first


def test_permanent_blocks_survive_restart_time_and_temporary_reset(db):
    ip = "198.51.100.5"
    guard = block_ip(db, ip)
    guard.reset(ip)
    guard.register_success(ip)
    assert guard.check_blocked(ip) == PERMANENT_BLOCK
    restarted = IpBlockGuard(rejections_db=RejectionsDB(), time_func=lambda: 10**12)
    assert restarted.check_blocked(ip) == PERMANENT_BLOCK
    assert restarted.list_permanent_blocks()[0]["client_ip"] == ip
    db.cleanup_old_records(retention_days=0)
    assert IpBlockGuard(rejections_db=RejectionsDB()).check_blocked(ip) == PERMANENT_BLOCK


def test_unblock_ignores_old_events_but_new_blocks_can_ban_again(db):
    ip = "198.51.100.6"
    guard = block_ip(db, ip)
    guard.register_failure(ip)
    assert guard.unblock_permanent_ip(ip)
    assert guard.check_blocked(ip) is None
    assert not guard.unblock_permanent_ip(ip)
    restarted = IpBlockGuard(rejections_db=RejectionsDB())
    restarted.scan_permanent_blocks()
    assert restarted.check_blocked(ip) is None
    # Delayed writes of pre-release events must also remain ignored.
    record_block(db, ip, datetime.now(timezone.utc) - timedelta(minutes=30))
    restarted.scan_permanent_blocks()
    assert restarted.check_blocked(ip) is None
    record_block(db, ip, datetime.now(timezone.utc))
    restarted.scan_permanent_blocks()
    assert restarted.check_blocked(ip) is None
    record_block(db, ip, datetime.now(timezone.utc))
    restarted.scan_permanent_blocks()
    assert restarted.check_blocked(ip) == PERMANENT_BLOCK


def build_app(db, guard):
    app = FastAPI()
    bind_app_services(app, rejections_db=db, ip_block_guard=guard)
    app.add_middleware(ApiKeyAuthMiddleware)
    app.include_router(admin_rejections_router, prefix="/v1")
    app.include_router(auth_router)

    @app.get("/v1/models")
    async def models():
        return {"data": []}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


def test_permanent_block_rejects_valid_key_and_login_without_retry_after(db):
    ip = "198.51.100.7"
    guard = block_ip(db, ip)
    with patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-test"):
        with TestClient(build_app(db, guard), client=(ip, 50000)) as client:
            for method, path, kwargs in [
                ("GET", "/v1/models", {"headers": {"Authorization": "Bearer master-test"}}),
                ("POST", "/auth/login", {"json": {"api_key": "master-test"}}),
            ]:
                response = client.request(method, path, **kwargs)
                assert response.status_code == 403
                assert "permanently blocked" in response.json()["detail"]
                assert "Retry-After" not in response.headers
            assert client.get("/health").status_code == 200


def test_admin_lists_and_releases_block_with_immediate_access_restored(db):
    ip = "198.51.100.8"
    guard = block_ip(db, ip)
    app = build_app(db, guard)
    headers = {"Authorization": "Bearer master-test"}
    with patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-test"):
        with TestClient(app) as client:
            assert client.get("/v1/admin/ip-blocks", headers=headers).json()["items"][0]["client_ip"] == ip
            assert client.delete(f"/v1/admin/ip-blocks/{ip}", headers=headers).status_code == 200
            assert client.get("/v1/admin/ip-blocks", headers=headers).json() == {"items": []}
            assert client.delete(f"/v1/admin/ip-blocks/{ip}", headers=headers).status_code == 404
            assert client.delete("/v1/admin/ip-blocks/invalid", headers=headers).status_code == 400
        with TestClient(app, client=(ip, 50000)) as client:
            assert client.get("/v1/models", headers=headers).status_code == 200


def test_block_management_requires_master_role(db):
    app = build_app(db, block_ip(db, "198.51.100.9"))
    with patch("llm_gateway_core.middleware.auth._authenticate_request", new_callable=AsyncMock) as auth:
        async def user_auth(request, services):
            request.state.api_key_role = "user"
            return True, "bearer-virtual"

        auth.side_effect = user_auth
        with TestClient(app) as client:
            assert client.get("/v1/admin/ip-blocks").status_code == 403
            assert client.delete("/v1/admin/ip-blocks/198.51.100.9").status_code == 403


def test_daily_task_runs_scan_then_sleeps_24_hours():
    import main

    guard = Mock()
    with patch("main.scheduler_sleep", new_callable=AsyncMock) as sleep:
        sleep.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(main.run_usage_stats_cleanup_loop(Mock(), Mock(), Mock(), ip_block_guard=guard))
        guard.scan_permanent_blocks.assert_called_once_with()
        sleep.assert_awaited_once_with(24 * 60 * 60)
