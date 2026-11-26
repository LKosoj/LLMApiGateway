"""Early single-process bootstrap for the application entrypoint.

This module itself has no import-time side effects: validating the worker count
and acquiring the single-process lease are invoked explicitly by
``main.lifespan`` and ``main.run_server`` before any DB state directory is
created. Note that this guarantee does not extend to all module-level imports
in ``main``: ``main`` calls ``configure_logging()`` (creating the ``logs/``
directory and opening a ``RotatingFileHandler``) and imports ``settings.py``
(which loads ``.env`` / environment settings) at import time before either
call site runs.
"""

from __future__ import annotations

from .single_process import (
    SINGLE_APPLICATION_WORKER_COUNT,
    SingleProcessInvariantError,
    SingleProcessLease,
    validate_single_worker_environment,
)

__all__ = (
    "SINGLE_APPLICATION_WORKER_COUNT",
    "SingleProcessInvariantError",
    "SingleProcessLease",
    "validate_single_worker_environment",
)
