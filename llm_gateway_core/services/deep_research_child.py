"""Spawn child entrypoint; keep imports stdlib-only until job env is installed."""

from __future__ import annotations

import importlib
import os
import socket

from .deep_research_protocol import (
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
        sock.sendall(encode_frame(result_message(job_id, validated)))
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
