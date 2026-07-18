import socket
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path

import httpx
import pytest
import requests

SESSION_COOKIE_NAME = "llmgateway_session"


def create_authenticated_session(server: str, gateway_api_key: str = "test-key") -> str:
    """Log in against the real ``/auth/login`` endpoint and return the value of
    the ``llmgateway_session`` cookie the gateway issues.

    Session cookies are signed server-side with a random secret the gateway
    keeps in its state directory (see
    ``llm_gateway_core/services/session_secret.py``), so tests cannot mint a
    valid cookie themselves — they must obtain one from the running gateway
    process, exactly like a real client would.
    """
    response = httpx.post(f"{server}/auth/login", json={"api_key": gateway_api_key})
    response.raise_for_status()
    session_cookie = response.cookies.get(SESSION_COOKIE_NAME)
    if not session_cookie:
        raise RuntimeError(
            f"Login response did not set a {SESSION_COOKIE_NAME!r} cookie "
            f"(status={response.status_code}, body={response.text!r})"
        )
    return session_cookie


def run_process_with_pid(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float = 10.0,
) -> tuple[int, int, str, str]:
    proc = subprocess.Popen(
        list(args),
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            proc.kill()
        proc.communicate()
        raise
    finally:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                with suppress(OSError, ValueError):
                    pipe.close()
    return proc.pid, proc.wait(), stdout, stderr


@contextmanager
def isolated_gateway_process(
    *,
    env: Mapping[str, str],
    temp_path: Path,
) -> Iterator[subprocess.Popen[str]]:
    """Run the real gateway with storage scoped to the fixture's temporary directory."""
    db_path = (temp_path / "db").resolve()
    outputs_path = (temp_path / "outputs").resolve()
    checkout_db_path = (Path(__file__).resolve().parents[1] / "db").resolve()
    if db_path == checkout_db_path:
        raise ValueError("isolated gateway process cannot use the checkout DB directory")

    launch_env = dict(env)
    launch_env["GATEWAY_DB_DIR"] = str(db_path)
    (outputs_path / "images").mkdir(parents=True)
    launch_env["GATEWAY_OUTPUTS_DIR"] = str(outputs_path)
    # Browser tests must never trigger a real HTTP fetch to api.freellmapi.co;
    # a test that wants the real fetch can still override it via `env`.
    launch_env.setdefault("FREE_LLM_CATALOG_ENABLED", "false")
    proc = subprocess.Popen(
        ["./.venv/bin/python", "main.py"],
        env=launch_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        yield proc
    finally:
        stop_gateway_process(proc)


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def stop_gateway_process(proc: subprocess.Popen[str], *, timeout_seconds: float = 5.0) -> str:
    """Stop and reap a gateway subprocess, then close every owned pipe."""
    try:
        pipes = (proc.stdin, proc.stdout, proc.stderr)
        if proc.poll() is not None and all(pipe is None or pipe.closed for pipe in pipes):
            proc.wait(timeout=timeout_seconds)
            return ""

        if proc.poll() is None:
            with suppress(ProcessLookupError):
                proc.terminate()

        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                proc.kill()
            stdout, stderr = proc.communicate()

        output = stdout or ""
        if stderr:
            output = f"{output}\n{stderr}" if output else stderr
        return output
    finally:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                with suppress(OSError, ValueError):
                    pipe.close()


def wait_for_gateway(
    base_url: str,
    proc: subprocess.Popen[str],
    *,
    retries: int = 30,
    delay_seconds: float = 0.5,
) -> None:
    health_url = f"{base_url}/health"

    for _ in range(retries):
        if proc.poll() is not None:
            stdout = stop_gateway_process(proc)
            pytest.fail(f"Server exited before startup completed. Output: {stdout}")

        try:
            response = requests.get(health_url, timeout=1)
            if response.status_code == 200:
                return
        except requests.exceptions.ConnectionError:
            pass

        time.sleep(delay_seconds)

    stdout = stop_gateway_process(proc)
    pytest.fail(f"Server failed to start. Output: {stdout}")
