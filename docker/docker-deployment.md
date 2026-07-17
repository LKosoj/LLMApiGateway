# Docker Deployment Guide for LLM Gateway

This guide explains how to deploy the LLM Gateway using Docker.

## Prerequisites

- Docker installed on your system
- Docker Compose installed (optional, for easier deployment)
- Basic understanding of Docker concepts
- API keys for at least one LLM provider

## Quick Start

The fastest way to get started is using Docker Compose:

```bash
# 1. Edit the root configuration sources before initialization
# providers.json and models_fallback_rules.json are mandatory
nano providers.json models_fallback_rules.json

# 2. Prepare directories for the fixed container UID/GID 10001:10001
sudo install -d -o 10001 -g 10001 -m 0750 data/db
sudo python3 scripts/init_docker_config.py --source-dir . --target-dir ./config

# 3. Edit the docker-compose.yml file to set your API keys
# (you need to customize this file)
nano docker-compose.yml

# 4. Derive the required build contract from the canonical source and start
export LLMGATEWAY_EXPECTED_PRODUCT_VERSION="$(python3 scripts/check_product_version.py --print)"
docker compose up -d --build
```

Both `providers.json` and `models_fallback_rules.json` are mandatory before
startup. If a root Operations, Fusion, Router, or Model Rules file exists, the
initializer copies it byte-for-byte. The container atomically materializes
missing Operations/Model Rules as `{}` and missing Fusion/Router Rules as `[]`.
It never generates a fallback route. The initialized `config` directory is
owned by `10001:10001`; after startup, use `/v1/ui/rules-editor` for further edits
instead of an unprivileged host editor.

## Host systemd alternative

The production installer runs directly from one canonical trusted deployment
checkout. Its project directory, `.venv`, `main.py`, and deployment helpers must
be root-owned and not writable by the service identity; a shared group-writable
development checkout is rejected. Prepare `.venv`, a local ignored `.env`, and
the mandatory `providers.json` and `models_fallback_rules.json` before
installation. Keep the source `.env` private because migration preserves it:

```bash
chmod 0600 .env
```

Run the deterministic read-only inventory before the installer:

```bash
sudo "$PWD/.venv/bin/python" "$PWD/docker/systemd_migration.py" inventory \
  --source-root "$PWD" \
  --target-env-dir /etc/llm-gateway \
  --target-state-dir /var/lib/llm-gateway \
  --target-cache-dir /var/cache/llm-gateway
```

The JSON report contains variable names, basenames, counts, hashes, and status
codes, but never environment values or file contents. It performs no migration.
Duplicate or reserved environment keys, syntax outside the common dotenv/systemd
subset, unknown state files, orphan SQLite sidecars, unsafe targets, and a
missing source/target environment or mandatory Providers/Fallback fail closed.
Resolve every reported error explicitly; do not delete or rename unknown state
through the installer. If a legacy CloakBrowser cache has been deliberately
identified, add its absolute `--source-cache-dir`; the installer itself derives
that path only from one valid legacy unit `User` home.

Then run the mutating installer:

```bash
sudo sh docker/setup-gateway-service.sh
```

This command changes the host. It may create the fixed
`llmgateway:llmgateway` system account with UID/GID `10001:10001`, create FHS
directories, stop an active legacy service when migration is required, migrate
data, install/enable the unit, and start it. Production overrides for paths,
identity, service name, and command locations are rejected; `--test-mode` is an
internal hermetic-test seam, not a deployment option.

The canonical layout is:

| Path | Owner | Mode | Purpose |
|---|---|---:|---|
| `/etc/llm-gateway` | `root:llmgateway` | `0750` | Environment root |
| `/etc/llm-gateway/gateway.env` | `root:llmgateway` | `0640` | User secrets and settings migrated from `.env` |
| `/etc/llm-gateway/runtime.env` | `root:llmgateway` | `0640` | Installer-generated runtime paths |
| `/var/lib/llm-gateway` | `llmgateway:llmgateway` | `0750` | Configs, SQLite state, outputs, migration manifests |
| `/var/log/llm-gateway` | `llmgateway:llmgateway` | `0750` | Gateway and chat logs |
| `/var/cache/llm-gateway` | `llmgateway:llmgateway` | `0750` | CloakBrowser cache |
| `/etc/systemd/system/llm-gateway.service` | `root:root` | `0644` | Canonical unit |

