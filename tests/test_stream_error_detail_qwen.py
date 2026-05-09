import unittest

from llm_gateway_core.services.request_handler import _extract_stream_error_detail


class QwenStreamErrorDetailTests(unittest.TestCase):
    def test_code_zero_success_chunks_are_not_errors(self):
        vectors = (
            ({"code": 0, "message": "success"}, None),
            ({"code": 429, "message": "rate limit"}, "rate limit"),
            ({"code": 0}, None),
            ({"code": "0", "message": " Success "}, None),
        )

        for chunk_json, expected in vectors:
            with self.subTest(chunk_json=chunk_json):
                self.assertEqual(_extract_stream_error_detail(chunk_json), expected)


if __name__ == "__main__":
    unittest.main()
