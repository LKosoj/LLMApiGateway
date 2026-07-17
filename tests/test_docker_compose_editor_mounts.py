from pathlib import Path
import unittest

ENTRYPOINT = Path("docker/entrypoint.sh")
DOCKERFILE = Path("Dockerfile")
DEFAULTS_SOURCE = Path("llm_gateway_core/config/container_preflight.py")

CONFIG_ENVIRONMENT = {
    "PROVIDERS_FILENAME": "/app/config/providers.json",
    "FALLBACK_RULES_FILENAME": "/app/config/models_fallback_rules.json",
    "OPERATION_RULES_FILENAME": "/app/config/models_operation_rules.json",
    "FUSION_RULES_FILENAME": "/app/config/models_fusion_rules.json",
    "MODEL_RULES_FILENAME": "/app/config/models_model_rules.json",
    "ROUTER_RULES_FILENAME": "/app/config/models_router_rules.json",
}
CONFIG_DOCS = (
    Path("README.md"),
    Path("README_EN.MD"),
    Path("docker/README.md"),
    Path("docker/docker-deployment.md"),
)
CONFIG_INITIALIZER = (
    "sudo python3 scripts/init_docker_config.py --source-dir . "
    "--target-dir ./config"
)
DATABASE_DIRECTORY_INITIALIZER = "sudo install -d -o 10001 -g 10001 -m 0750 data/db"
DOCKER_DEPLOYMENT_DOCS = (
    Path("docker/README.md"),
    Path("docker/docker-deployment.md"),
)


