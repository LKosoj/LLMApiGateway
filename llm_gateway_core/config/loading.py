import json5
import logging
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, Self

from .config_store import ConfigDocument, ConfigFile, ConfigSourceBundle
from .schema_validation import empty_operation_rules as _empty_operation_rules
from .schemas import ProviderDetails
from .validation import ConfigError, _ConfigValidationMixin, rule_validation

UTF8_BOM = b"\xef\xbb\xbf"
OPTIONAL_CONFIG_SHAPES = {
    ConfigFile.MODEL_RULES: "{}",
    ConfigFile.OPERATION_RULES: "{}",
    ConfigFile.FUSION_RULES: "[]",
    ConfigFile.ROUTER_RULES: "[]",
}

def _resolve_application_root(project_root: Path) -> Path:
    configured_root = os.getenv("APP_DIR")
    if configured_root is None:
        return project_root
    if not configured_root or not os.path.isabs(configured_root):
        raise ValueError("APP_DIR must be a non-empty absolute path.")
    return Path(os.path.normpath(configured_root))


def _resolve_config_path(
    application_root: Path,
    explicit_value: str | None,
    environment_name: str,
    default_filename: str,
) -> Path:
    value = (
        explicit_value
        if explicit_value is not None
        else os.getenv(environment_name, default_filename)
    )
    try:
        raw_path = os.fspath(value)
    except TypeError:
        raise TypeError(f"{environment_name} must be a text path.") from None
    if not isinstance(raw_path, str):
        raise TypeError(f"{environment_name} must be a text path.")
    if not raw_path:
        raise ValueError(f"{environment_name} must not be empty.")
    absolute_path = (
        raw_path
        if os.path.isabs(raw_path)
        else os.path.join(application_root, raw_path)
    )
    normalized_path = Path(os.path.normpath(absolute_path))
    if not normalized_path.name:
        raise ValueError(f"{environment_name} must identify a file.")
    return normalized_path


