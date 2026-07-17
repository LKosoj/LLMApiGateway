from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "docker" / "restart-gateway-service.sh"
SERVICE_NAME = "llm-gateway.service"


def _fake_sudo(bin_dir: Path, log: Path, fail_on: str = "") -> None:
    """Record what the script asks sudo to run, instead of running it.

    Recording at the sudo boundary keeps the real host untouched while still
    pinning both commands and the order the script issues them in.
    """
    path = bin_dir / "sudo"
    path.write_text(
        "#!/usr/bin/env sh\n"
        f'printf "%s\\n" "sudo $*" >> "{log}"\n'
        f'if [ "$1" = "{fail_on}" ]; then exit 1; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run(tmp_path: Path, fail_on: str = "") -> tuple[subprocess.CompletedProcess, Path]:
    log = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_sudo(bin_dir, log, fail_on)
    result = subprocess.run(
        [str(SCRIPT_PATH)],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, log


def _calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_service_is_restarted_before_its_logs_are_followed(tmp_path: Path) -> None:
    result, log = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _calls(log) == [
        f"sudo systemctl restart {SERVICE_NAME}",
        f"sudo journalctl -u {SERVICE_NAME} -f",
    ]


def test_failed_restart_reports_the_failure_instead_of_following_logs(
    tmp_path: Path,
) -> None:
    """A failed restart must not be papered over by tailing a stale service."""
    result, log = _run(tmp_path, fail_on="systemctl")

    assert result.returncode != 0
    assert _calls(log) == [f"sudo systemctl restart {SERVICE_NAME}"]
    assert f"could not restart {SERVICE_NAME}" in result.stderr
