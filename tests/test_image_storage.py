from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

import llm_gateway_core.services.image_storage as image_storage
from llm_gateway_core.services.image_storage import GeneratedImageStorage, ImageStorageError


PNG = b"\x89PNG\r\n\x1a\n" + b"image-bytes"
FILENAME = "image_deadbeef_0.png"


def _storage(tmp_path: Path) -> GeneratedImageStorage:
    root = tmp_path / "outputs" / "images"
    root.mkdir(parents=True)
    return GeneratedImageStorage(root)


def _temporary_entries(root: Path) -> list[Path]:
    return [entry for entry in root.rglob("*") if entry.name.startswith(".image_")]


def test_publish_png_writes_complete_file_and_returns_authenticated_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path)
    original_write = image_storage.os.write

    def short_write(fd: int, content: bytes) -> int:
        return original_write(fd, content[:3])

    monkeypatch.setattr(image_storage.os, "write", short_write)

    published = storage.publish_png(PNG, FILENAME, research_id="research-1")

    assert published.path == storage.images_root / "research-1" / FILENAME
    assert published.path.read_bytes() == PNG
    assert published.url == f"/outputs/images/research-1/{FILENAME}"
    assert _temporary_entries(storage.images_root) == []


def test_publish_png_without_research_id_uses_images_root(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    published = storage.publish_png(PNG, FILENAME)

    assert published.path == storage.images_root / FILENAME
    assert published.url == f"/outputs/images/{FILENAME}"


@pytest.mark.parametrize("research_id", ["_private", "-batch", "a.b_c-1"])
def test_publish_png_accepts_safe_non_hidden_components(tmp_path: Path, research_id: str) -> None:
    storage = _storage(tmp_path)

    published = storage.publish_png(PNG, FILENAME, research_id=research_id)

    assert published.path.read_bytes() == PNG
    assert published.url == f"/outputs/images/{research_id}/{FILENAME}"


@pytest.mark.parametrize(
    "research_id",
    [".", "..", ".hidden", "nested/id", r"nested\id", "has space", "control\n", "кириллица", "a" * 129],
)
def test_publish_png_rejects_unsafe_research_id(tmp_path: Path, research_id: str) -> None:
    storage = _storage(tmp_path)

    with pytest.raises(ImageStorageError) as exc_info:
        storage.publish_png(PNG, FILENAME, research_id=research_id)

    assert exc_info.value.reason == "research-id-invalid"
    assert research_id not in str(exc_info.value)
    assert list(storage.images_root.iterdir()) == []


@pytest.mark.parametrize(
    "filename",
    ["image.png", "../image_deadbeef_0.png", ".image_deadbeef_0.png", "image_DEADBEEF_0.png", "image_deadbeef_x.png"],
)
def test_publish_png_rejects_non_owned_filename(tmp_path: Path, filename: str) -> None:
    storage = _storage(tmp_path)

    with pytest.raises(ImageStorageError) as exc_info:
        storage.publish_png(PNG, filename)

    assert exc_info.value.reason == "filename-invalid"
    assert filename not in str(exc_info.value)


def test_publish_png_rejects_relative_root_without_exposing_it(tmp_path: Path) -> None:
    secret_component = "credential-secret"

    with pytest.raises(ImageStorageError) as exc_info:
        GeneratedImageStorage(Path(secret_component) / "images")

    assert exc_info.value.reason == "images-root-invalid"
    assert secret_component not in str(exc_info.value)


def test_publish_png_never_follows_images_root_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    images = tmp_path / "outputs" / "images"
    images.parent.mkdir()
    images.symlink_to(outside, target_is_directory=True)
    storage = GeneratedImageStorage(images)

    with pytest.raises(ImageStorageError) as exc_info:
        storage.publish_png(PNG, FILENAME)

    assert exc_info.value.reason == "images-root-unsafe"
    assert list(outside.iterdir()) == []


def test_publish_png_never_follows_research_directory_symlink(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (storage.images_root / "research-1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ImageStorageError) as exc_info:
        storage.publish_png(PNG, FILENAME, research_id="research-1")

    assert exc_info.value.reason == "research-directory-unsafe"
    assert list(outside.iterdir()) == []


def test_publish_png_rejects_non_regular_existing_target(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    (storage.images_root / FILENAME).symlink_to(outside)

    with pytest.raises(ImageStorageError) as exc_info:
        storage.publish_png(PNG, FILENAME)

    assert exc_info.value.reason == "target-unsafe"
    assert outside.read_bytes() == b"outside"
    assert (storage.images_root / FILENAME).is_symlink()


def test_publish_png_failure_before_replace_preserves_previous_file_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path)
    final = storage.images_root / FILENAME
    final.write_bytes(b"previous")
    original_fsync = image_storage.os.fsync

    def fail_file_fsync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "secret-path")
        original_fsync(fd)

    monkeypatch.setattr(image_storage.os, "fsync", fail_file_fsync)

    with pytest.raises(ImageStorageError) as exc_info:
        storage.publish_png(PNG, FILENAME)

    assert exc_info.value.reason == "image-sync-failed"
    assert not exc_info.value.publication_uncertain
    assert "secret-path" not in str(exc_info.value)
    assert final.read_bytes() == b"previous"
    assert _temporary_entries(storage.images_root) == []


def test_publish_png_cleans_temp_and_propagates_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path)

    def cancel_write(_fd: int, _content: bytes) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(image_storage, "_write_all", cancel_write)

    with pytest.raises(KeyboardInterrupt):
        storage.publish_png(PNG, FILENAME)

    assert not (storage.images_root / FILENAME).exists()
    assert _temporary_entries(storage.images_root) == []


def test_publish_png_reports_directory_sync_failure_as_publication_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path)
    original_fsync = image_storage.os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory-secret")
        original_fsync(fd)

    monkeypatch.setattr(image_storage.os, "fsync", fail_directory_fsync)

    with pytest.raises(ImageStorageError) as exc_info:
        storage.publish_png(PNG, FILENAME)

    assert exc_info.value.reason == "directory-sync-failed"
    assert exc_info.value.publication_uncertain
    assert (storage.images_root / FILENAME).read_bytes() == PNG
    assert _temporary_entries(storage.images_root) == []


