"""Route and access tests for the report pages: Live now, Model failures, My key."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.v1.quota import quota_router
from llm_gateway_core.api.v1.stats import stats_router
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.middleware.auth import (
    MASTER_ONLY_REDIRECT_PATH,
    ROLE_USER,
    SESSION_COOKIE_NAME,
    api_key_auth,
    create_authenticated_session,
)
from tests.runtime_test_support import bind_app_services

USER_RECORD = ApiKeyRecord(
    id=11,
    name="user",
    api_key="virtual-key",
    budget_usd=None,
    spent_usd=0.0,
    rpm=None,
    tpm=None,
    disabled=False,
)


class _FakeApiKeysDB:
    def get_by_id(self, key_id: int) -> ApiKeyRecord | None:
        return USER_RECORD if key_id == USER_RECORD.id else None


@pytest.fixture
def client():
    with patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "test-gateway-key"):
        app = FastAPI()
        bind_app_services(app, api_keys_db=_FakeApiKeysDB())
        app.middleware("http")(api_key_auth)
        app.include_router(stats_router, prefix="/v1")
        app.include_router(quota_router, prefix="/v1")
        with TestClient(app) as test_client:
            yield test_client


def _login_master(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, create_authenticated_session())


def _login_user(client: TestClient) -> None:
    client.cookies.set(
        SESSION_COOKIE_NAME,
        create_authenticated_session(role=ROLE_USER, key_id=USER_RECORD.id),
    )


@pytest.mark.parametrize(
    ("path", "page"),
    [("/v1/ui/live", "live"), ("/v1/ui/failures", "failures"), ("/v1/ui/my-key", "my-key")],
)
def test_master_can_open_report_pages(client: TestClient, path: str, page: str):
    _login_master(client)

    response = client.get(path, headers={"Accept": "text/html"})

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert f'data-i18n-page="{page}"' in response.text


@pytest.mark.parametrize("path", ["/v1/ui/live", "/v1/ui/failures"])
def test_virtual_key_is_redirected_from_master_report_pages(client: TestClient, path: str):
    _login_user(client)

    response = client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == MASTER_ONLY_REDIRECT_PATH


def test_virtual_key_can_open_my_key_page(client: TestClient):
    _login_user(client)

    response = client.get("/v1/ui/my-key", headers={"Accept": "text/html"})

    assert response.status_code == 200
    assert 'data-i18n-page="my-key"' in response.text
