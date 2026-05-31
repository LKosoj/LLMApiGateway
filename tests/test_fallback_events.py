import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.db.fallback_events_db import FallbackEventsDB, validate_fallback_records_pagination
from llm_gateway_core.api.v1.chat import _build_fallback_error_message, classify_error
from llm_gateway_core.services.request_handler import RequestErrorDetail
from tests._async_compat import run_async


class ClassifyErrorTests(unittest.TestCase):
    def test_read_timeout(self):
        self.assertEqual(classify_error("ReadTimeout"), "read_timeout")
        self.assertEqual(classify_error("httpx.ReadTimeout"), "read_timeout")

    def test_connect_error(self):
        self.assertEqual(classify_error("ConnectError: Connection refused"), "connect_error")

    def test_http_429(self):
        self.assertEqual(classify_error("Downstream error 429 from http://example.com"), "http_429")

    def test_http_400_status_code(self):
        self.assertEqual(classify_error("Request failed with status code 400"), "http_400")

    def test_structured_status_code_metadata(self):
        self.assertEqual(classify_error(RequestErrorDetail("slow down", status_code=429)), "http_429")
        self.assertEqual(classify_error(RequestErrorDetail("bad request", status_code=400)), "http_400")
        self.assertEqual(classify_error(RequestErrorDetail("server", status_code=500)), "http_500")

    def test_context_overflow(self):
        self.assertEqual(classify_error("context_length_exceeded"), "context_overflow")
        self.assertEqual(classify_error("prompt is too long"), "context_overflow")

    def test_none_returns_none(self):
        self.assertIsNone(classify_error(None))

    def test_unknown_error(self):
        self.assertEqual(classify_error("something weird happened"), "unknown")

    def test_generic_status_code_error_includes_request_summary(self):
        message = _build_fallback_error_message(
            "Request failed with status code 400",
            {
                "model": "qwc.coder-model",
                "response_format": {"type": "json_object"},
                "max_tokens": 4000,
                "temperature": 0.1,
            },
            is_streaming=False,
        )

        self.assertEqual(
            message,
            "Request failed with status code 400. Request summary: "
            "model=qwc.coder-model, response_format.type=json_object, "
            "stream=false, max_tokens=4000, temperature=0.1.",
        )

    def test_read_timeout_error_includes_request_summary(self):
        message = _build_fallback_error_message(
            "ReadTimeout connecting to https://example.com/chat/completions",
            {
                "model": "zai.glm-5",
                "response_format": {"type": "json_object"},
                "max_tokens": 12000,
                "temperature": 0.1,
            },
            is_streaming=False,
        )

        self.assertEqual(
            message,
            "ReadTimeout connecting to https://example.com/chat/completions. Request summary: "
            "model=zai.glm-5, response_format.type=json_object, "
            "stream=false, max_tokens=12000, temperature=0.1.",
        )

    def test_json_object_error_includes_system_preview(self):
        message = _build_fallback_error_message(
            "Request failed with status code 400",
            {
                "model": "qwc.coder-model",
                "messages": [
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "text",
                                "text": "Ответ должен быть строго в JSON. Используй поле texts и не добавляй markdown.",
                            }
                        ],
                    },
                    {"role": "user", "content": "hi"},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 4000,
                "temperature": 0.1,
            },
            is_streaming=False,
        )

        self.assertEqual(
            message,
            'Request failed with status code 400. Request summary: '
            'model=qwc.coder-model, response_format.type=json_object, '
            'system_preview="Ответ должен быть строго в JSON. Используй поле texts и не добавляй markdown.", '
            'stream=false, max_tokens=4000, temperature=0.1.',
        )


class FallbackEventsDBTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = FallbackEventsDB.__new__(FallbackEventsDB)
        self.db.db_path = os.path.join(self.temp_dir, "test.db")
        self.db._batcher = None
        self.db._init_db()

    def test_insert_and_retrieve_events(self):
        self.db.insert_event(
            request_id="req-1",
            gateway_model="llmgateway/light_model",
            attempt_number=1,
            provider="devbox",
            model="glm-5-turbo",
            success=False,
            error_type="http_429",
            error_message="Rate limited",
            duration_ms=1200,
            x_title="tgBot",
        )
        self.db.insert_event(
            request_id="req-1",
            gateway_model="llmgateway/light_model",
            attempt_number=2,
            provider="nvidia",
            model="gpt-oss-120b",
            success=True,
            error_type=None,
            error_message=None,
            duration_ms=2000,
        )

        records, total = run_async(self.db.get_fallback_records(limit=25, offset=0))
        self.assertEqual(total, 1)  # 1 request with 2 attempts
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["request_id"], "req-1")
        self.assertEqual(records[0]["total_attempts"], 2)
        self.assertTrue(records[0]["success"])
        self.assertEqual(records[0]["final_provider"], "nvidia")
        self.assertEqual(records[0]["x_title"], "tgBot")
        self.assertEqual(len(records[0]["attempts"]), 2)

    def test_init_db_adds_x_title_column_for_existing_table(self):
        db = FallbackEventsDB.__new__(FallbackEventsDB)
        db.db_path = os.path.join(self.temp_dir, "legacy.db")
        db._batcher = None

        conn = sqlite3.connect(db.db_path)
        conn.execute(
            """
            CREATE TABLE fallback_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                request_id TEXT NOT NULL,
                gateway_model TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 0,
                error_type TEXT,
                error_message TEXT,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                operation TEXT,
                api_key_id INTEGER,
                upstream_key_fingerprint TEXT
            )
            """
        )
        conn.commit()
        conn.close()

        db._init_db()

        conn = sqlite3.connect(db.db_path)
        cursor = conn.execute("PRAGMA table_info(fallback_events)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        self.assertIn("x_title", columns)

    def test_single_attempt_not_returned(self):
        """Requests with only 1 attempt should not appear in fallback records."""
        self.db.insert_event(
            request_id="req-solo",
            gateway_model="model",
            attempt_number=1,
            provider="provider",
            model="model",
            success=True,
            error_type=None,
            error_message=None,
            duration_ms=100,
        )

        records, total = run_async(self.db.get_fallback_records(limit=25, offset=0))
        self.assertEqual(total, 0)
        self.assertEqual(len(records), 0)

    def test_aggregated_stats_only_failures(self):
        self.db.insert_event(
            request_id="req-2",
            gateway_model="model",
            attempt_number=1,
            provider="p1",
            model="m1",
            success=False,
            error_type="http_429",
            error_message="Rate limited",
            duration_ms=1000,
        )
        self.db.insert_event(
            request_id="req-2",
            gateway_model="model",
            attempt_number=2,
            provider="p2",
            model="m2",
            success=True,
            error_type=None,
            error_message=None,
            duration_ms=500,
        )

        stats = run_async(self.db.get_aggregated_stats("day"))
        # Only failures should be returned
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["error_type"], "http_429")
        self.assertEqual(stats[0]["count"], 1)

    def test_aggregated_stats_do_not_split_existing_summary_by_upstream_key(self):
        for request_id, key_fingerprint in [("req-key-1", "key-a"), ("req-key-2", "key-b")]:
            self.db.insert_event(
                request_id=request_id,
                gateway_model="model",
                attempt_number=1,
                provider="p1",
                model="m1",
                success=False,
                error_type="http_429",
                error_message="Rate limited",
                duration_ms=1000,
                upstream_key_fingerprint=key_fingerprint,
            )

        stats = run_async(self.db.get_aggregated_stats("day"))

        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["provider"], "p1")
        self.assertEqual(stats[0]["model"], "m1")
        self.assertEqual(stats[0]["error_type"], "http_429")
        self.assertEqual(stats[0]["count"], 2)

    def test_upstream_stats_group_by_key_and_use_ui_expected_aliases(self):
        self.db.insert_event(
            request_id="req-upstream-1",
            gateway_model="model",
            attempt_number=1,
            provider="p1",
            model="m1",
            success=False,
            error_type="http_429",
            error_message="Rate limited",
            duration_ms=1000,
            operation="chat",
            api_key_id=7,
            upstream_key_fingerprint="key-a",
        )
        self.db.insert_event(
            request_id="req-upstream-2",
            gateway_model="model",
            attempt_number=1,
            provider="p1",
            model="m1",
            success=True,
            error_type=None,
            error_message=None,
            duration_ms=500,
            operation="chat",
            api_key_id=7,
            upstream_key_fingerprint="key-a",
        )
        self.db.insert_event(
            request_id="req-upstream-3",
            gateway_model="model",
            attempt_number=1,
            provider="p1",
            model="m1",
            success=False,
            error_type="http_429",
            error_message="Rate limited",
            duration_ms=700,
            operation="chat",
            api_key_id=8,
            upstream_key_fingerprint="key-b",
        )

        stats = run_async(self.db.get_upstream_stats("day", api_key_id=7))

        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["upstream_key_fingerprint"], "key-a")
        self.assertEqual(stats[0]["attempts"], 2)
        self.assertEqual(stats[0]["successes"], 1)
        self.assertEqual(stats[0]["errors"], 1)
        self.assertEqual(stats[0]["success_rate"], 50.0)

    def test_dashboard_fallback_filters_by_virtual_key_and_upstream_key(self):
        self.db.insert_event(
            request_id="req-dashboard-1",
            gateway_model="gateway-fast",
            attempt_number=1,
            provider="openrouter",
            model="qwen",
            success=False,
            error_type="http_429",
            error_message="Rate limited",
            duration_ms=1000,
            operation="chat",
            api_key_id=7,
            upstream_key_fingerprint="fp-1",
        )
        self.db.insert_event(
            request_id="req-dashboard-1",
            gateway_model="gateway-fast",
            attempt_number=2,
            provider="openrouter",
            model="qwen",
            success=True,
            error_type=None,
            error_message=None,
            duration_ms=500,
            operation="chat",
            api_key_id=7,
            upstream_key_fingerprint="fp-1",
        )
        self.db.insert_event(
            request_id="req-dashboard-2",
            gateway_model="gateway-fast",
            attempt_number=1,
            provider="openrouter",
            model="qwen",
            success=False,
            error_type="http_500",
            error_message="Failed",
            duration_ms=700,
            operation="chat",
            api_key_id=8,
            upstream_key_fingerprint="fp-2",
        )

        data = run_async(
            self.db.get_dashboard_fallback(
                "day",
                datetime.now(timezone.utc) - timedelta(minutes=1),
                datetime.now(timezone.utc) + timedelta(minutes=1),
                api_key_id=7,
                operation="chat",
                provider="openrouter",
                model="qwen",
                upstream_key_fingerprint="fp-1",
            )
        )

        self.assertEqual(data["summary"]["attempts"], 2)
        self.assertEqual(data["summary"]["successes"], 1)
        self.assertEqual(data["summary"]["errors"], 1)
        self.assertEqual(data["summary"]["success_rate"], 50.0)
        self.assertEqual(data["error_types"], [{"label": "http_429", "errors": 1, "avg_duration_ms": 1000}])
        self.assertEqual(data["upstream"][0]["upstream_key_fingerprint"], "fp-1")

    def test_insert_event_timestamp_matches_utc_stats_window(self):
        self.db.insert_event(
            request_id="req-utc",
            gateway_model="model",
            attempt_number=1,
            provider="p1",
            model="m1",
            success=False,
            error_type="http_429",
            error_message="Rate limited",
            duration_ms=1000,
        )

        stats = run_async(
            self.db.get_aggregated_stats(
                "day",
                start_date=datetime.now(timezone.utc) - timedelta(minutes=1),
                end_date=datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        )

        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["error_type"], "http_429")

    def test_cleanup_old_records(self):
        self.db.insert_event(
            request_id="req-old",
            gateway_model="model",
            attempt_number=1,
            provider="p",
            model="m",
            success=False,
            error_type="unknown",
            error_message="err",
            duration_ms=100,
        )
        # Clean with 0 days retention — should delete everything
        self.db.cleanup_old_records(retention_days=0)

        records, total = run_async(self.db.get_fallback_records(limit=25, offset=0))
        self.assertEqual(total, 0)

    def test_error_message_truncation(self):
        long_message = "x" * 1000
        self.db.insert_event(
            request_id="req-trunc",
            gateway_model="model",
            attempt_number=1,
            provider="p",
            model="m",
            success=False,
            error_type="unknown",
            error_message=long_message,
            duration_ms=100,
        )
        # Add second attempt so it appears in records
        self.db.insert_event(
            request_id="req-trunc",
            gateway_model="model",
            attempt_number=2,
            provider="p",
            model="m",
            success=True,
            error_type=None,
            error_message=None,
            duration_ms=50,
        )

        records, _ = run_async(self.db.get_fallback_records(limit=25, offset=0))
        err_msg = records[0]["attempts"][0]["error_message"]
        self.assertLessEqual(len(err_msg), 500)


