from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx

try:
    from gpt_researcher import GPTResearcher
except ImportError as exc:  # pragma: no cover - exercised through endpoint tests
    GPTResearcher = None  # type: ignore[assignment]
    GPT_RESEARCHER_IMPORT_ERROR: ImportError | None = exc
else:
    GPT_RESEARCHER_IMPORT_ERROR = None

_ENV_LOCK = threading.Lock()
logger = logging.getLogger(__name__)

SearchCallback = Callable[[str, int], Awaitable[list[dict[str, Any]]]]
ReadCallback = Callable[[str], Awaitable[dict[str, Any]]]

# Per-request accumulator populated by GatewayImageGenerator whenever it writes
# a PNG to disk. Consumed by DeepResearchManager.conduct_deep_research so the
# HTTP endpoint can expose the generated images as structured data instead of
# forcing clients to parse markdown for ![alt](url) fragments.
_CURRENT_IMAGE_ACCUMULATOR: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "_CURRENT_IMAGE_ACCUMULATOR", default=None
)


class DeepResearchUnavailableError(RuntimeError):
    pass


def _raise_if_cancelled(cancellation_event: threading.Event | None) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise asyncio.CancelledError("Deep research cancelled.")


@contextmanager
def _temporary_environ(values: Mapping[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _as_gpt_researcher_llm(model: str) -> str:
    value = model.strip()
    if not value or ":" in value:
        raise ValueError("Deep research model names must be gateway model ids without provider prefixes.")
    return f"openai:{value}"


def _as_gpt_researcher_embedding(model: str) -> str:
    value = model.strip()
    if not value or ":" in value:
        raise ValueError("Deep research embedding model must be a gateway model id without provider prefix.")
    return f"custom:{value}"


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value is None:
        return []
    return [value]


class GatewayImageGenerator:
    """Drop-in replacement for gpt_researcher's ImageGeneratorProvider that posts to the gateway's /v1/images/generations."""

    def __init__(
        self,
        model_name: str | None = None,
        api_key: str | None = None,
        output_dir: str = "outputs",
    ) -> None:
        self.model_name = (model_name or os.environ.get("IMAGE_GENERATION_MODEL", "")).strip()
        self.api_key = api_key or os.environ.get("IMAGE_GENERATION_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
        self.size = os.environ.get("IMAGE_GENERATION_SIZE", "").strip() or None
        self.output_dir = Path(output_dir)

    def is_available(self) -> bool:
        return bool(self.model_name and self.base_url and self.api_key)

    async def generate_image(
        self,
        prompt: str,
        context: str = "",
        research_id: str = "",
        aspect_ratio: str = "1:1",
        num_images: int = 1,
        style: str = "dark",
    ) -> list[dict[str, Any]]:
        if not self.is_available():
            logger.warning("Gateway image generator is missing model/base_url/api_key; skipping.")
            return []

        request_body: dict[str, Any] = {
            "model": self.model_name,
            "prompt": prompt,
            "response_format": "b64_json",
        }
        image_count = max(1, int(num_images))
        if image_count > 1:
            request_body["n"] = image_count
        if self.size:
            request_body["size"] = self.size

        url = f"{self.base_url}/images/generations"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, json=request_body, headers=headers)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.error("Gateway image generation request failed: %s", exc, exc_info=True)
            raise RuntimeError(f"Gateway image generation request failed: {exc}") from exc

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            logger.warning("Gateway image generation returned unexpected payload shape.")
            raise RuntimeError("Gateway image generation returned unexpected payload shape.")

        output_path = self.output_dir / "images"
        if research_id:
            output_path = output_path / research_id
        output_path.mkdir(parents=True, exist_ok=True)

        generated: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            image_bytes = _decode_image_item(item)
            if image_bytes is None:
                image_url = _image_item_url(item)
                if image_url is not None:
                    generated.append(
                        {
                            "path": image_url,
                            "url": image_url,
                            "absolute_url": image_url,
                            "prompt": prompt,
                            "alt_text": _alt_text(prompt),
                        }
                    )
                continue
            filename = _image_filename(prompt, index)
            filepath = (output_path / filename).resolve()
            filepath.write_bytes(image_bytes)
            web_prefix = f"/outputs/images/{research_id}" if research_id else "/outputs/images"
            generated.append(
                {
                    "path": str(filepath),
                    "url": f"{web_prefix}/{filename}",
                    "absolute_url": str(filepath),
                    "prompt": prompt,
                    "alt_text": _alt_text(prompt),
                }
            )

        accumulator = _CURRENT_IMAGE_ACCUMULATOR.get()
        if accumulator is not None and generated:
            accumulator.extend(generated)

        return generated


def _decode_image_item(item: dict[str, Any]) -> bytes | None:
    encoded = item.get("b64_json")
    if isinstance(encoded, str) and encoded:
        try:
            return base64.b64decode(encoded)
        except (ValueError, TypeError) as exc:
            logger.warning("Failed to decode b64_json image payload: %s", exc)
            return None
    url = item.get("url")
    if isinstance(url, str) and url.startswith("data:") and "," in url:
        try:
            return base64.b64decode(url.split(",", 1)[1])
        except (ValueError, TypeError) as exc:
            logger.warning("Failed to decode data URL image payload: %s", exc)
            return None
    return None


def _image_item_url(item: dict[str, Any]) -> str | None:
    url = item.get("url")
    if isinstance(url, str) and url.strip() and not url.startswith("data:"):
        return url.strip()
    return None


def _image_filename(prompt: str, index: int) -> str:
    import hashlib as _hashlib

    digest = _hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]
    return f"image_{digest}_{index}.png"


def _alt_text(prompt: str) -> str:
    clean = re.sub(r"\s+", " ", prompt).strip()
    if len(clean) > 120:
        clean = clean[:117] + "..."
    return f"Illustration: {clean}"


def _required_image_prompt(query: str, context: str) -> str:
    context_excerpt = re.sub(r"\s+", " ", context).strip()
    if len(context_excerpt) > 1400:
        context_excerpt = context_excerpt[:1397] + "..."
    return (
        "Create one professional, information-dense illustration for a deep research report. "
        "Visualize the central concept, key actors, relationships, and cause-effect flow. "
        "Use clear labels, structured composition, and a polished editorial infographic style. "
        f"Research topic: {query}. "
        f"Research context: {context_excerpt}"
    )


def _install_gateway_image_generator(researcher: Any, image_generation_model: str) -> None:
    image_generator = getattr(researcher, "image_generator", None)
    if image_generator is None:
        raise ValueError("GPT Researcher image generator is not available.")

    provider = GatewayImageGenerator(model_name=image_generation_model)
    if not provider.is_available():
        raise ValueError("Gateway image generator is missing model, base URL, or API key.")
    image_generator.image_provider = provider


def _research_context_as_text(context: Any) -> str:
    if isinstance(context, list):
        return "\n\n".join(str(item) for item in context)
    return str(context)


async def _generate_required_images(
    researcher: Any,
    *,
    query: str,
    generated_images: list[dict[str, Any]],
) -> None:
    image_generator = getattr(researcher, "image_generator", None)
    is_enabled = getattr(image_generator, "is_enabled", None)
    if image_generator is None or not callable(is_enabled) or not is_enabled():
        raise ValueError("Deep research image generation is enabled but GPT Researcher image generator is not available.")

    existing_images = _as_list(getattr(researcher, "available_images", []))
    if existing_images:
        if not generated_images:
            generated_images.extend(existing_images)
        return

    plan_and_generate_images = getattr(image_generator, "plan_and_generate_images", None)
    if not callable(plan_and_generate_images):
        raise ValueError("Deep research image generation is enabled but GPT Researcher image planner is not available.")

    generate_research_id = getattr(researcher, "_generate_research_id", None)
    research_id = str(generate_research_id()) if callable(generate_research_id) else ""
    available_images = _as_list(
        await plan_and_generate_images(
            context=_research_context_as_text(getattr(researcher, "context", [])),
            query=query,
            research_id=research_id,
        )
    )
    if not available_images:
        logger.warning("GPT Researcher image planner returned no images; generating one required image directly.")
        image_provider = getattr(image_generator, "image_provider", None)
        generate_image = getattr(image_provider, "generate_image", None)
        if not callable(generate_image):
            raise ValueError("Deep research image generation is enabled but gateway image provider is not available.")

        context = _research_context_as_text(getattr(researcher, "context", []))
        cfg = getattr(image_generator, "cfg", None)
        image_style = getattr(cfg, "image_generation_style", "dark")
        available_images = _as_list(
            await generate_image(
                prompt=_required_image_prompt(query, context),
                context=context,
                research_id=research_id,
                num_images=1,
                style=image_style,
            )
        )
    if not available_images:
        raise ValueError("Deep research image generation is enabled but the gateway image provider returned no images.")

    researcher.available_images = available_images
    if not generated_images:
        generated_images.extend(available_images)


@contextmanager
def _gateway_research_tools(
    search: SearchCallback,
    read: ReadCallback,
    *,
    gateway_callback_loop: asyncio.AbstractEventLoop | None = None,
    image_generation_enabled: bool = False,
    image_accumulator: list[dict[str, Any]] | None = None,
):
    import gpt_researcher.actions as actions_module
    import gpt_researcher.actions.query_processing as query_processing_module
    import gpt_researcher.agent as agent_module
    import gpt_researcher.skills.deep_research as deep_research_module
    import gpt_researcher.skills.researcher as researcher_module

    loop = gateway_callback_loop or asyncio.get_running_loop()

    async def _run_gateway_callback(callback: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            return await callback(*args)
        future = asyncio.run_coroutine_threadsafe(callback(*args), loop)
        return await asyncio.wrap_future(future)

    accumulator_token = None
    if image_generation_enabled and image_accumulator is not None:
        accumulator_token = _CURRENT_IMAGE_ACCUMULATOR.set(image_accumulator)

    def _format_search_results(results: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {
                "href": str(item["url"]),
                "title": str(item.get("title") or ""),
                "body": str(item.get("snippet") or item.get("content") or ""),
            }
            for item in results
            if isinstance(item, dict) and item.get("url")
        ]

    async def gateway_get_search_results(
        query: str,
        _retriever: Any,
        query_domains: list[str] | None = None,
        researcher: Any = None,
    ) -> list[dict[str, str]]:
        max_results = 5
        if researcher is not None:
            max_results = int(getattr(researcher.cfg, "max_search_results_per_query", max_results))
        return _format_search_results(await _run_gateway_callback(search, query, max_results))

    class GatewaySearchRetriever:
        def __init__(
            self,
            query: str,
            headers: dict | None = None,
            query_domains: list[str] | None = None,
            **_kwargs: Any,
        ):
            self.query = query
            self.query_domains = query_domains or []

        def search(self, max_results: int = 5) -> list[dict[str, str]]:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is loop:
                raise RuntimeError(
                    "GatewaySearchRetriever.search cannot run on the gateway event loop; "
                    "patch gpt_researcher async get_search_results bindings instead."
                )
            future = asyncio.run_coroutine_threadsafe(search(self.query, max_results), loop)
            return _format_search_results(future.result())

    class GatewayReadBrowserManager:
        def __init__(self, researcher: Any):
            self.researcher = researcher

        async def browse_urls(self, urls: list[str]) -> list[dict[str, Any]]:
            scraped_content: list[dict[str, Any]] = []
            for url in urls:
                article = await _run_gateway_callback(read, url)
                content = str(article.get("content") or "").strip()
                if not content:
                    logger.warning("Gateway web reader returned empty content for %s", url)
                    continue
                scraped_content.append(
                    {
                        "url": str(article.get("url") or url),
                        "raw_content": content,
                        "image_urls": [],
                        "title": str(article.get("title") or ""),
                    }
                )
            self.researcher.add_research_sources(scraped_content)
            return scraped_content

    original_get_retrievers = agent_module.get_retrievers
    original_browser_manager = agent_module.BrowserManager
    original_agent_get_search_results = agent_module.get_search_results
    original_actions_get_search_results = actions_module.get_search_results
    original_query_processing_get_search_results = query_processing_module.get_search_results
    original_deep_research_get_search_results = deep_research_module.get_search_results
    original_researcher_get_search_results = researcher_module.get_search_results
    agent_module.get_retrievers = lambda _headers, _cfg: [GatewaySearchRetriever]
    agent_module.BrowserManager = GatewayReadBrowserManager
    agent_module.get_search_results = gateway_get_search_results
    actions_module.get_search_results = gateway_get_search_results
    query_processing_module.get_search_results = gateway_get_search_results
    deep_research_module.get_search_results = gateway_get_search_results
    researcher_module.get_search_results = gateway_get_search_results
    try:
        yield
    finally:
        agent_module.get_retrievers = original_get_retrievers
        agent_module.BrowserManager = original_browser_manager
        agent_module.get_search_results = original_agent_get_search_results
        actions_module.get_search_results = original_actions_get_search_results
        query_processing_module.get_search_results = original_query_processing_get_search_results
        deep_research_module.get_search_results = original_deep_research_get_search_results
        researcher_module.get_search_results = original_researcher_get_search_results
        if accumulator_token is not None:
            _CURRENT_IMAGE_ACCUMULATOR.reset(accumulator_token)


class DeepResearchManager:
    def _get_researcher_factory(self) -> Any:
        if GPTResearcher is None:
            detail = f": {GPT_RESEARCHER_IMPORT_ERROR}" if GPT_RESEARCHER_IMPORT_ERROR else ""
            raise DeepResearchUnavailableError(f"Python package 'gpt-researcher' is not installed{detail}.")
        return GPTResearcher

    async def conduct_deep_research(
        self,
        *,
        query: str,
        fast_model: str,
        smart_model: str,
        strategic_model: str,
        embedding_model: str | None = None,
        gateway_base_url: str | None = None,
        gateway_api_key: str | None = None,
        report_type: str = "deep",
        max_words: int = 2500,
        breadth: int = 4,
        depth: int = 2,
        concurrency: int = 4,
        language: str = "English",
        verbose: bool = False,
        image_generation_enabled: bool = False,
        image_generation_model: str | None = None,
        image_generation_size: str | None = None,
        image_generation_api_key: str | None = None,
        gateway_search: SearchCallback | None = None,
        gateway_read: ReadCallback | None = None,
        gateway_callback_loop: asyncio.AbstractEventLoop | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        _raise_if_cancelled(cancellation_event)
        factory = self._get_researcher_factory()
        env_values = {
            "FAST_LLM": _as_gpt_researcher_llm(fast_model),
            "SMART_LLM": _as_gpt_researcher_llm(smart_model),
            "STRATEGIC_LLM": _as_gpt_researcher_llm(strategic_model),
            "TOTAL_WORDS": str(max_words),
            "DEEP_RESEARCH_BREADTH": str(breadth),
            "DEEP_RESEARCH_DEPTH": str(depth),
            "DEEP_RESEARCH_CONCURRENCY": str(concurrency),
            "LANGUAGE": language.strip() or "English",
            "IMAGE_GENERATION_ENABLED": "true" if image_generation_enabled else "false",
        }
        if embedding_model:
            env_values["EMBEDDING"] = _as_gpt_researcher_embedding(embedding_model)
        if gateway_base_url:
            env_values["OPENAI_BASE_URL"] = gateway_base_url.rstrip("/")
        if gateway_api_key:
            env_values["OPENAI_API_KEY"] = gateway_api_key
        if image_generation_enabled:
            if not image_generation_model or not image_generation_model.strip():
                raise ValueError("Deep research image generation is enabled but image_generation_model is not configured.")
            if not image_generation_size or not image_generation_size.strip():
                raise ValueError("Deep research image generation is enabled but image_generation_size is not configured.")
            env_values["IMAGE_GENERATION_MODEL"] = image_generation_model.strip()
            env_values["IMAGE_GENERATION_SIZE"] = image_generation_size.strip()
            env_values["IMAGE_GENERATION_PROVIDER"] = "gateway"
            if image_generation_api_key:
                env_values["IMAGE_GENERATION_API_KEY"] = image_generation_api_key

        generated_images: list[dict[str, Any]] = []

        with _ENV_LOCK:
            with _temporary_environ(env_values):
                if (gateway_search is None) != (gateway_read is None):
                    raise ValueError("Gateway deep research requires both search and read callbacks.")
                if gateway_search is None or gateway_read is None:
                    tools_context = nullcontext()
                else:
                    tools_context = _gateway_research_tools(
                        gateway_search,
                        gateway_read,
                        gateway_callback_loop=gateway_callback_loop,
                        image_generation_enabled=image_generation_enabled,
                        image_accumulator=generated_images if image_generation_enabled else None,
                    )

                with tools_context:
                    _raise_if_cancelled(cancellation_event)
                    researcher = factory(query=query, report_type=report_type, verbose=verbose)
                    if image_generation_enabled:
                        _install_gateway_image_generator(researcher, image_generation_model.strip())
                    _raise_if_cancelled(cancellation_event)
                    research_result = await researcher.conduct_research()
                    _raise_if_cancelled(cancellation_event)
                    if image_generation_enabled:
                        await _generate_required_images(
                            researcher,
                            query=query,
                            generated_images=generated_images,
                        )
                    _raise_if_cancelled(cancellation_event)
                    report = await researcher.write_report()
                    _raise_if_cancelled(cancellation_event)

                return {
                    "success": True,
                    "query": query,
                    "report": report,
                    "research_result": research_result,
                    "sources": _as_list(getattr(researcher, "sources", [])),
                    "source_urls": _as_list(getattr(researcher, "source_urls", [])),
                    "context": _as_list(getattr(researcher, "context", [])),
                    "costs": getattr(researcher, "costs", None),
                    "generated_images": generated_images,
                }
