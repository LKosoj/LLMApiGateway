from pathlib import Path


def test_editor_js_contains_provider_models_cache_logic():
    content = Path("static/editor.js").read_text(encoding="utf-8")

    assert "providerModelsCache" in content
    assert "MODELS_CACHE_TTL_MS = 15 * 60 * 1000" in content
    assert "/v1/config/models-rules/structured" in content
    assert "/v1/config/providers/${encodeURIComponent(providerName)}/models" in content
    assert "Choose an available model for provider" in content
    assert "Unavailable fallback models:" in content
    assert "context_overflow_fallback" in content
    assert "Context Overflow Fallback" in content
    assert "Enable dedicated fallback for context overflow errors" in content
    assert "strip_think_tags" in content
    assert "Strip <think> tags from replies" in content
    assert "max-total-attempts-input" in content
    assert "max_total_attempts" in content
    assert "Max Total Attempts (chain budget)" in content
    assert "use-provider-order-checkbox" in content
    assert "use_provider_order_as_fallback" in content
    assert "Use provider order as fallback" in content
    assert "Number.parseFloat(retryDelayInput.value)" in content
    assert "payload = getEmbeddingsPayloadForSave(await fetchOperationRulesPayload())" in content
    assert "availableProviders = [];" in content
    assert "/v1/config/providers/structured" in content
    assert "buildProviderCard" in content
    assert "provider-name-input" in content
    assert "Provider models metadata must be valid JSON." in content
