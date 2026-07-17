"""Contracts shared by the systemd migration planner and mutator."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


ENV_FILE_MODE = 0o640
FHS_DIRECTORY_MODE = 0o750
RUNTIME_DIRECTORY_MODE = 0o770
RUNTIME_FILE_MODE = 0o660

CONFIG_FILENAMES = (
    "providers.json",
    "models_fallback_rules.json",
    "models_operation_rules.json",
    "models_fusion_rules.json",
    "models_model_rules.json",
    "models_router_rules.json",
)
MANDATORY_CONFIG_FILENAMES = CONFIG_FILENAMES[:2]
KNOWN_DB_FILENAMES = (
    "tokens_usage.db",
    "api_keys.db",
    "llmgateway_rotation.db",
)
DEAD_LETTER_FILENAME = "tokens_usage.db.dead-letter.jsonl"
LOCK_FILENAME = ".llmgateway-single-process.lock"
CACHE_MANIFEST_FILENAME = ".migration-manifest.json"
OUTPUTS_MANIFEST_FILENAME = "outputs-images.manifest.json"
OUTPUTS_INCOMPLETE_MARKER = ".image-restore-incomplete"
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

RESERVED_ENVIRONMENT_KEYS = (
    "APP_DIR",
    "LLMGATEWAY_ENV_FILE",
    "GATEWAY_WORKERS",
    "GATEWAY_DB_DIR",
    "GATEWAY_OUTPUTS_DIR",
    "LLMGATEWAY_LOG_DIR",
    "CLOAKBROWSER_CACHE_DIR",
    "LLMGATEWAY_CONFIG_DIR",
    "PROVIDERS_FILENAME",
    "FALLBACK_RULES_FILENAME",
    "OPERATION_RULES_FILENAME",
    "FUSION_RULES_FILENAME",
    "MODEL_RULES_FILENAME",
    "ROUTER_RULES_FILENAME",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
)

_SAFE_DIAGNOSTIC_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MAX_DIAGNOSTIC_NAMES = 16


def _name_digest(value: str) -> str:
    encoded = value.encode("utf-8", errors="backslashreplace")
    return hashlib.sha256(encoded).hexdigest()[:16]


def sanitize_names(names: Sequence[str]) -> tuple[str, ...]:
    """Return deterministic bounded diagnostics without unsafe raw names."""

    raw_names = sorted(set(map(str, names)))
    visible = [
        name if _SAFE_DIAGNOSTIC_NAME.fullmatch(name) else f"name-sha256-{_name_digest(name)}"
        for name in raw_names[:_MAX_DIAGNOSTIC_NAMES]
    ]
    if len(raw_names) > _MAX_DIAGNOSTIC_NAMES:
        omitted = "\0".join(raw_names[_MAX_DIAGNOSTIC_NAMES:])
        visible.append(f"names-truncated-sha256-{_name_digest(omitted)}")
    return tuple(sorted(set(visible)))


class MigrationError(RuntimeError):
    """Operational error containing only a stable reason and safe names."""

    def __init__(self, reason: str, names: Sequence[str] = ()) -> None:
        self.reason = reason
        self.names = sanitize_names(names)
        suffix = f" names={','.join(self.names)}" if self.names else ""
        super().__init__(f"reason={reason}{suffix}")


@dataclass(frozen=True, slots=True)
class Ownership:
    env_uid: int
    service_uid: int
    service_gid: int


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class Artifact:
    name: str
    source: FileSnapshot | None
    target: FileSnapshot | None


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    source_root: Path
    target_env_dir: Path
    target_state_dir: Path
    target_cache_dir: Path
    source_cache_dir: Path | None
    environment: Artifact
    configs: tuple[Artifact, ...]
    databases: tuple[Artifact, ...]
    database_sidecars: tuple[tuple[str, tuple[FileSnapshot, ...]], ...]
    dead_letter: Artifact
    cache_manifest: dict[str, object] | None
    report: dict[str, object]


ImageMigrator = Callable[[Path, Path, Path], object]
