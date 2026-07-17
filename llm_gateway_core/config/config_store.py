"""Compatibility facade for gateway configuration storage APIs."""

from __future__ import annotations

from .atomic_config_transaction import (
    AtomicConfigFileTransaction,
    AtomicConfigTransactionConflictError,
    AtomicConfigTransactionError,
    AtomicConfigTransactionIntegrityError,
    AtomicConfigTransactionState,
    AtomicConfigTransactionStateError,
)
from .config_sources import (
    ConfigDocument,
    ConfigFile,
    ConfigFileMetadata,
    ConfigSourceBundle,
    ConfigSourceError,
)
from .comments_backup import (
    CommentsBackupError,
    CommentsBackupLifecycle,
    CommentsBackupPublishResult,
    CommentsBackupState,
    CommentsBackupStateError,
)


__all__ = (
    "AtomicConfigFileTransaction",
    "AtomicConfigTransactionConflictError",
    "AtomicConfigTransactionError",
    "AtomicConfigTransactionIntegrityError",
    "AtomicConfigTransactionState",
    "AtomicConfigTransactionStateError",
    "CommentsBackupError",
    "CommentsBackupLifecycle",
    "CommentsBackupPublishResult",
    "CommentsBackupState",
    "CommentsBackupStateError",
    "ConfigDocument",
    "ConfigFile",
    "ConfigFileMetadata",
    "ConfigSourceBundle",
    "ConfigSourceError",
)
