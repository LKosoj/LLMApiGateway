export function registerOperations(ctx) {
    const { gatewayI18n } = ctx;
    const ConfigUiError = ctx.ConfigUiError;

    function normalizeOperationRulesPayload(payload = {}) {
        const normalized = {
            embeddings: Array.isArray(payload.embeddings) ? payload.embeddings : [],
            rerank: Array.isArray(payload.rerank) ? payload.rerank : [],
            images_generations: Array.isArray(payload.images_generations) ? payload.images_generations : [],
            images_edits: Array.isArray(payload.images_edits) ? payload.images_edits : [],
        };
        if (Object.prototype.hasOwnProperty.call(payload, 'audio_transcriptions')) {
            normalized.audio_transcriptions = Array.isArray(payload.audio_transcriptions)
                ? payload.audio_transcriptions
                : [];
        }
        ['audio_speech', 'pdf_conversions'].forEach((sectionName) => {
            if (Object.prototype.hasOwnProperty.call(payload, sectionName)) {
                normalized[sectionName] = Array.isArray(payload[sectionName]) ? payload[sectionName] : [];
            }
        });
        ['web_search', 'web_read', 'web_research', 'web_deep_research'].forEach((sectionName) => {
            if (Object.prototype.hasOwnProperty.call(payload, sectionName)) {
                normalized[sectionName] = Array.isArray(payload[sectionName]) ? payload[sectionName] : [];
            }
        });
        return normalized;
    }

    function applyOperationRulesPayload(payload = {}) {
        const normalized = normalizeOperationRulesPayload(payload);
        ctx.state.embeddingRules = normalized.embeddings;
        ctx.state.rerankRules = normalized.rerank;
        ctx.state.imageGenerationRules = normalized.images_generations;
        ctx.state.imageEditRules = normalized.images_edits;
        ctx.state.audioSpeechRules = normalized.audio_speech || [];
        ctx.state.audioTranscriptionRules = normalized.audio_transcriptions || [];
        ctx.state.pdfConversionRules = normalized.pdf_conversions || [];
        ctx.state.webSearchRules = normalized.web_search || [];
        ctx.state.webReadRules = normalized.web_read || [];
        ctx.state.webResearchRules = normalized.web_research || [];
        ctx.state.webDeepResearchRules = normalized.web_deep_research || [];
        return normalized;
    }

    function buildOperationRoutesPayload(overrides = {}, basePayload = null) {
        const source = basePayload ? normalizeOperationRulesPayload(basePayload) : {
            embeddings: ctx.state.embeddingRules,
            rerank: ctx.state.rerankRules,
            images_generations: ctx.state.imageGenerationRules,
            images_edits: ctx.state.imageEditRules,
        };
        if (!basePayload && ctx.state.audioTranscriptionRules.length > 0) {
            source.audio_transcriptions = ctx.state.audioTranscriptionRules;
        }
        if (!basePayload && ctx.state.audioSpeechRules.length > 0) {
            source.audio_speech = ctx.state.audioSpeechRules;
        }
        if (!basePayload && ctx.state.pdfConversionRules.length > 0) {
            source.pdf_conversions = ctx.state.pdfConversionRules;
        }
        const payload = {
            embeddings: overrides.embeddings ?? source.embeddings,
            rerank: overrides.rerank ?? source.rerank,
            images_generations: overrides.images_generations ?? source.images_generations,
            images_edits: overrides.images_edits ?? source.images_edits,
        };
        if (
            Object.prototype.hasOwnProperty.call(overrides, 'audio_transcriptions')
            || Object.prototype.hasOwnProperty.call(source, 'audio_transcriptions')
            || ctx.state.audioTranscriptionRules.length > 0
        ) {
            payload.audio_transcriptions = overrides.audio_transcriptions ?? (source.audio_transcriptions || []);
        }
        ['audio_speech', 'pdf_conversions'].forEach((sectionName) => {
            if (
                Object.prototype.hasOwnProperty.call(overrides, sectionName)
                || Object.prototype.hasOwnProperty.call(source, sectionName)
            ) {
                payload[sectionName] = overrides[sectionName] ?? (source[sectionName] || []);
            }
        });
        ['web_search', 'web_read', 'web_research', 'web_deep_research'].forEach((sectionName) => {
            if (
                Object.prototype.hasOwnProperty.call(overrides, sectionName)
                || Object.prototype.hasOwnProperty.call(source, sectionName)
            ) {
                payload[sectionName] = overrides[sectionName] ?? (source[sectionName] || []);
            }
        });
        return payload;
    }

    Theme.attachToggle('darkModeToggle');

    async function loadOperationRulesPayload(configName, applyPayload) {
        ctx.showLocalizedMessage('info', `Loading ${configName}...`);
        return ctx.loadConfigDocument(
            'operation',
            '/v1/config/model-operations/structured',
            {
                validate: ctx.validateOperationPayload,
                apply: async payload => {
                    await ctx.ensureAvailableProvidersLoaded();
                    applyOperationRulesPayload(payload);
                    await applyPayload(payload);
                },
            }
        );
    }

    async function saveOperationPayload(payload, errorTitle, applyPublished) {
        const result = await ctx.saveConfigDocument(
            'operation',
            '/v1/config/model-operations/structured',
            payload,
            {
                errorTitle,
                validatePublished: ctx.validateOperationPayload,
            }
        );
        if (!result) {
            return null;
        }
        if (ctx.state.editorMutationVersion === result.submittedMutationVersion) {
            applyOperationRulesPayload(result.payload);
            const application = applyPublished(result.payload);
            ctx.syncInteractionLock();
            await application;
        }
        return result;
    }

    function collectCurrentWebSectionModels(listElement) {
        return Array.from(listElement.querySelectorAll('.rule-card > .rule-card-header .gateway-model-input'))
            .map(input => input.value.trim())
            .filter(Boolean);
    }

    function refreshWebCrossDropdowns() {
        ctx.state.gatewayModelCatalog.web_search = collectCurrentWebSectionModels(ctx.elements.webSearchList);
        ctx.state.gatewayModelCatalog.web_read = collectCurrentWebSectionModels(ctx.elements.webReadList);

        const crossSelectors = [
            { selector: '.search-model-input', options: ctx.state.gatewayModelCatalog.web_search },
            { selector: '.read-model-input', options: ctx.state.gatewayModelCatalog.web_read },
        ];
        [ctx.elements.webResearchList, ctx.elements.webDeepResearchList].forEach(list => {
            crossSelectors.forEach(({ selector, options }) => {
                list.querySelectorAll(selector).forEach(select => {
                    if (select.tagName !== 'SELECT') return;
                    ctx.setModelSelectOptions(select, options, select.value);
                });
            });
        });

        const chatSelects = [
            ...ctx.elements.webSearchList.querySelectorAll('.query-model-input'),
        ];
        chatSelects.forEach(select => {
            if (select.tagName !== 'SELECT') return;
            ctx.setModelSelectOptions(select, ctx.state.gatewayModelCatalog.chat, select.value);
        });
    }

    async function loadGatewayModelCatalog() {
        const response = await ctx.apiFetch('/v1/config/models-rules/structured');
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.detail || `HTTP ${response.status}`);
        }
        const rules = Array.isArray(payload.rules) ? payload.rules : [];
        ctx.state.gatewayModelCatalog.chat = rules
            .map(rule => typeof rule.gateway_model_name === 'string' ? rule.gateway_model_name.trim() : '')
            .filter(Boolean);
    }

    function applyOperationCatalog(normalizedPayload) {
        ctx.state.gatewayModelCatalog.embeddings = (normalizedPayload.embeddings || [])
            .map(item => typeof item.gateway_model_name === 'string' ? item.gateway_model_name.trim() : '')
            .filter(Boolean);
        ctx.state.gatewayModelCatalog.rerank = (normalizedPayload.rerank || [])
            .map(item => typeof item.gateway_model_name === 'string' ? item.gateway_model_name.trim() : '')
            .filter(Boolean);
        ctx.state.gatewayModelCatalog.images_generations = (normalizedPayload.images_generations || [])
            .map(item => typeof item.gateway_model_name === 'string' ? item.gateway_model_name.trim() : '')
            .filter(Boolean);
        ctx.state.gatewayModelCatalog.web_search = (normalizedPayload.web_search || [])
            .map(item => typeof item.gateway_model_name === 'string' ? item.gateway_model_name.trim() : '')
            .filter(Boolean);
        ctx.state.gatewayModelCatalog.web_read = (normalizedPayload.web_read || [])
            .map(item => typeof item.gateway_model_name === 'string' ? item.gateway_model_name.trim() : '')
            .filter(Boolean);
    }

    function getEmbeddingsPayloadForSave(basePayload = null) {
        const embeddings = Array.from(ctx.elements.embeddingsList.querySelectorAll('.rule-card')).map(normalizeEmbeddingCardForSave);
        return buildOperationRoutesPayload({ embeddings }, basePayload);
    }

    function getNormalizedEmbeddingsContent() {
        return ctx.stableSerialize(getEmbeddingsPayloadForSave());
    }

    function normalizeEmbeddingRouteForSave(routeRow) {
        const providerSelect = routeRow.querySelector('.provider-select');
        const modelInput = routeRow.querySelector('.model-input');
        const customBodyParamsInput = routeRow.querySelector('.custom-body-params-input');
        const customHeadersInput = routeRow.querySelector('.custom-headers-input');
        const targetPathInput = routeRow.querySelector('.target-path-input');
        const retryDelayInput = routeRow.querySelector('.retry-delay-input');
        const retryCountInput = routeRow.querySelector('.retry-count-input');

        const provider = providerSelect.value.trim();
        const model = modelInput.value.trim();

        if (!provider) {
            throw new Error('Each embedding route must have a provider selected.');
        }
        if (!model) {
            throw new Error(`Enter or choose a model for provider '${provider}' before saving.`);
        }

        const routePayload = {
            provider,
            model,
            target_path: targetPathInput.value.trim() || '/embeddings',
            custom_body_params: ctx.parseObjectTextarea(customBodyParamsInput.value, 'Custom body params'),
            custom_headers: ctx.parseObjectTextarea(customHeadersInput.value, 'Custom headers'),
        };

        ctx.applyRetrySettingsToPayload(routePayload, retryDelayInput, retryCountInput);

        return routePayload;
    }

    function normalizeEmbeddingCardForSave(ruleCard) {
        const gatewayModelInput = ruleCard.querySelector('.gateway-model-input');
        const routeRows = Array.from(ruleCard.querySelectorAll('.fallback-list > .fallback-row'));

        const gatewayModelName = gatewayModelInput.value.trim();
        if (!gatewayModelName) {
            throw new Error('Each embedding model rule must have a gateway model name.');
        }
        if (routeRows.length === 0) {
            throw new Error(`Embedding model '${gatewayModelName}' must contain at least one route.`);
        }

        return {
            gateway_model_name: gatewayModelName,
            routes: routeRows.map(normalizeEmbeddingRouteRowForSave),
        };
    }

    function normalizeEmbeddingRouteRowForSave(routeRow) {
        // Similar to normalizeEmbeddingRouteForSave but specifically for the UI structure
        return normalizeEmbeddingRouteForSave(routeRow);
    }

    async function loadEmbeddingsEditor() {
        try {
            const loaded = await loadOperationRulesPayload(
                'Embeddings Routes',
                async payload => {
                    await renderEmbeddings(payload.embeddings);
                }
            );
            if (!loaded) {
                ctx.showLocalizedMessage('warning', 'A newer local edit was preserved. Reload again to discard it.');
                return false;
            }
            ctx.state.originalEmbeddingsContent = getNormalizedEmbeddingsContent();
            ctx.updateSaveButtonDisabledState();
            ctx.showLocalizedMessage('success', 'Embeddings Routes loaded successfully.');
            return true;
        } catch (error) {
            console.error('Error fetching Embeddings Routes:', error);
            ctx.showLocalizedError('Error loading Embeddings Routes:', error);
            ctx.state.originalEmbeddingsContent = null;
            ctx.updateSaveButtonDisabledState();
            return false;
        }
    }

    function renderEmbeddings(embeddings) {
        ctx.elements.embeddingsList.textContent = '';

        if (!Array.isArray(embeddings) || embeddings.length === 0) {
            ctx.refreshEmbeddingsEmptyState();
            return;
        }

        embeddings.forEach(embedding => {
            const embeddingCard = buildEmbeddingCard(embedding);
            ctx.elements.embeddingsList.appendChild(embeddingCard);
        });
        ctx.refreshEmbeddingsEmptyState();
    }

    function buildEmbeddingCard(initialData) {
        const card = document.createElement('section');
        card.className = 'rule-card';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';

        const gatewayModelInput = ctx.createTextInput('gateway-model-input', 'llmgateway/embedding-model');
        gatewayModelInput.value = initialData.gateway_model_name || '';
        titleWrap.appendChild(ctx.createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, 'Remove Model');
        removeButton.addEventListener('click', () => {
            card.remove();
            ctx.refreshEmbeddingsEmptyState();
        });

        cardHeader.appendChild(titleWrap);
        cardHeader.appendChild(removeButton);

        const routeList = document.createElement('div');
        routeList.className = 'fallback-list';

        const addRouteButton = document.createElement('button');
        addRouteButton.type = 'button';
        addRouteButton.className = 'secondary-button add-fallback-button';
        ctx.bindKnownActionText(addRouteButton, 'Add Fallback Route');
        addRouteButton.addEventListener('click', () => {
            routeList.appendChild(buildEmbeddingRouteRow({}));
        });

        const cardBody = document.createElement('div');
        cardBody.className = 'rule-card-body';
        cardBody.appendChild(routeList);
        cardBody.appendChild(addRouteButton);

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
            ctx.toggleProviderCatalogCard(card);
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
        cardHeader.appendChild(removeButton);

        card.classList.add('collapsed');
        card.appendChild(cardHeader);
        card.appendChild(cardBody);

        const routes = Array.isArray(initialData.routes) ? initialData.routes : [];
        routes.forEach(route => {
            const routeRow = buildEmbeddingRouteRow(route);
            routeList.appendChild(routeRow);
        });

        if (routes.length === 0) {
            const routeRow = buildEmbeddingRouteRow({});
            routeList.appendChild(routeRow);
        }

        return card;
    }

    function buildEmbeddingRouteRow(initialData) {
        const row = document.createElement('div');
        row.className = 'fallback-row';

        ctx.setupRowReordering(row);

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid';

        const providerSelect = ctx.createSelect('provider-select');
        ctx.setSelectOptions(providerSelect, ctx.state.availableProviders, 'Choose a provider', initialData.provider || '');

        // Use datalist for model input to allow both selection and manual input
        const modelInput = ctx.createTextInput('model-input', 'Choose or enter model');
        modelInput.value = initialData.model || '';
        const dataListId = `models-list-${Math.random().toString(36).substr(2, 9)}`;
        modelInput.setAttribute('list', dataListId);
        const dataList = document.createElement('datalist');
        dataList.id = dataListId;
        row.appendChild(dataList);

        const targetPathInput = ctx.createTextInput('target-path-input', '/embeddings');
        targetPathInput.value = initialData.target_path || '/embeddings';
        targetPathInput.readOnly = true;
        const { retryDelayInput, retryCountInput } = ctx.createRetrySettingsInputs(initialData);

        const customBodyParamsInput = ctx.createTextarea('custom-body-params-input', '{"param": "value"}');
        customBodyParamsInput.value = ctx.normalizeObjectTextarea(initialData.custom_body_params);

        const customHeadersInput = ctx.createTextarea('custom-headers-input', '{"X-Header": "value"}');
        customHeadersInput.value = ctx.normalizeObjectTextarea(initialData.custom_headers);

        fieldsGrid.appendChild(ctx.createFieldGroup('Provider', providerSelect, 'provider-field'));
        fieldsGrid.appendChild(ctx.createFieldGroup('Model', modelInput, 'model-field'));
        fieldsGrid.appendChild(ctx.createFieldGroup('Target Path', targetPathInput));

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
        advancedGrid.appendChild(ctx.createFieldGroup('Retry Delay', retryDelayInput));
        advancedGrid.appendChild(ctx.createFieldGroup('Retry Count', retryCountInput));
        advancedGrid.appendChild(ctx.createFieldGroup('Custom Body Params', customBodyParamsInput, 'textarea-group'));
        advancedGrid.appendChild(ctx.createFieldGroup('Custom Headers', customHeadersInput, 'textarea-group'));
        advancedDetails.appendChild(advancedGrid);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, 'Remove Fallback Route');
        removeButton.addEventListener('click', () => {
            row.remove();
        });

        const rowActions = document.createElement('div');
        rowActions.className = 'fallback-row-actions';
        
        const { moveUpButton, moveDownButton } = ctx.createMoveButtons(row);
        rowActions.appendChild(moveUpButton);
        rowActions.appendChild(moveDownButton);
        rowActions.appendChild(removeButton);

        row.appendChild(fieldsGrid);
        row.appendChild(modelStatus);
        row.appendChild(advancedDetails);
        row.appendChild(rowActions);

        ctx.createLazyProviderCatalogRowController({
            row,
            providerSelect,
            modelControl: modelInput,
            dataList,
            modelStatus,
        });

        return row;
    }

    async function loadRerankEditor() {
        try {
            const loaded = await loadOperationRulesPayload(
                'Rerank Routes',
                async payload => {
                    await renderRerank(payload.rerank);
                }
            );
            if (!loaded) {
                ctx.showLocalizedMessage('warning', 'A newer local edit was preserved. Reload again to discard it.');
                return false;
            }
            ctx.state.originalRerankContent = getNormalizedRerankContent();
            ctx.updateSaveButtonDisabledState();
            ctx.showLocalizedMessage('success', 'Rerank Routes loaded successfully.');
            return true;
        } catch (error) {
            console.error('Error fetching Rerank Routes:', error);
            ctx.showLocalizedError('Error loading Rerank Routes:', error);
            ctx.state.originalRerankContent = null;
            ctx.updateSaveButtonDisabledState();
            return false;
        }
    }

    function renderRerank(rerank) {
        ctx.elements.rerankList.textContent = '';

        if (!Array.isArray(rerank) || rerank.length === 0) {
            ctx.refreshRerankEmptyState();
            return;
        }

        rerank.forEach(item => {
            const rerankCard = buildRerankCard(item);
            ctx.elements.rerankList.appendChild(rerankCard);
        });
        ctx.refreshRerankEmptyState();
    }

    function buildRerankCard(initialData) {
        const card = document.createElement('section');
        card.className = 'rule-card';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';

        const gatewayModelInput = ctx.createTextInput('gateway-model-input', 'llmgateway/rerank-model');
        gatewayModelInput.value = initialData.gateway_model_name || '';
        titleWrap.appendChild(ctx.createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, 'Remove Model');
        removeButton.addEventListener('click', () => {
            card.remove();
            ctx.refreshRerankEmptyState();
        });

        cardHeader.appendChild(titleWrap);
        cardHeader.appendChild(removeButton);

        const routeList = document.createElement('div');
        routeList.className = 'fallback-list';

        const addRouteButton = document.createElement('button');
        addRouteButton.type = 'button';
        addRouteButton.className = 'secondary-button add-fallback-button';
        ctx.bindKnownActionText(addRouteButton, 'Add Fallback Route');
        addRouteButton.addEventListener('click', () => {
            routeList.appendChild(buildRerankRouteRow({}));
        });

        const cardBody = document.createElement('div');
        cardBody.className = 'rule-card-body';
        cardBody.appendChild(routeList);
        cardBody.appendChild(addRouteButton);

        const accordionToggle = document.createElement('button');
        accordionToggle.type = 'button';
        accordionToggle.className = 'accordion-toggle';
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
            ctx.toggleProviderCatalogCard(card);
        });

        const headerLeft = document.createElement('div');
        headerLeft.className = 'rule-card-header-left';
        headerLeft.appendChild(accordionToggle);
        headerLeft.appendChild(titleWrap);

        while (cardHeader.firstChild) {
            cardHeader.removeChild(cardHeader.firstChild);
        }
        cardHeader.appendChild(headerLeft);
        cardHeader.appendChild(removeButton);

        card.classList.add('collapsed');
        card.appendChild(cardHeader);
        card.appendChild(cardBody);

        const routes = Array.isArray(initialData.routes) ? initialData.routes : [];
        routes.forEach(route => {
            const routeRow = buildRerankRouteRow(route);
            routeList.appendChild(routeRow);
        });

        if (routes.length === 0) {
            const routeRow = buildRerankRouteRow({});
            routeList.appendChild(routeRow);
        }

        return card;
    }

    function buildRerankRouteRow(initialData) {
        const row = document.createElement('div');
        row.className = 'fallback-row';

        ctx.setupRowReordering(row);

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid';

        const providerSelect = ctx.createSelect('provider-select');
        ctx.setSelectOptions(providerSelect, ctx.state.availableProviders, 'Choose a provider', initialData.provider || '');

        const modelInput = ctx.createTextInput('model-input', 'Choose or enter model');
        modelInput.value = initialData.model || '';
        const dataListId = `rerank-models-list-${Math.random().toString(36).substr(2, 9)}`;
        modelInput.setAttribute('list', dataListId);
        const dataList = document.createElement('datalist');
        dataList.id = dataListId;
        row.appendChild(dataList);

        const targetPathInput = ctx.createTextInput('target-path-input', '/score');
        targetPathInput.value = initialData.target_path || '/score';
        const requestFormatSelect = ctx.createSelect('request-format-select');
        ctx.setSelectOptions(requestFormatSelect, ['query_passages', 'query_texts'], 'Default request format', initialData.request_format || '');
        const responseFormatSelect = ctx.createSelect('response-format-select');
        ctx.setSelectOptions(responseFormatSelect, ['rankings_logit', 'scores'], 'Default response format', initialData.response_format || '');
        const responseOutputFormatSelect = ctx.createSelect('response-output-format-select');
        ctx.setSelectOptions(
            responseOutputFormatSelect,
            ['jina_results'],
            'Default output format',
            initialData.response_output_format || ''
        );
        const { retryDelayInput, retryCountInput } = ctx.createRetrySettingsInputs(initialData);

        const customBodyParamsInput = ctx.createTextarea('custom-body-params-input', '{"param": "value"}');
        customBodyParamsInput.value = ctx.normalizeObjectTextarea(initialData.custom_body_params);

        const customHeadersInput = ctx.createTextarea('custom-headers-input', '{"X-Header": "value"}');
        customHeadersInput.value = ctx.normalizeObjectTextarea(initialData.custom_headers);

        fieldsGrid.appendChild(ctx.createFieldGroup('Provider', providerSelect, 'provider-field'));
        fieldsGrid.appendChild(ctx.createFieldGroup('Model', modelInput, 'model-field'));
        fieldsGrid.appendChild(ctx.createFieldGroup('Target Path', targetPathInput));

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
        advancedGrid.appendChild(ctx.createFieldGroup('Request Format', requestFormatSelect));
        advancedGrid.appendChild(ctx.createFieldGroup('Response Format', responseFormatSelect));
        advancedGrid.appendChild(ctx.createFieldGroup('Response Output Format', responseOutputFormatSelect));
        advancedGrid.appendChild(ctx.createFieldGroup('Retry Delay', retryDelayInput));
        advancedGrid.appendChild(ctx.createFieldGroup('Retry Count', retryCountInput));
        advancedGrid.appendChild(ctx.createFieldGroup('Custom Body Params', customBodyParamsInput, 'textarea-group'));
        advancedGrid.appendChild(ctx.createFieldGroup('Custom Headers', customHeadersInput, 'textarea-group'));
        advancedDetails.appendChild(advancedGrid);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, 'Remove Fallback Route');
        removeButton.addEventListener('click', () => {
            row.remove();
        });

        const rowActions = document.createElement('div');
        rowActions.className = 'fallback-row-actions';
        
        const { moveUpButton, moveDownButton } = ctx.createMoveButtons(row);
        rowActions.appendChild(moveUpButton);
        rowActions.appendChild(moveDownButton);
        rowActions.appendChild(removeButton);

        row.appendChild(fieldsGrid);
        row.appendChild(modelStatus);
        row.appendChild(advancedDetails);
        row.appendChild(rowActions);

        ctx.createLazyProviderCatalogRowController({
            row,
            providerSelect,
            modelControl: modelInput,
            dataList,
            modelStatus,
        });

        return row;
    }

    function getRerankPayloadForSave(basePayload = null) {
        const rerank = Array.from(ctx.elements.rerankList.querySelectorAll('.rule-card')).map(normalizeRerankCardForSave);
        return buildOperationRoutesPayload({ rerank }, basePayload);
    }

    function getNormalizedRerankContent() {
        return ctx.stableSerialize(getRerankPayloadForSave());
    }

    function normalizeRerankRouteForSave(routeRow) {
        const providerSelect = routeRow.querySelector('.provider-select');
        const modelInput = routeRow.querySelector('.model-input');
        const customBodyParamsInput = routeRow.querySelector('.custom-body-params-input');
        const customHeadersInput = routeRow.querySelector('.custom-headers-input');
        const targetPathInput = routeRow.querySelector('.target-path-input');
        const requestFormatSelect = routeRow.querySelector('.request-format-select');
        const responseFormatSelect = routeRow.querySelector('.response-format-select');
        const responseOutputFormatSelect = routeRow.querySelector('.response-output-format-select');
        const retryDelayInput = routeRow.querySelector('.retry-delay-input');
        const retryCountInput = routeRow.querySelector('.retry-count-input');

        const provider = providerSelect.value.trim();
        const model = modelInput.value.trim();
        const target_path = targetPathInput.value.trim();
        const request_format = requestFormatSelect.value.trim();
        const response_format = responseFormatSelect.value.trim();
        const response_output_format = responseOutputFormatSelect.value.trim();

        if (!provider) {
            throw new Error('Each rerank route must have a provider selected.');
        }
        if (!model) {
            throw new Error(`Enter or choose a model for provider '${provider}' before saving.`);
        }
        if (!target_path) {
            throw new Error('Target path is required.');
        }
        if (!target_path.startsWith('/') && !/^https?:\/\//i.test(target_path)) {
            throw new Error('Target path must start with / or with http:// or https://');
        }

        const routePayload = {
            provider,
            model,
            target_path,
            custom_body_params: ctx.parseObjectTextarea(customBodyParamsInput.value, 'Custom body params'),
            custom_headers: ctx.parseObjectTextarea(customHeadersInput.value, 'Custom headers'),
        };

        if (request_format) {
            routePayload.request_format = request_format;
        }
        if (response_format) {
            routePayload.response_format = response_format;
        }
        if (response_output_format) {
            routePayload.response_output_format = response_output_format;
        }
        ctx.applyRetrySettingsToPayload(routePayload, retryDelayInput, retryCountInput);

        return routePayload;
    }

    function normalizeRerankCardForSave(ruleCard) {
        const gatewayModelInput = ruleCard.querySelector('.gateway-model-input');
        const routeRows = Array.from(ruleCard.querySelectorAll('.fallback-list > .fallback-row'));

        const gatewayModelName = gatewayModelInput.value.trim();
        if (!gatewayModelName) {
            throw new Error('Each rerank model rule must have a gateway model name.');
        }
        if (routeRows.length === 0) {
            throw new Error(`Rerank model '${gatewayModelName}' must contain at least one route.`);
        }

        return {
            gateway_model_name: gatewayModelName,
            routes: routeRows.map(normalizeRerankRouteForSave),
        };
    }

    async function saveRerank() {
        ctx.elements.saveButton.disabled = true;
        ctx.showLocalizedMessage('info', 'Saving Rerank Routes...');

        let payload;
        try {
            payload = getRerankPayloadForSave(ctx.getOperationBasePayload());
        } catch (error) {
            ctx.showClientValidationError(error);
            return;
        }

        try {
            const result = await saveOperationPayload(
                payload,
                'Error saving Rerank Routes:',
                () => {
                    ctx.state.originalRerankContent = getNormalizedRerankContent();
                }
            );
            if (!result) {
                return;
            }
            ctx.showLocalizedMessage(
                'success',
                ctx.safeSuccessMessage(result.body, 'Rerank Routes updated successfully.')
            );
        } catch (error) {
            console.error('Error saving Rerank:', error);
            ctx.showLocalizedError('Error saving Rerank Routes:', error);
        } finally {
            ctx.updateSaveButtonDisabledState();
        }
    }

    async function saveEmbeddings() {
        ctx.elements.saveButton.disabled = true;
        ctx.showLocalizedMessage('info', 'Saving Embeddings Routes...');

        let payload;
        try {
            payload = getEmbeddingsPayloadForSave(ctx.getOperationBasePayload());
        } catch (error) {
            ctx.showClientValidationError(error);
            return;
        }

        try {
            const result = await saveOperationPayload(
                payload,
                'Error saving Embeddings Routes:',
                () => {
                    ctx.state.originalEmbeddingsContent = getNormalizedEmbeddingsContent();
                }
            );
            if (!result) {
                return;
            }
            ctx.showLocalizedMessage(
                'success',
                ctx.safeSuccessMessage(result.body, 'Embeddings Routes updated successfully.')
            );
        } catch (error) {
            console.error('Error saving Embeddings:', error);
            ctx.showLocalizedError('Error saving Embeddings Routes:', error);
        } finally {
            ctx.updateSaveButtonDisabledState();
        }
    }

    function renderImageSection(listElement, refreshEmptyState, items, buildCard) {
        listElement.textContent = '';

        if (!Array.isArray(items) || items.length === 0) {
            refreshEmptyState();
            return;
        }

        items.forEach(item => {
            const itemCard = buildCard(item);
            listElement.appendChild(itemCard);
        });
        refreshEmptyState();
    }

    function buildImageCard(initialData, options) {
        const card = document.createElement('section');
        card.className = 'rule-card';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';

        const gatewayModelInput = ctx.createTextInput('gateway-model-input', options.gatewayPlaceholder);
        gatewayModelInput.value = initialData.gateway_model_name || '';
        titleWrap.appendChild(ctx.createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, 'Remove Model');
        removeButton.addEventListener('click', () => {
            card.remove();
            options.refreshEmptyState();
        });

        const routeList = document.createElement('div');
        routeList.className = 'fallback-list';

        const addRouteButton = document.createElement('button');
        addRouteButton.type = 'button';
        addRouteButton.className = 'secondary-button add-fallback-button';
        ctx.bindKnownActionText(addRouteButton, 'Add Route');
        addRouteButton.addEventListener('click', () => {
            routeList.appendChild(buildImageRouteRow({}, options.defaultTargetPath));
        });

        const cardBody = document.createElement('div');
        cardBody.className = 'rule-card-body';
        cardBody.appendChild(ctx.createOperationCostCalculatorField(initialData));
        cardBody.appendChild(routeList);
        cardBody.appendChild(addRouteButton);

        const accordionToggle = document.createElement('button');
        accordionToggle.type = 'button';
        accordionToggle.className = 'accordion-toggle';
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
            ctx.toggleProviderCatalogCard(card);
        });

        const headerLeft = document.createElement('div');
        headerLeft.className = 'rule-card-header-left';
        headerLeft.appendChild(accordionToggle);
        headerLeft.appendChild(titleWrap);

        cardHeader.appendChild(headerLeft);
        cardHeader.appendChild(removeButton);

        card.classList.add('collapsed');
        card.appendChild(cardHeader);
        card.appendChild(cardBody);

        const routes = Array.isArray(initialData.routes) ? initialData.routes : [];
        routes.forEach(route => {
            const routeRow = buildImageRouteRow(route, options.defaultTargetPath);
            routeList.appendChild(routeRow);
        });

        if (routes.length === 0) {
            const routeRow = buildImageRouteRow({}, options.defaultTargetPath);
            routeList.appendChild(routeRow);
        }

        return card;
    }

    function buildImageRouteRow(initialData, defaultTargetPath) {
        const row = document.createElement('div');
        row.className = 'fallback-row';

        ctx.setupRowReordering(row);

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid';

        const providerSelect = ctx.createSelect('provider-select');
        ctx.setSelectOptions(providerSelect, ctx.state.availableProviders, 'Choose a provider', initialData.provider || '');

        const modelInput = ctx.createTextInput('model-input', 'Choose or enter model');
        modelInput.value = initialData.model || '';
        const dataListId = `image-models-list-${Math.random().toString(36).substr(2, 9)}`;
        modelInput.setAttribute('list', dataListId);
        const dataList = document.createElement('datalist');
        dataList.id = dataListId;
        row.appendChild(dataList);

        const targetPathInput = ctx.createTextInput('target-path-input', defaultTargetPath);
        targetPathInput.value = initialData.target_path || defaultTargetPath;
        const requestFormatSelect = ctx.createSelect('request-format-select');
        ctx.setSelectOptions(
            requestFormatSelect,
            ctx.constants.IMAGE_REQUEST_FORMAT_OPTIONS,
            'Default request format',
            initialData.request_format || ''
        );
        const responseFormatSelect = ctx.createSelect('response-format-select');
        ctx.setSelectOptions(
            responseFormatSelect,
            ctx.constants.IMAGE_RESPONSE_FORMAT_OPTIONS,
            'Default response format',
            initialData.response_format || ''
        );
        const { retryDelayInput, retryCountInput } = ctx.createRetrySettingsInputs(initialData);

        const customBodyParamsInput = ctx.createTextarea('custom-body-params-input', '{"param": "value"}');
        customBodyParamsInput.value = ctx.normalizeObjectTextarea(initialData.custom_body_params);

        const customHeadersInput = ctx.createTextarea('custom-headers-input', '{"X-Header": "value"}');
        customHeadersInput.value = ctx.normalizeObjectTextarea(initialData.custom_headers);
        const requestMappingInput = ctx.createTextarea('request-mapping-input', '{"fields": {"prompt": "prompt"}}');
        requestMappingInput.value = ctx.normalizeObjectTextarea(initialData.request_mapping);
        const responseMappingInput = ctx.createTextarea('response-mapping-input', '{"artifacts_path": "artifacts"}');
        responseMappingInput.value = ctx.normalizeObjectTextarea(initialData.response_mapping);

        fieldsGrid.appendChild(ctx.createFieldGroup('Provider', providerSelect, 'provider-field'));
        fieldsGrid.appendChild(ctx.createFieldGroup('Model', modelInput, 'model-field'));
        fieldsGrid.appendChild(ctx.createFieldGroup('Target Path', targetPathInput));

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
        advancedGrid.appendChild(ctx.createFieldGroup('Request Format', requestFormatSelect));
        advancedGrid.appendChild(ctx.createFieldGroup('Response Format', responseFormatSelect));
        advancedGrid.appendChild(ctx.createFieldGroup('Retry Delay', retryDelayInput));
        advancedGrid.appendChild(ctx.createFieldGroup('Retry Count', retryCountInput));
        advancedGrid.appendChild(ctx.createFieldGroup('Request Mapping', requestMappingInput, 'textarea-group'));
        advancedGrid.appendChild(ctx.createFieldGroup('Response Mapping', responseMappingInput, 'textarea-group'));
        advancedGrid.appendChild(ctx.createFieldGroup('Custom Body Params', customBodyParamsInput, 'textarea-group'));
        advancedGrid.appendChild(ctx.createFieldGroup('Custom Headers', customHeadersInput, 'textarea-group'));
        advancedDetails.appendChild(advancedGrid);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, 'Remove Route');
        removeButton.addEventListener('click', () => {
            row.remove();
        });

        const rowActions = document.createElement('div');
        rowActions.className = 'fallback-row-actions';

        const { moveUpButton, moveDownButton } = ctx.createMoveButtons(row);
        rowActions.appendChild(moveUpButton);
        rowActions.appendChild(moveDownButton);
        rowActions.appendChild(removeButton);

        row.appendChild(fieldsGrid);
        row.appendChild(modelStatus);
        row.appendChild(advancedDetails);
        row.appendChild(rowActions);

        ctx.createLazyProviderCatalogRowController({
            row,
            providerSelect,
            modelControl: modelInput,
            dataList,
            modelStatus,
        });
        return row;
    }

    function normalizeImageRouteForSave(routeRow, defaultTargetPath, routeLabel) {
        const providerSelect = routeRow.querySelector('.provider-select');
        const modelInput = routeRow.querySelector('.model-input');
        const customBodyParamsInput = routeRow.querySelector('.custom-body-params-input');
        const customHeadersInput = routeRow.querySelector('.custom-headers-input');
        const requestMappingInput = routeRow.querySelector('.request-mapping-input');
        const responseMappingInput = routeRow.querySelector('.response-mapping-input');
        const targetPathInput = routeRow.querySelector('.target-path-input');
        const requestFormatSelect = routeRow.querySelector('.request-format-select');
        const responseFormatSelect = routeRow.querySelector('.response-format-select');
        const retryDelayInput = routeRow.querySelector('.retry-delay-input');
        const retryCountInput = routeRow.querySelector('.retry-count-input');

        const provider = providerSelect.value.trim();
        const model = modelInput.value.trim();
        const target_path = targetPathInput.value.trim() || defaultTargetPath;
        const request_format = requestFormatSelect.value.trim();
        const response_format = responseFormatSelect.value.trim();

        if (!provider) {
            throw new Error(`Each ${routeLabel} route must have a provider selected.`);
        }
        if (!model) {
            throw new Error(`Enter or choose a model for provider '${provider}' before saving.`);
        }
        if (!target_path.startsWith('/') && !/^https?:\/\//i.test(target_path)) {
            throw new Error('Target path must start with / or with http:// or https://');
        }

        const routePayload = {
            provider,
            model,
            target_path,
            custom_body_params: ctx.parseObjectTextarea(customBodyParamsInput.value, 'Custom body params'),
            custom_headers: ctx.parseObjectTextarea(customHeadersInput.value, 'Custom headers'),
        };
        const request_mapping = ctx.parseObjectTextarea(requestMappingInput.value, 'Request mapping');
        const response_mapping = ctx.parseObjectTextarea(responseMappingInput.value, 'Response mapping');
        if (request_format) {
            routePayload.request_format = request_format;
        }
        if (response_format) {
            routePayload.response_format = response_format;
        }
        if (Object.keys(request_mapping).length > 0) {
            routePayload.request_mapping = request_mapping;
        }
        if (Object.keys(response_mapping).length > 0) {
            routePayload.response_mapping = response_mapping;
        }
        ctx.applyRetrySettingsToPayload(routePayload, retryDelayInput, retryCountInput);
        return routePayload;
    }

    function normalizeImageCardForSave(ruleCard, routeLabel, defaultTargetPath) {
        const gatewayModelInput = ruleCard.querySelector('.gateway-model-input');
        const routeRows = Array.from(ruleCard.querySelectorAll('.fallback-list > .fallback-row'));

        const gatewayModelName = gatewayModelInput.value.trim();
        if (!gatewayModelName) {
            throw new Error(`Each ${routeLabel} model rule must have a gateway model name.`);
        }
        if (routeRows.length === 0) {
            throw new Error(`${routeLabel} model '${gatewayModelName}' must contain at least one route.`);
        }

        return ctx.applyOperationCostCalculator({
            gateway_model_name: gatewayModelName,
            routes: routeRows.map(routeRow => normalizeImageRouteForSave(routeRow, defaultTargetPath, routeLabel)),
        }, ruleCard);
    }

    function getImagesPayloadForSave(basePayload = null) {
        const images_generations = Array.from(ctx.elements.imageGenerationList.querySelectorAll('.rule-card')).map(ruleCard => (
            normalizeImageCardForSave(ruleCard, 'image generation', '/images/generations')
        ));
        const images_edits = Array.from(ctx.elements.imageEditList.querySelectorAll('.rule-card')).map(ruleCard => (
            normalizeImageCardForSave(ruleCard, 'image edit', '/images/edits')
        ));

        return buildOperationRoutesPayload({
            images_generations,
            images_edits,
        }, basePayload);
    }

    function getNormalizedImagesContent() {
        return ctx.stableSerialize(getImagesPayloadForSave());
    }

    async function loadImagesEditor() {
        try {
            const loaded = await loadOperationRulesPayload(
                'Images Routes',
                async payload => {
                    await renderImageSection(
                        ctx.elements.imageGenerationList,
                        ctx.refreshImageGenerationEmptyState,
                        payload.images_generations,
                        (item) => buildImageCard(item, {
                            gatewayPlaceholder: 'llmgateway/image-generation-model',
                            defaultTargetPath: '/images/generations',
                            refreshEmptyState: ctx.refreshImageGenerationEmptyState,
                        }),
                    );
                    await renderImageSection(
                        ctx.elements.imageEditList,
                        ctx.refreshImageEditEmptyState,
                        payload.images_edits,
                        (item) => buildImageCard(item, {
                            gatewayPlaceholder: 'llmgateway/image-edit-model',
                            defaultTargetPath: '/images/edits',
                            refreshEmptyState: ctx.refreshImageEditEmptyState,
                        }),
                    );
                }
            );
            if (!loaded) {
                ctx.showLocalizedMessage('warning', 'A newer local edit was preserved. Reload again to discard it.');
                return false;
            }
            ctx.state.originalImagesContent = getNormalizedImagesContent();
            ctx.updateSaveButtonDisabledState();
            ctx.showLocalizedMessage('success', 'Images Routes loaded successfully.');
            return true;
        } catch (error) {
            console.error('Error fetching Images Routes:', error);
            ctx.showLocalizedError('Error loading Images Routes:', error);
            ctx.state.originalImagesContent = null;
            ctx.updateSaveButtonDisabledState();
            return false;
        }
    }

    async function saveImages() {
        ctx.elements.saveButton.disabled = true;
        ctx.showLocalizedMessage('info', 'Saving Images Routes...');

        let payload;
        try {
            payload = getImagesPayloadForSave(ctx.getOperationBasePayload());
        } catch (error) {
            ctx.showClientValidationError(error);
            return;
        }

        try {
            const result = await saveOperationPayload(
                payload,
                'Error saving Images Routes:',
                () => {
                    ctx.state.originalImagesContent = getNormalizedImagesContent();
                }
            );
            if (!result) {
                return;
            }
            ctx.showLocalizedMessage(
                'success',
                ctx.safeSuccessMessage(result.body, 'Images Routes updated successfully.')
            );
        } catch (error) {
            console.error('Error saving Images:', error);
            ctx.showLocalizedError('Error saving Images Routes:', error);
        } finally {
            ctx.updateSaveButtonDisabledState();
        }
    }

    function getAudioPayloadForSave(basePayload = null) {
        const audio_speech = Array.from(ctx.elements.audioSpeechList.querySelectorAll('.rule-card')).map(
            normalizeAudioSpeechCardForSave
        );
        const audio_transcriptions = Array.from(ctx.elements.audioTranscriptionsList.querySelectorAll('.rule-card')).map(
            normalizeAudioTranscriptionCardForSave
        );
        return buildOperationRoutesPayload({ audio_speech, audio_transcriptions }, basePayload);
    }

    function getNormalizedAudioContent() {
        return ctx.stableSerialize(getAudioPayloadForSave());
    }

    function validateAudioTargetPath(targetPath, fieldLabel) {
        if (!targetPath.startsWith('/') && !/^https?:\/\//i.test(targetPath)) {
            throw new Error(`${fieldLabel} must start with / or with http:// or https://`);
        }
    }

    function normalizeAudioRouteForSave(routeRow, options) {
        const providerSelect = routeRow.querySelector('.provider-select');
        const modelInput = routeRow.querySelector('.model-input');
        const customBodyParamsInput = routeRow.querySelector('.custom-body-params-input');
        const customHeadersInput = routeRow.querySelector('.custom-headers-input');
        const targetPathInput = routeRow.querySelector('.target-path-input');
        const requestFormatSelect = routeRow.querySelector('.request-format-select');
        const voicesTargetPathInput = routeRow.querySelector('.voices-target-path-input');
        const retryDelayInput = routeRow.querySelector('.retry-delay-input');
        const retryCountInput = routeRow.querySelector('.retry-count-input');

        const provider = providerSelect.value.trim();
        const model = modelInput.value.trim();
        const target_path = targetPathInput.value.trim() || options.defaultTargetPath;
        const request_format = requestFormatSelect?.value.trim() || '';
        const voices_target_path = voicesTargetPathInput?.value.trim() || '';

        if (!provider) {
            throw new Error(`Each ${options.routeLabel} route must have a provider selected.`);
        }
        if (!model) {
            throw new Error(`Enter or choose a model for provider '${provider}' before saving.`);
        }
        validateAudioTargetPath(target_path, 'Target path');

        const routePayload = {
            provider,
            model,
            target_path,
            custom_body_params: ctx.parseObjectTextarea(customBodyParamsInput.value, 'Custom body params'),
            custom_headers: ctx.parseObjectTextarea(customHeadersInput.value, 'Custom headers'),
        };
        if (request_format) {
            routePayload.request_format = request_format;
        }
        if (voices_target_path) {
            validateAudioTargetPath(voices_target_path, 'Voices target path');
            routePayload.voices_target_path = voices_target_path;
        }
        ctx.applyRetrySettingsToPayload(routePayload, retryDelayInput, retryCountInput);
        return routePayload;
    }

    function normalizeAudioSpeechRouteForSave(routeRow) {
        return normalizeAudioRouteForSave(routeRow, {
            routeLabel: 'audio speech',
            defaultTargetPath: '/audio/speech',
        });
    }

    function normalizeAudioTranscriptionRouteForSave(routeRow) {
        return normalizeAudioRouteForSave(routeRow, {
            routeLabel: 'audio transcription',
            defaultTargetPath: '/audio/transcriptions',
        });
    }

    function normalizeAudioSpeechCardForSave(ruleCard) {
        const gatewayModelInput = ruleCard.querySelector('.gateway-model-input');
        const routeRows = Array.from(ruleCard.querySelectorAll('.fallback-list > .fallback-row'));

        const gatewayModelName = gatewayModelInput.value.trim();
        if (!gatewayModelName) {
            throw new Error('Each audio speech model rule must have a gateway model name.');
        }
        if (routeRows.length === 0) {
            throw new Error(`Audio speech model '${gatewayModelName}' must contain at least one route.`);
        }

        return ctx.applyOperationCostCalculator({
            gateway_model_name: gatewayModelName,
            routes: routeRows.map(normalizeAudioSpeechRouteForSave),
        }, ruleCard);
    }

    function normalizeAudioTranscriptionCardForSave(ruleCard) {
        const gatewayModelInput = ruleCard.querySelector('.gateway-model-input');
        const routeRows = Array.from(ruleCard.querySelectorAll('.fallback-list > .fallback-row'));

        const gatewayModelName = gatewayModelInput.value.trim();
        if (!gatewayModelName) {
            throw new Error('Each audio transcription model rule must have a gateway model name.');
        }
        if (routeRows.length === 0) {
            throw new Error(`Audio transcription model '${gatewayModelName}' must contain at least one route.`);
        }

        return ctx.applyOperationCostCalculator({
            gateway_model_name: gatewayModelName,
            routes: routeRows.map(normalizeAudioTranscriptionRouteForSave),
        }, ruleCard);
    }

    async function loadAudioEditor() {
        try {
            const loaded = await loadOperationRulesPayload(
                'Audio Routes',
                async payload => {
                    await renderAudioSpeech(payload.audio_speech || []);
                    await renderAudioTranscriptions(payload.audio_transcriptions || []);
                }
            );
            if (!loaded) {
                ctx.showLocalizedMessage('warning', 'A newer local edit was preserved. Reload again to discard it.');
                return false;
            }
            ctx.state.originalAudioContent = getNormalizedAudioContent();
            ctx.updateSaveButtonDisabledState();
            ctx.showLocalizedMessage('success', 'Audio Routes loaded successfully.');
            return true;
        } catch (error) {
            console.error('Error fetching Audio Routes:', error);
            ctx.showLocalizedError('Error loading Audio Routes:', error);
            ctx.state.originalAudioContent = null;
            ctx.updateSaveButtonDisabledState();
            return false;
        }
    }

    function renderAudioSpeech(items) {
        ctx.elements.audioSpeechList.textContent = '';

        if (!Array.isArray(items) || items.length === 0) {
            ctx.refreshAudioSpeechEmptyState();
            return;
        }

        items.forEach(item => {
            const card = buildAudioSpeechCard(item);
            ctx.elements.audioSpeechList.appendChild(card);
        });
        ctx.refreshAudioSpeechEmptyState();
    }

    function renderAudioTranscriptions(items) {
        ctx.elements.audioTranscriptionsList.textContent = '';

        if (!Array.isArray(items) || items.length === 0) {
            ctx.refreshAudioTranscriptionsEmptyState();
            return;
        }

        items.forEach(item => {
            const card = buildAudioTranscriptionCard(item);
            ctx.elements.audioTranscriptionsList.appendChild(card);
        });
        ctx.refreshAudioTranscriptionsEmptyState();
    }

    function buildAudioSpeechCard(initialData) {
        return buildAudioCard(initialData, {
            gatewayPlaceholder: 'llmgateway/audio-speech-model',
            addRouteButtonText: 'Add Route',
            refreshEmptyState: ctx.refreshAudioSpeechEmptyState,
            buildRouteRow: buildAudioSpeechRouteRow,
        });
    }

    function buildAudioTranscriptionCard(initialData) {
        return buildAudioCard(initialData, {
            gatewayPlaceholder: 'llmgateway/audio-transcription-model',
            addRouteButtonText: 'Add Fallback Route',
            refreshEmptyState: ctx.refreshAudioTranscriptionsEmptyState,
            buildRouteRow: buildAudioTranscriptionRouteRow,
        });
    }

    function buildAudioCard(initialData, options) {
        const card = document.createElement('section');
        card.className = 'rule-card';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';

        const gatewayModelInput = ctx.createTextInput('gateway-model-input', options.gatewayPlaceholder);
        gatewayModelInput.value = initialData.gateway_model_name || '';
        titleWrap.appendChild(ctx.createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, 'Remove Model');
        removeButton.addEventListener('click', () => {
            card.remove();
            options.refreshEmptyState();
        });

        const routeList = document.createElement('div');
        routeList.className = 'fallback-list';

        const addRouteButton = document.createElement('button');
        addRouteButton.type = 'button';
        addRouteButton.className = 'secondary-button add-fallback-button';
        ctx.bindKnownActionText(addRouteButton, options.addRouteButtonText);
        addRouteButton.addEventListener('click', () => {
            routeList.appendChild(options.buildRouteRow({}));
        });

        const cardBody = document.createElement('div');
        cardBody.className = 'rule-card-body';
        cardBody.appendChild(ctx.createOperationCostCalculatorField(initialData));
        cardBody.appendChild(routeList);
        cardBody.appendChild(addRouteButton);

        const accordionToggle = document.createElement('button');
        accordionToggle.type = 'button';
        accordionToggle.className = 'accordion-toggle';
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
            ctx.toggleProviderCatalogCard(card);
        });

        const headerLeft = document.createElement('div');
        headerLeft.className = 'rule-card-header-left';
        headerLeft.appendChild(accordionToggle);
        headerLeft.appendChild(titleWrap);

        cardHeader.appendChild(headerLeft);
        cardHeader.appendChild(removeButton);

        card.classList.add('collapsed');
        card.appendChild(cardHeader);
        card.appendChild(cardBody);

        const routes = Array.isArray(initialData.routes) ? initialData.routes : [];
        routes.forEach(route => {
            const routeRow = options.buildRouteRow(route);
            routeList.appendChild(routeRow);
        });

        if (routes.length === 0) {
            const routeRow = options.buildRouteRow({});
            routeList.appendChild(routeRow);
        }

        return card;
    }

    function buildAudioSpeechRouteRow(initialData) {
        return buildAudioRouteRow(initialData, {
            defaultTargetPath: '/audio/speech',
            includeRequestFormat: false,
            includeVoicesTargetPath: true,
            dataListPrefix: 'audio-speech-models-list',
            removeButtonText: 'Remove Route',
            customBodyPlaceholder: '{"voice": "alloy"}',
        });
    }

    function buildAudioTranscriptionRouteRow(initialData) {
        return buildAudioRouteRow(initialData, {
            defaultTargetPath: '/audio/transcriptions',
            includeRequestFormat: true,
            includeVoicesTargetPath: false,
            dataListPrefix: 'audio-transcription-models-list',
            removeButtonText: 'Remove Fallback Route',
            customBodyPlaceholder: '{"language": "en"}',
        });
    }

    function buildAudioRouteRow(initialData, options) {
        const row = document.createElement('div');
        row.className = 'fallback-row';

        ctx.setupRowReordering(row);

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid';

        const providerSelect = ctx.createSelect('provider-select');
        ctx.setSelectOptions(providerSelect, ctx.state.availableProviders, 'Choose a provider', initialData.provider || '');

        const modelInput = ctx.createTextInput('model-input', 'Choose or enter model');
        modelInput.value = initialData.model || '';
        const dataListId = `${options.dataListPrefix}-${Math.random().toString(36).substr(2, 9)}`;
        modelInput.setAttribute('list', dataListId);
        const dataList = document.createElement('datalist');
        dataList.id = dataListId;
        row.appendChild(dataList);

        const targetPathInput = ctx.createTextInput('target-path-input', options.defaultTargetPath);
        targetPathInput.value = initialData.target_path || options.defaultTargetPath;
        const { retryDelayInput, retryCountInput } = ctx.createRetrySettingsInputs(initialData);

        const customBodyParamsInput = ctx.createTextarea('custom-body-params-input', options.customBodyPlaceholder);
        customBodyParamsInput.value = ctx.normalizeObjectTextarea(initialData.custom_body_params);

        const customHeadersInput = ctx.createTextarea('custom-headers-input', '{"X-Header": "value"}');
        customHeadersInput.value = ctx.normalizeObjectTextarea(initialData.custom_headers);

        fieldsGrid.appendChild(ctx.createFieldGroup('Provider', providerSelect, 'provider-field'));
        fieldsGrid.appendChild(ctx.createFieldGroup('Model', modelInput, 'model-field'));
        fieldsGrid.appendChild(ctx.createFieldGroup('Target Path', targetPathInput));

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
        if (options.includeRequestFormat) {
            const requestFormatSelect = ctx.createSelect('request-format-select');
            ctx.setSelectOptions(
                requestFormatSelect,
                ctx.constants.AUDIO_REQUEST_FORMAT_OPTIONS,
                'Default request format',
                initialData.request_format || ''
            );
            advancedGrid.appendChild(ctx.createFieldGroup('Request Format', requestFormatSelect));
        }
        if (options.includeVoicesTargetPath) {
            const voicesTargetPathInput = ctx.createTextInput('voices-target-path-input', '/voices');
            voicesTargetPathInput.value = initialData.voices_target_path || '';
            advancedGrid.appendChild(ctx.createFieldGroup('Voices Target Path', voicesTargetPathInput));
        }
        advancedGrid.appendChild(ctx.createFieldGroup('Retry Delay', retryDelayInput));
        advancedGrid.appendChild(ctx.createFieldGroup('Retry Count', retryCountInput));
        advancedGrid.appendChild(ctx.createFieldGroup('Custom Body Params', customBodyParamsInput, 'textarea-group'));
        advancedGrid.appendChild(ctx.createFieldGroup('Custom Headers', customHeadersInput, 'textarea-group'));
        advancedDetails.appendChild(advancedGrid);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, options.removeButtonText);
        removeButton.addEventListener('click', () => {
            row.remove();
        });

        const rowActions = document.createElement('div');
        rowActions.className = 'fallback-row-actions';

        const { moveUpButton, moveDownButton } = ctx.createMoveButtons(row);
        rowActions.appendChild(moveUpButton);
        rowActions.appendChild(moveDownButton);
        rowActions.appendChild(removeButton);

        row.appendChild(fieldsGrid);
        row.appendChild(modelStatus);
        row.appendChild(advancedDetails);
        row.appendChild(rowActions);

        ctx.createLazyProviderCatalogRowController({
            row,
            providerSelect,
            modelControl: modelInput,
            dataList,
            modelStatus,
        });
        return row;
    }

    async function saveAudio() {
        ctx.elements.saveButton.disabled = true;
        ctx.showLocalizedMessage('info', 'Saving Audio Routes...');

        let payload;
        try {
            payload = getAudioPayloadForSave(ctx.getOperationBasePayload());
        } catch (error) {
            ctx.showClientValidationError(error);
            return;
        }

        try {
            const result = await saveOperationPayload(
                payload,
                'Error saving Audio Routes:',
                () => {
                    ctx.state.originalAudioContent = getNormalizedAudioContent();
                }
            );
            if (!result) {
                return;
            }
            ctx.showLocalizedMessage(
                'success',
                ctx.safeSuccessMessage(result.body, 'Audio Routes updated successfully.')
            );
        } catch (error) {
            console.error('Error saving Audio Routes:', error);
            ctx.showLocalizedError('Error saving Audio Routes:', error);
        } finally {
            ctx.updateSaveButtonDisabledState();
        }
    }

    function createAccordionToggle(card) {
        const accordionToggle = document.createElement('button');
        accordionToggle.type = 'button';
        accordionToggle.className = 'accordion-toggle';
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
            card.classList.toggle('collapsed');
        });
        return accordionToggle;
    }

    function createWebCardShell(initialData, gatewayPlaceholder, removeLabel, refreshEmptyState) {
        const card = document.createElement('section');
        card.className = 'rule-card collapsed';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';
        const gatewayModelInput = ctx.createTextInput('gateway-model-input', gatewayPlaceholder);
        gatewayModelInput.value = initialData.gateway_model_name || '';
        titleWrap.appendChild(ctx.createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const headerLeft = document.createElement('div');
        headerLeft.className = 'rule-card-header-left';
        headerLeft.appendChild(createAccordionToggle(card));
        headerLeft.appendChild(titleWrap);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, removeLabel);
        removeButton.addEventListener('click', () => {
            card.remove();
            refreshEmptyState();
            refreshWebCrossDropdowns();
        });

        cardHeader.appendChild(headerLeft);
        cardHeader.appendChild(removeButton);

        const cardBody = document.createElement('div');
        cardBody.className = 'rule-card-body';
        cardBody.appendChild(ctx.createOperationCostCalculatorField(initialData));
        card.appendChild(cardHeader);
        card.appendChild(cardBody);
        return { card, cardBody, gatewayModelInput };
    }

    function appendFieldHint(fieldGroup, hintKey) {
        if (!hintKey) return;
        const hint = document.createElement('small');
        hint.className = 'field-hint';
        ctx.bindLocalizedText(hint, hintKey);
        fieldGroup.appendChild(hint);
    }

    function attachFieldTooltip(fieldGroup, tooltipKey) {
        if (!tooltipKey) return;
        const label = fieldGroup.querySelector('.field-label');
        if (!label) return;
        const wrapper = document.createElement('span');
        wrapper.className = 'field-tooltip';

        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'field-tooltip-button';
        button.textContent = gatewayI18n.t('editor:hints.infoIcon');
        ctx.bindLocalizedAttribute(
            button,
            'aria-label',
            'editor:hints.fieldInfo',
            () => ({
                field: label.querySelector('.field-label-text')?.textContent
                    || ctx.t('editor:fields.field'),
            }),
        );
        ctx.bindLocalizedAttribute(button, 'title', tooltipKey);

        const popover = document.createElement('span');
        popover.className = 'field-tooltip-popover';
        popover.setAttribute('role', 'tooltip');
        ctx.bindLocalizedText(popover, tooltipKey);

        wrapper.appendChild(button);
        wrapper.appendChild(popover);
        label.appendChild(wrapper);
    }

    function buildWebSearchCard(initialData, options) {
        const { card, cardBody, gatewayModelInput } = createWebCardShell(
            initialData,
            options.gatewayPlaceholder,
            'Remove Service',
            options.refreshEmptyState
        );

        gatewayModelInput.addEventListener('input', refreshWebCrossDropdowns);

        const serviceGrid = document.createElement('div');
        serviceGrid.className = 'fallback-row-grid';
        const queryModelSelect = ctx.createSelect('query-model-input');
        ctx.setModelSelectOptions(queryModelSelect, ctx.state.gatewayModelCatalog.chat, initialData.query_model || '');
        const queryField = ctx.createFieldGroup('Query Model (optional)', queryModelSelect, 'model-field');
        appendFieldHint(queryField, 'editor:hints.webQueryModel');
        serviceGrid.appendChild(queryField);
        cardBody.appendChild(serviceGrid);

        return card;
    }

    function buildWebReadCard(initialData, options) {
        const { card, gatewayModelInput } = createWebCardShell(
            initialData,
            options.gatewayPlaceholder,
            'Remove Service',
            options.refreshEmptyState
        );
        gatewayModelInput.addEventListener('input', refreshWebCrossDropdowns);
        return card;
    }

    function normalizeWebSearchCardForSave(ruleCard) {
        const gatewayModelName = ruleCard.querySelector('.gateway-model-input').value.trim();
        const queryModel = ruleCard.querySelector('.query-model-input')?.value.trim();
        if (!gatewayModelName) {
            throw new Error('Each web search service must have a gateway model name.');
        }
        const payload = { gateway_model_name: gatewayModelName };
        if (queryModel) {
            payload.query_model = queryModel;
        }
        return ctx.applyOperationCostCalculator(payload, ruleCard);
    }

    function normalizeWebReadCardForSave(ruleCard) {
        const gatewayModelName = ruleCard.querySelector('.gateway-model-input').value.trim();
        if (!gatewayModelName) {
            throw new Error('Each web read service must have a gateway model name.');
        }
        return ctx.applyOperationCostCalculator({ gateway_model_name: gatewayModelName }, ruleCard);
    }

    function buildWebReferenceCard(initialData, options) {
        const { card, cardBody } = createWebCardShell(
            initialData,
            options.gatewayPlaceholder,
            'Remove Service',
            options.refreshEmptyState
        );
        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid';
        options.fields.forEach(field => {
            let control;
            if (field.catalog) {
                control = ctx.createSelect(field.className);
                const catalogOptions = ctx.state.gatewayModelCatalog[field.catalog] || [];
                ctx.setModelSelectOptions(control, catalogOptions, initialData[field.key] || field.defaultValue || '');
            } else {
                control = ctx.createTextInput(field.className, field.placeholder);
                control.value = initialData[field.key] || field.defaultValue || '';
            }
            const group = ctx.createFieldGroup(field.label, control, 'model-field');
            appendFieldHint(group, field.hintKey);
            fieldsGrid.appendChild(group);
        });
        cardBody.appendChild(fieldsGrid);
        return card;
    }

    function normalizeWebReferenceCardForSave(ruleCard, options) {
        const gatewayModelName = ruleCard.querySelector('.gateway-model-input').value.trim();
        if (!gatewayModelName) {
            throw new Error(`Each ${options.serviceLabel} must have a gateway model name.`);
        }
        const payload = { gateway_model_name: gatewayModelName };
        options.fields.forEach(field => {
            const value = ruleCard.querySelector(`.${field.className}`).value.trim();
            if (field.required && !value) {
                throw new Error(`${options.serviceLabel} '${gatewayModelName}' must define ${field.key}.`);
            }
            if (value) {
                payload[field.key] = value;
            }
        });
        return ctx.applyOperationCostCalculator(payload, ruleCard);
    }

    const WEB_SEARCH_CARD_OPTIONS = {
        gatewayPlaceholder: 'llmgateway/web-search',
        refreshEmptyState: ctx.refreshWebSearchEmptyState,
    };

    const WEB_READ_CARD_OPTIONS = {
        gatewayPlaceholder: 'llmgateway/web-read',
        refreshEmptyState: ctx.refreshWebReadEmptyState,
    };

    const WEB_RESEARCH_CARD_OPTIONS = {
        gatewayPlaceholder: 'llmgateway/web-research',
        serviceLabel: 'web research service',
        refreshEmptyState: ctx.refreshWebResearchEmptyState,
        fields: [
            { key: 'search_model', label: 'Search Model', className: 'search-model-input', catalog: 'web_search', required: true, hintKey: 'editor:hints.webSearchModel' },
            { key: 'read_model', label: 'Read Model', className: 'read-model-input', catalog: 'web_read', required: true, hintKey: 'editor:hints.webReadModel' },
            { key: 'rerank_model', label: 'Rerank Model', className: 'rerank-model-input', catalog: 'rerank', required: true, hintKey: 'editor:hints.webRerankModel' },
            { key: 'analysis_model', label: 'Analysis Model', className: 'analysis-model-input', catalog: 'chat', required: true, hintKey: 'editor:hints.webAnalysisModel' },
        ],
    };

    const WEB_DEEP_RESEARCH_CARD_OPTIONS = {
        gatewayPlaceholder: 'llmgateway/web-deep-research',
        serviceLabel: 'web deep research service',
        refreshEmptyState: ctx.refreshWebDeepResearchEmptyState,
        fields: [
            { key: 'search_model', label: 'Search Model', className: 'search-model-input', catalog: 'web_search', required: true, hintKey: 'editor:hints.webSearchModel' },
            { key: 'read_model', label: 'Read Model', className: 'read-model-input', catalog: 'web_read', required: true, hintKey: 'editor:hints.webReadModel' },
            { key: 'fast_model', label: 'Fast LLM', className: 'fast-model-input', catalog: 'chat', required: true, hintKey: 'editor:hints.webFastModel' },
            { key: 'smart_model', label: 'Smart LLM', className: 'smart-model-input', catalog: 'chat', required: true, hintKey: 'editor:hints.webSmartModel' },
            { key: 'strategic_model', label: 'Strategic LLM', className: 'strategic-model-input', catalog: 'chat', required: true, hintKey: 'editor:hints.webStrategicModel' },
            { key: 'embedding_model', label: 'Embedding Model', className: 'embedding-model-input', catalog: 'embeddings', hintKey: 'editor:hints.webEmbeddingModel' },
            { key: 'image_generation_model', label: 'Image Generation Model', className: 'image-generation-model-input', catalog: 'images_generations', hintKey: 'editor:hints.webImageModel' },
            { key: 'image_generation_size', label: 'Image Generation Size', className: 'image-generation-size-input', placeholder: '1024x1024', hintKey: 'editor:hints.webImageSize' },
        ],
    };

    function getWebPayloadForSave(basePayload = null) {
        const web_search = Array.from(ctx.elements.webSearchList.querySelectorAll('.rule-card')).map(normalizeWebSearchCardForSave);
        const web_read = Array.from(ctx.elements.webReadList.querySelectorAll('.rule-card')).map(normalizeWebReadCardForSave);
        const web_research = Array.from(ctx.elements.webResearchList.querySelectorAll('.rule-card')).map(
            card => normalizeWebReferenceCardForSave(card, WEB_RESEARCH_CARD_OPTIONS)
        );
        const web_deep_research = Array.from(ctx.elements.webDeepResearchList.querySelectorAll('.rule-card')).map(
            card => normalizeWebReferenceCardForSave(card, WEB_DEEP_RESEARCH_CARD_OPTIONS)
        );
        return buildOperationRoutesPayload(
            { web_search, web_read, web_research, web_deep_research },
            basePayload
        );
    }

    function getNormalizedWebContent() {
        return ctx.stableSerialize(getWebPayloadForSave());
    }

    async function loadWebEditor() {
        try {
            const loaded = await loadOperationRulesPayload(
                'Web Services',
                async payload => {
                    await loadGatewayModelCatalog();
                    applyOperationCatalog(payload);
                    await renderWebSections(payload);
                    refreshWebCrossDropdowns();
                }
            );
            if (!loaded) {
                ctx.showLocalizedMessage('warning', 'A newer local edit was preserved. Reload again to discard it.');
                return false;
            }
            ctx.state.originalWebContent = getNormalizedWebContent();
            ctx.updateSaveButtonDisabledState();
            ctx.showLocalizedMessage('success', 'Web Services loaded successfully.');
            return true;
        } catch (error) {
            console.error('Error fetching Web Services:', error);
            ctx.showLocalizedError('Error loading Web Services:', error);
            ctx.state.originalWebContent = null;
            ctx.updateSaveButtonDisabledState();
            return false;
        }
    }

    function renderWebSections(payload) {
        ctx.elements.webSearchList.textContent = '';
        ctx.elements.webReadList.textContent = '';
        ctx.elements.webResearchList.textContent = '';
        ctx.elements.webDeepResearchList.textContent = '';
        (payload.web_search || []).forEach(item => {
            const card = buildWebSearchCard(item, WEB_SEARCH_CARD_OPTIONS);
            ctx.elements.webSearchList.appendChild(card);
        });
        (payload.web_read || []).forEach(item => {
            const card = buildWebReadCard(item, WEB_READ_CARD_OPTIONS);
            ctx.elements.webReadList.appendChild(card);
        });
        (payload.web_research || []).forEach(item => {
            ctx.elements.webResearchList.appendChild(buildWebReferenceCard(item, WEB_RESEARCH_CARD_OPTIONS));
        });
        (payload.web_deep_research || []).forEach(item => {
            ctx.elements.webDeepResearchList.appendChild(buildWebReferenceCard(item, WEB_DEEP_RESEARCH_CARD_OPTIONS));
        });

        ctx.refreshWebSearchEmptyState();
        ctx.refreshWebReadEmptyState();
        ctx.refreshWebResearchEmptyState();
        ctx.refreshWebDeepResearchEmptyState();
    }

    async function saveWeb() {
        ctx.elements.saveButton.disabled = true;
        ctx.showLocalizedMessage('info', 'Saving Web Services...');

        let payload;
        try {
            payload = getWebPayloadForSave(ctx.getOperationBasePayload());
        } catch (error) {
            ctx.showClientValidationError(error);
            return;
        }

        try {
            const result = await saveOperationPayload(
                payload,
                'Error saving Web Services:',
                published => {
                    applyOperationCatalog(published);
                    refreshWebCrossDropdowns();
                    ctx.state.originalWebContent = getNormalizedWebContent();
                }
            );
            if (!result) {
                return;
            }
            ctx.showLocalizedMessage(
                'success',
                ctx.safeSuccessMessage(result.body, 'Web Services updated successfully.')
            );
        } catch (error) {
            console.error('Error saving Web Services:', error);
            ctx.showLocalizedError('Error saving Web Services:', error);
        } finally {
            ctx.updateSaveButtonDisabledState();
        }
    }

    async function loadModelRulesEditor() {
        ctx.showLocalizedMessage('info', 'Loading Model Rules...');
        try {
            const loaded = await ctx.loadConfigDocument(
                'model',
                '/v1/config/model-rules',
                {
                    responseType: 'text',
                    validate: content => {
                        if (typeof content !== 'string') {
                            throw new ConfigUiError('The configuration response has an invalid shape.');
                        }
                        return content;
                    },
                    apply: content => {
                        ctx.elements.modelRulesRawInput.value = content;
                    },
                }
            );
            if (!loaded) {
                ctx.showLocalizedMessage('warning', 'A newer local edit was preserved. Reload again to discard it.');
                return false;
            }
            ctx.state.originalModelRulesContent = ctx.elements.modelRulesRawInput.value;
            ctx.updateSaveButtonDisabledState();
            ctx.showLocalizedMessage('success', 'Model Rules loaded successfully.');
            return true;
        } catch (error) {
            console.error('Error fetching Model Rules:', error);
            ctx.showLocalizedError('Error loading Model Rules:', error);
            ctx.state.originalModelRulesContent = null;
            ctx.updateSaveButtonDisabledState();
            return false;
        }
    }

    async function saveModelRules() {
        ctx.elements.saveButton.disabled = true;
        ctx.showLocalizedMessage('info', 'Saving Model Rules...');
        try {
            const payload = ctx.elements.modelRulesRawInput.value;
            const result = await ctx.saveConfigDocument(
                'model',
                '/v1/config/model-rules',
                payload,
                {
                    contentType: 'text/plain',
                    body: payload,
                    errorTitle: 'Error saving Model Rules:',
                    extractPublishedPayload: (_body, submitted) => submitted,
                    validatePublished: content => {
                        if (typeof content !== 'string') {
                            throw new ConfigUiError('The configuration response has an invalid shape.');
                        }
                        return content;
                    },
                }
            );
            if (!result) {
                return;
            }
            if (ctx.state.editorMutationVersion === result.submittedMutationVersion) {
                ctx.elements.modelRulesRawInput.value = result.payload;
                ctx.state.originalModelRulesContent = result.payload;
            }
            ctx.showLocalizedMessage(
                'success',
                ctx.safeSuccessMessage(result.body, 'Model Rules saved successfully.')
            );
        } catch (error) {
            console.error('Error saving Model Rules:', error);
            ctx.showLocalizedError('Error saving Model Rules:', error);
        } finally {
            ctx.updateSaveButtonDisabledState();
        }
    }


    Object.assign(ctx, {
        normalizeOperationRulesPayload,
        applyOperationRulesPayload,
        buildOperationRoutesPayload,
        loadOperationRulesPayload,
        saveOperationPayload,
        collectCurrentWebSectionModels,
        refreshWebCrossDropdowns,
        loadGatewayModelCatalog,
        applyOperationCatalog,
        getEmbeddingsPayloadForSave,
        getNormalizedEmbeddingsContent,
        normalizeEmbeddingRouteForSave,
        normalizeEmbeddingCardForSave,
        normalizeEmbeddingRouteRowForSave,
        loadEmbeddingsEditor,
        renderEmbeddings,
        buildEmbeddingCard,
        buildEmbeddingRouteRow,
        loadRerankEditor,
        renderRerank,
        buildRerankCard,
        buildRerankRouteRow,
        getRerankPayloadForSave,
        getNormalizedRerankContent,
        normalizeRerankRouteForSave,
        normalizeRerankCardForSave,
        saveRerank,
        saveEmbeddings,
        renderImageSection,
        buildImageCard,
        buildImageRouteRow,
        normalizeImageRouteForSave,
        normalizeImageCardForSave,
        getImagesPayloadForSave,
        getNormalizedImagesContent,
        loadImagesEditor,
        saveImages,
        getAudioPayloadForSave,
        getNormalizedAudioContent,
        validateAudioTargetPath,
        normalizeAudioRouteForSave,
        normalizeAudioSpeechRouteForSave,
        normalizeAudioTranscriptionRouteForSave,
        normalizeAudioSpeechCardForSave,
        normalizeAudioTranscriptionCardForSave,
        loadAudioEditor,
        renderAudioSpeech,
        renderAudioTranscriptions,
        buildAudioSpeechCard,
        buildAudioTranscriptionCard,
        buildAudioCard,
        buildAudioSpeechRouteRow,
        buildAudioTranscriptionRouteRow,
        buildAudioRouteRow,
        saveAudio,
        createAccordionToggle,
        createWebCardShell,
        appendFieldHint,
        attachFieldTooltip,
        buildWebSearchCard,
        buildWebReadCard,
        normalizeWebSearchCardForSave,
        normalizeWebReadCardForSave,
        buildWebReferenceCard,
        normalizeWebReferenceCardForSave,
        getWebPayloadForSave,
        getNormalizedWebContent,
        loadWebEditor,
        renderWebSections,
        saveWeb,
        loadModelRulesEditor,
        saveModelRules,
        WEB_SEARCH_CARD_OPTIONS,
        WEB_READ_CARD_OPTIONS,
        WEB_RESEARCH_CARD_OPTIONS,
        WEB_DEEP_RESEARCH_CARD_OPTIONS,
    });
}
