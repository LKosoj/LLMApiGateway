#!/usr/bin/env python
"""
Health check script for LLM Gateway Docker container.
This script checks if the LLM Gateway is running and responding to requests.
Exit code 0 means the service is healthy, non-zero means unhealthy.
"""

import http.client
import os
import socket
import sys


def check_health() -> bool:
    """
    Check if the LLM Gateway is ready with one HEAD request to /health.
    """
    host = "localhost"
    port = int(os.environ.get("GATEWAY_PORT", 9000))
    conn = None

    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("HEAD", "/health")
        response = conn.getresponse()

        if response.status != 200:
            print(f"Unhealthy: Received status code {response.status}")
            return False

        print("Service is healthy")
        return True

    except (socket.error, http.client.HTTPException) as exc:
        print(f"Error connecting to service: {exc}")
        return False

    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    if check_health():
        sys.exit(0)  # Success
    else:
        sys.exit(1)  # Failure
