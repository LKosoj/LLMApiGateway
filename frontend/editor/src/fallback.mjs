export function registerFallback(ctx) {
    const ConfigUiError = ctx.ConfigUiError;
    const LocalizedUiError = ctx.LocalizedUiError;

    function clearUnavailableFallbackModelMetadata(fallbackRow) {
        delete fallbackRow.dataset.unavailableModel;
        delete fallbackRow.dataset.unavailableProvider;
    }

    function getUnavailableFallbackModelDetails(fallbackRow) {
        const unavailableModel = fallbackRow.dataset.unavailableModel?.trim();
        if (!unavailableModel) {
            return null;
        }

        const providerSelect = fallbackRow.querySelector('.provider-select');
        const gatewayModelInput = fallbackRow.closest('.rule-card')?.querySelector('.gateway-model-input');
        return {
            model: unavailableModel,
            provider: (fallbackRow.dataset.unavailableProvider || providerSelect?.value || '').trim(),
            gatewayModel: (gatewayModelInput?.value || '').trim(),
        };
    }

    function collectUnavailableFallbackModels(containerElement) {
        return Array.from(containerElement.querySelectorAll('.fallback-row'))
            .map(getUnavailableFallbackModelDetails)
            .filter(Boolean);
    }

    function formatUnavailableFallbackModelsDetails(unavailableModels) {
        if (!Array.isArray(unavailableModels) || unavailableModels.length === 0) {
            return '';
        }

        const formatTarget = ({ provider, model }) =>
            (provider ? `${provider}.${model}` : `${model}`);

        const groups = new Map();
        unavailableModels.forEach((details) => {
            const gatewayModel = (details.gatewayModel || '').trim();
            if (!groups.has(gatewayModel)) {
                groups.set(gatewayModel, new Set());
            }
            groups.get(gatewayModel).add(formatTarget(details));
        });

        const formattedGroups = Array.from(groups.entries()).map(([gatewayModel, targets]) => {
            const models = Array.from(targets).join(', ');
            return gatewayModel
                ? `${gatewayModel}: ${models}`
                : models;
        });

        return formattedGroups.join('; ');
    }

    function stableSerialize(value) {
        return JSON.stringify(value, null, 2);
    }

    function renderRulesPreview(titleKey, lines, payload) {
        if (!ctx.elements.rulesPreviewArea) return;
        ctx.clearElement(ctx.elements.rulesPreviewArea);
        const heading = document.createElement('strong');
        ctx.bindLocalizedText(heading, titleKey);
        ctx.elements.rulesPreviewArea.appendChild(heading);

        const list = document.createElement('ul');
        const entries = lines.length > 0
            ? lines
            : [{key: 'editor:messages.noChanges', values: {}}];
        entries.forEach(({key, values = {}}) => {
            const item = document.createElement('li');
            ctx.bindLocalizedText(item, key, values);
            list.appendChild(item);
        });
        ctx.elements.rulesPreviewArea.appendChild(list);

        if (payload) {
            const pre = document.createElement('pre');
            pre.textContent = stableSerialize(payload);
            pre.setAttribute('lang', 'und');
            pre.setAttribute('dir', 'auto');
            ctx.elements.rulesPreviewArea.appendChild(pre);
        }
        ctx.elements.rulesPreviewArea.hidden = false;
    }

    function routeKey(route) {
        return `${route.provider || ''}/${route.model || ''}`;
    }

    function previewRulesChanges() {
        let currentPayload;
        try {
            currentPayload = getRulesPayloadForSave();
        } catch (error) {
            ctx.showClientValidationError(error);
            return;
        }

        const previousPayload = ctx.state.originalRulesContent ? JSON.parse(ctx.state.originalRulesContent) : { rules: [] };
        const previousByModel = new Map((previousPayload.rules || []).map(rule => [rule.gateway_model_name, rule]));
        const currentByModel = new Map((currentPayload.rules || []).map(rule => [rule.gateway_model_name, rule]));
        const lines = [];

        currentByModel.forEach((rule, gatewayModel) => {
            const previousRule = previousByModel.get(gatewayModel);
            if (!previousRule) {
                lines.push({
                    key: 'editor:preview.added',
                    values: {model: gatewayModel},
                });
                return;
            }
            const previousOrder = (previousRule.fallback_models || []).map(routeKey).join(' -> ');
            const currentOrder = (rule.fallback_models || []).map(routeKey).join(' -> ');
            if (previousOrder !== currentOrder) {
                lines.push({
                    key: 'editor:preview.order',
                    values: () => ({
                        model: gatewayModel,
                        previous: previousOrder || ctx.t('editor:preview.empty'),
                        current: currentOrder || ctx.t('editor:preview.empty'),
                    }),
                });
            }
            if (Boolean(previousRule.dynamic_penalty) !== Boolean(rule.dynamic_penalty)) {
                lines.push({
                    key: 'editor:preview.penalty',
                    values: () => ({
                        model: gatewayModel,
                        previous: ctx.t(Boolean(previousRule.dynamic_penalty)
                            ? 'editor:preview.enabled'
                            : 'editor:preview.disabled'),
                        current: ctx.t(Boolean(rule.dynamic_penalty)
                            ? 'editor:preview.enabled'
                            : 'editor:preview.disabled'),
                    }),
                });
            }
        });
        previousByModel.forEach((_rule, gatewayModel) => {
            if (!currentByModel.has(gatewayModel)) {
                lines.push({
                    key: 'editor:preview.removed',
                    values: {model: gatewayModel},
                });
            }
        });

        renderRulesPreview('editor:preview.fallbackTitle', lines, currentPayload);
    }

    async function renderSuggestedFallbackOrder() {
        let currentPayload;
        try {
            currentPayload = getRulesPayloadForSave();
        } catch (error) {
            ctx.showClientValidationError(error);
            return;
        }

        try {
            const response = await ctx.apiFetch('/v1/fallback-model-evals');
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(payload.detail || `HTTP ${response.status}`);
            }
            const models = payload.snapshot && Array.isArray(payload.snapshot.models)
                ? payload.snapshot.models
                : [];
            const scoreByTarget = new Map(models.map(model => [`${model.provider}/${model.model}`, Number(model.score) || 0]));
            const suggestions = currentPayload.rules.map(rule => {
                const currentOrder = rule.fallback_models || [];
                const suggestedOrder = [...currentOrder].sort((left, right) => (
                    (scoreByTarget.get(routeKey(right)) || 0) - (scoreByTarget.get(routeKey(left)) || 0)
                ));
                return {
                    gateway_model_name: rule.gateway_model_name,
                    current_order: currentOrder.map(routeKey),
                    suggested_order: suggestedOrder.map(routeKey),
                };
            });
            const lines = suggestions
                .filter(item => item.current_order.join('|') !== item.suggested_order.join('|'))
                .map(item => ({
                    key: 'editor:preview.order',
                    values: () => ({
                        model: item.gateway_model_name,
                        previous: item.current_order.join(' → ') || ctx.t('editor:preview.empty'),
                        current: item.suggested_order.join(' → ') || ctx.t('editor:preview.noSuggestion'),
                    }),
                }));
            renderRulesPreview('editor:preview.suggestedTitle', lines, { suggestions });
        } catch (error) {
            ctx.showLocalizedError('Error loading fallback eval suggestions:', error.message);
        }
    }

    function normalizeFallbackModelForSave(fallbackRow) {
        const providerSelect = fallbackRow.querySelector('.provider-select');
        const modelSelect = fallbackRow.querySelector('.model-select');
        const modelStatus = fallbackRow.querySelector('.model-status');
        const useProviderOrderCheckbox = fallbackRow.querySelector('.use-provider-order-checkbox');
        const providersOrderInput = fallbackRow.querySelector('.providers-order-input');
        const upstreamKeyPoolInput = fallbackRow.querySelector('.upstream-key-pool-input');
        const retryDelayInput = fallbackRow.querySelector('.retry-delay-input');
        const retryCountInput = fallbackRow.querySelector('.retry-count-input');
        const customBodyParamsInput = fallbackRow.querySelector('.custom-body-params-input');
        const customHeadersInput = fallbackRow.querySelector('.custom-headers-input');
        const payloadTransformsInput = fallbackRow.querySelector('.payload-transforms-input');

        const provider = providerSelect.value.trim();
        const model = modelSelect.value.trim();
        const unavailableFallbackModel = getUnavailableFallbackModelDetails(fallbackRow);

        if (!provider) {
            throw new LocalizedUiError('editor:errors.chooseProvider');
        }
        if (fallbackRow.dataset.modelsLoadError === 'true') {
            if (unavailableFallbackModel) {
                throw new LocalizedUiError('editor:errors.chooseAvailableModel', {provider});
            }
            throw new ConfigUiError(
                modelStatus.textContent || `Could not load models for provider '${provider}'.`
            );
        }
        if (!model) {
            throw new LocalizedUiError('editor:errors.chooseAvailableModel', {provider});
        }

        const fallbackModel = {
            provider,
            model,
            use_provider_order_as_fallback: useProviderOrderCheckbox.checked,
            custom_body_params: ctx.parseObjectTextarea(customBodyParamsInput.value, 'Custom body params'),
            custom_headers: ctx.parseObjectTextarea(customHeadersInput.value, 'Custom headers'),
        };
        const payloadTransforms = ctx.parseObjectTextarea(payloadTransformsInput ? payloadTransformsInput.value : '', 'Payload transforms');
        if (Object.keys(payloadTransforms).length > 0) {
            fallbackModel.payload_transforms = payloadTransforms;
        }

        const providersOrder = ctx.parseProvidersOrder(providersOrderInput.value);
        if (providersOrder) {
            fallbackModel.providers_order = providersOrder;
        }
        const upstreamKeyPool = upstreamKeyPoolInput ? upstreamKeyPoolInput.value.trim() : '';
        if (upstreamKeyPool) {
            fallbackModel.upstream_key_pool = upstreamKeyPool;
        }

        ctx.applyRetrySettingsToPayload(fallbackModel, retryDelayInput, retryCountInput);

        return fallbackModel;
    }

    function normalizeRuleCardForSave(ruleCard) {
        const gatewayModelInput = ruleCard.querySelector('.gateway-model-input');
        const rotateModelsCheckbox = ruleCard.querySelector('.rotate-models-checkbox');
        const dynamicPenaltyCheckbox = ruleCard.querySelector('.dynamic-penalty-checkbox');
        const stripThinkTagsCheckbox = ruleCard.querySelector('.strip-think-tags-checkbox');
        const compressToolResultsCheckbox = ruleCard.querySelector('.compress-tool-results-checkbox');
        const maxTotalAttemptsInput = ruleCard.querySelector('.max-total-attempts-input');
        const contextOverflowEnabledCheckbox = ruleCard.querySelector('.context-overflow-enabled-checkbox');
        const contextOverflowRuleSlot = ruleCard.querySelector('.context-overflow-rule-slot');
        const fallbackRows = Array.from(ruleCard.querySelectorAll('.fallback-list > .fallback-row'));

        const gatewayModelName = gatewayModelInput.value.trim();
        if (!gatewayModelName) {
            throw new LocalizedUiError('editor:errors.gatewayName');
        }
        if (fallbackRows.length === 0) {
            throw new LocalizedUiError(
                'editor:errors.fallbackRequired',
                {model: gatewayModelName},
            );
        }

        const normalizedRule = {
            gateway_model_name: gatewayModelName,
            rotate_models: rotateModelsCheckbox.checked,
            dynamic_penalty: Boolean(dynamicPenaltyCheckbox?.checked),
            strip_think_tags: Boolean(stripThinkTagsCheckbox?.checked),
            compress_tool_results: Boolean(compressToolResultsCheckbox?.checked),
            fallback_models: fallbackRows.map(normalizeFallbackModelForSave),
        };

        if (maxTotalAttemptsInput && maxTotalAttemptsInput.value.trim() !== '') {
            const parsed = Number.parseInt(maxTotalAttemptsInput.value, 10);
            if (!Number.isFinite(parsed) || parsed < 0) {
                throw new Error(`Gateway model '${gatewayModelName}' has invalid max_total_attempts (must be a non-negative integer).`);
            }
            normalizedRule.max_total_attempts = parsed;
        }

        if (contextOverflowEnabledCheckbox?.checked) {
            const contextOverflowRow = contextOverflowRuleSlot?.querySelector('.fallback-row');
            if (!contextOverflowRow) {
                throw new Error(`Gateway model '${gatewayModelName}' must define a context overflow fallback model when the special fallback is enabled.`);
            }
            normalizedRule.context_overflow_fallback = normalizeFallbackModelForSave(contextOverflowRow);
        }

        return normalizedRule;
    }

    function getRulesPayloadForSave() {
        const rules = Array.from(ctx.elements.rulesList.querySelectorAll('.rule-card')).map(normalizeRuleCardForSave);
        return { rules };
    }

    function getNormalizedRulesContent() {
        return stableSerialize(getRulesPayloadForSave());
    }

    function snapshotFallbackModelState(fallbackRow) {
        const providerSelect = fallbackRow.querySelector('.provider-select');
        const modelSelect = fallbackRow.querySelector('.model-select');
        const useProviderOrderCheckbox = fallbackRow.querySelector('.use-provider-order-checkbox');
        const providersOrderInput = fallbackRow.querySelector('.providers-order-input');
        const upstreamKeyPoolInput = fallbackRow.querySelector('.upstream-key-pool-input');
        const retryDelayInput = fallbackRow.querySelector('.retry-delay-input');
        const retryCountInput = fallbackRow.querySelector('.retry-count-input');
        const customBodyParamsInput = fallbackRow.querySelector('.custom-body-params-input');
        const customHeadersInput = fallbackRow.querySelector('.custom-headers-input');
        const payloadTransformsInput = fallbackRow.querySelector('.payload-transforms-input');
        const unavailableFallbackModel = getUnavailableFallbackModelDetails(fallbackRow);

        const fallbackModel = {
            provider: providerSelect.value.trim(),
            model: modelSelect.value.trim() || unavailableFallbackModel?.model || '',
            use_provider_order_as_fallback: useProviderOrderCheckbox.checked,
            custom_body_params: ctx.parseObjectTextarea(customBodyParamsInput.value, 'Custom body params'),
            custom_headers: ctx.parseObjectTextarea(customHeadersInput.value, 'Custom headers'),
        };
        const payloadTransforms = ctx.parseObjectTextarea(payloadTransformsInput ? payloadTransformsInput.value : '', 'Payload transforms');
        if (Object.keys(payloadTransforms).length > 0) {
            fallbackModel.payload_transforms = payloadTransforms;
        }

        const providersOrder = ctx.parseProvidersOrder(providersOrderInput.value);
        if (providersOrder) {
            fallbackModel.providers_order = providersOrder;
        }
        const upstreamKeyPool = upstreamKeyPoolInput ? upstreamKeyPoolInput.value.trim() : '';
        if (upstreamKeyPool) {
            fallbackModel.upstream_key_pool = upstreamKeyPool;
        }

        ctx.applyRetrySettingsToPayload(fallbackModel, retryDelayInput, retryCountInput);
        return fallbackModel;
    }

    function getRulesSnapshotPayload() {
        const rules = Array.from(ctx.elements.rulesList.querySelectorAll('.rule-card')).map(ruleCard => {
            const gatewayModelInput = ruleCard.querySelector('.gateway-model-input');
            const rotateModelsCheckbox = ruleCard.querySelector('.rotate-models-checkbox');
            const dynamicPenaltyCheckbox = ruleCard.querySelector('.dynamic-penalty-checkbox');
            const stripThinkTagsCheckbox = ruleCard.querySelector('.strip-think-tags-checkbox');
            const compressToolResultsCheckbox = ruleCard.querySelector('.compress-tool-results-checkbox');
            const maxTotalAttemptsInput = ruleCard.querySelector('.max-total-attempts-input');
            const contextOverflowEnabledCheckbox = ruleCard.querySelector('.context-overflow-enabled-checkbox');
            const contextOverflowRuleSlot = ruleCard.querySelector('.context-overflow-rule-slot');
            const fallbackRows = Array.from(ruleCard.querySelectorAll('.fallback-list > .fallback-row'));

            const normalizedRule = {
                gateway_model_name: gatewayModelInput.value.trim(),
                rotate_models: rotateModelsCheckbox.checked,
                dynamic_penalty: Boolean(dynamicPenaltyCheckbox?.checked),
                strip_think_tags: Boolean(stripThinkTagsCheckbox?.checked),
                compress_tool_results: Boolean(compressToolResultsCheckbox?.checked),
                fallback_models: fallbackRows.map(snapshotFallbackModelState),
            };

            if (maxTotalAttemptsInput && maxTotalAttemptsInput.value.trim() !== '') {
                const parsed = Number.parseInt(maxTotalAttemptsInput.value, 10);
                if (Number.isFinite(parsed) && parsed >= 0) {
                    normalizedRule.max_total_attempts = parsed;
                }
            }

            if (contextOverflowEnabledCheckbox?.checked) {
                const contextOverflowRow = contextOverflowRuleSlot?.querySelector('.fallback-row');
                if (contextOverflowRow) {
                    normalizedRule.context_overflow_fallback = snapshotFallbackModelState(contextOverflowRow);
                }
            }

            return normalizedRule;
        });

        return { rules };
    }

    function getRulesSnapshotContent() {
        return stableSerialize(getRulesSnapshotPayload());
    }

    function setFallbackRowStatus(fallbackRow, statusText, state) {
        const modelStatus = fallbackRow.querySelector('.model-status');
        modelStatus.textContent = statusText || '';
        modelStatus.dataset.state = state || 'idle';
    }

    function buildFallbackRow(initialData, options = {}) {
        const fallbackRow = document.createElement('div');
        fallbackRow.className = 'fallback-row';
        fallbackRow.dataset.modelsLoadError = 'false';

        ctx.setupRowReordering(fallbackRow);

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid';

        const providerSelect = ctx.createSelect('provider-select');
        ctx.setSelectOptions(providerSelect, ctx.state.availableProviders, 'Choose a provider', initialData.provider || '');

        const modelSelect = ctx.createSelect('model-select');
        modelSelect.disabled = !initialData.provider;
        ctx.setSelectOptions(
            modelSelect,
            initialData.model ? [initialData.model] : [],
            initialData.provider ? 'Choose a model' : 'Choose a provider first',
            initialData.model || '',
        );

        const useProviderOrderCheckbox = document.createElement('input');
        useProviderOrderCheckbox.type = 'checkbox';
        useProviderOrderCheckbox.className = 'use-provider-order-checkbox';
        useProviderOrderCheckbox.checked = Boolean(initialData.use_provider_order_as_fallback);

        const rotateToggle = document.createElement('label');
        rotateToggle.className = 'toggle-field';
        rotateToggle.appendChild(useProviderOrderCheckbox);
        const toggleText = document.createElement('span');
        ctx.bindLocalizedText(toggleText, 'editor:toggles.providerOrder');
        rotateToggle.appendChild(toggleText);

        const providersOrderInput = ctx.createTextInput('providers-order-input', 'provider-a, provider-b');
        providersOrderInput.value = Array.isArray(initialData.providers_order) ? initialData.providers_order.join(', ') : '';

        const upstreamKeyPoolInput = ctx.createTextInput('upstream-key-pool-input', 'main');
        upstreamKeyPoolInput.value = initialData.upstream_key_pool || '';

        const retryDelayInput = ctx.createNumberInput('retry-delay-input', 'Retry delay (seconds)');
        retryDelayInput.value = initialData.retry_delay ?? '';

        const retryCountInput = ctx.createNumberInput('retry-count-input', 'Retry count');
        retryCountInput.value = initialData.retry_count ?? '';

        const customBodyParamsInput = ctx.createTextarea('custom-body-params-input', '{"temperature": 0.2}');
        customBodyParamsInput.value = ctx.normalizeObjectTextarea(initialData.custom_body_params);

        const customHeadersInput = ctx.createTextarea('custom-headers-input', '{"X-Provider": "value"}');
        customHeadersInput.value = ctx.normalizeObjectTextarea(initialData.custom_headers);

        const payloadTransformsInput = ctx.createTextarea('payload-transforms-input', '{"defaults": {"top_p": 0.9}, "overrides": {}, "filters": ["seed"]}');
        payloadTransformsInput.value = ctx.normalizeObjectTextarea(initialData.payload_transforms);

        fieldsGrid.appendChild(ctx.createFieldGroup('Provider', providerSelect, 'provider-field'));
        fieldsGrid.appendChild(ctx.createFieldGroup('Model', modelSelect, 'model-field'));
        fieldsGrid.appendChild(ctx.createFieldGroup('Provider Order', providersOrderInput));
        fieldsGrid.appendChild(ctx.createFieldGroup('Upstream Key Pool', upstreamKeyPoolInput));
        fieldsGrid.appendChild(ctx.createFieldGroup('Retry Delay', retryDelayInput));
        fieldsGrid.appendChild(ctx.createFieldGroup('Retry Count', retryCountInput));

        const modelStatus = document.createElement('div');
        modelStatus.className = 'model-status';
        modelStatus.dataset.state = 'idle';

        const advancedDetails = document.createElement('details');
        advancedDetails.className = 'advanced-options';
        const advancedSummary = document.createElement('summary');
        ctx.bindLocalizedText(advancedSummary, 'editor:actions.advanced');
        advancedDetails.appendChild(advancedSummary);

        const advancedGrid = document.createElement('div');
        advancedGrid.className = 'advanced-grid';
        advancedGrid.appendChild(ctx.createFieldGroup('', rotateToggle, 'toggle-group'));
        advancedGrid.appendChild(ctx.createFieldGroup('Custom Body Params', customBodyParamsInput, 'textarea-group'));
        advancedGrid.appendChild(ctx.createFieldGroup('Custom Headers', customHeadersInput, 'textarea-group'));
        advancedGrid.appendChild(ctx.createFieldGroup('Payload Transforms', payloadTransformsInput, 'textarea-group'));
        advancedDetails.appendChild(advancedGrid);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, options.removeButtonLabel || 'Remove Fallback');
        removeButton.addEventListener('click', () => {
            if (typeof options.onRemove === 'function') {
                options.onRemove(fallbackRow);
                return;
            }
            const parentRuleCard = fallbackRow.closest('.rule-card');
            fallbackRow.remove();
            if (parentRuleCard) {
                const fallbackContainer = parentRuleCard.querySelector('.fallback-list');
                if (fallbackContainer.children.length === 0) {
                    const addFallbackButton = parentRuleCard.querySelector('.add-fallback-button');
                    if (addFallbackButton) {
                        addFallbackButton.focus();
                    }
                }
            }
        });

        const rowActions = document.createElement('div');
        rowActions.className = 'fallback-row-actions';
        
        const { moveUpButton, moveDownButton } = ctx.createMoveButtons(fallbackRow);
        rowActions.appendChild(moveUpButton);
        rowActions.appendChild(moveDownButton);
        rowActions.appendChild(removeButton);

        fallbackRow.appendChild(fieldsGrid);
        fallbackRow.appendChild(modelStatus);
        fallbackRow.appendChild(advancedDetails);
        fallbackRow.appendChild(rowActions);

        const catalogController = ctx.createLazyProviderCatalogRowController({
            row: fallbackRow,
            providerSelect,
            modelControl: modelSelect,
            modelStatus,
            requireListedModel: true,
        });

        modelSelect.addEventListener('change', () => {
            // Picking an available model clears a previous "model unavailable"
            // error so the row can be saved (the options list only contains
            // models the provider actually exposes).
            if (modelSelect.value) {
                catalogController.markSelected(modelSelect.value);
            }
        });
        return fallbackRow;
    }

    function buildRuleCard(initialData) {
        const ruleCard = document.createElement('section');
        ruleCard.className = 'rule-card';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';

        const gatewayModelInput = ctx.createTextInput('gateway-model-input', 'llmgateway/model-name');
        gatewayModelInput.value = initialData.gateway_model_name || '';
        titleWrap.appendChild(ctx.createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const rotateModelsCheckbox = document.createElement('input');
        rotateModelsCheckbox.type = 'checkbox';
        rotateModelsCheckbox.className = 'rotate-models-checkbox';
        rotateModelsCheckbox.checked = Boolean(initialData.rotate_models);

        const rotateToggle = document.createElement('label');
        rotateToggle.className = 'toggle-field rotate-toggle';
        rotateToggle.appendChild(rotateModelsCheckbox);
        const rotateLabel = document.createElement('span');
        ctx.bindLocalizedText(rotateLabel, 'editor:toggles.rotate');
        rotateToggle.appendChild(rotateLabel);
        titleWrap.appendChild(rotateToggle);

        const dynamicPenaltyCheckbox = document.createElement('input');
        dynamicPenaltyCheckbox.type = 'checkbox';
        dynamicPenaltyCheckbox.className = 'dynamic-penalty-checkbox';
        dynamicPenaltyCheckbox.checked = Boolean(initialData.dynamic_penalty);

        const dynamicPenaltyToggle = document.createElement('label');
        dynamicPenaltyToggle.className = 'toggle-field';
        dynamicPenaltyToggle.appendChild(dynamicPenaltyCheckbox);
        const dynamicPenaltyLabel = document.createElement('span');
        ctx.bindLocalizedText(dynamicPenaltyLabel, 'editor:toggles.dynamicPenalty');
        dynamicPenaltyToggle.appendChild(dynamicPenaltyLabel);
        titleWrap.appendChild(dynamicPenaltyToggle);

        const stripThinkTagsCheckbox = document.createElement('input');
        stripThinkTagsCheckbox.type = 'checkbox';
        stripThinkTagsCheckbox.className = 'strip-think-tags-checkbox';
        stripThinkTagsCheckbox.checked = Boolean(initialData.strip_think_tags);

        const stripThinkTagsToggle = document.createElement('label');
        stripThinkTagsToggle.className = 'toggle-field';
        stripThinkTagsToggle.appendChild(stripThinkTagsCheckbox);
        const stripThinkTagsLabel = document.createElement('span');
        ctx.bindLocalizedText(stripThinkTagsLabel, 'editor:toggles.stripThink');
        stripThinkTagsToggle.appendChild(stripThinkTagsLabel);
        titleWrap.appendChild(stripThinkTagsToggle);

        const compressToolResultsCheckbox = document.createElement('input');
        compressToolResultsCheckbox.type = 'checkbox';
        compressToolResultsCheckbox.className = 'compress-tool-results-checkbox';
        compressToolResultsCheckbox.checked = Boolean(initialData.compress_tool_results);

        const compressToolResultsToggle = document.createElement('label');
        compressToolResultsToggle.className = 'toggle-field';
        compressToolResultsToggle.appendChild(compressToolResultsCheckbox);
        const compressToolResultsLabel = document.createElement('span');
        ctx.bindLocalizedText(compressToolResultsLabel, 'editor:toggles.compressTools');
        compressToolResultsToggle.appendChild(compressToolResultsLabel);
        titleWrap.appendChild(compressToolResultsToggle);

        const maxTotalAttemptsInput = document.createElement('input');
        maxTotalAttemptsInput.type = 'number';
        maxTotalAttemptsInput.className = 'max-total-attempts-input';
        maxTotalAttemptsInput.min = '0';
        maxTotalAttemptsInput.step = '1';
        ctx.bindKnownPlaceholder(maxTotalAttemptsInput, 'unlimited');
        if (Number.isFinite(initialData.max_total_attempts)) {
            maxTotalAttemptsInput.value = String(initialData.max_total_attempts);
        }
        titleWrap.appendChild(
            ctx.createFieldGroup(
                'Max Total Attempts (chain budget)',
                maxTotalAttemptsInput,
                'max-total-attempts-field',
            ),
        );

        const removeRuleButton = document.createElement('button');
        removeRuleButton.type = 'button';
        removeRuleButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeRuleButton, 'Remove Rule');
        removeRuleButton.addEventListener('click', () => {
            ruleCard.remove();
            ctx.refreshRulesEmptyState();
        });

        cardHeader.appendChild(titleWrap);
        cardHeader.appendChild(removeRuleButton);

        const fallbackList = document.createElement('div');
        fallbackList.className = 'fallback-list';

        const contextOverflowSection = document.createElement('section');
        contextOverflowSection.className = 'context-overflow-section';

        const contextOverflowHeader = document.createElement('div');
        contextOverflowHeader.className = 'context-overflow-header';

        const contextOverflowCopy = document.createElement('div');
        contextOverflowCopy.className = 'context-overflow-copy';

        const contextOverflowTitle = document.createElement('strong');
        ctx.bindLocalizedText(contextOverflowTitle, 'editor:toggles.contextOverflow');
        const contextOverflowDescription = document.createElement('span');
        ctx.bindLocalizedText(contextOverflowDescription, 'editor:hints.contextOverflow');
        contextOverflowCopy.appendChild(contextOverflowTitle);
        contextOverflowCopy.appendChild(contextOverflowDescription);

        const contextOverflowEnabledCheckbox = document.createElement('input');
        contextOverflowEnabledCheckbox.type = 'checkbox';
        contextOverflowEnabledCheckbox.className = 'context-overflow-enabled-checkbox';
        contextOverflowEnabledCheckbox.checked = Boolean(initialData.context_overflow_fallback);

        const contextOverflowToggle = document.createElement('label');
        contextOverflowToggle.className = 'toggle-field';
        contextOverflowToggle.appendChild(contextOverflowEnabledCheckbox);
        const contextOverflowToggleLabel = document.createElement('span');
        ctx.bindLocalizedText(contextOverflowToggleLabel, 'editor:toggles.contextOverflowEnable');
        contextOverflowToggle.appendChild(contextOverflowToggleLabel);

        contextOverflowHeader.appendChild(contextOverflowCopy);
        contextOverflowHeader.appendChild(contextOverflowToggle);

        const contextOverflowRuleSlot = document.createElement('div');
        contextOverflowRuleSlot.className = 'context-overflow-rule-slot';
        contextOverflowRuleSlot.hidden = !initialData.context_overflow_fallback;

        let contextOverflowRow = null;
        const ensureContextOverflowRow = () => {
            if (contextOverflowRow) {
                return contextOverflowRow;
            }

            contextOverflowRow = buildFallbackRow(initialData.context_overflow_fallback || {}, {
                removeButtonLabel: 'Disable Special Fallback',
                onRemove: (row) => {
                    row.remove();
                    contextOverflowRow = null;
                    contextOverflowEnabledCheckbox.checked = false;
                    contextOverflowRuleSlot.hidden = true;
                },
            });
            contextOverflowRuleSlot.appendChild(contextOverflowRow);
            return contextOverflowRow;
        };

        const addFallbackButton = document.createElement('button');
        addFallbackButton.type = 'button';
        addFallbackButton.className = 'secondary-button add-fallback-button';
        ctx.bindKnownActionText(addFallbackButton, 'Add Fallback Model');
        addFallbackButton.addEventListener('click', () => {
            fallbackList.appendChild(buildFallbackRow({}));
        });

        const cardBody = document.createElement('div');
        cardBody.className = 'rule-card-body';
        
        cardBody.appendChild(fallbackList);
        cardBody.appendChild(addFallbackButton);
        cardBody.appendChild(contextOverflowSection);

        const accordionToggle = document.createElement('button');
        accordionToggle.type = 'button';
        accordionToggle.className = 'accordion-toggle';
        // Create SVG safely without innerHTML to prevent XSS
        const svgNS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('width', '20');
        svg.setAttribute('height', '20');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        const polyline = document.createElementNS(svgNS, 'polyline');
        polyline.setAttribute('points', '6 9 12 15 18 9');
        svg.appendChild(polyline);
        accordionToggle.appendChild(svg);
        accordionToggle.addEventListener('click', () => {
            ctx.toggleProviderCatalogCard(ruleCard);
        });

        const headerLeft = document.createElement('div');
        headerLeft.className = 'rule-card-header-left';
        headerLeft.appendChild(accordionToggle);
        headerLeft.appendChild(titleWrap);

        // Clear cardHeader safely without innerHTML
        while (cardHeader.firstChild) {
            cardHeader.removeChild(cardHeader.firstChild);
        }
        cardHeader.appendChild(headerLeft);
        cardHeader.appendChild(removeRuleButton);

        ruleCard.classList.add('collapsed'); // Default to closed
        ruleCard.appendChild(cardHeader);
        ruleCard.appendChild(cardBody);

        const fallbackModels = Array.isArray(initialData.fallback_models) ? initialData.fallback_models : [];
        fallbackModels.forEach(fallbackModel => {
            const fallbackRow = buildFallbackRow(fallbackModel);
            fallbackList.appendChild(fallbackRow);
        });

        contextOverflowSection.appendChild(contextOverflowHeader);
        contextOverflowSection.appendChild(contextOverflowRuleSlot);

        if (initialData.context_overflow_fallback) {
            ensureContextOverflowRow();
        }

        contextOverflowEnabledCheckbox.addEventListener('change', () => {
            if (contextOverflowEnabledCheckbox.checked) {
                ensureContextOverflowRow();
                contextOverflowRuleSlot.hidden = false;
                return;
            }

            contextOverflowRuleSlot.hidden = true;
            return undefined;
        });

        if (fallbackModels.length === 0) {
            const fallbackRow = buildFallbackRow({});
            fallbackList.appendChild(fallbackRow);
        }

        return ruleCard;
    }

    function renderRules(rules) {
        ctx.elements.rulesList.textContent = '';

        if (!Array.isArray(rules) || rules.length === 0) {
            ctx.refreshRulesEmptyState();
            return;
        }

        rules.forEach(rule => {
            const ruleCard = buildRuleCard(rule);
            ctx.elements.rulesList.appendChild(ruleCard);
        });
        ctx.refreshRulesEmptyState();
    }

    async function loadRulesEditor() {
        ctx.showLocalizedMessage('info', 'Loading Fallback Rules...');
        try {
            const loaded = await ctx.loadConfigDocument(
                'fallback',
                '/v1/config/models-rules/structured',
                {
                    validate: payload => ctx.validateFallbackPayload(payload, true),
                    apply: async payload => {
                        ctx.state.availableProviders = payload.providers;
                        await renderRules(payload.rules);
                    },
                }
            );
            if (!loaded) {
                if (ctx.state.activeEditor === 'rules') {
                    ctx.showLocalizedMessage('warning', 'A newer local edit was preserved. Reload again to discard it.');
                }
                return false;
            }
            ctx.state.originalRulesContent = getRulesSnapshotContent();
            ctx.updateSaveButtonDisabledState();
            const unavailableFallbackModels = collectUnavailableFallbackModels(ctx.elements.rulesList);
            if (unavailableFallbackModels.length > 0) {
                if (ctx.state.activeEditor === 'rules') {
                    const unavailableDetails = formatUnavailableFallbackModelsDetails(
                        unavailableFallbackModels,
                    );
                    ctx.showLocalizedMessage(
                        'warning',
                        'editor:messages.loadedWithWarnings',
                        () => ({
                            details: ctx.t('editor:messages.unavailableModels', {
                                details: unavailableDetails,
                            }),
                        }),
                    );
                }
                return true;
            }
            if (ctx.state.activeEditor === 'rules') {
                ctx.showLocalizedMessage('success', 'Fallback Rules loaded successfully.');
            }
            return true;
        } catch (error) {
            console.error('Error fetching Fallback Rules:', error);
            if (ctx.state.activeEditor === 'rules') {
                ctx.showLocalizedError('Error loading Fallback Rules:', error);
            }
            ctx.state.originalRulesContent = null;
            ctx.state.documentBases.set('fallback', null);
            ctx.updateSaveButtonDisabledState();
            return false;
        }
    }

    async function ensureAvailableProvidersLoaded() {
        if (ctx.state.availableProviders.length !== 0) {
            return;
        }

        const rulesResp = await ctx.apiFetch('/v1/config/models-rules/structured');
        const rulesPayload = await rulesResp.json();
        if (!rulesResp.ok) {
            throw new Error(rulesPayload.detail || `HTTP ${rulesResp.status}`);
        }
        ctx.state.availableProviders = Array.isArray(rulesPayload.providers) ? rulesPayload.providers : [];
    }

    const EVAL_LABEL_KEYS = new Map([
        ['Status', 'editor:eval.status'],
        ['Last checked', 'editor:eval.lastChecked'],
        ['Last updated', 'editor:eval.lastUpdated'],
        ['Next refresh', 'editor:eval.nextRefresh'],
        ['Catalog models', 'editor:eval.catalogModels'],
        ['Eligible models', 'editor:eval.eligibleModels'],
        ['Unique targets', 'editor:eval.uniqueTargets'],
        ['Lite evals', 'editor:eval.liteEvals'],
        ['Last error', 'editor:eval.lastError'],
        ['Refresh mode', 'editor:eval.refreshMode'],
        ['Manual refresh', 'editor:eval.manualRefresh'],
        ['metadata', 'editor:eval.metadata'],
        ['health', 'editor:eval.health'],
        ['latency', 'editor:eval.latency'],
        ['eval', 'editor:eval.eval'],
        ['penalty', 'editor:eval.penalty'],
        ['latency ms', 'editor:eval.latencyMs'],
        ['context', 'editor:eval.context'],
        ['health status', 'editor:eval.healthStatus'],
    ]);

    const EVAL_STATE_KEYS = new Map([
        ['Running', 'editor:messages.statusRunning'],
        ['Idle', 'editor:messages.statusIdle'],
        ['n/a', 'editor:messages.notApplicable'],
        ['passed', 'editor:eval.statePassed'],
        ['imperfect', 'editor:eval.stateImperfect'],
        ['timeout', 'editor:eval.stateTimeout'],
        ['http_429', 'editor:eval.stateRateLimited'],
        ['rate_limited', 'editor:eval.stateRateLimited'],
        ['missing_provider', 'editor:eval.stateMissingProvider'],
        ['not_probed', 'editor:eval.stateNotProbed'],
        ['fullEval', 'editor:eval.refreshFullEval'],
        ['latencyOnly', 'editor:eval.refreshLatencyOnly'],
        ['manualEval', 'editor:eval.refreshManualEval'],
    ]);

    function formatEvalValue(value) {
        const resolved = typeof value === 'function' ? value() : value;
        const stateKey = EVAL_STATE_KEYS.get(resolved);
        return stateKey ? ctx.t(stateKey) : String(resolved ?? '');
    }

    function appendOpenRouterMeta(parent, label, value, raw = false) {
        const item = document.createElement('div');
        item.className = 'openrouter-free-meta-item';
        const labelElement = document.createElement('strong');
        const labelKey = EVAL_LABEL_KEYS.get(label);
        if (labelKey) {
            ctx.bindLocalizedText(labelElement, labelKey);
        } else {
            labelElement.textContent = label;
        }
        const valueElement = document.createElement('span');
        ctx.bindLocalizedValue(valueElement, () => formatEvalValue(value));
        if (raw) {
            valueElement.setAttribute('lang', 'und');
            valueElement.setAttribute('dir', 'auto');
        }
        item.appendChild(labelElement);
        item.appendChild(valueElement);
        parent.appendChild(item);
    }

    function appendEvalMetric(parent, label, value) {
        const metric = document.createElement('span');
        const labelElement = document.createElement('span');
        ctx.bindLocalizedText(labelElement, EVAL_LABEL_KEYS.get(label));
        const separator = document.createTextNode(': ');
        const valueElement = document.createElement('span');
        ctx.bindLocalizedValue(valueElement, () => (
            typeof value === 'number' ? ctx.formatNumber(value) : formatEvalValue(value || 'n/a')
        ));
        if (typeof value !== 'number' && !EVAL_STATE_KEYS.has(value) && value) {
            valueElement.setAttribute('lang', 'und');
            valueElement.setAttribute('dir', 'auto');
        }
        metric.appendChild(labelElement);
        metric.appendChild(separator);
        metric.appendChild(valueElement);
        parent.appendChild(metric);
    }

    function renderOpenRouterFreeModels(payload) {
        ctx.clearElement(ctx.elements.openRouterFreeStatus);
        ctx.clearElement(ctx.elements.openRouterFreeModels);

        const snapshot = payload.snapshot;
        if (!snapshot) {
            ctx.elements.openRouterFreeEmptyState.hidden = false;
            appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, 'Status', () => payload.lastError || ctx.t('editor:eval.waitingFirst'), Boolean(payload.lastError));
            appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, 'Next refresh', () => ctx.formatDateTime(payload.nextRefreshAt));
            return;
        }

        ctx.elements.openRouterFreeEmptyState.hidden = Array.isArray(snapshot.models) && snapshot.models.length > 0;

        appendOpenRouterMeta(
            ctx.elements.openRouterFreeStatus,
            'Refresh mode',
            () => snapshot.refreshMode || 'n/a',
            Boolean(snapshot.refreshMode && !EVAL_STATE_KEYS.has(snapshot.refreshMode)),
        );
        appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, 'Manual refresh', () => payload.manualRefreshRunning ? 'Running' : 'Idle');
        appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, 'Last updated', () => ctx.formatDateTime(snapshot.updatedAt));
        appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, 'Next refresh', () => ctx.formatDateTime(payload.nextRefreshAt));
        appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, 'Catalog models', () => ctx.formatNumber(snapshot.catalogCount));
        appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, 'Eligible models', () => ctx.formatNumber(snapshot.eligibleCount));
        appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, 'Lite evals', () => ctx.formatNumber(snapshot.evaluatedCount));
        if (payload.lastError) {
            appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, 'Last error', payload.lastError, true);
        }

        (snapshot.models || []).forEach(model => {
            const card = document.createElement('article');
            card.className = 'openrouter-free-card';

            const header = document.createElement('div');
            header.className = 'openrouter-free-card-header';

            const title = document.createElement('div');
            const rank = document.createElement('div');
            rank.className = 'openrouter-free-rank';
            ctx.bindLocalizedValue(rank, () => (
                Number.isFinite(Number(model.rank))
                    ? `#${ctx.formatNumber(Number(model.rank))}`
                    : '#?'
            ));
            const name = document.createElement('strong');
            ctx.bindLocalizedValue(name, () => model.name || model.id || ctx.t('editor:messages.unknown'));
            name.setAttribute('lang', 'und');
            name.setAttribute('dir', 'auto');
            const id = document.createElement('code');
            id.textContent = model.id || '';
            title.appendChild(rank);
            title.appendChild(name);
            title.appendChild(id);

            const score = document.createElement('div');
            score.className = 'openrouter-free-score';
            ctx.bindLocalizedValue(score, () => ctx.formatNumber(model.score));

            header.appendChild(title);
            header.appendChild(score);
            card.appendChild(header);

            const reason = document.createElement('p');
            reason.className = 'openrouter-free-reason';
            if (model.reason) {
                reason.textContent = model.reason;
                reason.setAttribute('lang', 'und');
                reason.setAttribute('dir', 'auto');
            } else {
                ctx.bindLocalizedText(reason, 'editor:eval.freeTextModel');
            }
            card.appendChild(reason);

            const metrics = document.createElement('div');
            metrics.className = 'openrouter-free-metrics';
            [
                ['metadata', model.metadataScore],
                ['health', model.healthScore],
                ['latency', model.latencyScore],
                ['eval', model.liteEvalScore],
                ['penalty', model.instabilityPenalty],
                ['latency ms', model.latencyMs],
                ['context', model.contextLength],
                ['health status', model.healthStatus],
            ].forEach(([label, value]) => {
                appendEvalMetric(metrics, label, value);
            });
            card.appendChild(metrics);

            ctx.elements.openRouterFreeModels.appendChild(card);
        });
    }

    function stopOpenRouterFreePolling() {
        ctx.state.openRouterFreePollingEnabled = false;
        if (ctx.state.openRouterFreePollTimer) {
            clearTimeout(ctx.state.openRouterFreePollTimer);
            ctx.state.openRouterFreePollTimer = null;
        }
    }

    function isRulesTabContextCurrent(tabName, context) {
        return ctx.state.activeEditor === tabName
            && context !== null
            && !context.signal.aborted
            && context.isCurrent();
    }

    function scheduleOpenRouterFreePolling(payload, context) {
        stopOpenRouterFreePolling();
        if (!isRulesTabContextCurrent('openrouter-free', context)) {
            return;
        }
        if (ctx.elements.runOpenRouterFreeEvalButton) {
            ctx.elements.runOpenRouterFreeEvalButton.disabled = Boolean(payload.manualRefreshRunning);
        }
        if (payload.manualRefreshRunning) {
            ctx.state.openRouterFreePollingEnabled = true;
            ctx.state.openRouterFreePollTimer = window.setTimeout(() => {
                void loadOpenRouterFreeModels(false, ctx.state.activeRulesTabContext);
            }, 3000);
        }
    }

    async function loadOpenRouterFreeModels(showMessage = true, context = ctx.state.activeRulesTabContext) {
        if (!isRulesTabContextCurrent('openrouter-free', context)) {
            return false;
        }
        if (showMessage) {
            ctx.state.evalTabLoadStates.set('openrouter-free', 'loading');
            ctx.showLocalizedMessage('info', 'Loading OpenRouter free model ranking...');
            ctx.clearElement(ctx.elements.openRouterFreeStatus);
            ctx.clearElement(ctx.elements.openRouterFreeModels);
            ctx.elements.openRouterFreeEmptyState.hidden = true;
        }
        try {
            const response = await ctx.apiFetch('/v1/openrouter/free-models');
            const payload = await response.json();
            if (!isRulesTabContextCurrent('openrouter-free', context)) {
                return false;
            }
            if (!response.ok) {
                throw new Error(payload.detail || `HTTP ${response.status}`);
            }
            if (!payload.configured) {
                ctx.state.evalTabLoadStates.set('openrouter-free', 'idle');
                ctx.elements.tabOpenRouterFree.hidden = true;
                await ctx.state.rulesTabsController.repair();
                return false;
            }
            renderOpenRouterFreeModels(payload);
            ctx.state.evalTabLoadStates.set('openrouter-free', 'ready');
            scheduleOpenRouterFreePolling(payload, context);
            if (showMessage) {
                ctx.showLocalizedMessage('success', 'OpenRouter free model ranking loaded.');
            }
            return true;
        } catch (error) {
            if (!isRulesTabContextCurrent('openrouter-free', context)) {
                return false;
            }
            ctx.state.evalTabLoadStates.set('openrouter-free', 'error');
            ctx.state.openRouterFreePollingEnabled = false;
            console.error('Error loading OpenRouter free model ranking:', error);
            ctx.showLocalizedError('Error loading OpenRouter free model ranking:', error.message);
            ctx.elements.openRouterFreeEmptyState.hidden = false;
            if (ctx.elements.runOpenRouterFreeEvalButton) {
                ctx.elements.runOpenRouterFreeEvalButton.disabled = false;
            }
            return false;
        }
    }

    async function runOpenRouterFreeEval() {
        if (!ctx.elements.runOpenRouterFreeEvalButton) return;
        ctx.elements.runOpenRouterFreeEvalButton.disabled = true;
        ctx.showLocalizedMessage('info', 'Starting OpenRouter free model full eval...');
        try {
            const response = await ctx.apiFetch('/v1/openrouter/free-models/run', { method: 'POST' });
            const payload = await response.json().catch(() => ({}));
            const context = ctx.state.activeRulesTabContext;
            if (!isRulesTabContextCurrent('openrouter-free', context)) {
                return;
            }
            if (!response.ok) {
                throw new Error(payload.detail || `HTTP ${response.status}`);
            }
            renderOpenRouterFreeModels(payload);
            scheduleOpenRouterFreePolling(payload, context);
            ctx.showLocalizedMessage('success', 'OpenRouter free model full eval started.');
        } catch (error) {
            const context = ctx.state.activeRulesTabContext;
            if (!isRulesTabContextCurrent('openrouter-free', context)) {
                return;
            }
            console.error('Error starting OpenRouter free model full eval:', error);
            ctx.showLocalizedError('Error starting OpenRouter free model full eval:', error.message);
            ctx.elements.runOpenRouterFreeEvalButton.disabled = false;
        }
    }

    async function initializeOpenRouterFreeTabAvailability() {
        try {
            const response = await ctx.apiFetch('/v1/openrouter/free-models');
            const payload = await response.json().catch(() => ({}));
            ctx.elements.tabOpenRouterFree.hidden = !response.ok || !payload.configured;
        } catch (error) {
            ctx.elements.tabOpenRouterFree.hidden = true;
        }
        await ctx.state.rulesTabsController.repair();
    }

    function renderFallbackEvalModels(payload) {
        ctx.clearElement(ctx.elements.fallbackEvalStatus);
        ctx.clearElement(ctx.elements.fallbackEvalModels);

        const snapshot = payload.snapshot;
        ctx.elements.fallbackEvalEmptyState.hidden = Boolean(snapshot && Array.isArray(snapshot.models) && snapshot.models.length > 0);

        appendOpenRouterMeta(ctx.elements.fallbackEvalStatus, 'Status', () => payload.running ? 'Running' : 'Idle');
        appendOpenRouterMeta(ctx.elements.fallbackEvalStatus, 'Last checked', () => ctx.formatDateTime(payload.lastCheckedAt));
        if (snapshot) {
            appendOpenRouterMeta(ctx.elements.fallbackEvalStatus, 'Unique targets', () => ctx.formatNumber(snapshot.configuredCount));
            appendOpenRouterMeta(ctx.elements.fallbackEvalStatus, 'Lite evals', () => ctx.formatNumber(snapshot.evaluatedCount));
            appendOpenRouterMeta(ctx.elements.fallbackEvalStatus, 'Last updated', () => ctx.formatDateTime(snapshot.updatedAt));
        }
        if (payload.lastError) {
            appendOpenRouterMeta(ctx.elements.fallbackEvalStatus, 'Last error', payload.lastError, true);
        }

        if (!snapshot) {
            return;
        }

        (snapshot.models || []).forEach(model => {
            const card = document.createElement('article');
            card.className = 'openrouter-free-card';

            const header = document.createElement('div');
            header.className = 'openrouter-free-card-header';

            const title = document.createElement('div');
            const rank = document.createElement('div');
            rank.className = 'openrouter-free-rank';
            ctx.bindLocalizedValue(rank, () => (
                Number.isFinite(Number(model.rank))
                    ? `#${ctx.formatNumber(Number(model.rank))}`
                    : '#?'
            ));
            const name = document.createElement('strong');
            ctx.bindLocalizedValue(
                name,
                () => model.name || model.model || model.id || ctx.t('editor:messages.unknown'),
            );
            name.setAttribute('lang', 'und');
            name.setAttribute('dir', 'auto');
            const id = document.createElement('code');
            id.textContent = `${model.provider || ''} / ${model.model || model.id || ''}`;
            title.appendChild(rank);
            title.appendChild(name);
            title.appendChild(id);

            const score = document.createElement('div');
            score.className = 'openrouter-free-score';
            ctx.bindLocalizedValue(score, () => ctx.formatNumber(model.score));

            header.appendChild(title);
            header.appendChild(score);
            card.appendChild(header);

            const reason = document.createElement('p');
            reason.className = 'openrouter-free-reason';
            if (Array.isArray(model.gatewayModels) && model.gatewayModels.length > 0) {
                const gateways = document.createElement('span');
                ctx.bindLocalizedText(
                    gateways,
                    'editor:eval.gatewayModels',
                    () => ({models: model.gatewayModels.join(', ')}),
                );
                reason.appendChild(gateways);
            }
            if (model.reason) {
                const rawReason = document.createElement('span');
                rawReason.textContent = model.reason;
                rawReason.setAttribute('lang', 'und');
                rawReason.setAttribute('dir', 'auto');
                reason.appendChild(document.createTextNode(' '));
                reason.appendChild(rawReason);
            }
            if (!reason.childNodes.length) {
                ctx.bindLocalizedText(reason, 'editor:eval.configuredTarget');
            }
            card.appendChild(reason);

            const metrics = document.createElement('div');
            metrics.className = 'openrouter-free-metrics';
            [
                ['metadata', model.metadataScore],
                ['health', model.healthScore],
                ['latency', model.latencyScore],
                ['eval', model.liteEvalScore],
                ['penalty', model.instabilityPenalty],
                ['latency ms', model.latencyMs],
                ['context', model.contextLength],
                ['health status', model.healthStatus],
            ].forEach(([label, value]) => {
                appendEvalMetric(metrics, label, value);
            });
            card.appendChild(metrics);

            ctx.elements.fallbackEvalModels.appendChild(card);
        });
    }

    function stopFallbackEvalPolling() {
        ctx.state.fallbackEvalPollingEnabled = false;
        if (ctx.state.fallbackEvalPollTimer) {
            clearTimeout(ctx.state.fallbackEvalPollTimer);
            ctx.state.fallbackEvalPollTimer = null;
        }
    }

    function scheduleFallbackEvalPolling(payload, context) {
        stopFallbackEvalPolling();
        if (!isRulesTabContextCurrent('fallback-eval', context)) {
            return;
        }
        ctx.elements.runFallbackEvalButton.disabled = Boolean(payload.running);
        if (payload.running) {
            ctx.state.fallbackEvalPollingEnabled = true;
            ctx.state.fallbackEvalPollTimer = window.setTimeout(() => {
                void loadFallbackModelEvals(false, ctx.state.activeRulesTabContext);
            }, 3000);
        }
    }

    async function loadFallbackModelEvals(showMessage = true, context = ctx.state.activeRulesTabContext) {
        if (!isRulesTabContextCurrent('fallback-eval', context)) {
            return false;
        }
        if (showMessage) {
            ctx.state.evalTabLoadStates.set('fallback-eval', 'loading');
            ctx.showLocalizedMessage('info', 'Loading fallback model eval status...');
        }
        try {
            const response = await ctx.apiFetch('/v1/fallback-model-evals');
            const payload = await response.json();
            if (!isRulesTabContextCurrent('fallback-eval', context)) {
                return false;
            }
            if (!response.ok) {
                throw new Error(payload.detail || `HTTP ${response.status}`);
            }
            renderFallbackEvalModels(payload);
            ctx.state.evalTabLoadStates.set('fallback-eval', 'ready');
            scheduleFallbackEvalPolling(payload, context);
            if (showMessage) {
                ctx.showLocalizedMessage('success', 'Fallback model eval status loaded.');
            }
            return true;
        } catch (error) {
            if (!isRulesTabContextCurrent('fallback-eval', context)) {
                return false;
            }
            ctx.state.evalTabLoadStates.set('fallback-eval', 'error');
            ctx.state.fallbackEvalPollingEnabled = false;
            console.error('Error loading fallback model eval status:', error);
            ctx.showLocalizedError('Error loading fallback model eval status:', error.message);
            ctx.elements.fallbackEvalEmptyState.hidden = false;
            ctx.elements.runFallbackEvalButton.disabled = false;
            return false;
        }
    }

    async function runFallbackModelEval() {
        ctx.elements.runFallbackEvalButton.disabled = true;
        ctx.showLocalizedMessage('info', 'Starting fallback model eval...');
        try {
            const response = await ctx.apiFetch('/v1/fallback-model-evals/run', { method: 'POST' });
            const payload = await response.json().catch(() => ({}));
            const context = ctx.state.activeRulesTabContext;
            if (!isRulesTabContextCurrent('fallback-eval', context)) {
                return;
            }
            if (!response.ok) {
                throw new Error(payload.detail || `HTTP ${response.status}`);
            }
            renderFallbackEvalModels(payload);
            scheduleFallbackEvalPolling(payload, context);
            ctx.showLocalizedMessage('success', 'Fallback model eval started.');
        } catch (error) {
            const context = ctx.state.activeRulesTabContext;
            if (!isRulesTabContextCurrent('fallback-eval', context)) {
                return;
            }
            console.error('Error starting fallback model eval:', error);
            ctx.showLocalizedError('Error starting fallback model eval:', error.message);
            ctx.elements.runFallbackEvalButton.disabled = false;
        }
    }

    async function saveRules() {
        const unavailableFallbackModels = collectUnavailableFallbackModels(ctx.elements.rulesList);
        if (unavailableFallbackModels.length > 0) {
            const unavailableDetails = formatUnavailableFallbackModelsDetails(
                unavailableFallbackModels,
            );
            ctx.showLocalizedMessage(
                'error',
                'editor:messages.unavailableModels',
                {details: unavailableDetails},
            );
            return;
        }

        let payload;
        try {
            payload = getRulesPayloadForSave();
        } catch (error) {
            ctx.showClientValidationError(error);
            return;
        }

        ctx.elements.saveButton.disabled = true;
        ctx.showLocalizedMessage('info', 'Saving Fallback Rules...');

        try {
            const result = await ctx.saveConfigDocument(
                'fallback',
                '/v1/config/models-rules/structured',
                payload,
                {
                    errorTitle: 'Error saving Fallback Rules:',
                    extractPublishedPayload: body => ({ rules: body.rules }),
                    validatePublished: published => ctx.validateFallbackPayload(published, false),
                }
            );
            if (!result) {
                return;
            }
            if (ctx.state.editorMutationVersion === result.submittedMutationVersion) {
                const application = renderRules(result.payload.rules);
                ctx.syncInteractionLock();
                await application;
                ctx.state.originalRulesContent = getRulesSnapshotContent();
            }
            ctx.showLocalizedMessage(
                'success',
                ctx.safeSuccessMessage(result.body, 'Fallback Rules updated successfully.')
            );
        } catch (error) {
            console.error('Error saving Fallback Rules:', error);
            ctx.showLocalizedError('Error saving Fallback Rules:', error);
        } finally {
            ctx.updateSaveButtonDisabledState();
        }
    }

    Object.assign(ctx, {
        clearUnavailableFallbackModelMetadata,
        getUnavailableFallbackModelDetails,
        collectUnavailableFallbackModels,
        formatUnavailableFallbackModelsDetails,
        stableSerialize,
        renderRulesPreview,
        routeKey,
        previewRulesChanges,
        renderSuggestedFallbackOrder,
        normalizeFallbackModelForSave,
        normalizeRuleCardForSave,
        getRulesPayloadForSave,
        getNormalizedRulesContent,
        snapshotFallbackModelState,
        getRulesSnapshotPayload,
        getRulesSnapshotContent,
        setFallbackRowStatus,
        buildFallbackRow,
        buildRuleCard,
        renderRules,
        loadRulesEditor,
        ensureAvailableProvidersLoaded,
        formatEvalValue,
        appendOpenRouterMeta,
        appendEvalMetric,
        renderOpenRouterFreeModels,
        stopOpenRouterFreePolling,
        isRulesTabContextCurrent,
        scheduleOpenRouterFreePolling,
        loadOpenRouterFreeModels,
        runOpenRouterFreeEval,
        initializeOpenRouterFreeTabAvailability,
        renderFallbackEvalModels,
        stopFallbackEvalPolling,
        scheduleFallbackEvalPolling,
        loadFallbackModelEvals,
        runFallbackModelEval,
        saveRules,
    });
}
