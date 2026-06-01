import asyncio
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_gateway_core.agents import deep_research as deep_research_module
from llm_gateway_core.agents.deep_research import (
    DeepResearchManager,
    GatewayImageGenerator,
    _image_filename,
)
from tests._async_compat import run_async


class _FakeImageGenerator:
    def __init__(self, researcher):
        self.researcher = researcher
        self.image_provider = None

    def is_enabled(self):
        return self.image_provider is not None and self.image_provider.is_available()

    async def plan_and_generate_images(self, *, context: str, query: str, research_id: str):
        _FakeResearcher.image_generation_call = {
            "context": context,
            "query": query,
            "research_id": research_id,
        }
        return list(_FakeResearcher.planned_images)


class _FakeResearcher:
    env_snapshot = {}
    image_provider = None
    image_generation_call = None
    report_images = None
    planned_images = [
        {
            "url": "/outputs/images/research-id/image.png",
            "prompt": "visual prompt",
            "alt_text": "Illustration",
        }
    ]
    sources = [{"title": "source"}]
    source_urls = ["https://example.com"]
    context = ["context"]
    costs = 0.01

    def __init__(self, *, query: str, report_type: str, verbose: bool) -> None:
        self.query = query
        self.report_type = report_type
        self.verbose = verbose
        self.context = ["context"]
        self.available_images = []
        _FakeResearcher.image_provider = None
        _FakeResearcher.image_generation_call = None
        _FakeResearcher.report_images = None
        _FakeResearcher.env_snapshot = {
            key: os.environ.get(key)
            for key in (
                "FAST_LLM",
                "SMART_LLM",
                "STRATEGIC_LLM",
                "EMBEDDING",
                "DEEP_RESEARCH_CONCURRENCY",
                "LANGUAGE",
                "IMAGE_GENERATION_ENABLED",
                "IMAGE_GENERATION_MODEL",
                "IMAGE_GENERATION_SIZE",
                "IMAGE_GENERATION_PROVIDER",
                "IMAGE_GENERATION_API_KEY",
            )
        }
        self.image_generator = _FakeImageGenerator(self)

    def _generate_research_id(self):
        return "research-id"

    async def conduct_research(self):
        _FakeResearcher.image_provider = self.image_generator.image_provider
        return {"status": "ok"}

    async def write_report(self):
        _FakeResearcher.report_images = list(self.available_images)
        return "report"


class _FakeDeepResearchManager(DeepResearchManager):
    def _get_researcher_factory(self):
        return _FakeResearcher


class _FakeGatewayToolResearcher:
    sources = []
    source_urls = []
    context = []
    costs = 0.02

    def __init__(self, *, query: str, report_type: str, verbose: bool) -> None:
        self.query = query
        self.report_type = report_type
        self.verbose = verbose
        self.research_sources = []

    def add_research_sources(self, sources):
        self.research_sources.extend(sources)

    async def conduct_research(self):
        import gpt_researcher.agent as agent_module

        async_results = await agent_module.get_search_results(
            self.query,
            None,
            researcher=types.SimpleNamespace(
                cfg=types.SimpleNamespace(max_search_results_per_query=3),
            ),
        )
        retriever_cls = agent_module.get_retrievers({}, object())[0]
        sync_results = await asyncio.to_thread(
            retriever_cls(f"{self.query} sync").search,
            max_results=2,
        )
        search_results = async_results + sync_results
        urls = [item["href"] for item in search_results]
        browser_manager = agent_module.BrowserManager(self)
        pages = await browser_manager.browse_urls(urls)
        self.sources = pages
        self.source_urls = urls
        self.context = pages
        return pages

    async def write_report(self):
        return "gateway report"


class _FakeGatewayToolDeepResearchManager(DeepResearchManager):
    def _get_researcher_factory(self):
        return _FakeGatewayToolResearcher


