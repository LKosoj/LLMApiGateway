"""One-shot selection and loading of the application environment file."""

from __future__ import annotations

import io
import os
import re
import stat
from functools import cache
from pathlib import Path
from typing import Final

import dotenv
from dotenv.parser import parse_stream


ENVIRONMENT_FILE_KEY: Final[str] = "LLMGATEWAY_ENV_FILE"
PROJECT_DOTENV: Path = Path(__file__).resolve().parents[2] / ".env"
_COMMON_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class EnvironmentFileError(RuntimeError):
    """Path-safe environment-file selection or loading failure."""

    def __init__(self, reason: str, path: str) -> None:
        self.key = ENVIRONMENT_FILE_KEY
        self.reason = reason
        self.path = path
        super().__init__(f"key={ENVIRONMENT_FILE_KEY} reason={reason}")


class EnvironmentSubsetError(ValueError):
    """Failure to parse the strict syntax shared by systemd and python-dotenv."""

    def __init__(self, reason: str, names: tuple[str, ...] = ()) -> None:
        self.reason = reason
        self.names = names
        super().__init__(reason)


def _common_value(raw_value: str, name: str) -> str:
    if not raw_value:
        return ""
    if raw_value.startswith("'"):
        if len(raw_value) < 2 or not raw_value.endswith("'"):
            raise EnvironmentSubsetError("unsupported-syntax", (name,))
        value = raw_value[1:-1]
        if "'" in value or "\\" in value:
            raise EnvironmentSubsetError("unsupported-syntax", (name,))
        return value
    if raw_value.startswith('"'):
        if len(raw_value) < 2 or not raw_value.endswith('"'):
            raise EnvironmentSubsetError("unsupported-syntax", (name,))
        value = raw_value[1:-1]
        if '"' in value or "\\" in value or "$" in value:
            raise EnvironmentSubsetError("unsupported-syntax", (name,))
        return value
    if any(character.isspace() or character in "'\"\\$#;" for character in raw_value):
        raise EnvironmentSubsetError("unsupported-syntax", (name,))
    return raw_value


def parse_environment_subset(payload: str) -> dict[str, str]:
    """Parse the deliberately small EnvironmentFile/python-dotenv intersection."""

    if "\x00" in payload:
        raise EnvironmentSubsetError("value-invalid")
    values: dict[str, str] = {}
    for raw_line in payload.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        match = _COMMON_ASSIGNMENT.fullmatch(raw_line)
        if match is None:
            raise EnvironmentSubsetError("unsupported-syntax")
        name, raw_value = match.groups()
        if name in values:
            raise EnvironmentSubsetError("duplicate-key", (name,))
        value = _common_value(raw_value, name)
        parsed = tuple(parse_stream(io.StringIO(f"{raw_line}\n")))
        if (
            len(parsed) != 1
            or parsed[0].error
            or parsed[0].key != name
            or parsed[0].value != value
        ):
            raise EnvironmentSubsetError("unsupported-syntax", (name,))
        values[name] = value
    return values


def _same_regular_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISREG(first.st_mode)
        and stat.S_ISREG(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def _close_descriptor(descriptor: int) -> None:
    if descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _read_explicit_environment_file(path: Path, display_path: str) -> str:
    try:
        initial = path.lstat()
    except FileNotFoundError:
        raise EnvironmentFileError("missing", display_path) from None
    except (OSError, ValueError):
        raise EnvironmentFileError("unavailable", display_path) from None
    if stat.S_ISLNK(initial.st_mode):
        raise EnvironmentFileError("symlink", display_path)
    if not stat.S_ISREG(initial.st_mode):
        raise EnvironmentFileError("not-regular", display_path)

    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            opened = os.fstat(stream.fileno())
            if not _same_regular_file(initial, opened):
                raise EnvironmentFileError("changed", display_path)
            payload = stream.read()
            after_read = os.fstat(stream.fileno())
        current = path.lstat()
    except EnvironmentFileError:
        raise
    except (OSError, ValueError):
        raise EnvironmentFileError("unavailable", display_path) from None
    finally:
        _close_descriptor(descriptor)

    if (
        len(payload) != opened.st_size
        or not _same_regular_file(opened, after_read)
        or not _same_regular_file(opened, current)
    ):
        raise EnvironmentFileError("changed", display_path)
    try:
        return payload.decode("utf-8")
    except UnicodeError:
        raise EnvironmentFileError("decode-failed", display_path) from None


def _explicit_environment_file_values(
    payload: str,
    display_path: str,
) -> dict[str, str]:
    """Parse an explicit LLMGATEWAY_ENV_FILE with the default .env value syntax.

    Keeps the same strict line grammar, duplicate-key rejection, and null-byte
    rejection as the systemd/dotenv intersection subset, but resolves each
    value with python-dotenv itself (quotes, escapes, ``${VAR}``
    interpolation) so a file valid for the default project ``.env`` is no
    longer fatally rejected here for using that syntax (item 6 fix).
    """
    if "\x00" in payload:
        raise EnvironmentFileError("value-invalid", display_path)
    names: list[str] = []
    seen: set[str] = set()
    for raw_line in payload.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        match = _COMMON_ASSIGNMENT.fullmatch(raw_line)
        if match is None:
            raise EnvironmentFileError("parse-failed", display_path)
        name = match.group(1)
        if name in seen:
            raise EnvironmentFileError("duplicate-key", display_path)
        seen.add(name)
        names.append(name)
    try:
        parsed = dotenv.dotenv_values(stream=io.StringIO(payload))
    except Exception:
        raise EnvironmentFileError("value-invalid", display_path) from None
    values: dict[str, str] = {}
    for name in names:
        value = parsed.get(name)
        if value is None:
            raise EnvironmentFileError("parse-failed", display_path)
        values[name] = value
    return values


def _apply_missing_environment(values: dict[str, str], display_path: str) -> None:
    try:
        pending = [
            (key, value)
            for key, value in values.items()
            if key not in os.environ
        ]
    except Exception:
        raise EnvironmentFileError("application-failed", display_path) from None
    applied: list[str] = []
    try:
        for key, value in pending:
            applied.append(key)
            os.environ[key] = value
    except Exception:
        rollback_failed = False
        for key in reversed(applied):
            try:
                if key in os.environ:
                    del os.environ[key]
            except Exception:
                rollback_failed = True
        reason = "rollback-failed" if rollback_failed else "application-failed"
        raise EnvironmentFileError(reason, display_path) from None


@cache
def load_application_environment() -> Path:
    """Load exactly one startup environment source without overriding process env."""

    raw_path = os.environ.get(ENVIRONMENT_FILE_KEY)
    if raw_path is None:
        try:
            dotenv.load_dotenv(dotenv_path=PROJECT_DOTENV, override=False)
        except Exception:
            raise EnvironmentFileError(
                "load-failed",
                os.fspath(PROJECT_DOTENV),
            ) from None
        return PROJECT_DOTENV

    if not raw_path.strip():
        raise EnvironmentFileError("empty", raw_path)
    try:
        path = Path(raw_path)
    except (TypeError, ValueError):
        raise EnvironmentFileError("invalid-path", raw_path) from None
    if not path.is_absolute():
        raise EnvironmentFileError("not-absolute", raw_path)

    payload = _read_explicit_environment_file(path, raw_path)
    values = _explicit_environment_file_values(payload, raw_path)
    _apply_missing_environment(values, raw_path)
    return path


__all__ = (
    "ENVIRONMENT_FILE_KEY",
    "EnvironmentFileError",
    "EnvironmentSubsetError",
    "PROJECT_DOTENV",
    "load_application_environment",
    "parse_environment_subset",
)
