# LLM Gateway Docker Implementation

This directory contains the Docker implementation for the LLM Gateway project.

## Quick Reference

- **Dockerfile**: Multi-stage build for production use
- **docker-compose.yml**: Easy deployment configuration
- **entrypoint.sh**: Container startup script
- **healthcheck.py**: Container health monitoring
- **docker-deployment.md**: Comprehensive deployment guide

## Getting Started

1. Create necessary directories:
   ```bash
   mkdir -p data/db
   ```

2. Edit the configuration files with your details:
   - Edit `providers.json` with your provider details
   - Edit `models_fallback_rules.json` with your fallback rules
   - Edit `models_operation_rules.json` with operation routes, or let the container create a minimal default file on first start
   - Create Router models in `/v1/ui/rules-editor` on the **Router** tab, or leave `models_router_rules.json` as `[]`
   - Edit `models_model_rules.json` with model aliases, excludes, and upstream model pools, or leave it as `{}`

3. Deploy using Docker Compose:
   ```bash
   docker-compose up -d
   ```

## Accessing the Gateway

- Web UI: http://localhost:9000/v1/ui/rules-editor
- API: http://localhost:9000/v1/chat/completions

## Configuration

### Required Environment Variables

- `GATEWAY_API_KEY`: API key for accessing the gateway
- At least one provider API key (e.g., `APIKEY_OPENROUTER`)

### Volume Mounts

1. **providers.json**: `-v ./providers.json:/app/providers.json`
2. **models_fallback_rules.json**: `-v ./models_fallback_rules.json:/app/models_fallback_rules.json`
3. **models_operation_rules.json**: `-v ./models_operation_rules.json:/app/models_operation_rules.json`
4. **models_router_rules.json**: `-v ./models_router_rules.json:/app/models_router_rules.json`
5. **models_model_rules.json**: `-v ./models_model_rules.json:/app/models_model_rules.json`
6. **Database**: `-v ./data/db:/app/db`

The config files are mounted read-write on purpose. This allows the web editor at `/v1/ui/rules-editor` to save supported changes from inside the container and persist them back to the host copies of `providers.json`, `models_fallback_rules.json`, `models_operation_rules.json`, `models_router_rules.json`, and `models_model_rules.json`, including Router models from the **Router** tab.

## Documentation

For detailed deployment instructions, refer to [docker-deployment.md](docker-deployment.md).

## Security Considerations

- The container runs as a non-root user
- Configuration files for the web editor are mounted read-write so editor changes persist to the host
- Sensitive information is passed via environment variables
- The base image is kept minimal for a reduced attack surface
