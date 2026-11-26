import gzip
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from llm_gateway_core.api.v1 import chat
from llm_gateway_core.middleware import chat_logging
from llm_gateway_core.utils.gzip_utils import (
    GzipPayloadTooLarge,
    decompress_gzip_bounded,
)


class GzipDecompressionLimitTests(unittest.TestCase):
    def test_decompress_gzip_bounded_accepts_payload_within_limit(self):
        raw = b'{"model":"demo","messages":[{"role":"user","content":"hi"}]}'
        compressed = gzip.compress(raw)

        self.assertEqual(decompress_gzip_bounded(compressed, len(raw)), raw)

    def test_decompress_gzip_bounded_rejects_payload_over_limit(self):
        raw = b"A" * 2048
        compressed = gzip.compress(raw)

        with self.assertRaises(GzipPayloadTooLarge):
            decompress_gzip_bounded(compressed, 512)

    def test_chat_read_json_request_body_returns_413_on_gzip_bomb(self):
        payload = json.dumps({"model": "demo", "data": "x" * 2048}).encode("utf-8")
        compressed = gzip.compress(payload)
        request = SimpleNamespace(headers={"content-encoding": "gzip"})

        with patch.object(chat.settings, "max_request_body_bytes", 256):
            with self.assertRaises(HTTPException) as ctx:
                chat._read_json_request_body(compressed, "/v1/chat/completions", request)

        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(ctx.exception.detail, "Decompressed request body too large")

    def test_chat_read_json_request_body_accepts_valid_gzip_within_limit(self):
        payload = {"model": "demo", "stream": False}
        compressed = gzip.compress(json.dumps(payload).encode("utf-8"))
        request = SimpleNamespace(headers={"content-encoding": "gzip"})

        with patch.object(chat.settings, "max_request_body_bytes", 1024):
            parsed = chat._read_json_request_body(compressed, "/v1/chat/completions", request)

        self.assertEqual(parsed, payload)

    def test_chat_logging_decode_body_returns_empty_and_warns_on_gzip_bomb(self):
        compressed = gzip.compress(b"Z" * 2048)

        with (
            patch.object(chat_logging.settings, "max_request_body_bytes", 256),
            self.assertLogs(chat_logging.logger, level="WARNING") as logs,
        ):
            decoded = chat_logging._decode_body(compressed, {"content-encoding": "gzip"})

        self.assertEqual(decoded, "")
        self.assertTrue(any("exceeds limit" in entry for entry in logs.output))

    def test_chat_logging_decode_body_decodes_valid_gzip(self):
        raw = '{"model":"demo"}'
        compressed = gzip.compress(raw.encode("utf-8"))

        with patch.object(chat_logging.settings, "max_request_body_bytes", 1024):
            decoded = chat_logging._decode_body(compressed, {"content-encoding": "gzip"})

        self.assertEqual(decoded, raw)


if __name__ == "__main__":
    unittest.main()
