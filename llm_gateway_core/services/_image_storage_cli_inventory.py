"""Inventory and manifest primitives for generated-image storage administration."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator

from llm_gateway_core.config.paths import validate_safe_absolute_path


RUNTIME_UID = 10001
RUNTIME_GID = 10001
DIRECTORY_MODE = 0o770
FILE_MODE = 0o660
PRIVATE_FILE_MODE = 0o600
MANIFEST_VERSION = 1
INCOMPLETE_MARKER = ".image-restore-incomplete"
ARCHIVE_PREFIX = "images"


class ImageStorageCliError(RuntimeError):
    """Credential- and path-safe operational failure."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    path: str
    size: int
    sha256: str
    mtime_ns: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "mtime_ns": self.mtime_ns,
        }


@dataclass(frozen=True, slots=True)
class ImageInventory:
    files: tuple[InventoryEntry, ...]
    tree_sha256: str

    @property
    def count(self) -> int:
        return len(self.files)

    def as_dict(self) -> dict[str, object]:
        return {
            "version": MANIFEST_VERSION,
            "count": self.count,
            "tree_sha256": self.tree_sha256,
            "files": [entry.as_dict() for entry in self.files],
        }


def _require_absolute(path: Path) -> Path:
    if not path.is_absolute():
        raise ImageStorageCliError("path-not-absolute")
    return path


def _require_safe_cli_path(path: str | os.PathLike[str]) -> Path:
    try:
        return validate_safe_absolute_path(path)
    except ValueError as error:
        candidate = Path(os.fspath(path))
        reason = "path-not-absolute" if not candidate.is_absolute() else "path-unsafe"
        raise ImageStorageCliError(reason) from error


def _canonical_path(path: Path) -> Path:
    path = _require_absolute(path)
    try:
        return path.resolve(strict=False)
    except OSError as error:
        raise ImageStorageCliError("path-unavailable") from error


def _require_disjoint_paths(first: Path, second: Path) -> None:
    canonical_first = _canonical_path(first)
    canonical_second = _canonical_path(second)
    if (
        canonical_first == canonical_second
        or canonical_first in canonical_second.parents
        or canonical_second in canonical_first.parents
    ):
        raise ImageStorageCliError("path-overlap")


def _require_distinct_artifacts(first: Path, second: Path) -> None:
    if _canonical_path(first) == _canonical_path(second):
        raise ImageStorageCliError("artifact-path-conflict")


def _reject_symlink_components(path: Path, *, allow_missing_leaf: bool = False) -> None:
    _require_absolute(path)
    current = Path(path.anchor)
    parts = path.parts[1:]
    for index, component in enumerate(parts):
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and index == len(parts) - 1:
                return
            raise ImageStorageCliError("path-missing") from None
        except OSError as error:
            raise ImageStorageCliError("path-unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ImageStorageCliError("path-symlink")


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ImageStorageCliError("manifest-path-invalid")
    return path


@contextmanager
def _open_directory_path(path: Path, *, error_reason: str) -> Iterator[int]:
    path = _require_absolute(path)
    descriptor = -1
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path.anchor, flags)
        for component in path.parts[1:]:
            child_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child_descriptor
        yield descriptor
    except ImageStorageCliError:
        raise
    except OSError as error:
        raise ImageStorageCliError(error_reason) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
    )


def _same_regular_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _same_identity(first, second)
        and second.st_nlink == first.st_nlink == 1
        and second.st_size == first.st_size
        and second.st_mtime_ns == first.st_mtime_ns
    )


def _hash_open_file(descriptor: int) -> str:
    digest = hashlib.sha256()
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    except OSError as error:
        raise ImageStorageCliError("inventory-read-failed") from error
    return digest.hexdigest()