Mutable child directories are normalized to `0770` and files to `0660`.
`runtime.env` sets `APP_DIR`, `LLMGATEWAY_ENV_FILE`, `PYTHONUNBUFFERED`,
`PYTHONDONTWRITEBYTECODE`, `GATEWAY_WORKERS=1`, `GATEWAY_DB_DIR`,
`GATEWAY_OUTPUTS_DIR`, `LLMGATEWAY_LOG_DIR`, `CLOAKBROWSER_CACHE_DIR`,
`LLMGATEWAY_CONFIG_DIR`, and the Providers/Fallback/Operations/Fusion/Model/Router
filename variables. Do not put these installer-owned keys in `.env` or
`gateway.env`.

Migration copies only absent known artifacts. Safe existing targets are
authoritative and are never overwritten. Known SQLite databases are copied by
the offline SQLite backup API, including committed WAL content; generated
images delegate to the verified image-storage migration and retain a manifest;
the legacy CloakBrowser cache is staged and verified with its own manifest.
Legacy logs are inventory-only. Lock/temp entries are skipped explicitly, and
unknown state blocks the entire migration. No source file or directory is
deleted automatically.

Before installing a changed unit, the script renders a temporary candidate and
runs `/usr/bin/systemd-analyze verify`. Replaced unit/runtime files receive
adjacent mode-`0600` backups and are atomically published. A failure restores
those files, disables only an enable performed by that run, and leaves the
service stopped. The hardened unit loads `gateway.env` before `runtime.env`,
uses only the state/log/cache paths above, and runs a bounded `ExecStartPost`
probe: one local `HEAD /health` attempt per second for up to 60 seconds under
`TimeoutStartSec=75`. It never probes an LLM provider. A fully converged rerun
with the service already active and enabled does not rewrite files,
stop/restart the service, or issue another mutating systemctl command.

Repository tests exercise this flow only with temporary FHS roots, fake
systemctl/account tools, and hermetic readiness/migration fixtures. They do not
run the production installer against or mutate a live systemd deployment.

## Manual Docker Deployment

If you prefer to use Docker CLI directly:

```bash
# 1. Edit the mandatory root provider and fallback configurations
nano providers.json models_fallback_rules.json

# 2. Prepare directories for the fixed container UID/GID 10001:10001
sudo install -d -o 10001 -g 10001 -m 0750 data/db
sudo python3 scripts/init_docker_config.py --source-dir . --target-dir ./config

# 3. Build the image with the canonical product-version contract
LLMGATEWAY_EXPECTED_PRODUCT_VERSION="$(python3 scripts/check_product_version.py --print)"
docker build \
  --build-arg LLMGATEWAY_EXPECTED_PRODUCT_VERSION="${LLMGATEWAY_EXPECTED_PRODUCT_VERSION}" \
  -t llm-gateway:latest .

# 4. Create and initialize persistent generated-image storage
docker volume create llm-gateway-outputs
docker run --rm \
  --user 0:0 \
  --network none \
  --read-only \
  --cap-drop ALL \
  --cap-add CHOWN \
  --cap-add DAC_OVERRIDE \
  --cap-add FOWNER \
  -v llm-gateway-outputs:/app/outputs \
  --entrypoint python \
  llm-gateway:latest \
  -m llm_gateway_core.services.image_storage_cli \
  init-volume --outputs-dir /app/outputs

# 5. Run the container
docker run -d \
  --name llm-gateway \
  -p 9000:9000 \
  --read-only \
  --tmpfs /app/logs:rw,mode=0770,uid=10001,gid=10001 \
  --tmpfs /tmp:rw,mode=1777 \
  -v "$(pwd)/config:/app/config" \
  -v "$(pwd)/data/db:/app/db" \
  -v llm-gateway-outputs:/app/outputs \
  -e GATEWAY_API_KEY=your-secure-api-key \
  -e APIKEY_OPENROUTER=your-openrouter-key \
  -e APIKEY_OPENAI=your-openai-key \
  -e LOG_CHAT_ENABLED=false \
  -e FALLBACK_PROVIDER=openrouter \
  llm-gateway:latest
```

CI checks the pinned arm64 CloakBrowser archive checksum, extraction safety,
and ELF architecture metadata without emulation. Its real headless browser
launch smoke runs on amd64 only, so arm64 is not execution-tested by CI.

## Configuration

### Required Environment Variables

- `GATEWAY_API_KEY`: API key for accessing the gateway
- At least one provider API key (e.g., `APIKEY_OPENROUTER`)
- `LLMGATEWAY_EXPECTED_PRODUCT_VERSION`: required by Compose at build time;
  derive it with `python3 scripts/check_product_version.py --print`. It is not
  passed into the running container.

### Optional Environment Variables

- `APP_DIR`: Absolute application root for relative configuration filenames
  (image default: `/app`; empty and relative values are rejected)
- `GATEWAY_OUTPUTS_DIR`: Absolute generated-output root (image default:
  `/app/outputs`; empty and relative values are rejected)
