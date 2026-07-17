from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

import llm_gateway_core.services.image_retention as image_retention
from llm_gateway_core.services.image_retention import ImageRetentionService, cleanup_old_images
from llm_gateway_core.services.image_storage import GeneratedImageStorage


NOW = 2_000_000_000.0
DAY = 24 * 60 * 60
FINAL = "image_deadbeef_0.png"
TEMP = ".image_deadbeef_0.png.llmgateway-0123456789abcdef0123456789abcdef.tmp"


def _storage(tmp_path: Path) -> GeneratedImageStorage:
    root = tmp_path / "outputs" / "images"
    root.mkdir(parents=True)
    return GeneratedImageStorage(root)


def _touch(path: Path, *, age_days: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    mtime = NOW - age_days * DAY
    os.utime(path, (mtime, mtime), follow_symlinks=False)
    return path


def _service(storage: GeneratedImageStorage, retention_days: int = 10) -> ImageRetentionService:
    return ImageRetentionService(storage, retention_days=retention_days, clock=lambda: NOW)


def test_retention_days_must_be_positive(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    with pytest.raises(ValueError):
        ImageRetentionService(storage, retention_days=0)
    with pytest.raises(ValueError):
        ImageRetentionService(storage, retention_days=-1)


def test_initial_snapshot_is_path_free_and_has_not_run(tmp_path: Path) -> None:
    service = _service(_storage(tmp_path))

    snapshot = service.snapshot()

    assert snapshot.last_run is None
    assert snapshot.deleted_final == 0
    assert snapshot.deleted_temp == 0
    assert snapshot.deleted_dirs == 0
    assert snapshot.failures == 0
    assert str(tmp_path) not in repr(snapshot)


def test_run_deletes_only_stale_owned_files_and_empty_research_directories(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    service = _service(storage)
    stale_final = _touch(storage.images_root / "research-1" / FINAL, age_days=11)
    stale_temp = _touch(storage.images_root / TEMP, age_days=11)
    fresh_final = _touch(storage.images_root / "research-2" / FINAL, age_days=9)

    snapshot = service.run()

    assert not stale_final.exists()
    assert not stale_temp.exists()
    assert not (storage.images_root / "research-1").exists()
    assert fresh_final.exists()
    assert snapshot.last_run == NOW
    assert snapshot.deleted_final == 1
    assert snapshot.deleted_temp == 1
    assert snapshot.deleted_dirs == 1
    assert snapshot.failures == 0
    assert service.snapshot() == snapshot


def test_run_preserves_foreign_files_links_special_entries_and_invalid_directories(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    outside = _touch(tmp_path / "outside.png", age_days=20)
    linked_final = storage.images_root / FINAL
    linked_final.symlink_to(outside)
    foreign = _touch(storage.images_root / "foreign.txt", age_days=20)
    invalid_nested = _touch(storage.images_root / ".hidden" / FINAL, age_days=20)
    nested_directory = storage.images_root / "research-1" / "nested"
    nested_directory.mkdir(parents=True)
    fifo = storage.images_root / "research-2"
    fifo.mkdir()
    os.mkfifo(fifo / FINAL)

    snapshot = _service(storage).run()

    assert linked_final.is_symlink()
    assert outside.read_bytes() == b"data"
    assert foreign.exists()
    assert invalid_nested.exists()
    assert nested_directory.exists()
    assert (fifo / FINAL).exists()
    assert snapshot.deleted_final == 0
    assert snapshot.deleted_temp == 0
    assert snapshot.deleted_dirs == 0
    assert snapshot.failures == 0


def test_run_preserves_fresh_owned_temporary_file(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    fresh_temp = _touch(storage.images_root / "research-1" / TEMP, age_days=1)

    snapshot = _service(storage).run()

    assert fresh_temp.exists()
    assert snapshot.deleted_temp == 0
    assert snapshot.failures == 0


def test_run_failure_is_path_free_and_next_success_resets_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path)
    stale = _touch(storage.images_root / FINAL, age_days=20)
    service = _service(storage)
    original_unlink = image_retention.os.unlink

    def fail_unlink(*_args: object, **_kwargs: object) -> None:
        raise OSError("credential-secret")

    monkeypatch.setattr(image_retention.os, "unlink", fail_unlink)
    failed = service.run()

    assert stale.exists()
    assert failed.failures == 1
    assert "credential-secret" not in repr(failed)
    assert str(tmp_path) not in repr(failed)
    assert service.snapshot() == failed

    monkeypatch.setattr(image_retention.os, "unlink", original_unlink)
    recovered = service.run()

    assert not stale.exists()
    assert recovered.failures == 0
    assert recovered.deleted_final == 1
    assert service.snapshot() == recovered


def test_missing_root_publishes_failed_snapshot_instead_of_raising(tmp_path: Path) -> None:
    images = tmp_path / "outputs" / "images"
    service = _service(GeneratedImageStorage(images))

    snapshot = service.run()

    assert snapshot.last_run == NOW
    assert snapshot.failures == 1
    assert snapshot.deleted_final == 0
    assert snapshot.deleted_temp == 0
    assert snapshot.deleted_dirs == 0


def test_retention_and_publication_share_the_storage_lock(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    service = _service(storage)
    started = threading.Event()
    finished = threading.Event()

    def run_retention() -> None:
        started.set()
        service.run()
        finished.set()

    worker = threading.Thread(target=run_retention)
    with storage.exclusive():
        worker.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.05)
    assert finished.wait(timeout=1)
    worker.join(timeout=1)
    assert not worker.is_alive()


def test_cleanup_old_images_keeps_missing_root_compatibility(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    assert cleanup_old_images(missing, retention_days=10) == 0