def _inventory_digest(entries: Iterable[InventoryEntry]) -> str:
    encoded = json.dumps(
        [entry.as_dict() for entry in entries],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_inventory(images_dir: Path) -> ImageInventory:
    """Return a deterministic inventory without following filesystem links."""

    images_dir = _require_absolute(images_dir)
    _reject_symlink_components(images_dir)
    entries: list[InventoryEntry] = []

    def visit(directory_descriptor: int, relative: PurePosixPath | None = None) -> None:
        try:
            with os.scandir(directory_descriptor) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            raise ImageStorageCliError("inventory-scan-failed") from error
        for child in children:
            child_relative = (
                PurePosixPath(child.name)
                if relative is None
                else relative / child.name
            )
            try:
                metadata = os.stat(
                    child.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise ImageStorageCliError("inventory-stat-failed") from error
            if stat.S_ISDIR(metadata.st_mode):
                child_descriptor = -1
                try:
                    child_descriptor = os.open(
                        child.name,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=directory_descriptor,
                    )
                    opened = os.fstat(child_descriptor)
                    if not _same_identity(metadata, opened):
                        raise ImageStorageCliError("inventory-source-changed")
                    visit(child_descriptor, child_relative)
                    current = os.stat(
                        child.name,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                    if not _same_identity(opened, current):
                        raise ImageStorageCliError("inventory-source-changed")
                except ImageStorageCliError:
                    raise
                except OSError as error:
                    raise ImageStorageCliError("inventory-source-changed") from error
                finally:
                    if child_descriptor >= 0:
                        os.close(child_descriptor)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ImageStorageCliError("inventory-entry-unsupported")
            relative_text = child_relative.as_posix()
            _safe_relative_path(relative_text)
            descriptor = -1
            try:
                descriptor = os.open(
                    child.name,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_descriptor,
                )
                opened = os.fstat(descriptor)
                if not _same_regular_snapshot(metadata, opened):
                    raise ImageStorageCliError("inventory-source-changed")
                digest = _hash_open_file(descriptor)
                after_read = os.fstat(descriptor)
                current = os.stat(
                    child.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not _same_regular_snapshot(opened, after_read)
                    or not _same_regular_snapshot(opened, current)
                ):
                    raise ImageStorageCliError("inventory-source-changed")
                entries.append(
                    InventoryEntry(
                        path=relative_text,
                        size=opened.st_size,
                        sha256=digest,
                        mtime_ns=opened.st_mtime_ns,
                    )
                )
            except ImageStorageCliError:
                raise
            except OSError as error:
                raise ImageStorageCliError("inventory-source-changed") from error
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

    try:
        with _open_directory_path(
            images_dir,
            error_reason="inventory-root-unavailable",
        ) as root_descriptor:
            root_metadata = os.fstat(root_descriptor)
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise ImageStorageCliError("inventory-root-invalid")
            visit(root_descriptor)
            current_root = images_dir.lstat()
            if not _same_identity(root_metadata, current_root):
                raise ImageStorageCliError("inventory-source-changed")
    except ImageStorageCliError:
        raise
    except OSError as error:
        raise ImageStorageCliError("inventory-root-unavailable") from error
    ordered = tuple(sorted(entries, key=lambda entry: entry.path))
    return ImageInventory(ordered, _inventory_digest(ordered))


def _fsync_directory(directory: Path) -> None:
    with _open_directory_path(
        directory,
        error_reason="directory-sync-failed",
    ) as descriptor:
        try:
            os.fsync(descriptor)
        except OSError as error:
            raise ImageStorageCliError("directory-sync-failed") from error


def _write_private_atomic(path: Path, payload: bytes) -> None:
    path = _require_absolute(path)
    _reject_symlink_components(path.parent)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            PRIVATE_FILE_MODE,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ImageStorageCliError("private-file-write-stalled")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        os.chmod(path, PRIVATE_FILE_MODE, follow_symlinks=False)
        _fsync_directory(path.parent)
    except ImageStorageCliError:
        raise
    except OSError as error:
        raise ImageStorageCliError("private-file-write-failed") from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_manifest(path: Path, inventory: ImageInventory) -> None:
    payload = (
        json.dumps(
            inventory.as_dict(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    _write_private_atomic(path, payload)


def read_manifest(path: Path) -> ImageInventory:
    path = _require_absolute(path)
    _reject_symlink_components(path)
    descriptor = -1
    try:
        with _open_directory_path(
            path.parent,
            error_reason="manifest-read-failed",
        ) as parent_descriptor:
            descriptor = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
            opened = os.fstat(descriptor)
            current = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ImageStorageCliError("manifest-file-invalid")
            if not _same_regular_snapshot(opened, current):
                raise ImageStorageCliError("manifest-changed")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after_read = os.fstat(descriptor)
            current = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            payload = b"".join(chunks)
            if (
                len(payload) != opened.st_size
                or not _same_regular_snapshot(opened, after_read)
                or not _same_regular_snapshot(opened, current)
            ):
                raise ImageStorageCliError("manifest-changed")
        raw = json.loads(payload)
    except ImageStorageCliError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ImageStorageCliError("manifest-read-failed") from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if not isinstance(raw, dict) or set(raw) != {
        "version",
        "count",
        "tree_sha256",
        "files",
    }:
        raise ImageStorageCliError("manifest-schema-invalid")
    if raw["version"] != MANIFEST_VERSION or not isinstance(raw["files"], list):
        raise ImageStorageCliError("manifest-schema-invalid")
    if (
        not isinstance(raw["count"], int)
        or isinstance(raw["count"], bool)
        or raw["count"] < 0
        or not isinstance(raw["tree_sha256"], str)
        or len(raw["tree_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in raw["tree_sha256"]
        )
    ):
        raise ImageStorageCliError("manifest-schema-invalid")
    entries: list[InventoryEntry] = []
    previous = ""
    for item in raw["files"]:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "size",
            "sha256",
            "mtime_ns",
        }:
            raise ImageStorageCliError("manifest-schema-invalid")
        relative = item["path"]
        size = item["size"]
        digest = item["sha256"]
        mtime_ns = item["mtime_ns"]
        if (
            not isinstance(relative, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(mtime_ns, int)
            or isinstance(mtime_ns, bool)
            or mtime_ns < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ImageStorageCliError("manifest-schema-invalid")
        _safe_relative_path(relative)
        if relative <= previous:
            raise ImageStorageCliError("manifest-order-invalid")
        previous = relative
        entries.append(InventoryEntry(relative, size, digest, mtime_ns))
    inventory = ImageInventory(tuple(entries), _inventory_digest(entries))
    if (
        raw["count"] != inventory.count
        or raw["tree_sha256"] != inventory.tree_sha256
    ):
        raise ImageStorageCliError("manifest-digest-mismatch")
    return inventory
