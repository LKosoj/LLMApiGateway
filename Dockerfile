# syntax=docker.io/docker/dockerfile:1.7-labs@sha256:b99fecfe00268a8b556fad7d9c37ee25d716ae08a5d7320e6d51c4dd83246894

# ====================================================
# LLM Gateway Dockerfile
# Hermetic multi-stage Python 3.12 build
# ====================================================

# ============= VERSION CONTRACT STAGE =============
FROM python:3.12-slim@sha256:46cb7cc2877e60fbd5e21a9ae6115c30ace7a077b9f8772da879e4590c18c2e3 AS version-contract

ARG LLMGATEWAY_EXPECTED_PRODUCT_VERSION

WORKDIR /version-contract

COPY llm_gateway_core/__init__.py llm_gateway_core/version.py ./llm_gateway_core/
COPY scripts/check_product_version.py ./scripts/check_product_version.py

RUN python scripts/check_product_version.py --expected "${LLMGATEWAY_EXPECTED_PRODUCT_VERSION}"

# ============= DEPENDENCY STAGE =============
FROM version-contract AS builder

WORKDIR /build

COPY requirements-container.txt ./requirements-container.txt

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python - <<'PY'
from pathlib import Path

lines = Path("requirements-container.txt").read_text(encoding="utf-8").splitlines()
starts = [index for index, line in enumerate(lines) if line.startswith("setuptools==")]
assert len(starts) == 1
start = starts[0]
end = next(
    index
    for index in range(start, len(lines))
    if not lines[index].rstrip().endswith("\\")
)
Path("/tmp/requirements-build-tools.txt").write_text(
    "\n".join(lines[start : end + 1]) + "\n",
    encoding="utf-8",
)
PY

RUN python -m pip install --no-cache-dir \
        --require-hashes \
        --only-binary=:all: \
        --requirement /tmp/requirements-build-tools.txt && \
    python -m pip install --no-cache-dir \
        --require-hashes \
        --no-build-isolation \
        --only-binary=:all: \
        --no-binary=docopt,langdetect,sgmllib3k \
        --requirement requirements-container.txt && \
    python -m pip check && \
    python -c "import importlib.metadata as m; import gpt_researcher; assert m.version('cloakbrowser') == '0.3.28'" && \
    rm -f /tmp/requirements-build-tools.txt && \
    find /opt/venv -type d -name __pycache__ -prune -exec rm -rf {} + && \
    find /opt/venv -type f -name '*.py[co]' -delete && \
    find /opt/venv -type d -exec chmod 0555 {} + && \
    find /opt/venv -type f -exec chmod a-w {} +

# ============= BROWSER ASSET STAGE =============
FROM python:3.12-slim@sha256:46cb7cc2877e60fbd5e21a9ae6115c30ace7a077b9f8772da879e4590c18c2e3 AS browser-assets

ARG TARGETARCH

COPY docker/install_cloakbrowser_asset.py /usr/local/bin/install-cloakbrowser-asset

RUN --mount=type=cache,target=/var/cache/cloakbrowser \
    python /usr/local/bin/install-cloakbrowser-asset \
        --arch "${TARGETARCH}" \
        --archive-cache /var/cache/cloakbrowser \
        --destination /opt/cloakbrowser/chromium-146.0.7680.177.3

# ============= RUNTIME STAGE =============
FROM python:3.12-slim@sha256:46cb7cc2877e60fbd5e21a9ae6115c30ace7a077b9f8772da879e4590c18c2e3 AS runtime

ARG LLMGATEWAY_EXPECTED_PRODUCT_VERSION
ARG LLMGATEWAY_BUILD_SHA=
ARG LLMGATEWAY_BUILD_DATE=
ARG LLMGATEWAY_BUILD_REF=

LABEL description="Fault-Tolerant Personal LLM Gateway with advanced fallback support" \
      org.opencontainers.image.version="${LLMGATEWAY_EXPECTED_PRODUCT_VERSION}" \
      org.opencontainers.image.revision="${LLMGATEWAY_BUILD_SHA}" \
      org.opencontainers.image.created="${LLMGATEWAY_BUILD_DATE}" \
      org.opencontainers.image.ref.name="${LLMGATEWAY_BUILD_REF}"

