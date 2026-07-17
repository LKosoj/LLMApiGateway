#!/usr/bin/env sh

set -eu

fail() {
    printf 'setup-gateway-service: %s\n' "$1" >&2
    exit 1
}

MODE=production
case "$#:$*" in
    0:) ;;
    1:--test-mode) MODE="test" ;;
    *) fail "usage: setup-gateway-service.sh [--test-mode]" ;;
esac

if [ "$MODE" = production ]; then
    PATH=/usr/sbin:/usr/bin:/sbin:/bin
    export PATH
fi
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP

if [ "$MODE" = production ]; then
    PRODUCTION_OVERRIDE=
    [ "${PROJECT_DIR+x}" != x ] || PRODUCTION_OVERRIDE=PROJECT_DIR
    [ "${PYTHON_BIN+x}" != x ] || PRODUCTION_OVERRIDE=PYTHON_BIN
    [ "${SYSTEMD_UNIT_DIR+x}" != x ] || PRODUCTION_OVERRIDE=SYSTEMD_UNIT_DIR
    [ "${SYSTEMCTL_BIN+x}" != x ] || PRODUCTION_OVERRIDE=SYSTEMCTL_BIN
    [ "${SYSTEMD_ANALYZE_BIN+x}" != x ] || PRODUCTION_OVERRIDE=SYSTEMD_ANALYZE_BIN
    [ "${ENV_DIR+x}" != x ] || PRODUCTION_OVERRIDE=ENV_DIR
    [ "${STATE_DIR+x}" != x ] || PRODUCTION_OVERRIDE=STATE_DIR
    [ "${LOG_DIR+x}" != x ] || PRODUCTION_OVERRIDE=LOG_DIR
    [ "${CACHE_DIR+x}" != x ] || PRODUCTION_OVERRIDE=CACHE_DIR
    [ "${SERVICE_UID+x}" != x ] || PRODUCTION_OVERRIDE=SERVICE_UID
    [ "${SERVICE_GID+x}" != x ] || PRODUCTION_OVERRIDE=SERVICE_GID
    [ "${ENV_UID+x}" != x ] || PRODUCTION_OVERRIDE=ENV_UID
    [ "${GETENT_BIN+x}" != x ] || PRODUCTION_OVERRIDE=GETENT_BIN
    [ "${GROUPADD_BIN+x}" != x ] || PRODUCTION_OVERRIDE=GROUPADD_BIN
    [ "${USERADD_BIN+x}" != x ] || PRODUCTION_OVERRIDE=USERADD_BIN
    [ "${SERVICE_NAME+x}" != x ] || PRODUCTION_OVERRIDE=SERVICE_NAME
    [ "${SERVICE_USER+x}" != x ] || PRODUCTION_OVERRIDE=SERVICE_USER
    [ "${SERVICE_GROUP+x}" != x ] || PRODUCTION_OVERRIDE=SERVICE_GROUP
    [ "${TMPDIR+x}" != x ] || PRODUCTION_OVERRIDE=TMPDIR
    [ "${ENV_GID+x}" != x ] || PRODUCTION_OVERRIDE=ENV_GID
    [ -z "$PRODUCTION_OVERRIDE" ] || {
        fail "forbidden production override: $PRODUCTION_OVERRIDE"
    }
fi

SERVICE_NAME=llm-gateway.service
SERVICE_USER=llmgateway
SERVICE_GROUP=llmgateway
TRUST_PYTHON_BIN=/usr/bin/python3

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
CANONICAL_PROJECT_DIR=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)

if [ "$MODE" = production ]; then
    PROJECT_DIR=$CANONICAL_PROJECT_DIR
    PYTHON_BIN=$PROJECT_DIR/.venv/bin/python
    SYSTEMD_UNIT_DIR=/etc/systemd/system
    SYSTEMCTL_BIN=/usr/bin/systemctl
    SYSTEMD_ANALYZE_BIN=/usr/bin/systemd-analyze
    ENV_DIR=/etc/llm-gateway
    STATE_DIR=/var/lib/llm-gateway
    LOG_DIR=/var/log/llm-gateway
    CACHE_DIR=/var/cache/llm-gateway
    SERVICE_UID=10001
    SERVICE_GID=10001
    ENV_UID=0
    ENV_GID=0
    TMPDIR=/tmp
    GETENT_BIN=/usr/bin/getent
    GROUPADD_BIN=/usr/sbin/groupadd
    USERADD_BIN=/usr/sbin/useradd
else
    PROJECT_DIR=${PROJECT_DIR:-$CANONICAL_PROJECT_DIR}
    PYTHON_BIN=${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}
    SYSTEMD_UNIT_DIR=${SYSTEMD_UNIT_DIR:?}
    SYSTEMCTL_BIN=${SYSTEMCTL_BIN:?}
    SYSTEMD_ANALYZE_BIN=${SYSTEMD_ANALYZE_BIN:?}
    ENV_DIR=${ENV_DIR:?}
    STATE_DIR=${STATE_DIR:?}
    LOG_DIR=${LOG_DIR:?}
    CACHE_DIR=${CACHE_DIR:?}
    SERVICE_UID=${SERVICE_UID:?}
    SERVICE_GID=${SERVICE_GID:?}
    ENV_UID=${ENV_UID:?}
    ENV_GID=${ENV_GID:?}
    TMPDIR=${TMPDIR:?}
    GETENT_BIN=${GETENT_BIN:?}
    GROUPADD_BIN=${GROUPADD_BIN:?}
    USERADD_BIN=${USERADD_BIN:?}
fi

