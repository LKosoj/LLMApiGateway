"""Tests for compress_tool_results propagation through ConfigLoader._build_fallback_rules_config."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.config import settings as _settings


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_providers_file(tmp: str) -> Path:
    p = Path(tmp) / "providers.json"
    _write_json(p, [
        {"test-provider": {"baseUrl": "https://example.com", "apikey": "key123"}}
    ])
    return p


def _make_fallback_rules_file(tmp: str, compress: bool) -> Path:
    f = Path(tmp) / "fallback_rules.json"
    _write_json(f, [
        {
            "gateway_model_name": "my-model",
            "compress_tool_results": compress,
            "fallback_models": [
                {"provider": "test-provider", "model": "real-model"}
            ],
        }
    ])
    return f


def _make_operation_rules_file(tmp: str) -> Path:
    o = Path(tmp) / "operation_rules.json"
    _write_json(o, {})
    return o


def _load_with_patched_settings(tmp: str, compress_rules_path: Path) -> "ConfigLoader":
    """Create and load a ConfigLoader with fallback_provider patched to match test-provider."""
    p = _make_providers_file(tmp)
    o = _make_operation_rules_file(tmp)
    loader = ConfigLoader(
        providers_filename=str(p),
        fallback_rules_filename=str(compress_rules_path),
        operation_rules_filename=str(o),
    )
    with patch.object(_settings.settings, "fallback_provider", "test-provider"):
        loader.load_providers()
        loader.load_fallback_rules()
    return loader


def test_compress_tool_results_true_propagates():
    """compress_tool_results: true in JSON reaches model_config via ConfigLoader."""
    with tempfile.TemporaryDirectory() as tmp:
        f = _make_fallback_rules_file(tmp, compress=True)
        loader = _load_with_patched_settings(tmp, f)

        rule = loader.fallback_rules.get("my-model")
        assert rule is not None, "Rule for 'my-model' not found"
        assert rule.get("compress_tool_results") is True, (
            f"Expected compress_tool_results=True, got {rule.get('compress_tool_results')!r}"
        )


def test_compress_tool_results_false_propagates():
    """compress_tool_results: false reaches model_config as False."""
    with tempfile.TemporaryDirectory() as tmp:
        f = _make_fallback_rules_file(tmp, compress=False)
        loader = _load_with_patched_settings(tmp, f)

        rule = loader.fallback_rules.get("my-model")
        assert rule is not None
        assert rule.get("compress_tool_results") is False


def test_compress_tool_results_absent_defaults_false():
    """compress_tool_results absent from JSON defaults to False in model_config."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "fallback_rules.json"
        # No compress_tool_results key at all
        _write_json(f, [
            {
                "gateway_model_name": "my-model",
                "fallback_models": [
                    {"provider": "test-provider", "model": "real-model"}
                ],
            }
        ])
        loader = _load_with_patched_settings(tmp, f)

        rule = loader.fallback_rules.get("my-model")
        assert rule is not None
        assert rule.get("compress_tool_results") is False
