#!/bin/bash
set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "container-entrypoint: reason=missing-command" >&2
    exit 64
fi

if ! command -v -- "$1" >/dev/null 2>&1; then
    echo "container-entrypoint: reason=command-not-executable" >&2
    exit 126
fi

python -m llm_gateway_core.services.single_process --check-environment
python -m llm_gateway_core.config.container_preflight

# The outputs volume is initialized by the dedicated one-shot Compose service.
# Runtime startup performs its own write/rename/delete probe.
mkdir -p /app/logs /app/db

# Print a message about where the logs will be
echo "LLM Gateway starting..."
echo "Container logs will be available in the container only and not persisted to host."
echo "Container database will be persisted to host via volume mount."
echo "Generated images will be persisted in the project-scoped outputs volume."

exec python -m llm_gateway_core.services.container_exec "$@"
