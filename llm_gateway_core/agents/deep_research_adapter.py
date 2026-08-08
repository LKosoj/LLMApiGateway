"""Lazy, version-pinned adapter for the public GPT Researcher API."""

from __future__ import annotations

import asyncio
import importlib
import logging
import random
import re
import socket
from contextlib import contextmanager
from importlib import metadata
from typing import Any

from ..services.deep_research_protocol import (
    DeepResearchCallbackOperation,
    DeepResearchCallbackReportedError,
    DeepResearchCallbackRequest,
    DeepResearchJob,
    DeepResearchProtocolError,
    DeepResearchResult,
    JsonValue,
    _validate_safe_image_metadata,
    callback_request_message,
    parse_callback_response,
    recv_async_frame,
    send_async_frame,
)


GPT_RESEARCHER_REQUIRED_VERSION = "0.14.8"
logger = logging.getLogger(__name__)


class DeepResearchUnavailableError(RuntimeError):
    pass


class DeepResearchIpcClient:
    """Multiplex child callback requests over the inherited socket."""

    def __init__(self, sock: socket.socket, *, job_id: str) -> None:
        self._sock = sock
        self._job_id = job_id
        self._next_message_id = 0
        self._pending: dict[
            str,
            tuple[DeepResearchCallbackRequest, asyncio.Future[JsonValue]],
        ] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None

    async def start(self) -> None:
        if self._reader_task is not None:
            raise RuntimeError("deep-research IPC client already started")
        self._sock.setblocking(False)
        self._reader_task = asyncio.create_task(
            self._read_responses(),
            name=f"deep-research-child-ipc-{self._job_id}",
        )

    async def aclose(self) -> None:
        reader_task = self._reader_task
        if reader_task is None:
            return
        self._reader_task = None
        if not reader_task.done():
            reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)
        for _request, future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def search(self, query: str, max_results: int) -> list[dict[str, str]]:
        result = await self._call(
            DeepResearchCallbackOperation.SEARCH,
            {"query": query, "max_results": max_results},
        )
        return result  # type: ignore[return-value]

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    async def read(self, url: str) -> dict[str, str]:
        result = await self._call(
            DeepResearchCallbackOperation.READ,
            {"url": url},
        )
        return result  # type: ignore[return-value]

    async def image(
        self,
        *,
        prompt: str,
        context: str,
        research_id: str,
        aspect_ratio: str,
        num_images: int,
        style: str,
    ) -> list[dict[str, str]]:
        result = await self._call(
            DeepResearchCallbackOperation.IMAGE,
            {
                "prompt": prompt,
                "context": context,
                "research_id": research_id,
                "aspect_ratio": aspect_ratio,
                "num_images": num_images,
                "style": style,
            },
        )
        return result  # type: ignore[return-value]

    async def _call(
        self,
        operation: DeepResearchCallbackOperation,
        arguments: dict[str, JsonValue],
    ) -> JsonValue:
        self.raise_if_failed()
        if self._reader_task is None or self._reader_task.done():
            raise DeepResearchProtocolError("deep-research IPC client is unavailable")
        self._next_message_id += 1
        message_id = f"message-{self._next_message_id}"
        request = DeepResearchCallbackRequest(
            job_id=self._job_id,
            message_id=message_id,
            operation=operation,
            arguments=arguments,
        )
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = (request, future)
        try:
            async with self._write_lock:
                await send_async_frame(self._sock, callback_request_message(request))
            return await future
        finally:
            self._pending.pop(message_id, None)

    async def _read_responses(self) -> None:
        try:
            while True:
                message = await recv_async_frame(self._sock)
                if not isinstance(message, dict):
                    raise DeepResearchProtocolError(
                        "callback response must be an object"
                    )
                message_id = message.get("message_id")
                if not isinstance(message_id, str) or message_id not in self._pending:
                    raise DeepResearchProtocolError(
                        "callback response has an unknown message id"
                    )
                request, future = self._pending[message_id]
                try:
                    result = parse_callback_response(message, request=request)
                except DeepResearchCallbackReportedError as exc:
                    # A read the upstream refused is not a broken run: that one
                    # source is unavailable (paywall, dead link, blocked
                    # transcript) and the batch continues without it. Search and
                    # image failures stay sticky — those really do end the run,
                    # and the parent logs every failed callback either way.
                    if (
                        self._failure is None
                        and request.operation
                        is not DeepResearchCallbackOperation.READ
                    ):
                        self._failure = exc
                    if not future.done():
                        future.set_exception(exc)
                else:
                    if not future.done():
                        future.set_result(result)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if self._failure is None:
                self._failure = exc
            failure = self._failure
            for _request, future in self._pending.values():
                if not future.done():
                    future.set_exception(failure)
            raise


