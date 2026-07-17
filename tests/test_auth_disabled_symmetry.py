import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.middleware.auth import (
    API_KEY_DISABLED_DETAIL,
    API_KEY_DISABLED_HTML,
    ApiKeyAuthMiddleware,
    ROLE_USER,
    SESSION_COOKIE_NAME,
    create_authenticated_session,
)
from tests.runtime_test_support import bind_app_services


class FakeApiKeysDB:
    def __init__(self, record: ApiKeyRecord):
        self.record = record

    def get_by_key(self, api_key: str) -> ApiKeyRecord | None:
        if api_key == self.record.api_key:
            return self.record
        return None

    def get_by_id(self, key_id: int) -> ApiKeyRecord | None:
        if key_id == self.record.id:
            return self.record
        return None


def build_test_app(record: ApiKeyRecord) -> FastAPI:
    app = FastAPI()
    bind_app_services(app, api_keys_db=FakeApiKeysDB(record))
    app.add_middleware(ApiKeyAuthMiddleware)

    @app.get("/v1/models")
    async def models():
        return {"status": "protected-models"}

    @app.get("/v1/ui/usage-stats")
    async def usage_stats():
        return {"status": "protected-ui"}

    return app


def make_disabled_record() -> ApiKeyRecord:
    return ApiKeyRecord(
        id=7,
        name="disabled-user",
        api_key="disabled-key",
        budget_usd=None,
        spent_usd=0.0,
        rpm=None,
        tpm=None,
        disabled=True,
    )


class AuthDisabledSymmetryTests(unittest.TestCase):
    def setUp(self):
        self.gateway_key_patcher = patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key",
            "test-gateway-key",
        )
        self.gateway_key_patcher.start()
        self.record = make_disabled_record()
        self.client = TestClient(build_test_app(self.record))

    def tearDown(self):
        self.client.close()
        self.gateway_key_patcher.stop()

    def test_disabled_bearer_key_returns_403_json(self):
        response = self.client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {self.record.api_key}"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": API_KEY_DISABLED_DETAIL})

    def test_disabled_session_key_returns_403_json(self):
        self.client.cookies.set(
            SESSION_COOKIE_NAME,
            create_authenticated_session(role=ROLE_USER, key_id=self.record.id),
        )

        response = self.client.get("/v1/models", follow_redirects=False)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": API_KEY_DISABLED_DETAIL})
        self.assertNotIn("location", response.headers)

    def test_disabled_session_key_returns_403_html_for_ui_navigation(self):
        self.client.cookies.set(
            SESSION_COOKIE_NAME,
            create_authenticated_session(role=ROLE_USER, key_id=self.record.id),
        )

        response = self.client.get(
            "/v1/ui/usage-stats",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn(API_KEY_DISABLED_HTML, response.text)
        self.assertNotIn("location", response.headers)


if __name__ == "__main__":
    unittest.main()
