"""Single source of truth for project filesystem layout.

All modules that need to resolve paths relative to the project root should
import ``PROJECT_ROOT`` from here instead of hand-rolling
``Path(__file__).parent.parent.parent`` chains — those break whenever a
module is moved and hide the actual semantics.
"""
from pathlib import Path


PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
"""Absolute path to the repository root (the directory that contains ``main.py``)."""


STATIC_DIR: Path = PROJECT_ROOT / "static"
"""Directory with static files served by FastAPI (HTML/JS/CSS)."""


OUTPUTS_IMAGES_DIR: Path = PROJECT_ROOT / "outputs" / "images"
"""Directory where deep research stores generated PNG illustrations.

Served by FastAPI under ``/outputs/images/`` and subject to retention cleanup
(see ``llm_gateway_core.services.image_retention``)."""
