"""Symlink-safe, atomic storage for locally generated images."""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat
import threading
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final


_DIRECTORY_OPEN_FLAGS: Final[int] = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_OPEN_FLAGS: Final[int] = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_RESEARCH_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_FINAL_FILENAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"image_[0-9a-f]{8}_[0-9]+\.png\Z", re.ASCII)
_TEMP_FILENAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\.image_[0-9a-f]{8}_[0-9]+\.png\.llmgateway-[0-9a-f]{32}\.tmp\Z",
    re.ASCII,
)
_RESTORE_MARKER: Final[str] = ".image-restore-incomplete"
_PROBE_CONTENT: Final[bytes] = b"llmgateway-generated-images-probe\n"


class ImageStorageError(RuntimeError):
    """A stable, credential- and path-safe image storage failure."""

    def __init__(self, reason: str, *, publication_uncertain: bool = False) -> None:
        self.reason = reason
        self.publication_uncertain = publication_uncertain
        super().__init__(f"generated image storage failed: {reason}")


@dataclass(frozen=True, slots=True)
class PublishedImage:
    path: Path
    url: str


def _error(reason: str, *, publication_uncertain: bool = False) -> ImageStorageError:
    return ImageStorageError(reason, publication_uncertain=publication_uncertain)


def _is_research_id(value: str) -> bool:
    return bool(_RESEARCH_ID_PATTERN.fullmatch(value))


def _is_final_filename(value: str) -> bool:
    return bool(_FINAL_FILENAME_PATTERN.fullmatch(value))


def _is_temp_filename(value: str) -> bool:
    return bool(_TEMP_FILENAME_PATTERN.fullmatch(value))


def _temporary_filename(final_filename: str) -> str:
    return f".{final_filename}.llmgateway-{secrets.token_hex(16)}.tmp"


