import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.v1.admin_api_keys import admin_api_keys_router
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.middleware.auth import (
    ApiKeyAuthMiddleware,
    DEFAULT_UI_PATH,
    ROLE_USER,
    SESSION_COOKIE_NAME,
    create_authenticated_session,
)
from tests.runtime_test_support import bind_app_services


class FakeApiKeysDB:
    def __init__(self, record: ApiKeyRecord):
        self.record = record

    def get_by_id(self, key_id: int) -> ApiKeyRecord | None:
        if key_id == self.record.id:
            return self.record
        return None


def build_test_app(record: ApiKeyRecord) -> FastAPI:
    app = FastAPI()
    bind_app_services(app, api_keys_db=FakeApiKeysDB(record))
    app.add_middleware(ApiKeyAuthMiddleware)
    app.include_router(admin_api_keys_router, prefix="/v1")
    return app


def make_user_record() -> ApiKeyRecord:
    return ApiKeyRecord(
        id=11,
        name="user",
        api_key="virtual-key",
        budget_usd=None,
        spent_usd=0.0,
        rpm=None,
        tpm=None,
        disabled=False,
    )


class ApiKeysUiMasterOnlyTests(unittest.TestCase):
    def setUp(self):
        self.gateway_key_patcher = patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key",
            "test-gateway-key",
        )
        self.gateway_key_patcher.start()
        self.record = make_user_record()
        self.client = TestClient(build_test_app(self.record))

    def tearDown(self):
        self.client.close()
        self.gateway_key_patcher.stop()

    def test_non_master_session_is_redirected_before_api_keys_ui_handler(self):
        self.client.cookies.set(
            SESSION_COOKIE_NAME,
            create_authenticated_session(role=ROLE_USER, key_id=self.record.id),
        )

        response = self.client.get(
            "/v1/ui/api-keys",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], DEFAULT_UI_PATH)

    def test_master_session_can_open_api_keys_ui(self):
        self.client.cookies.set(SESSION_COOKIE_NAME, create_authenticated_session())

        response = self.client.get(
            "/v1/ui/api-keys",
            headers={"Accept": "text/html"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
