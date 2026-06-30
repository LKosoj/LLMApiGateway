from pathlib import Path


def test_editor_js_contains_provider_models_cache_logic():
    content = Path("static/editor.js").read_text(encoding="utf-8")

    assert "providerModelsCache" in content
    assert "MODELS_CACHE_TTL_MS = 15 * 60 * 1000" in content
    assert "MODEL_ID_COLLATOR" in content
    assert "sortProviderModelIds(models)" in content
    assert "models: sortedModels" in content
    assert "/v1/config/models-rules/structured" in content
    assert "/v1/config/router-rules/structured" in content
    assert "/v1/config/providers/${encodeURIComponent(providerName)}/models" in content
    assert "Choose an available model for provider" in content
    assert "Unavailable fallback models —" in content
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
    assert "upstream-key-pool-input" in content
    assert "upstream_key_pool" in content
    assert "Upstream Key Pool" in content
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
    assert "reasonParts.push(`Gateway models:" in content
    assert "buildRouterCard" in content
    assert "router-selector-model-select" in content
    assert "router-target-type-select" in content
    assert "fallback_entry" in content
    assert "Configured fallback model; metadata score is 0" not in content


def test_editor_js_exposes_provider_field_tooltips_and_upstream_limits_editor():
    content = Path("static/editor.js").read_text(encoding="utf-8")

    assert "PROVIDER_FIELD_TOOLTIPS" in content
    assert "attachFieldTooltip" in content
    assert "buildUpstreamLimitsSection" in content
    assert "splitProviderModelsMetadata" in content
    assert "mergeUpstreamLimitsIntoModels" in content
    assert "upstream-limit-row" in content
    assert "upstream-limit-${key}" in content
    assert "UPSTREAM_LIMIT_KEYS = ['rpm', 'rpd', 'tpm', 'tpd']" in content
    assert "Upstream Limits per Model" in content
    assert "Per-model upstream quota ledger" in content
    assert "Requests per minute allowed per upstream key" in content
    assert "provider-routing-input" in content
    assert "provider-upstream-key-pools-input" in content
    assert "Routing Policy (JSON)" in content
    assert "Upstream Key Pools (JSON)" in content
    assert "session_affinity" in content
    assert "payload-transforms-input" in content
    assert "Payload Transforms" in content
    assert "/v1/config/model-rules" in content


def test_rules_editor_html_contains_model_rules_tab():
    content = Path("static/rules-editor.html").read_text(encoding="utf-8")

    assert "tabModelRules" in content
    assert "editor-container-model-rules" in content
    assert "models_model_rules.json" in content


def test_rules_editor_html_contains_router_tab():
    content = Path("static/rules-editor.html").read_text(encoding="utf-8")

    assert 'id="tabRouter"' in content
    assert 'data-tab="router"' in content
    assert 'id="editor-container-router"' in content
    assert 'id="addRouterButton"' in content
    assert 'id="routerList"' in content
    assert "Router Models" in content
    assert "models_router_rules.json" not in content


def test_editor_css_includes_tooltip_and_upstream_limits_styles():
    content = Path("static/editor.css").read_text(encoding="utf-8")

    assert ".field-tooltip" in content
    assert ".field-tooltip-button" in content
    assert ".field-tooltip-popover" in content
    assert ".upstream-limits-section" in content
    assert ".upstream-limit-row" in content


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
