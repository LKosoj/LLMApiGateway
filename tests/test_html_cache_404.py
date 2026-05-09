import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from llm_gateway_core.utils import html_cache
from tests._async_compat import run_async


class HtmlCacheNotFoundTests(unittest.TestCase):
    def setUp(self):
        html_cache.clear_cache()

    def tearDown(self):
        html_cache.clear_cache()

    def test_missing_template_returns_404(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "missing.html"

            with self.assertRaises(HTTPException) as context:
                run_async(html_cache.get_template(missing_path))

        self.assertEqual(context.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