class _ConfigLoaderCore(_ConfigValidationMixin):
    def __init__(
        self,
        providers_filename: str | None = None,
        fallback_rules_filename: str | None = None,
        operation_rules_filename: str | None = None,
        fusion_rules_filename: str | None = None,
        model_rules_filename: str | None = None,
        router_rules_filename: str | None = None,
    ):
        from .paths import PROJECT_ROOT
        self.project_root = _resolve_application_root(PROJECT_ROOT)

        self.providers_path = _resolve_config_path(
            self.project_root,
            providers_filename,
            "PROVIDERS_FILENAME",
            "providers.json",
        )
        self.fallback_rules_path = _resolve_config_path(
            self.project_root,
            fallback_rules_filename,
            "FALLBACK_RULES_FILENAME",
            "models_fallback_rules.json",
        )
        self.operation_rules_path = _resolve_config_path(
            self.project_root,
            operation_rules_filename,
            "OPERATION_RULES_FILENAME",
            "models_operation_rules.json",
        )
        self.fusion_rules_path = _resolve_config_path(
            self.project_root,
            fusion_rules_filename,
            "FUSION_RULES_FILENAME",
            "models_fusion_rules.json",
        )
        self.model_rules_path = _resolve_config_path(
            self.project_root,
            model_rules_filename,
            "MODEL_RULES_FILENAME",
            "models_model_rules.json",
        )
        self.router_rules_path = _resolve_config_path(
            self.project_root,
            router_rules_filename,
            "ROUTER_RULES_FILENAME",
            "models_router_rules.json",
        )

        self.providers_config: Dict[str, ProviderDetails] = {}
        self.fallback_rules: Dict[str, Dict[str, Any]] = {} # Store validated rules as dicts
        self._fallback_rules_base: Dict[str, Dict[str, Any]] = {}
        self.operation_rules: Dict[str, Dict[str, Any]] = _empty_operation_rules()
        self.fusion_rules: Dict[str, Dict[str, Any]] = {} # Store validated fusion configs as dicts
        self.model_rules: Dict[str, Any] = {}
        self.router_rules: Dict[str, Dict[str, Any]] = {} # Store validated router configs as dicts
        self._source_bundle: ConfigSourceBundle | None = None

    @classmethod
    def from_source_bundle(cls, bundle: ConfigSourceBundle) -> Self:
        if not isinstance(bundle, ConfigSourceBundle):
            raise TypeError("bundle must be a ConfigSourceBundle")
        loader = cls(
            providers_filename=str(bundle[ConfigFile.PROVIDERS].path),
            fallback_rules_filename=str(bundle[ConfigFile.FALLBACK_RULES].path),
            operation_rules_filename=str(bundle[ConfigFile.OPERATION_RULES].path),
            fusion_rules_filename=str(bundle[ConfigFile.FUSION_RULES].path),
            model_rules_filename=str(bundle[ConfigFile.MODEL_RULES].path),
            router_rules_filename=str(bundle[ConfigFile.ROUTER_RULES].path),
        )
        loader._source_bundle = bundle
        return loader

    @property
    def source_bundle(self) -> ConfigSourceBundle | None:
        return self._source_bundle

    @property
    def configured_paths(self) -> Mapping[ConfigFile, Path]:
        """Return the exact six paths snapshotted when this loader was created."""
        return MappingProxyType(
            {
                ConfigFile.PROVIDERS: self.providers_path,
                ConfigFile.FALLBACK_RULES: self.fallback_rules_path,
                ConfigFile.MODEL_RULES: self.model_rules_path,
                ConfigFile.OPERATION_RULES: self.operation_rules_path,
                ConfigFile.FUSION_RULES: self.fusion_rules_path,
                ConfigFile.ROUTER_RULES: self.router_rules_path,
            }
        )

    def bind_persisted_source_bundle(self, bundle: ConfigSourceBundle) -> None:
        """Replace candidate-only metadata with a verified persisted recapture."""
        if not isinstance(bundle, ConfigSourceBundle):
            raise TypeError("bundle must be a ConfigSourceBundle")
        current_bundle = self._source_bundle
        if current_bundle is None:
            raise ConfigError("A persisted source bundle cannot be bound before loading.")

        for config_file in ConfigFile:
            candidate = current_bundle[config_file]
            persisted = bundle[config_file]
            if (
                candidate.path != persisted.path
                or candidate.exists != persisted.exists
                or candidate.content != persisted.content
                or candidate.digest != persisted.digest
            ):
                raise ConfigError("Persisted configuration does not match the candidate sources.")
            if persisted.exists and persisted.metadata is None:
                raise ConfigError("Persisted configuration metadata is unavailable.")

        self._source_bundle = bundle

    def load_complete(self) -> Self:
        bundle = self._source_bundle
        if bundle is None:
            try:
                bundle = ConfigSourceBundle.capture(
                    self.project_root,
                    overrides=self.configured_paths,
                )
            except Exception as exc:
                raise ConfigError("Could not capture configuration sources.") from exc

        providers_config = self._parse_complete_document(
            bundle,
            ConfigFile.PROVIDERS,
            self._parse_and_validate_complete_providers,
        )
        fallback_rules_base = self._parse_complete_document(
            bundle,
            ConfigFile.FALLBACK_RULES,
            lambda payload: self.parse_and_validate_fallback_rules_payload(
                payload,
                providers_config=providers_config,
            ),
        )
        model_rules, fallback_rules = self._parse_complete_document(
            bundle,
            ConfigFile.MODEL_RULES,
            lambda payload: self.parse_and_validate_model_rules_payload_with_fallbacks(
                payload,
                providers_config=providers_config,
                fallback_rules=fallback_rules_base,
            ),
        )
        operation_rules = self._parse_complete_document(
            bundle,
            ConfigFile.OPERATION_RULES,
            lambda payload: self.parse_and_validate_operation_routes_payload(
                payload,
                providers_config=providers_config,
                fallback_rules=fallback_rules,
            ),
        )
        fusion_rules = self._parse_complete_document(
            bundle,
            ConfigFile.FUSION_RULES,
            lambda payload: self.parse_and_validate_fusion_rules_payload(
                payload,
                providers_config=providers_config,
            ),
        )
        router_rules = self._parse_complete_document(
            bundle,
            ConfigFile.ROUTER_RULES,
            lambda payload: self.parse_and_validate_router_rules_payload(
                payload,
                fallback_rules=fallback_rules,
                fusion_rules=fusion_rules,
            ),
        )
        try:
            with rule_validation():
                self.validate_fallback_operation_consistency(
                    fallback_rules=fallback_rules,
                    operation_rules=operation_rules,
                )
                self._validate_complete_cross_graph(
                    model_rules=model_rules,
                    fallback_rules=fallback_rules,
                    operation_rules=operation_rules,
                    fusion_rules=fusion_rules,
                    router_rules=router_rules,
                )
        except Exception as exc:
            raise ConfigError("Invalid cross-graph configuration.") from exc

        self.providers_config = providers_config
        self._fallback_rules_base = fallback_rules_base
        self.model_rules = model_rules
        self.fallback_rules = fallback_rules
        self.operation_rules = operation_rules
        self.fusion_rules = fusion_rules
        self.router_rules = router_rules
        self._source_bundle = bundle
        return self

    def _parse_complete_document(
        self,
        bundle: ConfigSourceBundle,
        config_file: ConfigFile,
        parser: Callable[[str], Any],
    ) -> Any:
        try:
            payload = self._decode_complete_document(
                bundle[config_file],
                missing_shape=OPTIONAL_CONFIG_SHAPES.get(config_file),
            )
            return parser(payload)
        except Exception as exc:
            raise ConfigError(
                f"Invalid '{config_file.value}' configuration.",
                config_file=config_file,
            ) from exc

    @staticmethod
    def _decode_complete_document(
        document: ConfigDocument,
        *,
        missing_shape: str | None,
    ) -> str:
        if not document.exists:
            if missing_shape is None:
                raise ValueError("mandatory configuration is missing")
            return missing_shape
        assert document.content is not None
        if not document.content:
            raise ValueError("configuration document is empty")
        validation_bytes = document.content
        if validation_bytes.startswith(UTF8_BOM):
            validation_bytes = validation_bytes[len(UTF8_BOM):]
        if UTF8_BOM in validation_bytes:
            raise ValueError("configuration document contains repeated or embedded BOM")
        return validation_bytes.decode("utf-8", errors="strict")

    def _resolve_operation_rules_path(self, filename: str = "models_operation_rules.json") -> Path:
        if filename == "models_operation_rules.json":
            return self.operation_rules_path
        return self.project_root / filename

    def _read_operation_rules_payload(self, operation_rules_path: Path) -> Any:
        with open(operation_rules_path, 'r', encoding='utf-8') as f:
            payload_text = f.read()

        if not payload_text.strip():
            return {}

        return json5.loads(payload_text)

    def load_providers(self) -> Dict[str, ProviderDetails]:
        """Loads and validates provider configurations from the JSON file."""

        if not self.providers_path.exists():
            message = f"Provider configuration file not found at {self.providers_path}"
            logging.error(message)
            raise ConfigError(message)

        try:
            with open(self.providers_path, 'r', encoding='utf-8') as f:
                raw_mapping = json5.load(f)

            providers_config_temp = self._build_providers_config(raw_mapping)

            self.providers_config = providers_config_temp
            if not self._perform_provider_semantic_validation(self.providers_config, raise_on_error=True):
                logging.critical("Provider semantic validation unexpectedly returned False during initial load.")
                raise ConfigError("Provider semantic validation failed during initial load.")

            logging.info(f"Successfully loaded and validated providers from {self.providers_path}")
            logging.info(f"Loaded providers: {list(self.providers_config.keys())}")
            return self.providers_config

        except ConfigError:
            raise
        except Exception as e:
            logging.error(f"Failed to load or validate '{self.providers_path.name}': {str(e)}", exc_info=True)
            raise ConfigError(f"Failed to load or validate '{self.providers_path.name}': {e}") from e

    def load_model_rules(self) -> Dict[str, Any]:
        """Loads optional model alias/prefix/exclusion/upstream-pool rules."""
        if not self.model_rules_path.exists():
            self.model_rules = {}
            return {}

        try:
            with open(self.model_rules_path, 'r', encoding='utf-8') as f:
                raw_rules = json5.load(f)

            model_rules_temp = self._build_model_rules_config(raw_rules)
            base_fallback_rules = (
                self._fallback_rules_base
                if self._fallback_rules_base is not None
                else self.fallback_rules
            )
            combined_fallback_rules = self._combine_fallback_rules_with_model_rules(
                model_rules_temp,
                base_fallback_rules,
                providers_config=self.providers_config,
            )
            self.fallback_rules = combined_fallback_rules
            self.model_rules = model_rules_temp
            logging.info(f"Successfully loaded model rules from {self.model_rules_path}")
            return self.model_rules

        except ConfigError:
            raise
        except Exception as e:
            logging.error(f"Failed to load or validate '{self.model_rules_path.name}': {str(e)}", exc_info=True)
            raise ConfigError(f"Failed to load or validate '{self.model_rules_path.name}': {e}") from e


    def load_fallback_rules(self) -> Dict[str, Dict[str, Any]]:
        """Loads and validates model fallback rules from the JSON file."""
        if not self.fallback_rules_path.exists():
            raise ConfigError(
                f"Required fallback rules file '{self.fallback_rules_path.name}' was not found."
            )

        try:
            with open(self.fallback_rules_path, 'r', encoding='utf-8') as f:
                raw_rules = json5.load(f)

            fallback_rules_temp = self._build_fallback_rules_config(raw_rules)

            self.fallback_rules = fallback_rules_temp
            self._fallback_rules_base = fallback_rules_temp
            self._validate_fallback_rules() # Perform post-load validation
            logging.info(f"Successfully loaded and validated model fallback rules from {self.fallback_rules_path}")
            logging.info(f"Loaded model rules for: {list(self.fallback_rules.keys())}")
            return self.fallback_rules

        except ConfigError:
            raise
        except Exception as e:
            logging.error(f"Failed to load or validate '{self.fallback_rules_path.name}': {str(e)}", exc_info=True)
            raise ConfigError(f"Failed to load or validate '{self.fallback_rules_path.name}': {e}") from e

    def load_fusion_rules(self) -> Dict[str, Dict[str, Any]]:
        """Loads and validates Fusion model configs from the JSON file."""
        if not self.fusion_rules_path.exists():
            logging.info(
                f"Fusion rules file not found at {self.fusion_rules_path}. Proceeding without fusion models."
            )
            self.fusion_rules = {}
            return {}

        try:
            with open(self.fusion_rules_path, 'r', encoding='utf-8') as f:
                raw_rules = json5.load(f)

            fusion_rules_temp = self._build_fusion_rules_config(raw_rules)

            if not self.providers_config:
                logging.warning("Providers not loaded. Loading providers before validating fusion rules.")
                self.load_providers()

            self.validate_fusion_rules_mapping(fusion_rules_temp, providers_config=self.providers_config)
            self.fusion_rules = fusion_rules_temp
            logging.info(f"Successfully loaded and validated fusion rules from {self.fusion_rules_path}")
            logging.info(f"Loaded fusion models for: {list(self.fusion_rules.keys())}")
            return self.fusion_rules

        except ConfigError:
            raise
        except Exception as e:
            logging.error(f"Failed to load or validate '{self.fusion_rules_path.name}': {str(e)}", exc_info=True)
            raise ConfigError(f"Failed to load or validate '{self.fusion_rules_path.name}': {e}") from e

    def load_router_rules(self) -> Dict[str, Dict[str, Any]]:
        """Loads and validates Router model configs from the JSON file."""
        if not self.router_rules_path.exists():
            logging.info(
                f"Router rules file not found at {self.router_rules_path}. Proceeding without router models."
            )
            self.router_rules = {}
            return {}

        try:
            with open(self.router_rules_path, 'r', encoding='utf-8') as f:
                payload_text = f.read()

            if not payload_text.strip():
                payload_text = "[]"

            router_rules_temp = self.parse_and_validate_router_rules_payload(
                payload_text,
                fallback_rules=self.fallback_rules,
                fusion_rules=self.fusion_rules,
            )
            self.router_rules = router_rules_temp
            logging.info(f"Successfully loaded and validated router rules from {self.router_rules_path}")
            logging.info(f"Loaded router models for: {list(self.router_rules.keys())}")
            return self.router_rules

        except ConfigError:
            raise
        except Exception as e:
            logging.error(f"Failed to load or validate '{self.router_rules_path.name}': {str(e)}", exc_info=True)
            raise ConfigError(f"Failed to load or validate '{self.router_rules_path.name}': {e}") from e

    def load_operation_rules(self, filename: str = "models_operation_rules.json") -> Dict[str, Dict[str, Any]]:
        """Loads and validates operation routing rules from the JSON file."""
        operation_rules_path = self._resolve_operation_rules_path(filename)
        self.operation_rules_path = operation_rules_path
        if not operation_rules_path.exists():
            logging.warning(
                "Model operation rules file not found at %s. Proceeding without operation rules.",
                operation_rules_path,
            )
            self.operation_rules = _empty_operation_rules()
            return self.operation_rules

        try:
            raw_rules = self._read_operation_rules_payload(operation_rules_path)
            operation_rules_temp = self._build_operation_config(raw_rules)

            self.operation_rules = operation_rules_temp
            self._validate_operation_rules()
            logging.info("Successfully loaded and validated model operation rules from %s", operation_rules_path)
            logging.info(
                "Loaded operation rules for embeddings: %s; rerank: %s; images_generations: %s; "
                "images_edits: %s; audio_speech: %s; audio_transcriptions: %s; web_search: %s; "
                "web_read: %s; web_research: %s; web_deep_research: %s; pdf_conversions: %s",
                list(self.operation_rules["embeddings"].keys()),
                list(self.operation_rules["rerank"].keys()),
                list(self.operation_rules["images_generations"].keys()),
                list(self.operation_rules["images_edits"].keys()),
                list(self.operation_rules.get("audio_speech", {}).keys()),
                list(self.operation_rules.get("audio_transcriptions", {}).keys()),
                list(self.operation_rules.get("web_search", {}).keys()),
                list(self.operation_rules.get("web_read", {}).keys()),
                list(self.operation_rules.get("web_research", {}).keys()),
                list(self.operation_rules.get("web_deep_research", {}).keys()),
                list(self.operation_rules.get("pdf_conversions", {}).keys()),
            )
            return self.operation_rules

        except ConfigError:
            raise
        except Exception as e:
            logging.error(f"Failed to load or validate '{operation_rules_path.name}': {str(e)}", exc_info=True)
            raise ConfigError(f"Failed to load or validate '{operation_rules_path.name}': {e}") from e
