from __future__ import annotations

import hashlib
import importlib.util
import io
import stat
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = PROJECT_ROOT / "docker" / "install_cloakbrowser_asset.py"
AMD64_ELF_MACHINE = 62
ARM64_ELF_MACHINE = 183


def _load_installer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "install_cloakbrowser_asset",
        INSTALLER_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _elf_payload(machine: int = AMD64_ELF_MACHINE) -> bytes:
    header = bytearray(20)
    header[:7] = b"\x7fELF\x02\x01\x01"
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header) + b"pinned chromium sentinel\n"


def _write_archive(
    path: Path,
    *,
    member_name: str = "payload/chrome",
    machine: int = AMD64_ELF_MACHINE,
) -> bytes:
    payload = _elf_payload(machine)
    with tarfile.open(path, mode="w:gz") as archive:
        info = tarfile.TarInfo(member_name)
        info.size = len(payload)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(payload))
        if member_name == "payload/chrome":
            resource = tarfile.TarInfo("payload/resources.pak")
            resource.size = len(payload)
            resource.mode = 0o644
            archive.addfile(resource, io.BytesIO(payload))
    return payload


def _write_archive_with_unsafe_member(path: Path, kind: str) -> None:
    payload = _elf_payload()
    with tarfile.open(path, mode="w:gz") as archive:
        chrome = tarfile.TarInfo("payload/chrome")
        chrome.size = len(payload)
        chrome.mode = 0o755
        archive.addfile(chrome, io.BytesIO(payload))

        unsafe = tarfile.TarInfo("payload/unsafe")
        if kind == "symlink":
            unsafe.type = tarfile.SYMTYPE
            unsafe.linkname = "../../escaped"
        elif kind == "hardlink":
            unsafe.type = tarfile.LNKTYPE
            unsafe.linkname = "../../escaped"
        elif kind == "fifo":
            unsafe.type = tarfile.FIFOTYPE
        else:  # pragma: no cover - test helper contract
            raise AssertionError(f"unexpected archive member kind: {kind}")
        archive.addfile(unsafe)


def test_asset_manifest_pins_both_linux_architectures() -> None:
    installer = _load_installer()

    assert installer.ENGINE_VERSION == "146.0.7680.177.3"
    assert installer.ASSETS == {
        "amd64": (
            "linux-x64",
            "5af027faafb1fef9933eb784c094b764706de22a372a2cee84bc117fc4ab537f",
            AMD64_ELF_MACHINE,
        ),
        "arm64": (
            "linux-arm64",
            "8b71ce53b4fd131327331a31fba3835d71882d19bfaabde78dd0f5390bd16f45",
            ARM64_ELF_MACHINE,
        ),
    }


def test_verified_archive_downloads_and_rejects_checksum_mismatch(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    source = tmp_path / "source.tar.gz"
    _write_archive(source)
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    cached = tmp_path / "cache" / "asset.tar.gz"

    assert installer._verified_archive(source.as_uri(), cached, expected) == cached
    assert cached.read_bytes() == source.read_bytes()

    mismatched = tmp_path / "cache" / "mismatched.tar.gz"
    with pytest.raises(RuntimeError, match="asset checksum mismatch"):
        installer._verified_archive(source.as_uri(), mismatched, "0" * 64)
    assert not mismatched.exists()


def test_extract_archive_flattens_wrapper_and_makes_binary_read_only(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    archive = tmp_path / "asset.tar.gz"
    payload = _write_archive(archive)
    destination = tmp_path / "chromium"

    installer._extract_archive(
        archive,
        destination,
        expected_machine=AMD64_ELF_MACHINE,
    )

    binary = destination / "chrome"
    assert binary.read_bytes() == payload
    assert stat.S_IMODE(binary.stat().st_mode) == 0o555
    assert stat.S_IMODE(destination.stat().st_mode) == 0o555
    assert stat.S_IMODE((destination / "resources.pak").stat().st_mode) == 0o444
    assert not (destination / "payload").exists()


def test_extract_archive_rejects_path_traversal(tmp_path: Path) -> None:
    installer = _load_installer()
    archive = tmp_path / "malicious.tar.gz"
    _write_archive(archive, member_name="../escaped")
    destination = tmp_path / "chromium"

    with pytest.raises(tarfile.FilterError):
        installer._extract_archive(
            archive,
            destination,
            expected_machine=AMD64_ELF_MACHINE,
        )

    assert not destination.exists()
    assert not (tmp_path / "escaped").exists()


def test_extract_archive_rejects_wrong_elf_architecture(tmp_path: Path) -> None:
    installer = _load_installer()
    archive = tmp_path / "wrong-architecture.tar.gz"
    _write_archive(archive, machine=AMD64_ELF_MACHINE)
    destination = tmp_path / "chromium"

    with pytest.raises(RuntimeError, match="unexpected ELF architecture"):
        installer._extract_archive(
            archive,
            destination,
            expected_machine=ARM64_ELF_MACHINE,
        )

    assert not destination.exists()


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo"))
def test_extract_archive_rejects_unsafe_member_types(
    tmp_path: Path,
    kind: str,
) -> None:
    installer = _load_installer()
    archive = tmp_path / f"unsafe-{kind}.tar.gz"
    _write_archive_with_unsafe_member(archive, kind)
    destination = tmp_path / "chromium"

    with pytest.raises(tarfile.FilterError):
        installer._extract_archive(
            archive,
            destination,
            expected_machine=AMD64_ELF_MACHINE,
        )

    assert not destination.exists()
    assert not (tmp_path / "escaped").exists()
