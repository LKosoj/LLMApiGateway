#!/bin/bash
set -e

# Print a message about where the logs will be
echo "LLM Gateway starting..."
echo "Container logs will be available in the container only and not persisted to host."
echo "Container database will be persisted to host via volume mount."

# Check if providers.json exists
if [ ! -f "/app/providers.json" ]; then
    echo "ERROR: providers.json not found. Please mount this file as a volume."
    echo "Example: -v ./providers.json:/app/providers.json"
    exit 1
fi

# Create logs directory if it doesn't exist
mkdir -p /app/logs

# Create directory for database if it doesn't exist
mkdir -p /app/db

# If models_fallback_rules.json doesn't exist, use the template or create a default one
if [ ! -f "/app/models_fallback_rules.json" ]; then
    if [ -f "/app/docker/models_fallback_rules.json.template" ]; then
        echo "models_fallback_rules.json not found, copying from template."
        cp /app/docker/models_fallback_rules.json.template /app/models_fallback_rules.json
    else
        echo "models_fallback_rules.json not found and template not available, creating with default content."
        echo '[
    {
        "gateway_model_name": "llmgateway/default",
        "rotate_models": false,
        "fallback_models": [
            {
                "provider": "'${FALLBACK_PROVIDER:-openrouter}'",
                "model": "openai/gpt-3.5-turbo",
                "retry_delay": 15,
                "retry_count": 3
            }
        ]
    }
]' > /app/models_fallback_rules.json
    fi
fi

# If models_operation_rules.json doesn't exist, create an empty operation rules file.
if [ ! -f "/app/models_operation_rules.json" ]; then
    echo "models_operation_rules.json not found, creating empty operation rules."
    echo '{}' > /app/models_operation_rules.json
fi

# If models_fusion_rules.json doesn't exist, create empty Fusion rules.
if [ ! -f "/app/models_fusion_rules.json" ]; then
    echo "models_fusion_rules.json not found, creating empty Fusion rules."
    echo '[]' > /app/models_fusion_rules.json
fi

# If models_router_rules.json doesn't exist, create empty router rules
if [ ! -f "/app/models_router_rules.json" ]; then
    echo "models_router_rules.json not found, creating empty router rules."
    echo '[]' > /app/models_router_rules.json
fi

# If models_model_rules.json doesn't exist, create an empty model policy file
if [ ! -f "/app/models_model_rules.json" ]; then
    echo "models_model_rules.json not found, creating empty model policy."
    echo '{}' > /app/models_model_rules.json
fi

# Print some useful information
echo "Gateway configured to listen on ${GATEWAY_HOST:-0.0.0.0}:${GATEWAY_PORT:-9000}"
echo "Default fallback provider: ${FALLBACK_PROVIDER:-openrouter}"
echo "Log chat enabled: ${LOG_CHAT_ENABLED:-false}"

exec "$@"