class DockerComposeEditorMountsTests(unittest.TestCase):
    def test_build_requires_canonical_version_without_hardcoding_it(self):
        compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn(
            "LLMGATEWAY_EXPECTED_PRODUCT_VERSION: "
            "${LLMGATEWAY_EXPECTED_PRODUCT_VERSION:?derive it with "
            "python3 scripts/check_product_version.py --print}",
            compose_text,
        )
        self.assertNotIn("LLMGATEWAY_EXPECTED_PRODUCT_VERSION: 1.10.0", compose_text)

    def test_config_uses_one_writable_directory_mount_for_atomic_replaces(self):
        compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")

        self.assertEqual(compose_text.count("./config:/app/config"), 1)
        self.assertNotIn("./config:/app/config:ro", compose_text)
        for filename in (
            "providers.json",
            "models_fallback_rules.json",
            "models_operation_rules.json",
            "models_fusion_rules.json",
            "models_router_rules.json",
            "models_model_rules.json",
        ):
            self.assertNotIn(f"./{filename}:/app/{filename}", compose_text)

    def test_six_config_environment_paths_are_direct_children_of_mount(self):
        compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")

        for name, value in CONFIG_ENVIRONMENT.items():
            self.assertEqual(compose_text.count(f"- {name}={value}"), 1)

    def test_container_docs_use_one_initialized_config_directory(self):
        for path in CONFIG_DOCS:
            text = path.read_text(encoding="utf-8")
            self.assertIn(CONFIG_INITIALIZER, text, path)
            self.assertIn("./config", text, path)
            self.assertIn("/app/config", text, path)
            for filename in (
                "providers.json",
                "models_fallback_rules.json",
                "models_operation_rules.json",
                "models_fusion_rules.json",
                "models_router_rules.json",
                "models_model_rules.json",
            ):
                self.assertNotIn(f":/app/{filename}", text, path)

    def test_docker_docs_prepare_both_mandatory_configs_before_initialization(self):
        for path in DOCKER_DEPLOYMENT_DOCS:
            text = path.read_text(encoding="utf-8")
            self.assertIn(DATABASE_DIRECTORY_INITIALIZER, text, path)
            self.assertIn("10001:10001", text, path)
            self.assertNotIn("999:999", text, path)
            self.assertNotIn("mkdir -p data/db", text, path)
            self.assertNotIn("nano config/", text, path)
            self.assertNotIn("Only `providers.json` is mandatory before startup.", text, path)
            self.assertIn("models_fallback_rules.json", text, path)
            self.assertLess(text.index("nano providers.json"), text.index(CONFIG_INITIALIZER))

    def test_manual_docker_docs_preserve_the_read_only_mount_contract(self):
        deployment = Path("docker/docker-deployment.md").read_text(encoding="utf-8")

        self.assertIn("--read-only", deployment)
        self.assertIn("--tmpfs /app/logs:rw,mode=0770,uid=10001,gid=10001", deployment)
        self.assertIn("llm-gateway-outputs:/app/outputs", deployment)
        self.assertNotIn("--tmpfs /app/outputs", deployment)
        self.assertIn("--tmpfs /tmp:rw,mode=1777", deployment)

    def test_restore_docs_verify_a_separate_volume_before_destructive_replace(self):
        deployment = Path("docker/docker-deployment.md").read_text(encoding="utf-8")

        check_start = deployment.index(
            'restore_check_volume="llm-gateway-restore-check-$(date +%s)"'
        )
        check_cleanup = deployment.index('docker volume rm "${restore_check_volume}"')
        destructive_replace = deployment.index("\ndocker compose down -v\n")
        replacement = deployment[destructive_replace:]

        self.assertLess(check_start, check_cleanup)
        self.assertLess(check_cleanup, destructive_replace)
        self.assertLess(
            replacement.index("docker compose run --rm --no-deps outputs-init"),
            replacement.index("image_storage_cli restore"),
        )

    def test_image_has_fixed_existing_non_root_identity_and_writable_directories(self):
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        self.assertNotIn("ARG LLMGATEWAY_UID", dockerfile)
        self.assertNotIn("ARG LLMGATEWAY_GID", dockerfile)
        self.assertIn("groupadd --gid 10001 llmgateway", dockerfile)
        self.assertIn(
            "useradd --uid 10001 --gid 10001",
            dockerfile,
        )
        self.assertIn("/app/config /app/db /app/logs /app/outputs /app/outputs/images", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertNotIn("USER root", dockerfile)
        self.assertIn('APP_DIR="/app"', dockerfile)
        self.assertIn('GATEWAY_OUTPUTS_DIR="/app/outputs"', dockerfile)

        for name, value in CONFIG_ENVIRONMENT.items():
            self.assertIn(f'{name}="{value}"', dockerfile)

    def test_compose_root_is_read_only_with_only_declared_writable_mounts(self):
        compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")

        self.assertEqual(compose_text.count("read_only: true"), 2)
        self.assertIn("./config:/app/config", compose_text)
        self.assertIn("./data/db:/app/db", compose_text)
        self.assertIn("gateway_outputs:/app/outputs", compose_text)
        self.assertIn("/app/logs:mode=0770,uid=10001,gid=10001", compose_text)
        self.assertNotIn("/app/outputs:mode=0770,uid=10001,gid=10001", compose_text)
        self.assertIn("/tmp:mode=1777", compose_text)
        self.assertIn("- GATEWAY_DB_DIR=/app/db", compose_text)
        self.assertIn("- GATEWAY_OUTPUTS_DIR=/app/outputs", compose_text)

    def test_compose_uses_project_scoped_outputs_volume_and_one_shot_initializer(self):
        compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")

        self.assertNotIn("container_name:", compose_text)
        self.assertIn("outputs-init:", compose_text)
        self.assertIn('user: "0:0"', compose_text)
        self.assertIn('network_mode: "none"', compose_text)
        self.assertIn('restart: "no"', compose_text)
        self.assertIn("condition: service_completed_successfully", compose_text)
        self.assertIn(
            '["init-volume", "--outputs-dir", "/app/outputs"]',
            compose_text,
        )
        volume_block = compose_text[compose_text.rindex("\nvolumes:") :]
        self.assertIn("gateway_outputs:", volume_block)
        self.assertNotIn("name:", volume_block)

    def test_entrypoint_creates_runtime_directories_only_after_config_preflight(self):
        entrypoint_text = ENTRYPOINT.read_text(encoding="utf-8")

        preflight = entrypoint_text.index(
            "python -m llm_gateway_core.config.container_preflight"
        )
        runtime_directories = entrypoint_text.index(
            "mkdir -p /app/logs /app/db"
        )
        self.assertLess(preflight, runtime_directories)
        self.assertNotIn("mkdir -p /app/outputs", entrypoint_text)

    def test_environment_example_values_are_clean_and_documented_with_yaml_comments(self):
        compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")
        env_lines = [
            line.strip()
            for line in compose_text.splitlines()
            if line.strip().startswith("- ") and "=" in line and "/app/" not in line
        ]

        self.assertIn("- GATEWAY_API_KEY=your-secure-api-key", env_lines)
        self.assertIn("- APIKEY_OPENROUTER=your-openrouter-api-key", env_lines)
        self.assertTrue(all("(" not in line and ")" not in line for line in env_lines))
        self.assertIn("# Required: this is the gateway token clients must send to this service.", compose_text)
        self.assertIn("# APIKEY_OPENROUTER is issued by OpenRouter.", compose_text)

    def test_entrypoint_delegates_one_fail_closed_prepare_to_symlink_safe_python(self):
        entrypoint_text = ENTRYPOINT.read_text(encoding="utf-8")

        self.assertEqual(
            entrypoint_text.count("python -m llm_gateway_core.config.container_preflight"),
            1,
        )
        self.assertNotIn("--materialize-legacy-defaults", entrypoint_text)
        self.assertNotIn('[ ! -f "${', entrypoint_text)
        self.assertNotIn("cp /app/docker/models_fallback_rules.json.template", entrypoint_text)
        self.assertNotIn("> \"${", entrypoint_text)

    def test_preflight_requires_provider_and_fallback_before_optional_defaults(self):
        defaults_source = DEFAULTS_SOURCE.read_text(encoding="utf-8")
        prepare_source = defaults_source[
            defaults_source.index("def prepare_container_config(") : defaults_source.index(
                "def main("
            )
        ]

        capture_index = prepare_source.index("initial_sources = _capture_sources")
        optional_index = prepare_source.index("_materialize_optional_defaults")
        self.assertLess(capture_index, optional_index)

    def test_entrypoint_creates_empty_operation_and_fusion_rules_without_fake_routes(self):
        defaults_source = DEFAULTS_SOURCE.read_text(encoding="utf-8")

        self.assertIn('b"{}\\n"', defaults_source)
        self.assertIn('b"[]\\n"', defaults_source)
        self.assertNotIn("openai/text-embedding-3-large", defaults_source)
        self.assertNotIn("rerank-multilingual-v3", defaults_source)
        self.assertNotIn("openai/gpt-image-1", defaults_source)

    def test_entrypoint_has_no_synthetic_or_template_fallback_route(self):
        defaults_source = DEFAULTS_SOURCE.read_text(encoding="utf-8")

        self.assertNotIn("openai/gpt-3.5-turbo", defaults_source)
        self.assertNotIn("FALLBACK_PROVIDER", defaults_source)
        self.assertNotIn("models_fallback_rules.json.template", defaults_source)

    def test_entrypoint_execs_command_as_pid1_without_background_wrapper(self):
        entrypoint_text = ENTRYPOINT.read_text(encoding="utf-8")

        self.assertIn(
            'exec python -m llm_gateway_core.services.container_exec "$@"',
            entrypoint_text,
        )
        self.assertNotIn('exec "$@" &', entrypoint_text)
        self.assertNotIn("trap 'kill -TERM $child'", entrypoint_text)


if __name__ == "__main__":
    unittest.main()
