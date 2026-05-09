import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from llm_gateway_core.services.image_retention import cleanup_old_images


def _touch(path: Path, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n")
    os.utime(path, (mtime, mtime))
    return path


class CleanupOldImagesTests(unittest.TestCase):
    def test_retention_days_must_be_positive(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                cleanup_old_images(root, retention_days=0)
            with self.assertRaises(ValueError):
                cleanup_old_images(root, retention_days=-1)

    def test_missing_root_is_noop(self):
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            self.assertEqual(cleanup_old_images(missing, retention_days=10), 0)

    def test_only_files_older_than_retention_are_removed(self):
        now = time.time()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            recent = _touch(root / "recent" / "a.png", now - 24 * 60 * 60)
            stale = _touch(root / "old" / "b.png", now - 15 * 24 * 60 * 60)

            deleted = cleanup_old_images(root, retention_days=10)

            self.assertEqual(deleted, 1)
            self.assertTrue(recent.exists())
            self.assertFalse(stale.exists())
            # The now-empty "old" directory should also be removed.
            self.assertFalse((root / "old").exists())

    def test_non_empty_directories_are_preserved(self):
        now = time.time()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            kept = _touch(root / "mixed" / "fresh.png", now - 60)
            _touch(root / "mixed" / "stale.png", now - 20 * 24 * 60 * 60)

            deleted = cleanup_old_images(root, retention_days=10)

            self.assertEqual(deleted, 1)
            self.assertTrue(kept.exists())
            self.assertTrue((root / "mixed").is_dir())


if __name__ == "__main__":
    unittest.main()