def get_gpt_researcher_factory() -> Any:
    """Load GPT Researcher only at execution time and reject version drift."""
    try:
        installed_version = metadata.version("gpt-researcher")
    except metadata.PackageNotFoundError as exc:
        raise DeepResearchUnavailableError(
            "Python package 'gpt-researcher' is not installed."
        ) from exc
    if installed_version != GPT_RESEARCHER_REQUIRED_VERSION:
        raise DeepResearchUnavailableError(
            "Python package 'gpt-researcher' has an unsupported version; "
            f"required {GPT_RESEARCHER_REQUIRED_VERSION}."
        )
    try:
        module = importlib.import_module("gpt_researcher")
    except ImportError as exc:
        raise DeepResearchUnavailableError(
            "Python package 'gpt-researcher' could not be imported."
        ) from exc
    factory = getattr(module, "GPTResearcher", None)
    if not callable(factory):
        raise DeepResearchUnavailableError(
            "Python package 'gpt-researcher' does not expose GPTResearcher."
        )
    return factory


@contextmanager
def _gateway_research_tools(ipc: DeepResearchIpcClient):
    import gpt_researcher.actions as actions_module
    import gpt_researcher.actions.query_processing as query_processing_module
    import gpt_researcher.agent as agent_module
    import gpt_researcher.skills.deep_research as deep_research_module
    import gpt_researcher.skills.researcher as researcher_module

    async def gateway_get_search_results(
        query: str,
        _retriever: Any,
        query_domains: list[str] | None = None,
        researcher: Any = None,
    ) -> list[dict[str, str]]:
        del query_domains
        max_results = 5
        if researcher is not None:
            max_results = int(
                getattr(researcher.cfg, "max_search_results_per_query", max_results)
            )
        results = await ipc.search(query, max_results)
        return [
            {
                "href": item["url"],
                "title": item["title"],
                "body": item["snippet"],
            }
            for item in results
        ]

    class GatewaySearchRetriever:
        def __init__(
            self,
            query: str,
            headers: dict | None = None,
            query_domains: list[str] | None = None,
            **_kwargs: Any,
        ) -> None:
            del query, headers, query_domains

        def search(self, max_results: int = 5) -> list[dict[str, str]]:
            del max_results
            raise RuntimeError(
                "Synchronous GPT Researcher retrieval is unavailable in the isolated child."
            )

    class GatewayReadBrowserManager:
        def __init__(self, researcher: Any) -> None:
            self.researcher = researcher

        async def browse_urls(self, urls: list[str]) -> list[dict[str, Any]]:
            articles = await asyncio.gather(
                *(ipc.read(url) for url in urls),
                return_exceptions=True,
            )
            for url, article in zip(urls, articles, strict=True):
                # Only an upstream-reported refusal is survivable. A broken
                # socket or a protocol violation means the child can no longer
                # talk to the gateway at all, and swallowing that would turn a
                # dead run into a silently empty report.
                if isinstance(article, BaseException):
                    if not isinstance(article, DeepResearchCallbackReportedError):
                        raise article
                    logger.warning(
                        "deep-research skipped unreadable source %s: %s",
                        url,
                        article,
                    )
            scraped_content = [
                {
                    "url": article.get("url") or url,
                    "raw_content": article.get("content") or "",
                    "image_urls": [],
                    "title": article.get("title") or "",
                }
                for url, article in zip(urls, articles, strict=True)
                if isinstance(article, dict)
                and (article.get("content") or "").strip()
            ]
            self.researcher.add_research_sources(scraped_content)
            return scraped_content

    async def gateway_search_relevant_source_urls(
        conductor: Any,
        query: str,
        query_domains: list[str] | None = None,
    ) -> list[str]:
        del query_domains
        max_results = int(
            getattr(conductor.researcher.cfg, "max_search_results_per_query", 5)
        )
        results = await ipc.search(query, max_results)
        new_urls = await conductor._get_new_urls(
            [item["url"] for item in results if item.get("url")]
        )
        random.shuffle(new_urls)
        return new_urls

    original_values = (
        agent_module.get_retrievers,
        agent_module.BrowserManager,
        agent_module.get_search_results,
        actions_module.get_search_results,
        query_processing_module.get_search_results,
        deep_research_module.get_search_results,
        researcher_module.get_search_results,
        researcher_module.ResearchConductor._search_relevant_source_urls,
    )
    agent_module.get_retrievers = lambda _headers, _cfg: [GatewaySearchRetriever]
    agent_module.BrowserManager = GatewayReadBrowserManager
    agent_module.get_search_results = gateway_get_search_results
    actions_module.get_search_results = gateway_get_search_results
    query_processing_module.get_search_results = gateway_get_search_results
    deep_research_module.get_search_results = gateway_get_search_results
    researcher_module.get_search_results = gateway_get_search_results
    researcher_module.ResearchConductor._search_relevant_source_urls = (
        gateway_search_relevant_source_urls
    )
    try:
        yield
    finally:
        (
            agent_module.get_retrievers,
            agent_module.BrowserManager,
            agent_module.get_search_results,
            actions_module.get_search_results,
            query_processing_module.get_search_results,
            deep_research_module.get_search_results,
            researcher_module.get_search_results,
            researcher_module.ResearchConductor._search_relevant_source_urls,
        ) = original_values


