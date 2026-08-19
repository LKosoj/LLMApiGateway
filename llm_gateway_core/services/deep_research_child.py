"""Spawn child entrypoint; keep imports stdlib-only until job env is installed."""

from __future__ import annotations

import dataclasses
import importlib
import logging
import os
import socket

from .deep_research_protocol import (
    DEEP_RESEARCH_FRAME_MAX_BYTES,
    DeepResearchCallbackReportedError,
    DeepResearchProtocolError,
    DeepResearchResult,
    encode_frame,
    error_message,
    parse_run_message,
    ready_message,
    recv_frame,
    result_message,
)


logger = logging.getLogger(__name__)


_SYSTEM_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NO_PROXY",
)


def child_main(sock: socket.socket, adapter_module: str) -> None:
    job_id = "unknown"
    try:
        if not hasattr(os, "setsid"):
            raise RuntimeError("process groups are unavailable")
        os.setsid()
        inherited = {
            name: os.environ[name]
            for name in _SYSTEM_ENV_ALLOWLIST
            if name in os.environ
        }
        os.environ.clear()
        os.environ.update(inherited)
        sock.sendall(encode_frame(ready_message(os.getpid())))

        job = parse_run_message(recv_frame(sock))
        job_id = job.job_id
        os.environ.update(job.environment())

        adapter = importlib.import_module(adapter_module)
        run_job = getattr(adapter, "run_deep_research_job", None)
        if not callable(run_job):
            raise RuntimeError("adapter entrypoint is unavailable")
        result = run_job(job, sock)
        if not isinstance(result, DeepResearchResult):
            raise ValueError("adapter returned an invalid result type")
        validated = DeepResearchResult.from_wire(result.to_wire())
        sock.sendall(_result_frame(job_id, validated))
    except BaseException as exc:
        code = _error_code(exc)
        exception_type = type(exc).__name__
        if not exception_type.isascii() or not exception_type.isidentifier():
            exception_type = "Exception"
        try:
            sock.sendall(
                encode_frame(error_message(job_id, code, exception_type[:64]))
            )
        except BaseException:
            pass
    finally:
        sock.close()


def _drop_source_bodies(result: DeepResearchResult) -> DeepResearchResult:
    return dataclasses.replace(
        result,
        sources=tuple(
            {
                key: value
                for key, value in source.items()
                if key in {"url", "title"} and isinstance(value, str)
            }
            for source in result.sources
        ),
    )


def _drop_context(result: DeepResearchResult) -> DeepResearchResult:
    return dataclasses.replace(result, context=())


def _drop_research_result(result: DeepResearchResult) -> DeepResearchResult:
    return dataclasses.replace(result, research_result=None)


# Evidence the parent can live without, heaviest first. The report itself, the
# source urls and the costs are never shed: they are the answer the caller paid for.
_RESULT_REDUCTIONS = (
    ("scraped source bodies", _drop_source_bodies),
    ("research context", _drop_context),
    ("research tree", _drop_research_result),
)


def _result_frame(job_id: str, result: DeepResearchResult) -> bytes:
    """Never trade a finished report for an error: shed evidence until the frame fits."""
    try:
        return encode_frame(result_message(job_id, result))
    except DeepResearchProtocolError as exc:
        rejected = exc
    dropped: list[str] = []
    for name, reduce in _RESULT_REDUCTIONS:
        result = reduce(result)
        dropped.append(name)
        try:
            frame = encode_frame(result_message(job_id, result))
        except DeepResearchProtocolError:
            continue
        logger.warning(
            "deep-research result did not fit the %d byte frame; dropped %s to deliver the report",
            DEEP_RESEARCH_FRAME_MAX_BYTES,
            ", ".join(dropped),
        )
        return frame
    raise rejected


def _error_code(exc: BaseException) -> str:
    name = type(exc).__name__
    if isinstance(exc, DeepResearchCallbackReportedError):
        return "callback_failed"
    if isinstance(exc, DeepResearchProtocolError):
        return "invalid_job"
    if name in {
        "DeepResearchUnavailableError",
        "PackageNotFoundError",
        "ModuleNotFoundError",
        "ImportError",
    }:
        return "adapter_unavailable"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_result"
    return "child_failure"