# The base digest was produced from this Debian snapshot. Keep package selection
# on the same immutable snapshot instead of following mutable distribution mirrors.
RUN printf '%s\n' \
        'Types: deb' \
        'URIs: https://snapshot.debian.org/archive/debian/20260421T000000Z/' \
        'Suites: trixie trixie-updates' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
        'Check-Valid-Until: no' \
        '' \
        'Types: deb' \
        'URIs: https://snapshot.debian.org/archive/debian-security/20260421T000000Z/' \
        'Suites: trixie-security' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
        'Check-Valid-Until: no' \
        > /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
        libxcb1 libxext6 libxshmfence1 libglib2.0-0 libgtk-3-0 \
        libpangocairo-1.0-0 libcairo-gobject2 libgdk-pixbuf-2.0-0 \
        libxss1 libxtst6 fonts-liberation fonts-noto-color-emoji \
        fonts-unifont fonts-freefont-ttf fonts-ipafont-gothic \
        fonts-wqy-zenhei fonts-tlwg-loma-otf && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 llmgateway && \
    useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin llmgateway

WORKDIR /app

COPY --from=builder --chown=0:0 /opt/venv /opt/venv
COPY --from=browser-assets --chown=0:0 /opt/cloakbrowser /opt/cloakbrowser

COPY --chown=0:0 main.py /app/main.py
COPY --chown=0:0 llm_gateway_core/ /app/llm_gateway_core/
COPY --chown=0:0 static/ /app/static/
COPY --chown=0:0 examples/free-tier-providers.md /app/examples/free-tier-providers.md
COPY --chown=0:0 --chmod=0555 docker/healthcheck.py /app/healthcheck.py
COPY --chown=0:0 --chmod=0555 docker/entrypoint.sh /app/entrypoint.sh

RUN install -d -o 10001 -g 10001 -m 0770 \
        /app/config /app/db /app/logs /app/outputs /app/outputs/images && \
    chmod 0444 /app/main.py /app/examples/free-tier-providers.md && \
    find /app/llm_gateway_core /app/static -type d -exec chmod 0555 {} + && \
    find /app/llm_gateway_core /app/static -type f -exec chmod 0444 {} +

ENV PATH="/opt/venv/bin:$PATH" \
    GATEWAY_PORT=9000 \
    GATEWAY_HOST="0.0.0.0" \
    GATEWAY_WORKERS=1 \
    GATEWAY_OUTPUTS_DIR="/app/outputs" \
    APP_DIR="/app" \
    HOME="/tmp" \
    LOG_FILE_LIMIT=15 \
    LOG_CHAT_ENABLED=false \
    FALLBACK_PROVIDER=openrouter \
    LLMGATEWAY_CONFIG_DIR="/app/config" \
    PROVIDERS_FILENAME="/app/config/providers.json" \
    FALLBACK_RULES_FILENAME="/app/config/models_fallback_rules.json" \
    OPERATION_RULES_FILENAME="/app/config/models_operation_rules.json" \
    FUSION_RULES_FILENAME="/app/config/models_fusion_rules.json" \
    MODEL_RULES_FILENAME="/app/config/models_model_rules.json" \
    ROUTER_RULES_FILENAME="/app/config/models_router_rules.json" \
    CLOAKBROWSER_BINARY_PATH="/opt/cloakbrowser/chromium-146.0.7680.177.3/chrome" \
    CLOAKBROWSER_CACHE_DIR="/opt/cloakbrowser" \
    CLOAKBROWSER_AUTO_UPDATE=false \
    LLMGATEWAY_BUILD_SHA="${LLMGATEWAY_BUILD_SHA}" \
    LLMGATEWAY_BUILD_DATE="${LLMGATEWAY_BUILD_DATE}" \
    LLMGATEWAY_BUILD_REF="${LLMGATEWAY_BUILD_REF}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 9000

USER 10001:10001

# Validate the exact wrapper/engine contract and a real offline headless launch.
RUN --mount=type=tmpfs,target=/tmp \
    python -c "import importlib.metadata as m; from cloakbrowser import launch; from cloakbrowser.config import get_chromium_version; assert m.version('cloakbrowser') == '0.3.28'; assert get_chromium_version() == '146.0.7680.177.3'; browser = launch(); page = browser.new_page(); assert page.url == 'about:blank'; assert page.evaluate('1 + 1') == 2; page.evaluate(\"document.title = 'ok'\"); assert page.title() == 'ok'; browser.close()"

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python /app/healthcheck.py || exit 1