class _IpcImageProvider:
    def __init__(self, ipc: DeepResearchIpcClient) -> None:
        self._ipc = ipc

    def is_available(self) -> bool:
        return True

    async def generate_image(
        self,
        prompt: str,
        context: str = "",
        research_id: str = "",
        aspect_ratio: str = "1:1",
        num_images: int = 1,
        style: str = "dark",
    ) -> list[dict[str, str]]:
        return await self._ipc.image(
            prompt=prompt,
            context=context,
            research_id=research_id,
            aspect_ratio=aspect_ratio,
            num_images=max(1, int(num_images)),
            style=style,
        )


def _install_ipc_image_provider(
    researcher: object,
    ipc: DeepResearchIpcClient,
) -> None:
    image_generator = getattr(researcher, "image_generator", None)
    if image_generator is None:
        raise ValueError("GPT Researcher image generator is unavailable.")
    image_generator.image_provider = _IpcImageProvider(ipc)


async def _ensure_required_images(
    researcher: object,
    *,
    ipc: DeepResearchIpcClient,
    job: DeepResearchJob,
) -> tuple[dict[str, str], ...]:
    existing = _safe_image_metadata(getattr(researcher, "available_images", []))
    if existing:
        return existing

    image_generator = getattr(researcher, "image_generator", None)
    planner = getattr(image_generator, "plan_and_generate_images", None)
    if image_generator is None or not callable(planner):
        raise ValueError("GPT Researcher image planner is unavailable.")
    context = _research_context_text(
        _call_public_getter(researcher, "get_research_context")
    )
    planned = await planner(
        context=context,
        query=job.query,
        research_id=job.job_id,
    )
    generated = _safe_image_metadata(planned)
    if not generated:
        generated = tuple(
            await ipc.image(
                prompt=_required_image_prompt(job.query, context),
                context=context,
                research_id=job.job_id,
                aspect_ratio="1:1",
                num_images=1,
                style="dark",
            )
        )
    if not generated:
        raise ValueError("Gateway image generation returned no images.")
    researcher.available_images = list(generated)
    return generated


