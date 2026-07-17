"""Single source of truth for project filesystem layout.

All modules that need to resolve paths relative to the project root should
import ``PROJECT_ROOT`` from here instead of hand-rolling
``Path(__file__).parent.parent.parent`` chains — those break whenever a
module is moved and hide the actual semantics.
"""
import os
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
"""Absolute path to the repository root (the directory that contains ``main.py``)."""


STATIC_DIR: Path = PROJECT_ROOT / "static"
"""Directory with static files served by FastAPI (HTML/JS/CSS)."""


def validate_safe_absolute_path(
    value: str | os.PathLike[str],
    *,
    name: str = "path",
) -> Path:
    """Return a non-root absolute path without unsafe lexical components."""
    raw_value = os.fspath(value)
    path = Path(raw_value)
    if not raw_value or not path.is_absolute():
        raise ValueError(f"{name} must be a non-empty absolute path")
    if ".." in path.parts:
        raise ValueError(f"{name} must not contain '..' components")
    if path == Path(path.anchor):
        raise ValueError(f"{name} must not be the filesystem root")
    if raw_value.endswith(os.sep):
        raise ValueError(f"{name} must not end with an empty path component")
    return path


def resolve_outputs_dir() -> Path:
    """Resolve the absolute persistent outputs root.

    Direct source runs default to the checkout-local ``outputs`` directory.
    An explicitly configured value must be non-empty and absolute so runtime
    storage never changes with the process working directory.
    """
    if "GATEWAY_OUTPUTS_DIR" not in os.environ:
        return PROJECT_ROOT / "outputs"

    raw_value = os.environ["GATEWAY_OUTPUTS_DIR"]
    value = raw_value.strip()
    if not value:
        raise ValueError("GATEWAY_OUTPUTS_DIR must be a non-empty absolute path")

    path = validate_safe_absolute_path(value, name="GATEWAY_OUTPUTS_DIR")
    resolved = path.resolve(strict=False)
    return validate_safe_absolute_path(resolved, name="GATEWAY_OUTPUTS_DIR")


OUTPUTS_DIR: Path = resolve_outputs_dir()
"""Absolute root for persistent generated output."""


OUTPUTS_IMAGES_DIR: Path = OUTPUTS_DIR / "images"
"""Directory where deep research stores generated PNG illustrations.

Served by FastAPI under ``/outputs/images/`` and subject to retention cleanup
(see ``llm_gateway_core.services.image_retention``)."""


def resolve_log_dir() -> Path:
    """Resolve the absolute application log root."""
    if "LLMGATEWAY_LOG_DIR" not in os.environ:
        return PROJECT_ROOT / "logs"

    raw_value = os.environ["LLMGATEWAY_LOG_DIR"]
    value = raw_value.strip()
    if not value:
        raise ValueError("LLMGATEWAY_LOG_DIR must be a non-empty absolute path")

    path = validate_safe_absolute_path(value, name="LLMGATEWAY_LOG_DIR")
    resolved = path.resolve(strict=False)
    return validate_safe_absolute_path(resolved, name="LLMGATEWAY_LOG_DIR")


def resolve_db_dir(module_file: str | None = None) -> Path:
    """Resolve the directory for SQLite DB files.

    Priority:
    1. ``GATEWAY_DB_DIR`` environment variable (used by tests and ops that
       need to redirect storage to a temporary or per-instance location).
    2. ``<module_file>/../../../db`` when ``module_file`` is provided
       (preserves legacy ``patch.object(module, "__file__", ...)`` tests).
    3. ``PROJECT_ROOT / "db"``.

    These SQLite files (notably ``api_keys.db``, which stores plaintext
    virtual API keys) must not be group/world-readable, so a directory created
    here is owner-only from the start.

    The mode of a directory that already exists is left untouched. Re-asserting
    it on every call would silently revert whatever the operator chose — and
    this runs on every call, not just at startup — so a deployment that widens
    the directory on purpose (say, to let an admin group list it) would find
    the change undone at the next restart, with nothing but a log line to
    explain it. Choosing the mode of a directory it did not create is not this
    process's decision to make.
    """
    env_dir = os.getenv("GATEWAY_DB_DIR")
    if env_dir:
        db_dir = Path(env_dir)
    elif module_file is not None:
        db_dir = Path(module_file).parent.parent.parent / "db"
    else:
        db_dir = PROJECT_ROOT / "db"

    os.makedirs(db_dir, mode=0o700, exist_ok=True)
    return db_dir
