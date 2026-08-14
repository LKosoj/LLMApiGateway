"""Serialized file-and-runtime configuration updates."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx

from ..config.config_store import (
    AtomicConfigFileTransaction,
    AtomicConfigTransactionConflictError,
    AtomicConfigTransactionError,
    AtomicConfigTransactionState,
    CommentsBackupLifecycle,
    CommentsBackupState,
    ConfigFile,
    ConfigSourceBundle,
    ConfigSourceError,
)
from ..config.loader import ConfigError, ConfigLoader, RuleValidationError
from ..utils.log_redaction import safe_exception_type_name

if TYPE_CHECKING:
    from .runtime_candidate import RuntimeCandidate
    from .runtime_config import RuntimeGenerationManager, RuntimeSnapshot


logger = logging.getLogger(__name__)


ConfigCandidatePreflight = Callable[["RuntimeSnapshot"], Awaitable[None]]

# Matches container_preflight.py's convention for newly-created config files.
_NEW_CONFIG_FILE_MODE = 0o644


class ConfigUpdateState(StrEnum):
    """Process-lifetime state of the update coordinator."""

    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    BROKEN = "broken"


class ConfigUpdateErrorCode(StrEnum):
    """Stable, HTTP-agnostic failure codes for config writers."""

    VALIDATION_FAILED = "config_validation_failed"
    GENERATION_STALE = "config_generation_stale"
    REVISION_CONFLICT = "config_revision_conflict"
    SOURCES_OUT_OF_SYNC = "config_sources_out_of_sync"
    GENERATION_BUSY = "config_generation_busy"
    COMMIT_FAILED = "config_commit_failed"
    UPDATE_UNAVAILABLE = "config_update_unavailable"
    UPDATE_BROKEN = "config_update_broken"


_ERROR_MESSAGES = {
    ConfigUpdateErrorCode.VALIDATION_FAILED: "Configuration validation failed.",
    ConfigUpdateErrorCode.GENERATION_STALE: "The configuration generation is stale.",
    ConfigUpdateErrorCode.REVISION_CONFLICT: "The configuration revision changed.",
    ConfigUpdateErrorCode.SOURCES_OUT_OF_SYNC: (
        "The loaded configuration no longer matches the files on disk."
    ),
    ConfigUpdateErrorCode.GENERATION_BUSY: "Too many runtime generations are still retiring.",
    ConfigUpdateErrorCode.COMMIT_FAILED: "The configuration update could not be committed.",
    ConfigUpdateErrorCode.UPDATE_UNAVAILABLE: "Configuration updates are unavailable.",
    ConfigUpdateErrorCode.UPDATE_BROKEN: "Configuration update integrity is not proven.",
}


class ConfigUpdateError(RuntimeError):
    """Credential-safe failure exposed by the coordinator boundary."""

    def __init__(
        self,
        code: ConfigUpdateErrorCode,
        *,
        errors: tuple[dict[str, object], ...] | None = None,
    ) -> None:
        if not isinstance(code, ConfigUpdateErrorCode):
            raise TypeError("code must be a ConfigUpdateErrorCode")
        self.code = code
        self.errors = errors
        super().__init__(_ERROR_MESSAGES[code])


def _rule_validation_errors(
    error: ConfigError,
) -> tuple[dict[str, object], ...] | None:
    """Surface a cross-validation message, and only that.

    The loader wraps every candidate failure in the same ``ConfigError``, so the
    marker type on the cause is what separates a message that names which
    gateway model rejected which target — safe, and the only way an operator can
    fix the rule — from a json5 or Pydantic failure, which quotes raw input and
    therefore stays behind the frozen generic message.
    """
    cause = error.__cause__
    if not isinstance(cause, RuleValidationError):
        return None
    return ({"type": "rule_validation", "loc": [], "msg": str(cause)},)


@dataclass(frozen=True, slots=True)
class ConfigRevision:
    """Strong revision parsed by an HTTP boundary before coordinator admission."""

    config_file: ConfigFile
    digest: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.config_file, ConfigFile):
            raise TypeError("config_file must be a ConfigFile")
        if self.digest is None:
            return
        if (
            not isinstance(self.digest, str)
            or len(self.digest) != 64
            or any(character not in "0123456789abcdef" for character in self.digest)
        ):
            raise ValueError("digest must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class ConfigUpdateDiagnostic:
    """Bounded, credential-safe last-failure information."""

    phase: str
    exception_type: str


@dataclass(frozen=True, slots=True)
class ConfigUpdateStatusSnapshot:
    """Immutable status intended for a future health response."""

    state: ConfigUpdateState
    accepting: bool
    active_updates: int
    pending_cleanup: int
    last_failure: ConfigUpdateDiagnostic | None


@dataclass(frozen=True, slots=True)
class ConfigUpdateResult:
    """Published generation and its post-publish cleanup state."""

    snapshot: RuntimeSnapshot = field(repr=False)
    cleanup_pending: bool
    comments_backup: str | None = field(default=None, repr=False)


class _PendingCleanupAction(StrEnum):
    ABORT = "abort"
    ROLLBACK = "rollback"
    FINALIZE = "finalize"


class _PendingCommentsBackupAction(StrEnum):
    ABORT = "abort"
    PUBLISH = "publish"
    RETRY = "retry"


class _RuntimePublishOutcome(StrEnum):
    PUBLISHED = "published"
    NOT_PUBLISHED = "not_published"
    AMBIGUOUS = "ambiguous"


@dataclass(slots=True)
class _PendingCleanup:
    transaction: AtomicConfigFileTransaction
    action: _PendingCleanupAction


@dataclass(slots=True)
class _PendingCommentsBackup:
    lifecycle: CommentsBackupLifecycle
    action: _PendingCommentsBackupAction


class ConfigUpdateCoordinator:
    """Build candidates concurrently and serialize transaction publication."""

    def __init__(
        self,
        *,
        runtime_manager: RuntimeGenerationManager,
        shared_http_client: httpx.AsyncClient,
        initial_snapshot: RuntimeSnapshot,
    ) -> None:
        from .runtime_config import RuntimeGenerationManager, RuntimeSnapshot

        if not isinstance(runtime_manager, RuntimeGenerationManager):
            raise TypeError("runtime_manager must be a RuntimeGenerationManager")
        if not isinstance(shared_http_client, httpx.AsyncClient):
            raise TypeError("shared_http_client must be an httpx.AsyncClient")
        if not isinstance(initial_snapshot, RuntimeSnapshot):
            raise TypeError("initial_snapshot must be a RuntimeSnapshot")
        if runtime_manager.current_generation != initial_snapshot.generation:
            raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
        initial_bundle = initial_snapshot.config_loader.source_bundle
        if not isinstance(initial_bundle, ConfigSourceBundle):
            raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)

        try:
            for config_file in ConfigFile:
                AtomicConfigFileTransaction.cleanup_orphans(
                    initial_bundle[config_file]
                )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE) from None

        self._runtime_manager = runtime_manager
        self._shared_http_client = shared_http_client
        self._loop: asyncio.AbstractEventLoop | None = None
        self._state = ConfigUpdateState.RUNNING
        self._active_updates = 0
        self._active_updates_drained = asyncio.Event()
        self._active_updates_drained.set()
        self._commit_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._pending_cleanup: _PendingCleanup | None = None
        self._pending_comments_backup: _PendingCommentsBackup | None = None
        self._restart_required = False
        self._last_failure: ConfigUpdateDiagnostic | None = None

    @property
    def status_snapshot(self) -> ConfigUpdateStatusSnapshot:
        return ConfigUpdateStatusSnapshot(
            state=self._state,
            accepting=self._accepting_updates(),
            active_updates=self._active_updates,
            pending_cleanup=(
                int(self._pending_cleanup is not None)
                + int(self._pending_comments_backup is not None)
                + len(self._runtime_manager.unresolved_cleanup_generations)
            ),
            last_failure=self._last_failure,
        )

    def check_base(
        self,
        *,
        base_snapshot: RuntimeSnapshot,
        config_file: ConfigFile,
        expected_revision: ConfigRevision | None = None,
    ) -> None:
        """Validate an update base without admission, I/O, or candidate work."""
        self._bind_loop()
        self._raise_if_not_accepting()
        self._validate_base_input(
            base_snapshot=base_snapshot,
            config_file=config_file,
            expected_revision=expected_revision,
        )
        base_bundle = self._require_source_bundle(base_snapshot)
        self._validate_early_revision(
            base_snapshot=base_snapshot,
            base_bundle=base_bundle,
            config_file=config_file,
            expected_revision=expected_revision,
        )

    def check_sources(
        self,
        *,
        base_snapshot: RuntimeSnapshot,
        config_file: ConfigFile,
    ) -> None:
        """Reject early when the disk no longer matches the loaded sources.

        The same check runs again under the commit lock; this one only lets
        a writer fail before paying for candidate construction.
        """
        self._validate_base_input(
            base_snapshot=base_snapshot,
            config_file=config_file,
            expected_revision=None,
        )
        base_bundle = self._require_source_bundle(base_snapshot)
        self._validate_source_digests(
            base_bundle=base_bundle,
            config_file=config_file,
        )

    async def update(
        self,
        *,
        base_snapshot: RuntimeSnapshot,
        config_file: ConfigFile,
        candidate_bytes: bytes,
        expected_revision: ConfigRevision | None = None,
        comments_backup: bool = False,
        preflight: ConfigCandidatePreflight | None = None,
    ) -> ConfigUpdateResult:
        self._admit_update()
        candidate: RuntimeCandidate | None = None
        transaction: AtomicConfigFileTransaction | None = None
        backup: CommentsBackupLifecycle | None = None
        transaction_cleanup_attempted = False
        backup_cleanup_attempted = False
        publish_attempted = False
        pending_candidate_slots = 0
        published = False
        try:
            self._validate_update_input(
                base_snapshot=base_snapshot,
                config_file=config_file,
                candidate_bytes=candidate_bytes,
                expected_revision=expected_revision,
                comments_backup=comments_backup,
            )
            base_bundle = self._require_source_bundle(base_snapshot)
            self._validate_early_revision(
                base_snapshot=base_snapshot,
                base_bundle=base_bundle,
                config_file=config_file,
                expected_revision=expected_revision,
            )

            candidate_bundle = base_bundle.with_candidate(
                config_file,
                candidate_bytes,
            )
            try:
                candidate_loader = ConfigLoader.from_source_bundle(
                    candidate_bundle
                ).load_complete()
            except ConfigError as exc:
                raise ConfigUpdateError(
                    ConfigUpdateErrorCode.VALIDATION_FAILED,
                    errors=_rule_validation_errors(exc),
                ) from None

            candidate = await self._build_candidate(
                base_snapshot=base_snapshot,
                candidate_loader=candidate_loader,
            )
            if preflight is not None:
                try:
                    await preflight(candidate.snapshot)
                except ConfigUpdateError:
                    raise
                except BaseException as exc:
                    if not isinstance(exc, Exception):
                        raise
                    logger.exception(
                        "Configuration preflight rejected candidate for %s.",
                        config_file.value,
                    )
                    raise ConfigUpdateError(
                        ConfigUpdateErrorCode.VALIDATION_FAILED,
                        errors=(
                            {
                                "type": "preflight",
                                "loc": [],
                                "msg": str(exc),
                            },
                        ),
                    ) from None

            await self._commit_lock.acquire()
            try:
                try:
                    self._validate_commit_preconditions(
                        base_snapshot=base_snapshot,
                        base_bundle=base_bundle,
                        config_file=config_file,
                        expected_revision=expected_revision,
                        candidate=candidate,
                    )
                    if comments_backup:
                        backup = CommentsBackupLifecycle.begin(
                            base_bundle[config_file]
                        )
                        if backup is not None:
                            backup.prepare()
                    transaction = AtomicConfigFileTransaction.begin(
                        base_bundle[config_file],
                        candidate_bytes,
                        new_file_mode=_NEW_CONFIG_FILE_MODE,
                    )
                    transaction.prepare()
                    self._commit_prepared_transaction(
                        candidate=candidate,
                        transaction=transaction,
                    )
                    self._register_publication_owners(
                        transaction=transaction,
                        backup=backup,
                    )
                    pending_candidate_slots = (
                        self._runtime_manager.pending_unpublished_generations.count(
                            candidate.snapshot.generation
                        )
                    )
                    publish_attempted = True
                    published_snapshot = candidate.publish(
                        expected_generation=base_snapshot.generation
                    )
                    self._promote_publication_owners(
                        transaction=transaction,
                        backup=backup,
                    )
                    published = True
                except BaseException as exc:
                    if published:
                        raise
                    if publish_attempted:
                        outcome = self._classify_runtime_publish_outcome(
                            base_snapshot=base_snapshot,
                            candidate=candidate,
                            pending_candidate_slots=pending_candidate_slots,
                        )
                        if outcome is _RuntimePublishOutcome.PUBLISHED:
                            published_snapshot = candidate.snapshot
                            self._promote_publication_owners(
                                transaction=transaction,
                                backup=backup,
                            )
                            published = True
                            if not isinstance(exc, Exception):
                                raise
                        elif outcome is _RuntimePublishOutcome.AMBIGUOUS:
                            self._promote_publication_owners(
                                transaction=transaction,
                                backup=backup,
                            )
                            published = True
                            self._mark_publication_outcome_ambiguous(
                                exception=exc,
                            )
                            if not isinstance(exc, Exception):
                                raise
                            raise ConfigUpdateError(
                                ConfigUpdateErrorCode.UPDATE_BROKEN
                            ) from None
                    if not published:
                        primary = self._translate_commit_exception(exc)
                        if transaction is not None:
                            cleanup_error = self._resolve_failed_transaction(transaction)
                            transaction_cleanup_attempted = True
                            if cleanup_error is not None:
                                self._mark_broken(
                                    phase="transaction_recovery",
                                    exception=cleanup_error,
                                    transaction=transaction,
                                )
                                if isinstance(primary, Exception):
                                    primary = (
                                        ConfigUpdateError(
                                            ConfigUpdateErrorCode.UPDATE_BROKEN
                                        )
                                        if isinstance(cleanup_error, Exception)
                                        else cleanup_error
                                    )
                            elif (
                                self._pending_cleanup is not None
                                and self._pending_cleanup.transaction is transaction
                            ):
                                self._pending_cleanup = None
                        if backup is not None:
                            backup_error = self._resolve_failed_comments_backup(backup)
                            backup_cleanup_attempted = True
                            if (
                                backup_error is not None
                                and not isinstance(backup_error, Exception)
                                and isinstance(primary, Exception)
                            ):
                                primary = backup_error
                        raise primary

                comments_backup_name: str | None = None
                postpublish_terminal: BaseException | None = None
                if backup is not None:
                    self._pending_comments_backup = _PendingCommentsBackup(
                        lifecycle=backup,
                        action=_PendingCommentsBackupAction.PUBLISH,
                    )
                    try:
                        backup_result = backup.publish()
                    except BaseException as exc:
                        self._last_failure = self._diagnostic(
                            "comments_backup_publish",
                            exc,
                        )
                        if backup.cleanup_pending:
                            self._pending_comments_backup.action = (
                                _PendingCommentsBackupAction.RETRY
                            )
                        if not isinstance(exc, Exception):
                            postpublish_terminal = exc
                    else:
                        comments_backup_name = backup_result.basename
                        if backup_result.cleanup_pending:
                            self._pending_comments_backup.action = (
                                _PendingCommentsBackupAction.RETRY
                            )
                            self._last_failure = ConfigUpdateDiagnostic(
                                phase="comments_backup_publish",
                                exception_type="IncompleteCleanup",
                            )
                        else:
                            self._pending_comments_backup = None
                if postpublish_terminal is not None:
                    raise postpublish_terminal
                try:
                    transaction.finalize()
                except BaseException as exc:
                    self._last_failure = self._diagnostic("finalize", exc)
                    if not isinstance(exc, Exception):
                        raise
                else:
                    self._pending_cleanup = None
                return ConfigUpdateResult(
                    snapshot=published_snapshot,
                    cleanup_pending=(
                        self._pending_cleanup is not None
                        or self._pending_comments_backup is not None
                    ),
                    comments_backup=comments_backup_name,
                )
            finally:
                self._commit_lock.release()
        except BaseException as primary:
            replacement: BaseException = primary
            if (
                publish_attempted
                and not published
                and transaction is not None
                and candidate is not None
            ):
                outcome = self._classify_runtime_publish_outcome(
                    base_snapshot=base_snapshot,
                    candidate=candidate,
                    pending_candidate_slots=pending_candidate_slots,
                )
                if outcome is _RuntimePublishOutcome.PUBLISHED:
                    self._promote_publication_owners(
                        transaction=transaction,
                        backup=backup,
                    )
                    published = True
                elif outcome is _RuntimePublishOutcome.AMBIGUOUS:
                    self._promote_publication_owners(
                        transaction=transaction,
                        backup=backup,
                    )
                    published = True
                    self._mark_publication_outcome_ambiguous(
                        exception=primary,
                    )
                    if isinstance(primary, Exception):
                        replacement = ConfigUpdateError(
                            ConfigUpdateErrorCode.UPDATE_BROKEN
                        )
            if (
                transaction is not None
                and not transaction_cleanup_attempted
                and not published
            ):
                cleanup_error = self._resolve_failed_transaction(transaction)
                if cleanup_error is not None:
                    self._mark_broken(
                        phase="transaction_recovery",
                        exception=cleanup_error,
                        transaction=transaction,
                    )
                    if isinstance(primary, Exception):
                        replacement = (
                            ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_BROKEN)
                            if isinstance(cleanup_error, Exception)
                            else cleanup_error
                        )
                elif (
                    self._pending_cleanup is not None
                    and self._pending_cleanup.transaction is transaction
                ):
                    self._pending_cleanup = None
            if backup is not None and not backup_cleanup_attempted and not published:
                backup_error = self._resolve_failed_comments_backup(backup)
                if (
                    backup_error is not None
                    and not isinstance(backup_error, Exception)
                    and isinstance(replacement, Exception)
                ):
                    replacement = backup_error
            if candidate is not None and not published:
                cleanup_terminal = await self._close_candidate(candidate)
                if cleanup_terminal is not None and isinstance(replacement, Exception):
                    replacement = cleanup_terminal
            raise replacement
        finally:
            self._finish_update()

    async def resync(
        self,
        *,
        base_snapshot: RuntimeSnapshot,
    ) -> ConfigUpdateResult:
        """Republish the runtime from the current on-disk sources.

        Writers refuse to commit while the loaded sources differ from the
        files on disk, and nothing else reloads them: without this, an
        out-of-band edit locks every configuration editor until the process
        restarts. No file is written here — the disk is the input, and the
        only outcome is a new runtime generation that matches it.
        """
        self._admit_update()
        candidate: RuntimeCandidate | None = None
        pending_candidate_slots = 0
        publish_attempted = False
        published = False
        try:
            self._validate_base_input(
                base_snapshot=base_snapshot,
                config_file=ConfigFile.PROVIDERS,
                expected_revision=None,
            )
            base_bundle = self._require_source_bundle(base_snapshot)
            self._raise_if_not_accepting()
            self._validate_manager_generation(base_snapshot.generation)

            try:
                disk_bundle = base_bundle.recapture()
            except ConfigSourceError:
                raise ConfigUpdateError(
                    ConfigUpdateErrorCode.REVISION_CONFLICT
                ) from None
            disk_digests = disk_bundle.content_digests()
            if disk_digests == base_bundle.content_digests():
                return ConfigUpdateResult(
                    snapshot=base_snapshot,
                    cleanup_pending=False,
                )

            try:
                disk_loader = ConfigLoader.from_source_bundle(
                    disk_bundle
                ).load_complete()
            except ConfigError as exc:
                logger.warning(
                    "Configuration resync rejected the on-disk sources: %s.",
                    safe_exception_type_name(exc),
                )
                raise ConfigUpdateError(
                    ConfigUpdateErrorCode.VALIDATION_FAILED,
                    errors=_rule_validation_errors(exc),
                ) from None

            candidate = await self._build_candidate(
                base_snapshot=base_snapshot,
                candidate_loader=disk_loader,
            )

            await self._commit_lock.acquire()
            try:
                self._raise_if_admitted_update_cannot_commit()
                self._validate_manager_generation(base_snapshot.generation)
                try:
                    recheck_bundle = base_bundle.recapture()
                except ConfigSourceError:
                    raise ConfigUpdateError(
                        ConfigUpdateErrorCode.REVISION_CONFLICT
                    ) from None
                if recheck_bundle.content_digests() != disk_digests:
                    raise ConfigUpdateError(
                        ConfigUpdateErrorCode.REVISION_CONFLICT
                    )

                from .runtime_config import (
                    RuntimeGenerationConflictError,
                    RuntimeManagerStateError,
                )

                try:
                    self._runtime_manager.validate_publish(
                        candidate.snapshot,
                        expected_generation=base_snapshot.generation,
                    )
                except RuntimeGenerationConflictError:
                    raise ConfigUpdateError(
                        ConfigUpdateErrorCode.GENERATION_STALE
                    ) from None
                except RuntimeManagerStateError:
                    raise ConfigUpdateError(
                        ConfigUpdateErrorCode.GENERATION_BUSY
                    ) from None

                pending_candidate_slots = (
                    self._runtime_manager.pending_unpublished_generations.count(
                        candidate.snapshot.generation
                    )
                )
                publish_attempted = True
                published_snapshot = candidate.publish(
                    expected_generation=base_snapshot.generation
                )
                published = True
            finally:
                self._commit_lock.release()

            logger.info(
                "Configuration resynced from disk into generation %d.",
                published_snapshot.generation,
            )
            return ConfigUpdateResult(
                snapshot=published_snapshot,
                cleanup_pending=(
                    self._pending_cleanup is not None
                    or self._pending_comments_backup is not None
                ),
            )
        except BaseException as primary:
            replacement: BaseException = primary
            if publish_attempted and not published and candidate is not None:
                outcome = self._classify_runtime_publish_outcome(
                    base_snapshot=base_snapshot,
                    candidate=candidate,
                    pending_candidate_slots=pending_candidate_slots,
                )
                if outcome is _RuntimePublishOutcome.PUBLISHED:
                    published = True
                elif outcome is _RuntimePublishOutcome.AMBIGUOUS:
                    published = True
                    self._mark_publication_outcome_ambiguous(exception=primary)
                    if isinstance(primary, Exception):
                        replacement = ConfigUpdateError(
                            ConfigUpdateErrorCode.UPDATE_BROKEN
                        )
            if candidate is not None and not published:
                cleanup_terminal = await self._close_candidate(candidate)
                if cleanup_terminal is not None and isinstance(
                    replacement,
                    Exception,
                ):
                    replacement = cleanup_terminal
            raise replacement
        finally:
            self._finish_update()

    async def close(self) -> None:
        loop = self._bind_loop()
        if self._state is ConfigUpdateState.STOPPED:
            return
        if self._close_task is not None and self._close_task.done():
            try:
                self._close_task.result()
            except BaseException:
                pass
            self._close_task = None
        if self._close_task is None:
            previous_state = self._state
            if not self._restart_required:
                self._state = ConfigUpdateState.STOPPING
            close_coroutine = self._close_impl()
            try:
                self._close_task = loop.create_task(
                    close_coroutine,
                    name="config-update-coordinator-close",
                )
            except BaseException:
                close_coroutine.close()
                self._state = previous_state
                raise
        await asyncio.shield(self._close_task)

    def _admit_update(self) -> None:
        self._bind_loop()
        self._raise_if_not_accepting()
        if self._active_updates == 0:
            self._active_updates_drained.clear()
        self._active_updates += 1

    def _finish_update(self) -> None:
        self._active_updates -= 1
        if self._active_updates == 0:
            self._active_updates_drained.set()

    def _accepting_updates(self) -> bool:
        return (
            self._state is ConfigUpdateState.RUNNING
            and self._pending_cleanup is None
            and self._pending_comments_backup is None
            and not self._runtime_manager.unresolved_cleanup_generations
        )

    def _raise_if_not_accepting(self) -> None:
        if self._state is ConfigUpdateState.BROKEN:
            raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_BROKEN)
        if not self._accepting_updates():
            raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)

    def _raise_if_admitted_update_cannot_commit(self) -> None:
        if self._state is ConfigUpdateState.BROKEN:
            raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_BROKEN)
        if (
            self._state not in {ConfigUpdateState.RUNNING, ConfigUpdateState.STOPPING}
            or self._pending_cleanup is not None
            or self._pending_comments_backup is not None
            or self._runtime_manager.unresolved_cleanup_generations
        ):
            raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise ConfigUpdateError(
                ConfigUpdateErrorCode.UPDATE_UNAVAILABLE
            ) from exc
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
        return loop

    @staticmethod
    def _validate_base_input(
        *,
        base_snapshot: RuntimeSnapshot,
        config_file: ConfigFile,
        expected_revision: ConfigRevision | None,
    ) -> None:
        from .runtime_config import RuntimeSnapshot

        if not isinstance(base_snapshot, RuntimeSnapshot):
            raise TypeError("base_snapshot must be a RuntimeSnapshot")
        if not isinstance(config_file, ConfigFile):
            raise TypeError("config_file must be a ConfigFile")
        if expected_revision is not None:
            if not isinstance(expected_revision, ConfigRevision):
                raise TypeError("expected_revision must be a ConfigRevision")
            if expected_revision.config_file is not config_file:
                raise ValueError("expected revision config identity mismatch")

    @classmethod
    def _validate_update_input(
        cls,
        *,
        base_snapshot: RuntimeSnapshot,
        config_file: ConfigFile,
        candidate_bytes: bytes,
        expected_revision: ConfigRevision | None,
        comments_backup: bool,
    ) -> None:
        cls._validate_base_input(
            base_snapshot=base_snapshot,
            config_file=config_file,
            expected_revision=expected_revision,
        )
        if not isinstance(candidate_bytes, bytes):
            raise TypeError("candidate_bytes must be bytes")
        if type(comments_backup) is not bool:
            raise TypeError("comments_backup must be a bool")

    @staticmethod
    def _require_source_bundle(
        base_snapshot: RuntimeSnapshot,
    ) -> ConfigSourceBundle:
        bundle = base_snapshot.config_loader.source_bundle
        if not isinstance(bundle, ConfigSourceBundle):
            raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
        return bundle

    def _validate_early_revision(
        self,
        *,
        base_snapshot: RuntimeSnapshot,
        base_bundle: ConfigSourceBundle,
        config_file: ConfigFile,
        expected_revision: ConfigRevision | None,
    ) -> None:
        self._raise_if_not_accepting()
        self._validate_manager_generation(base_snapshot.generation)
        self._validate_expected_revision(
            base_bundle,
            config_file,
            expected_revision,
        )

    def _validate_commit_preconditions(
        self,
        *,
        base_snapshot: RuntimeSnapshot,
        base_bundle: ConfigSourceBundle,
        config_file: ConfigFile,
        expected_revision: ConfigRevision | None,
        candidate: RuntimeCandidate,
    ) -> None:
        self._raise_if_admitted_update_cannot_commit()
        self._validate_manager_generation(base_snapshot.generation)
        self._validate_expected_revision(
            base_bundle,
            config_file,
            expected_revision,
        )
        self._validate_source_digests(
            base_bundle=base_bundle,
            config_file=config_file,
        )

        from .runtime_config import (
            RuntimeGenerationConflictError,
            RuntimeManagerStateError,
        )

        try:
            self._runtime_manager.validate_publish(
                candidate.snapshot,
                expected_generation=base_snapshot.generation,
            )
        except RuntimeGenerationConflictError:
            raise ConfigUpdateError(
                ConfigUpdateErrorCode.GENERATION_STALE
            ) from None
        except RuntimeManagerStateError:
            raise ConfigUpdateError(
                ConfigUpdateErrorCode.GENERATION_BUSY
            ) from None

    def _validate_manager_generation(self, expected_generation: int) -> None:
        from .runtime_config import RuntimeManagerStatus

        if self._runtime_manager.status is not RuntimeManagerStatus.RUNNING:
            raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
        if self._runtime_manager.current_generation != expected_generation:
            raise ConfigUpdateError(ConfigUpdateErrorCode.GENERATION_STALE)

    @staticmethod
    def _validate_source_digests(
        *,
        base_bundle: ConfigSourceBundle,
        config_file: ConfigFile,
    ) -> None:
        """Reject a write whose validation base no longer matches the disk.

        Only content identity is compared: the candidate was validated
        against the bytes of all six sources, so bytes are what must still
        hold. Filesystem metadata is deliberately excluded — a ``chmod`` or
        an atomic replacement with identical bytes changes nothing the
        candidate depends on.

        Drift here always means the loaded snapshot fell behind the disk,
        and that holds for the file being written just as much as for the
        other five: re-reading through the API only ever serves the loaded
        snapshot, so a reload cannot clear it and only a resync can.
        ``REVISION_CONFLICT`` stays reserved for a stale ``If-Match`` — the
        one conflict a reload does fix.
        """
        try:
            current_bundle = base_bundle.recapture()
        except ConfigSourceError:
            raise ConfigUpdateError(
                ConfigUpdateErrorCode.REVISION_CONFLICT
            ) from None

        current_digests = current_bundle.content_digests()
        base_digests = base_bundle.content_digests()
        drifted = tuple(
            candidate_file
            for candidate_file in ConfigFile
            if current_digests[candidate_file] != base_digests[candidate_file]
        )
        if not drifted:
            return
        logger.warning(
            "Loaded configuration no longer matches disk for %s; "
            "an update of %s cannot proceed until the runtime is resynced.",
            ", ".join(drifted_file.value for drifted_file in drifted),
            config_file.value,
        )
        raise ConfigUpdateError(
            ConfigUpdateErrorCode.SOURCES_OUT_OF_SYNC,
            errors=tuple(
                {
                    "type": "source_drift",
                    "loc": [drifted_file.value],
                    "msg": (
                        f"{drifted_file.value} on disk differs from the "
                        "configuration this process loaded"
                    ),
                }
                for drifted_file in drifted
            ),
        )

    @staticmethod
    def _validate_expected_revision(
        bundle: ConfigSourceBundle,
        config_file: ConfigFile,
        expected_revision: ConfigRevision | None,
    ) -> None:
        if expected_revision is None:
            return
        document = bundle[config_file]
        actual_digest = document.digest if document.exists else None
        if expected_revision.digest != actual_digest:
            raise ConfigUpdateError(ConfigUpdateErrorCode.REVISION_CONFLICT)

    async def _build_candidate(
        self,
        *,
        base_snapshot: RuntimeSnapshot,
        candidate_loader: ConfigLoader,
    ) -> RuntimeCandidate:
        from .http_client_factory import ProxyConfigurationError
        from .runtime_candidate import build_runtime_candidate
        from .runtime_config import (
            RuntimeGenerationConflictError,
            RuntimeManagerStateError,
            RuntimeManagerStatus,
        )

        try:
            return await build_runtime_candidate(
                manager=self._runtime_manager,
                generation=base_snapshot.generation + 1,
                config_loader=candidate_loader,
                shared_http_client=self._shared_http_client,
            )
        except ProxyConfigurationError:
            raise ConfigUpdateError(
                ConfigUpdateErrorCode.VALIDATION_FAILED
            ) from None
        except RuntimeGenerationConflictError:
            raise ConfigUpdateError(
                ConfigUpdateErrorCode.GENERATION_STALE
            ) from None
        except RuntimeManagerStateError:
            code = (
                ConfigUpdateErrorCode.GENERATION_BUSY
                if self._runtime_manager.status is RuntimeManagerStatus.RUNNING
                else ConfigUpdateErrorCode.UPDATE_UNAVAILABLE
            )
            raise ConfigUpdateError(code) from None
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise ConfigUpdateError(ConfigUpdateErrorCode.COMMIT_FAILED) from None

    def _commit_prepared_transaction(
        self,
        *,
        candidate: RuntimeCandidate,
        transaction: AtomicConfigFileTransaction,
    ) -> None:
        transaction.commit()
        candidate_bundle = candidate.snapshot.config_loader.source_bundle
        if not isinstance(candidate_bundle, ConfigSourceBundle):
            raise ConfigUpdateError(ConfigUpdateErrorCode.COMMIT_FAILED)
        try:
            persisted_bundle = candidate_bundle.recapture()
        except ConfigSourceError:
            raise ConfigUpdateError(
                ConfigUpdateErrorCode.REVISION_CONFLICT
            ) from None
        candidate.snapshot.config_loader.bind_persisted_source_bundle(
            persisted_bundle
        )

    def _register_publication_owners(
        self,
        *,
        transaction: AtomicConfigFileTransaction,
        backup: CommentsBackupLifecycle | None,
    ) -> None:
        self._pending_cleanup = _PendingCleanup(
            transaction=transaction,
            action=_PendingCleanupAction.ROLLBACK,
        )
        if backup is not None:
            self._pending_comments_backup = _PendingCommentsBackup(
                lifecycle=backup,
                action=_PendingCommentsBackupAction.ABORT,
            )

    def _promote_publication_owners(
        self,
        *,
        transaction: AtomicConfigFileTransaction,
        backup: CommentsBackupLifecycle | None,
    ) -> None:
        pending_cleanup = self._pending_cleanup
        if (
            pending_cleanup is None
            or pending_cleanup.transaction is not transaction
        ):
            self._pending_cleanup = _PendingCleanup(
                transaction=transaction,
                action=_PendingCleanupAction.FINALIZE,
            )
        else:
            pending_cleanup.action = _PendingCleanupAction.FINALIZE
        if backup is not None:
            pending_backup = self._pending_comments_backup
            if (
                pending_backup is None
                or pending_backup.lifecycle is not backup
            ):
                self._pending_comments_backup = _PendingCommentsBackup(
                    lifecycle=backup,
                    action=_PendingCommentsBackupAction.PUBLISH,
                )
            else:
                pending_backup.action = _PendingCommentsBackupAction.PUBLISH

    def _classify_runtime_publish_outcome(
        self,
        *,
        base_snapshot: RuntimeSnapshot,
        candidate: RuntimeCandidate,
        pending_candidate_slots: int,
    ) -> _RuntimePublishOutcome:
        from .runtime_config import RuntimeGenerationStatus

        lease = None
        inspection_failed = False
        try:
            lease = self._runtime_manager.acquire_current()
            current_snapshot = lease.snapshot
            pending_after = (
                self._runtime_manager.pending_unpublished_generations.count(
                    candidate.snapshot.generation
                )
            )
            statuses = self._runtime_manager.generation_statuses
            candidate_status = statuses.get(candidate.snapshot.generation)
            base_status = statuses.get(base_snapshot.generation)
        except BaseException:
            inspection_failed = True
        if lease is not None:
            try:
                lease.release()
            except BaseException:
                return _RuntimePublishOutcome.AMBIGUOUS
        if inspection_failed:
            return _RuntimePublishOutcome.AMBIGUOUS

        if (
            current_snapshot is candidate.snapshot
            and pending_after == pending_candidate_slots - 1
            and candidate_status is RuntimeGenerationStatus.ACTIVE
            and base_status is not None
            and base_status is not RuntimeGenerationStatus.ACTIVE
        ):
            return _RuntimePublishOutcome.PUBLISHED
        if (
            current_snapshot is base_snapshot
            and pending_after == pending_candidate_slots
            and base_status is RuntimeGenerationStatus.ACTIVE
            and candidate.snapshot.generation not in statuses
        ):
            return _RuntimePublishOutcome.NOT_PUBLISHED
        return _RuntimePublishOutcome.AMBIGUOUS

    def _mark_publication_outcome_ambiguous(
        self,
        *,
        exception: BaseException,
    ) -> None:
        self._state = ConfigUpdateState.BROKEN
        self._restart_required = True
        self._last_failure = self._diagnostic(
            "runtime_publish_outcome",
            exception,
        )

    @staticmethod
    def _translate_commit_exception(exception: BaseException) -> BaseException:
        if isinstance(exception, ConfigUpdateError):
            return exception
        if isinstance(exception, AtomicConfigTransactionConflictError):
            return ConfigUpdateError(ConfigUpdateErrorCode.REVISION_CONFLICT)
        if isinstance(exception, AtomicConfigTransactionError):
            return ConfigUpdateError(ConfigUpdateErrorCode.COMMIT_FAILED)
        if not isinstance(exception, Exception):
            return exception
        return ConfigUpdateError(ConfigUpdateErrorCode.COMMIT_FAILED)

    @staticmethod
    def _resolve_failed_transaction(
        transaction: AtomicConfigFileTransaction,
    ) -> BaseException | None:
        if transaction.state is AtomicConfigTransactionState.RECOVERY_REQUIRED:
            return AtomicConfigTransactionError(
                transaction.config_file,
                "transaction requires startup recovery",
            )
        try:
            if transaction.state in {
                AtomicConfigTransactionState.BEGUN,
                AtomicConfigTransactionState.PREPARED,
            }:
                transaction.abort()
            elif transaction.state in {
                AtomicConfigTransactionState.REPLACED,
                AtomicConfigTransactionState.COMMITTED,
            }:
                transaction.rollback()
        except BaseException as exc:
            return exc
        return None

    def _resolve_failed_comments_backup(
        self,
        backup: CommentsBackupLifecycle,
    ) -> BaseException | None:
        if backup.state is CommentsBackupState.ABORTED:
            if (
                self._pending_comments_backup is not None
                and self._pending_comments_backup.lifecycle is backup
            ):
                self._pending_comments_backup = None
            return None
        self._pending_comments_backup = _PendingCommentsBackup(
            lifecycle=backup,
            action=_PendingCommentsBackupAction.ABORT,
        )
        try:
            backup.abort()
        except BaseException as exc:
            self._last_failure = self._diagnostic("comments_backup_abort", exc)
            return exc
        self._pending_comments_backup = None
        return None

    async def _close_candidate(
        self,
        candidate: RuntimeCandidate,
    ) -> BaseException | None:
        try:
            closed = await candidate.close_unpublished()
        except BaseException as exc:
            self._last_failure = self._diagnostic("candidate_cleanup", exc)
            return exc if not isinstance(exc, Exception) else None
        if not closed:
            self._last_failure = ConfigUpdateDiagnostic(
                phase="candidate_cleanup",
                exception_type="IncompleteCleanup",
            )
        return None

    def _mark_broken(
        self,
        *,
        phase: str,
        exception: BaseException,
        transaction: AtomicConfigFileTransaction,
    ) -> None:
        self._state = ConfigUpdateState.BROKEN
        if transaction.state is AtomicConfigTransactionState.RECOVERY_REQUIRED:
            self._restart_required = True
        self._last_failure = self._diagnostic(phase, exception)
        action: _PendingCleanupAction | None = None
        if transaction.state in {
                AtomicConfigTransactionState.BEGUN,
                AtomicConfigTransactionState.PREPARED,
        }:
            action = _PendingCleanupAction.ABORT
        elif transaction.state in {
            AtomicConfigTransactionState.REPLACED,
            AtomicConfigTransactionState.COMMITTED,
        }:
            action = _PendingCleanupAction.ROLLBACK
        self._pending_cleanup = (
            _PendingCleanup(transaction, action)
            if action is not None
            else None
        )

    @staticmethod
    def _diagnostic(
        phase: str,
        exception: BaseException,
    ) -> ConfigUpdateDiagnostic:
        return ConfigUpdateDiagnostic(
            phase=phase,
            exception_type=safe_exception_type_name(exception),
        )

    def _run_pending_transaction_cleanup(
        self,
        pending_cleanup: _PendingCleanup,
    ) -> None:
        if pending_cleanup.action is _PendingCleanupAction.ABORT:
            pending_cleanup.transaction.abort()
        elif pending_cleanup.action is _PendingCleanupAction.ROLLBACK:
            pending_cleanup.transaction.rollback()
        else:
            pending_cleanup.transaction.finalize()
        self._pending_cleanup = None

    def _run_pending_comments_backup_cleanup(
        self,
        pending_backup: _PendingCommentsBackup,
    ) -> bool:
        if pending_backup.action is _PendingCommentsBackupAction.ABORT:
            pending_backup.lifecycle.abort()
            self._pending_comments_backup = None
            return True
        if pending_backup.action is _PendingCommentsBackupAction.PUBLISH:
            result = pending_backup.lifecycle.publish()
        else:
            result = pending_backup.lifecycle.retry_cleanup()
        if result.cleanup_pending:
            pending_backup.action = _PendingCommentsBackupAction.RETRY
            self._last_failure = ConfigUpdateDiagnostic(
                phase="comments_backup_cleanup",
                exception_type="IncompleteCleanup",
            )
            return False
        self._pending_comments_backup = None
        return True

    def _retryable_close_error_code(self) -> ConfigUpdateErrorCode:
        if self._restart_required:
            self._state = ConfigUpdateState.BROKEN
            return ConfigUpdateErrorCode.UPDATE_BROKEN
        self._state = ConfigUpdateState.STOPPING
        return ConfigUpdateErrorCode.UPDATE_UNAVAILABLE

    async def _close_impl(self) -> None:
        await self._active_updates_drained.wait()
        await self._commit_lock.acquire()
        try:
            pending_cleanup = self._pending_cleanup
            if (
                pending_cleanup is not None
                and pending_cleanup.action is not _PendingCleanupAction.FINALIZE
            ):
                try:
                    self._run_pending_transaction_cleanup(pending_cleanup)
                except BaseException as exc:
                    self._state = ConfigUpdateState.BROKEN
                    self._last_failure = self._diagnostic("close_cleanup", exc)
                    if not isinstance(exc, Exception):
                        raise
                    raise ConfigUpdateError(
                        ConfigUpdateErrorCode.UPDATE_BROKEN
                    ) from None

            pending_backup = self._pending_comments_backup
            if pending_backup is not None:
                try:
                    backup_clean = self._run_pending_comments_backup_cleanup(
                        pending_backup
                    )
                except BaseException as exc:
                    self._last_failure = self._diagnostic(
                        "comments_backup_cleanup",
                        exc,
                    )
                    if not isinstance(exc, Exception):
                        self._retryable_close_error_code()
                        raise
                    raise ConfigUpdateError(
                        self._retryable_close_error_code()
                    ) from None
                if not backup_clean:
                    raise ConfigUpdateError(
                        self._retryable_close_error_code()
                    )

            pending_cleanup = self._pending_cleanup
            if pending_cleanup is not None:
                try:
                    self._run_pending_transaction_cleanup(pending_cleanup)
                except BaseException as exc:
                    self._state = ConfigUpdateState.BROKEN
                    self._last_failure = self._diagnostic("close_cleanup", exc)
                    if not isinstance(exc, Exception):
                        raise
                    raise ConfigUpdateError(
                        ConfigUpdateErrorCode.UPDATE_BROKEN
                    ) from None

            if self._runtime_manager.unresolved_cleanup_generations:
                try:
                    await self._runtime_manager.retry_failed_cleanup()
                except BaseException as exc:
                    self._last_failure = self._diagnostic("candidate_cleanup", exc)
                    if not isinstance(exc, Exception):
                        self._retryable_close_error_code()
                        raise
                    raise ConfigUpdateError(
                        self._retryable_close_error_code()
                    ) from None
                if self._runtime_manager.unresolved_cleanup_generations:
                    self._last_failure = ConfigUpdateDiagnostic(
                        phase="candidate_cleanup",
                        exception_type="IncompleteCleanup",
                    )
                    raise ConfigUpdateError(
                        self._retryable_close_error_code()
                    )
            if self._restart_required:
                self._state = ConfigUpdateState.BROKEN
                return
            self._state = ConfigUpdateState.STOPPED
        finally:
            self._commit_lock.release()