class DeepResearchManagerTests(unittest.TestCase):
    def test_image_generation_env_is_configured_for_single_call(self):
        old_enabled = os.environ.get("IMAGE_GENERATION_ENABLED")
        os.environ["IMAGE_GENERATION_ENABLED"] = "outside"
        try:
            result = run_async(
                _FakeDeepResearchManager().conduct_deep_research(
                    query="topic",
                    fast_model="llmgateway/light_model",
                    smart_model="llmgateway/light_model",
                    strategic_model="llmgateway/light_model",
                    embedding_model="llmgateway/embedding",
                    gateway_base_url="http://127.0.0.1:9000/v1",
                    gateway_api_key="test-gateway-key",
                    image_generation_api_key="virtual-key",
                    language="Russian",
                    image_generation_enabled=True,
                    image_generation_model="llmgateway/image-gen",
                    image_generation_size="1024x1024",
                )
            )
        finally:
            if old_enabled is None:
                os.environ.pop("IMAGE_GENERATION_ENABLED", None)
            else:
                os.environ["IMAGE_GENERATION_ENABLED"] = old_enabled

        self.assertEqual(result["report"], "report")
        self.assertEqual(_FakeResearcher.env_snapshot["FAST_LLM"], "openai:llmgateway/light_model")
        self.assertEqual(_FakeResearcher.env_snapshot["EMBEDDING"], "custom:llmgateway/embedding")
        self.assertEqual(_FakeResearcher.env_snapshot["DEEP_RESEARCH_CONCURRENCY"], "6")
        self.assertEqual(_FakeResearcher.env_snapshot["LANGUAGE"], "Russian")
        self.assertEqual(_FakeResearcher.env_snapshot["IMAGE_GENERATION_ENABLED"], "true")
        self.assertEqual(
            _FakeResearcher.env_snapshot["IMAGE_GENERATION_MODEL"],
            "llmgateway/image-gen",
        )
        self.assertEqual(_FakeResearcher.env_snapshot["IMAGE_GENERATION_SIZE"], "1024x1024")
        self.assertEqual(_FakeResearcher.env_snapshot["IMAGE_GENERATION_PROVIDER"], "gateway")
        self.assertEqual(_FakeResearcher.env_snapshot["IMAGE_GENERATION_API_KEY"], "virtual-key")
        self.assertIsInstance(_FakeResearcher.image_provider, GatewayImageGenerator)
        self.assertEqual(
            _FakeResearcher.image_generation_call,
            {
                "context": "context",
                "query": "topic",
                "research_id": "research-id",
            },
        )
        self.assertEqual(_FakeResearcher.report_images, _FakeResearcher.planned_images)
        self.assertEqual(result["generated_images"], _FakeResearcher.planned_images)
        self.assertEqual(os.environ.get("IMAGE_GENERATION_ENABLED"), old_enabled)

    def test_image_generation_uses_required_prompt_when_planner_returns_no_images(self):
        required_images = [
            {
                "url": "/outputs/images/research-id/required.png",
                "prompt": "required visual prompt",
                "alt_text": "Illustration",
            }
        ]
        generate_calls = []
        old_planned_images = _FakeResearcher.planned_images
        _FakeResearcher.planned_images = []
        try:
            async def fake_generate_image(_provider, **kwargs):
                generate_calls.append(kwargs)
                return required_images

            with patch.object(GatewayImageGenerator, "generate_image", fake_generate_image):
                result = run_async(
                    _FakeDeepResearchManager().conduct_deep_research(
                        query="topic",
                        fast_model="llmgateway/light_model",
                        smart_model="llmgateway/light_model",
                        strategic_model="llmgateway/light_model",
                        gateway_base_url="http://127.0.0.1:9000/v1",
                        gateway_api_key="test-gateway-key",
                        image_generation_enabled=True,
                        image_generation_model="llmgateway/image-gen",
                        image_generation_size="1024x1024",
                    )
                )
        finally:
            _FakeResearcher.planned_images = old_planned_images

        self.assertEqual(result["generated_images"], required_images)
        self.assertEqual(_FakeResearcher.report_images, required_images)
        self.assertEqual(generate_calls[0]["research_id"], "research-id")
        self.assertEqual(generate_calls[0]["num_images"], 1)
        self.assertIn("topic", generate_calls[0]["prompt"])
        self.assertIn("context", generate_calls[0]["prompt"])

    def test_image_generation_enabled_requires_provider_images(self):
        old_planned_images = _FakeResearcher.planned_images
        _FakeResearcher.planned_images = []
        try:
            async def fake_generate_image(_provider, **_kwargs):
                return []

            with (
                patch.object(GatewayImageGenerator, "generate_image", fake_generate_image),
                self.assertRaisesRegex(ValueError, "provider returned no images"),
            ):
                run_async(
                    _FakeDeepResearchManager().conduct_deep_research(
                        query="topic",
                        fast_model="llmgateway/light_model",
                        smart_model="llmgateway/light_model",
                        strategic_model="llmgateway/light_model",
                        gateway_base_url="http://127.0.0.1:9000/v1",
                        gateway_api_key="test-gateway-key",
                        image_generation_enabled=True,
                        image_generation_model="llmgateway/image-gen",
                        image_generation_size="1024x1024",
                    )
                )
        finally:
            _FakeResearcher.planned_images = old_planned_images

    def test_gateway_search_and_read_are_installed_as_gpt_researcher_tools(self):
        gpt_researcher_module = types.ModuleType("gpt_researcher")
        agent_module = types.ModuleType("gpt_researcher.agent")
        actions_module = types.ModuleType("gpt_researcher.actions")
        query_processing_module = types.ModuleType("gpt_researcher.actions.query_processing")
        skills_module = types.ModuleType("gpt_researcher.skills")
        deep_research_module = types.ModuleType("gpt_researcher.skills.deep_research")
        researcher_module = types.ModuleType("gpt_researcher.skills.researcher")

        def original_get_retrievers(_headers, _cfg):
            return []

        async def original_get_search_results(_query, _retriever, query_domains=None, researcher=None):
            return []

        class OriginalBrowserManager:
            pass

        agent_module.get_retrievers = original_get_retrievers
        agent_module.BrowserManager = OriginalBrowserManager
        agent_module.get_search_results = original_get_search_results
        actions_module.get_search_results = original_get_search_results
        query_processing_module.get_search_results = original_get_search_results
        deep_research_module.get_search_results = original_get_search_results
        researcher_module.get_search_results = original_get_search_results
        original_browser_manager = agent_module.BrowserManager
        search_calls = []
        read_calls = []
        callback_loops = []

        async def gateway_search(query: str, max_results: int):
            callback_loops.append(asyncio.get_running_loop())
            search_calls.append((query, max_results))
            return [
                {
                    "url": "https://example.com/article",
                    "title": "Article",
                    "snippet": "Short snippet",
                }
            ]

        async def gateway_read(url: str):
            callback_loops.append(asyncio.get_running_loop())
            read_calls.append(url)
            return {
                "url": url,
                "title": "Article",
                "content": "Downloaded article content",
            }

        with patch.dict(
            sys.modules,
            {
                "gpt_researcher": gpt_researcher_module,
                "gpt_researcher.agent": agent_module,
                "gpt_researcher.actions": actions_module,
                "gpt_researcher.actions.query_processing": query_processing_module,
                "gpt_researcher.skills": skills_module,
                "gpt_researcher.skills.deep_research": deep_research_module,
                "gpt_researcher.skills.researcher": researcher_module,
            },
        ):
            async def _run_in_worker():
                callback_loop = asyncio.get_running_loop()
                result = await asyncio.to_thread(
                    lambda: asyncio.run(
                        _FakeGatewayToolDeepResearchManager().conduct_deep_research(
                            query="topic",
                            fast_model="llmgateway/light_model",
                            smart_model="llmgateway/light_model",
                            strategic_model="llmgateway/light_model",
                            gateway_search=gateway_search,
                            gateway_read=gateway_read,
                            gateway_callback_loop=callback_loop,
                        )
                    )
                )
                return result, callback_loop

            result, callback_loop = run_async(_run_in_worker())

        self.assertEqual(result["report"], "gateway report")
        self.assertEqual(search_calls, [("topic", 3), ("topic sync", 2)])
        self.assertEqual(read_calls, ["https://example.com/article", "https://example.com/article"])
        self.assertTrue(callback_loops)
        self.assertTrue(all(loop is callback_loop for loop in callback_loops))
        self.assertEqual(result["sources"][0]["raw_content"], "Downloaded article content")
        self.assertIs(agent_module.get_retrievers, original_get_retrievers)
        self.assertIs(agent_module.BrowserManager, original_browser_manager)
        self.assertIs(agent_module.get_search_results, original_get_search_results)
        self.assertIs(actions_module.get_search_results, original_get_search_results)
        self.assertIs(query_processing_module.get_search_results, original_get_search_results)
        self.assertIs(deep_research_module.get_search_results, original_get_search_results)
        self.assertIs(researcher_module.get_search_results, original_get_search_results)

    def test_research_conductor_search_binding_uses_gateway_callback_without_blocking_event_loop(self):
        import gpt_researcher.agent as agent_module
        import gpt_researcher.skills.researcher as researcher_module

        original_get_search_results = researcher_module.get_search_results
        search_calls = []

        async def gateway_search(query: str, max_results: int):
            search_calls.append((query, max_results))
            await asyncio.sleep(0)
            return [
                {
                    "url": "https://example.com/article",
                    "title": "Article",
                    "snippet": "Short snippet",
                }
            ]

        async def gateway_read(_url: str):
            return {}

        async def _run():
            with deep_research_module._gateway_research_tools(gateway_search, gateway_read):
                retriever_cls = agent_module.get_retrievers({}, object())[0]
                return await asyncio.wait_for(
                    researcher_module.get_search_results(
                        "topic",
                        retriever_cls,
                        researcher=types.SimpleNamespace(
                            cfg=types.SimpleNamespace(max_search_results_per_query=7),
                        ),
                    ),
                    timeout=1.0,
                )

        result = run_async(_run())

        self.assertEqual(search_calls, [("topic", 7)])
        self.assertEqual(result[0]["href"], "https://example.com/article")
        self.assertIs(researcher_module.get_search_results, original_get_search_results)


