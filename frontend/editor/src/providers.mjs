export function registerProviders(ctx) {

    async function getProviderModels(providerName) {
        if (!providerName) {
            return [];
        }

        const cachedEntry = ctx.state.providerModelsCache.get(providerName);
        if (cachedEntry && Date.now() - cachedEntry.fetchedAt < ctx.constants.MODELS_CACHE_TTL_MS) {
            return cachedEntry.models;
        }

        const existingRequest = ctx.state.providerModelsRequests.get(providerName);
        if (existingRequest) {
            return existingRequest;
        }

        const requestEpoch = ctx.state.providerModelsCacheEpoch;
        let requestPromise;
        requestPromise = ctx.apiFetch(`/v1/config/providers/${encodeURIComponent(providerName)}/models`)
            .then(async response => {
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(payload.detail || `HTTP ${response.status}`);
                }

                const models = Array.isArray(payload.models)
                    ? payload.models.map(item => item.id).filter(modelId => typeof modelId === 'string')
                    : [];
                const sortedModels = ctx.sortProviderModelIds(models);

                if (ctx.state.providerModelsCacheEpoch === requestEpoch) {
                    ctx.state.providerModelsCache.set(providerName, {
                        models: sortedModels,
                        fetchedAt: Date.now(),
                    });
                }
                return sortedModels;
            })
            .finally(() => {
                if (ctx.state.providerModelsRequests.get(providerName) === requestPromise) {
                    ctx.state.providerModelsRequests.delete(providerName);
                }
            });

        ctx.state.providerModelsRequests.set(providerName, requestPromise);
        return requestPromise;
    }

    function catalogStatusText(status) {
        const values = {
            count: status.count || 0,
            error: status.error || '',
            model: status.model || '',
            provider: status.provider || '',
        };
        const key = status.state === 'idle' && !status.provider
            ? 'noProvider'
            : status.state;
        return window.gatewayI18n.t(`editor:catalog.${key}`, values);
    }

    function populateProviderModelDataList(dataList, models) {
        dataList.textContent = '';
        models.forEach(modelId => {
            const option = document.createElement('option');
            option.value = modelId;
            dataList.appendChild(option);
        });
    }

    function createLazyProviderCatalogRowController(options) {
        const {
            row,
            providerSelect,
            modelControl,
            dataList = null,
            modelStatus,
            requireListedModel = false,
        } = options;
        const statusText = document.createElement('span');
        const retryButton = document.createElement('button');
        retryButton.type = 'button';
        retryButton.className = 'model-catalog-retry secondary-button';
        modelStatus.textContent = '';
        modelStatus.setAttribute('aria-live', 'polite');
        modelStatus.setAttribute('aria-atomic', 'true');
        modelStatus.appendChild(statusText);
        modelStatus.appendChild(retryButton);

        let rowGeneration = 0;
        let availableModels = null;
        let status = {
            state: 'idle',
            provider: providerSelect.value.trim(),
            model: modelControl.value.trim(),
        };

        function render() {
            modelStatus.dataset.state = status.state;
            statusText.textContent = catalogStatusText(status);
            retryButton.textContent = window.gatewayI18n.t('editor:catalog.retry');
            retryButton.hidden = status.state !== 'error';
        }

        function setStatus(state, values = {}) {
            status = {
                state,
                provider: providerSelect.value.trim(),
                model: modelControl.value.trim(),
                ...values,
            };
            render();
        }

        function applyModels(models, selectedModel) {
            if (dataList) {
                populateProviderModelDataList(dataList, models);
                return;
            }
            modelControl.disabled = false;
            const optionsWithSavedModel = selectedModel && !models.includes(selectedModel)
                ? [selectedModel, ...models]
                : models;
            ctx.setSelectOptions(
                modelControl,
                optionsWithSavedModel,
                models.length > 0 ? 'Choose a model' : 'No models available',
                selectedModel,
            );
        }

        async function load() {
            const provider = providerSelect.value.trim();
            const selectedModel = modelControl.value.trim();
            const loadGeneration = ++rowGeneration;
            const pageGeneration = ctx.state.providerCatalogGeneration;
            if (!provider) {
                row.dataset.modelsLoadError = 'false';
                ctx.clearUnavailableFallbackModelMetadata(row);
                setStatus('idle');
                return [];
            }

            const cachedEntry = ctx.state.providerModelsCache.get(provider);
            if (
                cachedEntry
                && Date.now() - cachedEntry.fetchedAt < ctx.constants.MODELS_CACHE_TTL_MS
                && status.provider === provider
                && ['ready', 'empty', 'unavailable'].includes(status.state)
            ) {
                return cachedEntry.models;
            }

            row.dataset.modelsLoadError = 'false';
            ctx.clearUnavailableFallbackModelMetadata(row);
            setStatus('loading', { provider, model: selectedModel });
            try {
                const models = await getProviderModels(provider);
                if (
                    rowGeneration !== loadGeneration
                    || ctx.state.providerCatalogGeneration !== pageGeneration
                    || !row.isConnected
                    || providerSelect.value.trim() !== provider
                ) {
                    return models;
                }
                applyModels(models, selectedModel);
                availableModels = models;
                if (selectedModel && !models.includes(selectedModel)) {
                    row.dataset.modelsLoadError = requireListedModel ? 'true' : 'false';
                    row.dataset.unavailableModel = selectedModel;
                    row.dataset.unavailableProvider = provider;
                    setStatus('unavailable', { provider, model: selectedModel });
                } else {
                    setStatus(models.length > 0 ? 'ready' : 'empty', {
                        count: models.length,
                        provider,
                    });
                }
                return models;
            } catch (error) {
                if (
                    rowGeneration !== loadGeneration
                    || ctx.state.providerCatalogGeneration !== pageGeneration
                    || !row.isConnected
                    || providerSelect.value.trim() !== provider
                ) {
                    return [];
                }
                row.dataset.modelsLoadError = 'false';
                setStatus('error', {
                    error: ctx.boundedSafeText(ctx.safeClientError(error)),
                    provider,
                    model: selectedModel,
                });
                return [];
            }
        }

        function resetForProviderChange() {
            rowGeneration += 1;
            availableModels = null;
            row.dataset.modelsLoadError = 'false';
            ctx.clearUnavailableFallbackModelMetadata(row);
            modelControl.value = '';
            if (dataList) {
                dataList.textContent = '';
            } else {
                modelControl.disabled = true;
                ctx.setSelectOptions(
                    modelControl,
                    [],
                    providerSelect.value.trim() ? 'Loading models...' : 'Choose a provider first',
                    '',
                );
            }
            void load();
        }

        function invalidate() {
            rowGeneration += 1;
            if (status.state === 'loading') {
                setStatus('idle');
            }
        }

        providerSelect.addEventListener('change', resetForProviderChange);
        modelControl.addEventListener('focus', () => {
            void load();
        });
        modelControl.addEventListener('pointerdown', () => {
            void load();
        });
        retryButton.addEventListener('click', () => {
            void load();
        });

        const controller = Object.freeze({
            invalidate,
            isConnected: () => row.isConnected,
            load,
            markSelected(model) {
                if (availableModels && !availableModels.includes(model)) {
                    row.dataset.modelsLoadError = requireListedModel ? 'true' : 'false';
                    row.dataset.unavailableModel = model;
                    row.dataset.unavailableProvider = providerSelect.value.trim();
                    setStatus('unavailable', { model });
                    return;
                }
                row.dataset.modelsLoadError = 'false';
                ctx.clearUnavailableFallbackModelMetadata(row);
                setStatus('selected', { model });
            },
            render,
        });
        ctx.state.providerCatalogControllers.add(controller);
        ctx.state.providerCatalogControllerByRow.set(row, controller);
        render();
        return controller;
    }

    function invalidateProviderCatalogRows() {
        ctx.state.providerCatalogGeneration += 1;
        ctx.state.providerCatalogControllers.forEach(controller => {
            if (!controller.isConnected()) {
                ctx.state.providerCatalogControllers.delete(controller);
                return;
            }
            controller.invalidate();
        });
    }

    function rerenderProviderCatalogStatuses() {
        ctx.state.providerCatalogControllers.forEach(controller => {
            if (!controller.isConnected()) {
                ctx.state.providerCatalogControllers.delete(controller);
                return;
            }
            controller.render();
        });
    }

    function loadProviderCatalogsInCard(card) {
        const loads = Array.from(card.querySelectorAll('.fallback-row'))
            .map(row => ctx.state.providerCatalogControllerByRow.get(row))
            .filter(Boolean)
            .map(controller => controller.load());
        return Promise.allSettled(loads);
    }

    function toggleProviderCatalogCard(card) {
        const isCollapsed = card.classList.toggle('collapsed');
        if (!isCollapsed) {
            void loadProviderCatalogsInCard(card);
        }
    }

    function clearRulesCache() {
        ctx.state.providerModelsCacheEpoch += 1;
        ctx.state.providerModelsCache.clear();
        ctx.state.providerModelsRequests.clear();
    }

    function parseProviderModelsMetadata(value) {
        const trimmedValue = value.trim();
        if (!trimmedValue) {
            return undefined;
        }

        try {
            const parsedValue = JSON.parse(trimmedValue);
            return parsedValue === null ? undefined : parsedValue;
        } catch (error) {
            throw new Error('Provider models metadata must be valid JSON.');
        }
    }

    function normalizeProviderModelsMetadata(value) {
        if (value === undefined || value === null) {
            return '';
        }
        return JSON.stringify(value, null, 2);
    }

    function parseProviderJsonObject(value, label) {
        const trimmedValue = value.trim();
        if (!trimmedValue) {
            return undefined;
        }

        let parsedValue;
        try {
            parsedValue = JSON.parse(trimmedValue);
        } catch (error) {
            throw new Error(`${label} must be valid JSON.`);
        }

        if (parsedValue === null) {
            return undefined;
        }
        if (typeof parsedValue !== 'object' || Array.isArray(parsedValue)) {
            throw new Error(`${label} must be a JSON object.`);
        }
        return parsedValue;
    }

    function normalizeProviderJsonObject(value) {
        if (value === undefined || value === null) {
            return '';
        }
        return JSON.stringify(value, null, 2);
    }

    function parseAvailableModels(value) {
        const items = String(value || '')
            .split(/[\n,]/)
            .map(item => item.trim())
            .filter(Boolean);
        const seen = new Set();
        const result = [];
        items.forEach(item => {
            if (seen.has(item)) {
                return;
            }
            seen.add(item);
            result.push(item);
        });
        return result;
    }

    function normalizeAvailableModels(value) {
        return Array.isArray(value) ? value.join('\n') : '';
    }

    const PROVIDER_FIELD_TOOLTIPS = {
        name: 'editor:tooltips.providerName',
        baseUrl: 'editor:tooltips.baseUrl',
        apikey: 'editor:tooltips.apiKey',
        type: 'editor:tooltips.apiType',
        proxy: 'editor:tooltips.proxy',
        modelsMetadata: 'editor:tooltips.modelsMetadata',
        availableModels: 'editor:tooltips.availableModels',
        customHeaders: 'editor:tooltips.providerCustomHeaders',
        routing: 'editor:tooltips.routing',
        upstreamKeyPools: 'editor:tooltips.upstreamKeyPools',
        upstreamLimits: 'editor:tooltips.upstreamLimits',
        modelId: 'editor:tooltips.modelId',
        rpm: 'editor:tooltips.rpm',
        rpd: 'editor:tooltips.rpd',
        tpm: 'editor:tooltips.tpm',
        tpd: 'editor:tooltips.tpd',
    };

    const UPSTREAM_LIMIT_KEYS = ['rpm', 'rpd', 'tpm', 'tpd'];

    function splitProviderModelsMetadata(value) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) {
            return { upstreamLimits: [], extra: value === undefined ? undefined : value };
        }
        const upstreamLimits = [];
        const extra = {};
        Object.entries(value).forEach(([modelId, modelMeta]) => {
            if (modelMeta && typeof modelMeta === 'object' && !Array.isArray(modelMeta) &&
                modelMeta.upstream_limits && typeof modelMeta.upstream_limits === 'object') {
                const limits = modelMeta.upstream_limits;
                const row = { modelId };
                UPSTREAM_LIMIT_KEYS.forEach(key => {
                    row[key] = limits[key] === undefined || limits[key] === null ? '' : String(limits[key]);
                });
                upstreamLimits.push(row);
                const rest = { ...modelMeta };
                delete rest.upstream_limits;
                if (Object.keys(rest).length > 0) {
                    extra[modelId] = rest;
                }
            } else {
                extra[modelId] = modelMeta;
            }
        });
        return {
            upstreamLimits,
            extra: Object.keys(extra).length > 0 ? extra : undefined,
        };
    }

    function buildUpstreamLimitsSection(initialModels) {
        const container = document.createElement('div');
        container.className = 'upstream-limits-section';

        const header = document.createElement('div');
        header.className = 'upstream-limits-section-header';

        const title = document.createElement('div');
        title.className = 'upstream-limits-title field-label';
        ctx.bindLocalizedText(title, 'editor:fields.upstreamLimits');
        header.appendChild(title);
        const titleFieldWrapper = { querySelector: () => title };
        ctx.attachFieldTooltip(titleFieldWrapper, PROVIDER_FIELD_TOOLTIPS.upstreamLimits);

        const addButton = document.createElement('button');
        addButton.type = 'button';
        addButton.className = 'secondary-button upstream-limit-add';
        ctx.bindKnownActionText(addButton, 'Add Model');
        header.appendChild(addButton);

        container.appendChild(header);

        const list = document.createElement('div');
        list.className = 'upstream-limits-list';
        container.appendChild(list);

        const emptyState = document.createElement('div');
        emptyState.className = 'upstream-limits-empty';
        ctx.bindLocalizedText(emptyState, 'editor:messages.noUpstreamLimits');
        container.appendChild(emptyState);

        function refreshEmptyState() {
            emptyState.hidden = list.children.length > 0;
        }

        function appendRow(initialRow) {
            const row = document.createElement('div');
            row.className = 'upstream-limit-row';

            const modelInput = ctx.createTextInput('upstream-limit-model', 'deepseek/deepseek-r1:free');
            modelInput.value = initialRow && initialRow.modelId ? initialRow.modelId : '';
            const modelField = ctx.createFieldGroup('Model', modelInput, 'upstream-limit-model-field');
            ctx.attachFieldTooltip(modelField, PROVIDER_FIELD_TOOLTIPS.modelId);
            row.appendChild(modelField);

            UPSTREAM_LIMIT_KEYS.forEach(key => {
                const input = ctx.createNumberInput(`upstream-limit-${key}`, '');
                input.min = '1';
                input.value = initialRow && initialRow[key] !== undefined ? initialRow[key] : '';
                const field = ctx.createFieldGroup(key.toUpperCase(), input, `upstream-limit-${key}-field`);
                ctx.attachFieldTooltip(field, PROVIDER_FIELD_TOOLTIPS[key]);
                row.appendChild(field);
            });

            const removeButton = document.createElement('button');
            removeButton.type = 'button';
            removeButton.className = 'upstream-limit-remove';
            ctx.bindKnownActionText(removeButton, 'Remove');
            removeButton.addEventListener('click', () => {
                row.remove();
                refreshEmptyState();
            });
            row.appendChild(removeButton);

            list.appendChild(row);
            refreshEmptyState();
        }

        const splitInitial = splitProviderModelsMetadata(initialModels);
        splitInitial.upstreamLimits.forEach(appendRow);
        refreshEmptyState();

        addButton.addEventListener('click', () => appendRow());

        function getRows() {
            return Array.from(list.querySelectorAll('.upstream-limit-row')).map(row => {
                const result = {
                    modelId: row.querySelector('.upstream-limit-model').value.trim(),
                };
                UPSTREAM_LIMIT_KEYS.forEach(key => {
                    result[key] = row.querySelector(`.upstream-limit-${key}`).value.trim();
                });
                return result;
            });
        }

        return { container, getRows };
    }

    function mergeUpstreamLimitsIntoModels(extraMetadata, rows, providerName) {
        const merged = (extraMetadata && typeof extraMetadata === 'object' && !Array.isArray(extraMetadata))
            ? { ...extraMetadata }
            : {};
        const seen = new Set();
        rows.forEach((row, index) => {
            const modelId = row.modelId;
            if (!modelId) {
                const hasAnyValue = UPSTREAM_LIMIT_KEYS.some(key => row[key]);
                if (hasAnyValue) {
                    throw new Error(`Provider '${providerName}' upstream limits row #${index + 1} is missing a model id.`);
                }
                return;
            }
            if (seen.has(modelId)) {
                throw new Error(`Provider '${providerName}' has duplicate upstream limits for model '${modelId}'.`);
            }
            seen.add(modelId);
            const limits = {};
            UPSTREAM_LIMIT_KEYS.forEach(key => {
                const raw = row[key];
                if (raw === '' || raw === undefined) return;
                const parsed = Number(raw);
                if (!Number.isInteger(parsed) || parsed <= 0) {
                    throw new Error(`Provider '${providerName}' model '${modelId}' ${key} must be a positive integer.`);
                }
                limits[key] = parsed;
            });
            if (Object.keys(limits).length === 0) return;
            const base = (merged[modelId] && typeof merged[modelId] === 'object' && !Array.isArray(merged[modelId]))
                ? { ...merged[modelId] }
                : {};
            base.upstream_limits = limits;
            merged[modelId] = base;
        });
        return Object.keys(merged).length > 0 ? merged : undefined;
    }

    function normalizeProviderCardForSave(providerCard) {
        const nameInput = providerCard.querySelector('.provider-name-input');
        const baseUrlInput = providerCard.querySelector('.provider-base-url-input');
        const apiKeyInput = providerCard.querySelector('.provider-api-key-input');
        const typeSelect = providerCard.querySelector('.provider-type-select');
        const proxyInput = providerCard.querySelector('.provider-proxy-input');
        const modelsInput = providerCard.querySelector('.provider-models-input');
        const routingInput = providerCard.querySelector('.provider-routing-input');
        const upstreamKeyPoolsInput = providerCard.querySelector('.provider-upstream-key-pools-input');

        const name = nameInput.value.trim();
        const baseUrl = baseUrlInput.value.trim();
        const apikey = apiKeyInput.value.trim();
        const type = typeSelect.value.trim();
        const proxy = proxyInput.value.trim();

        if (!name) {
            throw new Error('Each provider must have a name.');
        }
        if (!baseUrl) {
            throw new Error(`Provider '${name}' must have a base URL.`);
        }
        if (!/^https?:\/\//i.test(baseUrl)) {
            throw new Error(`Provider '${name}' base URL must start with http:// or https://.`);
        }
        if (!['openai', 'anthropic'].includes(type)) {
            throw new Error(`Provider '${name}' must use API type openai or anthropic.`);
        }
        const routing = parseProviderJsonObject(routingInput ? routingInput.value : '', 'Provider routing');
        const upstreamKeyPools = parseProviderJsonObject(
            upstreamKeyPoolsInput ? upstreamKeyPoolsInput.value : '',
            'Provider upstream key pools',
        );
        if (!apikey && !upstreamKeyPools) {
            throw new Error(`Provider '${name}' must have an API key, environment reference, or upstream key pool.`);
        }

        const providerPayload = {
            name,
            baseUrl,
            type,
        };
        if (apikey) {
            providerPayload.apikey = apikey;
        }
        if (proxy) {
            providerPayload.proxy = proxy;
        }
        if (routing) {
            providerPayload.routing = routing;
        }
        if (upstreamKeyPools) {
            providerPayload.upstream_key_pools = upstreamKeyPools;
        }
        const extraModels = parseProviderModelsMetadata(modelsInput.value);
        const upstreamRows = providerCard._getUpstreamLimitsRows ? providerCard._getUpstreamLimitsRows() : [];
        const mergedModels = mergeUpstreamLimitsIntoModels(extraModels, upstreamRows, name);
        if (mergedModels !== undefined) {
            providerPayload.models = mergedModels;
        } else if (extraModels !== undefined) {
            providerPayload.models = extraModels;
        }
        const availableModelsInput = providerCard.querySelector('.provider-available-models-input');
        const availableModels = parseAvailableModels(availableModelsInput ? availableModelsInput.value : '');
        if (availableModels.length > 0) {
            providerPayload.available_models = availableModels;
        }
        const customHeadersInput = providerCard.querySelector('.provider-custom-headers-input');
        const customHeaders = ctx.parseObjectTextarea(
            customHeadersInput ? customHeadersInput.value : '',
            'Custom headers',
        );
        if (Object.keys(customHeaders).length > 0) {
            providerPayload.custom_headers = customHeaders;
        }
        return providerPayload;
    }

    function getProvidersPayloadForSave() {
        const providers = Array.from(ctx.elements.providersList.querySelectorAll('.provider-card')).map(normalizeProviderCardForSave);
        const seenProviderNames = new Set();
        const duplicateNames = [];
        providers.forEach(provider => {
            if (seenProviderNames.has(provider.name)) {
                duplicateNames.push(provider.name);
            }
            seenProviderNames.add(provider.name);
        });
        if (duplicateNames.length > 0) {
            throw new Error(`Duplicate provider names: ${duplicateNames.join(', ')}.`);
        }
        return { providers };
    }

    function getProvidersSnapshotContent() {
        return ctx.stableSerialize(getProvidersPayloadForSave());
    }

    function buildProviderCard(initialData = {}) {
        const card = document.createElement('section');
        card.className = 'rule-card provider-card collapsed';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';
        const providerNameInput = ctx.createTextInput('provider-name-input', 'openrouter');
        providerNameInput.value = initialData.name || '';
        const providerNameField = ctx.createFieldGroup('Provider Name', providerNameInput, 'gateway-model-field');
        ctx.attachFieldTooltip(providerNameField, PROVIDER_FIELD_TOOLTIPS.name);
        titleWrap.appendChild(providerNameField);

        const headerLeft = document.createElement('div');
        headerLeft.className = 'rule-card-header-left';
        headerLeft.appendChild(ctx.createAccordionToggle(card));
        headerLeft.appendChild(titleWrap);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, 'Remove Provider');
        removeButton.addEventListener('click', () => {
            card.remove();
            ctx.refreshProvidersEmptyState();
        });

        cardHeader.appendChild(headerLeft);
        cardHeader.appendChild(removeButton);

        const cardBody = document.createElement('div');
        cardBody.className = 'rule-card-body';

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid provider-fields-grid';

        const baseUrlInput = ctx.createTextInput('provider-base-url-input', 'https://api.example.com/v1');
        baseUrlInput.value = initialData.baseUrl || '';
        const apiKeyInput = ctx.createTextInput('provider-api-key-input', '${APIKEY_PROVIDER}');
        apiKeyInput.type = 'password';
        apiKeyInput.autocomplete = 'off';
        apiKeyInput.value = initialData.apikey || '';
        const apiKeyRevealButton = document.createElement('button');
        apiKeyRevealButton.type = 'button';
        apiKeyRevealButton.className = 'secondary-button compact-button';
        ctx.bindLocalizedText(
            apiKeyRevealButton,
            () => apiKeyInput.type === 'password'
                ? 'editor:actions.show'
                : 'editor:actions.hide',
        );
        apiKeyRevealButton.addEventListener('click', () => {
            const shouldShow = apiKeyInput.type === 'password';
            apiKeyInput.type = shouldShow ? 'text' : 'password';
            ctx.rerenderLocale();
        });
        const apiKeyControl = document.createElement('div');
        apiKeyControl.className = 'secret-input-row';
        apiKeyControl.appendChild(apiKeyInput);
        apiKeyControl.appendChild(apiKeyRevealButton);
        const typeSelect = ctx.createSelect('provider-type-select');
        ctx.setSelectOptions(typeSelect, ['openai', 'anthropic'], 'Choose API type', initialData.type || 'openai');
        const proxyInput = ctx.createTextInput('provider-proxy-input', '${PROXY_PROVIDER} or https://proxy:8080');
        proxyInput.value = initialData.proxy || '';

        const baseUrlField = ctx.createFieldGroup('Base URL', baseUrlInput, 'provider-base-url-field');
        ctx.attachFieldTooltip(baseUrlField, PROVIDER_FIELD_TOOLTIPS.baseUrl);
        const apiKeyField = ctx.createFieldGroup('API Key', apiKeyControl, 'provider-api-key-field');
        ctx.attachFieldTooltip(apiKeyField, PROVIDER_FIELD_TOOLTIPS.apikey);
        const typeField = ctx.createFieldGroup('API Type', typeSelect, 'provider-type-field');
        ctx.attachFieldTooltip(typeField, PROVIDER_FIELD_TOOLTIPS.type);
        const proxyField = ctx.createFieldGroup('Proxy (optional)', proxyInput, 'provider-proxy-field');
        ctx.attachFieldTooltip(proxyField, PROVIDER_FIELD_TOOLTIPS.proxy);

        fieldsGrid.appendChild(baseUrlField);
        fieldsGrid.appendChild(apiKeyField);
        fieldsGrid.appendChild(typeField);
        fieldsGrid.appendChild(proxyField);

        const advancedDetails = document.createElement('details');
        advancedDetails.className = 'advanced-options';
        const advancedSummary = document.createElement('summary');
        ctx.bindLocalizedText(advancedSummary, 'editor:actions.advanced');
        advancedDetails.appendChild(advancedSummary);

        const advancedGrid = document.createElement('div');
        advancedGrid.className = 'advanced-grid';

        const { container: upstreamLimitsContainer, getRows: getUpstreamLimitsRows } =
            buildUpstreamLimitsSection(initialData.models);
        advancedGrid.appendChild(upstreamLimitsContainer);

        const splitModels = splitProviderModelsMetadata(initialData.models);
        const modelsInput = ctx.createTextarea('provider-models-input', '{"pricing": {"input": 0.1}}');
        modelsInput.value = normalizeProviderModelsMetadata(splitModels.extra);
        const modelsField = ctx.createFieldGroup('Models Metadata (JSON)', modelsInput, 'textarea-group');
        ctx.attachFieldTooltip(modelsField, PROVIDER_FIELD_TOOLTIPS.modelsMetadata);
        ctx.appendFieldHint(modelsField, 'editor:hints.providerMetadata');
        advancedGrid.appendChild(modelsField);

        const availableModelsInput = ctx.createTextarea('provider-available-models-input', 'deepseek/deepseek-r1:free\nqwen/qwen3-max');
        availableModelsInput.value = normalizeAvailableModels(initialData.available_models);
        const availableModelsField = ctx.createFieldGroup('Available Models (optional)', availableModelsInput, 'textarea-group');
        ctx.attachFieldTooltip(availableModelsField, PROVIDER_FIELD_TOOLTIPS.availableModels);
        ctx.appendFieldHint(availableModelsField, 'editor:hints.providerAvailableModels');
        advancedGrid.appendChild(availableModelsField);

        const customHeadersInput = ctx.createTextarea(
            'provider-custom-headers-input',
            '{"User-Agent": "Cline/1.0"}',
        );
        customHeadersInput.value = ctx.normalizeObjectTextarea(initialData.custom_headers);
        const customHeadersField = ctx.createFieldGroup('Custom Headers', customHeadersInput, 'textarea-group');
        ctx.attachFieldTooltip(customHeadersField, PROVIDER_FIELD_TOOLTIPS.customHeaders);
        ctx.appendFieldHint(customHeadersField, 'editor:hints.providerCustomHeaders');
        advancedGrid.appendChild(customHeadersField);

        const routingInput = ctx.createTextarea(
            'provider-routing-input',
            '{"strategy": "round-robin", "session_affinity": false}',
        );
        routingInput.value = normalizeProviderJsonObject(initialData.routing);
        const routingField = ctx.createFieldGroup('Routing Policy (JSON)', routingInput, 'textarea-group');
        ctx.attachFieldTooltip(routingField, PROVIDER_FIELD_TOOLTIPS.routing);
        ctx.appendFieldHint(routingField, 'editor:hints.providerRouting');
        advancedGrid.appendChild(routingField);

        const upstreamKeyPoolsInput = ctx.createTextarea(
            'provider-upstream-key-pools-input',
            '{"main": {"strategy": "priority", "keys": [{"id": "primary", "apikey": "${PROVIDER_KEY_1}", "priority": 100}]}}',
        );
        upstreamKeyPoolsInput.value = normalizeProviderJsonObject(initialData.upstream_key_pools);
        const upstreamKeyPoolsField = ctx.createFieldGroup('Upstream Key Pools (JSON)', upstreamKeyPoolsInput, 'textarea-group');
        ctx.attachFieldTooltip(upstreamKeyPoolsField, PROVIDER_FIELD_TOOLTIPS.upstreamKeyPools);
        ctx.appendFieldHint(upstreamKeyPoolsField, 'editor:hints.providerKeyPools');
        advancedGrid.appendChild(upstreamKeyPoolsField);

        advancedDetails.appendChild(advancedGrid);

        cardBody.appendChild(fieldsGrid);
        cardBody.appendChild(advancedDetails);
        card.appendChild(cardHeader);
        card.appendChild(cardBody);

        card._getUpstreamLimitsRows = getUpstreamLimitsRows;

        return card;
    }

    async function renderProviders(providers) {
        ctx.elements.providersList.textContent = '';
        if (!Array.isArray(providers) || providers.length === 0) {
            ctx.refreshProvidersEmptyState();
            return;
        }

        providers.forEach(provider => {
            ctx.elements.providersList.appendChild(buildProviderCard(provider));
        });
        ctx.refreshProvidersEmptyState();
    }

    async function loadProvidersEditor() {
        const requestId = ++ctx.state.providersLoadRequestId;
        ctx.state.originalProvidersContent = null;
        ctx.setProvidersLoadState('loading');
        ctx.showLocalizedMessage('info', 'Loading Providers...');
        try {
            const loaded = await ctx.loadConfigDocument(
                'providers',
                '/v1/config/providers/structured',
                {
                    validate: ctx.validateProvidersPayload,
                    apply: async payload => {
                        if (requestId !== ctx.state.providersLoadRequestId) {
                            return;
                        }
                        await renderProviders(payload.providers);
                        ctx.state.availableProviders = payload.providers
                            .map(provider => typeof provider.name === 'string' ? provider.name.trim() : '')
                            .filter(Boolean);
                    },
                }
            );
            if (!loaded || requestId !== ctx.state.providersLoadRequestId) {
                if (!loaded) {
                    ctx.showLocalizedMessage('warning', 'A newer local edit was preserved. Reload again to discard it.');
                }
                return false;
            }
            ctx.state.originalProvidersContent = getProvidersSnapshotContent();
            ctx.setProvidersLoadState('ready');
            ctx.showLocalizedMessage('success', 'Providers loaded successfully.');
            return true;
        } catch (error) {
            if (requestId !== ctx.state.providersLoadRequestId) {
                return false;
            }
            console.error('Error fetching Providers:', error);
            ctx.showLocalizedError('Error loading Providers:', error);
            ctx.state.originalProvidersContent = null;
            ctx.setProvidersLoadState('error');
            return false;
        }
    }

    async function saveProviders() {
        if (ctx.state.providersLoadState !== 'ready' || ctx.state.originalProvidersContent === null) {
            ctx.showLocalizedMessage('error', 'Cannot save Providers: provider configuration has not loaded successfully.');
            return;
        }

        let payload;
        try {
            payload = ctx.getProvidersPayloadForSave();
        } catch (error) {
            ctx.showClientValidationError(error);
            return;
        }

        ctx.elements.saveButton.disabled = true;
        ctx.showLocalizedMessage('info', 'Saving Providers...');

        try {
            const result = await ctx.saveConfigDocument(
                'providers',
                '/v1/config/providers/structured',
                payload,
                {
                    errorTitle: 'Error saving Providers:',
                    validatePublished: ctx.validateProvidersPayload,
                }
            );
            if (!result) {
                return;
            }
            if (ctx.state.editorMutationVersion === result.submittedMutationVersion) {
                const application = ctx.renderProviders(result.payload.providers);
                ctx.syncInteractionLock();
                await application;
                ctx.state.originalProvidersContent = ctx.getProvidersSnapshotContent();
                ctx.clearRulesCache();
                ctx.state.availableProviders = result.payload.providers
                    .map(provider => typeof provider.name === 'string' ? provider.name.trim() : '')
                    .filter(Boolean);
            }
            ctx.showLocalizedMessage(
                'success',
                ctx.safeSuccessMessage(result.body, 'Providers updated successfully.')
            );
        } catch (error) {
            console.error('Error saving Providers:', error);
            ctx.showLocalizedError('Error saving Providers:', error);
        } finally {
            ctx.updateProvidersControlsState();
        }
    }


    Object.assign(ctx, {
        getProviderModels,
        catalogStatusText,
        populateProviderModelDataList,
        createLazyProviderCatalogRowController,
        invalidateProviderCatalogRows,
        rerenderProviderCatalogStatuses,
        loadProviderCatalogsInCard,
        toggleProviderCatalogCard,
        clearRulesCache,
        parseProviderModelsMetadata,
        normalizeProviderModelsMetadata,
        parseProviderJsonObject,
        normalizeProviderJsonObject,
        parseAvailableModels,
        normalizeAvailableModels,
        splitProviderModelsMetadata,
        buildUpstreamLimitsSection,
        mergeUpstreamLimitsIntoModels,
        normalizeProviderCardForSave,
        getProvidersPayloadForSave,
        getProvidersSnapshotContent,
        buildProviderCard,
        renderProviders,
        loadProvidersEditor,
        saveProviders,
    });
}
