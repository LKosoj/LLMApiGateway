from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from tests.ui_server_helpers import run_process_with_pid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
ENTRYPOINT = PROJECT_ROOT / "docker" / "entrypoint.sh"
PREFLIGHT = "python -m llm_gateway_core.services.single_process --check-environment"
CONFIG_PREFLIGHT = "python -m llm_gateway_core.config.container_preflight"
SAFE_EXEC = "exec python -m llm_gateway_core.services.container_exec"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _significant_shell_lines(source: str) -> list[str]:
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_image_and_compose_pin_exact_single_worker_assertion() -> None:
    dockerfile = _read(DOCKERFILE)
    compose = _read(COMPOSE_FILE)
    runtime_stage = dockerfile[dockerfile.index(" AS runtime") :]
    docker_environment = re.search(r"(?ms)^ENV .*?(?=^EXPOSE )", runtime_stage)
    assert docker_environment is not None
    compose_environment = compose[
        compose.index("    environment:") : compose.index("    healthcheck:")
    ]

    assert docker_environment.group(0).count("GATEWAY_WORKERS=1") == 1
    assert compose_environment.count("- GATEWAY_WORKERS=1") == 1
    assert dockerfile.count("GATEWAY_WORKERS=") == 1
    assert compose.count("GATEWAY_WORKERS=") == 1


def test_repository_owned_container_command_remains_canonical_and_single_worker() -> None:
    dockerfile = _read(DOCKERFILE)
    compose = _read(COMPOSE_FILE)
    entrypoint = _read(ENTRYPOINT)
    default_commands = re.findall(r"^CMD\s+(.+)$", dockerfile, flags=re.MULTILINE)
    gateway_service = compose[
        compose.index("  llm-gateway:") : compose.index("\nnetworks:")
    ]

    assert default_commands == ['["python", "main.py"]']
    assert not re.search(
        r"^    (?:command|entrypoint):",
        gateway_service,
        flags=re.MULTILINE,
    )

    launch_sources = "\n".join((dockerfile, compose, entrypoint))
    assert re.search(r"(?i)\bgunicorn\b", launch_sources) is None
    assert re.search(r"(?:^|\s)--workers(?:=|\s)", launch_sources) is None
    assert "WEB_CONCURRENCY" not in launch_sources
    assert "UVICORN_WORKERS" not in launch_sources
    assert "GUNICORN_CMD_ARGS" not in launch_sources
    assert re.search(r"^\s*(?:replicas|scale)\s*:", compose, flags=re.MULTILINE) is None


def test_entrypoint_has_one_config_prepare_before_every_observable_startup_action() -> None:
    entrypoint = _read(ENTRYPOINT)
    commands = _significant_shell_lines(entrypoint)

    assert commands[0] == "set -euo pipefail"
    assert entrypoint.count(CONFIG_PREFLIGHT) == 1
    assert "--materialize-legacy-defaults" not in entrypoint
    preflight_index = entrypoint.index(PREFLIGHT)
    config_preflight_index = entrypoint.index(CONFIG_PREFLIGHT)
    command_check_index = entrypoint.index('command -v -- "$1"')
    assert command_check_index < preflight_index
    assert preflight_index < config_preflight_index
    for later_operation in (
        'echo "LLM Gateway starting..."',
        "mkdir -p /app/logs /app/db",
        f'{SAFE_EXEC} "$@"',
    ):
        assert config_preflight_index < entrypoint.index(later_operation)
    assert f"{PREFLIGHT} ||" not in entrypoint
    assert f"{CONFIG_PREFLIGHT} ||" not in entrypoint