- `GATEWAY_PORT`: Port to run the gateway (default: 9000)
- `LOG_FILE_LIMIT`: Maximum log files to keep (default: 15)
- `LOG_CHAT_ENABLED`: Enable chat logging (default: false)
- `FALLBACK_PROVIDER`: Default fallback provider (default: openrouter)

### Provider API Keys

Set any of these environment variables for the providers you want to use:

- `APIKEY_OPENROUTER`: OpenRouter API key
- `APIKEY_OPENAI`: OpenAI API key
- `APIKEY_GOOGLE`: Google API key
- `APIKEY_NEBIUS`: Nebius API key
- `APIKEY_TOGETHER`: Together API key
- `APIKEY_KLUSTERAI`: KlusterAI API key
- `APIKEY_REQUESTY`: Requesty API key
- `APIKEY_XAI`: xAI API key

## Volume Mounts

### Required Mounts

1. **Configuration directory**: `-v ./config:/app/config`

   - Contains mandatory `providers.json` and `models_fallback_rules.json`, plus
     materialized or custom Operations, Fusion, Router, and Model Rules files
   - Must be read-write and searchable by container UID/GID `10001:10001`
   - A single directory is required for atomic same-directory replacement;
     individual file bind mounts are unsupported
   - Prepare it with
     `sudo python3 scripts/init_docker_config.py --source-dir . --target-dir ./config`

2. **Database**: `-v ./data/db:/app/db`
   - Persists SQLite database for model rotation state
   - Must be prepared for UID/GID `10001:10001` with
     `sudo install -d -o 10001 -g 10001 -m 0750 data/db`

3. **Generated images**: `gateway_outputs:/app/outputs` in Compose, or
   `llm-gateway-outputs:/app/outputs` in the manual example
   - Persists authenticated `/outputs/images/...` URLs across restart,
     force-recreate, and ordinary `down`/`up`
   - The one-shot root initializer applies mode `0770` and UID/GID
     `10001:10001`; the gateway itself remains non-root
   - Compose scopes the volume by project because no global `name:` or
     `container_name` is configured

The root filesystem is read-only. Logs and `/tmp` use tmpfs. Configuration,
database state, and the generated-image volume are the persistent writable
paths.

## Generated Image Storage

The image writer, authenticated StaticFiles mount, startup write/rename/delete
probe, and retention service all use `<GATEWAY_OUTPUTS_DIR>/images`. Local PNGs
are published with a same-directory temporary file, `fsync`, and atomic
replace. Retention removes only known stale final/temporary images and empty
research directories after 10 days; failures make `/health` return `503`
without exposing paths or filenames.

> **Warning:** `docker compose down -v` permanently deletes the project
> generated-image volume. A named volume is not a backup.

All migration, backup, and restore commands below require the gateway to be
stopped. They produce or consume a sorted manifest containing relative path,
file count, SHA-256, and `mtime_ns`. Any mismatch fails closed; migration never
deletes its source, and restore accepts only an empty/new volume.

Prepare a private host directory for manifests and archives:

```bash
sudo install -d -o 0 -g 0 -m 0700 data/generated-images-backups
docker compose stop llm-gateway
```

Migrate an existing checkout `./outputs/images` directory:

```bash
docker compose run --rm --no-deps --user 0:0 \
  --volume "$PWD/outputs/images:/migration-source:ro" \
  --volume "$PWD/data/generated-images-backups:/backup" \
  --entrypoint python \
  outputs-init \
  -m llm_gateway_core.services.image_storage_cli migrate \
  --source-images /migration-source \
  --outputs-dir /app/outputs \
  --manifest /backup/legacy-images.manifest.json
```

For an old container layer, stop that container and stage the files first:

```bash
sudo rm -rf data/generated-images-staging
sudo install -d -o 0 -g 0 -m 0700 data/generated-images-staging
docker stop old-llm-gateway
docker cp old-llm-gateway:/app/outputs/images/. data/generated-images-staging/
# Use data/generated-images-staging as /migration-source in the migrate command.
```

Create a verified backup:

```bash
backup_id="generated-images-$(date -u +%Y%m%dT%H%M%SZ)-$$"
printf 'backup_id=%s\n' "${backup_id}"
docker compose run --rm --no-deps --user 0:0 \
  --volume "$PWD/data/generated-images-backups:/backup" \
  --entrypoint python \
  outputs-init \
  -m llm_gateway_core.services.image_storage_cli backup \
  --outputs-dir /app/outputs \
  --archive "/backup/${backup_id}.tar" \
  --manifest "/backup/${backup_id}.manifest.json"
```

