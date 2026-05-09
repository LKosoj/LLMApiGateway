"""Unified PROJECT_ROOT in llm_gateway_core/config/paths.py.

Before, each module that needed the repo root recomputed
``Path(__file__).parent.parent.parent`` (or .parent.parent.parent.parent!),
which silently broke whenever a module moved. Everything now imports
from one module, which these tests pin.
"""
import unittest
from pathlib import Path

from llm_gateway_core.config import paths as project_paths


class ProjectPathsTests(unittest.TestCase):
    def test_project_root_points_at_repository_root(self):
        # main.py is the entrypoint — by definition, it lives in the project root.
        self.assertTrue((project_paths.PROJECT_ROOT / "main.py").is_file())

    def test_project_root_is_absolute(self):
        self.assertTrue(project_paths.PROJECT_ROOT.is_absolute())

    def test_static_dir_is_under_project_root(self):
        self.assertEqual(
            project_paths.STATIC_DIR,
            project_paths.PROJECT_ROOT / "static",
        )

    def test_loader_uses_same_project_root(self):
        from llm_gateway_core.config.loader import ConfigLoader

        loader = ConfigLoader()
        self.assertEqual(Path(loader.project_root), project_paths.PROJECT_ROOT)


if __name__ == "__main__":
    unittest.main()
