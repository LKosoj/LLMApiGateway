import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from llm_gateway_core.db import tokens_usage_db as tokens_usage_db_module
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.middleware import chat_logging


class ChatLoggingTailExceptionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self._root = Path(self._tmp.name)
        os.makedirs(self._root / "db", exist_ok=True)
        path_patch = patch.object(
            tokens_usage_db_module,
            "__file__",
            str(self._root / "llm_gateway_core" / "db" / "tokens_usage_db.py"),
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)

        self._original_tokens_usage_db = chat_logging.state.tokens_usage_db
        self._original_api_keys_db = chat_logging.state.api_keys_db
        self._original_rate_limiter = chat_logging.state.rate_limiter
        self._original_usd_budget_ledger = chat_logging.state.usd_budget_ledger

        self.tokens_db = TokensUsageDB(db_filename="test_tail_exception.db")
        chat_logging.set_tokens_usage_db(self.tokens_db)
        chat_logging.set_api_keys_db(None)
        chat_logging.set_rate_limiter(None)
        chat_logging.set_usd_budget_ledger(None)

    def tearDown(self):
        chat_logging.set_tokens_usage_db(self._original_tokens_usage_db)
        chat_logging.set_api_keys_db(self._original_api_keys_db)
        chat_logging.set_rate_limiter(self._original_rate_limiter)
        chat_logging.set_usd_budget_ledger(self._original_usd_budget_ledger)

    def test_tail_process_exception_still_writes_usage_row_and_does_not_break_client(self):
        app = FastAPI()
        app.middleware("http")(chat_logging.log_chat_completions)

        @app.post("/v1/chat/completions")
        async def completions(request: Request):
            request.state.llmgateway_provider = "provider-a"
            request.state.llmgateway_provider_model = "provider-model-a"

            async def body():
                yield b'data: {"choices":[{"delta":{"content":"hello"}}]}'

            return StreamingResponse(body(), media_type="text/event-stream")

        def raise_process_decoded_parts(self, parts):
            raise RuntimeError("tail decode failed")

        with patch.object(chat_logging.settings, "log_chat_messages", False):
            with patch.object(
                chat_logging.ChunkProcessor,
                "_process_decoded_parts",
                raise_process_decoded_parts,
            ):
                with self.assertLogs(chat_logging.logger, level="ERROR") as logs:
                    with TestClient(app) as client:
                        with client.stream(
                            "POST",
                            "/v1/chat/completions",
                            json={"model": "gateway-model", "messages": [{"role": "user", "content": "hi"}]},
                        ) as response:
                            body = b"".join(response.iter_bytes())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, b'data: {"choices":[{"delta":{"content":"hello"}}]}')
        self.assertTrue(
            any("ChatLogging: error processing trailing stream chunk parts" in entry for entry in logs.output)
        )

        with sqlite3.connect(self.tokens_db.db_path) as conn:
            conn.row_factory = sqlite3.Row
            records = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM tokens_usage ORDER BY timestamp DESC LIMIT 5"
                ).fetchall()
            ]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["gateway_model"], "gateway-model")
        self.assertEqual(record["operation"], "chat")
        self.assertEqual(record["provider"], "provider-a")
        self.assertEqual(record["model"], "provider-model-a")
        self.assertIsNotNone(record["duration_ms"])


if __name__ == "__main__":
    unittest.main()
