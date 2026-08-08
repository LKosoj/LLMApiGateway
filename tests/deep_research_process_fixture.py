"""Spawn-safe adapter fixture for deep-research process tests."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from llm_gateway_core.agents.deep_research_adapter import DeepResearchIpcClient
from llm_gateway_core.services.deep_research_protocol import (
    DeepResearchCallbackOperation,
    DeepResearchCallbackRequest,
    DeepResearchJob,
    DeepResearchResult,
    callback_request_message,
    encode_frame,
)


_IMPORT_ENV = {
    "fast_llm": os.environ.get("FAST_LLM"),
    "parent_secret_present": "LLMGATEWAY_PARENT_ONLY_SECRET" in os.environ,
}


async def _run_callback_fixture(
    job: DeepResearchJob,
    sock: socket.socket,
) -> DeepResearchResult:
    ipc = DeepResearchIpcClient(sock, job_id=job.job_id)
    await ipc.start()
    try:
        search, article, images = await asyncio.gather(
            ipc.search("callback search", 2),
            ipc.read("https://example.com/article"),
            ipc.image(
                prompt="callback image",
                context="context",
                research_id=job.job_id,
                aspect_ratio="1:1",
                num_images=1,
                style="dark",
            ),
        )
    finally:
        await ipc.aclose()
        sock.setblocking(True)
    return DeepResearchResult(
        query=job.query,
        report="callback report",
        research_result={"search": search, "article": article},
        sources=({"title": article["title"], "url": article["url"]},),
        source_urls=(article["url"],),
        context=(article["content"],),
        costs=9.99,
        generated_images=tuple(images),
    )


async def _run_swallowed_callback_fixture(
    job: DeepResearchJob,
    sock: socket.socket,
) -> DeepResearchResult:
    ipc = DeepResearchIpcClient(sock, job_id=job.job_id)
    await ipc.start()
    try:
        try:
            await ipc.search("swallowed callback", 1)
        except Exception:
            pass
        ipc.raise_if_failed()
        raise AssertionError("sticky callback failure was not preserved")
    finally:
        await ipc.aclose()
        sock.setblocking(True)


async def _run_read_failure_fixture(
    job: DeepResearchJob,
    sock: socket.socket,
) -> DeepResearchResult:
    ipc = DeepResearchIpcClient(sock, job_id=job.job_id)
    await ipc.start()
    try:
        try:
            await ipc.read("https://example.com/unreadable")
        except Exception:
            pass
        # A refused read leaves the client usable: it must not poison the run,
        # and the operations after it must still reach the parent.
        ipc.raise_if_failed()
        search = await ipc.search("after failed read", 1)
    finally:
        await ipc.aclose()
        sock.setblocking(True)
    return DeepResearchResult(
        query=job.query,
        report="report after failed read",
        research_result={"search": search},
        sources=({"title": "recovered"},),
        source_urls=("https://example.com",),
        context=("recovered context",),
        costs=0.5,
    )


async def _run_mixed_callback_fixture(
    job: DeepResearchJob,
    sock: socket.socket,
) -> DeepResearchResult:
    ipc = DeepResearchIpcClient(sock, job_id=job.job_id)
    await ipc.start()
    try:
        search, article = await asyncio.gather(
            ipc.search("mixed callback", 1),
            ipc.read("https://example.com/mixed"),
            return_exceptions=True,
        )
        if not isinstance(search, Exception) or not isinstance(article, dict):
            raise AssertionError("mixed callback responses were cross-wired")
        ipc.raise_if_failed()
        raise AssertionError("sticky mixed callback failure was not preserved")
    finally:
        await ipc.aclose()
        sock.setblocking(True)


def run_deep_research_job(
    job: DeepResearchJob,
    _sock: socket.socket,
) -> DeepResearchResult:
    if job.query == "callbacks":
        return asyncio.run(_run_callback_fixture(job, _sock))
    if job.query == "swallowed-callback":
        return asyncio.run(_run_swallowed_callback_fixture(job, _sock))
    if job.query == "mixed-callback":
        return asyncio.run(_run_mixed_callback_fixture(job, _sock))
    if job.query == "read-failure":
        return asyncio.run(_run_read_failure_fixture(job, _sock))
    if job.query == "terminal-before-callback":
        request = DeepResearchCallbackRequest(
            job_id=job.job_id,
            message_id="message-early",
            operation=DeepResearchCallbackOperation.SEARCH,
            arguments={"query": "early terminal", "max_results": 1},
        )
        _sock.sendall(encode_frame(callback_request_message(request)))
        time.sleep(0.05)
    grandchild_pid: int | None = None
    if job.query.startswith("grandchild-"):
        mode, pid_path = job.query.split(":", 1)
        grandchild = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            close_fds=True,
        )
        grandchild_pid = grandchild.pid
        Path(pid_path).write_text(str(grandchild.pid), encoding="utf-8")
        if mode == "grandchild-crash":
            os._exit(23)
    if job.query.startswith("ignore-term:"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(job.query.split(":", 1)[1]).write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(30)
    if job.query == "crash":
        os._exit(23)
    if job.query == "block":
        time.sleep(30)
    if job.query == "invalid-result":
        return {"invalid": True}  # type: ignore[return-value]
    research_result = dict(_IMPORT_ENV)
    if grandchild_pid is not None:
        research_result["grandchild_pid"] = grandchild_pid
    return DeepResearchResult(
        query=job.query,
        report="fixture report",
        research_result=research_result,
        sources=({"title": "fixture"},),
        source_urls=("https://example.com",),
        context=("fixture context",),
        costs=0.25,
    )