def test_entrypoint_rejects_empty_argv_before_python_or_filesystem_work(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "unexpected-side-effect"
    for name in ("python", "mkdir"):
        executable = fake_bin / name
        executable.write_text(
            f"#!/bin/sh\nprintf '%s\\n' {name!r} >> {os.fspath(marker)!r}\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)

    completed = subprocess.run(
        [str(ENTRYPOINT)],
        cwd=PROJECT_ROOT,
        env={"PATH": os.fspath(fake_bin)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 64
    assert completed.stdout == ""
    assert completed.stderr == "container-entrypoint: reason=missing-command\n"
    assert not marker.exists()


def test_entrypoint_rejects_unknown_command_without_printing_argv_or_side_effects(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "unexpected-side-effect"
    for name in ("python", "mkdir"):
        executable = fake_bin / name
        executable.write_text(
            f"#!/bin/sh\nprintf '%s\\n' {name!r} >> {os.fspath(marker)!r}\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
    secret_command = "super-secret-command"

    completed = subprocess.run(
        [str(ENTRYPOINT), secret_command],
        cwd=PROJECT_ROOT,
        env={"PATH": os.fspath(fake_bin)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 126
    assert completed.stdout == ""
    assert completed.stderr == "container-entrypoint: reason=command-not-executable\n"
    assert secret_command not in completed.stderr
    assert not marker.exists()


def test_entrypoint_preflight_failure_stops_before_startup_output(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "printf 'preflight:%s\\n' \"$*\"\n"
        "exit 37\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    completed = subprocess.run(
        [str(ENTRYPOINT), "/bin/true"],
        cwd=PROJECT_ROOT,
        env={"PATH": os.fspath(fake_bin)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 37
    assert completed.stdout == (
        "preflight:-m llm_gateway_core.services.single_process --check-environment\n"
    )
    assert completed.stderr == ""


def test_entrypoint_real_preflight_rejects_invalid_worker_before_startup_output() -> None:
    secret = "entrypoint-environment-secret-sentinel"
    completed = subprocess.run(
        [str(ENTRYPOINT), "/bin/true"],
        cwd=PROJECT_ROOT,
        env={
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "PYTHONPATH": os.fspath(PROJECT_ROOT),
            "GATEWAY_WORKERS": "2",
            "SECRET_SENTINEL": secret,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "source=GATEWAY_WORKERS reason=invalid-worker-count" in completed.stderr
    assert "LLM Gateway starting" not in completed.stderr
    assert secret not in completed.stderr


def test_entrypoint_config_preflight_failure_stops_before_mkdir_and_startup_output(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "mkdir-called"
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        f"if [ \"$*\" = {CONFIG_PREFLIGHT.removeprefix('python ')!r} ]; then exit 41; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_mkdir = fake_bin / "mkdir"
    fake_mkdir.write_text(
        f"#!/bin/sh\nprintf called > {os.fspath(marker)!r}\n",
        encoding="utf-8",
    )
    fake_mkdir.chmod(0o755)

    completed = subprocess.run(
        [str(ENTRYPOINT), "/bin/true"],
        cwd=PROJECT_ROOT,
        env={"PATH": os.fspath(fake_bin)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 41
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert not marker.exists()


def test_entrypoint_real_preflight_requires_fallback_with_exact_reason(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "application root"
    app_root.mkdir()
    config_root = app_root / "config with spaces"
    config_root.mkdir()
    (config_root / "providers.json").write_bytes(b"{}\n")
    secret = "container-config-secret-sentinel"
    config_paths = {
        "PROVIDERS_FILENAME": config_root / "providers.json",
        "FALLBACK_RULES_FILENAME": config_root / "models_fallback_rules.json",
        "OPERATION_RULES_FILENAME": config_root / "models_operation_rules.json",
        "FUSION_RULES_FILENAME": config_root / "models_fusion_rules.json",
        "MODEL_RULES_FILENAME": config_root / "models_model_rules.json",
        "ROUTER_RULES_FILENAME": config_root / "models_router_rules.json",
    }
    completed = subprocess.run(
        [str(ENTRYPOINT), "/bin/true"],
        cwd=PROJECT_ROOT,
        env={
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "PYTHONPATH": os.fspath(PROJECT_ROOT),
            "GATEWAY_WORKERS": "1",
            "APP_DIR": os.fspath(app_root),
            "LLMGATEWAY_CONFIG_DIR": os.fspath(config_root),
            "SECRET_SENTINEL": secret,
            **{name: os.fspath(path) for name, path in config_paths.items()},
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "container-config-preflight: reason=mandatory-config-missing\n"
    assert "LLM Gateway starting" not in completed.stderr
    assert secret not in completed.stderr


def test_config_preflight_bounds_import_time_settings_errors_without_env_values() -> None:
    secret = "super-secret-invalid-port"

    completed = subprocess.run(
        [sys.executable, "-m", "llm_gateway_core.config.container_preflight"],
        cwd=PROJECT_ROOT,
        env={
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "PYTHONPATH": os.fspath(PROJECT_ROOT),
            "GATEWAY_PORT": secret,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "container-config-preflight: reason=internal-error\n"
    assert secret not in completed.stderr


def test_entrypoint_preserves_pid_exact_argv_and_exit_without_environment_output(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = '-m' ] && "
        "[ \"$2\" = 'llm_gateway_core.services.container_exec' ]; then\n"
        f"    exec {os.fspath(Path(sys.executable))!r} \"$@\"\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_mkdir = fake_bin / "mkdir"
    fake_mkdir.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_mkdir.chmod(0o755)
    command = tmp_path / "command with spaces"
    command.write_text(
        "#!/bin/sh\n"
        "printf 'child-pid=%s\\n' \"$$\"\n"
        "for arg in \"$@\"; do printf 'arg=<%s>\\n' \"$arg\"; done\n"
        "exit 23\n",
        encoding="utf-8",
    )
    command.chmod(0o755)
    secret = "must-not-be-printed"

    pid, returncode, stdout, stderr = run_process_with_pid(
        [str(ENTRYPOINT), str(command), "first argument", "second", ""],
        cwd=PROJECT_ROOT,
        env={"PATH": os.fspath(fake_bin), "SECRET_SENTINEL": secret},
    )

    assert returncode == 23
    assert f"child-pid={pid}\n" in stdout
    assert "arg=<first argument>\narg=<second>\narg=<>\n" in stdout
    assert secret not in stdout
    assert secret not in stderr


def test_entrypoint_bounds_malformed_shebang_error_without_printing_command(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text(
        f"#!/bin/sh\nexec {os.fspath(Path(sys.executable))!r} \"$@\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_mkdir = fake_bin / "mkdir"
    fake_mkdir.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_mkdir.chmod(0o755)

    app_root = tmp_path / "app"
    config_root = app_root / "config"
    config_root.mkdir(parents=True)
    _providers = config_root / "providers.json"
    _fallback = config_root / "models_fallback_rules.json"
    _providers.write_bytes(b"{}\n")
    _fallback.write_bytes(b"[]\n")
    secret = "malformed-shebang-secret"
    command = tmp_path / f"{secret}-command"
    command.write_text("#!/missing-secret-interpreter\n", encoding="utf-8")
    command.chmod(0o755)

    completed = subprocess.run(
        [str(ENTRYPOINT), os.fspath(command), secret],
        cwd=PROJECT_ROOT,
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "PYTHONPATH": os.fspath(PROJECT_ROOT),
            "GATEWAY_WORKERS": "1",
            "APP_DIR": os.fspath(app_root),
            "LLMGATEWAY_CONFIG_DIR": os.fspath(config_root),
            "PROVIDERS_FILENAME": os.fspath(_providers),
            "FALLBACK_RULES_FILENAME": os.fspath(_fallback),
            "OPERATION_RULES_FILENAME": os.fspath(
                config_root / "models_operation_rules.json"
            ),
            "FUSION_RULES_FILENAME": os.fspath(config_root / "models_fusion_rules.json"),
            "MODEL_RULES_FILENAME": os.fspath(config_root / "models_model_rules.json"),
            "ROUTER_RULES_FILENAME": os.fspath(config_root / "models_router_rules.json"),
            "SECRET_SENTINEL": secret,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 126
    assert completed.stderr == "container-entrypoint: reason=command-not-executable\n"
    assert secret not in completed.stderr