def test_publish_png_detects_replace_that_completed_before_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path)
    original_replace = image_storage.os.replace

    def replace_then_fail(*args: object, **kwargs: object) -> None:
        original_replace(*args, **kwargs)
        raise OSError(errno.EIO, "post-replace-secret")

    monkeypatch.setattr(image_storage.os, "replace", replace_then_fail)

    with pytest.raises(ImageStorageError) as exc_info:
        storage.publish_png(PNG, FILENAME)

    assert exc_info.value.reason == "image-replace-failed"
    assert exc_info.value.publication_uncertain
    assert (storage.images_root / FILENAME).read_bytes() == PNG
    assert "post-replace-secret" not in str(exc_info.value)
    assert _temporary_entries(storage.images_root) == []


def test_probe_proves_write_rename_delete_without_artifacts(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    storage.probe()

    assert list(storage.images_root.iterdir()) == []


def test_probe_fails_closed_when_restore_marker_is_any_entry(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    secret_target = tmp_path / "credential-secret"
    secret_target.write_text("secret", encoding="utf-8")
    marker = storage.images_root.parent / ".image-restore-incomplete"
    marker.symlink_to(secret_target)

    with pytest.raises(ImageStorageError) as exc_info:
        storage.probe()

    assert exc_info.value.reason == "restore-incomplete"
    assert "credential-secret" not in str(exc_info.value)
    assert marker.is_symlink()


def test_probe_does_not_create_missing_images_root(tmp_path: Path) -> None:
    images = tmp_path / "outputs" / "images"
    images.parent.mkdir()
    storage = GeneratedImageStorage(images)

    with pytest.raises(ImageStorageError) as exc_info:
        storage.probe()

    assert exc_info.value.reason == "images-root-missing"
    assert not images.exists()


def test_probe_failure_cleans_temporary_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path)

    def fail_write(_fd: int, _content: bytes) -> None:
        raise OSError(errno.EIO, "probe-secret")

    monkeypatch.setattr(image_storage, "_write_all", fail_write)

    with pytest.raises(ImageStorageError) as exc_info:
        storage.probe()

    assert exc_info.value.reason == "probe-write-failed"
    assert "probe-secret" not in str(exc_info.value)
    assert list(storage.images_root.iterdir()) == []