class ValidatePaginationTests(unittest.TestCase):
    def test_valid_params(self):
        self.assertEqual(validate_fallback_records_pagination(25, 0), (25, 0))

    def test_negative_limit_raises(self):
        with self.assertRaises(ValueError):
            validate_fallback_records_pagination(-1, 0)

    def test_negative_offset_raises(self):
        with self.assertRaises(ValueError):
            validate_fallback_records_pagination(25, -1)

    def test_exceeds_max_raises(self):
        with self.assertRaises(ValueError):
            validate_fallback_records_pagination(200, 0)


class _FakeFallbackEventsDB:
    def __init__(self):
        self.stats = [
            {
                "time_period": "2026-03-20",
                "gateway_model": "llmgateway/light_model",
                "provider": "devbox",
                "model": "glm-5-turbo",
                "error_type": "http_429",
                "count": 15,
                "avg_duration_ms": 1200,
            }
        ]
        self.records = [
            {
                "request_id": "req-1",
                "timestamp": "2026-03-20T13:21:18",
                "gateway_model": "llmgateway/light_model",
                "total_attempts": 3,
                "total_duration_ms": 103000,
                "success": True,
                "final_provider": "nvidia",
                "final_model": "gpt-oss-120b",
                "x_title": "tgBot",
                "attempts": [
                    {"attempt_number": 1, "provider": "devbox", "model": "glm-5-turbo", "success": False, "error_type": "http_429", "error_message": "Rate limited", "duration_ms": 1200},
                    {"attempt_number": 2, "provider": "nvidia", "model": "qwen", "success": False, "error_type": "read_timeout", "error_message": "ReadTimeout", "duration_ms": 100000},
                    {"attempt_number": 3, "provider": "nvidia", "model": "gpt-oss-120b", "success": True, "error_type": None, "error_message": None, "duration_ms": 1800},
                ],
            }
        ]

    async def get_aggregated_stats(self, period, start_date=None, end_date=None):
        return self.stats

    async def get_fallback_records(self, limit=25, offset=0):
        return self.records[offset:offset + limit], len(self.records)

    def cleanup_old_records(self, retention_days=180):
        pass


