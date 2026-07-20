import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastapi.staticfiles import StaticFiles

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.db.api_keys_db import ApiKeyRecord, ApiKeysDB
from llm_gateway_core.db.rejections_db import RejectionsDB
from llm_gateway_core.middleware.auth import (
    ApiKeyAuthMiddleware,
    MASTER_ONLY_REDIRECT_PATH,
    ROLE_USER,
    SESSION_COOKIE_NAME,
    create_authenticated_session,
)
from llm_gateway_core.middleware.request_logging import RequestLoggingASGIMiddleware
from llm_gateway_core.services.active_requests import get_active_requests_registry
from llm_gateway_core.services.ip_blocklist import IpBlockGuard
from tests.runtime_test_support import bind_app_services


def build_test_app(**service_overrides: object) -> FastAPI:
    app = FastAPI()
    bind_app_services(app, **service_overrides)
    app.add_middleware(ApiKeyAuthMiddleware)
    app.add_middleware(RequestLoggingASGIMiddleware)
    app.include_router(auth_router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.head("/health")
    async def health_head():
        return None

    @app.get("/static/test.js")
    async def static_asset():
        return {"status": "public"}

    @app.get("/v1/ui/rules-editor")
    async def ui_page():
        return {"status": "protected-ui"}

    @app.get("/v1/ui/usage-stats")
    async def usage_stats():
        return {"status": "protected-usage-stats"}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        active_records = get_active_requests_registry(request.app).list_records()
        return {
            "status": "protected-chat",
            "active_records": len(active_records),
            "active_request_id": getattr(request.state, "llmgateway_active_request_id", None),
        }

    @app.get("/v1/models")
    async def models():
        return {"status": "protected-models"}

    @app.get("/v1/config/providers")
    async def config():
        return {"status": "protected-config"}

    @app.get("/v1/openrouter/free-models")
    async def openrouter_free_models():
        return {"status": "protected-openrouter-free-models"}

    @app.post("/v1/ui/providers-config")
    async def providers_config():
        return {"status": "protected-providers-config"}

    @app.get("/v1/fallback-model-evals")
    async def fallback_model_evals():
        return {"status": "protected-fallback-model-evals"}

    @app.get("/v1/api/usage-records")
    async def stats():
        return {"status": "protected-stats"}

    @app.get("/docs")
    async def docs():
        return {"status": "protected-docs"}

    return app


def make_enabled_user_record(key_id: int = 7) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=key_id,
        name="enabled-user",
        api_key="enabled-user-key",
        budget_usd=None,
        spent_usd=0.0,
        rpm=None,
        tpm=None,
        disabled=False,
    )


def api_keys_db_with_record(record: ApiKeyRecord) -> MagicMock:
    db = MagicMock(spec=ApiKeysDB)
    db.get_by_key.return_value = None
    db.get_by_id.return_value = record
    return db


class AuthMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.gateway_key_patchers = [
            patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "test-gateway-key"),
            patch("llm_gateway_core.api.auth_ui.settings.gateway_api_key", "test-gateway-key"),
            patch("llm_gateway_core.middleware.auth.settings.session_cookie_secure", False),
        ]
        for patcher in self.gateway_key_patchers:
            patcher.start()

        self.client = TestClient(build_test_app())

    def tearDown(self):
        self.client.close()
        for patcher in reversed(self.gateway_key_patchers):
            patcher.stop()

    def test_public_paths_and_root_redirect_without_session(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.head("/health").status_code, 200)
        self.assertEqual(self.client.get("/static/test.js").status_code, 200)

        response = self.client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/auth/login?next=/v1/ui/overview")

    def test_generated_images_mount_requires_auth_and_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images_root = root / "images"
            research_root = images_root / "research-1"
            research_root.mkdir(parents=True)
            expected = b"\x89PNG\r\n\x1a\nverified"
            (research_root / "image.png").write_bytes(expected)
            (root / "secret.png").write_bytes(b"must-not-leak")
            app = build_test_app()
            app.mount(
                "/outputs/images",
                StaticFiles(directory=images_root, check_dir=False),
                name="test-outputs-images",
            )

            with TestClient(app) as client:
                unauthenticated = client.get(
                    "/outputs/images/research-1/image.png"
                )
                authenticated = client.get(
                    "/outputs/images/research-1/image.png",
                    headers={"Authorization": "Bearer test-gateway-key"},
                )
                traversal = client.get(
                    "/outputs/images/%2e%2e/secret.png",
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(authenticated.status_code, 200)
        self.assertEqual(authenticated.content, expected)
        self.assertEqual(traversal.status_code, 404)
        self.assertNotEqual(traversal.content, b"must-not-leak")

    def test_html_pages_redirect_to_login_without_session_but_api_stays_401(self):
        response = self.client.get(
            "/v1/ui/rules-editor",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/auth/login?next=/v1/ui/rules-editor")

        docs_response = self.client.get("/docs", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(docs_response.status_code, 303)
        self.assertEqual(docs_response.headers["location"], "/auth/login?next=/docs")

        self.assertEqual(self.client.post("/v1/chat/completions", json={"model": "demo"}).status_code, 401)
        self.assertEqual(self.client.get("/v1/models").status_code, 401)
        self.assertEqual(self.client.get("/v1/config/providers").status_code, 401)
        self.assertEqual(self.client.get("/v1/api/usage-records").status_code, 401)

    def test_login_creates_session_cookie_and_allows_ui_without_bearer(self):
        login_response = self.client.post(
            "/auth/login",
            json={"api_key": "test-gateway-key", "next": "/v1/ui/usage-stats"},
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(
            login_response.json(),
            {"redirect_to": "/v1/ui/usage-stats", "role": "master"},
        )
        self.assertIn(SESSION_COOKIE_NAME, self.client.cookies)

        self.assertEqual(self.client.get("/v1/ui/rules-editor").status_code, 200)

        root_response = self.client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(root_response.status_code, 303)
        self.assertEqual(root_response.headers["location"], "/v1/ui/overview")

        login_page_response = self.client.get("/auth/login", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(login_page_response.status_code, 303)
        self.assertEqual(login_page_response.headers["location"], "/v1/ui/overview")

    def test_login_cookie_is_secure_by_default(self):
        app = build_test_app()
        with patch("llm_gateway_core.middleware.auth.settings.session_cookie_secure", True):
            with TestClient(app, base_url="https://testserver") as client:
                response = client.post(
                    "/auth/login",
                    json={"api_key": "test-gateway-key", "next": "/v1/ui/usage-stats"},
                )

        self.assertEqual(response.status_code, 200)
        set_cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=lax", set_cookie)
        self.assertIn("Secure", set_cookie)

    def test_login_cookie_secure_can_be_disabled_for_local_http(self):
        app = build_test_app()
        with patch("llm_gateway_core.middleware.auth.settings.session_cookie_secure", False):
            with TestClient(app) as client:
                response = client.post(
                    "/auth/login",
                    json={"api_key": "test-gateway-key", "next": "/v1/ui/usage-stats"},
                )

        self.assertEqual(response.status_code, 200)
        set_cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=lax", set_cookie)
        self.assertNotIn("Secure", set_cookie)

    def test_persistent_cookie_survives_new_client_instance(self):
        login_response = self.client.post(
            "/auth/login",
            json={"api_key": "test-gateway-key", "next": "/v1/ui/usage-stats"},
        )
        self.assertEqual(login_response.status_code, 200)
        session_cookie_value = self.client.cookies.get(SESSION_COOKIE_NAME)
        self.assertTrue(session_cookie_value)

        with TestClient(build_test_app()) as fresh_client:
            fresh_client.cookies.set(SESSION_COOKIE_NAME, session_cookie_value)

            ui_response = fresh_client.get("/v1/ui/usage-stats")
            root_response = fresh_client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)

        self.assertEqual(ui_response.status_code, 200)
        self.assertEqual(root_response.status_code, 303)
        self.assertEqual(root_response.headers["location"], "/v1/ui/overview")

    def test_invalid_login_is_rejected(self):
        response = self.client.post(
            "/auth/login",
            json={"api_key": "wrong-key", "next": "/v1/ui/usage-stats"},
        )
        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["detail"], "Invalid API Key")
        self.assertEqual(payload["error"]["message"], "Invalid API Key")
        self.assertEqual(payload["error"]["code"], "auth_invalid_api_key")
        self.assertIn(f"{SESSION_COOKIE_NAME}=", response.headers["set-cookie"])
        self.assertIn("Max-Age=0", response.headers["set-cookie"])

    def test_login_rejects_malformed_request_with_stable_code(self):
        requests = (
            {"content": "{", "headers": {"Content-Type": "application/json"}},
            {"json": []},
        )

        for request_kwargs in requests:
            with self.subTest(request_kwargs=request_kwargs):
                response = self.client.post("/auth/login", **request_kwargs)

                self.assertEqual(response.status_code, 400)
                payload = response.json()
                self.assertEqual(payload["detail"], "Request body must be a JSON object")
                self.assertEqual(
                    payload["error"]["message"],
                    "Request body must be a JSON object",
                )
                self.assertEqual(payload["error"]["code"], "auth_invalid_request")
                self.assertNotIn("set-cookie", response.headers)

    def test_invalid_login_records_auth_invalid_rejection(self):
        mock_db = MagicMock(spec=RejectionsDB)
        app = build_test_app(rejections_db=mock_db)

        with TestClient(app) as client:
            response = client.post(
                "/auth/login",
                json={"api_key": "wrong-key", "next": "/v1/ui/usage-stats"},
            )

        self.assertEqual(response.status_code, 401)
        mock_db.insert_rejection.assert_called()
        call_kwargs = mock_db.insert_rejection.call_args.kwargs
        self.assertEqual(call_kwargs["category"], "auth_invalid")
        self.assertEqual(call_kwargs["status_code"], 401)
        self.assertEqual(call_kwargs["path"], "/auth/login")

    def test_login_blocks_after_repeated_invalid_keys(self):
        guard = IpBlockGuard(max_failures=2, block_seconds=600.0)
        mock_db = MagicMock(spec=RejectionsDB)
        app = build_test_app(ip_block_guard=guard, rejections_db=mock_db)

        with TestClient(app) as client:
            for _ in range(2):
                response = client.post(
                    "/auth/login",
                    json={"api_key": "wrong-key", "next": "/v1/ui/usage-stats"},
                )
                self.assertEqual(response.status_code, 401)

            blocked = client.post(
                "/auth/login",
                json={"api_key": "test-gateway-key", "next": "/v1/ui/usage-stats"},
            )
            login_page = client.get("/auth/login")
            health = client.get("/health")
            static_asset = client.get("/static/test.js")

        self.assertEqual(blocked.status_code, 429)
        blocked_payload = blocked.json()
        self.assertEqual(
            blocked_payload["detail"],
            "Too many failed authentication attempts. Try again later.",
        )
        self.assertEqual(blocked_payload["error"]["code"], "auth_rate_limited")
        self.assertEqual(blocked.headers.get("Retry-After"), "600")
        self.assertNotIn("set-cookie", blocked.headers)
        self.assertEqual(login_page.status_code, 200)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(static_asset.status_code, 200)
        categories = [call.kwargs["category"] for call in mock_db.insert_rejection.call_args_list]
        self.assertEqual(categories.count("auth_invalid"), 2)
        self.assertEqual(categories.count("ip_blocked"), 1)

    def test_disabled_login_records_key_disabled_without_blocking_ip(self):
        guard = IpBlockGuard(max_failures=1, block_seconds=600.0)
        mock_rejections_db = MagicMock(spec=RejectionsDB)
        mock_api_keys_db = MagicMock(spec=ApiKeysDB)
        mock_api_keys_db.get_by_key.return_value = ApiKeyRecord(
            id=7,
            name="disabled-key",
            api_key="disabled-key",
            budget_usd=None,
            spent_usd=0.0,
            rpm=None,
            tpm=None,
            disabled=True,
        )
        app = build_test_app(
            api_keys_db=mock_api_keys_db,
            ip_block_guard=guard,
            rejections_db=mock_rejections_db,
        )
        app.state.api_keys_db = object()
        app.state.ip_block_guard = object()

        with TestClient(app) as client:
            disabled_response = client.post("/auth/login", json={"api_key": "disabled-key"})
            valid_response = client.post("/auth/login", json={"api_key": "test-gateway-key"})
        invalid_response = self.client.post("/auth/login", json={"api_key": "wrong-key"})

        self.assertEqual(disabled_response.status_code, 401)
        disabled_payload = disabled_response.json()
        self.assertEqual(disabled_payload["detail"], "Invalid API Key")
        self.assertEqual(disabled_payload["error"]["message"], "Invalid API Key")
        self.assertEqual(
            disabled_payload["error"]["code"],
            "auth_invalid_api_key",
        )
        self.assertEqual(
            disabled_payload["error"]["code"],
            invalid_response.json()["error"]["code"],
        )
        self.assertIn(
            f"{SESSION_COOKIE_NAME}=",
            disabled_response.headers["set-cookie"],
        )
        self.assertIn("Max-Age=0", disabled_response.headers["set-cookie"])
        self.assertEqual(valid_response.status_code, 200)
        categories = [call.kwargs["category"] for call in mock_rejections_db.insert_rejection.call_args_list]
        self.assertEqual(categories, ["key_disabled"])

    def test_successful_login_resets_invalid_key_counter(self):
        guard = IpBlockGuard(max_failures=3, block_seconds=600.0)
        app = build_test_app(
            ip_block_guard=guard,
            rejections_db=MagicMock(spec=RejectionsDB),
        )

        with TestClient(app) as client:
            for _ in range(2):
                self.assertEqual(
                    client.post("/auth/login", json={"api_key": "wrong-key"}).status_code,
                    401,
                )
            self.assertEqual(
                client.post("/auth/login", json={"api_key": "test-gateway-key"}).status_code,
                200,
            )
            for _ in range(2):
                self.assertEqual(
                    client.post("/auth/login", json={"api_key": "wrong-key"}).status_code,
                    401,
                )
            self.assertEqual(
                client.post("/auth/login", json={"api_key": "test-gateway-key"}).status_code,
                200,
            )

    def test_sensitive_paths_accept_valid_bearer_token(self):
        headers = {"Authorization": "Bearer test-gateway-key", "Accept": "text/html"}
        self.assertEqual(self.client.post("/v1/chat/completions", json={"model": "demo"}, headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/v1/models", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/v1/ui/rules-editor", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/v1/openrouter/free-models", headers=headers).status_code, 200)

        root_response = self.client.get("/", headers=headers, follow_redirects=False)
        self.assertEqual(root_response.status_code, 303)
        self.assertEqual(root_response.headers["location"], "/v1/ui/overview")

    def test_usage_producing_request_is_active_during_handler_and_finished_after_response(self):
        response = self.client.post(
            "/v1/chat/completions",
            json={"model": "demo"},
            headers={"Authorization": "Bearer test-gateway-key"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["active_records"], 1)
        self.assertTrue(response.json()["active_request_id"])
        self.assertEqual(get_active_requests_registry(self.client.app).list_records(), [])

    def test_sensitive_paths_reject_invalid_authorization_formats(self):
        invalid_headers = [
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer    "},
            {"Authorization": "Foo Bearer test-gateway-key"},
            {"Authorization": "Bearer test-gateway-key extra"},
            {"Authorization": "Basic test-gateway-key"},
            {"Authorization": "Token test-gateway-key"},
            {"Authorization": "Bearertest-gateway-key"},
        ]

        for headers in invalid_headers:
            with self.subTest(headers=headers):
                self.assertEqual(
                    self.client.get("/v1/models", headers=headers).status_code,
                    401,
                )

    def test_sensitive_paths_accept_case_insensitive_bearer_scheme(self):
        valid_headers = [
            {"Authorization": "bearer test-gateway-key"},
            {"Authorization": "Bearer     test-gateway-key"},
        ]

        for headers in valid_headers:
            with self.subTest(headers=headers):
                self.assertEqual(
                    self.client.get("/v1/models", headers=headers).status_code,
                    200,
                )

    def test_openrouter_free_models_status_is_master_only(self):
        record = make_enabled_user_record()
        app = build_test_app(api_keys_db=api_keys_db_with_record(record))
        with TestClient(app) as client:
            client.cookies.set(
                SESSION_COOKIE_NAME,
                create_authenticated_session(role=ROLE_USER, key_id=record.id),
            )

            response = client.get("/v1/openrouter/free-models")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "This endpoint is reserved for the master API key"},
        )

    def test_config_editor_legacy_and_eval_paths_are_master_only(self):
        record = make_enabled_user_record()
        app = build_test_app(api_keys_db=api_keys_db_with_record(record))
        with TestClient(app) as client:
            client.cookies.set(
                SESSION_COOKIE_NAME,
                create_authenticated_session(role=ROLE_USER, key_id=record.id),
            )

            providers_response = client.post("/v1/ui/providers-config", content="[]")
            eval_response = client.get("/v1/fallback-model-evals")

        self.assertEqual(providers_response.status_code, 403)
        self.assertEqual(eval_response.status_code, 403)
        self.assertEqual(
            providers_response.json(),
            {"detail": "This endpoint is reserved for the master API key"},
        )
        self.assertEqual(
            eval_response.json(),
            {"detail": "This endpoint is reserved for the master API key"},
        )

    def test_unauthenticated_api_request_records_auth_invalid_rejection(self):
        mock_db = MagicMock(spec=RejectionsDB)
        app = build_test_app(rejections_db=mock_db)

        with TestClient(app) as client:
            response = client.get("/v1/models")

        self.assertEqual(response.status_code, 401)
        mock_db.insert_rejection.assert_called()
        call_kwargs = mock_db.insert_rejection.call_args.kwargs
        self.assertEqual(call_kwargs["category"], "auth_invalid")
        self.assertEqual(call_kwargs["status_code"], 401)

    def test_invalid_bearer_token_records_auth_invalid_rejection(self):
        mock_db = MagicMock(spec=RejectionsDB)
        app = build_test_app(rejections_db=mock_db)

        with TestClient(app) as client:
            response = client.get(
                "/v1/models",
                headers={"Authorization": "Bearer totally-wrong-key"},
            )

        self.assertEqual(response.status_code, 403)
        mock_db.insert_rejection.assert_called()
        call_kwargs = mock_db.insert_rejection.call_args.kwargs
        self.assertEqual(call_kwargs["category"], "auth_invalid")
        self.assertEqual(call_kwargs["status_code"], 403)

    def test_master_only_path_for_user_role_records_master_only_rejection(self):
        mock_db = MagicMock(spec=RejectionsDB)
        record = make_enabled_user_record()
        app = build_test_app(
            api_keys_db=api_keys_db_with_record(record),
            rejections_db=mock_db,
        )

        session = create_authenticated_session(role=ROLE_USER, key_id=record.id)
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, session)
            response = client.get("/v1/openrouter/free-models")

        self.assertEqual(response.status_code, 403)
        mock_db.insert_rejection.assert_called()
        call_kwargs = mock_db.insert_rejection.call_args.kwargs
        self.assertEqual(call_kwargs["category"], "master_only")
        self.assertEqual(call_kwargs["status_code"], 403)

    def test_master_only_ui_navigation_for_user_role_redirects_to_access_denied_page(self):
        record = make_enabled_user_record()
        app = build_test_app(api_keys_db=api_keys_db_with_record(record))

        with TestClient(app) as client:
            client.cookies.set(
                SESSION_COOKIE_NAME,
                create_authenticated_session(role=ROLE_USER, key_id=record.id),
            )
            response = client.get(
                "/v1/ui/rules-editor",
                headers={"Accept": "text/html"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], MASTER_ONLY_REDIRECT_PATH)
        self.assertEqual(MASTER_ONLY_REDIRECT_PATH, "/v1/ui/access-denied?reason=master-only")

    def test_master_role_is_never_redirected_to_access_denied_and_can_open_it_directly(self):
        with TestClient(build_test_app()) as client:
            client.cookies.set(SESSION_COOKIE_NAME, create_authenticated_session())

            rules_response = client.get(
                "/v1/ui/rules-editor",
                headers={"Accept": "text/html"},
                follow_redirects=False,
            )
            access_denied_response = client.get(
                "/v1/ui/access-denied",
                headers={"Accept": "text/html"},
            )

        self.assertEqual(rules_response.status_code, 200)
        self.assertEqual(access_denied_response.status_code, 200)
        self.assertIn("text/html", access_denied_response.headers["content-type"])

    def test_user_role_can_open_access_denied_page_directly(self):
        record = make_enabled_user_record()
        app = build_test_app(api_keys_db=api_keys_db_with_record(record))

        with TestClient(app) as client:
            client.cookies.set(
                SESSION_COOKIE_NAME,
                create_authenticated_session(role=ROLE_USER, key_id=record.id),
            )
            response = client.get("/v1/ui/access-denied", headers={"Accept": "text/html"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
