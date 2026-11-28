#!/usr/bin/env python3
"""Live network diagnostics for the free-search engines and paid web adapters.

Opt-in tool: performs real HTTP calls to every engine in ``FREE_ENGINES`` (and,
with ``--paid``, to the enabled built-in paid adapters) on a control query and
prints a status/latency table. Engines are called directly, never through
``run_engine``, so no cooldowns are written into the routing state. Not part of
CI; never invoked from tests.

Usage: ``.venv/bin/python scripts/check_web_adapters.py [--query ...] [--paid] [--timeout N]``
Exit code 0 when at least one free engine returns results, else 1.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FREE_ENGINES: dict[str, Any] = import_module("llm_gateway_core.api.v1.web_free_engines").FREE_ENGINES
settings: Any = import_module("llm_gateway_core.config.settings").settings
classify_engine_failure: Callable[[BaseException], str] = import_module(
    "llm_gateway_core.services.web_engine_routing"
).classify_engine_failure

DEFAULT_QUERY = "python httpx tutorial"
DEFAULT_TIMEOUT_SECONDS = 25.0
MAX_RESULTS = 5
MESSAGE_LIMIT = 120


@dataclass
class ProbeResult:
    name: str
    ok: bool
    count: int
    latency_ms: float
    failure_class: str = ""
    message: str = ""


def _short_message(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= MESSAGE_LIMIT:
        return collapsed
    return collapsed[: MESSAGE_LIMIT - 1] + "…"


async def probe_engine(name: str, factory: Callable[[], Awaitable[list[dict[str, Any]]]], timeout: float) -> ProbeResult:
    started = time.perf_counter()
    try:
        results = await asyncio.wait_for(factory(), timeout=timeout)
    except TimeoutError:
        # Outer per-engine budget (asyncio.wait_for), not an httpx timeout:
        # classify_engine_failure would map the builtin TimeoutError to "unknown".
        elapsed_ms = (time.perf_counter() - started) * 1000
        return ProbeResult(name, False, 0, elapsed_ms, "timeout", f"exceeded per-engine budget of {timeout:g}s")
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return ProbeResult(name, False, 0, elapsed_ms, classify_engine_failure(exc), _short_message(str(exc)))
    elapsed_ms = (time.perf_counter() - started) * 1000
    if not results:
        return ProbeResult(name, False, 0, elapsed_ms, "empty", "call succeeded but returned 0 results")
    return ProbeResult(name, True, len(results), elapsed_ms)


async def run_free_probes(query: str, timeout: float) -> list[ProbeResult]:
    searxng_kwargs: dict[str, Any] = {
        "instances": [part.strip() for part in settings.searxng_instances.split(",") if part.strip()],
        "bearer_token": settings.searxng_bearer_token,
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:

        async def one(name: str, engine: Any) -> ProbeResult:
            kwargs = searxng_kwargs if name == "searxng" else {}
            return await probe_engine(name, lambda: engine(client, query, MAX_RESULTS, **kwargs), timeout)

        return list(await asyncio.gather(*(one(name, engine) for name, engine in FREE_ENGINES.items())))


async def run_paid_probes(query: str, timeout: float) -> list[ProbeResult]:
    adapters = import_module("llm_gateway_core.api.v1.web_adapters")
    enabled = [name for name in adapters._BUILTIN_SEARCH_ADAPTERS if adapters._search_adapter_enabled(name)]
    if not enabled:
        return []
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:

        async def one(name: str) -> ProbeResult:
            search = adapters._SEARCH_ADAPTERS[name]
            return await probe_engine(name, lambda: search(client, query, MAX_RESULTS), timeout)

        return list(await asyncio.gather(*(one(name) for name in enabled)))


def print_table(title: str, rows: list[ProbeResult]) -> None:
    headers = ("engine", "status", "results", "latency_ms", "class", "message")
    cells = [
        (row.name, "OK" if row.ok else "FAIL", str(row.count), f"{row.latency_ms:.0f}", row.failure_class, row.message)
        for row in rows
    ]
    widths = [max(len(header), *(len(line[i]) for line in cells)) if cells else len(header)
              for i, header in enumerate(headers)]
    print(title)
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)).rstrip())
    for line in cells:
        print("  ".join(value.ljust(width) for value, width in zip(line, widths, strict=True)).rstrip())


async def run(args: argparse.Namespace) -> int:
    print(f"query: {args.query!r}  timeout: {args.timeout:g}s  max_results: {MAX_RESULTS}")
    print()
    free_rows = await run_free_probes(args.query, args.timeout)
    print_table("Free engines:", free_rows)
    ok_count = sum(1 for row in free_rows if row.ok)
    print(f"\nfree engines OK: {ok_count}/{len(free_rows)}")

    if args.paid:
        print()
        paid_rows = await run_paid_probes(args.query, args.timeout)
        if paid_rows:
            print_table("Paid adapters (enabled in settings):", paid_rows)
            paid_ok = sum(1 for row in paid_rows if row.ok)
            print(f"\npaid adapters OK: {paid_ok}/{len(paid_rows)}")
        else:
            print("Paid adapters: none enabled in settings (proxy/tavily/jina/zai).")

    return 0 if ok_count > 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", default=DEFAULT_QUERY, help=f"control query (default: {DEFAULT_QUERY!r})")
    parser.add_argument("--paid", action="store_true", help="also probe the paid adapters enabled in settings")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-engine budget in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
