import os
import stat
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "docker" / "setup-gateway-service.sh"


def _write_fake_systemctl(tmp_path: Path) -> tuple[Path, Path]:
    log_path = tmp_path / "systemctl.log"
    fake_path = tmp_path / "systemctl"
    fake_path.write_text(
        "#!/usr/bin/env sh\n"
        "printf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
        "if [ \"$1\" = \"is-enabled\" ]; then\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_path.chmod(fake_path.stat().st_mode | stat.S_IEXEC)
    return fake_path, log_path


def _run_script(tmp_path: Path, service_name: str = "test-gateway.service") -> subprocess.CompletedProcess[str]:
    fake_systemctl, log_path = _write_fake_systemctl(tmp_path)
    env = os.environ.copy()
    env["SYSTEMCTL_BIN"] = str(fake_systemctl)
    env["SYSTEMCTL_LOG"] = str(log_path)
    env["SYSTEMD_UNIT_DIR"] = str(tmp_path / "systemd")
    env["SERVICE_NAME"] = service_name
    env["PROJECT_DIR"] = str(PROJECT_ROOT)
    return subprocess.run(
        ["sh", str(SCRIPT_PATH)],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def test_service_script_creates_unit_and_enables_it(tmp_path: Path):
    result = _run_script(tmp_path)

    service_file = tmp_path / "systemd" / "test-gateway.service"
    assert service_file.exists()
    service_text = service_file.read_text(encoding="utf-8")
    assert f"WorkingDirectory={PROJECT_ROOT}" in service_text
    assert f"ExecStart={PROJECT_ROOT / '.venv' / 'bin' / 'python'} {PROJECT_ROOT / 'main.py'}" in service_text
    assert "Restart=always" in service_text
    assert "Created systemd unit:" in result.stdout
    assert "Service restarted: test-gateway.service" in result.stdout

    log_lines = (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()
    assert log_lines == [
        "daemon-reload",
        "is-enabled test-gateway.service",
        "enable test-gateway.service",
        "restart test-gateway.service",
    ]


def test_service_script_keeps_existing_unit_and_only_restarts(tmp_path: Path):
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    existing_service = systemd_dir / "test-gateway.service"
    existing_service.write_text(
        "[Unit]\nDescription=Existing\n\n[Service]\nExecStart=/bin/true\n",
        encoding="utf-8",
    )

    result = _run_script(tmp_path)

    assert existing_service.read_text(encoding="utf-8") == (
        "[Unit]\nDescription=Existing\n\n[Service]\nExecStart=/bin/true\n"
    )
    assert "Systemd unit already exists:" in result.stdout

    log_lines = (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()
    assert log_lines == [
        "daemon-reload",
        "restart test-gateway.service",
    ]
