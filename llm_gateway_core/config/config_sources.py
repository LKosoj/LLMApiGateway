"""Immutable, credential-safe snapshots of gateway configuration sources."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Mapping, Self


__all__ = (
    "ConfigDocument",
    "ConfigFile",
    "ConfigFileMetadata",
    "ConfigSourceBundle",
    "ConfigSourceError",
)


logger = logging.getLogger(__name__)


class ConfigFile(StrEnum):
    """Logical identities of the six gateway configuration documents."""

    PROVIDERS = "providers"
    FALLBACK_RULES = "fallback_rules"
    MODEL_RULES = "model_rules"
    OPERATION_RULES = "operation_rules"
    FUSION_RULES = "fusion_rules"
    ROUTER_RULES = "router_rules"


_DEFAULT_FILENAMES: Final[Mapping[ConfigFile, str]] = MappingProxyType(
    {
        ConfigFile.PROVIDERS: "providers.json",
        ConfigFile.FALLBACK_RULES: "models_fallback_rules.json",
        ConfigFile.MODEL_RULES: "models_model_rules.json",
        ConfigFile.OPERATION_RULES: "models_operation_rules.json",
        ConfigFile.FUSION_RULES: "models_fusion_rules.json",
        ConfigFile.ROUTER_RULES: "models_router_rules.json",
    }
)
_MANDATORY_FILES: Final[frozenset[ConfigFile]] = frozenset({ConfigFile.PROVIDERS, ConfigFile.FALLBACK_RULES})
# Directory components may be symlinks (item 5 fix); the final file open below
# keeps O_NOFOLLOW to still reject a symlink-swapped config file.
_DIRECTORY_OPEN_FLAGS: Final[int] = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_FILE_OPEN_FLAGS: Final[int] = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_READ_CHUNK_SIZE: Final[int] = 1024 * 1024


class ConfigSourceError(RuntimeError):
    """A credential-safe failure while capturing one configuration source."""

    def __init__(self, config_file: ConfigFile, reason: str) -> None:
        self.config_file = config_file
        self.reason = reason
        super().__init__(f"{config_file.value}: {reason}")


@dataclass(frozen=True, slots=True)
class ConfigFileMetadata:
    """Stable filesystem metadata captured around the exact source bytes."""

    mode: int
    uid: int
    gid: int
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    link_count: int

    @classmethod
    def from_stat(cls, source_stat: os.stat_result) -> Self:
        return cls(
            mode=source_stat.st_mode,
            uid=source_stat.st_uid,
            gid=source_stat.st_gid,
            device=source_stat.st_dev,
            inode=source_stat.st_ino,
            size=source_stat.st_size,
            mtime_ns=source_stat.st_mtime_ns,
            ctime_ns=source_stat.st_ctime_ns,
            link_count=source_stat.st_nlink,
        )


@dataclass(frozen=True, slots=True, repr=False)
class ConfigDocument:
    """One immutable source document without payload or path in diagnostics."""

    config_file: ConfigFile
    path: Path = field(repr=False)
    exists: bool
    content: bytes | None = field(repr=False)
    digest: str | None
    metadata: ConfigFileMetadata | None

    def __post_init__(self) -> None:
        if not isinstance(self.config_file, ConfigFile):
            raise TypeError("config_file must be a ConfigFile")

        path = Path(self.path)
        object.__setattr__(self, "path", path)

        if not self.exists:
            if self.content is not None or self.digest is not None or self.metadata is not None:
                raise ValueError("missing documents cannot contain source data")
            return

        if not isinstance(self.content, bytes):
            raise TypeError("existing document content must be bytes")
        expected_digest = hashlib.sha256(self.content).hexdigest()
        if self.digest != expected_digest:
            raise ValueError("document digest does not match its content")
        if self.metadata is not None and self.metadata.size != len(self.content):
            raise ValueError("document metadata size does not match its content")

    @classmethod
    def missing(cls, config_file: ConfigFile, path: Path) -> Self:
        return cls(
            config_file=config_file,
            path=path,
            exists=False,
            content=None,
            digest=None,
            metadata=None,
        )

    @classmethod
    def from_bytes(
        cls,
        config_file: ConfigFile,
        path: Path,
        content: bytes,
        *,
        metadata: ConfigFileMetadata | None = None,
    ) -> Self:
        immutable_content = bytes(content)
        return cls(
            config_file=config_file,
            path=path,
            exists=True,
            content=immutable_content,
            digest=hashlib.sha256(immutable_content).hexdigest(),
            metadata=metadata,
        )

    def __repr__(self) -> str:
        size = len(self.content) if self.content is not None else None
        return (
            "ConfigDocument("
            f"config_file={self.config_file.value!r}, "
            f"exists={self.exists!r}, digest={self.digest!r}, size={size!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ConfigSourceBundle:
    """Immutable, complete mapping of logical config files to source documents."""

    documents: Mapping[ConfigFile, ConfigDocument] = field(repr=False)

    def __post_init__(self) -> None:
        documents = dict(self.documents)
        if set(documents) != set(ConfigFile):
            raise ValueError("config source bundle must contain all six documents")

        seen_paths: set[Path] = set()
        seen_inodes: set[tuple[int, int]] = set()
        for config_file, document in documents.items():
            if not isinstance(config_file, ConfigFile) or document.config_file is not config_file:
                raise ValueError("config source bundle document identity mismatch")
            canonical_path = _canonicalize_path(document.path)
            if canonical_path != document.path:
                document = replace(document, path=canonical_path)
                documents[config_file] = document
            if config_file in _MANDATORY_FILES and not document.exists:
                raise ValueError("mandatory config source is missing")
            if canonical_path in seen_paths:
                raise ValueError("config source bundle contains a path collision")
            seen_paths.add(canonical_path)
            if document.metadata is None:
                continue
            inode = (document.metadata.device, document.metadata.inode)
            if inode in seen_inodes:
                raise ValueError("config source bundle contains an inode collision")
            seen_inodes.add(inode)

        object.__setattr__(self, "documents", MappingProxyType(documents))

    @classmethod
    def capture(
        cls,
        project_root: str | os.PathLike[str],
        *,
        overrides: Mapping[ConfigFile, str | os.PathLike[str]] | None = None,
    ) -> Self:
        """Capture all six sources using lexical paths and symlink-safe dirfds."""
        root = _normalize_project_root(project_root)
        override_paths = dict(overrides or {})
        if any(not isinstance(config_file, ConfigFile) for config_file in override_paths):
            raise TypeError("config path override keys must be ConfigFile values")

        paths = {
            config_file: _normalize_source_path(
                config_file,
                root,
                override_paths.get(config_file, default_filename),
            )
            for config_file, default_filename in _DEFAULT_FILENAMES.items()
        }
        seen_paths: set[Path] = set()
        for config_file, path in paths.items():
            if path in seen_paths:
                raise ConfigSourceError(config_file, "config path collision")
            seen_paths.add(path)

        documents: dict[ConfigFile, ConfigDocument] = {}
        seen_inodes: dict[tuple[int, int], ConfigFile] = {}
        for config_file in ConfigFile:
            document = _capture_document(
                config_file,
                paths[config_file],
                required=config_file in _MANDATORY_FILES,
            )
            if document.metadata is not None:
                inode = (document.metadata.device, document.metadata.inode)
                if inode in seen_inodes:
                    raise ConfigSourceError(config_file, "config inode collision")
                seen_inodes[inode] = config_file
            documents[config_file] = document

        return cls(documents)

    def __getitem__(self, config_file: ConfigFile) -> ConfigDocument:
        return self.documents[config_file]

    def recapture(self) -> Self:
        """Recapture the exact six configured paths without changing identities."""
        return type(self).capture(
            Path("/"),
            overrides={
                config_file: document.path
                for config_file, document in self.documents.items()
            },
        )

    def with_candidate(
        self,
        config_file: ConfigFile,
        content: bytes | bytearray | memoryview,
    ) -> Self:
        """Return a new bundle with one in-memory document and no disk access."""
        if not isinstance(config_file, ConfigFile):
            raise TypeError("config_file must be a ConfigFile")
        if not isinstance(content, bytes | bytearray | memoryview):
            raise TypeError("candidate content must be bytes-like")

        documents = dict(self.documents)
        documents[config_file] = ConfigDocument.from_bytes(
            config_file,
            documents[config_file].path,
            bytes(content),
        )
        return type(self)(documents)

    def __repr__(self) -> str:
        statuses = ", ".join(
            f"{config_file.value}={'present' if self.documents[config_file].exists else 'missing'}"
            for config_file in ConfigFile
        )
        return f"ConfigSourceBundle({statuses})"


def _normalize_project_root(project_root: str | os.PathLike[str]) -> Path:
    return _canonicalize_path(project_root)


def _normalize_source_path(
    config_file: ConfigFile,
    project_root: Path,
    source_path: str | os.PathLike[str],
) -> Path:
    normalized_path = _canonicalize_path(source_path, relative_to=project_root)
    if not normalized_path.is_absolute() or not normalized_path.name:
        raise ConfigSourceError(config_file, "invalid config path")
    return normalized_path


def _canonicalize_path(
    source_path: str | os.PathLike[str],
    *,
    relative_to: Path | None = None,
) -> Path:
    raw_path = os.fspath(source_path)
    if not isinstance(raw_path, str):
        raise TypeError("config source must be a text path")
    if os.path.isabs(raw_path):
        absolute_path = raw_path
    elif relative_to is not None:
        absolute_path = os.path.join(relative_to, raw_path)
    else:
        absolute_path = os.path.abspath(raw_path)
    normalized_path = os.path.normpath(absolute_path)
    return Path(f"/{normalized_path.lstrip('/')}")


def _capture_document(
    config_file: ConfigFile,
    path: Path,
    *,
    required: bool,
) -> ConfigDocument:
    parent_fd: int | None = None
    try:
        parent_fd = _open_parent_directory(path)
        return _capture_document_at(
            config_file,
            path,
            parent_fd,
            required=required,
        )
    except ConfigSourceError:
        raise
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            if required:
                raise ConfigSourceError(
                    config_file,
                    "required config source is missing",
                ) from None
            return ConfigDocument.missing(config_file, path)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ConfigSourceError(config_file, "config source path is unsafe") from None
        raise ConfigSourceError(config_file, "config source could not be read") from None
    except (TypeError, ValueError):
        raise ConfigSourceError(config_file, "config source path is invalid") from None
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _capture_document_at(
    config_file: ConfigFile,
    path: Path,
    parent_fd: int,
    *,
    required: bool,
) -> ConfigDocument:
    return _capture_document_name_at(
        config_file,
        path,
        parent_fd,
        source_name=path.name,
        required=required,
    )


def _capture_document_name_at(
    config_file: ConfigFile,
    path: Path,
    parent_fd: int,
    *,
    source_name: str,
    required: bool,
) -> ConfigDocument:
    source_fd: int | None = None
    try:
        source_fd = os.open(source_name, _FILE_OPEN_FLAGS, dir_fd=parent_fd)
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ConfigSourceError(config_file, "config source is not a regular file")
        if before.st_nlink != 1:
            logger.warning(
                "%s: config source %s is hard-linked (nlink=%d)",
                config_file.value,
                source_name,
                before.st_nlink,
            )

        content = _read_all(source_fd)
        after = os.fstat(source_fd)
        if _stat_identity(before) != _stat_identity(after) or len(content) != after.st_size:
            raise ConfigSourceError(config_file, "config source changed during capture")

        metadata = ConfigFileMetadata.from_stat(after)
        return ConfigDocument.from_bytes(config_file, path, content, metadata=metadata)
    except ConfigSourceError:
        raise
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            if required:
                raise ConfigSourceError(config_file, "required config source is missing") from None
            return ConfigDocument.missing(config_file, path)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ConfigSourceError(config_file, "config source path is unsafe") from None
        raise ConfigSourceError(config_file, "config source could not be read") from None
    except (TypeError, ValueError):
        raise ConfigSourceError(config_file, "config source path is invalid") from None
    finally:
        if source_fd is not None:
            os.close(source_fd)


def _open_parent_directory(path: Path) -> int:
    current_fd = os.open("/", _DIRECTORY_OPEN_FLAGS)
    try:
        for component in path.parent.parts[1:]:
            next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _read_all(source_fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(source_fd, _READ_CHUNK_SIZE):
        chunks.append(chunk)
    return b"".join(chunks)


def _stat_identity(source_stat: os.stat_result) -> tuple[int, ...]:
    return (
        source_stat.st_mode,
        source_stat.st_uid,
        source_stat.st_gid,
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        source_stat.st_ctime_ns,
        source_stat.st_nlink,
    )
