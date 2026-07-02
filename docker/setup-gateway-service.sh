#!/usr/bin/env sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_PROJECT_DIR=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)

PROJECT_DIR=${PROJECT_DIR:-$DEFAULT_PROJECT_DIR}
SERVICE_NAME=${SERVICE_NAME:-llm-gateway.service}
SYSTEMD_UNIT_DIR=${SYSTEMD_UNIT_DIR:-/etc/systemd/system}
SYSTEMCTL_BIN=${SYSTEMCTL_BIN:-systemctl}
PYTHON_BIN=${PYTHON_BIN:-${PROJECT_DIR}/.venv/bin/python}
SERVICE_USER=${SERVICE_USER:-${SUDO_USER:-$(id -un)}}
SERVICE_GROUP=${SERVICE_GROUP:-$(id -gn "$SERVICE_USER")}
SERVICE_FILE="${SYSTEMD_UNIT_DIR}/${SERVICE_NAME}"

require_systemctl() {
    case "$SYSTEMCTL_BIN" in
        */*)
            [ -x "$SYSTEMCTL_BIN" ] || {
                echo "systemctl helper not found or not executable: $SYSTEMCTL_BIN" >&2
                exit 1
            }
            ;;
        *)
            command -v "$SYSTEMCTL_BIN" >/dev/null 2>&1 || {
                echo "systemctl not found in PATH." >&2
                exit 1
            }
            ;;
    esac
}

if [ "$SYSTEMD_UNIT_DIR" = "/etc/systemd/system" ] && [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root when writing to /etc/systemd/system." >&2
    exit 1
fi

[ -f "${PROJECT_DIR}/main.py" ] || {
    echo "main.py not found in project directory: ${PROJECT_DIR}" >&2
    exit 1
}

[ -x "$PYTHON_BIN" ] || {
    echo "Python executable not found in .venv: $PYTHON_BIN" >&2
    exit 1
}

mkdir -p "$SYSTEMD_UNIT_DIR"
require_systemctl
CREATED_UNIT=0

if [ ! -f "$SERVICE_FILE" ]; then
    cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=LLM Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON_BIN $PROJECT_DIR/main.py
Restart=always
RestartSec=5
UMask=0007
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
    chmod 0644 "$SERVICE_FILE"
    echo "Created systemd unit: $SERVICE_FILE"
    CREATED_UNIT=1
else
    echo "Systemd unit already exists: $SERVICE_FILE"
fi

"$SYSTEMCTL_BIN" daemon-reload

if [ ! -f "$SERVICE_FILE" ]; then
    echo "Systemd unit was not created: $SERVICE_FILE" >&2
    exit 1
fi

UNIT_SECTION_COUNT=$(grep -c '^\[Unit\]' "$SERVICE_FILE" 2>/dev/null || true)
SERVICE_SECTION_COUNT=$(grep -c '^\[Service\]' "$SERVICE_FILE" 2>/dev/null || true)
EXEC_START_COUNT=$(grep -c '^ExecStart=' "$SERVICE_FILE" 2>/dev/null || true)

if [ "$UNIT_SECTION_COUNT" -gt 0 ] && [ "$SERVICE_SECTION_COUNT" -gt 0 ]; then
    :
else
    echo "Existing service file does not look like a valid systemd unit: $SERVICE_FILE" >&2
    exit 1
fi

if [ "$EXEC_START_COUNT" -eq 0 ]; then
    echo "Existing service file is missing ExecStart: $SERVICE_FILE" >&2
    exit 1
fi

if [ "$CREATED_UNIT" -eq 1 ] && ! "$SYSTEMCTL_BIN" is-enabled "$SERVICE_NAME" >/dev/null 2>&1; then
    "$SYSTEMCTL_BIN" enable "$SERVICE_NAME"
fi

"$SYSTEMCTL_BIN" restart "$SERVICE_NAME"
echo "Service restarted: $SERVICE_NAME"
