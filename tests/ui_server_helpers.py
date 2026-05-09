import socket
import time

import pytest
import requests


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def wait_for_gateway(base_url: str, proc, *, retries: int = 30, delay_seconds: float = 0.5) -> None:
    health_url = f"{base_url}/health"

    for _ in range(retries):
        if proc.poll() is not None:
            stdout = ""
            if proc.stdout is not None:
                stdout = proc.stdout.read()
            pytest.fail(f"Server exited before startup completed. Output: {stdout}")

        try:
            response = requests.get(health_url, timeout=1)
            if response.status_code == 200:
                return
        except requests.exceptions.ConnectionError:
            pass

        time.sleep(delay_seconds)

    proc.terminate()
    stdout = ""
    if proc.stdout is not None:
        stdout = proc.stdout.read()
    pytest.fail(f"Server failed to start. Output: {stdout}")
