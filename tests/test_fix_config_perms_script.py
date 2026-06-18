import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fix_config_perms.sh"
CONFIG_FILES = [
    "providers.json",
    "models_fallback_rules.json",
    "models_operation_rules.json",
    "models_fusion_rules.json",
    "models_model_rules.json",
]


class FixConfigPermsScriptTests(unittest.TestCase):
    def test_script_exists_and_executable(self):
        self.assertTrue(SCRIPT.is_file(), f"missing {SCRIPT}")
        self.assertTrue(os.access(SCRIPT, os.X_OK), "script not executable")

    def test_script_syntax_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_targets_all_config_files(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for name in CONFIG_FILES:
            self.assertIn(name, text, f"{name} not referenced in script")

    def test_script_grants_group_read_write(self):
        # Прогон end-to-end на временных файлах, принадлежащих текущему
        # пользователю: sudo-ветка не срабатывает (мы владелец), и скрипт
        # реально приводит права к 660.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            scripts_dir = repo / "scripts"
            scripts_dir.mkdir()
            local_script = scripts_dir / "fix_config_perms.sh"
            local_script.write_bytes(SCRIPT.read_bytes())
            local_script.chmod(0o755)
            for name in CONFIG_FILES:
                f = repo / name
                f.write_text("{}", encoding="utf-8")
                f.chmod(0o600)

            env = dict(os.environ)
            # Группа, в которой пользователь заведомо состоит (его primary gid),
            # чтобы chgrp прошёл без root.
            env["FIX_CONFIG_GROUP"] = str(os.getgid())
            result = subprocess.run(
                [str(local_script)], capture_output=True, text=True, env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in CONFIG_FILES:
                mode = (repo / name).stat().st_mode & 0o777
                self.assertEqual(mode, 0o660, f"{name} mode={oct(mode)}")


if __name__ == "__main__":
    unittest.main()
