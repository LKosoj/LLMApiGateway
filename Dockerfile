# syntax=docker.io/docker/dockerfile:1.7-labs

# ====================================================
# LLM Gateway Dockerfile
# Multi-stage build with Python 3.12
# ====================================================

# ============= BUILD STAGE =============
FROM python:3.12-slim AS builder

# Set working directory
WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files
COPY requirements.txt pyproject.toml ./

# Create virtual environment and install dependencies
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ============= RUNTIME STAGE =============
FROM python:3.12-slim AS runtime

ARG LLMGATEWAY_BUILD_VERSION=dev
ARG LLMGATEWAY_BUILD_SHA=
ARG LLMGATEWAY_BUILD_DATE=
ARG LLMGATEWAY_BUILD_REF=

# Set metadata
LABEL description="Fault-Tolerant Personal LLM Gateway with advanced fallback support" \
      org.opencontainers.image.version="${LLMGATEWAY_BUILD_VERSION}" \
      org.opencontainers.image.revision="${LLMGATEWAY_BUILD_SHA}" \
      org.opencontainers.image.created="${LLMGATEWAY_BUILD_DATE}" \
      org.opencontainers.image.ref.name="${LLMGATEWAY_BUILD_REF}"

# Create non-root user
RUN groupadd -r llmgateway && useradd -r -g llmgateway llmgateway

# Set working directory
WORKDIR /app

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create necessary directories
RUN mkdir -p /app/logs /app/db && \
    chown -R llmgateway:llmgateway /app/logs /app/db

# Copy application code
COPY . /app/
RUN rm -rf /app/docker && \
    rm -f /app/.env /app/providers.json /app/models_fallback_rules.json \
    /app/models_operation_rules.json /app/models_fusion_rules.json \
    /app/models_router_rules.json /app/models_model_rules.json

# Copy the healthcheck and entrypoint scripts
COPY docker/healthcheck.py /app/
COPY docker/entrypoint.sh /app/

# Set proper permissions
RUN chown -R llmgateway:llmgateway /app && \
    chmod -R 755 /app && \
    chmod +x /app/entrypoint.sh

# Set default environment variables
ENV GATEWAY_PORT=9000 \
    GATEWAY_HOST="0.0.0.0" \
    LOG_FILE_LIMIT=15 \
    LOG_CHAT_ENABLED=false \
    FALLBACK_PROVIDER=openrouter \
    LLMGATEWAY_BUILD_VERSION="${LLMGATEWAY_BUILD_VERSION}" \
    LLMGATEWAY_BUILD_SHA="${LLMGATEWAY_BUILD_SHA}" \
    LLMGATEWAY_BUILD_DATE="${LLMGATEWAY_BUILD_DATE}" \
    LLMGATEWAY_BUILD_REF="${LLMGATEWAY_BUILD_REF}" \
    PYTHONUNBUFFERED=1

# Expose the application port
EXPOSE 9000

# Switch to non-root user
USER llmgateway

# Set the entrypoint script
ENTRYPOINT ["/app/entrypoint.sh"]

# Default command
CMD ["python", "main.py"]

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python /app/healthcheck.py || exit 1
