import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from llm_gateway_core.db import tokens_usage_db as tokens_usage_db_module
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.middleware import chat_logging
from llm_gateway_core.middleware.response_observation import (
    ResponseObservationMiddleware,
)
from llm_gateway_core.middleware.runtime_snapshot import RuntimeSnapshotMiddleware
from tests.chat_accounting_test_support import install_legacy_chat_logging_passthrough
from tests.runtime_test_support import installed_runtime


class ChatLoggingTailExceptionTests(unittest.TestCase):
    def setUp(self):
        self._accounting_stack = ExitStack()
        self.addCleanup(self._accounting_stack.close)
        install_legacy_chat_logging_passthrough(self._accounting_stack)
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

        self.tokens_db = TokensUsageDB(db_filename="test_tail_exception.db")

    def test_tail_process_exception_still_records_observability_and_does_not_break_client(self):
        app = FastAPI()

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            async with installed_runtime(_app, tokens_usage_db=self.tokens_db):
                yield

        app.router.lifespan_context = lifespan
        app.add_middleware(
            ResponseObservationMiddleware,
            request_preparer=chat_logging.prepare_chat_response_observation,
        )
        app.add_middleware(RuntimeSnapshotMiddleware)

        @app.post("/v1/chat/completions")
        async def completions(request: Request):
            await request.body()
            request.state.llmgateway_provider = "provider-a"
            request.state.llmgateway_provider_model = "provider-model-a"

            async def body():
                yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                yield b"data: [DONE]"

            return StreamingResponse(body(), media_type="text/event-stream")

        process_decoded_parts = chat_logging.ChunkProcessor._process_decoded_parts

        def raise_process_decoded_parts(self, parts, *, canonical_events=False):
            decoded_parts = tuple(parts)
            if decoded_parts == ("[DONE]",):
                raise RuntimeError("tail decode failed")
            return process_decoded_parts(
                self,
                decoded_parts,
                canonical_events=canonical_events,
            )

        with patch.object(chat_logging.settings, "log_chat_messages", False):
            with patch.object(
                chat_logging.ChunkProcessor,
                "_process_decoded_parts",
                raise_process_decoded_parts,
            ):
                with patch.object(
                    chat_logging,
                    "record_chat_observability",
                    wraps=chat_logging.record_chat_observability,
                ) as observability_mock:
                    with self.assertLogs(chat_logging.logger, level="ERROR") as logs:
                        with TestClient(app) as client:
                            with client.stream(
                                "POST",
                                "/v1/chat/completions",
                                json={"model": "gateway-model", "messages": [{"role": "user", "content": "hi"}]},
                            ) as response:
                                body = b"".join(response.iter_bytes())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            body,
            b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            b"data: [DONE]",
        )
        self.assertTrue(
            any(
                "Stream observation task failed type=RuntimeError" in entry
                for entry in logs.output
            )
        )
        self.assertFalse(any("tail decode failed" in entry for entry in logs.output))
        self.assertFalse(any("hello" in entry for entry in logs.output))
        observability_mock.assert_called_once()

        with sqlite3.connect(self.tokens_db.db_path) as conn:
            conn.row_factory = sqlite3.Row
            records = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM tokens_usage ORDER BY timestamp DESC LIMIT 5"
                ).fetchall()
            ]
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
