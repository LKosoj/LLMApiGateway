# LLM Gateway Docker Implementation

This directory contains the Docker implementation for the LLM Gateway project.

## Quick Reference

- **Dockerfile**: Multi-stage build for production use
- **docker-compose.yml**: Easy deployment configuration
- **entrypoint.sh**: Container startup script
- **healthcheck.py**: Container health monitoring
- **install_cloakbrowser_asset.py**: Verified pinned browser asset installer
- **image_storage_cli.py**: Volume initialization and verified migration/backup/restore
- **systemd_migration.py**: Secret-safe host inventory and offline FHS migration
- **systemd_readiness.py**: Bounded systemd readiness gate on the single `/health` endpoint
- **setup-gateway-service.sh**: Convergent hardened host-systemd installer
- **restart-gateway-service.sh**: Restarts the installed unit and follows its journal
- **docker-deployment.md**: Comprehensive deployment guide

## Getting Started

1. Edit the root configuration sources before initialization. Both
   `providers.json` and `models_fallback_rules.json` are mandatory. Operations,
   Fusion, Router, and Model Rules are optional; the container materializes
   their canonical empty shapes when they are absent.
   ```bash
   nano providers.json models_fallback_rules.json
   ```

2. Prepare the host directories for the fixed container identity:
   ```bash
   sudo install -d -o 10001 -g 10001 -m 0750 data/db
   sudo python3 scripts/init_docker_config.py --source-dir . --target-dir ./config
   ```

   The initializer copies any existing root configuration files byte-for-byte,
   never overwrites a target, and creates the shared directory for the fixed
   container identity `10001:10001`. The initialized directory is intentionally
   owned by that identity; after startup, make further changes through
   `/v1/ui/rules-editor` rather than an unprivileged host editor.

3. Deploy using Docker Compose:
   ```bash
   export LLMGATEWAY_EXPECTED_PRODUCT_VERSION="$(python3 scripts/check_product_version.py --print)"
   docker compose up -d --build
   ```

   `docker-compose.yml` deliberately has no duplicated version literal. Set the
   variable from the canonical Python source before every Compose command in a
   new shell; missing or mismatched values fail the image build.

   CI verifies the pinned arm64 CloakBrowser archive by checksum, safe
   extraction, and ELF architecture metadata on its amd64 runner. The real
   headless browser launch smoke is amd64-only; CI does not emulate or execute
   the arm64 binary.

## Accessing the Gateway

- Web UI: http://localhost:9000/v1/ui/rules-editor
- API: http://localhost:9000/v1/chat/completions
- Readiness: `GET` or `HEAD http://localhost:9000/health`

`GET /health` returns a local readiness report. It returns `200` only when the
runtime, configuration, accounting/writer, generated-image retention, and
mandatory SQLite stores are ready; before readiness or on failure it returns
`503`. `HEAD /health` returns the same status and headers without a body.
Neither method calls an LLM provider. The container healthcheck makes one
`HEAD /health` request per Docker probe; Docker owns the retry schedule.

## Configuration

### Required Environment Variables

- `GATEWAY_API_KEY`: API key for accessing the gateway
- At least one provider API key (e.g., `APIKEY_OPENROUTER`)
- `LLMGATEWAY_EXPECTED_PRODUCT_VERSION`: build-time Compose contract; derive it
  with `python3 scripts/check_product_version.py --print`

The image sets `APP_DIR=/app` for relative configuration filenames and
`GATEWAY_OUTPUTS_DIR=/app/outputs` for generated files. Both manual overrides
must be non-empty absolute paths.

### Volume Mounts

1. **Configuration directory**: `-v ./config:/app/config`
2. **Database**: `-v ./data/db:/app/db`
3. **Generated images**: project-scoped `gateway_outputs:/app/outputs`

Compose keeps the image root filesystem read-only. `/app/logs` and `/tmp` are
tmpfs mounts. Generated images live in a project-scoped named volume; the
one-shot root `outputs-init` service sets mode `0770` and ownership
`10001:10001` before the non-root gateway starts. The volume survives restart,
force-recreate, and ordinary `docker compose down`/`up`.

> **Destructive command:** `docker compose down -v` permanently removes the
> generated-image volume. A named volume is not a backup.

Stop `llm-gateway` before migration, backup, or restore. The maintenance CLI
uses a sorted manifest with file count, SHA-256, and `mtime_ns`, never deletes a
legacy source, writes backup artifacts with mode `0600`, and restores only into
an empty/new volume. Every backup requires a fresh archive/manifest stem; the
CLI never overwrites either artifact. See [docker-deployment.md](docker-deployment.md#generated-image-storage)
for exact commands.

The configuration directory is mounted read-write on purpose. It always
contains mandatory `providers.json` and `models_fallback_rules.json`; the
entrypoint materializes only missing Operations/Model Rules as `{}` and
Fusion/Router Rules as `[]`. Keeping the files in one
filesystem directory lets the web editor at
`/v1/ui/rules-editor` publish changes with an atomic same-directory `rename`.
The entrypoint validates both mandatory files and application/config path,
write/search/rename support before other startup side effects. It exits
explicitly when the directory contract fails; an ambiguous
post-publication error is reported with `publication=uncertain` and the public
pathname is left untouched. The final command is handed PID 1 through a
secret-safe exec trampoline; execution errors emit a fixed reason without
printing command arguments.

## Documentation

For detailed deployment instructions, refer to [docker-deployment.md](docker-deployment.md).

## Host systemd alternative

For a non-container host deployment, run `sudo sh docker/setup-gateway-service.sh`
from a trusted root-owned deployment checkout. This is a mutating host command:
it creates the fixed `llmgateway:llmgateway` UID/GID `10001:10001` identity when
absent, migrates known data into `/etc/llm-gateway` and `/var/{lib,log,cache}/llm-gateway`,
installs the hardened unit, and starts it. Production paths, identity, service
name, and command locations cannot be overridden; `--test-mode` is only for
hermetic tests.

The installer first runs a deterministic inventory that never prints env
values. Duplicate/reserved env keys, unknown state, unsafe targets, or missing
source/target environment and mandatory Providers/Fallback fail before
mutation. Safe existing targets remain authoritative and sources are never
deleted. The unit loads `gateway.env` before generated `runtime.env`, pins one
worker, and uses a bounded `ExecStartPost` `HEAD /health` readiness gate. Unit
and runtime replacements are backed up and published atomically; a fully
converged rerun with the service already active and enabled performs no
mutating systemctl action. See the
[host systemd runbook](docker-deployment.md#host-systemd-alternative) for the
read-only inventory, exact FHS ownership/modes, migration, and rollback contract.

## Security Considerations

- The container runs as the fixed non-root identity `10001:10001`
- Application code, dependencies, and the bundled browser are root-owned and
  the root filesystem is read-only under Compose
- Configuration files for the web editor are mounted read-write so editor changes persist to the host
- Sensitive information is passed via environment variables
- The base image is kept minimal for a reduced attack surface
