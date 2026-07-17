#!/usr/bin/env python3
"""Read or verify the canonical LLMApiGateway product version."""

from __future__ import annotations

import argparse
import re
import sys
from importlib import import_module
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

__version__ = str(import_module("llm_gateway_core.version").__version__)


SEMANTIC_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def verify_expected_version(expected: str) -> None:
    if not expected:
        raise ValueError("expected product version must not be empty")
    if expected != expected.strip():
        raise ValueError("expected product version must not contain surrounding whitespace")
    if not SEMANTIC_VERSION_PATTERN.fullmatch(__version__):
        raise ValueError(f"canonical product version is not X.Y.Z: {__version__!r}")
    if expected != __version__:
        raise ValueError(
            f"product version mismatch: expected {expected!r}, canonical is {__version__!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--expected", help="fail unless this equals the canonical version")
    action.add_argument(
        "--print",
        action="store_true",
        dest="print_version",
        help="print the canonical version",
    )
    args = parser.parse_args()

    try:
        if args.expected is not None:
            verify_expected_version(args.expected)
        else:
            verify_expected_version(__version__)
            print(__version__)
    except ValueError as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
