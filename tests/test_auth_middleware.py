import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.db.rejections_db import RejectionsDB
from llm_gateway_core.middleware.auth import (
    ROLE_USER,
    SESSION_COOKIE_NAME,
    api_key_auth,
    create_authenticated_session,
)
from llm_gateway_core.services.active_requests import get_active_requests_registry


def build_test_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(api_key_auth)
    app.include_router(auth_router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

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

    @app.get("/v1/api/usage-records")
    async def stats():
        return {"status": "protected-stats"}

    @app.get("/docs")
    async def docs():
        return {"status": "protected-docs"}

    return app


class AuthMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.gateway_key_patchers = [
            patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "test-gateway-key"),
            patch("llm_gateway_core.api.auth_ui.settings.gateway_api_key", "test-gateway-key"),
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
        self.assertEqual(self.client.get("/static/test.js").status_code, 200)

        response = self.client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/auth/login?next=/v1/ui/usage-stats")

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
        self.assertEqual(root_response.headers["location"], "/v1/ui/usage-stats")

        login_page_response = self.client.get("/auth/login", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(login_page_response.status_code, 303)
        self.assertEqual(login_page_response.headers["location"], "/v1/ui/usage-stats")

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
        self.assertEqual(root_response.headers["location"], "/v1/ui/usage-stats")

    def test_invalid_login_is_rejected(self):
        response = self.client.post(
            "/auth/login",
            json={"api_key": "wrong-key", "next": "/v1/ui/usage-stats"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Invalid API Key"})

    def test_sensitive_paths_accept_valid_bearer_token(self):
        headers = {"Authorization": "Bearer test-gateway-key", "Accept": "text/html"}
        self.assertEqual(self.client.post("/v1/chat/completions", json={"model": "demo"}, headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/v1/models", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/v1/ui/rules-editor", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/v1/openrouter/free-models", headers=headers).status_code, 200)

        root_response = self.client.get("/", headers=headers, follow_redirects=False)
        self.assertEqual(root_response.status_code, 303)
        self.assertEqual(root_response.headers["location"], "/v1/ui/usage-stats")

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
        self.client.cookies.set(
            SESSION_COOKIE_NAME,
            create_authenticated_session(role=ROLE_USER, key_id=7),
        )

        response = self.client.get("/v1/openrouter/free-models")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "This endpoint is reserved for the master API key"},
        )

    def test_unauthenticated_api_request_records_auth_invalid_rejection(self):
        mock_db = MagicMock(spec=RejectionsDB)
        app = build_test_app()
        app.state.rejections_db = mock_db

        with TestClient(app) as client:
            response = client.get("/v1/models")

        self.assertEqual(response.status_code, 401)
        mock_db.insert_rejection.assert_called()
        call_kwargs = mock_db.insert_rejection.call_args.kwargs
        self.assertEqual(call_kwargs["category"], "auth_invalid")
        self.assertEqual(call_kwargs["status_code"], 401)

    def test_invalid_bearer_token_records_auth_invalid_rejection(self):
        mock_db = MagicMock(spec=RejectionsDB)
        app = build_test_app()
        app.state.rejections_db = mock_db

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
        app = build_test_app()
        app.state.rejections_db = mock_db

        session = create_authenticated_session(role=ROLE_USER, key_id=7)
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE_NAME, session)
            response = client.get("/v1/openrouter/free-models")

        self.assertEqual(response.status_code, 403)
        mock_db.insert_rejection.assert_called()
        call_kwargs = mock_db.insert_rejection.call_args.kwargs
        self.assertEqual(call_kwargs["category"], "master_only")
        self.assertEqual(call_kwargs["status_code"], 403)


if __name__ == "__main__":
    unittest.main()
