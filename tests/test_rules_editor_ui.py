from pathlib import Path


EDITOR_SOURCE = Path("frontend/editor/src")


def _editor_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(EDITOR_SOURCE.glob("*.mjs"))
    )


def test_editor_js_contains_provider_models_cache_logic():
    content = _editor_source()

    assert "providerModelsCache" in content
    assert "MODELS_CACHE_TTL_MS: 15 * 60 * 1000" in content
    assert "createLazyProviderCatalogRowController" in content
    assert "loadProviderCatalogsInCard" in content
    assert "Promise.allSettled" in content
    assert "gatewayI18n.subscribe" in content
    assert "_modelLoadPromise" not in content
    assert "Promise.all(modelLoadPromises)" not in content
    assert "gatewayI18n.getCollator" in content
    assert "sortProviderModelIds(models)" in content
    assert "models: sortedModels" in content
    assert "/v1/config/models-rules/structured" in content
    assert "/v1/config/router-rules/structured" in content
    assert "/v1/config/providers/${encodeURIComponent(providerName)}/models" in content
    assert "editor:errors.chooseAvailableModel" in content
    assert "editor:messages.unavailableModels" in content
    assert "context_overflow_fallback" in content
    assert "editor:toggles.contextOverflow" in content
    assert "editor:toggles.contextOverflowEnable" in content
    assert "strip_think_tags" in content
    assert "editor:toggles.stripThink" in content
    assert "max-total-attempts-input" in content
    assert "max_total_attempts" in content
    assert "Max Total Attempts (chain budget)" in content
    assert "use-provider-order-checkbox" in content
    assert "use_provider_order_as_fallback" in content
    assert "editor:toggles.providerOrder" in content
    assert "upstream-key-pool-input" in content
    assert "upstream_key_pool" in content
    assert "Upstream Key Pool" in content
    assert "Number.parseFloat(retryDelayInput.value)" in content
    assert "getEmbeddingsPayloadForSave(ctx.getOperationBasePayload())" in content
    assert "fetchOperationRulesPayload" not in content
    assert "'If-Match': base.etag" in content
    assert "availableProviders: []" in content
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
    assert "editor:eval.gatewayModels" in content
    assert "buildRouterCard" in content
    assert "router-selector-model-select" in content
    assert "router-target-type-select" in content
    assert "fallback_entry" in content
    assert "Configured fallback model; metadata score is 0" not in content


def test_editor_js_exposes_provider_field_tooltips_and_upstream_limits_editor():
    content = _editor_source()

    assert "PROVIDER_FIELD_TOOLTIPS" in content
    assert "attachFieldTooltip" in content
    assert "buildUpstreamLimitsSection" in content
    assert "splitProviderModelsMetadata" in content
    assert "mergeUpstreamLimitsIntoModels" in content
    assert "upstream-limit-row" in content
    assert "upstream-limit-${key}" in content
    assert "UPSTREAM_LIMIT_KEYS = ['rpm', 'rpd', 'tpm', 'tpd']" in content
    assert "editor:fields.upstreamLimits" in content
    assert "editor:tooltips.upstreamLimits" in content
    assert "editor:tooltips.rpm" in content
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
