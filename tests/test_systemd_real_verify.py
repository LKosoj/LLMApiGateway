from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from tests.test_gateway_service_script import (
    SERVICE_NAME,
    _prepare_test_mode,
    _run_test_mode,
    _write_executable,
)


SYSTEMD_ANALYZE = Path("/usr/bin/systemd-analyze")


def test_installer_unit_passes_real_systemd_analyze_in_isolated_unit_path(
    tmp_path: Path,
) -> None:
    if not SYSTEMD_ANALYZE.is_file() or not os.access(SYSTEMD_ANALYZE, os.X_OK):
        pytest.fail(f"required executable is unavailable: {SYSTEMD_ANALYZE}")

    fixture = _prepare_test_mode(tmp_path)
    unit_path = tmp_path / "systemd-unit-path"
    unit_path.mkdir()
    for target in ("network-online.target", "multi-user.target", "sysinit.target"):
        (unit_path / target).write_text(
            "[Unit]\nDescription=Hermetic verification target\n",
            encoding="utf-8",
        )

    analyzer_stdout = tmp_path / "real-systemd-analyze.stdout"
    analyzer_stderr = tmp_path / "real-systemd-analyze.stderr"
    analyzer_status = tmp_path / "real-systemd-analyze.status"
    analyzer_arguments = tmp_path / "real-systemd-analyze.arguments"
    analyzer_wrapper = _write_executable(
        tmp_path / "real-systemd-analyze",
        f"""#!/usr/bin/env sh
set -u
printf '%s\\n' "$@" > "$REAL_SYSTEMD_ANALYZE_ARGUMENTS"
SYSTEMD_UNIT_PATH="$REAL_SYSTEMD_UNIT_PATH" \
    {SYSTEMD_ANALYZE} --man=no --generators=no "$@" \
    > "$REAL_SYSTEMD_ANALYZE_STDOUT" 2> "$REAL_SYSTEMD_ANALYZE_STDERR"
status=$?
printf '%s\\n' "$status" > "$REAL_SYSTEMD_ANALYZE_STATUS"
exit "$status"
""",
    )
    env = cast(dict[str, str], fixture["env"])
    env.update(
        {
            "SYSTEMD_ANALYZE_BIN": os.fspath(analyzer_wrapper),
            "REAL_SYSTEMD_UNIT_PATH": os.fspath(unit_path),
            "REAL_SYSTEMD_ANALYZE_STDOUT": os.fspath(analyzer_stdout),
            "REAL_SYSTEMD_ANALYZE_STDERR": os.fspath(analyzer_stderr),
            "REAL_SYSTEMD_ANALYZE_STATUS": os.fspath(analyzer_status),
            "REAL_SYSTEMD_ANALYZE_ARGUMENTS": os.fspath(analyzer_arguments),
        }
    )

    result = _run_test_mode(fixture)

    assert result.stderr == ""
    assert analyzer_status.read_text(encoding="utf-8") == "0\n"
    assert analyzer_stdout.read_bytes() == b""
    assert analyzer_stderr.read_bytes() == b""
    analyzer_args = analyzer_arguments.read_text(encoding="utf-8").splitlines()
    assert len(analyzer_args) == 2
    action, raw_unit = analyzer_args
    assert action == "verify"
    analyzed_unit = Path(raw_unit)
    assert analyzed_unit.name == SERVICE_NAME
    assert analyzed_unit.is_relative_to(tmp_path)

    systemctl = Path(env["SYSTEMCTL_BIN"])
    assert systemctl.is_relative_to(tmp_path)
    assert Path(fixture["systemctl_log"]).read_text(encoding="utf-8")
    unit_text = (Path(fixture["unit_dir"]) / SERVICE_NAME).read_text(encoding="utf-8")
    for live_path in (
        "/etc/llm-gateway",
        "/var/lib/llm-gateway",
        "/var/log/llm-gateway",
        "/var/cache/llm-gateway",
    ):
        assert f"={live_path}" not in unit_text
