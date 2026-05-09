from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def cleanup_old_images(root: Path, retention_days: int) -> int:
    """Delete files and empty directories older than ``retention_days`` under ``root``.

    Returns the number of files deleted. Directories are removed when they
    become empty after file pruning. Symlinks are never followed — they are
    treated as regular entries and unlinked if stale. Missing roots are a
    no-op to keep startup idempotent.
    """
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    if not root.exists():
        return 0

    cutoff = time.time() - retention_days * 24 * 60 * 60
    deleted_files = 0

    for entry in sorted(root.rglob("*"), reverse=True):
        try:
            stat_result = entry.lstat()
        except OSError:
            logger.warning("Failed to stat %s during image retention cleanup.", entry, exc_info=True)
            continue

        if entry.is_symlink() or entry.is_file():
            if stat_result.st_mtime < cutoff:
                try:
                    entry.unlink()
                    deleted_files += 1
                except OSError:
                    logger.warning("Failed to delete stale image %s.", entry, exc_info=True)
            continue

        if entry.is_dir():
            try:
                next(entry.iterdir())
            except StopIteration:
                try:
                    entry.rmdir()
                except OSError:
                    logger.warning("Failed to remove empty directory %s.", entry, exc_info=True)
            except OSError:
                logger.warning("Failed to scan directory %s during cleanup.", entry, exc_info=True)

    return deleted_files


def purge_images_tree(root: Path) -> None:
    """Delete the entire ``root`` tree and recreate an empty directory.

    Intended for tests and manual maintenance, not called from production code.
    """
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