def _safe_image_metadata(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list | tuple):
        return ()
    images: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if not all(
            isinstance(item.get(key), str)
            for key in ("url", "prompt", "alt_text")
        ):
            continue
        image = {
            "url": item["url"],
            "prompt": item["prompt"],
            "alt_text": item["alt_text"],
        }
        try:
            _validate_safe_image_metadata(image)
        except (DeepResearchProtocolError, ValueError):
            continue
        images.append(image)
    return tuple(images)


def _research_context_text(value: object) -> str:
    if isinstance(value, list | tuple):
        return "\n\n".join(str(item) for item in value)
    return str(value)


def _required_image_prompt(query: str, context: str) -> str:
    excerpt = re.sub(r"\s+", " ", context).strip()
    if len(excerpt) > 1400:
        excerpt = excerpt[:1397] + "..."
    return (
        "Create one professional, information-dense illustration for a deep research "
        "report. Visualize the central concept, key actors, relationships, and "
        f"cause-effect flow. Research topic: {query}. Research context: {excerpt}"
    )


def collect_public_result(
    researcher: object,
    *,
    query: str,
    report: object,
    research_result: object,
    generated_images: tuple[dict[str, str], ...] = (),
) -> DeepResearchResult:
    """Read only the public 0.14.8 result getters and validate their schema."""
    sources = _call_public_getter(researcher, "get_research_sources")
    source_urls = _call_public_getter(researcher, "get_source_urls")
    context = _call_public_getter(researcher, "get_research_context")
    try:
        costs = _call_public_getter(researcher, "get_costs")
    except Exception:
        logger.warning("GPT Researcher diagnostic cost is unavailable.")
        costs = None
    if not isinstance(sources, list):
        raise ValueError("GPT Researcher get_research_sources() must return a list.")
    if not isinstance(source_urls, list):
        raise ValueError("GPT Researcher get_source_urls() must return a list.")
    if isinstance(context, str) and context.strip():
        context = [context]
    elif not isinstance(context, list):
        raise ValueError(
            "GPT Researcher get_research_context() must return a list or non-empty string."
        )
    if not isinstance(report, str):
        raise ValueError("GPT Researcher write_report() must return a string.")
    return DeepResearchResult(
        query=query,
        report=report,
        research_result=research_result,  # type: ignore[arg-type]
        sources=tuple(sources),  # type: ignore[arg-type]
        source_urls=tuple(source_urls),  # type: ignore[arg-type]
        context=tuple(context),  # type: ignore[arg-type]
        costs=costs,  # type: ignore[arg-type]
        generated_images=generated_images,
    )


async def conduct_deep_research_job(
    job: DeepResearchJob,
    sock: socket.socket,
) -> DeepResearchResult:
    ipc = DeepResearchIpcClient(sock, job_id=job.job_id)
    await ipc.start()
    factory = get_gpt_researcher_factory()
    try:
        generated_images: tuple[dict[str, str], ...] = ()
        with _gateway_research_tools(ipc):
            researcher = factory(
                query=job.query,
                report_type=job.report_type,
                verbose=job.verbose,
            )
            if job.image_generation_enabled:
                _install_ipc_image_provider(researcher, ipc)
            research_result = await researcher.conduct_research()
            ipc.raise_if_failed()
            if job.image_generation_enabled:
                generated_images = await _ensure_required_images(
                    researcher,
                    ipc=ipc,
                    job=job,
                )
            report = await researcher.write_report()
            ipc.raise_if_failed()
        result = collect_public_result(
            researcher,
            query=job.query,
            report=report,
            research_result=research_result,
            generated_images=generated_images,
        )
        ipc.raise_if_failed()
        return result
    finally:
        await ipc.aclose()


def run_deep_research_job(
    job: DeepResearchJob,
    sock: socket.socket,
) -> DeepResearchResult:
    try:
        return asyncio.run(conduct_deep_research_job(job, sock))
    finally:
        sock.setblocking(True)


def _call_public_getter(researcher: object, name: str) -> object:
    getter = getattr(researcher, name, None)
    if not callable(getter):
        raise ValueError(f"GPT Researcher 0.14.8 public method {name}() is unavailable.")
    return getter()
