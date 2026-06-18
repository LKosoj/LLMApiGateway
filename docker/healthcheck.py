#!/usr/bin/env python
"""
Health check script for LLM Gateway Docker container.
This script checks if the LLM Gateway is running and responding to requests.
Exit code 0 means the service is healthy, non-zero means unhealthy.
"""

import os
import sys
import http.client
import socket
import time

def check_health():
    """
    Check if the LLM Gateway is healthy by making a HEAD request to /healthz.
    """
    host = "localhost"
    port = int(os.environ.get("GATEWAY_PORT", 9000))

    # Give the service a moment to respond (useful for startup)
    retries = 3
    while retries > 0:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("HEAD", "/healthz")
            response = conn.getresponse()

            if response.status != 200:
                print(f"Unhealthy: Received status code {response.status}")
                return False

            print("Service is healthy")
            return True

        except (socket.error, http.client.HTTPException) as e:
            print(f"Error connecting to service: {e}")
            retries -= 1
            if retries > 0:
                print(f"Retrying in 1 second... ({retries} attempts left)")
                time.sleep(1)
            else:
                return False

        finally:
            if 'conn' in locals():
                conn.close()

    return False

if __name__ == "__main__":
    if check_health():
        sys.exit(0)  # Success
    else:
        sys.exit(1)  # Failure
