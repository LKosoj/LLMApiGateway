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


OUTPUTS_IMAGES_DIR: Path = PROJECT_ROOT / "outputs" / "images"
"""Directory where deep research stores generated PNG illustrations.

Served by FastAPI under ``/outputs/images/`` and subject to retention cleanup
(see ``llm_gateway_core.services.image_retention``)."""


def resolve_db_dir(module_file: str | None = None) -> Path:
    """Resolve the directory for SQLite DB files.

    Priority:
    1. ``GATEWAY_DB_DIR`` environment variable (used by tests and ops that
       need to redirect storage to a temporary or per-instance location).
    2. ``<module_file>/../../../db`` when ``module_file`` is provided
       (preserves legacy ``patch.object(module, "__file__", ...)`` tests).
    3. ``PROJECT_ROOT / "db"``.
    """
    env_dir = os.getenv("GATEWAY_DB_DIR")
    if env_dir:
        return Path(env_dir)
    if module_file is not None:
        return Path(module_file).parent.parent.parent / "db"
    return PROJECT_ROOT / "db"
