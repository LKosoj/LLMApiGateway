from pathlib import Path
import unittest

ENTRYPOINT = Path("docker/entrypoint.sh")


class DockerComposeEditorMountsTests(unittest.TestCase):
    def test_config_mounts_are_writable_for_web_editor(self):
        compose_text = Path("docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("./providers.json:/app/providers.json", compose_text)
        self.assertIn("./models_fallback_rules.json:/app/models_fallback_rules.json", compose_text)
        self.assertIn("./models_operation_rules.json:/app/models_operation_rules.json", compose_text)
        self.assertIn("./models_fusion_rules.json:/app/models_fusion_rules.json", compose_text)
        self.assertIn("./models_router_rules.json:/app/models_router_rules.json", compose_text)
        self.assertIn("./models_model_rules.json:/app/models_model_rules.json", compose_text)
        self.assertNotIn("./providers.json:/app/providers.json:ro", compose_text)
        self.assertNotIn("./models_fallback_rules.json:/app/models_fallback_rules.json:ro", compose_text)
        self.assertNotIn("./models_operation_rules.json:/app/models_operation_rules.json:ro", compose_text)
        self.assertNotIn("./models_fusion_rules.json:/app/models_fusion_rules.json:ro", compose_text)
        self.assertNotIn("./models_router_rules.json:/app/models_router_rules.json:ro", compose_text)
        self.assertNotIn("./models_model_rules.json:/app/models_model_rules.json:ro", compose_text)

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

    def test_entrypoint_creates_empty_model_rules_file_when_missing(self):
        entrypoint_text = ENTRYPOINT.read_text(encoding="utf-8")

        self.assertIn('/app/models_model_rules.json', entrypoint_text)
        self.assertIn("echo '{}'", entrypoint_text)

    def test_entrypoint_creates_empty_router_rules_file_when_missing(self):
        entrypoint_text = ENTRYPOINT.read_text(encoding="utf-8")

        self.assertIn('/app/models_router_rules.json', entrypoint_text)
        self.assertIn("echo '[]'", entrypoint_text)

    def test_entrypoint_keeps_provider_config_fail_fast_before_defaults(self):
        entrypoint_text = ENTRYPOINT.read_text(encoding="utf-8")

        providers_check_index = entrypoint_text.index('if [ ! -f "/app/providers.json" ]; then')
        operation_defaults_index = entrypoint_text.index('if [ ! -f "/app/models_operation_rules.json" ]; then')
        self.assertLess(providers_check_index, operation_defaults_index)
        self.assertIn("ERROR: providers.json not found", entrypoint_text)
        self.assertIn("exit 1", entrypoint_text)

    def test_entrypoint_creates_empty_operation_and_fusion_rules_without_fake_routes(self):
        entrypoint_text = ENTRYPOINT.read_text(encoding="utf-8")

        self.assertIn('/app/models_operation_rules.json', entrypoint_text)
        self.assertIn('/app/models_fusion_rules.json', entrypoint_text)
        self.assertIn("echo '{}' > /app/models_operation_rules.json", entrypoint_text)
        self.assertIn("echo '[]' > /app/models_fusion_rules.json", entrypoint_text)
        self.assertNotIn("openai/text-embedding-3-large", entrypoint_text)
        self.assertNotIn("rerank-multilingual-v3", entrypoint_text)
        self.assertNotIn("openai/gpt-image-1", entrypoint_text)

    def test_entrypoint_execs_command_as_pid1_without_background_wrapper(self):
        entrypoint_text = ENTRYPOINT.read_text(encoding="utf-8")

        self.assertIn('exec "$@"', entrypoint_text)
        self.assertNotIn('exec "$@" &', entrypoint_text)
        self.assertNotIn("trap 'kill -TERM $child'", entrypoint_text)


if __name__ == "__main__":
    unittest.main()
