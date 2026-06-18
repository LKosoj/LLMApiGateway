from unittest.mock import AsyncMock, Mock

import httpx
from cryptography.fernet import Fernet

from tests._async_compat import run_async
from llm_gateway_core.config.loader import ProviderAuthConfig
from llm_gateway_core.db.oauth_tokens_db import OAuthTokensDB
from llm_gateway_core.services.managed_oauth import ManagedOAuthService, OAuthRefreshFailed


def _auth_config() -> ProviderAuthConfig:
    return ProviderAuthConfig(
        type="codex_oauth",
        credential_id="codex-main",
        oauth_client={
            "client_id": "client-id",
            "token_endpoint": "https://issuer.example/oauth/token",
            "device_authorization_endpoint": "https://issuer.example/oauth/device/code",
            "scopes": ["openid"],
        },
    )


def test_managed_oauth_refreshes_expired_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db = OAuthTokensDB(encryption_key=Fernet.generate_key().decode("ascii"))
    db.upsert_tokens(
        credential_id="codex-main",
        provider_name="codex",
        auth_type="codex_oauth",
        access_token="old-access",
        refresh_token="refresh-secret",
        expires_at=100,
    )
    http_client = Mock()
    http_client.post = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
            request=httpx.Request("POST", "https://issuer.example/oauth/token"),
        )
    )
    service = ManagedOAuthService(tokens_db=db, http_client=http_client, time_func=lambda: 200)

    token = run_async(
        service.get_access_token(
            provider_name="codex",
            auth_config=_auth_config(),
        )
    )

    assert token.access_token == "new-access"
    assert http_client.post.await_count == 1
    form = http_client.post.await_args.kwargs["data"]
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "refresh-secret"
    assert db.get("codex-main").refresh_token == "new-refresh"


def test_managed_oauth_device_flow_stores_token_after_poll(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db = OAuthTokensDB(encryption_key=Fernet.generate_key().decode("ascii"))
    http_client = Mock()
    http_client.post = AsyncMock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "device_code": "device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://issuer.example/device",
                    "expires_in": 600,
                    "interval": 1,
                },
                request=httpx.Request("POST", "https://issuer.example/oauth/device/code"),
            ),
            httpx.Response(
                200,
                json={"access_token": "device-access", "refresh_token": "device-refresh", "expires_in": 3600},
                request=httpx.Request("POST", "https://issuer.example/oauth/token"),
            ),
        ]
    )
    service = ManagedOAuthService(tokens_db=db, http_client=http_client, time_func=lambda: 100)

    start = run_async(service.start_device_login(provider_name="codex", auth_config=_auth_config()))
    result = run_async(service.poll_device_login(provider_name="codex", state=start.state))

    assert start.user_code == "ABCD-EFGH"
    assert result.status == "complete"
    record = db.get("codex-main")
    assert record.access_token == "device-access"
    assert record.refresh_token == "device-refresh"


def test_managed_oauth_non_json_error_does_not_expose_secret_body(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db = OAuthTokensDB(encryption_key=Fernet.generate_key().decode("ascii"))
    db.upsert_tokens(
        credential_id="codex-main",
        provider_name="codex",
        auth_type="codex_oauth",
        access_token="old-access",
        refresh_token="refresh-secret",
        expires_at=100,
    )
    http_client = Mock()
    http_client.post = AsyncMock(
        return_value=httpx.Response(
            502,
            text="upstream echoed refresh-secret and client-id",
            request=httpx.Request("POST", "https://issuer.example/oauth/token"),
        )
    )
    service = ManagedOAuthService(tokens_db=db, http_client=http_client, time_func=lambda: 200)

    try:
        run_async(
            service.get_access_token(
                provider_name="codex",
                auth_config=_auth_config(),
            )
        )
    except OAuthRefreshFailed as exc:
        detail = str(exc)
    else:
        raise AssertionError("Expected OAuthRefreshFailed")

    assert "refresh-secret" not in detail
    assert "client-id" not in detail
    assert "non-JSON" in detail


def test_managed_oauth_json_error_does_not_expose_untrusted_error_text(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db = OAuthTokensDB(encryption_key=Fernet.generate_key().decode("ascii"))
    db.upsert_tokens(
        credential_id="codex-main",
        provider_name="codex",
        auth_type="codex_oauth",
        access_token="old-access",
        refresh_token="refresh-secret",
        expires_at=100,
    )
    http_client = Mock()
    http_client.post = AsyncMock(
        return_value=httpx.Response(
            400,
            json={"error": "refresh-secret leaked by upstream"},
            request=httpx.Request("POST", "https://issuer.example/oauth/token"),
        )
    )
    service = ManagedOAuthService(tokens_db=db, http_client=http_client, time_func=lambda: 200)

    try:
        run_async(
            service.get_access_token(
                provider_name="codex",
                auth_config=_auth_config(),
            )
        )
    except OAuthRefreshFailed as exc:
        detail = str(exc)
    else:
        raise AssertionError("Expected OAuthRefreshFailed")

    assert "refresh-secret" not in detail
    assert detail == "OAuth endpoint returned an error."


def test_managed_oauth_invalid_grant_marks_reauth_required(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db = OAuthTokensDB(encryption_key=Fernet.generate_key().decode("ascii"))
    db.upsert_tokens(
        credential_id="codex-main",
        provider_name="codex",
        auth_type="codex_oauth",
        access_token="old-access",
        refresh_token="refresh-secret",
        expires_at=100,
    )
    http_client = Mock()
    http_client.post = AsyncMock(
        return_value=httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "refresh-secret"},
            request=httpx.Request("POST", "https://issuer.example/oauth/token"),
        )
    )
    service = ManagedOAuthService(tokens_db=db, http_client=http_client, time_func=lambda: 200)

    try:
        run_async(
            service.get_access_token(
                provider_name="codex",
                auth_config=_auth_config(),
            )
        )
    except OAuthRefreshFailed as exc:
        detail = str(exc)
    else:
        raise AssertionError("Expected OAuthRefreshFailed")

    assert detail == "invalid_grant"
    assert db.get_status("codex-main").reauth_reason == "invalid_grant"
