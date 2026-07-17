#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"
exec "${PYTHON:-python3}" "${SCRIPT_DIR}/check_dependency_contract.py" \
    --root "${PROJECT_ROOT}" --compile