Record `backup_id` with the backup. Each backup must use a fresh stem: the CLI
never overwrites an existing archive or manifest and fails closed with
`backup-artifact-exists`. After an interrupted backup, inspect or remove only
that incomplete stem, or choose a new one; never reuse a known-good pair.

Before touching the current volume, prove that the backup restores into a
separate empty volume. The restore command verifies count, SHA-256, and
`mtime_ns`; do not continue if it fails. In a new shell, first set `backup_id`
to the exact value recorded by the backup command:

```bash
restore_check_volume="llm-gateway-restore-check-$(date +%s)"
docker volume create "${restore_check_volume}"
docker run --rm --user 0:0 --network none --read-only \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
  --volume "${restore_check_volume}:/app/outputs" \
  --entrypoint python \
  llm-gateway:latest \
  -m llm_gateway_core.services.image_storage_cli init-volume \
  --outputs-dir /app/outputs
docker run --rm --user 0:0 --network none --read-only \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
  --volume "${restore_check_volume}:/app/outputs" \
  --volume "$PWD/data/generated-images-backups:/backup:ro" \
  --entrypoint python \
  llm-gateway:latest \
  -m llm_gateway_core.services.image_storage_cli restore \
  --outputs-dir /app/outputs \
  --archive "/backup/${backup_id}.tar" \
  --manifest "/backup/${backup_id}.manifest.json"
docker volume rm "${restore_check_volume}"
```

Only after that independent restore succeeds, replace the Compose volume. The
following `down -v` is intentionally destructive:

```bash
docker compose down -v
docker compose run --rm --no-deps outputs-init
docker compose run --rm --no-deps --user 0:0 \
  --volume "$PWD/data/generated-images-backups:/backup:ro" \
  --entrypoint python \
  outputs-init \
  -m llm_gateway_core.services.image_storage_cli restore \
  --outputs-dir /app/outputs \
  --archive "/backup/${backup_id}.tar" \
  --manifest "/backup/${backup_id}.manifest.json"
docker compose up -d llm-gateway
```

The CLI verifies the post-copy inventory before publication. Do not remove a
legacy source until the migration command succeeds and its reported
`tree_sha256`/`count` match the private manifest.

## Managing the Container

### View Logs

```bash
# Docker CLI
docker logs llm-gateway

# Docker Compose (after exporting LLMGATEWAY_EXPECTED_PRODUCT_VERSION)
docker compose logs llm-gateway
```

### Restart Container

```bash
# Docker CLI
docker restart llm-gateway

# Docker Compose (after exporting LLMGATEWAY_EXPECTED_PRODUCT_VERSION)
docker compose restart llm-gateway
```

### Stop Container

```bash
# Docker CLI
docker stop llm-gateway

# Docker Compose (after exporting LLMGATEWAY_EXPECTED_PRODUCT_VERSION)
docker compose stop llm-gateway
```

## Accessing the Gateway

Once the container is running, you can access:

- Web UI: `http://localhost:9000/v1/ui/rules-editor`
- API: `http://localhost:9000/v1/chat/completions`
- Readiness: `GET` or `HEAD http://localhost:9000/health`

The readiness check is local and never calls an LLM provider. `GET /health`
returns `200` only when the runtime, configuration, accounting/writer,
generated-image retention, and mandatory SQLite stores are ready; it returns
`503` before readiness or on failure. `HEAD /health` returns the same status
and headers without a body. The container probe performs one HEAD request and
leaves retry scheduling to Docker.

Remember to use the `GATEWAY_API_KEY` in your requests as:

```
Authorization: Bearer your-gateway-api-key
```

## Common Issues

### Container not starting

Check the logs for errors:

```bash
docker logs llm-gateway
```

### API calls failing

Verify that:

1. You've provided the correct API keys
2. Your `providers.json` file is correctly configured
3. Your `models_fallback_rules.json`, `models_operation_rules.json`, `models_router_rules.json`, and `models_model_rules.json` files reference existing providers and gateway models
4. You're including the `Authorization` header in your requests

### Database persistence issues

Make sure the volume mount for the database is correct and the directory has appropriate permissions:

```bash
ls -la data/db
```

## Upgrading

To upgrade to a newer version:

```bash
# Pull latest code and rebuild
git pull
export LLMGATEWAY_EXPECTED_PRODUCT_VERSION="$(python3 scripts/check_product_version.py --print)"
docker compose build

# Restart the service
docker compose down
docker compose up -d
```

## Production Considerations

For production deployments, consider:

1. Using a reverse proxy (e.g., Nginx) for SSL termination
2. Setting up proper monitoring and alerts
3. Implementing regular database backups
4. Using Docker secrets for sensitive API keys instead of environment variables