SERVICE_FILE=$SYSTEMD_UNIT_DIR/$SERVICE_NAME
RUNTIME_FILE=$ENV_DIR/runtime.env
GATEWAY_ENV_FILE=$ENV_DIR/gateway.env
MIGRATION_SCRIPT=$PROJECT_DIR/docker/systemd_migration.py
READINESS_SCRIPT=$PROJECT_DIR/docker/systemd_readiness.py
EXPECTED_UNIT_FILE=
EXPECTED_RUNTIME_FILE=
INSTALL_UNIT_FILE=
INSTALL_RUNTIME_FILE=
TEMPLATE_DIR=
ROLLBACK_ARMED=0
UNIT_EXISTED=0
RUNTIME_EXISTED=0
UNIT_PUBLISHED=0
RUNTIME_PUBLISHED=0
NEWLY_ENABLED=0
UNIT_BACKUP=
RUNTIME_BACKUP=
UNIT_ORIGINAL_METADATA=
RUNTIME_ORIGINAL_METADATA=
ACTIVE_TEMP_FILE=

cleanup() {
    [ -z "$EXPECTED_UNIT_FILE" ] || rm -f -- "$EXPECTED_UNIT_FILE" || :
    [ -z "$EXPECTED_RUNTIME_FILE" ] || rm -f -- "$EXPECTED_RUNTIME_FILE" || :
    [ -z "$INSTALL_UNIT_FILE" ] || rm -f -- "$INSTALL_UNIT_FILE" || :
    [ -z "$INSTALL_RUNTIME_FILE" ] || rm -f -- "$INSTALL_RUNTIME_FILE" || :
    [ -z "$TEMPLATE_DIR" ] || rm -rf -- "$TEMPLATE_DIR" || :
    [ -z "$ACTIVE_TEMP_FILE" ] || rm -f -- "$ACTIVE_TEMP_FILE" || :
}

on_exit() {
    EXIT_STATUS=$?
    trap - 0
    if [ "$EXIT_STATUS" -ne 0 ]; then
        if ! rollback_installation; then
            printf 'setup-gateway-service: rollback incomplete; service left stopped\n' >&2
        fi
    fi
    cleanup
    exit "$EXIT_STATUS"
}

trap on_exit 0
trap 'exit 1' HUP INT TERM

validate_absolute_path() {
    PATH_LABEL=$1
    PATH_VALUE=$2
    case "$PATH_VALUE" in
        /*) ;;
        *) fail "$PATH_LABEL must be an absolute safe path" ;;
    esac
    case "$PATH_VALUE" in
        /|*/|*//*|*/./*|*/.|*/../*|*/..|*[!A-Za-z0-9_./-]*)
            fail "$PATH_LABEL must be an absolute safe path"
            ;;
    esac
}

validate_command() {
    COMMAND_LABEL=$1
    COMMAND_PATH=$2
    validate_absolute_path "$COMMAND_LABEL" "$COMMAND_PATH"
    if [ ! -f "$COMMAND_PATH" ] || [ ! -x "$COMMAND_PATH" ]; then
        fail "$COMMAND_LABEL is not an executable regular file"
    fi
}

validate_numeric_id() {
    ID_LABEL=$1
    ID_VALUE=$2
    case "$ID_VALUE" in
        ''|*[!0-9]*) fail "$ID_LABEL must be a decimal numeric ID" ;;
    esac
}

