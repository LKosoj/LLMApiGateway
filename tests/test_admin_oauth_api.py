from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.v1.admin_oauth import router as admin_oauth_router
from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.db.oauth_tokens_db import OAuthCredentialStatus


@dataclass
class _FakeManagedOAuthService:
    imported: dict | None = None

    async def list_provider_statuses(self, providers_config):
        return [
            {
                "provider_name": "codex",
                "auth_type": "codex_oauth",
                "credential_id": "codex-main",
                "login_methods": ["device_code"],
                "token_status": OAuthCredentialStatus(
                    credential_id="codex-main",
                    provider_name="codex",
                    auth_type="codex_oauth",
                    status="active",
                    expires_at=123456,
                    scopes=["openid"],
                    account_label="user@example.com",
                    metadata={},
                    reauth_reason=None,
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                ),
            }
        ]

    async def import_tokens(self, **kwargs):
        self.imported = kwargs
        return OAuthCredentialStatus(
            credential_id="codex-main",
            provider_name="codex",
            auth_type="codex_oauth",
            status="active",
            expires_at=kwargs["expires_at"],
            scopes=kwargs["scopes"] or [],
            account_label=kwargs["account_label"],
            metadata={},
            reauth_reason=None,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )


def _app(fake_service):
    app = FastAPI()
    app.include_router(admin_oauth_router, prefix="/v1")
    app.state.managed_oauth_service = fake_service
    app.state.config_loader = SimpleNamespace(
        providers_config={
            "codex": ProviderDetails(
                baseUrl="https://codex.example",
                auth={
                    "type": "codex_oauth",
                    "credential_id": "codex-main",
                    "oauth_client": {
                        "client_id": "client-id",
                        "token_endpoint": "https://issuer.example/oauth/token",
                        "device_authorization_endpoint": "https://issuer.example/oauth/device/code",
                        "scopes": ["openid"],
                    },
                },
            )
        }
    )
    return app


def test_admin_oauth_providers_response_does_not_expose_tokens():
    with TestClient(_app(_FakeManagedOAuthService())) as client:
        response = client.get("/v1/admin/oauth/providers")

    assert response.status_code == 200
    body = response.json()
    raw = response.text
    assert body["providers"][0]["credential_id"] == "codex-main"
    assert "access_token" not in raw
    assert "refresh_token" not in raw


def test_admin_oauth_import_passes_tokens_to_service_without_echoing_them():
    fake_service = _FakeManagedOAuthService()
    with TestClient(_app(fake_service)) as client:
        response = client.post(
            "/v1/admin/oauth/codex/import",
            json={
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
                "account_label": "user@example.com",
            },
        )

    assert response.status_code == 200
    assert fake_service.imported["access_token"] == "access-secret"
    assert fake_service.imported["refresh_token"] == "refresh-secret"
    assert "access-secret" not in response.text
    assert "refresh-secret" not in response.text
