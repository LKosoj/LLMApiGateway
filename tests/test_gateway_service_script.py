from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "docker" / "setup-gateway-service.sh"
SERVICE_NAME = "llm-gateway.service"


def _write_executable(path: Path, payload: str) -> Path:
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o755)
    return path


def _test_ids() -> tuple[int, int, int, int]:
    service_uid = os.geteuid() or 10001
    service_gid = os.getegid() or 10001
    return service_uid, service_gid, os.geteuid(), os.getegid()


def _prepare_test_mode(tmp_path: Path) -> dict[str, object]:
    project = tmp_path / "project"
    docker_dir = project / "docker"
    venv_bin = project / ".venv" / "bin"
    core_dir = project / "llm_gateway_core"
    site_packages = project / ".venv" / "lib" / "python3.12" / "site-packages"
    docker_dir.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    core_dir.mkdir()
    site_packages.mkdir(parents=True)
    for directory in (
        project, docker_dir, core_dir, project / ".venv", venv_bin,
        *site_packages.parents[:2], site_packages,
    ):
        directory.chmod(0o755)
    for path in (core_dir / "nested.py", site_packages / "nested.py"):
        path.write_text("# trusted nested code\n", encoding="utf-8")
        path.chmod(0o644)
    pyvenv_config = project / ".venv" / "pyvenv.cfg"
    pyvenv_config.write_text("home = /usr/bin\n", encoding="utf-8")
    pyvenv_config.chmod(0o644)
    main_file = project / "main.py"
    main_file.write_text("# test gateway\n", encoding="utf-8")
    main_file.chmod(0o644)
    readiness_file = docker_dir / "systemd_readiness.py"
    readiness_file.write_text("# test readiness\n", encoding="utf-8")
    readiness_file.chmod(0o644)
    python_bin = _write_executable(
        venv_bin / "python",
        (
            "#!/usr/bin/env sh\n"
            "for arg do\n"
            "case \"$arg\" in *os.fsync*) count=0; "
            "[ ! -f \"$FSYNC_COUNT_FILE\" ] || read -r count < \"$FSYNC_COUNT_FILE\"; "
            "count=$((count + 1)); printf '%s\\n' \"$count\" > \"$FSYNC_COUNT_FILE\"; "
            "printf 'fsync\\n' >> \"$PYTHON_CALL_LOG\"; "
            "[ \"${FAIL_FSYNC_AT:-0}\" != \"$count\" ] || exit 1 ;; esac\n"
            "done\n"
            f"exec {sys.executable} \"$@\"\n"
        ),
    )
    migration_log = tmp_path / "migration.log"
    _write_executable(
        docker_dir / "systemd_migration.py",
        """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["MIGRATION_LOG"], "a", encoding="utf-8") as stream: stream.write(json.dumps(sys.argv[1:]) + "\\n")
with open(os.environ["EVENT_LOG"], "a", encoding="utf-8") as stream: stream.write("migration " + sys.argv[1] + "\\n")
with open(os.environ["MIGRATION_CWD_LOG"], "a", encoding="utf-8") as stream: stream.write(os.getcwd() + "\\n")
if os.environ.get("FAIL_MIGRATION") == sys.argv[1]: raise SystemExit(1)
if sys.argv[1] == "inventory":
    output = os.environ.get("INVENTORY_OUTPUT")
    if output is None: output = '{"migration_required":' + os.environ.get("MIGRATION_REQUIRED", "true") + '}'
    if output == "__RAW_NUL__": sys.stdout.buffer.write(b'{"migration_required":false,"detail":"\\x00"}'); raise SystemExit
    sys.stdout.write(output)
else:
    target_env = sys.argv[sys.argv.index("--target-env-dir") + 1]
    os.makedirs(target_env, exist_ok=True)
    gateway_env = os.path.join(target_env, "gateway.env")
    open(gateway_env, "a", encoding="utf-8").close()
    os.chown(gateway_env, int(os.environ["ENV_UID"]), int(os.environ["SERVICE_GID"]))
    os.chmod(gateway_env, 0o640)
    sys.stdout.write(os.environ.get("MIGRATE_OUTPUT", '{"migration_required":false}'))
""",
    )
    systemctl_log = tmp_path / "systemctl.log"
    systemctl = _write_executable(
        tmp_path / "systemctl",
        """#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["SYSTEMCTL_LOG"], "a", encoding="utf-8") as stream: stream.write(" ".join(args) + "\\n")
with open(os.environ["EVENT_LOG"], "a", encoding="utf-8") as stream: stream.write("systemctl " + " ".join(args) + "\\n")
state = Path(os.environ["SYSTEMCTL_STATE"])
state.mkdir(exist_ok=True)
active, enabled, command = state / "active", state / "enabled", args[0]
if command == "is-active": raise SystemExit(0 if active.exists() else 3)
if command == "is-enabled": raise SystemExit(0 if enabled.exists() else 1)
if os.environ.get("FAIL_SYSTEMCTL") == command: raise SystemExit(1)
if command == "stop": active.unlink(missing_ok=True)
elif command in {"start", "restart"}: active.touch()
elif command == "enable": enabled.touch()
elif command == "disable": enabled.unlink(missing_ok=True)
""",
    )
    analyze_log = tmp_path / "systemd-analyze.log"
    systemd_analyze = _write_executable(
        tmp_path / "systemd-analyze",
        """#!/usr/bin/env python3
import os, sys
with open(os.environ["SYSTEMD_ANALYZE_LOG"], "a", encoding="utf-8") as stream: stream.write(" ".join(sys.argv[1:]) + "\\n")
with open(os.environ["EVENT_LOG"], "a", encoding="utf-8") as stream: stream.write("verify\\n")
raise SystemExit(1 if os.environ.get("FAIL_SYSTEMD_ANALYZE") else 0)
""",
    )
    service_uid, service_gid, env_uid, env_gid = _test_ids()
    getent = _write_executable(
        tmp_path / "getent",
        """#!/usr/bin/env python3
import os, sys
database, key = sys.argv[1:]
uid, gid = os.environ["SERVICE_UID"], os.environ["SERVICE_GID"]
mode = os.environ.get("IDENTITY_MODE", "ready")
group_exists = mode != "missing" or os.path.exists(os.environ["GROUP_STATE"])
user_exists = mode != "missing" or os.path.exists(os.environ["USER_STATE"])
if mode == "getent-error": raise SystemExit(1)
if mode == "gid-collision" and database == "group" and key == gid: print(f"occupied-group:x:{gid}:")
elif mode == "uid-collision" and database == "passwd" and key == uid: print(f"occupied-user:x:{uid}:{gid}:Occupied:/nonexistent:/usr/sbin/nologin")
elif database == "group" and key in {"llmgateway", gid} and group_exists: print(f"llmgateway:x:{gid}:")
elif database == "passwd" and key in {"llmgateway", uid} and user_exists:
    home = os.environ.get("USER_HOME_OVERRIDE", os.environ["STATE_DIR"])
    shell = os.environ.get("USER_SHELL_OVERRIDE", "/usr/sbin/nologin")
    print(f"llmgateway:x:{uid}:{gid}:LLM Gateway:{home}:{shell}")
elif database == "passwd" and key == "legacy-user": print(f"legacy-user:x:{os.environ['ENV_UID']}:{os.environ['ENV_GID']}:Legacy:{os.environ['LEGACY_HOME']}:/bin/sh")
else: raise SystemExit(2)
""",
    )
    account_log = tmp_path / "accounts.log"
    groupadd = _write_executable(
        tmp_path / "groupadd",
        "#!/usr/bin/env sh\nprintf 'groupadd %s\\n' \"$*\" >> \"$ACCOUNT_LOG\"\nprintf 'groupadd\\n' >> \"$EVENT_LOG\"\n: > \"$GROUP_STATE\"\n",
    )
    useradd = _write_executable(
        tmp_path / "useradd",
        "#!/usr/bin/env sh\nprintf 'useradd %s\\n' \"$*\" >> \"$ACCOUNT_LOG\"\nprintf 'useradd\\n' >> \"$EVENT_LOG\"\n[ \"${FAIL_USERADD:-0}\" = 1 ] && exit 1\n: > \"$USER_STATE\"\n",
    )
    env_dir = tmp_path / "etc" / "llm-gateway"
    state_dir = tmp_path / "var" / "lib" / "llm-gateway"
    log_dir = tmp_path / "var" / "log" / "llm-gateway"
    cache_dir = tmp_path / "var" / "cache" / "llm-gateway"
    unit_dir = tmp_path / "systemd"
    for parent in (
        env_dir.parent,
        state_dir.parent,
        log_dir.parent,
        cache_dir.parent,
        unit_dir.parent,
    ):
        parent.mkdir(parents=True, exist_ok=True)
        parent.chmod(0o755)
    unit_dir.mkdir()
    unit_dir.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PROJECT_DIR": os.fspath(project),
            "PYTHON_BIN": os.fspath(python_bin),
            "SYSTEMD_UNIT_DIR": os.fspath(unit_dir),
            "SYSTEMCTL_BIN": os.fspath(systemctl),
            "SYSTEMD_ANALYZE_BIN": os.fspath(systemd_analyze),
            "ENV_DIR": os.fspath(env_dir),
            "STATE_DIR": os.fspath(state_dir),
            "LOG_DIR": os.fspath(log_dir),
            "CACHE_DIR": os.fspath(cache_dir),
            "SERVICE_UID": str(service_uid),
            "SERVICE_GID": str(service_gid),
            "ENV_UID": str(env_uid),
            "ENV_GID": str(env_gid),
            "GETENT_BIN": os.fspath(getent),
            "GROUPADD_BIN": os.fspath(groupadd),
            "USERADD_BIN": os.fspath(useradd),
            "MIGRATION_LOG": os.fspath(migration_log),
            "SYSTEMCTL_LOG": os.fspath(systemctl_log),
            "SYSTEMCTL_STATE": os.fspath(tmp_path / "systemctl-state"),
            "SYSTEMD_ANALYZE_LOG": os.fspath(analyze_log),
            "ACCOUNT_LOG": os.fspath(account_log),
            "EVENT_LOG": os.fspath(tmp_path / "events.log"),
            "MIGRATION_CWD_LOG": os.fspath(tmp_path / "migration-cwd.log"),
            "GROUP_STATE": os.fspath(tmp_path / "group-created"),
            "USER_STATE": os.fspath(tmp_path / "user-created"),
            "LEGACY_HOME": os.fspath(tmp_path / "legacy-home"),
            "PYTHON_CALL_LOG": os.fspath(tmp_path / "python-calls.log"),
            "FSYNC_COUNT_FILE": os.fspath(tmp_path / "fsync-count"),
            "TMPDIR": os.fspath(tmp_path / "tmp"),
        }
    )
    (tmp_path / "tmp").mkdir()
    return {
        "env": env,
        "project": project,
        "env_dir": env_dir,
        "state_dir": state_dir,
        "log_dir": log_dir,
        "cache_dir": cache_dir,
        "unit_dir": unit_dir,
        "migration_log": migration_log,
        "systemctl_log": systemctl_log,
        "analyze_log": analyze_log,
        "account_log": account_log,
        "event_log": tmp_path / "events.log",
        "migration_cwd_log": tmp_path / "migration-cwd.log",
        "python_bin": python_bin,
        "python_call_log": tmp_path / "python-calls.log",
        "service_uid": service_uid,
        "service_gid": service_gid,
        "env_uid": env_uid,
        "env_gid": env_gid,
    }