def _close_quietly(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _open_validated_directory(path: Path, reason_prefix: str) -> int:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        raise _error(f"{reason_prefix}-missing") from None
    except (OSError, TypeError, ValueError):
        raise _error(f"{reason_prefix}-stat-failed") from None
    if not stat.S_ISDIR(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
        raise _error(f"{reason_prefix}-unsafe")

    current_fd: int | None = None
    try:
        current_fd = os.open(os.sep, _DIRECTORY_OPEN_FLAGS)
        for component in path.parts[1:]:
            next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            try:
                os.close(current_fd)
            except OSError:
                _close_quietly(next_fd)
                raise _error(f"{reason_prefix}-close-failed") from None
            current_fd = next_fd
        opened_stat = os.fstat(current_fd)
    except ImageStorageError:
        _close_quietly(current_fd)
        raise
    except (OSError, TypeError, ValueError) as exc:
        _close_quietly(current_fd)
        reason = "unsafe" if isinstance(exc, OSError) and exc.errno in {errno.ELOOP, errno.ENOTDIR} else "open-failed"
        raise _error(f"{reason_prefix}-{reason}") from None

    if not stat.S_ISDIR(opened_stat.st_mode) or (opened_stat.st_dev, opened_stat.st_ino) != (
        path_stat.st_dev,
        path_stat.st_ino,
    ):
        _close_quietly(current_fd)
        raise _error(f"{reason_prefix}-changed")
    return current_fd


def _write_all(fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(fd, content[offset:])
        if written <= 0:
            raise OSError("image write made no progress")
        offset += written


def _cleanup_names(directory_fd: int, names: Sequence[str]) -> bool:
    removed = False
    cleanup_failed = False
    for name in names:
        cleaned = False
        for _attempt in range(2):
            try:
                os.unlink(name, dir_fd=directory_fd)
                removed = True
                cleaned = True
                break
            except FileNotFoundError:
                cleaned = True
                break
            except OSError:
                continue
        cleanup_failed = cleanup_failed or not cleaned
    if removed:
        try:
            os.fsync(directory_fd)
        except OSError:
            cleanup_failed = True
    return not cleanup_failed


def _target_matches(directory_fd: int, filename: str, identity: tuple[int, int]) -> bool:
    try:
        target_stat = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except (OSError, TypeError, ValueError):
        return False
    return stat.S_ISREG(target_stat.st_mode) and (target_stat.st_dev, target_stat.st_ino) == identity


def _validate_existing_target(directory_fd: int, filename: str) -> None:
    try:
        target_stat = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except (OSError, TypeError, ValueError):
        raise _error("target-check-failed") from None
    if not stat.S_ISREG(target_stat.st_mode) or target_stat.st_nlink != 1:
        raise _error("target-unsafe")


def _open_or_create_directory(
    parent_fd: int,
    name: str,
    reason_prefix: str,
) -> int:
    try:
        named_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o770, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        except OSError:
            raise _error(f"{reason_prefix}-create-failed") from None
        try:
            named_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise _error(f"{reason_prefix}-stat-failed") from None
    except OSError:
        raise _error(f"{reason_prefix}-stat-failed") from None

    if not stat.S_ISDIR(named_stat.st_mode) or stat.S_ISLNK(named_stat.st_mode):
        raise _error(f"{reason_prefix}-unsafe")
    directory_fd: int | None = None
    try:
        directory_fd = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        opened_stat = os.fstat(directory_fd)
    except OSError as exc:
        _close_quietly(directory_fd)
        reason = "unsafe" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "open-failed"
        raise _error(f"{reason_prefix}-{reason}") from None
    if not stat.S_ISDIR(opened_stat.st_mode) or (opened_stat.st_dev, opened_stat.st_ino) != (
        named_stat.st_dev,
        named_stat.st_ino,
    ):
        _close_quietly(directory_fd)
        raise _error(f"{reason_prefix}-changed")
    return directory_fd


def _open_research_directory(root_fd: int, research_id: str) -> int:
    return _open_or_create_directory(
        root_fd,
        research_id,
        "research-directory",
    )


class GeneratedImageStorage:
    def __init__(self, images_root: Path) -> None:
        try:
            root = Path(images_root)
            raw_root = os.fspath(root)
        except (TypeError, ValueError):
            raise _error("images-root-invalid") from None
        if (
            not isinstance(raw_root, str)
            or "\x00" in raw_root
            or not root.is_absolute()
            or root.anchor != os.sep
            or ".." in root.parts
            or root.parent == root
        ):
            raise _error("images-root-invalid")
        self._images_root = root
        self._lock = threading.RLock()

    @property
    def images_root(self) -> Path:
        return self._images_root

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._lock:
            yield

    @contextmanager
    def locked_root(self) -> Iterator[int]:
        with self._lock:
            root_fd = _open_validated_directory(self._images_root, "images-root")
            failed = False
            try:
                yield root_fd
            except BaseException:
                failed = True
                raise
            finally:
                try:
                    os.close(root_fd)
                except OSError:
                    if not failed:
                        raise _error("images-root-close-failed") from None

    def publish_png(self, image_bytes: bytes, filename: str, research_id: str = "") -> PublishedImage:
        if not isinstance(image_bytes, bytes) or not image_bytes:
            raise _error("image-bytes-invalid")
        if not isinstance(filename, str) or not _is_final_filename(filename):
            raise _error("filename-invalid")
        if not isinstance(research_id, str) or (research_id and not _is_research_id(research_id)):
            raise _error("research-id-invalid")

        published = False
        try:
            with self.locked_root() as root_fd:
                directory_fd = root_fd
                owns_directory_fd = False
                if research_id:
                    directory_fd = _open_research_directory(root_fd, research_id)
                    owns_directory_fd = True
                try:
                    self._publish_to_directory(directory_fd, filename, image_bytes)
                    published = True
                finally:
                    if owns_directory_fd:
                        try:
                            os.close(directory_fd)
                        except OSError:
                            if not published:
                                raise _error("research-directory-close-failed") from None
                            raise _error("research-directory-close-failed", publication_uncertain=True) from None
        except ImageStorageError as exc:
            if published and not exc.publication_uncertain:
                raise _error(exc.reason, publication_uncertain=True) from None
            raise

        path = self._images_root / research_id / filename if research_id else self._images_root / filename
        url_prefix = f"/outputs/images/{research_id}" if research_id else "/outputs/images"
        return PublishedImage(path=path, url=f"{url_prefix}/{filename}")

    @staticmethod
    def _publish_to_directory(directory_fd: int, filename: str, image_bytes: bytes) -> None:
        temp_name = _temporary_filename(filename)
        temp_fd: int | None = None
        temp_exists = False
        published = False
        identity: tuple[int, int] | None = None
        primary: BaseException | None = None
        phase = "temp-create-failed"
        try:
            temp_fd = os.open(temp_name, _CREATE_OPEN_FLAGS, 0o600, dir_fd=directory_fd)
            temp_exists = True
            phase = "image-write-failed"
            _write_all(temp_fd, image_bytes)
            phase = "image-sync-failed"
            os.fsync(temp_fd)
            temp_stat = os.fstat(temp_fd)
            if not stat.S_ISREG(temp_stat.st_mode) or temp_stat.st_nlink != 1:
                raise _error("temp-file-unsafe")
            identity = (temp_stat.st_dev, temp_stat.st_ino)
            try:
                os.close(temp_fd)
            except OSError:
                temp_fd = None
                raise _error("temp-close-failed") from None
            temp_fd = None
            _validate_existing_target(directory_fd, filename)
            phase = "image-replace-failed"
            try:
                os.replace(temp_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            except OSError:
                assert identity is not None
                published = _target_matches(directory_fd, filename, identity)
                if published:
                    temp_exists = False
                raise
            temp_exists = False
            published = True
            phase = "directory-sync-failed"
            os.fsync(directory_fd)
        except ImageStorageError as exc:
            primary = exc
        except OSError:
            primary = _error(phase, publication_uncertain=published)
        except BaseException as exc:
            if phase == "image-replace-failed" and identity is not None and _target_matches(directory_fd, filename, identity):
                published = True
                temp_exists = False
            primary = exc

        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                if primary is None:
                    primary = _error("temp-close-failed")
        if temp_exists and not _cleanup_names(directory_fd, (temp_name,)) and primary is None:
            primary = _error("temp-cleanup-failed")
        if primary is not None:
            if isinstance(primary, ImageStorageError) and published and not primary.publication_uncertain:
                primary = _error(primary.reason, publication_uncertain=True)
            raise primary

    def probe(self) -> None:
        with self.exclusive():
            self._check_restore_marker()
            with self.locked_root() as root_fd:
                first_name = _temporary_filename("image_00000000_0.png")
                second_name = _temporary_filename("image_00000000_1.png")
                probe_fd: int | None = None
                primary: BaseException | None = None
                phase = "probe-create-failed"
                try:
                    probe_fd = os.open(first_name, _CREATE_OPEN_FLAGS, 0o600, dir_fd=root_fd)
                    phase = "probe-write-failed"
                    _write_all(probe_fd, _PROBE_CONTENT)
                    phase = "probe-sync-failed"
                    os.fsync(probe_fd)
                    try:
                        os.close(probe_fd)
                    except OSError:
                        probe_fd = None
                        raise _error("probe-close-failed") from None
                    probe_fd = None
                    phase = "probe-rename-failed"
                    os.rename(first_name, second_name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                    phase = "probe-directory-sync-failed"
                    os.fsync(root_fd)
                    phase = "probe-delete-failed"
                    os.unlink(second_name, dir_fd=root_fd)
                    phase = "probe-directory-sync-failed"
                    os.fsync(root_fd)
                except ImageStorageError as exc:
                    primary = exc
                except OSError:
                    primary = _error(phase)
                except BaseException as exc:
                    primary = exc
                if probe_fd is not None:
                    try:
                        os.close(probe_fd)
                    except OSError:
                        if primary is None:
                            primary = _error("probe-close-failed")
                if not _cleanup_names(root_fd, (first_name, second_name)) and primary is None:
                    primary = _error("probe-cleanup-failed")
                if primary is not None:
                    raise primary

    def _check_restore_marker(self) -> None:
        parent_fd = _open_validated_directory(self._images_root.parent, "outputs-root")
        failed = False
        try:
            try:
                os.stat(_RESTORE_MARKER, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            except OSError:
                raise _error("restore-marker-check-failed") from None
            raise _error("restore-incomplete")
        except BaseException:
            failed = True
            raise
        finally:
            try:
                os.close(parent_fd)
            except OSError:
                if not failed:
                    raise _error("outputs-root-close-failed") from None


def prepare_default_images_root(project_root: Path, images_root: Path) -> None:
    """Create ``<project>/outputs/images`` without following directory links."""
    try:
        project = Path(project_root)
        images = Path(images_root)
    except (TypeError, ValueError):
        raise _error("default-root-mismatch") from None
    if images != project / "outputs" / "images":
        raise _error("default-root-mismatch")

    try:
        with GeneratedImageStorage(project).locked_root() as project_fd:
            with ExitStack() as stack:
                outputs_fd = _open_or_create_directory(
                    project_fd,
                    "outputs",
                    "default-outputs-directory",
                )
                stack.callback(os.close, outputs_fd)
                images_fd = _open_or_create_directory(
                    outputs_fd,
                    "images",
                    "default-images-directory",
                )
                stack.callback(os.close, images_fd)
    except (ImageStorageError, OSError):
        raise _error("default-root-prepare-failed") from None
