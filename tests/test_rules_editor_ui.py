from pathlib import Path


def test_editor_js_contains_provider_models_cache_logic():
    content = Path("static/editor.js").read_text(encoding="utf-8")

    assert "providerModelsCache" in content
    assert "MODELS_CACHE_TTL_MS = 15 * 60 * 1000" in content
    assert "MODEL_ID_COLLATOR" in content
    assert "sortProviderModelIds(models)" in content
    assert "models: sortedModels" in content
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
    assert "/v1/openrouter/free-models" in content
    assert "/v1/fallback-model-evals" in content
    assert "/v1/fallback-model-evals/run" in content
    assert "tabOpenRouterFree.hidden = !response.ok || !payload.configured" in content
    assert "OpenRouter free model ranking" in content
    assert "runFallbackModelEval" in content


def test_rules_editor_html_contains_openrouter_free_tab():
    content = Path("static/rules-editor.html").read_text(encoding="utf-8")

    assert 'id="tabOpenRouterFree"' in content
    assert 'data-tab="openrouter-free"' in content
    assert 'id="openRouterFreeModels"' in content
    assert 'class="openrouter-free-guide"' in content
    assert "OpenRouter Free Model Ranking" in content
    assert "OpenRouter scoring metric descriptions" in content
    assert "metadata" in content
    assert "Lite eval score for instruction following" in content
    assert "health status" in content


def test_rules_editor_html_contains_fallback_eval_tab():
    content = Path("static/rules-editor.html").read_text(encoding="utf-8")

    assert 'id="tabFallbackEval"' in content
    assert 'data-tab="fallback-eval"' in content
    assert 'id="editor-container-fallback-eval"' in content
    assert 'id="runFallbackEvalButton"' in content
    assert 'id="fallbackEvalModels"' in content
    assert "Fallback Model Eval" in content
    assert "Fallback model eval metric descriptions" in content
    assert "unique target" in content
    assert "metadata" in content
    assert "health status" in content