class ImageFilenameTests(unittest.TestCase):
    def test_filename_shape_matches_sha1_prefix_and_index(self):
        pattern = re.compile(r"^image_[0-9a-f]{8}_\d+\.png$")
        self.assertRegex(_image_filename("hello world", 0), pattern)
        self.assertRegex(_image_filename("hello world", 7), pattern)

    def test_filename_is_deterministic_for_same_prompt_and_index(self):
        self.assertEqual(
            _image_filename("hello world", 0),
            _image_filename("hello world", 0),
        )
        self.assertNotEqual(
            _image_filename("hello world", 0),
            _image_filename("hello world", 1),
        )
        self.assertNotEqual(
            _image_filename("hello world", 0),
            _image_filename("different prompt", 0),
        )


class GatewayImageGeneratorTests(unittest.TestCase):
    def test_generate_image_writes_png_and_populates_accumulator(self):
        from base64 import b64encode

        pixel_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 16
        encoded = b64encode(pixel_bytes).decode("ascii")
        captured_request = {}

        class _FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"b64_json": encoded}]}

        class _FakeClient:
            async def post(self, *args, **kwargs):
                captured_request["json"] = kwargs["json"]
                return _FakeResponse()

        with tempfile.TemporaryDirectory() as tmp:
            generator = GatewayImageGenerator(output_dir=tmp, http_client=_FakeClient())
            # is_available() depends on env; feed minimal identity:
            generator.model_name = "gw/image"
            generator.api_key = "k"
            generator.base_url = "http://localhost:0/v1"

            accumulator: list[dict] = []

            async def _run():
                token = deep_research_module._CURRENT_IMAGE_ACCUMULATOR.set(accumulator)
                try:
                    return await generator.generate_image(prompt="hello world", research_id="r1")
                finally:
                    deep_research_module._CURRENT_IMAGE_ACCUMULATOR.reset(token)

            result = run_async(_run())

            self.assertEqual(len(result), 1)
            self.assertEqual(result, accumulator, "accumulator should mirror returned entries")
            written = Path(result[0]["path"])
            self.assertTrue(written.exists())
            self.assertEqual(written.read_bytes(), pixel_bytes)
            self.assertNotIn("n", captured_request["json"])
            self.assertTrue(result[0]["url"].startswith("/outputs/images/r1/"))
            self.assertRegex(Path(result[0]["url"]).name, r"^image_[0-9a-f]{8}_0\.png$")

    def test_generate_image_accepts_remote_url_response(self):
        class _FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"url": "https://cdn.example/image.png"}]}

        class _FakeClient:
            async def post(self, *args, **kwargs):
                return _FakeResponse()

        generator = GatewayImageGenerator(http_client=_FakeClient())
        generator.model_name = "gw/image"
        generator.api_key = "k"
        generator.base_url = "http://localhost:0/v1"

        result = run_async(generator.generate_image(prompt="hello world", research_id="r1"))

        self.assertEqual(
            result,
            [
                {
                    "path": "https://cdn.example/image.png",
                    "url": "https://cdn.example/image.png",
                    "absolute_url": "https://cdn.example/image.png",
                    "prompt": "hello world",
                    "alt_text": "Illustration: hello world",
                }
            ],
        )

    def test_gateway_image_generator_uses_injected_http_client(self):
        """When an http_client is injected, no new httpx.AsyncClient must be created."""
        from base64 import b64encode

        pixel_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 16
        encoded = b64encode(pixel_bytes).decode("ascii")

        class _FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"b64_json": encoded}]}

        class _FakeClient:
            async def post(self, *args, **kwargs):
                return _FakeResponse()

        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeClient()
            generator = GatewayImageGenerator(output_dir=tmp, http_client=fake_client)
            generator.model_name = "gw/image"
            generator.api_key = "k"
            generator.base_url = "http://localhost:0/v1"

            def _must_not_be_called(*args, **kwargs):
                raise AssertionError("httpx.AsyncClient constructor must not be called when http_client is injected")

            with patch("llm_gateway_core.agents.deep_research.httpx.AsyncClient", side_effect=_must_not_be_called):
                result = run_async(generator.generate_image(prompt="test injection", research_id="r99"))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["prompt"], "test injection")


class GeneratedImagesInResultTests(unittest.TestCase):
    def test_conduct_deep_research_exposes_empty_generated_images_list(self):
        # Without patched image_generation, the accumulator stays empty but the
        # key is always present so the endpoint can rely on it.
        result = run_async(
            _FakeDeepResearchManager().conduct_deep_research(
                query="topic",
                fast_model="llmgateway/light_model",
                smart_model="llmgateway/light_model",
                strategic_model="llmgateway/light_model",
            )
        )
        self.assertIn("generated_images", result)
        self.assertEqual(result["generated_images"], [])


if __name__ == "__main__":
    unittest.main()
