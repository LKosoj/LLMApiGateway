from __future__ import annotations

import errno
import logging
import math
import os
import shutil
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .image_storage import (
    GeneratedImageStorage,
    ImageStorageError,
    _is_final_filename,
    _is_research_id,
    _is_temp_filename,
)

logger = logging.getLogger(__name__)

_DAY_SECONDS: Final[int] = 24 * 60 * 60
_MAX_COUNTER: Final[int] = 2_147_483_647
_DIRECTORY_OPEN_FLAGS: Final[int] = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True, slots=True)
class ImageRetentionSnapshot:
    last_run: float | None = None
    deleted_final: int = 0
    deleted_temp: int = 0
    deleted_dirs: int = 0
    failures: int = 0


@dataclass(slots=True)
class _Counters:
    deleted_final: int = 0
    deleted_temp: int = 0
    deleted_dirs: int = 0
    failures: int = 0

    def increment(self, field: Literal["deleted_final", "deleted_temp", "deleted_dirs", "failures"]) -> None:
        setattr(self, field, min(getattr(self, field) + 1, _MAX_COUNTER))


class ImageRetentionService:
    def __init__(
        self,
        storage: GeneratedImageStorage,
        retention_days: int,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(storage, GeneratedImageStorage):
            raise TypeError("storage must be a GeneratedImageStorage")
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days <= 0:
            raise ValueError("retention_days must be a positive integer")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._storage = storage
        self._retention_days = retention_days
        self._clock = clock
        self._snapshot = ImageRetentionSnapshot()
        self._snapshot_lock = threading.Lock()

    def snapshot(self) -> ImageRetentionSnapshot:
        with self._snapshot_lock:
            return self._snapshot

    def run(self) -> ImageRetentionSnapshot:
        now = float(self._clock())
        if not math.isfinite(now):
            raise ValueError("clock must return a finite timestamp")
        cutoff = now - self._retention_days * _DAY_SECONDS
        counters = _Counters()
        try:
            with self._storage.locked_root() as root_fd:
                self._clean_root(root_fd, cutoff, counters)
        except ImageStorageError:
            counters.increment("failures")
            logger.warning("Generated image retention could not access its storage root.")

        snapshot = ImageRetentionSnapshot(
            last_run=now,
            deleted_final=counters.deleted_final,
            deleted_temp=counters.deleted_temp,
            deleted_dirs=counters.deleted_dirs,
            failures=counters.failures,
        )
        with self._snapshot_lock:
            self._snapshot = snapshot
        return snapshot

    @staticmethod
    def _clean_root(root_fd: int, cutoff: float, counters: _Counters) -> None:
        try:
            names = sorted(os.listdir(root_fd))
        except OSError:
            counters.increment("failures")
            return
        for name in names:
            if _is_final_filename(name):
                _delete_stale_owned_file(root_fd, name, "deleted_final", cutoff, counters)
            elif _is_temp_filename(name):
                _delete_stale_owned_file(root_fd, name, "deleted_temp", cutoff, counters)
            elif _is_research_id(name):
                _clean_research_directory(root_fd, name, cutoff, counters)


def _delete_stale_owned_file(
    directory_fd: int,
    name: str,
    counter: Literal["deleted_final", "deleted_temp"],
    cutoff: float,
    counters: _Counters,
) -> None:
    try:
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        counters.increment("failures")
        return
    if not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_nlink != 1 or entry_stat.st_mtime >= cutoff:
        return
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    except OSError:
        counters.increment("failures")
        return
    counters.increment(counter)


def _clean_research_directory(root_fd: int, name: str, cutoff: float, counters: _Counters) -> None:
    try:
        named_stat = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        counters.increment("failures")
        return
    if not stat.S_ISDIR(named_stat.st_mode) or stat.S_ISLNK(named_stat.st_mode):
        return

    directory_fd: int | None = None
    try:
        directory_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=root_fd)
        opened_stat = os.fstat(directory_fd)
    except OSError:
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        counters.increment("failures")
        return
    assert directory_fd is not None
    if not stat.S_ISDIR(opened_stat.st_mode) or (opened_stat.st_dev, opened_stat.st_ino) != (
        named_stat.st_dev,
        named_stat.st_ino,
    ):
        try:
            os.close(directory_fd)
        except OSError:
            counters.increment("failures")
        return

    removable = False
    try:
        try:
            children = sorted(os.listdir(directory_fd))
        except OSError:
            counters.increment("failures")
            return
        for child in children:
            if _is_final_filename(child):
                _delete_stale_owned_file(directory_fd, child, "deleted_final", cutoff, counters)
            elif _is_temp_filename(child):
                _delete_stale_owned_file(directory_fd, child, "deleted_temp", cutoff, counters)
        try:
            removable = not os.listdir(directory_fd)
        except OSError:
            counters.increment("failures")
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            counters.increment("failures")
            removable = False

    if not removable:
        return
    try:
        current_stat = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if (current_stat.st_dev, current_stat.st_ino) != (named_stat.st_dev, named_stat.st_ino):
            return
        os.rmdir(name, dir_fd=root_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
            counters.increment("failures")
        return
    counters.increment("deleted_dirs")


def cleanup_old_images(root: Path, retention_days: int) -> int:
    """Compatibility wrapper for callers migrating to ``ImageRetentionService``."""
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    try:
        os.lstat(root)
    except FileNotFoundError:
        return 0
    except OSError:
        return 0
    snapshot = ImageRetentionService(
        GeneratedImageStorage(Path(root)),
        retention_days=retention_days,
    ).run()
    return snapshot.deleted_final + snapshot.deleted_temp


def purge_images_tree(root: Path) -> None:
    """Delete the entire ``root`` tree and recreate an empty directory.

    Intended for tests and manual maintenance, not called from production code.
    """
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
