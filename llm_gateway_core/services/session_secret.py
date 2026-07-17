"""Persistent secret used to sign browser session cookies.

Signing cookies with ``GATEWAY_API_KEY`` itself turns every issued cookie into
an offline oracle: its holder knows both the signed payload and the matching
signature, so candidate master keys can be checked locally at GPU speed without
ever reaching the gateway, where ``IpBlockGuard`` would throttle them. A cookie
signed with the random secret kept here reveals nothing about the master key,
which leaves it attackable only online.

The secret lives next to the SQLite databases so sessions keep surviving a
restart. Rotating ``GATEWAY_API_KEY`` still ends every live session: the stored
fingerprint stops matching and the secret is replaced.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any

from ..config.paths import resolve_db_dir

logger = logging.getLogger(__name__)

SECRET_FILENAME = "session_secret.json"
SECRET_VERSION = 1

_SECRET_BYTES = 32
_SALT_BYTES = 16

# A plain digest of GATEWAY_API_KEY on disk would be the very oracle this module
# exists to remove, so the fingerprint is deliberately expensive to grind. It is
# computed once per process, at startup.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

_cached_secret: bytes | None = None
_cache_guard = threading.Lock()


class SessionSecretError(RuntimeError):
    """Raised when the stored session secret cannot be read or created."""


def _fingerprint(api_key: str, salt: bytes) -> str:
    return hashlib.scrypt(
        api_key.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    ).hex()


def _write_atomically(path: Path, document: dict[str, Any]) -> None:
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".session_secret-",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _issue(path: Path, api_key: str) -> bytes:
    secret = secrets.token_bytes(_SECRET_BYTES)
    salt = secrets.token_bytes(_SALT_BYTES)
    try:
        _write_atomically(
            path,
            {
                "version": SECRET_VERSION,
                "secret": secret.hex(),
                "salt": salt.hex(),
                "fingerprint": _fingerprint(api_key, salt),
            },
        )
    except OSError as error:
        raise SessionSecretError(
            f"Session secret at {path} could not be written: {error}"
        ) from error
    return secret


def load_or_create_session_secret(path: Path, api_key: str) -> bytes:
    """Return the secret stored at ``path``, issuing a new one when required.

    A missing file means a first start. A fingerprint that no longer matches
    ``api_key`` means ``GATEWAY_API_KEY`` was rotated, which must end every live
    session, so the secret is replaced. Anything else is refused rather than
    quietly replaced: silently issuing a new secret would log every browser out
    without saying why.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _issue(path, api_key)
    except OSError as error:
        raise SessionSecretError(
            f"Session secret at {path} could not be read: {error}"
        ) from error

    try:
        document = json.loads(raw)
        version = document["version"]
        secret = bytes.fromhex(document["secret"])
        salt = bytes.fromhex(document["salt"])
        fingerprint = document["fingerprint"]
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise SessionSecretError(
            f"Session secret at {path} is unusable: {error}. Delete the file to issue a "
            "new one, which logs every active browser session out."
        ) from error

    if version != SECRET_VERSION:
        raise SessionSecretError(
            f"Session secret at {path} has unsupported version {version!r}; "
            f"this gateway writes version {SECRET_VERSION}."
        )
    if len(secret) != _SECRET_BYTES or not isinstance(fingerprint, str):
        raise SessionSecretError(f"Session secret at {path} is malformed.")

    if not hmac.compare_digest(fingerprint, _fingerprint(api_key, salt)):
        logger.warning(
            "GATEWAY_API_KEY changed since the session secret at %s was issued; "
            "replacing it, which logs every active browser session out.",
            path,
        )
        return _issue(path, api_key)
    return secret


def get_session_secret(api_key: str | None) -> bytes | None:
    """Return the process-wide session-signing secret for ``api_key``.

    ``None`` means no API key is configured, which leaves session auth off and
    is reported by the caller. The secret is cached because the fingerprint is
    intentionally slow to compute, and because ``GATEWAY_API_KEY`` is read from
    the environment at import time and so cannot change without a restart.
    """
    if not api_key or not api_key.strip():
        return None
    global _cached_secret
    with _cache_guard:
        if _cached_secret is None:
            _cached_secret = load_or_create_session_secret(
                resolve_db_dir() / SECRET_FILENAME,
                api_key,
            )
        return _cached_secret