class _FakeTokensUsageDB:
    async def get_latest_usage_records(self, limit=25, offset=0):
        return []

    async def get_total_records_count(self):
        return 0

    async def get_aggregated_usage(self, period, start_date=None, end_date=None):
        return []

    def cleanup_old_records(self, retention_days=180):
        pass


class FallbackStatsApiTests(unittest.TestCase):
    @contextmanager
    def _client(self):
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        fake_config_loader = Mock()
        fake_config_loader.load_providers.return_value = {}
        fake_config_loader.load_fallback_rules.return_value = {}

        with ExitStack() as stack:
            stack.enter_context(patch("main.ConfigLoader", return_value=fake_config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
            stack.enter_context(patch("main.TokensUsageDB", return_value=_FakeTokensUsageDB()))
            stack.enter_context(patch("main.FallbackEventsDB", return_value=_FakeFallbackEventsDB()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-key"))

            with TestClient(main.app) as client:
                yield client

    def test_fallback_stats_returns_data(self):
        with self._client() as client:
            response = client.get(
                "/v1/api/fallback-stats/day",
                headers={"Authorization": "Bearer test-key"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["error_type"], "http_429")
        self.assertEqual(data[0]["count"], 15)

    def test_fallback_stats_rejects_invalid_period(self):
        with self._client() as client:
            response = client.get(
                "/v1/api/fallback-stats/invalid",
                headers={"Authorization": "Bearer test-key"},
            )
        self.assertEqual(response.status_code, 400)

    def test_fallback_records_returns_chains(self):
        with self._client() as client:
            response = client.get(
                "/v1/api/fallback-records",
                headers={"Authorization": "Bearer test-key"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_records"], 1)
        self.assertEqual(len(data["records"]), 1)
        self.assertEqual(data["records"][0]["total_attempts"], 3)
        self.assertEqual(data["records"][0]["x_title"], "tgBot")

    def test_fallback_records_rejects_invalid_pagination(self):
        with self._client() as client:
            response = client.get(
                "/v1/api/fallback-records?limit=-1",
                headers={"Authorization": "Bearer test-key"},
            )
        self.assertEqual(response.status_code, 400)


class FallbackEventsIntegrationTests(unittest.TestCase):
    """Test that fallback events are recorded during chat request processing."""

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_fallback_events_recorded_on_provider_failure(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        from types import SimpleNamespace

        fake_config_loader = Mock()
        fake_config_loader.providers_config = {
            "test-provider": SimpleNamespace(
                baseUrl="https://provider.example",
                apikey="DIRECT-KEY",
            ),
        }
        fake_config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {"provider": "test-provider", "model": "provider-model"},
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        # First call fails, model succeeds on next call
        make_llm_request_mock.return_value = ({"id": "success"}, None)

        fake_fallback_db = Mock()
        fake_fallback_db.insert_event = Mock()
        fake_fallback_db.cleanup_old_records = Mock()

        with patch("main.FallbackEventsDB", return_value=fake_fallback_db):
            with patch.object(main.settings, "gateway_api_key", "test-key"):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        json={"model": "gateway-model", "messages": [{"role": "user", "content": "hi"}]},
                        headers={"Authorization": "Bearer test-key", "X-Title": " tgBot "},
                    )

        self.assertEqual(response.status_code, 200)
        # Should have recorded exactly one event (success on first try)
        self.assertEqual(fake_fallback_db.insert_event.call_count, 1)
        call_kwargs = fake_fallback_db.insert_event.call_args
        self.assertTrue(call_kwargs.kwargs["success"])
        self.assertEqual(call_kwargs.kwargs["provider"], "test-provider")
        self.assertEqual(call_kwargs.kwargs["model"], "provider-model")
        self.assertEqual(call_kwargs.kwargs["attempt_number"], 1)
        self.assertEqual(call_kwargs.kwargs["x_title"], "tgBot")
        # request_id should be a UUID string
        self.assertIsNotNone(call_kwargs.kwargs["request_id"])
        self.assertGreater(len(call_kwargs.kwargs["request_id"]), 0)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_fallback_events_record_detailed_error_message_for_generic_400(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        from types import SimpleNamespace

        fake_config_loader = Mock()
        fake_config_loader.providers_config = {
            "test-provider": SimpleNamespace(
                baseUrl="https://provider.example",
                apikey="DIRECT-KEY",
            ),
        }
        fake_config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {"provider": "test-provider", "model": "provider-model-a"},
                    {"provider": "test-provider", "model": "provider-model-b"},
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.side_effect = [
            (None, "Request failed with status code 400"),
            ({"id": "success"}, None),
        ]

        fake_fallback_db = Mock()
        fake_fallback_db.insert_event = Mock()
        fake_fallback_db.cleanup_old_records = Mock()

        with patch("main.FallbackEventsDB", return_value=fake_fallback_db):
            with patch.object(main.settings, "gateway_api_key", "test-key"):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "gateway-model",
                            "messages": [
                                {
                                    "role": "system",
                                    "content": "Ответ должен быть строго в JSON. Используй поле texts.",
                                },
                                {"role": "user", "content": "hi"},
                            ],
                            "response_format": {"type": "json_object"},
                            "max_tokens": 4000,
                            "temperature": 0.1,
                        },
                        headers={"Authorization": "Bearer test-key"},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_fallback_db.insert_event.call_count, 2)

        first_call = fake_fallback_db.insert_event.call_args_list[0].kwargs
        self.assertFalse(first_call["success"])
        self.assertEqual(first_call["error_type"], "http_400")
        self.assertEqual(first_call["attempt_number"], 1)
        self.assertIn("Request failed with status code 400", first_call["error_message"])
        self.assertIn("model=provider-model-a", first_call["error_message"])
        self.assertIn("response_format.type=json_object", first_call["error_message"])
        self.assertIn('system_preview="Ответ должен быть строго в JSON. Используй поле texts."', first_call["error_message"])
        self.assertIn("stream=false", first_call["error_message"])
        self.assertIn("max_tokens=4000", first_call["error_message"])
        self.assertIn("temperature=0.1", first_call["error_message"])

        second_call = fake_fallback_db.insert_event.call_args_list[1].kwargs
        self.assertTrue(second_call["success"])
        self.assertEqual(second_call["attempt_number"], 2)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_fallback_events_record_detailed_error_message_for_timeout(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        from types import SimpleNamespace

        fake_config_loader = Mock()
        fake_config_loader.providers_config = {
            "test-provider": SimpleNamespace(
                baseUrl="https://provider.example",
                apikey="DIRECT-KEY",
            ),
        }
        fake_config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {"provider": "test-provider", "model": "provider-model-a"},
                    {"provider": "test-provider", "model": "provider-model-b"},
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.side_effect = [
            (None, "ReadTimeout connecting to https://provider.example/chat/completions"),
            ({"id": "success"}, None),
        ]

        fake_fallback_db = Mock()
        fake_fallback_db.insert_event = Mock()
        fake_fallback_db.cleanup_old_records = Mock()

        with patch("main.FallbackEventsDB", return_value=fake_fallback_db):
            with patch.object(main.settings, "gateway_api_key", "test-key"):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "gateway-model",
                            "messages": [{"role": "user", "content": "hi"}],
                            "response_format": {"type": "json_object"},
                            "max_tokens": 12000,
                            "temperature": 0.1,
                        },
                        headers={"Authorization": "Bearer test-key"},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_fallback_db.insert_event.call_count, 2)

        first_call = fake_fallback_db.insert_event.call_args_list[0].kwargs
        self.assertFalse(first_call["success"])
        self.assertEqual(first_call["error_type"], "read_timeout")
        self.assertIn("ReadTimeout connecting to https://provider.example/chat/completions", first_call["error_message"])
        self.assertIn("model=provider-model-a", first_call["error_message"])
        self.assertIn("response_format.type=json_object", first_call["error_message"])
        self.assertIn("stream=false", first_call["error_message"])
        self.assertIn("max_tokens=12000", first_call["error_message"])


if __name__ == "__main__":
    unittest.main()
