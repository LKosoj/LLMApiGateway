import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from main import configure_cors


def build_cors_test_app(cors_allow_origins: list[str] | None = None) -> FastAPI:
    app = FastAPI()
    configure_cors(app, cors_allow_origins)

    @app.get("/v1/ui/rules-editor")
    async def ui_page():
        return {"status": "ok"}

    return app


class CorsConfigurationTests(unittest.TestCase):
    def test_default_configuration_does_not_emit_wildcard_cors_header(self):
        with TestClient(build_cors_test_app()) as client:
            response = client.get("/v1/ui/rules-editor", headers={"Origin": "https://evil.example"})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_same_origin_ui_path_works_without_cors_middleware(self):
        with TestClient(build_cors_test_app()) as client:
            response = client.get("/v1/ui/rules-editor")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertNotIn("access-control-allow-origin", response.headers)


if __name__ == "__main__":
    unittest.main()
