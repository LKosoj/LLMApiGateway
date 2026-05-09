"""HTML template cache — preload once at startup, serve from memory.

Eliminates per-request disk reads for the static UI pages.
"""
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from llm_gateway_core.utils import html_cache
from tests._async_compat import run_async


def get_template(path: Path) -> str:
    return run_async(html_cache.get_template(path))


class HtmlCacheTests(unittest.TestCase):
    def setUp(self):
        html_cache.clear_cache()

    def tearDown(self):
        html_cache.clear_cache()

    def test_preload_then_get_returns_cached_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "page.html"
            template_path.write_text("<h1>hello</h1>", encoding="utf-8")

            html_cache.preload_templates([template_path])

            # Mutate the file on disk — the cached copy must not change.
            template_path.write_text("<h1>changed</h1>", encoding="utf-8")
            self.assertEqual(get_template(template_path), "<h1>hello</h1>")

    def test_preload_skips_missing_file_without_raising(self):
        missing_path = Path("/nonexistent-directory-for-test") / "does-not-exist.html"
        html_cache.preload_templates([missing_path])  # Must not raise
        with self.assertRaises(HTTPException) as context:
            get_template(missing_path)
        self.assertEqual(context.exception.status_code, 404)

    def test_get_template_reads_on_demand_when_not_preloaded(self):
        # Tests and scripts that bypass lifespan can still use get_template;
        # the on-demand path caches after the first read.
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "page.html"
            template_path.write_text("<p>lazy</p>", encoding="utf-8")

            result_first = get_template(template_path)
            self.assertEqual(result_first, "<p>lazy</p>")

            # Change on disk — cached result should remain the original.
            template_path.write_text("<p>changed</p>", encoding="utf-8")
            result_second = get_template(template_path)
            self.assertEqual(result_second, "<p>lazy</p>")

    def test_clear_cache_drops_all_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "page.html"
            template_path.write_text("<x/>", encoding="utf-8")

            html_cache.preload_templates([template_path])
            html_cache.clear_cache()

            template_path.write_text("<y/>", encoding="utf-8")
            self.assertEqual(get_template(template_path), "<y/>")


if __name__ == "__main__":
    unittest.main()
