#!/usr/bin/env sh

set -eu

fail() {
    printf 'restart-gateway-service: %s\n' "$1" >&2
    exit 1
}

SERVICE_NAME=llm-gateway.service

sudo systemctl restart "$SERVICE_NAME" || fail "could not restart $SERVICE_NAME"
printf 'restart-gateway-service: %s restarted, following logs (Ctrl+C to stop)\n' \
    "$SERVICE_NAME"
exec sudo journalctl -u "$SERVICE_NAME" -f
