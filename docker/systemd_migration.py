#!/usr/bin/env python3
"""Secret-safe, offline migration into the systemd filesystem layout."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if os.fspath(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(_PROJECT_ROOT))

from docker._systemd_migration_apply import (  # noqa: E402
    apply_plan,
    default_image_migrator,
)
from docker._systemd_migration_model import (  # noqa: E402, F401
    CACHE_MANIFEST_FILENAME,
    CONFIG_FILENAMES,
    DEAD_LETTER_FILENAME,
    ENV_FILE_MODE,
    FHS_DIRECTORY_MODE,
    KNOWN_DB_FILENAMES,
    LOCK_FILENAME,
    MANDATORY_CONFIG_FILENAMES,
    OUTPUTS_MANIFEST_FILENAME,
    RESERVED_ENVIRONMENT_KEYS,
    RUNTIME_DIRECTORY_MODE,
    RUNTIME_FILE_MODE,
    ImageMigrator,
    MigrationError,
    Ownership,
)
from docker._systemd_migration_plan import build_plan  # noqa: E402


ENV_UID = 0
SERVICE_UID = 10001
SERVICE_GID = 10001


class _RedactedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise MigrationError("invalid-arguments")


def _ownership() -> Ownership:
    return Ownership(
        env_uid=ENV_UID,
        service_uid=SERVICE_UID,
        service_gid=SERVICE_GID,
    )


def _default_image_migrator(source: Path, outputs: Path, manifest: Path) -> object:
    return default_image_migrator(source, outputs, manifest)


def inventory(
    *,
    source_root: str | os.PathLike[str],
    target_env_dir: str | os.PathLike[str],
    target_state_dir: str | os.PathLike[str],
    target_cache_dir: str | os.PathLike[str],
    source_cache_dir: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Return a deterministic, value-free shallow preflight report."""

    plan = build_plan(
        source_root=source_root,
        target_env_dir=target_env_dir,
        target_state_dir=target_state_dir,
        target_cache_dir=target_cache_dir,
        source_cache_dir=source_cache_dir,
        ownership=_ownership(),
        full=False,
    )
    return copy.deepcopy(plan.report)


def migrate(
    *,
    source_root: str | os.PathLike[str],
    target_env_dir: str | os.PathLike[str],
    target_state_dir: str | os.PathLike[str],
    target_cache_dir: str | os.PathLike[str],
    source_cache_dir: str | os.PathLike[str] | None = None,
    image_migrator: ImageMigrator | None = None,
) -> dict[str, object]:
    """Build a stopped-service plan and publish only absent known artifacts."""

    ownership = _ownership()
    plan = build_plan(
        source_root=source_root,
        target_env_dir=target_env_dir,
        target_state_dir=target_state_dir,
        target_cache_dir=target_cache_dir,
        source_cache_dir=source_cache_dir,
        ownership=ownership,
        full=True,
    )
    return apply_plan(plan, ownership, image_migrator or _default_image_migrator)


def build_parser() -> argparse.ArgumentParser:
    parser = _RedactedArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("inventory", "migrate"):
        subparser = subcommands.add_parser(command)
        subparser.add_argument("--source-root", required=True)
        subparser.add_argument("--target-env-dir", required=True)
        subparser.add_argument("--target-state-dir", required=True)
        subparser.add_argument("--target-cache-dir", required=True)
        subparser.add_argument("--source-cache-dir")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        kwargs = {
            "source_root": arguments.source_root,
            "target_env_dir": arguments.target_env_dir,
            "target_state_dir": arguments.target_state_dir,
            "target_cache_dir": arguments.target_cache_dir,
            "source_cache_dir": arguments.source_cache_dir,
        }
        report = inventory(**kwargs) if arguments.command == "inventory" else migrate(**kwargs)
    except MigrationError as error:
        payload = {"status": "error", "reason": error.reason, "names": list(error.names)}
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 1
    except Exception:
        payload = {"status": "error", "reason": "unexpected-error", "names": []}
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
