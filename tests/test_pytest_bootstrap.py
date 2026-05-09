import sys
import unittest
from pathlib import Path


class PytestBootstrapTests(unittest.TestCase):
    def test_project_root_is_available_in_sys_path(self):
        project_root = str(Path(__file__).resolve().parents[1])
        self.assertIn(project_root, sys.path)


if __name__ == "__main__":
    unittest.main()
