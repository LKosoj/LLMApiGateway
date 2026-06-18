import sqlite3

from cryptography.fernet import Fernet

from llm_gateway_core.db.oauth_tokens_db import OAuthTokensDB, STATUS_ACTIVE, STATUS_REAUTH_REQUIRED


def test_oauth_tokens_are_encrypted_at_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db = OAuthTokensDB(encryption_key=Fernet.generate_key().decode("ascii"))

    status = db.upsert_tokens(
        credential_id="codex-main",
        provider_name="codex",
        auth_type="codex_oauth",
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=123456,
        scopes=["openid"],
        account_label="user@example.com",
    )

    assert status.status == STATUS_ACTIVE
    raw_db = (tmp_path / "oauth_tokens.db").read_bytes()
    assert b"access-secret" not in raw_db
    assert b"refresh-secret" not in raw_db

    record = db.get("codex-main")
    assert record is not None
    assert record.access_token == "access-secret"
    assert record.refresh_token == "refresh-secret"
    assert record.scopes == ["openid"]
    assert record.account_label == "user@example.com"


def test_oauth_tokens_status_does_not_expose_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db = OAuthTokensDB(encryption_key=Fernet.generate_key().decode("ascii"))
    db.upsert_tokens(
        credential_id="xai-main",
        provider_name="xai",
        auth_type="xai_oauth",
        access_token="access-secret",
        refresh_token="refresh-secret",
    )

    status = db.get_status("xai-main")

    assert status is not None
    assert status.credential_id == "xai-main"
    assert not hasattr(status, "access_token")
    assert not hasattr(status, "refresh_token")


def test_oauth_tokens_mark_reauth_required(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db = OAuthTokensDB(encryption_key=Fernet.generate_key().decode("ascii"))
    db.upsert_tokens(
        credential_id="codex-main",
        provider_name="codex",
        auth_type="codex_oauth",
        access_token="access-secret",
        refresh_token=None,
    )

    db.mark_reauth_required("codex-main", "invalid_grant")
    status = db.get_status("codex-main")

    assert status is not None
    assert status.status == STATUS_REAUTH_REQUIRED
    assert status.reauth_reason == "invalid_grant"


def test_oauth_tokens_require_encryption_key_for_secret_access(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db = OAuthTokensDB(encryption_key=Fernet.generate_key().decode("ascii"))
    db.upsert_tokens(
        credential_id="codex-main",
        provider_name="codex",
        auth_type="codex_oauth",
        access_token="access-secret",
        refresh_token=None,
    )

    db_without_key = OAuthTokensDB()

    try:
        db_without_key.get("codex-main")
    except ValueError as exc:
        assert "OAUTH_CREDENTIAL_ENCRYPTION_KEY" in str(exc)
    else:
        raise AssertionError("Expected ValueError for missing encryption key")

    with sqlite3.connect(tmp_path / "oauth_tokens.db") as conn:
        row = conn.execute("SELECT access_token_enc FROM oauth_credentials").fetchone()
    assert row is not None
    assert row[0] != "access-secret"
