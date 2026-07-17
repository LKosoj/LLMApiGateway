"""Early single-process bootstrap for the application entrypoint.

Loading the environment and validating the worker count are deliberately
not import-time side effects: both are still invoked explicitly, by
``main.lifespan`` and ``main.run_server``, before any DB state directory is
created or single-process lease is acquired. Note this guarantee does not
extend to logging: ``main`` calls ``configure_logging()`` (creating the
``logs/`` directory and opening a ``RotatingFileHandler``) as a module-level
side effect before either call site runs.
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
