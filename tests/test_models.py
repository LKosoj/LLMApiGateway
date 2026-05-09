import unittest

import tests.test_models_runtime_config as models_runtime_module


class ModelsLiveReloadTests(unittest.TestCase):
    def setUp(self):
        self.runtime_case = models_runtime_module.ModelsRuntimeConfigTests()
        self.runtime_case.setUp()

    def tearDown(self):
        self.runtime_case.tearDown()

    def test_models_endpoint_returns_updated_data_after_config_save(self):
        headers = {"Authorization": "Bearer test-gateway-key"}

        with self.runtime_case._client() as (client, _fake_http_client):
            initial_response = client.get("/v1/models", headers=headers)
            save_response = client.post(
                "/v1/config/models-rules",
                content=models_runtime_module.UPDATED_RULES_TEXT,
                headers={**headers, "Content-Type": "text/plain"},
            )
            updated_response = client.get("/v1/models", headers=headers)

        self.assertEqual(initial_response.status_code, 200)
        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(updated_response.status_code, 200)
        self.assertIn("gateway-model-v1", {item["id"] for item in initial_response.json()["data"]})
        self.assertIn("gateway-model-v2", {item["id"] for item in updated_response.json()["data"]})
        self.assertNotIn("gateway-model-v1", {item["id"] for item in updated_response.json()["data"]})


if __name__ == "__main__":
    unittest.main()