def _run_test_mode(
    fixture: dict[str, object], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", os.fspath(SCRIPT_PATH), "--test-mode"],
        cwd=PROJECT_ROOT,
        env=fixture["env"],
        text=True,
        capture_output=True,
        check=check,
    )


def _clear_logs(fixture: dict[str, Any]) -> None:
    for key in ("migration_log", "systemctl_log", "analyze_log", "event_log"):
        Path(fixture[key]).unlink(missing_ok=True)


def _overwrite_deployment(
    fixture: dict[str, Any], *, unit: bytes, runtime: bytes
) -> tuple[Path, Path]:
    unit_path = Path(fixture["unit_dir"]) / SERVICE_NAME
    runtime_path = Path(fixture["env_dir"]) / "runtime.env"
    unit_path.write_bytes(unit)
    unit_path.chmod(0o666)
    runtime_path.write_bytes(runtime)
    runtime_path.chmod(0o600)
    return unit_path, runtime_path


@pytest.mark.parametrize(
    "override",
    "PROJECT_DIR PYTHON_BIN SYSTEMD_UNIT_DIR SYSTEMCTL_BIN "
    "SYSTEMD_ANALYZE_BIN ENV_DIR STATE_DIR LOG_DIR CACHE_DIR SERVICE_UID "
    "SERVICE_GID ENV_UID GETENT_BIN GROUPADD_BIN USERADD_BIN SERVICE_NAME "
    "SERVICE_USER SERVICE_GROUP TMPDIR ENV_GID".split(),
)
def test_production_rejects_every_test_override_before_side_effects(
    tmp_path: Path, override: str
) -> None:
    env = os.environ.copy()
    env[override] = (
        "2000" if override.endswith(("UID", "GID")) else "/private/override"
    )

    result = subprocess.run(
        ["sh", os.fspath(SCRIPT_PATH)],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stderr == f"setup-gateway-service: forbidden production override: {override}\n"


def test_test_mode_installs_exact_runtime_unit_and_frozen_migration_cli(
    tmp_path: Path,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    result = _run_test_mode(fixture)
    project = fixture["project"]
    env_dir = fixture["env_dir"]
    state_dir = fixture["state_dir"]
    log_dir = fixture["log_dir"]
    cache_dir = fixture["cache_dir"]
    runtime = env_dir / "runtime.env"
    assert runtime.read_text(encoding="utf-8") == (
        f"APP_DIR={project}\n"
        f"LLMGATEWAY_ENV_FILE={env_dir / 'gateway.env'}\n"
        "PYTHONUNBUFFERED=1\n"
        "PYTHONDONTWRITEBYTECODE=1\n"
        "GATEWAY_WORKERS=1\n"
        f"GATEWAY_DB_DIR={state_dir}\n"
        f"GATEWAY_OUTPUTS_DIR={state_dir / 'outputs'}\n"
        f"LLMGATEWAY_LOG_DIR={log_dir}\n"
        f"CLOAKBROWSER_CACHE_DIR={cache_dir}\n"
        f"LLMGATEWAY_CONFIG_DIR={state_dir / 'config'}\n"
        f"PROVIDERS_FILENAME={state_dir / 'config' / 'providers.json'}\n"
        f"FALLBACK_RULES_FILENAME={state_dir / 'config' / 'models_fallback_rules.json'}\n"
        f"OPERATION_RULES_FILENAME={state_dir / 'config' / 'models_operation_rules.json'}\n"
        f"FUSION_RULES_FILENAME={state_dir / 'config' / 'models_fusion_rules.json'}\n"
        f"MODEL_RULES_FILENAME={state_dir / 'config' / 'models_model_rules.json'}\n"
        f"ROUTER_RULES_FILENAME={state_dir / 'config' / 'models_router_rules.json'}\n"
    )
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o640
    assert (runtime.stat().st_uid, runtime.stat().st_gid) == (
        fixture["env_uid"],
        fixture["service_gid"],
    )
    unit = fixture["unit_dir"] / SERVICE_NAME
    unit_text = unit.read_text(encoding="utf-8")
    assert f"User=llmgateway\nGroup=llmgateway\nWorkingDirectory={project}\n" in unit_text
    assert f"ExecStart={fixture['python_bin']} {project / 'main.py'}\n" in unit_text
    assert (
        f"ExecStartPost={fixture['python_bin']} {project / 'docker' / 'systemd_readiness.py'}\n"
        in unit_text
    )
    assert unit_text.index(f"EnvironmentFile={env_dir / 'gateway.env'}") < unit_text.index(
        f"EnvironmentFile={runtime}"
    )
    for directive in (
        "TimeoutStartSec=75",
        "StateDirectory=llm-gateway",
        "StateDirectoryMode=0750",
        "LogsDirectory=llm-gateway",
        "LogsDirectoryMode=0750",
        "CacheDirectory=llm-gateway",
        "CacheDirectoryMode=0750",
        "UMask=0007",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        f"ReadWritePaths={state_dir} {log_dir} {cache_dir}",
    ):
        assert f"{directive}\n" in unit_text
    assert stat.S_IMODE(unit.stat().st_mode) == 0o644
    assert (unit.stat().st_uid, unit.stat().st_gid) == (
        fixture["env_uid"],
        fixture["env_gid"],
    )
    assert (env_dir.stat().st_uid, env_dir.stat().st_gid) == (
        fixture["env_uid"],
        fixture["service_gid"],
    )
    for directory in (env_dir, state_dir, log_dir, cache_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o750
    base_cli = [
        "--source-root",
        os.fspath(project),
        "--target-env-dir",
        os.fspath(env_dir),
        "--target-state-dir",
        os.fspath(state_dir),
        "--target-cache-dir",
        os.fspath(cache_dir),
    ]
    migration_calls = [
        json.loads(line)
        for line in fixture["migration_log"].read_text(encoding="utf-8").splitlines()
    ]
    assert migration_calls == [["inventory", *base_cli], ["migrate", *base_cli]]
    assert fixture["systemctl_log"].read_text(encoding="utf-8").splitlines() == [
        f"is-active --quiet {SERVICE_NAME}",
        f"is-enabled --quiet {SERVICE_NAME}",
        "daemon-reload",
        f"enable {SERVICE_NAME}",
        f"start {SERVICE_NAME}",
        f"is-enabled --quiet {SERVICE_NAME}",
        f"is-active --quiet {SERVICE_NAME}",
    ]
    analyze_call = fixture["analyze_log"].read_text(encoding="utf-8").strip()
    assert analyze_call.startswith("verify ")
    assert "Installed and started llm-gateway.service" in result.stdout


def test_collision_fails_before_inventory_or_any_mutation(tmp_path: Path) -> None:
    fixture = _prepare_test_mode(tmp_path)
    fixture["env"]["IDENTITY_MODE"] = "uid-collision"
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert "service user name/UID collision" in result.stderr
    for path in (
        fixture["migration_log"],
        fixture["analyze_log"],
        fixture["account_log"],
        fixture["systemctl_log"],
    ):
        assert not path.exists()
    for path in (
        fixture["env_dir"],
        fixture["state_dir"],
        fixture["log_dir"],
        fixture["cache_dir"],
    ):
        assert not path.exists()


def test_getent_operational_failure_is_not_treated_as_not_found(tmp_path: Path) -> None:
    fixture = _prepare_test_mode(tmp_path)
    fixture["env"]["IDENTITY_MODE"] = "getent-error"
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert "account database lookup failed" in result.stderr
    assert not Path(fixture["migration_log"]).exists()
    assert not Path(fixture["account_log"]).exists()


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ("USER_HOME_OVERRIDE", "service user home mismatch"),
        ("USER_SHELL_OVERRIDE", "service user shell mismatch"),
    ],
)
def test_existing_service_user_requires_exact_home_and_nologin_shell(
    tmp_path: Path, override: str, expected: str
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    fixture["env"][override] = "/unsafe"
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not Path(fixture["migration_log"]).exists()


@pytest.mark.parametrize("risk", ["symlink", "group-writable"])
@pytest.mark.parametrize(
    "relative",
    "main.py docker/systemd_migration.py docker/systemd_readiness.py "
    ".venv/bin/python .venv/pyvenv.cfg llm_gateway_core/nested.py "
    ".venv/lib/python3.12/site-packages/nested.py".split(),
)
def test_untrusted_project_file_fails_before_inventory(
    tmp_path: Path, risk: str, relative: str
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    main_file = Path(fixture["project"]) / relative
    if risk == "symlink":
        main_file.unlink()
        target = tmp_path / "outside-main.py"
        target.write_text("# outside\n", encoding="utf-8")
        target.chmod(0o755)
        main_file.symlink_to(target)
    else:
        fixture["env"]["SERVICE_GID"] = str(int(fixture["env"]["ENV_GID"]) + 1)
        main_file.chmod(0o770)
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert "untrusted project runtime path" in result.stderr
    assert not Path(fixture["migration_log"]).exists()
    assert not Path(fixture["account_log"]).exists()


def test_production_sets_fixed_path_before_external_commands() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    path_assignment = "PATH=/usr/sbin:/usr/bin:/sbin:/bin"
    assert path_assignment in source
    assert source.index(path_assignment) < source.index("SCRIPT_DIR=")
    assert "unset PYTHONPATH PYTHONHOME PYTHONSTARTUP" in source
    assert '"$PYTHON_BIN" -I "$MIGRATION_SCRIPT"' in source
    assert '"$PYTHON_BIN" -I -S -c' in source
    assert source.index("\nvalidate_recursive_migration_code\n") < source.index(
        "\nvalidate_trusted_python\n"
    )


def test_missing_account_flags_force_convergent_account_creation(tmp_path: Path) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    fixture["env"].update(
        {"IDENTITY_MODE": "missing", "MIGRATION_REQUIRED": "false"}
    )
    Path(fixture["env"]["GROUP_STATE"]).touch()
    Path(fixture["env"]["USER_STATE"]).unlink(missing_ok=True)
    Path(fixture["account_log"]).unlink(missing_ok=True)
    _clear_logs(fixture)
    _run_test_mode(fixture)
    lines = Path(fixture["account_log"]).read_text(encoding="utf-8").splitlines()
    assert lines == [
        f"useradd --system --uid {fixture['service_uid']} --gid llmgateway "
        f"--home-dir {fixture['state_dir']} --no-create-home "
        "--shell /usr/sbin/nologin llmgateway"
    ]


def test_partial_group_creation_is_safe_to_resume_after_useradd_failure(
    tmp_path: Path,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    fixture["env"].update({"IDENTITY_MODE": "missing", "FAIL_USERADD": "1"})
    first = _run_test_mode(fixture, check=False)
    assert first.returncode != 0
    assert "service account creation incomplete" in first.stderr
    assert Path(fixture["env"]["GROUP_STATE"]).exists()
    assert not Path(fixture["env"]["USER_STATE"]).exists()
    assert Path(fixture["systemctl_log"]).read_text(encoding="utf-8").splitlines() == [
        f"is-active --quiet {SERVICE_NAME}",
        f"is-enabled --quiet {SERVICE_NAME}",
    ]
    assert not Path(fixture["env_dir"]).exists()
    fixture["env"].pop("FAIL_USERADD")
    Path(fixture["account_log"]).unlink()
    _run_test_mode(fixture)
    lines = Path(fixture["account_log"]).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("useradd ")


@pytest.mark.parametrize(
    ("failure_env", "expected_error", "analyze_called"),
    [
        ("FAIL_MIGRATION", "migration inventory failed", False),
        ("FAIL_SYSTEMD_ANALYZE", "systemd unit verification failed", True),
    ],
)
def test_inventory_or_unit_verify_failure_has_zero_install_mutation(
    tmp_path: Path,
    failure_env: str,
    expected_error: str,
    analyze_called: bool,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    fixture["env"][failure_env] = (
        "inventory" if failure_env == "FAIL_MIGRATION" else "1"
    )
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not fixture["account_log"].exists()
    assert not fixture["systemctl_log"].exists()
    assert fixture["analyze_log"].exists() is analyze_called
    for path in (
        fixture["env_dir"],
        fixture["state_dir"],
        fixture["log_dir"],
        fixture["cache_dir"],
    ):
        assert not path.exists()


def test_missing_fixed_account_is_created_after_read_only_preflight(
    tmp_path: Path,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    fixture["env"]["IDENTITY_MODE"] = "missing"
    _run_test_mode(fixture)
    assert fixture["migration_log"].exists()
    assert fixture["analyze_log"].exists()
    assert fixture["account_log"].read_text(encoding="utf-8").splitlines() == [
        f"groupadd --system --gid {fixture['service_gid']} llmgateway",
        (
            f"useradd --system --uid {fixture['service_uid']} --gid llmgateway "
            f"--home-dir {fixture['state_dir']} --no-create-home "
            "--shell /usr/sbin/nologin llmgateway"
        ),
    ]


def test_converged_active_enabled_rerun_does_not_mutate_deployment(
    tmp_path: Path,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    unit = fixture["unit_dir"] / SERVICE_NAME
    runtime = fixture["env_dir"] / "runtime.env"
    gateway_env = fixture["env_dir"] / "gateway.env"
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (unit, runtime, gateway_env)
    }
    fixture["env"]["MIGRATION_REQUIRED"] = "false"
    for log in (
        fixture["migration_log"],
        fixture["systemctl_log"],
        fixture["analyze_log"],
    ):
        log.unlink()
    _run_test_mode(fixture)
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in before
    } == before
    calls = [
        json.loads(line)
        for line in fixture["migration_log"].read_text(encoding="utf-8").splitlines()
    ]
    assert len(calls) == 1
    assert calls[0][0] == "inventory"
    assert fixture["systemctl_log"].read_text(encoding="utf-8").splitlines() == [
        f"is-active --quiet {SERVICE_NAME}",
        f"is-enabled --quiet {SERVICE_NAME}",
        f"is-enabled --quiet {SERVICE_NAME}",
        f"is-active --quiet {SERVICE_NAME}",
    ]
    assert not list(fixture["unit_dir"].glob("*.backup.*"))
    assert not list(fixture["env_dir"].glob("*.backup.*"))


def test_legacy_cache_source_comes_only_from_one_normalized_unit_user(
    tmp_path: Path,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    legacy_unit = fixture["unit_dir"] / SERVICE_NAME
    legacy_unit.write_text(
        "[Service]\nUser=legacy-user\nExecStart=/bin/true\n",
        encoding="utf-8",
    )
    source_cache = Path(fixture["env"]["LEGACY_HOME"]) / ".cloakbrowser"
    source_cache.mkdir(parents=True)
    source_cache.chmod(0o750)
    _run_test_mode(fixture)
    first_call = json.loads(
        fixture["migration_log"].read_text(encoding="utf-8").splitlines()[0]
    )
    assert first_call[-2:] == [
        "--source-cache-dir",
        os.fspath(source_cache),
    ]


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_symlink_or_special_existing_unit_is_rejected_before_inventory(
    tmp_path: Path, kind: str
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    unit_dir = Path(fixture["unit_dir"])
    unit = unit_dir / SERVICE_NAME
    if kind == "symlink":
        unit.symlink_to(tmp_path / "attacker-unit")
    else:
        os.mkfifo(unit)
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert "unsafe existing systemd unit" in result.stderr
    assert not Path(fixture["migration_log"]).exists()
    assert not Path(fixture["systemctl_log"]).exists()
    assert not Path(fixture["account_log"]).exists()


def test_untrusted_unit_parent_is_rejected_before_inventory(tmp_path: Path) -> None:
    fixture = _prepare_test_mode(tmp_path)
    unit_dir = Path(fixture["unit_dir"])
    unit_dir.chmod(0o777)
    (unit_dir / SERVICE_NAME).write_text("[Service]\n", encoding="utf-8")
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert "untrusted systemd unit directory" in result.stderr
    assert not Path(fixture["migration_log"]).exists()
    assert not Path(fixture["systemctl_log"]).exists()


def test_unsafe_regular_unit_and_runtime_are_backed_up_then_replaced(
    tmp_path: Path,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    old_unit = b"[Service]\nUser=legacy-user\nExecStart=/bin/true\n"
    old_runtime = b"APP_DIR=/legacy\n"
    unit, runtime = _overwrite_deployment(
        fixture, unit=old_unit, runtime=old_runtime
    )
    fixture["env"]["MIGRATION_REQUIRED"] = "false"
    _clear_logs(fixture)
    _run_test_mode(fixture)
    assert unit.read_bytes() != old_unit
    assert runtime.read_bytes() != old_runtime
    unit_backups = list(Path(fixture["unit_dir"]).glob(f"{SERVICE_NAME}.backup.*"))
    runtime_backups = list(Path(fixture["env_dir"]).glob("runtime.env.backup.*"))
    assert [path.read_bytes() for path in unit_backups] == [old_unit]
    assert [path.read_bytes() for path in runtime_backups] == [old_runtime]
    for backup in (*unit_backups, *runtime_backups):
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert (backup.stat().st_uid, backup.stat().st_gid) == (
            fixture["env_uid"],
            fixture["env_gid"],
        )


def test_active_runtime_change_without_migration_restarts_without_stop(
    tmp_path: Path,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    unit_dir = Path(fixture["unit_dir"])
    unit_dir.chmod(0o700)
    unit_dir_metadata = unit_dir.stat().st_mode, unit_dir.stat().st_uid, unit_dir.stat().st_gid
    runtime = Path(fixture["env_dir"]) / "runtime.env"
    runtime.write_text("APP_DIR=/legacy\n", encoding="utf-8")
    fixture["env"]["MIGRATION_REQUIRED"] = "false"
    _clear_logs(fixture)
    _run_test_mode(fixture)
    systemctl = Path(fixture["systemctl_log"]).read_text(encoding="utf-8")
    assert f"stop {SERVICE_NAME}" not in systemctl
    assert f"restart {SERVICE_NAME}" in systemctl
    assert (unit_dir.stat().st_mode, unit_dir.stat().st_uid, unit_dir.stat().st_gid) == unit_dir_metadata
    calls = [
        json.loads(line)
        for line in Path(fixture["migration_log"]).read_text(encoding="utf-8").splitlines()
    ]
    assert [call[0] for call in calls] == ["inventory"]


def test_active_migration_orders_stop_migrate_publish_reload_then_start(
    tmp_path: Path,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    _overwrite_deployment(
        fixture,
        unit=b"[Service]\nUser=legacy-user\nExecStart=/bin/true\n",
        runtime=b"APP_DIR=/legacy\n",
    )
    fixture["env"]["MIGRATION_REQUIRED"] = "true"
    _clear_logs(fixture)
    _run_test_mode(fixture)
    events = Path(fixture["event_log"]).read_text(encoding="utf-8").splitlines()
    markers = [
        "migration inventory",
        "verify",
        f"systemctl stop {SERVICE_NAME}",
        "migration migrate",
        "systemctl daemon-reload",
        f"systemctl start {SERVICE_NAME}",
    ]
    assert [events.index(marker) for marker in markers] == sorted(
        events.index(marker) for marker in markers
    )


def test_migration_failure_preserves_files_and_leaves_service_stopped(
    tmp_path: Path,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    old_unit = b"[Service]\nUser=legacy-user\nExecStart=/bin/true\n"
    old_runtime = b"APP_DIR=/legacy\n"
    unit, runtime = _overwrite_deployment(
        fixture, unit=old_unit, runtime=old_runtime
    )
    fixture["env"]["FAIL_MIGRATION"] = "migrate"
    _clear_logs(fixture)
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert unit.read_bytes() == old_unit
    assert runtime.read_bytes() == old_runtime
    state = Path(fixture["env"]["SYSTEMCTL_STATE"])
    assert not (state / "active").exists()
    assert (state / "enabled").exists()


def test_start_failure_rolls_back_files_disables_new_enable_and_stays_stopped(
    tmp_path: Path,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    old_unit = b"[Service]\nUser=legacy-user\nExecStart=/bin/true\n"
    old_runtime = b"APP_DIR=/legacy\n"
    unit, runtime = _overwrite_deployment(
        fixture, unit=old_unit, runtime=old_runtime
    )
    state = Path(fixture["env"]["SYSTEMCTL_STATE"])
    (state / "active").unlink()
    (state / "enabled").unlink()
    fixture["env"].update(
        {"MIGRATION_REQUIRED": "false", "FAIL_SYSTEMCTL": "start"}
    )
    _clear_logs(fixture)
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert unit.read_bytes() == old_unit
    assert runtime.read_bytes() == old_runtime
    assert not (state / "active").exists()
    assert not (state / "enabled").exists()
    systemctl = Path(fixture["systemctl_log"]).read_text(encoding="utf-8")
    assert f"disable {SERVICE_NAME}" in systemctl


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        "[]",
        '{"migration_required":1}',
        '{"migration_required":true,"migration_required":false}',
        '{"migration_required":false,"detail":"\\u0000"}',
        "__RAW_NUL__",
        '{"migration_required":true}\n{"migration_required":false}',
        '{"migration_required":false} trailing',
    ],
)
def test_inventory_requires_one_json_object_with_strict_boolean(
    tmp_path: Path, payload: str
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    fixture["env"]["INVENTORY_OUTPUT"] = payload
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert "migration inventory returned an invalid report" in result.stderr
    assert not Path(fixture["analyze_log"]).exists()
    assert not Path(fixture["account_log"]).exists()
    assert not Path(fixture["systemctl_log"]).exists()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"migration_required":true}', "migration returned a non-converged report"),
        ('{"migration_required":false', "migration returned an invalid report"),
    ],
)
def test_migrate_report_must_be_strict_false_and_failure_stays_stopped(
    tmp_path: Path, payload: str, expected: str,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    old_unit = b"[Service]\nUser=legacy-user\nExecStart=/bin/true\n"
    old_runtime = b"APP_DIR=/legacy\n"
    unit, runtime = _overwrite_deployment(fixture, unit=old_unit, runtime=old_runtime)
    fixture["env"].update({"MIGRATION_REQUIRED": "true", "MIGRATE_OUTPUT": payload})
    _clear_logs(fixture)
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert expected in result.stderr
    assert unit.read_bytes() == old_unit
    assert runtime.read_bytes() == old_runtime
    state = Path(fixture["env"]["SYSTEMCTL_STATE"])
    assert not (state / "active").exists()


@pytest.mark.parametrize("risk", ["missing", "mode", "owner", "symlink"])
def test_inventory_false_requires_existing_safe_gateway_env(
    tmp_path: Path, risk: str,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    gateway_env = Path(fixture["env_dir"]) / "gateway.env"
    if risk in {"missing", "symlink"}:
        gateway_env.unlink()
    if risk == "mode":
        gateway_env.chmod(0o600)
    elif risk == "owner":
        if fixture["service_uid"] == fixture["env_uid"]:
            pytest.skip("requires distinct service and environment identities")
        os.chown(gateway_env, fixture["service_uid"], fixture["service_gid"])
    elif risk == "symlink":
        gateway_env.symlink_to(tmp_path / "attacker.env")
    fixture["env"]["MIGRATION_REQUIRED"] = "false"
    _clear_logs(fixture)
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert "unsafe gateway.env" in result.stderr
    assert not Path(fixture["systemctl_log"]).exists()


def test_migration_helper_runs_with_project_as_working_directory(tmp_path: Path) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    cwd_lines = Path(fixture["migration_cwd_log"]).read_text(encoding="utf-8").splitlines()
    assert cwd_lines == [os.fspath(fixture["project"])] * 2


def test_absent_legacy_cache_is_not_requested(tmp_path: Path) -> None:
    fixture = _prepare_test_mode(tmp_path)
    unit_dir = Path(fixture["unit_dir"])
    (unit_dir / SERVICE_NAME).write_text(
        "[Service]\nUser=legacy-user\nExecStart=/bin/true\n",
        encoding="utf-8",
    )
    _run_test_mode(fixture)
    inventory = json.loads(
        Path(fixture["migration_log"]).read_text(encoding="utf-8").splitlines()[0]
    )
    assert "--source-cache-dir" not in inventory


@pytest.mark.parametrize("kind", ["symlink", "regular-file", "world-writable"])
def test_unsafe_legacy_cache_is_rejected_before_inventory(
    tmp_path: Path, kind: str
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    unit_dir = Path(fixture["unit_dir"])
    (unit_dir / SERVICE_NAME).write_text(
        "[Service]\nUser=legacy-user\nExecStart=/bin/true\n",
        encoding="utf-8",
    )
    cache = Path(fixture["env"]["LEGACY_HOME"]) / ".cloakbrowser"
    cache.parent.mkdir()
    if kind == "symlink":
        cache.symlink_to(tmp_path)
    elif kind == "regular-file":
        cache.write_text("not a directory", encoding="utf-8")
    else:
        cache.mkdir()
        cache.chmod(0o777)
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert "unsafe legacy cache directory" in result.stderr
    assert not Path(fixture["migration_log"]).exists()


def test_canonical_bytes_with_wrong_metadata_are_republished(tmp_path: Path) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    unit = Path(fixture["unit_dir"]) / SERVICE_NAME
    runtime = Path(fixture["env_dir"]) / "runtime.env"
    unit.chmod(0o600)
    runtime.chmod(0o644)
    fixture["env"]["MIGRATION_REQUIRED"] = "false"
    _clear_logs(fixture)
    _run_test_mode(fixture)
    assert stat.S_IMODE(unit.stat().st_mode) == 0o644
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o640
    assert list(Path(fixture["unit_dir"]).glob(f"{SERVICE_NAME}.backup.*"))
    assert list(Path(fixture["env_dir"]).glob("runtime.env.backup.*"))


@pytest.mark.parametrize("directory_key", ["env_dir", "state_dir", "log_dir", "cache_dir"])
@pytest.mark.parametrize("mode", [0o600, 0o700, 0o755])
def test_managed_directory_metadata_drift_forces_exact_convergence(
    tmp_path: Path, directory_key: str, mode: int,
) -> None:
    if directory_key == "env_dir" and mode == 0o600 and os.geteuid() != 0:
        pytest.skip(
            "env_dir without the traversal bit blocks stat of gateway.env for "
            "non-root; the script fails closed before convergence, root "
            "(CAP_DAC_OVERRIDE) is required to exercise this drift"
        )
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    directory = Path(fixture[directory_key])
    directory.chmod(mode)
    fixture["env"]["MIGRATION_REQUIRED"] = "false"
    _clear_logs(fixture)
    _run_test_mode(fixture)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o750
    assert f"restart {SERVICE_NAME}" in Path(fixture["systemctl_log"]).read_text()


def test_parent_fsync_failure_after_replace_restores_target_and_leaves_no_temps(
    tmp_path: Path,
) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    old_unit = b"[Service]\nUser=legacy-user\nExecStart=/bin/true\n"
    old_runtime = b"APP_DIR=/legacy\n"
    unit, runtime = _overwrite_deployment(fixture, unit=old_unit, runtime=old_runtime)
    fixture["env"].update({"MIGRATION_REQUIRED": "false", "FAIL_FSYNC_AT": "4"})
    Path(fixture["env"]["FSYNC_COUNT_FILE"]).unlink()
    _clear_logs(fixture)
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert "durability sync failed" in result.stderr
    assert "rollback incomplete" not in result.stderr
    assert unit.read_bytes() == old_unit
    assert runtime.read_bytes() == old_runtime
    assert not (Path(fixture["env"]["SYSTEMCTL_STATE"]) / "active").exists()
    for directory in (Path(fixture["unit_dir"]), Path(fixture["env_dir"])):
        assert not list(directory.glob(".llm-gateway-*.??????"))


def test_rollback_failure_is_visible_and_service_remains_stopped(tmp_path: Path) -> None:
    fixture = _prepare_test_mode(tmp_path)
    _run_test_mode(fixture)
    old_unit = b"[Service]\nUser=legacy-user\nExecStart=/bin/true\n"
    old_runtime = b"APP_DIR=/legacy\n"
    unit, runtime = _overwrite_deployment(
        fixture, unit=old_unit, runtime=old_runtime
    )
    fixture["env"].update(
        {"MIGRATION_REQUIRED": "false", "FAIL_SYSTEMCTL": "daemon-reload"}
    )
    _clear_logs(fixture)
    result = _run_test_mode(fixture, check=False)
    assert result.returncode != 0
    assert "rollback incomplete" in result.stderr
    assert unit.read_bytes() == old_unit
    assert runtime.read_bytes() == old_runtime
    state = Path(fixture["env"]["SYSTEMCTL_STATE"])
    assert not (state / "active").exists()