validate_trusted_runtime_path() {
    RUNTIME_PATH=$1
    RUNTIME_KIND=$2
    RUNTIME_EXPECTED_UID=${3:-$ENV_UID}
    if [ -L "$RUNTIME_PATH" ]; then
        fail "untrusted project runtime path"
    fi
    if [ "$RUNTIME_KIND" = directory ]; then
        [ -d "$RUNTIME_PATH" ] || fail "untrusted project runtime path"
    else
        [ -f "$RUNTIME_PATH" ] || fail "untrusted project runtime path"
    fi
    RUNTIME_METADATA=$(stat -c '%a:%u:%g' -- "$RUNTIME_PATH" 2>/dev/null) || {
        fail "untrusted project runtime path"
    }
    RUNTIME_MODE=${RUNTIME_METADATA%%:*}
    RUNTIME_OWNER=${RUNTIME_METADATA#*:}
    RUNTIME_UID=${RUNTIME_OWNER%%:*}
    RUNTIME_GID=${RUNTIME_OWNER#*:}
    case "$RUNTIME_MODE:$RUNTIME_UID:$RUNTIME_GID" in
        *[!0-9:]*) fail "untrusted project runtime path" ;;
    esac
    [ "$RUNTIME_UID" -eq "$RUNTIME_EXPECTED_UID" ] || fail "untrusted project runtime path"
    [ $((0$RUNTIME_MODE & 0022)) -eq 0 ] || fail "untrusted project runtime path"
}

validate_recursive_migration_code() {
    "$TRUST_PYTHON_BIN" -I -S -c '
import glob
import os
import stat
import sys

owner = int(sys.argv[1])
project = sys.argv[2]

def validate(metadata, directory):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(metadata.st_mode) or metadata.st_uid != owner:
        raise ValueError
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ValueError

def walk(path):
    validate(os.lstat(path), True)
    with os.scandir(path) as entries:
        for entry in entries:
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                walk(entry.path)
            elif stat.S_ISREG(metadata.st_mode):
                validate(metadata, False)
            else:
                raise ValueError

try:
    walk(os.path.join(project, "docker"))
    walk(os.path.join(project, "llm_gateway_core"))
    venv = os.path.join(project, ".venv")
    sites = glob.glob(os.path.join(venv, "lib", "python*", "site-packages"))
    if not sites:
        raise ValueError
    for site in sites:
        parent = site
        while parent != venv:
            validate(os.lstat(parent), True)
            parent = os.path.dirname(parent)
        walk(site)
except (OSError, ValueError):
    raise SystemExit(1) from None
' "$ENV_UID" "$PROJECT_DIR" || fail "untrusted project runtime path"
}

validate_trusted_python() {
    if [ -L "$PYTHON_BIN" ]; then
        RESOLVED_PYTHON=$(readlink -f -- "$PYTHON_BIN" 2>/dev/null) || {
            fail "untrusted project runtime path"
        }
        case "$RESOLVED_PYTHON" in
            /usr/bin/*|/usr/local/bin/*) ;;
            *) fail "untrusted project runtime path" ;;
        esac
        validate_trusted_runtime_path "$RESOLVED_PYTHON" file
    else
        validate_trusted_runtime_path "$PYTHON_BIN" file
    fi
    [ -x "$PYTHON_BIN" ] || fail "untrusted project runtime path"
}

validate_trusted_directory() {
    TRUSTED_LABEL=$1
    TRUSTED_DIRECTORY=$2
    TRUSTED_UID=$3
    TRUSTED_GID=$4
    if [ -L "$TRUSTED_DIRECTORY" ] || [ ! -d "$TRUSTED_DIRECTORY" ]; then
        fail "untrusted $TRUSTED_LABEL"
    fi
    DIRECTORY_METADATA=$(stat -c '%a:%u:%g' -- "$TRUSTED_DIRECTORY" 2>/dev/null) || {
        fail "untrusted $TRUSTED_LABEL"
    }
    DIRECTORY_MODE=${DIRECTORY_METADATA%%:*}
    DIRECTORY_OWNER=${DIRECTORY_METADATA#*:}
    case "$DIRECTORY_MODE:$DIRECTORY_OWNER" in
        *[!0-9:]*) fail "untrusted $TRUSTED_LABEL" ;;
    esac
    [ "$DIRECTORY_OWNER" = "$TRUSTED_UID:$TRUSTED_GID" ] || {
        fail "untrusted $TRUSTED_LABEL"
    }
    [ $((0$DIRECTORY_MODE & 0022)) -eq 0 ] || fail "untrusted $TRUSTED_LABEL"
}

validate_target_parent() {
    TARGET_LABEL=$1
    TARGET_PATH=$2
    TARGET_UID=$3
    TARGET_GID=$4
    if [ -e "$TARGET_PATH" ] || [ -L "$TARGET_PATH" ]; then
        validate_trusted_directory "$TARGET_LABEL" "$TARGET_PATH" "$TARGET_UID" "$TARGET_GID"
    else
        validate_trusted_directory "$TARGET_LABEL parent" "$(dirname -- "$TARGET_PATH")" "$ENV_UID" "$ENV_GID"
    fi
}

get_entry() {
    ENTRY_DATABASE=$1
    ENTRY_KEY=$2
    if ENTRY_OUTPUT=$("$GETENT_BIN" "$ENTRY_DATABASE" "$ENTRY_KEY" 2>/dev/null); then
        printf '%s\n' "$ENTRY_OUTPUT"
        return 0
    else
        ENTRY_STATUS=$?
    fi
    [ "$ENTRY_STATUS" -eq 2 ] || fail "account database lookup failed"
}

reject_multiline_entry() {
    case "$1" in
        *'
'*) fail "account database returned multiple entries" ;;
    esac
}

entry_name() {
    printf '%s\n' "${1%%:*}"
}

entry_id() {
    ENTRY_REMAINDER=${1#*:}
    ENTRY_REMAINDER=${ENTRY_REMAINDER#*:}
    printf '%s\n' "${ENTRY_REMAINDER%%:*}"
}

passwd_gid() {
    PASSWD_REMAINDER=${1#*:}
    PASSWD_REMAINDER=${PASSWD_REMAINDER#*:}
    PASSWD_REMAINDER=${PASSWD_REMAINDER#*:}
    printf '%s\n' "${PASSWD_REMAINDER%%:*}"
}

passwd_home() {
    PASSWD_REMAINDER=${1#*:}
    PASSWD_REMAINDER=${PASSWD_REMAINDER#*:}
    PASSWD_REMAINDER=${PASSWD_REMAINDER#*:}
    PASSWD_REMAINDER=${PASSWD_REMAINDER#*:}
    PASSWD_REMAINDER=${PASSWD_REMAINDER#*:}
    printf '%s\n' "${PASSWD_REMAINDER%%:*}"
}

passwd_shell() {
    PASSWD_REMAINDER=${1#*:}
    PASSWD_REMAINDER=${PASSWD_REMAINDER#*:}
    PASSWD_REMAINDER=${PASSWD_REMAINDER#*:}
    PASSWD_REMAINDER=${PASSWD_REMAINDER#*:}
    PASSWD_REMAINDER=${PASSWD_REMAINDER#*:}
    PASSWD_REMAINDER=${PASSWD_REMAINDER#*:}
    printf '%s\n' "${PASSWD_REMAINDER%%:*}"
}

validate_account_collisions() {
    GROUP_BY_NAME=$(get_entry group "$SERVICE_GROUP")
    GROUP_BY_ID=$(get_entry group "$SERVICE_GID")
    USER_BY_NAME=$(get_entry passwd "$SERVICE_USER")
    USER_BY_ID=$(get_entry passwd "$SERVICE_UID")
    reject_multiline_entry "$GROUP_BY_NAME"
    reject_multiline_entry "$GROUP_BY_ID"
    reject_multiline_entry "$USER_BY_NAME"
    reject_multiline_entry "$USER_BY_ID"

    GROUP_NEEDS_CREATE=0
    if [ -z "$GROUP_BY_NAME" ] && [ -z "$GROUP_BY_ID" ]; then
        GROUP_NEEDS_CREATE=1
    else
        if [ -z "$GROUP_BY_NAME" ] || [ -z "$GROUP_BY_ID" ]; then
            fail "service group name/GID collision"
        fi
        [ "$(entry_name "$GROUP_BY_NAME")" = "$SERVICE_GROUP" ] || {
            fail "service group name/GID collision"
        }
        [ "$(entry_id "$GROUP_BY_NAME")" = "$SERVICE_GID" ] || {
            fail "service group name/GID collision"
        }
        [ "$GROUP_BY_NAME" = "$GROUP_BY_ID" ] || {
            fail "service group name/GID collision"
        }
    fi

    USER_NEEDS_CREATE=0
    if [ -z "$USER_BY_NAME" ] && [ -z "$USER_BY_ID" ]; then
        USER_NEEDS_CREATE=1
    else
        if [ -z "$USER_BY_NAME" ] || [ -z "$USER_BY_ID" ]; then
            fail "service user name/UID collision"
        fi
        [ "$(entry_name "$USER_BY_NAME")" = "$SERVICE_USER" ] || {
            fail "service user name/UID collision"
        }
        [ "$(entry_id "$USER_BY_NAME")" = "$SERVICE_UID" ] || {
            fail "service user name/UID collision"
        }
        [ "$(passwd_gid "$USER_BY_NAME")" = "$SERVICE_GID" ] || {
            fail "service user primary GID mismatch"
        }
        [ "$(passwd_home "$USER_BY_NAME")" = "$STATE_DIR" ] || {
            fail "service user home mismatch"
        }
        [ "$(passwd_shell "$USER_BY_NAME")" = /usr/sbin/nologin ] || {
            fail "service user shell mismatch"
        }
        [ "$USER_BY_NAME" = "$USER_BY_ID" ] || {
            fail "service user name/UID collision"
        }
    fi
}

render_runtime() {
    cat <<EOF
APP_DIR=$PROJECT_DIR
LLMGATEWAY_ENV_FILE=$GATEWAY_ENV_FILE
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
GATEWAY_WORKERS=1
GATEWAY_DB_DIR=$STATE_DIR
GATEWAY_OUTPUTS_DIR=$STATE_DIR/outputs
LLMGATEWAY_LOG_DIR=$LOG_DIR
CLOAKBROWSER_CACHE_DIR=$CACHE_DIR
LLMGATEWAY_CONFIG_DIR=$STATE_DIR/config
PROVIDERS_FILENAME=$STATE_DIR/config/providers.json
FALLBACK_RULES_FILENAME=$STATE_DIR/config/models_fallback_rules.json
OPERATION_RULES_FILENAME=$STATE_DIR/config/models_operation_rules.json
FUSION_RULES_FILENAME=$STATE_DIR/config/models_fusion_rules.json
MODEL_RULES_FILENAME=$STATE_DIR/config/models_model_rules.json
ROUTER_RULES_FILENAME=$STATE_DIR/config/models_router_rules.json
EOF
}

render_unit() {
    cat <<EOF
[Unit]
Description=LLM Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$GATEWAY_ENV_FILE
EnvironmentFile=$RUNTIME_FILE
ExecStart=$PYTHON_BIN $PROJECT_DIR/main.py
ExecStartPost=$PYTHON_BIN $READINESS_SCRIPT
TimeoutStartSec=75
StateDirectory=llm-gateway
StateDirectoryMode=0750
LogsDirectory=llm-gateway
LogsDirectoryMode=0750
CacheDirectory=llm-gateway
CacheDirectoryMode=0750
Restart=always
RestartSec=5
UMask=0007
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
CapabilityBoundingSet=
AmbientCapabilities=
ReadWritePaths=$STATE_DIR $LOG_DIR $CACHE_DIR

[Install]
WantedBy=multi-user.target
EOF
}

ensure_directory() {
    DIRECTORY_PATH=$1
    DIRECTORY_MODE=$2
    DIRECTORY_UID=$3
    DIRECTORY_GID=$4
    if [ -e "$DIRECTORY_PATH" ] || [ -L "$DIRECTORY_PATH" ]; then
        if [ -L "$DIRECTORY_PATH" ] || [ ! -d "$DIRECTORY_PATH" ]; then
            fail "unsafe runtime directory"
        fi
    else
        mkdir -p -- "$DIRECTORY_PATH"
    fi
    chmod "$DIRECTORY_MODE" -- "$DIRECTORY_PATH"
    CURRENT_OWNER=$(stat -c '%u:%g' -- "$DIRECTORY_PATH")
    [ "$CURRENT_OWNER" = "$DIRECTORY_UID:$DIRECTORY_GID" ] || {
        chown "$DIRECTORY_UID:$DIRECTORY_GID" -- "$DIRECTORY_PATH"
    }
}

sync_file_and_parent() {
    (
        cd -- "$PROJECT_DIR"
        "$PYTHON_BIN" -I -S -c '
import os
import sys

path = sys.argv[1]
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
parent = os.path.dirname(path)
descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
' "$1"
    )
}

sync_directory() {
    (
        cd -- "$PROJECT_DIR"
        "$PYTHON_BIN" -I -S -c '
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
' "$1"
    )
}

atomic_install() {
    SOURCE_FILE=$1
    TARGET_FILE=$2
    TARGET_MODE=$3
    TARGET_UID=$4
    TARGET_GID=$5
    TARGET_DIRECTORY=$(dirname -- "$TARGET_FILE")
    ACTIVE_TEMP_FILE=$(mktemp "$TARGET_DIRECTORY/.llm-gateway-install.XXXXXX") || {
        fail "cannot create atomic install file"
    }
    cp -- "$SOURCE_FILE" "$ACTIVE_TEMP_FILE"
    chmod "$TARGET_MODE" -- "$ACTIVE_TEMP_FILE"
    CURRENT_OWNER=$(stat -c '%u:%g' -- "$ACTIVE_TEMP_FILE")
    [ "$CURRENT_OWNER" = "$TARGET_UID:$TARGET_GID" ] || {
        chown "$TARGET_UID:$TARGET_GID" -- "$ACTIVE_TEMP_FILE"
    }
    sync_file_and_parent "$ACTIVE_TEMP_FILE" || fail "durability sync failed"
    mv -f -- "$ACTIVE_TEMP_FILE" "$TARGET_FILE"
    ACTIVE_TEMP_FILE=
    sync_directory "$TARGET_DIRECTORY" || fail "durability sync failed"
}

validate_gateway_environment() {
    if [ -L "$GATEWAY_ENV_FILE" ] || [ ! -f "$GATEWAY_ENV_FILE" ]; then
        fail "unsafe gateway.env"
    fi
    GATEWAY_METADATA=$(stat -c '%a:%u:%g' -- "$GATEWAY_ENV_FILE" 2>/dev/null) || {
        fail "unsafe gateway.env"
    }
    [ "$GATEWAY_METADATA" = "640:$ENV_UID:$SERVICE_GID" ] || {
        fail "unsafe gateway.env"
    }
}

backup_file() {
    BACKUP_SOURCE=$1
    ACTIVE_TEMP_FILE=$(mktemp "$BACKUP_SOURCE.backup.XXXXXX") || {
        fail "cannot create deployment backup"
    }
    cp -- "$BACKUP_SOURCE" "$ACTIVE_TEMP_FILE"
    chmod 0600 -- "$ACTIVE_TEMP_FILE"
    BACKUP_OWNER=$(stat -c '%u:%g' -- "$ACTIVE_TEMP_FILE")
    [ "$BACKUP_OWNER" = "$ENV_UID:$ENV_GID" ] || {
        chown "$ENV_UID:$ENV_GID" -- "$ACTIVE_TEMP_FILE"
    }
    sync_file_and_parent "$ACTIVE_TEMP_FILE" || fail "durability sync failed"
    CREATED_BACKUP=$ACTIVE_TEMP_FILE
    ACTIVE_TEMP_FILE=
}

restore_file() {
    RESTORE_BACKUP=$1
    RESTORE_TARGET=$2
    RESTORE_METADATA=$3
    RESTORE_DIRECTORY=$(dirname -- "$RESTORE_TARGET")
    ACTIVE_TEMP_FILE=$(mktemp "$RESTORE_DIRECTORY/.llm-gateway-restore.XXXXXX") || return 1
    cp -- "$RESTORE_BACKUP" "$ACTIVE_TEMP_FILE" || {
        rm -f -- "$ACTIVE_TEMP_FILE"
        ACTIVE_TEMP_FILE=
        return 1
    }
    RESTORE_MODE=${RESTORE_METADATA%%:*}
    RESTORE_OWNER=${RESTORE_METADATA#*:}
    chmod "$RESTORE_MODE" -- "$ACTIVE_TEMP_FILE" || {
        rm -f -- "$ACTIVE_TEMP_FILE"
        ACTIVE_TEMP_FILE=
        return 1
    }
    chown "$RESTORE_OWNER" -- "$ACTIVE_TEMP_FILE" 2>/dev/null || {
        CURRENT_RESTORE_OWNER=$(stat -c '%u:%g' -- "$ACTIVE_TEMP_FILE" 2>/dev/null) || :
        [ "$CURRENT_RESTORE_OWNER" = "$RESTORE_OWNER" ] || {
            rm -f -- "$ACTIVE_TEMP_FILE"
            ACTIVE_TEMP_FILE=
            return 1
        }
    }
    sync_file_and_parent "$ACTIVE_TEMP_FILE" || {
        rm -f -- "$ACTIVE_TEMP_FILE"
        ACTIVE_TEMP_FILE=
        return 1
    }
    mv -f -- "$ACTIVE_TEMP_FILE" "$RESTORE_TARGET" || {
        rm -f -- "$ACTIVE_TEMP_FILE"
        ACTIVE_TEMP_FILE=
        return 1
    }
    ACTIVE_TEMP_FILE=
    sync_directory "$RESTORE_DIRECTORY"
}

rollback_installation() {
    [ "$ROLLBACK_ARMED" -eq 1 ] || return 0
    ROLLBACK_FAILED=0
    "$SYSTEMCTL_BIN" stop "$SERVICE_NAME" >/dev/null 2>&1 || ROLLBACK_FAILED=1
    if [ "$RUNTIME_PUBLISHED" -eq 1 ]; then
        if [ "$RUNTIME_EXISTED" -eq 1 ]; then
            restore_file "$RUNTIME_BACKUP" "$RUNTIME_FILE" "$RUNTIME_ORIGINAL_METADATA" || {
                ROLLBACK_FAILED=1
            }
        else
            rm -f -- "$RUNTIME_FILE" || ROLLBACK_FAILED=1
            sync_directory "$(dirname -- "$RUNTIME_FILE")" || ROLLBACK_FAILED=1
        fi
    fi
    if [ "$UNIT_PUBLISHED" -eq 1 ]; then
        if [ "$UNIT_EXISTED" -eq 1 ]; then
            restore_file "$UNIT_BACKUP" "$SERVICE_FILE" "$UNIT_ORIGINAL_METADATA" || {
                ROLLBACK_FAILED=1
            }
        else
            rm -f -- "$SERVICE_FILE" || ROLLBACK_FAILED=1
            sync_directory "$(dirname -- "$SERVICE_FILE")" || ROLLBACK_FAILED=1
        fi
        "$SYSTEMCTL_BIN" daemon-reload >/dev/null 2>&1 || ROLLBACK_FAILED=1
    fi
    [ "$NEWLY_ENABLED" -eq 0 ] || {
        "$SYSTEMCTL_BIN" disable "$SERVICE_NAME" >/dev/null 2>&1 || ROLLBACK_FAILED=1
    }
    ROLLBACK_ARMED=0
    [ "$ROLLBACK_FAILED" -eq 0 ]
}

run_migration_command() {
    MIGRATION_ACTION=$1
    set -- "$MIGRATION_ACTION" \
        --source-root "$PROJECT_DIR" \
        --target-env-dir "$ENV_DIR" \
        --target-state-dir "$STATE_DIR" \
        --target-cache-dir "$CACHE_DIR"
    if [ -n "$SOURCE_CACHE_DIR" ]; then
        set -- "$@" --source-cache-dir "$SOURCE_CACHE_DIR"
    fi
    (
        cd -- "$PROJECT_DIR"
        "$PYTHON_BIN" -I "$MIGRATION_SCRIPT" "$@"
    )
}

parse_migration_required() {
    (
        cd -- "$PROJECT_DIR"
        "$PYTHON_BIN" -I -S -c '
import json
import sys

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result

def contains_nul(value):
    if isinstance(value, str):
        return "\0" in value
    if isinstance(value, list):
        return any(contains_nul(item) for item in value)
    if isinstance(value, dict):
        return any(contains_nul(key) or contains_nul(item) for key, item in value.items())
    return False

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        payload = json.load(stream, object_pairs_hook=unique_object)
except (OSError, ValueError, UnicodeError):
    raise SystemExit(1) from None
if contains_nul(payload) or type(payload) is not dict or type(payload.get("migration_required")) is not bool:
    raise SystemExit(1)
print("true" if payload["migration_required"] else "false")
' "$1"
    )
}

for PATH_PAIR in \
    "PROJECT_DIR:$PROJECT_DIR" \
    "PYTHON_BIN:$PYTHON_BIN" \
    "SYSTEMD_UNIT_DIR:$SYSTEMD_UNIT_DIR" \
    "ENV_DIR:$ENV_DIR" \
    "STATE_DIR:$STATE_DIR" \
    "LOG_DIR:$LOG_DIR" \
    "CACHE_DIR:$CACHE_DIR" \
    "TMPDIR:$TMPDIR"
do
    validate_absolute_path "${PATH_PAIR%%:*}" "${PATH_PAIR#*:}"
done
validate_command PYTHON_BIN "$PYTHON_BIN"
validate_command TRUST_PYTHON_BIN "$TRUST_PYTHON_BIN"
validate_command SYSTEMCTL_BIN "$SYSTEMCTL_BIN"
validate_command SYSTEMD_ANALYZE_BIN "$SYSTEMD_ANALYZE_BIN"
validate_command GETENT_BIN "$GETENT_BIN"
validate_command GROUPADD_BIN "$GROUPADD_BIN"
validate_command USERADD_BIN "$USERADD_BIN"
validate_numeric_id SERVICE_UID "$SERVICE_UID"
validate_numeric_id SERVICE_GID "$SERVICE_GID"
validate_numeric_id ENV_UID "$ENV_UID"
validate_numeric_id ENV_GID "$ENV_GID"
[ "$SERVICE_UID" -gt 0 ] || fail "SERVICE_UID must be non-root"
[ "$SERVICE_GID" -gt 0 ] || fail "SERVICE_GID must be non-root"

if [ "$MODE" = production ]; then
    [ "$(/usr/bin/id -u)" -eq 0 ] || fail "production installation requires root"
fi
[ ! -L "$0" ] || fail "untrusted project runtime path"
TRUST_PYTHON_TARGET=$(readlink -f -- "$TRUST_PYTHON_BIN" 2>/dev/null) || {
    fail "untrusted project runtime path"
}
case "$TRUST_PYTHON_TARGET" in
    /usr/bin/python3|/usr/bin/python3.*) ;;
    *) fail "untrusted project runtime path" ;;
esac
validate_trusted_runtime_path /usr/bin directory 0
validate_trusted_runtime_path "$TRUST_PYTHON_TARGET" file 0
validate_trusted_runtime_path "$PROJECT_DIR" directory
validate_trusted_runtime_path "$PROJECT_DIR/docker" directory
validate_trusted_runtime_path "$PROJECT_DIR/.venv" directory
validate_trusted_runtime_path "$PROJECT_DIR/.venv/bin" directory
validate_trusted_runtime_path "$PROJECT_DIR/.venv/pyvenv.cfg" file
validate_trusted_runtime_path "$PROJECT_DIR/main.py" file
validate_trusted_runtime_path "$READINESS_SCRIPT" file
validate_recursive_migration_code
validate_trusted_python

validate_account_collisions
validate_trusted_directory "systemd unit directory" "$SYSTEMD_UNIT_DIR" "$ENV_UID" "$ENV_GID"
validate_target_parent "environment directory" "$ENV_DIR" "$ENV_UID" "$SERVICE_GID"
validate_target_parent "state directory" "$STATE_DIR" "$SERVICE_UID" "$SERVICE_GID"
validate_target_parent "log directory" "$LOG_DIR" "$SERVICE_UID" "$SERVICE_GID"
validate_target_parent "cache directory" "$CACHE_DIR" "$SERVICE_UID" "$SERVICE_GID"

managed_directory_needs_change() {
    MANAGED_METADATA=$(stat -c '%a:%u:%g' -- "$1" 2>/dev/null) || return 0
    [ "$MANAGED_METADATA" != "750:$2:$3" ]
}

DIRECTORY_CHANGED=0
managed_directory_needs_change "$ENV_DIR" "$ENV_UID" "$SERVICE_GID" && DIRECTORY_CHANGED=1
managed_directory_needs_change "$STATE_DIR" "$SERVICE_UID" "$SERVICE_GID" && DIRECTORY_CHANGED=1
managed_directory_needs_change "$LOG_DIR" "$SERVICE_UID" "$SERVICE_GID" && DIRECTORY_CHANGED=1
managed_directory_needs_change "$CACHE_DIR" "$SERVICE_UID" "$SERVICE_GID" && DIRECTORY_CHANGED=1

if [ -e "$SERVICE_FILE" ] || [ -L "$SERVICE_FILE" ]; then
    if [ -L "$SERVICE_FILE" ] || [ ! -f "$SERVICE_FILE" ]; then
        fail "unsafe existing systemd unit"
    fi
fi
if [ -e "$RUNTIME_FILE" ] || [ -L "$RUNTIME_FILE" ]; then
    if [ -L "$RUNTIME_FILE" ] || [ ! -f "$RUNTIME_FILE" ]; then
        fail "unsafe existing runtime.env"
    fi
fi
if [ -e "$GATEWAY_ENV_FILE" ] || [ -L "$GATEWAY_ENV_FILE" ]; then
    validate_gateway_environment
fi

SOURCE_CACHE_DIR=
if [ -f "$SERVICE_FILE" ]; then
    LEGACY_USER=$(/usr/bin/awk -F= '
        $1 == "User" { count += 1; value = $2 }
        END { if (count == 1) print value }
    ' "$SERVICE_FILE")
    case "$LEGACY_USER" in
        ''|"$SERVICE_USER"|*[!A-Za-z0-9_.-]*) ;;
        *)
            LEGACY_ENTRY=$(get_entry passwd "$LEGACY_USER")
            reject_multiline_entry "$LEGACY_ENTRY"
            if [ -n "$LEGACY_ENTRY" ] && [ "$(entry_name "$LEGACY_ENTRY")" = "$LEGACY_USER" ]; then
                LEGACY_HOME=$(passwd_home "$LEGACY_ENTRY")
                validate_absolute_path "legacy user home" "$LEGACY_HOME"
                LEGACY_CACHE=$LEGACY_HOME/.cloakbrowser
                if [ -e "$LEGACY_CACHE" ] || [ -L "$LEGACY_CACHE" ]; then
                    if [ -L "$LEGACY_CACHE" ] || [ ! -d "$LEGACY_CACHE" ]; then
                        fail "unsafe legacy cache directory"
                    fi
                    LEGACY_CACHE_METADATA=$(stat -c '%a:%u:%g' -- "$LEGACY_CACHE" 2>/dev/null) || {
                        fail "unsafe legacy cache directory"
                    }
                    LEGACY_CACHE_MODE=${LEGACY_CACHE_METADATA%%:*}
                    LEGACY_CACHE_OWNER=${LEGACY_CACHE_METADATA#*:}
                    [ "$LEGACY_CACHE_OWNER" = "$(entry_id "$LEGACY_ENTRY"):$(passwd_gid "$LEGACY_ENTRY")" ] || {
                        fail "unsafe legacy cache directory"
                    }
                    [ $((0$LEGACY_CACHE_MODE & 0022)) -eq 0 ] || {
                        fail "unsafe legacy cache directory"
                    }
                    SOURCE_CACHE_DIR=$LEGACY_CACHE
                fi
            fi
            ;;
    esac
fi

TEMPLATE_DIR=$(mktemp -d "$TMPDIR/llm-gateway-verify.XXXXXX") || {
    fail "cannot create verification directory"
}
chmod 0700 "$TEMPLATE_DIR"
INVENTORY_REPORT_FILE=$TEMPLATE_DIR/inventory.json
run_migration_command inventory >"$INVENTORY_REPORT_FILE" || {
    fail "migration inventory failed"
}
MIGRATION_STATE=$(parse_migration_required "$INVENTORY_REPORT_FILE") || {
    fail "migration inventory returned an invalid report"
}
if [ "$MIGRATION_STATE" = true ]; then
    MIGRATION_REQUIRED=1
else
    MIGRATION_REQUIRED=0
    validate_gateway_environment
fi

EXPECTED_RUNTIME_FILE=$TEMPLATE_DIR/runtime.env
EXPECTED_UNIT_FILE=$TEMPLATE_DIR/$SERVICE_NAME
render_runtime >"$EXPECTED_RUNTIME_FILE"
render_unit >"$EXPECTED_UNIT_FILE"
"$SYSTEMD_ANALYZE_BIN" verify "$EXPECTED_UNIT_FILE" >/dev/null 2>&1 || {
    fail "systemd unit verification failed"
}

RUNTIME_CHANGED=1
if [ -f "$RUNTIME_FILE" ] && cmp -s "$EXPECTED_RUNTIME_FILE" "$RUNTIME_FILE"; then
    RUNTIME_METADATA=$(stat -c '%a:%u:%g' -- "$RUNTIME_FILE" 2>/dev/null) || :
    [ "$RUNTIME_METADATA" != "640:$ENV_UID:$SERVICE_GID" ] || RUNTIME_CHANGED=0
fi
UNIT_CHANGED=1
if [ -f "$SERVICE_FILE" ] && cmp -s "$EXPECTED_UNIT_FILE" "$SERVICE_FILE"; then
    UNIT_METADATA=$(stat -c '%a:%u:%g' -- "$SERVICE_FILE" 2>/dev/null) || :
    [ "$UNIT_METADATA" != "644:$ENV_UID:$ENV_GID" ] || UNIT_CHANGED=0
fi

SERVICE_ACTIVE=0
"$SYSTEMCTL_BIN" is-active --quiet "$SERVICE_NAME" >/dev/null 2>&1 && {
    SERVICE_ACTIVE=1
}
SERVICE_WAS_ACTIVE=$SERVICE_ACTIVE
SERVICE_ENABLED=0
"$SYSTEMCTL_BIN" is-enabled --quiet "$SERVICE_NAME" >/dev/null 2>&1 && {
    SERVICE_ENABLED=1
}

if [ "$MIGRATION_REQUIRED" -eq 0 ] \
    && [ "$RUNTIME_CHANGED" -eq 0 ] \
    && [ "$UNIT_CHANGED" -eq 0 ] \
    && [ "$DIRECTORY_CHANGED" -eq 0 ] \
    && [ "$GROUP_NEEDS_CREATE" -eq 0 ] \
    && [ "$USER_NEEDS_CREATE" -eq 0 ]; then
    MUTATION_REQUIRED=0
else
    MUTATION_REQUIRED=1
fi

if [ "$MUTATION_REQUIRED" -eq 1 ]; then
    [ "$GROUP_NEEDS_CREATE" -eq 0 ] || {
        "$GROUPADD_BIN" --system --gid "$SERVICE_GID" "$SERVICE_GROUP" || {
            fail "service group creation failed"
        }
    }
    [ "$USER_NEEDS_CREATE" -eq 0 ] || {
        "$USERADD_BIN" --system --uid "$SERVICE_UID" --gid "$SERVICE_GROUP" \
            --home-dir "$STATE_DIR" --no-create-home --shell /usr/sbin/nologin \
            "$SERVICE_USER" || {
                fail "service account creation incomplete; rerun installer"
            }
    }

    ensure_directory "$ENV_DIR" 0750 "$ENV_UID" "$SERVICE_GID"
    ensure_directory "$STATE_DIR" 0750 "$SERVICE_UID" "$SERVICE_GID"
    ensure_directory "$LOG_DIR" 0750 "$SERVICE_UID" "$SERVICE_GID"
    ensure_directory "$CACHE_DIR" 0750 "$SERVICE_UID" "$SERVICE_GID"

    if [ "$RUNTIME_CHANGED" -eq 1 ] && [ -f "$RUNTIME_FILE" ]; then
        RUNTIME_EXISTED=1
        RUNTIME_ORIGINAL_METADATA=$(stat -c '%a:%u:%g' -- "$RUNTIME_FILE")
        backup_file "$RUNTIME_FILE"
        RUNTIME_BACKUP=$CREATED_BACKUP
    fi
    if [ "$UNIT_CHANGED" -eq 1 ] && [ -f "$SERVICE_FILE" ]; then
        UNIT_EXISTED=1
        UNIT_ORIGINAL_METADATA=$(stat -c '%a:%u:%g' -- "$SERVICE_FILE")
        backup_file "$SERVICE_FILE"
        UNIT_BACKUP=$CREATED_BACKUP
    fi
    ROLLBACK_ARMED=1

    [ "$MIGRATION_REQUIRED" -eq 0 ] || [ "$SERVICE_ACTIVE" -eq 0 ] || {
        "$SYSTEMCTL_BIN" stop "$SERVICE_NAME"
        SERVICE_ACTIVE=0
    }

    if [ "$MIGRATION_REQUIRED" -eq 1 ]; then
        MIGRATE_REPORT_FILE=$TEMPLATE_DIR/migrate.json
        run_migration_command migrate >"$MIGRATE_REPORT_FILE" || fail "migration failed"
        MIGRATE_STATE=$(parse_migration_required "$MIGRATE_REPORT_FILE") || {
            fail "migration returned an invalid report"
        }
        [ "$MIGRATE_STATE" = false ] || {
            fail "migration returned a non-converged report"
        }
    fi
    validate_gateway_environment

    if [ "$RUNTIME_CHANGED" -eq 1 ]; then
        RUNTIME_PUBLISHED=1
        atomic_install "$EXPECTED_RUNTIME_FILE" "$RUNTIME_FILE" 0640 "$ENV_UID" "$SERVICE_GID"
    fi
    if [ "$UNIT_CHANGED" -eq 1 ]; then
        UNIT_PUBLISHED=1
        atomic_install "$EXPECTED_UNIT_FILE" "$SERVICE_FILE" 0644 "$ENV_UID" "$ENV_GID"
        "$SYSTEMCTL_BIN" daemon-reload
    fi
fi

if [ "$SERVICE_ENABLED" -eq 0 ]; then
    ROLLBACK_ARMED=1
    "$SYSTEMCTL_BIN" enable "$SERVICE_NAME"
    NEWLY_ENABLED=1
fi
if [ "$MUTATION_REQUIRED" -eq 1 ] && [ "$SERVICE_WAS_ACTIVE" -eq 1 ] && [ "$MIGRATION_REQUIRED" -eq 0 ]; then
    "$SYSTEMCTL_BIN" restart "$SERVICE_NAME"
elif [ "$MUTATION_REQUIRED" -eq 1 ] || [ "$SERVICE_ACTIVE" -eq 0 ]; then
    ROLLBACK_ARMED=1
    "$SYSTEMCTL_BIN" start "$SERVICE_NAME"
fi
"$SYSTEMCTL_BIN" is-enabled --quiet "$SERVICE_NAME" >/dev/null 2>&1 || {
    fail "service is not enabled"
}
"$SYSTEMCTL_BIN" is-active --quiet "$SERVICE_NAME" >/dev/null 2>&1 || {
    fail "service is not active"
}

ROLLBACK_ARMED=0
printf 'Installed and started %s\n' "$SERVICE_NAME"
