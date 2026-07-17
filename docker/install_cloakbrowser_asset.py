#!/usr/bin/env python3
"""Install the pinned CloakBrowser Chromium asset for a container build."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import tarfile
import tempfile
import urllib.request
from pathlib import Path


ENGINE_VERSION = "146.0.7680.177.3"
ASSETS = {
    "amd64": (
        "linux-x64",
        "5af027faafb1fef9933eb784c094b764706de22a372a2cee84bc117fc4ab537f",
        62,
    ),
    "arm64": (
        "linux-arm64",
        "8b71ce53b4fd131327331a31fba3835d71882d19bfaabde78dd0f5390bd16f45",
        183,
    ),
}
RELEASE_BASE_URL = (
    "https://github.com/CloakHQ/cloakbrowser/releases/download/"
    f"chromium-v{ENGINE_VERSION}"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target, urllib.request.urlopen(
            url, timeout=600
        ) as response:
            shutil.copyfileobj(response, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def _verified_archive(url: str, archive: Path, expected_sha256: str) -> Path:
    if archive.exists() and _sha256(archive) != expected_sha256:
        archive.unlink()
    if not archive.exists():
        _download(url, archive)
    actual_sha256 = _sha256(archive)
    if actual_sha256 != expected_sha256:
        archive.unlink(missing_ok=True)
        raise RuntimeError(
            "CloakBrowser asset checksum mismatch: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )
    return archive


def _validate_elf_machine(binary: Path, expected_machine: int) -> None:
    with binary.open("rb") as source:
        header = source.read(20)
    if (
        len(header) != 20
        or header[:4] != b"\x7fELF"
        or header[4] != 2
        or header[5] != 1
        or int.from_bytes(header[18:20], "little") != expected_machine
    ):
        raise RuntimeError("CloakBrowser chrome binary has an unexpected ELF architecture")


def _extract_archive(
    archive: Path,
    destination: Path,
    *,
    expected_machine: int,
) -> None:
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(dir=destination.parent, prefix=f".{destination.name}.")
    )
    try:
        with tarfile.open(archive, mode="r:gz") as source:
            source.extractall(temporary, filter="data")
        entries = list(temporary.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            wrapper = entries[0]
            for child in wrapper.iterdir():
                shutil.move(os.fspath(child), temporary / child.name)
            wrapper.rmdir()
        binary = temporary / "chrome"
        if not binary.is_file() or binary.is_symlink():
            raise RuntimeError("CloakBrowser archive does not contain a regular chrome binary")
        _validate_elf_machine(binary, expected_machine)
        binary.chmod(0o555)
        for path in temporary.rglob("*"):
            if path.is_symlink():
                continue
            if path.is_dir():
                path.chmod(0o555)
            elif path.is_file():
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
        temporary.chmod(0o555)
        os.replace(temporary, destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def install_asset(arch: str, archive_cache: Path, destination: Path) -> None:
    try:
        platform_name, expected_sha256, expected_machine = ASSETS[arch]
    except KeyError as exc:
        raise ValueError(f"unsupported target architecture: {arch}") from exc
    archive_name = f"cloakbrowser-{platform_name}.tar.gz"
    archive = archive_cache / archive_name
    url = f"{RELEASE_BASE_URL}/{archive_name}"
    _extract_archive(
        _verified_archive(url, archive, expected_sha256),
        destination,
        expected_machine=expected_machine,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=sorted(ASSETS), required=True)
    parser.add_argument("--archive-cache", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    install_asset(args.arch, args.archive_cache, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
