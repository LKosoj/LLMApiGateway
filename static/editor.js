document.addEventListener('DOMContentLoaded', function () {
    const { apiFetch } = window.gatewayAuth;

    const messageArea = document.getElementById('messageArea');
    const darkModeToggle = document.getElementById('darkModeToggle');
    const bodyElement = document.body;
    const saveButton = document.getElementById('saveButton');
    const addRuleButton = document.getElementById('addRuleButton');
    const rulesList = document.getElementById('rulesList');
    const rulesEmptyState = document.getElementById('rulesEmptyState');

    const tabRules = document.getElementById('tabRules');
    const tabEmbeddings = document.getElementById('tabEmbeddings');
    const tabRerank = document.getElementById('tabRerank');
    const tabImages = document.getElementById('tabImages');
    const tabAudio = document.getElementById('tabAudio');
    const tabWeb = document.getElementById('tabWeb');
    const tabOpenRouterFree = document.getElementById('tabOpenRouterFree');
    const tabProviders = document.getElementById('tabProviders');
    const editorContainerRules = document.getElementById('editor-container-rules');
    const editorContainerEmbeddings = document.getElementById('editor-container-embeddings');
    const editorContainerRerank = document.getElementById('editor-container-rerank');
    const editorContainerImages = document.getElementById('editor-container-images');
    const editorContainerAudio = document.getElementById('editor-container-audio');
    const editorContainerWeb = document.getElementById('editor-container-web');
    const editorContainerOpenRouterFree = document.getElementById('editor-container-openrouter-free');
    const editorContainerProviders = document.getElementById('editor-container-providers');
    const addProviderButton = document.getElementById('addProviderButton');
    const providersList = document.getElementById('providersList');
    const providersEmptyState = document.getElementById('providersEmptyState');
    const addEmbeddingButton = document.getElementById('addEmbeddingButton');
    const embeddingsList = document.getElementById('embeddingsList');
    const embeddingsEmptyState = document.getElementById('embeddingsEmptyState');
    const addRerankButton = document.getElementById('addRerankButton');
    const rerankList = document.getElementById('rerankList');
    const rerankEmptyState = document.getElementById('rerankEmptyState');
    const addImageGenerationButton = document.getElementById('addImageGenerationButton');
    const imageGenerationList = document.getElementById('imageGenerationList');
    const imageGenerationEmptyState = document.getElementById('imageGenerationEmptyState');
    const addImageEditButton = document.getElementById('addImageEditButton');
    const imageEditList = document.getElementById('imageEditList');
    const imageEditEmptyState = document.getElementById('imageEditEmptyState');
    const addAudioSpeechButton = document.getElementById('addAudioSpeechButton');
    const audioSpeechList = document.getElementById('audioSpeechList');
    const audioSpeechEmptyState = document.getElementById('audioSpeechEmptyState');
    const addAudioTranscriptionButton = document.getElementById('addAudioTranscriptionButton');
    const audioTranscriptionsList = document.getElementById('audioTranscriptionsList');
    const audioTranscriptionsEmptyState = document.getElementById('audioTranscriptionsEmptyState');
    const addWebSearchButton = document.getElementById('addWebSearchButton');
    const webSearchList = document.getElementById('webSearchList');
    const webSearchEmptyState = document.getElementById('webSearchEmptyState');
    const addWebReadButton = document.getElementById('addWebReadButton');
    const webReadList = document.getElementById('webReadList');
    const webReadEmptyState = document.getElementById('webReadEmptyState');
    const addWebResearchButton = document.getElementById('addWebResearchButton');
    const webResearchList = document.getElementById('webResearchList');
    const webResearchEmptyState = document.getElementById('webResearchEmptyState');
    const addWebDeepResearchButton = document.getElementById('addWebDeepResearchButton');
    const webDeepResearchList = document.getElementById('webDeepResearchList');
    const webDeepResearchEmptyState = document.getElementById('webDeepResearchEmptyState');
    const openRouterFreeStatus = document.getElementById('openRouterFreeStatus');
    const openRouterFreeModels = document.getElementById('openRouterFreeModels');
    const openRouterFreeEmptyState = document.getElementById('openRouterFreeEmptyState');

    const MODELS_CACHE_TTL_MS = 15 * 60 * 1000;
    const IMAGE_REQUEST_FORMAT_OPTIONS = ['openai_images', 'openai_images_multipart', 'nvidia_genai_json'];
    const IMAGE_RESPONSE_FORMAT_OPTIONS = ['openai_images', 'nvidia_artifacts'];
    const AUDIO_REQUEST_FORMAT_OPTIONS = ['nvidia_riva_grpc'];

    let activeEditor = 'rules';
    let originalRulesContent = null;
    let originalEmbeddingsContent = null;
    let originalRerankContent = null;
    let originalImagesContent = null;
    let originalAudioContent = null;
    let originalWebContent = null;
    let originalProvidersContent = null;
    let availableProviders = [];
    let embeddingRules = [];
    let rerankRules = [];
    let imageGenerationRules = [];
    let imageEditRules = [];
    let audioSpeechRules = [];
    let audioTranscriptionRules = [];
    let pdfConversionRules = [];
    let webSearchRules = [];
    let webReadRules = [];
    let webResearchRules = [];
    let webDeepResearchRules = [];
    let gatewayModelCatalog = {
        chat: [],
        embeddings: [],
        images_generations: [],
        web_search: [],
        web_read: [],
    };
    const providerModelsCache = new Map();
    const providerModelsRequests = new Map();

    function renderMessage(type, text) {
        messageArea.className = type;
        messageArea.textContent = text;
    }

    function renderErrorWithDetails(title, detail) {
        messageArea.className = 'error';
        messageArea.textContent = '';
        const strong = document.createElement('strong');
        strong.textContent = title;
        const pre = document.createElement('pre');
        pre.textContent = detail;
        messageArea.appendChild(strong);
        messageArea.appendChild(pre);
    }

    function clearElement(element) {
        while (element.firstChild) {
            element.removeChild(element.firstChild);
        }
    }

    function formatDateTime(value) {
        if (!value) {
            return 'Not available';
        }
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            return 'Not available';
        }
        return date.toLocaleString();
    }

    function formatNumber(value) {
        if (typeof value !== 'number' || !Number.isFinite(value)) {
            return 'n/a';
        }
        return value.toLocaleString();
    }

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
        return {
            model: unavailableModel,
            provider: (fallbackRow.dataset.unavailableProvider || providerSelect?.value || '').trim(),
        };
    }

    function collectUnavailableFallbackModels(containerElement) {
        return Array.from(containerElement.querySelectorAll('.fallback-row'))
            .map(getUnavailableFallbackModelDetails)
            .filter(Boolean);
    }

    function formatUnavailableFallbackModelsMessage(unavailableModels) {
        if (!Array.isArray(unavailableModels) || unavailableModels.length === 0) {
            return '';
        }

        const formattedModels = unavailableModels.map(({ provider, model }) => (
            provider
                ? `'${model}' for provider '${provider}'`
                : `'${model}'`
        ));
        return `Unavailable fallback models: ${formattedModels.join(', ')}.`;
    }

    function stableSerialize(value) {
        return JSON.stringify(value, null, 2);
    }

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
        embeddingRules = normalized.embeddings;
        rerankRules = normalized.rerank;
        imageGenerationRules = normalized.images_generations;
        imageEditRules = normalized.images_edits;
        audioSpeechRules = normalized.audio_speech || [];
        audioTranscriptionRules = normalized.audio_transcriptions || [];
        pdfConversionRules = normalized.pdf_conversions || [];
        webSearchRules = normalized.web_search || [];
        webReadRules = normalized.web_read || [];
        webResearchRules = normalized.web_research || [];
        webDeepResearchRules = normalized.web_deep_research || [];
        return normalized;
    }

    function buildOperationRoutesPayload(overrides = {}, basePayload = null) {
        const source = basePayload ? normalizeOperationRulesPayload(basePayload) : {
            embeddings: embeddingRules,
            rerank: rerankRules,
            images_generations: imageGenerationRules,
            images_edits: imageEditRules,
        };
        if (!basePayload && audioTranscriptionRules.length > 0) {
            source.audio_transcriptions = audioTranscriptionRules;
        }
        if (!basePayload && audioSpeechRules.length > 0) {
            source.audio_speech = audioSpeechRules;
        }
        if (!basePayload && pdfConversionRules.length > 0) {
            source.pdf_conversions = pdfConversionRules;
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
            || audioTranscriptionRules.length > 0
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

    function setDarkMode(isDark) {
        bodyElement.classList.toggle('dark-mode', isDark);
        localStorage.setItem('darkMode', isDark ? 'enabled' : 'disabled');
        updateDarkModeIcon();
    }

    function updateDarkModeIcon() {
        if (bodyElement.classList.contains('dark-mode')) {
            darkModeToggle.setAttribute('aria-label', 'Switch to light mode');
        } else {
            darkModeToggle.setAttribute('aria-label', 'Switch to dark mode');
        }
    }

    darkModeToggle.addEventListener('click', () => {
        setDarkMode(!bodyElement.classList.contains('dark-mode'));
    });

    const savedDarkMode = localStorage.getItem('darkMode');
    if (savedDarkMode === 'enabled') {
        setDarkMode(true);
    } else if (savedDarkMode === 'disabled') {
        setDarkMode(false);
    } else {
        setDarkMode(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    }
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
        if (localStorage.getItem('darkMode') === null) {
            setDarkMode(e.matches);
        }
    });

    function updateControlsVisibility() {
        saveButton.hidden = activeEditor === 'openrouter-free';
        if (activeEditor === 'rules') {
            saveButton.textContent = 'Save Fallback Rules';
        } else if (activeEditor === 'embeddings') {
            saveButton.textContent = 'Save Embeddings Routes';
        } else if (activeEditor === 'rerank') {
            saveButton.textContent = 'Save Rerank Routes';
        } else if (activeEditor === 'images') {
            saveButton.textContent = 'Save Images Routes';
        } else if (activeEditor === 'audio') {
            saveButton.textContent = 'Save Audio Routes';
        } else if (activeEditor === 'web') {
            saveButton.textContent = 'Save Web Services';
        } else if (activeEditor === 'openrouter-free') {
            saveButton.textContent = '';
        } else {
            saveButton.textContent = 'Save Configuration';
        }
    }

    function refreshRulesEmptyState() {
        rulesEmptyState.hidden = rulesList.children.length !== 0;
    }

    function refreshEmbeddingsEmptyState() {
        embeddingsEmptyState.hidden = embeddingsList.children.length !== 0;
    }

    function refreshRerankEmptyState() {
        rerankEmptyState.hidden = rerankList.children.length !== 0;
    }

    function refreshImageGenerationEmptyState() {
        imageGenerationEmptyState.hidden = imageGenerationList.children.length !== 0;
    }

    function refreshImageEditEmptyState() {
        imageEditEmptyState.hidden = imageEditList.children.length !== 0;
    }

    function refreshAudioSpeechEmptyState() {
        audioSpeechEmptyState.hidden = audioSpeechList.children.length !== 0;
    }

    function refreshAudioTranscriptionsEmptyState() {
        audioTranscriptionsEmptyState.hidden = audioTranscriptionsList.children.length !== 0;
    }

    function refreshWebSearchEmptyState() {
        webSearchEmptyState.hidden = webSearchList.children.length !== 0;
    }

    function refreshWebReadEmptyState() {
        webReadEmptyState.hidden = webReadList.children.length !== 0;
    }

    function refreshWebResearchEmptyState() {
        webResearchEmptyState.hidden = webResearchList.children.length !== 0;
    }

    function refreshWebDeepResearchEmptyState() {
        webDeepResearchEmptyState.hidden = webDeepResearchList.children.length !== 0;
    }

    function refreshProvidersEmptyState() {
        providersEmptyState.hidden = providersList.children.length !== 0;
    }

    function createFieldGroup(labelText, inputElement, className) {
        const group = document.createElement('label');
        group.className = `field-group ${className || ''}`.trim();
        const label = document.createElement('span');
        label.className = 'field-label';
        label.textContent = labelText;
        group.appendChild(label);
        group.appendChild(inputElement);
        return group;
    }

    function createTextInput(className, placeholder) {
        const input = document.createElement('input');
        input.type = 'text';
        input.className = className;
        input.placeholder = placeholder;
        return input;
    }

    function createNumberInput(className, placeholder) {
        const input = document.createElement('input');
        input.type = 'number';
        input.min = '0';
        input.step = '1';
        input.className = className;
        input.placeholder = placeholder;
        return input;
    }

    function createTextarea(className, placeholder) {
        const textarea = document.createElement('textarea');
        textarea.className = className;
        textarea.placeholder = placeholder;
        return textarea;
    }

    function createSelect(className) {
        const select = document.createElement('select');
        select.className = className;
        return select;
    }

    function setSelectOptions(select, options, placeholder, selectedValue) {
        select.textContent = '';
        const placeholderOption = document.createElement('option');
        placeholderOption.value = '';
        placeholderOption.textContent = placeholder;
        select.appendChild(placeholderOption);

        options.forEach(optionValue => {
            const option = document.createElement('option');
            option.value = optionValue;
            option.textContent = optionValue;
            select.appendChild(option);
        });

        select.value = selectedValue && options.includes(selectedValue) ? selectedValue : '';
    }

    function setModelSelectOptions(select, options, selectedValue, placeholder) {
        const currentValue = typeof selectedValue === 'string' ? selectedValue : '';
        select.textContent = '';
        const placeholderOption = document.createElement('option');
        placeholderOption.value = '';
        placeholderOption.textContent = placeholder || 'Select a gateway model';
        select.appendChild(placeholderOption);

        options.forEach(optionValue => {
            const option = document.createElement('option');
            option.value = optionValue;
            option.textContent = optionValue;
            select.appendChild(option);
        });

        if (currentValue && !options.includes(currentValue)) {
            const staleOption = document.createElement('option');
            staleOption.value = currentValue;
            staleOption.textContent = `${currentValue} (not configured)`;
            staleOption.dataset.stale = 'true';
            select.appendChild(staleOption);
        }

        select.value = currentValue;
    }

    function normalizeObjectTextarea(value) {
        if (!value || Object.keys(value).length === 0) {
            return '';
        }
        return JSON.stringify(value, null, 2);
    }

    function parseObjectTextarea(value, fieldLabel) {
        const trimmedValue = value.trim();
        if (!trimmedValue) {
            return {};
        }

        let parsedValue;
        try {
            parsedValue = JSON.parse(trimmedValue);
        } catch (error) {
            throw new Error(`${fieldLabel} must be a valid JSON object.`);
        }

        if (!parsedValue || Array.isArray(parsedValue) || typeof parsedValue !== 'object') {
            throw new Error(`${fieldLabel} must be a valid JSON object.`);
        }

        return parsedValue;
    }

    function parseProvidersOrder(value) {
        if (!value.trim()) {
            return undefined;
        }

        const providerNames = value
            .split(',')
            .map(item => item.trim())
            .filter(Boolean);

        if (providerNames.length === 0) {
            return undefined;
        }

        const unknownProviders = providerNames.filter(providerName => !availableProviders.includes(providerName));
        if (unknownProviders.length > 0) {
            throw new Error(`Provider order contains unknown providers: ${unknownProviders.join(', ')}.`);
        }

        return providerNames;
    }

    function createRetrySettingsInputs(initialData = {}) {
        const retryDelayInput = createNumberInput('retry-delay-input', 'Retry delay (seconds)');
        retryDelayInput.value = initialData.retry_delay ?? '';

        const retryCountInput = createNumberInput('retry-count-input', 'Retry count');
        retryCountInput.value = initialData.retry_count ?? '';

        return { retryDelayInput, retryCountInput };
    }

    function applyRetrySettingsToPayload(payload, retryDelayInput, retryCountInput) {
        if (retryDelayInput.value !== '') {
            payload.retry_delay = Number.parseFloat(retryDelayInput.value);
        }

        if (retryCountInput.value !== '') {
            payload.retry_count = Number.parseInt(retryCountInput.value, 10);
        }
    }

    function normalizeFallbackModelForSave(fallbackRow) {
        const providerSelect = fallbackRow.querySelector('.provider-select');
        const modelSelect = fallbackRow.querySelector('.model-select');
        const modelStatus = fallbackRow.querySelector('.model-status');
        const useProviderOrderCheckbox = fallbackRow.querySelector('.use-provider-order-checkbox');
        const providersOrderInput = fallbackRow.querySelector('.providers-order-input');
        const retryDelayInput = fallbackRow.querySelector('.retry-delay-input');
        const retryCountInput = fallbackRow.querySelector('.retry-count-input');
        const customBodyParamsInput = fallbackRow.querySelector('.custom-body-params-input');
        const customHeadersInput = fallbackRow.querySelector('.custom-headers-input');

        const provider = providerSelect.value.trim();
        const model = modelSelect.value.trim();
        const unavailableFallbackModel = getUnavailableFallbackModelDetails(fallbackRow);

        if (!provider) {
            throw new Error('Each fallback model row must have a provider selected.');
        }
        if (fallbackRow.dataset.modelsLoadError === 'true') {
            throw new Error(
                modelStatus.textContent
                || formatUnavailableFallbackModelsMessage([unavailableFallbackModel])
                || `Could not load models for provider '${provider}'.`
            );
        }
        if (!model) {
            const unavailableModelsMessage = unavailableFallbackModel
                ? ` ${formatUnavailableFallbackModelsMessage([unavailableFallbackModel])}`
                : '';
            throw new Error(`Choose an available model for provider '${provider}' before saving.${unavailableModelsMessage}`);
        }

        const fallbackModel = {
            provider,
            model,
            use_provider_order_as_fallback: useProviderOrderCheckbox.checked,
            custom_body_params: parseObjectTextarea(customBodyParamsInput.value, 'Custom body params'),
            custom_headers: parseObjectTextarea(customHeadersInput.value, 'Custom headers'),
        };

        const providersOrder = parseProvidersOrder(providersOrderInput.value);
        if (providersOrder) {
            fallbackModel.providers_order = providersOrder;
        }

        applyRetrySettingsToPayload(fallbackModel, retryDelayInput, retryCountInput);

        return fallbackModel;
    }

    function normalizeRuleCardForSave(ruleCard) {
        const gatewayModelInput = ruleCard.querySelector('.gateway-model-input');
        const rotateModelsCheckbox = ruleCard.querySelector('.rotate-models-checkbox');
        const stripThinkTagsCheckbox = ruleCard.querySelector('.strip-think-tags-checkbox');
        const maxTotalAttemptsInput = ruleCard.querySelector('.max-total-attempts-input');
        const contextOverflowEnabledCheckbox = ruleCard.querySelector('.context-overflow-enabled-checkbox');
        const contextOverflowRuleSlot = ruleCard.querySelector('.context-overflow-rule-slot');
        const fallbackRows = Array.from(ruleCard.querySelectorAll('.fallback-list > .fallback-row'));

        const gatewayModelName = gatewayModelInput.value.trim();
        if (!gatewayModelName) {
            throw new Error('Each gateway model rule must have a gateway model name.');
        }
        if (fallbackRows.length === 0) {
            throw new Error(`Gateway model '${gatewayModelName}' must contain at least one fallback model.`);
        }

        const normalizedRule = {
            gateway_model_name: gatewayModelName,
            rotate_models: rotateModelsCheckbox.checked,
            strip_think_tags: Boolean(stripThinkTagsCheckbox?.checked),
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
        const rules = Array.from(rulesList.querySelectorAll('.rule-card')).map(normalizeRuleCardForSave);
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
        const retryDelayInput = fallbackRow.querySelector('.retry-delay-input');
        const retryCountInput = fallbackRow.querySelector('.retry-count-input');
        const customBodyParamsInput = fallbackRow.querySelector('.custom-body-params-input');
        const customHeadersInput = fallbackRow.querySelector('.custom-headers-input');
        const unavailableFallbackModel = getUnavailableFallbackModelDetails(fallbackRow);

        const fallbackModel = {
            provider: providerSelect.value.trim(),
            model: modelSelect.value.trim() || unavailableFallbackModel?.model || '',
            use_provider_order_as_fallback: useProviderOrderCheckbox.checked,
            custom_body_params: parseObjectTextarea(customBodyParamsInput.value, 'Custom body params'),
            custom_headers: parseObjectTextarea(customHeadersInput.value, 'Custom headers'),
        };

        const providersOrder = parseProvidersOrder(providersOrderInput.value);
        if (providersOrder) {
            fallbackModel.providers_order = providersOrder;
        }

        applyRetrySettingsToPayload(fallbackModel, retryDelayInput, retryCountInput);
        return fallbackModel;
    }

    function getRulesSnapshotPayload() {
        const rules = Array.from(rulesList.querySelectorAll('.rule-card')).map(ruleCard => {
            const gatewayModelInput = ruleCard.querySelector('.gateway-model-input');
            const rotateModelsCheckbox = ruleCard.querySelector('.rotate-models-checkbox');
            const stripThinkTagsCheckbox = ruleCard.querySelector('.strip-think-tags-checkbox');
            const maxTotalAttemptsInput = ruleCard.querySelector('.max-total-attempts-input');
            const contextOverflowEnabledCheckbox = ruleCard.querySelector('.context-overflow-enabled-checkbox');
            const contextOverflowRuleSlot = ruleCard.querySelector('.context-overflow-rule-slot');
            const fallbackRows = Array.from(ruleCard.querySelectorAll('.fallback-list > .fallback-row'));

            const normalizedRule = {
                gateway_model_name: gatewayModelInput.value.trim(),
                rotate_models: rotateModelsCheckbox.checked,
                strip_think_tags: Boolean(stripThinkTagsCheckbox?.checked),
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

    function setupRowReordering(row) {
        row.draggable = true;

        row.addEventListener('dragstart', (e) => {
            if (['input', 'textarea', 'select', 'button'].includes(e.target.tagName.toLowerCase())) {
                e.preventDefault();
                return;
            }
            row.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
            setTimeout(() => {
                row.style.opacity = '0.5';
            }, 0);
            window._draggedRow = row;
        });

        row.addEventListener('dragend', () => {
            row.classList.remove('dragging');
            row.style.opacity = '';
            window._draggedRow = null;
        });

        row.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            if (window._draggedRow && window._draggedRow !== row && row.classList.contains('fallback-row')) {
                if (row.parentNode !== window._draggedRow.parentNode) return;
                const bounding = row.getBoundingClientRect();
                const offset = bounding.y + (bounding.height / 2);
                if (e.clientY > offset) {
                    row.parentNode.insertBefore(window._draggedRow, row.nextSibling);
                } else {
                    row.parentNode.insertBefore(window._draggedRow, row);
                }
            }
        });
    }

    function createMoveButtons(row) {
        const moveUpButton = document.createElement('button');
        moveUpButton.type = 'button';
        moveUpButton.className = 'icon-button move-up-button';
        moveUpButton.textContent = '↑';
        moveUpButton.title = 'Move Up';
        moveUpButton.addEventListener('click', () => {
            if (row.previousElementSibling) {
                row.parentNode.insertBefore(row, row.previousElementSibling);
            }
        });

        const moveDownButton = document.createElement('button');
        moveDownButton.type = 'button';
        moveDownButton.className = 'icon-button move-down-button';
        moveDownButton.textContent = '↓';
        moveDownButton.title = 'Move Down';
        moveDownButton.addEventListener('click', () => {
            if (row.nextElementSibling) {
                row.parentNode.insertBefore(row.nextElementSibling, row);
            }
        });

        return { moveUpButton, moveDownButton };
    }

    async function getProviderModels(providerName) {
        if (!providerName) {
            return [];
        }

        const cachedEntry = providerModelsCache.get(providerName);
        if (cachedEntry && Date.now() - cachedEntry.fetchedAt < MODELS_CACHE_TTL_MS) {
            return cachedEntry.models;
        }

        const existingRequest = providerModelsRequests.get(providerName);
        if (existingRequest) {
            return existingRequest;
        }

        const requestPromise = apiFetch(`/v1/config/providers/${encodeURIComponent(providerName)}/models`)
            .then(async response => {
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(payload.detail || `HTTP ${response.status}`);
                }

                const models = Array.isArray(payload.models)
                    ? payload.models.map(item => item.id).filter(modelId => typeof modelId === 'string')
                    : [];

                providerModelsCache.set(providerName, {
                    models,
                    fetchedAt: Date.now(),
                });
                return models;
            })
            .finally(() => {
                providerModelsRequests.delete(providerName);
            });

        providerModelsRequests.set(providerName, requestPromise);
        return requestPromise;
    }

    async function refreshFallbackRowModels(fallbackRow, providerName, selectedModel) {
        const modelSelect = fallbackRow.querySelector('.model-select');
        fallbackRow.dataset.modelsLoadError = 'false';
        clearUnavailableFallbackModelMetadata(fallbackRow);
        modelSelect.disabled = true;
        setSelectOptions(modelSelect, [], 'Loading models...', '');
        setFallbackRowStatus(fallbackRow, providerName ? 'Loading models…' : 'Choose a provider first.', providerName ? 'loading' : 'idle');

        if (!providerName) {
            setSelectOptions(modelSelect, [], 'Choose a provider first', '');
            return;
        }

        try {
            const models = await getProviderModels(providerName);
            modelSelect.disabled = false;
            setSelectOptions(modelSelect, models, models.length > 0 ? 'Choose a model' : 'No models available', selectedModel || '');

            if (selectedModel && !models.includes(selectedModel)) {
                fallbackRow.dataset.modelsLoadError = 'true';
                fallbackRow.dataset.unavailableModel = selectedModel;
                fallbackRow.dataset.unavailableProvider = providerName;
                setSelectOptions(modelSelect, models, 'Choose a model', '');
                setFallbackRowStatus(
                    fallbackRow,
                    `Model '${selectedModel}' is not available for provider '${providerName}'. Choose another one.`,
                    'error'
                );
                return;
            }

            setFallbackRowStatus(
                fallbackRow,
                models.length > 0 ? `${models.length} models loaded for ${providerName}.` : `Provider '${providerName}' returned no models.`,
                models.length > 0 ? 'success' : 'warning'
            );
        } catch (error) {
            fallbackRow.dataset.modelsLoadError = 'true';
            setSelectOptions(modelSelect, [], 'Could not load models', '');
            setFallbackRowStatus(
                fallbackRow,
                `Could not load models for provider '${providerName}': ${error.message}`,
                'error'
            );
        }
    }

    function buildFallbackRow(initialData, options = {}) {
        const fallbackRow = document.createElement('div');
        fallbackRow.className = 'fallback-row';
        fallbackRow.dataset.modelsLoadError = 'false';

        setupRowReordering(fallbackRow);

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid';

        const providerSelect = createSelect('provider-select');
        setSelectOptions(providerSelect, availableProviders, 'Choose a provider', initialData.provider || '');

        const modelSelect = createSelect('model-select');
        modelSelect.disabled = true;
        setSelectOptions(modelSelect, [], 'Choose a provider first', '');

        const useProviderOrderCheckbox = document.createElement('input');
        useProviderOrderCheckbox.type = 'checkbox';
        useProviderOrderCheckbox.className = 'use-provider-order-checkbox';
        useProviderOrderCheckbox.checked = Boolean(initialData.use_provider_order_as_fallback);

        const rotateToggle = document.createElement('label');
        rotateToggle.className = 'toggle-field';
        rotateToggle.appendChild(useProviderOrderCheckbox);
        const toggleText = document.createElement('span');
        toggleText.textContent = 'Use provider order as fallback';
        rotateToggle.appendChild(toggleText);

        const providersOrderInput = createTextInput('providers-order-input', 'provider-a, provider-b');
        providersOrderInput.value = Array.isArray(initialData.providers_order) ? initialData.providers_order.join(', ') : '';

        const retryDelayInput = createNumberInput('retry-delay-input', 'Retry delay (seconds)');
        retryDelayInput.value = initialData.retry_delay ?? '';

        const retryCountInput = createNumberInput('retry-count-input', 'Retry count');
        retryCountInput.value = initialData.retry_count ?? '';

        const customBodyParamsInput = createTextarea('custom-body-params-input', '{"temperature": 0.2}');
        customBodyParamsInput.value = normalizeObjectTextarea(initialData.custom_body_params);

        const customHeadersInput = createTextarea('custom-headers-input', '{"X-Provider": "value"}');
        customHeadersInput.value = normalizeObjectTextarea(initialData.custom_headers);

        fieldsGrid.appendChild(createFieldGroup('Provider', providerSelect, 'provider-field'));
        fieldsGrid.appendChild(createFieldGroup('Model', modelSelect, 'model-field'));
        fieldsGrid.appendChild(createFieldGroup('Provider Order', providersOrderInput));
        fieldsGrid.appendChild(createFieldGroup('Retry Delay', retryDelayInput));
        fieldsGrid.appendChild(createFieldGroup('Retry Count', retryCountInput));

        const modelStatus = document.createElement('div');
        modelStatus.className = 'model-status';
        modelStatus.dataset.state = 'idle';

        const advancedDetails = document.createElement('details');
        advancedDetails.className = 'advanced-options';
        const advancedSummary = document.createElement('summary');
        advancedSummary.textContent = 'Advanced options';
        advancedDetails.appendChild(advancedSummary);

        const advancedGrid = document.createElement('div');
        advancedGrid.className = 'advanced-grid';
        advancedGrid.appendChild(createFieldGroup('', rotateToggle, 'toggle-group'));
        advancedGrid.appendChild(createFieldGroup('Custom Body Params', customBodyParamsInput, 'textarea-group'));
        advancedGrid.appendChild(createFieldGroup('Custom Headers', customHeadersInput, 'textarea-group'));
        advancedDetails.appendChild(advancedGrid);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        removeButton.textContent = options.removeButtonLabel || 'Remove Fallback';
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
        
        const { moveUpButton, moveDownButton } = createMoveButtons(fallbackRow);
        rowActions.appendChild(moveUpButton);
        rowActions.appendChild(moveDownButton);
        rowActions.appendChild(removeButton);

        fallbackRow.appendChild(fieldsGrid);
        fallbackRow.appendChild(modelStatus);
        fallbackRow.appendChild(advancedDetails);
        fallbackRow.appendChild(rowActions);

        providerSelect.addEventListener('change', async () => {
            await refreshFallbackRowModels(fallbackRow, providerSelect.value, '');
        });

        fallbackRow._modelLoadPromise = refreshFallbackRowModels(
            fallbackRow,
            initialData.provider || '',
            initialData.model || ''
        );
        return fallbackRow;
    }

    function buildRuleCard(initialData) {
        const ruleCard = document.createElement('section');
        ruleCard.className = 'rule-card';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';

        const gatewayModelInput = createTextInput('gateway-model-input', 'llmgateway/model-name');
        gatewayModelInput.value = initialData.gateway_model_name || '';
        titleWrap.appendChild(createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const rotateModelsCheckbox = document.createElement('input');
        rotateModelsCheckbox.type = 'checkbox';
        rotateModelsCheckbox.className = 'rotate-models-checkbox';
        rotateModelsCheckbox.checked = Boolean(initialData.rotate_models);

        const rotateToggle = document.createElement('label');
        rotateToggle.className = 'toggle-field rotate-toggle';
        rotateToggle.appendChild(rotateModelsCheckbox);
        const rotateLabel = document.createElement('span');
        rotateLabel.textContent = 'Rotate fallback models';
        rotateToggle.appendChild(rotateLabel);
        titleWrap.appendChild(rotateToggle);

        const stripThinkTagsCheckbox = document.createElement('input');
        stripThinkTagsCheckbox.type = 'checkbox';
        stripThinkTagsCheckbox.className = 'strip-think-tags-checkbox';
        stripThinkTagsCheckbox.checked = Boolean(initialData.strip_think_tags);

        const stripThinkTagsToggle = document.createElement('label');
        stripThinkTagsToggle.className = 'toggle-field';
        stripThinkTagsToggle.appendChild(stripThinkTagsCheckbox);
        const stripThinkTagsLabel = document.createElement('span');
        stripThinkTagsLabel.textContent = 'Strip <think> tags from replies';
        stripThinkTagsToggle.appendChild(stripThinkTagsLabel);
        titleWrap.appendChild(stripThinkTagsToggle);

        const maxTotalAttemptsInput = document.createElement('input');
        maxTotalAttemptsInput.type = 'number';
        maxTotalAttemptsInput.className = 'max-total-attempts-input';
        maxTotalAttemptsInput.min = '0';
        maxTotalAttemptsInput.step = '1';
        maxTotalAttemptsInput.placeholder = 'unlimited';
        if (Number.isFinite(initialData.max_total_attempts)) {
            maxTotalAttemptsInput.value = String(initialData.max_total_attempts);
        }
        titleWrap.appendChild(
            createFieldGroup(
                'Max Total Attempts (chain budget)',
                maxTotalAttemptsInput,
                'max-total-attempts-field',
            ),
        );

        const removeRuleButton = document.createElement('button');
        removeRuleButton.type = 'button';
        removeRuleButton.className = 'icon-button danger-button';
        removeRuleButton.textContent = 'Remove Rule';
        removeRuleButton.addEventListener('click', () => {
            ruleCard.remove();
            refreshRulesEmptyState();
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
        contextOverflowTitle.textContent = 'Context Overflow Fallback';
        const contextOverflowDescription = document.createElement('span');
        contextOverflowDescription.textContent = 'Used only when the provider reports that the current model ran out of context window.';
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
        contextOverflowToggleLabel.textContent = 'Enable dedicated fallback for context overflow errors';
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
        addFallbackButton.textContent = 'Add Fallback Model';
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
            ruleCard.classList.toggle('collapsed');
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
        const modelLoadPromises = [];
        fallbackModels.forEach(fallbackModel => {
            const fallbackRow = buildFallbackRow(fallbackModel);
            fallbackList.appendChild(fallbackRow);
            modelLoadPromises.push(fallbackRow._modelLoadPromise);
        });

        contextOverflowSection.appendChild(contextOverflowHeader);
        contextOverflowSection.appendChild(contextOverflowRuleSlot);

        if (initialData.context_overflow_fallback) {
            const overflowRow = ensureContextOverflowRow();
            modelLoadPromises.push(overflowRow._modelLoadPromise);
        }

        contextOverflowEnabledCheckbox.addEventListener('change', () => {
            if (contextOverflowEnabledCheckbox.checked) {
                const overflowRow = ensureContextOverflowRow();
                contextOverflowRuleSlot.hidden = false;
                return overflowRow._modelLoadPromise;
            }

            contextOverflowRuleSlot.hidden = true;
            return undefined;
        });

        if (fallbackModels.length === 0) {
            const fallbackRow = buildFallbackRow({});
            fallbackList.appendChild(fallbackRow);
            modelLoadPromises.push(fallbackRow._modelLoadPromise);
        }

        ruleCard._modelLoadPromises = modelLoadPromises;
        return ruleCard;
    }

    async function renderRules(rules) {
        rulesList.textContent = '';
        const modelLoadPromises = [];

        if (!Array.isArray(rules) || rules.length === 0) {
            refreshRulesEmptyState();
            return;
        }

        rules.forEach(rule => {
            const ruleCard = buildRuleCard(rule);
            rulesList.appendChild(ruleCard);
            modelLoadPromises.push(...ruleCard._modelLoadPromises);
        });
        refreshRulesEmptyState();
        await Promise.all(modelLoadPromises);
    }

    async function loadRulesEditor() {
        renderMessage('info', 'Loading Fallback Rules...');
        try {
            const response = await apiFetch('/v1/config/models-rules/structured');
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.detail || `HTTP ${response.status}`);
            }

            availableProviders = Array.isArray(payload.providers) ? payload.providers : [];
            await renderRules(payload.rules);
            originalRulesContent = getRulesSnapshotContent();
            const unavailableFallbackModels = collectUnavailableFallbackModels(rulesList);
            if (unavailableFallbackModels.length > 0) {
                renderMessage(
                    'warning',
                    `Fallback Rules loaded with warnings. ${formatUnavailableFallbackModelsMessage(unavailableFallbackModels)}`
                );
                return;
            }
            renderMessage('success', 'Fallback Rules loaded successfully.');
        } catch (error) {
            console.error('Error fetching Fallback Rules:', error);
            renderErrorWithDetails('Error loading Fallback Rules:', error.message);
            rulesList.textContent = '';
            refreshRulesEmptyState();
            originalRulesContent = stableSerialize({ rules: [] });
        }
    }

    async function ensureAvailableProvidersLoaded() {
        if (availableProviders.length !== 0) {
            return;
        }

        const rulesResp = await apiFetch('/v1/config/models-rules/structured');
        const rulesPayload = await rulesResp.json();
        if (!rulesResp.ok) {
            throw new Error(rulesPayload.detail || `HTTP ${rulesResp.status}`);
        }
        availableProviders = Array.isArray(rulesPayload.providers) ? rulesPayload.providers : [];
    }

    async function fetchOperationRulesPayload() {
        const response = await apiFetch('/v1/config/model-operations/structured');
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.detail || `HTTP ${response.status}`);
        }
        return normalizeOperationRulesPayload(payload);
    }

    async function loadOperationRulesPayload(configName) {
        renderMessage('info', `Loading ${configName}...`);
        const payload = await fetchOperationRulesPayload();

        await ensureAvailableProvidersLoaded();
        return applyOperationRulesPayload(payload);
    }

    function collectCurrentWebSectionModels(listElement) {
        return Array.from(listElement.querySelectorAll('.rule-card > .rule-card-header .gateway-model-input'))
            .map(input => input.value.trim())
            .filter(Boolean);
    }

    function refreshWebCrossDropdowns() {
        gatewayModelCatalog.web_search = collectCurrentWebSectionModels(webSearchList);
        gatewayModelCatalog.web_read = collectCurrentWebSectionModels(webReadList);

        const crossSelectors = [
            { selector: '.search-model-input', options: gatewayModelCatalog.web_search },
            { selector: '.read-model-input', options: gatewayModelCatalog.web_read },
        ];
        [webResearchList, webDeepResearchList].forEach(list => {
            crossSelectors.forEach(({ selector, options }) => {
                list.querySelectorAll(selector).forEach(select => {
                    if (select.tagName !== 'SELECT') return;
                    setModelSelectOptions(select, options, select.value);
                });
            });
        });

        const chatSelects = [
            ...webSearchList.querySelectorAll('.query-model-input'),
        ];
        chatSelects.forEach(select => {
            if (select.tagName !== 'SELECT') return;
            setModelSelectOptions(select, gatewayModelCatalog.chat, select.value);
        });
    }

    async function loadGatewayModelCatalog() {
        const response = await apiFetch('/v1/config/models-rules/structured');
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.detail || `HTTP ${response.status}`);
        }
        const rules = Array.isArray(payload.rules) ? payload.rules : [];
        gatewayModelCatalog.chat = rules
            .map(rule => typeof rule.gateway_model_name === 'string' ? rule.gateway_model_name.trim() : '')
            .filter(Boolean);
    }

    function applyOperationCatalog(normalizedPayload) {
        gatewayModelCatalog.embeddings = (normalizedPayload.embeddings || [])
            .map(item => typeof item.gateway_model_name === 'string' ? item.gateway_model_name.trim() : '')
            .filter(Boolean);
        gatewayModelCatalog.rerank = (normalizedPayload.rerank || [])
            .map(item => typeof item.gateway_model_name === 'string' ? item.gateway_model_name.trim() : '')
            .filter(Boolean);
        gatewayModelCatalog.images_generations = (normalizedPayload.images_generations || [])
            .map(item => typeof item.gateway_model_name === 'string' ? item.gateway_model_name.trim() : '')
            .filter(Boolean);
        gatewayModelCatalog.web_search = (normalizedPayload.web_search || [])
            .map(item => typeof item.gateway_model_name === 'string' ? item.gateway_model_name.trim() : '')
            .filter(Boolean);
        gatewayModelCatalog.web_read = (normalizedPayload.web_read || [])
            .map(item => typeof item.gateway_model_name === 'string' ? item.gateway_model_name.trim() : '')
            .filter(Boolean);
    }

    function getEmbeddingsPayloadForSave(basePayload = null) {
        const embeddings = Array.from(embeddingsList.querySelectorAll('.rule-card')).map(normalizeEmbeddingCardForSave);
        return buildOperationRoutesPayload({ embeddings }, basePayload);
    }

    function getNormalizedEmbeddingsContent() {
        return stableSerialize(getEmbeddingsPayloadForSave());
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
            custom_body_params: parseObjectTextarea(customBodyParamsInput.value, 'Custom body params'),
            custom_headers: parseObjectTextarea(customHeadersInput.value, 'Custom headers'),
        };

        applyRetrySettingsToPayload(routePayload, retryDelayInput, retryCountInput);

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
            const payload = await loadOperationRulesPayload('Embeddings Routes');
            await renderEmbeddings(payload.embeddings);
            originalEmbeddingsContent = getNormalizedEmbeddingsContent();
            renderMessage('success', 'Embeddings Routes loaded successfully.');
        } catch (error) {
            console.error('Error fetching Embeddings Routes:', error);
            renderErrorWithDetails('Error loading Embeddings Routes:', error.message);
            embeddingsList.textContent = '';
            refreshEmbeddingsEmptyState();
            originalEmbeddingsContent = stableSerialize(buildOperationRoutesPayload({ embeddings: [] }));
        }
    }

    async function renderEmbeddings(embeddings) {
        embeddingsList.textContent = '';
        const modelLoadPromises = [];

        if (!Array.isArray(embeddings) || embeddings.length === 0) {
            refreshEmbeddingsEmptyState();
            return;
        }

        embeddings.forEach(embedding => {
            const embeddingCard = buildEmbeddingCard(embedding);
            embeddingsList.appendChild(embeddingCard);
            modelLoadPromises.push(...embeddingCard._modelLoadPromises);
        });
        refreshEmbeddingsEmptyState();
        await Promise.all(modelLoadPromises);
    }

    function buildEmbeddingCard(initialData) {
        const card = document.createElement('section');
        card.className = 'rule-card';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';

        const gatewayModelInput = createTextInput('gateway-model-input', 'llmgateway/embedding-model');
        gatewayModelInput.value = initialData.gateway_model_name || '';
        titleWrap.appendChild(createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        removeButton.textContent = 'Remove Model';
        removeButton.addEventListener('click', () => {
            card.remove();
            refreshEmbeddingsEmptyState();
        });

        cardHeader.appendChild(titleWrap);
        cardHeader.appendChild(removeButton);

        const routeList = document.createElement('div');
        routeList.className = 'fallback-list';

        const addRouteButton = document.createElement('button');
        addRouteButton.type = 'button';
        addRouteButton.className = 'secondary-button add-fallback-button';
        addRouteButton.textContent = 'Add Fallback Route';
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
            card.classList.toggle('collapsed');
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
        const modelLoadPromises = [];
        routes.forEach(route => {
            const routeRow = buildEmbeddingRouteRow(route);
            routeList.appendChild(routeRow);
            modelLoadPromises.push(routeRow._modelLoadPromise);
        });

        if (routes.length === 0) {
            const routeRow = buildEmbeddingRouteRow({});
            routeList.appendChild(routeRow);
            modelLoadPromises.push(routeRow._modelLoadPromise);
        }

        card._modelLoadPromises = modelLoadPromises;
        return card;
    }

    function buildEmbeddingRouteRow(initialData) {
        const row = document.createElement('div');
        row.className = 'fallback-row';

        setupRowReordering(row);

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid';

        const providerSelect = createSelect('provider-select');
        setSelectOptions(providerSelect, availableProviders, 'Choose a provider', initialData.provider || '');

        // Use datalist for model input to allow both selection and manual input
        const modelInput = createTextInput('model-input', 'Choose or enter model');
        modelInput.value = initialData.model || '';
        const dataListId = `models-list-${Math.random().toString(36).substr(2, 9)}`;
        modelInput.setAttribute('list', dataListId);
        const dataList = document.createElement('datalist');
        dataList.id = dataListId;
        row.appendChild(dataList);

        const targetPathInput = createTextInput('target-path-input', '/embeddings');
        targetPathInput.value = initialData.target_path || '/embeddings';
        targetPathInput.readOnly = true;
        const { retryDelayInput, retryCountInput } = createRetrySettingsInputs(initialData);

        const customBodyParamsInput = createTextarea('custom-body-params-input', '{"param": "value"}');
        customBodyParamsInput.value = normalizeObjectTextarea(initialData.custom_body_params);

        const customHeadersInput = createTextarea('custom-headers-input', '{"X-Header": "value"}');
        customHeadersInput.value = normalizeObjectTextarea(initialData.custom_headers);

        fieldsGrid.appendChild(createFieldGroup('Provider', providerSelect, 'provider-field'));
        fieldsGrid.appendChild(createFieldGroup('Model', modelInput, 'model-field'));
        fieldsGrid.appendChild(createFieldGroup('Target Path', targetPathInput));

        const modelStatus = document.createElement('div');
        modelStatus.className = 'model-status';
        modelStatus.dataset.state = 'idle';

        const advancedDetails = document.createElement('details');
        advancedDetails.className = 'advanced-options';
        const advancedSummary = document.createElement('summary');
        advancedSummary.textContent = 'Advanced options';
        advancedDetails.appendChild(advancedSummary);

        const advancedGrid = document.createElement('div');
        advancedGrid.className = 'advanced-grid';
        advancedGrid.appendChild(createFieldGroup('Retry Delay', retryDelayInput));
        advancedGrid.appendChild(createFieldGroup('Retry Count', retryCountInput));
        advancedGrid.appendChild(createFieldGroup('Custom Body Params', customBodyParamsInput, 'textarea-group'));
        advancedGrid.appendChild(createFieldGroup('Custom Headers', customHeadersInput, 'textarea-group'));
        advancedDetails.appendChild(advancedGrid);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        removeButton.textContent = 'Remove Fallback Route';
        removeButton.addEventListener('click', () => {
            row.remove();
        });

        const rowActions = document.createElement('div');
        rowActions.className = 'fallback-row-actions';
        
        const { moveUpButton, moveDownButton } = createMoveButtons(row);
        rowActions.appendChild(moveUpButton);
        rowActions.appendChild(moveDownButton);
        rowActions.appendChild(removeButton);

        row.appendChild(fieldsGrid);
        row.appendChild(modelStatus);
        row.appendChild(advancedDetails);
        row.appendChild(rowActions);

        const refreshModels = async (providerName) => {
            if (!providerName) {
                dataList.textContent = '';
                return;
            }
            modelStatus.textContent = 'Loading models...';
            modelStatus.dataset.state = 'loading';
            try {
                const models = await getProviderModels(providerName);
                dataList.textContent = '';
                models.forEach(modelId => {
                    const option = document.createElement('option');
                    option.value = modelId;
                    dataList.appendChild(option);
                });
                modelStatus.textContent = `${models.length} models loaded for ${providerName}.`;
                modelStatus.dataset.state = 'success';
            } catch (error) {
                modelStatus.textContent = `Could not load models: ${error.message}`;
                modelStatus.dataset.state = 'error';
            }
        };

        providerSelect.addEventListener('change', () => {
            refreshModels(providerSelect.value);
        });

        row._modelLoadPromise = refreshModels(initialData.provider || '');

        return row;
    }

    async function loadRerankEditor() {
        try {
            const payload = await loadOperationRulesPayload('Rerank Routes');
            await renderRerank(payload.rerank);
            originalRerankContent = getNormalizedRerankContent();
            renderMessage('success', 'Rerank Routes loaded successfully.');
        } catch (error) {
            console.error('Error fetching Rerank Routes:', error);
            renderErrorWithDetails('Error loading Rerank Routes:', error.message);
            rerankList.textContent = '';
            refreshRerankEmptyState();
            originalRerankContent = stableSerialize(buildOperationRoutesPayload({ rerank: [] }));
        }
    }

    async function renderRerank(rerank) {
        rerankList.textContent = '';
        const modelLoadPromises = [];

        if (!Array.isArray(rerank) || rerank.length === 0) {
            refreshRerankEmptyState();
            return;
        }

        rerank.forEach(item => {
            const rerankCard = buildRerankCard(item);
            rerankList.appendChild(rerankCard);
            modelLoadPromises.push(...rerankCard._modelLoadPromises);
        });
        refreshRerankEmptyState();
        await Promise.all(modelLoadPromises);
    }

    function buildRerankCard(initialData) {
        const card = document.createElement('section');
        card.className = 'rule-card';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';

        const gatewayModelInput = createTextInput('gateway-model-input', 'llmgateway/rerank-model');
        gatewayModelInput.value = initialData.gateway_model_name || '';
        titleWrap.appendChild(createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        removeButton.textContent = 'Remove Model';
        removeButton.addEventListener('click', () => {
            card.remove();
            refreshRerankEmptyState();
        });

        cardHeader.appendChild(titleWrap);
        cardHeader.appendChild(removeButton);

        const routeList = document.createElement('div');
        routeList.className = 'fallback-list';

        const addRouteButton = document.createElement('button');
        addRouteButton.type = 'button';
        addRouteButton.className = 'secondary-button add-fallback-button';
        addRouteButton.textContent = 'Add Fallback Route';
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
            card.classList.toggle('collapsed');
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
        const modelLoadPromises = [];
        routes.forEach(route => {
            const routeRow = buildRerankRouteRow(route);
            routeList.appendChild(routeRow);
            modelLoadPromises.push(routeRow._modelLoadPromise);
        });

        if (routes.length === 0) {
            const routeRow = buildRerankRouteRow({});
            routeList.appendChild(routeRow);
            modelLoadPromises.push(routeRow._modelLoadPromise);
        }

        card._modelLoadPromises = modelLoadPromises;
        return card;
    }

    function buildRerankRouteRow(initialData) {
        const row = document.createElement('div');
        row.className = 'fallback-row';

        setupRowReordering(row);

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid';

        const providerSelect = createSelect('provider-select');
        setSelectOptions(providerSelect, availableProviders, 'Choose a provider', initialData.provider || '');

        const modelInput = createTextInput('model-input', 'Choose or enter model');
        modelInput.value = initialData.model || '';
        const dataListId = `rerank-models-list-${Math.random().toString(36).substr(2, 9)}`;
        modelInput.setAttribute('list', dataListId);
        const dataList = document.createElement('datalist');
        dataList.id = dataListId;
        row.appendChild(dataList);

        const targetPathInput = createTextInput('target-path-input', '/score');
        targetPathInput.value = initialData.target_path || '/score';
        const requestFormatSelect = createSelect('request-format-select');
        setSelectOptions(requestFormatSelect, ['query_passages', 'query_texts'], 'Default request format', initialData.request_format || '');
        const responseFormatSelect = createSelect('response-format-select');
        setSelectOptions(responseFormatSelect, ['rankings_logit', 'scores'], 'Default response format', initialData.response_format || '');
        const responseOutputFormatSelect = createSelect('response-output-format-select');
        setSelectOptions(
            responseOutputFormatSelect,
            ['jina_results'],
            'Default output format',
            initialData.response_output_format || ''
        );
        const { retryDelayInput, retryCountInput } = createRetrySettingsInputs(initialData);

        const customBodyParamsInput = createTextarea('custom-body-params-input', '{"param": "value"}');
        customBodyParamsInput.value = normalizeObjectTextarea(initialData.custom_body_params);

        const customHeadersInput = createTextarea('custom-headers-input', '{"X-Header": "value"}');
        customHeadersInput.value = normalizeObjectTextarea(initialData.custom_headers);

        fieldsGrid.appendChild(createFieldGroup('Provider', providerSelect, 'provider-field'));
        fieldsGrid.appendChild(createFieldGroup('Model', modelInput, 'model-field'));
        fieldsGrid.appendChild(createFieldGroup('Target Path', targetPathInput));

        const modelStatus = document.createElement('div');
        modelStatus.className = 'model-status';
        modelStatus.dataset.state = 'idle';

        const advancedDetails = document.createElement('details');
        advancedDetails.className = 'advanced-options';
        const advancedSummary = document.createElement('summary');
        advancedSummary.textContent = 'Advanced options';
        advancedDetails.appendChild(advancedSummary);

        const advancedGrid = document.createElement('div');
        advancedGrid.className = 'advanced-grid';
        advancedGrid.appendChild(createFieldGroup('Request Format', requestFormatSelect));
        advancedGrid.appendChild(createFieldGroup('Response Format', responseFormatSelect));
        advancedGrid.appendChild(createFieldGroup('Response Output Format', responseOutputFormatSelect));
        advancedGrid.appendChild(createFieldGroup('Retry Delay', retryDelayInput));
        advancedGrid.appendChild(createFieldGroup('Retry Count', retryCountInput));
        advancedGrid.appendChild(createFieldGroup('Custom Body Params', customBodyParamsInput, 'textarea-group'));
        advancedGrid.appendChild(createFieldGroup('Custom Headers', customHeadersInput, 'textarea-group'));
        advancedDetails.appendChild(advancedGrid);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        removeButton.textContent = 'Remove Fallback Route';
        removeButton.addEventListener('click', () => {
            row.remove();
        });

        const rowActions = document.createElement('div');
        rowActions.className = 'fallback-row-actions';
        
        const { moveUpButton, moveDownButton } = createMoveButtons(row);
        rowActions.appendChild(moveUpButton);
        rowActions.appendChild(moveDownButton);
        rowActions.appendChild(removeButton);

        row.appendChild(fieldsGrid);
        row.appendChild(modelStatus);
        row.appendChild(advancedDetails);
        row.appendChild(rowActions);

        const refreshModels = async (providerName) => {
            if (!providerName) {
                dataList.textContent = '';
                return;
            }
            modelStatus.textContent = 'Loading models...';
            modelStatus.dataset.state = 'loading';
            try {
                const models = await getProviderModels(providerName);
                dataList.textContent = '';
                models.forEach(modelId => {
                    const option = document.createElement('option');
                    option.value = modelId;
                    dataList.appendChild(option);
                });
                modelStatus.textContent = `${models.length} models loaded for ${providerName}.`;
                modelStatus.dataset.state = 'success';
            } catch (error) {
                modelStatus.textContent = `Could not load models: ${error.message}`;
                modelStatus.dataset.state = 'error';
            }
        };

        providerSelect.addEventListener('change', () => {
            refreshModels(providerSelect.value);
        });

        row._modelLoadPromise = refreshModels(initialData.provider || '');

        return row;
    }

    function getRerankPayloadForSave(basePayload = null) {
        const rerank = Array.from(rerankList.querySelectorAll('.rule-card')).map(normalizeRerankCardForSave);
        return buildOperationRoutesPayload({ rerank }, basePayload);
    }

    function getNormalizedRerankContent() {
        return stableSerialize(getRerankPayloadForSave());
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
            custom_body_params: parseObjectTextarea(customBodyParamsInput.value, 'Custom body params'),
            custom_headers: parseObjectTextarea(customHeadersInput.value, 'Custom headers'),
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
        applyRetrySettingsToPayload(routePayload, retryDelayInput, retryCountInput);

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
        let payload;
        saveButton.disabled = true;
        renderMessage('info', 'Saving Rerank Routes...');

        try {
            payload = getRerankPayloadForSave(await fetchOperationRulesPayload());
            const response = await apiFetch('/v1/config/model-operations/structured', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload),
            });

            const body = await response.json().catch(() => ({}));
            if (!response.ok) {
                if (body.detail && Array.isArray(body.detail.errors)) {
                    const errorDetails = body.detail.errors.map(err => {
                        const loc = err.loc ? err.loc.join(' -> ') : 'N/A';
                        return `- Location: ${loc}, Message: ${err.msg}, Type: ${err.type}`;
                    }).join('\n');
                    renderErrorWithDetails(
                        `Validation Error for Rerank (HTTP ${response.status}):`,
                        `${body.detail.message}\n${errorDetails}`
                    );
                } else {
                    renderErrorWithDetails(
                        `Error saving Rerank (HTTP ${response.status}):`,
                        body.detail || 'Unknown error'
                    );
                }
                return;
            }

            applyOperationRulesPayload(payload);
            originalRerankContent = stableSerialize(payload);
            renderMessage('success', body.message || 'Rerank Routes updated successfully.');
        } catch (error) {
            console.error('Error saving Rerank:', error);
            renderMessage('error', `Error saving Rerank: ${error.message}`);
        } finally {
            saveButton.disabled = false;
        }
    }

    async function saveEmbeddings() {
        let payload;
        saveButton.disabled = true;
        renderMessage('info', 'Saving Embeddings Routes...');

        try {
            payload = getEmbeddingsPayloadForSave(await fetchOperationRulesPayload());
            const response = await apiFetch('/v1/config/model-operations/structured', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload),
            });

            const body = await response.json().catch(() => ({}));
            if (!response.ok) {
                if (body.detail && Array.isArray(body.detail.errors)) {
                    const errorDetails = body.detail.errors.map(err => {
                        const loc = err.loc ? err.loc.join(' -> ') : 'N/A';
                        return `- Location: ${loc}, Message: ${err.msg}, Type: ${err.type}`;
                    }).join('\n');
                    renderErrorWithDetails(
                        `Validation Error for Embeddings (HTTP ${response.status}):`,
                        `${body.detail.message}\n${errorDetails}`
                    );
                } else {
                    renderErrorWithDetails(
                        `Error saving Embeddings (HTTP ${response.status}):`,
                        body.detail || 'Unknown error'
                    );
                }
                return;
            }

            applyOperationRulesPayload(payload);
            originalEmbeddingsContent = stableSerialize(payload);
            renderMessage('success', body.message || 'Embeddings Routes updated successfully.');
        } catch (error) {
            console.error('Error saving Embeddings:', error);
            renderMessage('error', `Error saving Embeddings: ${error.message}`);
        } finally {
            saveButton.disabled = false;
        }
    }

    async function renderImageSection(listElement, refreshEmptyState, items, buildCard) {
        listElement.textContent = '';
        const modelLoadPromises = [];

        if (!Array.isArray(items) || items.length === 0) {
            refreshEmptyState();
            return;
        }

        items.forEach(item => {
            const itemCard = buildCard(item);
            listElement.appendChild(itemCard);
            modelLoadPromises.push(...itemCard._modelLoadPromises);
        });
        refreshEmptyState();
        await Promise.all(modelLoadPromises);
    }

    function buildImageCard(initialData, options) {
        const card = document.createElement('section');
        card.className = 'rule-card';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';

        const gatewayModelInput = createTextInput('gateway-model-input', options.gatewayPlaceholder);
        gatewayModelInput.value = initialData.gateway_model_name || '';
        titleWrap.appendChild(createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        removeButton.textContent = 'Remove Model';
        removeButton.addEventListener('click', () => {
            card.remove();
            options.refreshEmptyState();
        });

        const routeList = document.createElement('div');
        routeList.className = 'fallback-list';

        const addRouteButton = document.createElement('button');
        addRouteButton.type = 'button';
        addRouteButton.className = 'secondary-button add-fallback-button';
        addRouteButton.textContent = 'Add Route';
        addRouteButton.addEventListener('click', () => {
            routeList.appendChild(buildImageRouteRow({}, options.defaultTargetPath));
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
            card.classList.toggle('collapsed');
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
        const modelLoadPromises = [];
        routes.forEach(route => {
            const routeRow = buildImageRouteRow(route, options.defaultTargetPath);
            routeList.appendChild(routeRow);
            modelLoadPromises.push(routeRow._modelLoadPromise);
        });

        if (routes.length === 0) {
            const routeRow = buildImageRouteRow({}, options.defaultTargetPath);
            routeList.appendChild(routeRow);
            modelLoadPromises.push(routeRow._modelLoadPromise);
        }

        card._modelLoadPromises = modelLoadPromises;
        return card;
    }

    function buildImageRouteRow(initialData, defaultTargetPath) {
        const row = document.createElement('div');
        row.className = 'fallback-row';

        setupRowReordering(row);

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid';

        const providerSelect = createSelect('provider-select');
        setSelectOptions(providerSelect, availableProviders, 'Choose a provider', initialData.provider || '');

        const modelInput = createTextInput('model-input', 'Choose or enter model');
        modelInput.value = initialData.model || '';
        const dataListId = `image-models-list-${Math.random().toString(36).substr(2, 9)}`;
        modelInput.setAttribute('list', dataListId);
        const dataList = document.createElement('datalist');
        dataList.id = dataListId;
        row.appendChild(dataList);

        const targetPathInput = createTextInput('target-path-input', defaultTargetPath);
        targetPathInput.value = initialData.target_path || defaultTargetPath;
        const requestFormatSelect = createSelect('request-format-select');
        setSelectOptions(
            requestFormatSelect,
            IMAGE_REQUEST_FORMAT_OPTIONS,
            'Default request format',
            initialData.request_format || ''
        );
        const responseFormatSelect = createSelect('response-format-select');
        setSelectOptions(
            responseFormatSelect,
            IMAGE_RESPONSE_FORMAT_OPTIONS,
            'Default response format',
            initialData.response_format || ''
        );
        const { retryDelayInput, retryCountInput } = createRetrySettingsInputs(initialData);

        const customBodyParamsInput = createTextarea('custom-body-params-input', '{"param": "value"}');
        customBodyParamsInput.value = normalizeObjectTextarea(initialData.custom_body_params);

        const customHeadersInput = createTextarea('custom-headers-input', '{"X-Header": "value"}');
        customHeadersInput.value = normalizeObjectTextarea(initialData.custom_headers);
        const requestMappingInput = createTextarea('request-mapping-input', '{"fields": {"prompt": "prompt"}}');
        requestMappingInput.value = normalizeObjectTextarea(initialData.request_mapping);
        const responseMappingInput = createTextarea('response-mapping-input', '{"artifacts_path": "artifacts"}');
        responseMappingInput.value = normalizeObjectTextarea(initialData.response_mapping);

        fieldsGrid.appendChild(createFieldGroup('Provider', providerSelect, 'provider-field'));
        fieldsGrid.appendChild(createFieldGroup('Model', modelInput, 'model-field'));
        fieldsGrid.appendChild(createFieldGroup('Target Path', targetPathInput));

        const modelStatus = document.createElement('div');
        modelStatus.className = 'model-status';
        modelStatus.dataset.state = 'idle';

        const advancedDetails = document.createElement('details');
        advancedDetails.className = 'advanced-options';
        const advancedSummary = document.createElement('summary');
        advancedSummary.textContent = 'Advanced options';
        advancedDetails.appendChild(advancedSummary);

        const advancedGrid = document.createElement('div');
        advancedGrid.className = 'advanced-grid';
        advancedGrid.appendChild(createFieldGroup('Request Format', requestFormatSelect));
        advancedGrid.appendChild(createFieldGroup('Response Format', responseFormatSelect));
        advancedGrid.appendChild(createFieldGroup('Retry Delay', retryDelayInput));
        advancedGrid.appendChild(createFieldGroup('Retry Count', retryCountInput));
        advancedGrid.appendChild(createFieldGroup('Request Mapping', requestMappingInput, 'textarea-group'));
        advancedGrid.appendChild(createFieldGroup('Response Mapping', responseMappingInput, 'textarea-group'));
        advancedGrid.appendChild(createFieldGroup('Custom Body Params', customBodyParamsInput, 'textarea-group'));
        advancedGrid.appendChild(createFieldGroup('Custom Headers', customHeadersInput, 'textarea-group'));
        advancedDetails.appendChild(advancedGrid);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        removeButton.textContent = 'Remove Route';
        removeButton.addEventListener('click', () => {
            row.remove();
        });

        const rowActions = document.createElement('div');
        rowActions.className = 'fallback-row-actions';

        const { moveUpButton, moveDownButton } = createMoveButtons(row);
        rowActions.appendChild(moveUpButton);
        rowActions.appendChild(moveDownButton);
        rowActions.appendChild(removeButton);

        row.appendChild(fieldsGrid);
        row.appendChild(modelStatus);
        row.appendChild(advancedDetails);
        row.appendChild(rowActions);

        const refreshModels = async (providerName) => {
            if (!providerName) {
                dataList.textContent = '';
                return;
            }
            modelStatus.textContent = 'Loading models...';
            modelStatus.dataset.state = 'loading';
            try {
                const models = await getProviderModels(providerName);
                dataList.textContent = '';
                models.forEach(modelId => {
                    const option = document.createElement('option');
                    option.value = modelId;
                    dataList.appendChild(option);
                });
                modelStatus.textContent = `${models.length} models loaded for ${providerName}.`;
                modelStatus.dataset.state = 'success';
            } catch (error) {
                modelStatus.textContent = `Could not load models: ${error.message}`;
                modelStatus.dataset.state = 'error';
            }
        };

        providerSelect.addEventListener('change', () => {
            refreshModels(providerSelect.value);
        });

        row._modelLoadPromise = refreshModels(initialData.provider || '');
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
            custom_body_params: parseObjectTextarea(customBodyParamsInput.value, 'Custom body params'),
            custom_headers: parseObjectTextarea(customHeadersInput.value, 'Custom headers'),
        };
        const request_mapping = parseObjectTextarea(requestMappingInput.value, 'Request mapping');
        const response_mapping = parseObjectTextarea(responseMappingInput.value, 'Response mapping');
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
        applyRetrySettingsToPayload(routePayload, retryDelayInput, retryCountInput);
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

        return {
            gateway_model_name: gatewayModelName,
            routes: routeRows.map(routeRow => normalizeImageRouteForSave(routeRow, defaultTargetPath, routeLabel)),
        };
    }

    function getImagesPayloadForSave(basePayload = null) {
        const images_generations = Array.from(imageGenerationList.querySelectorAll('.rule-card')).map(ruleCard => (
            normalizeImageCardForSave(ruleCard, 'image generation', '/images/generations')
        ));
        const images_edits = Array.from(imageEditList.querySelectorAll('.rule-card')).map(ruleCard => (
            normalizeImageCardForSave(ruleCard, 'image edit', '/images/edits')
        ));

        return buildOperationRoutesPayload({
            images_generations,
            images_edits,
        }, basePayload);
    }

    function getNormalizedImagesContent() {
        return stableSerialize(getImagesPayloadForSave());
    }

    async function loadImagesEditor() {
        try {
            const payload = await loadOperationRulesPayload('Images Routes');
            await renderImageSection(
                imageGenerationList,
                refreshImageGenerationEmptyState,
                payload.images_generations,
                (item) => buildImageCard(item, {
                    gatewayPlaceholder: 'llmgateway/image-generation-model',
                    defaultTargetPath: '/images/generations',
                    refreshEmptyState: refreshImageGenerationEmptyState,
                }),
            );
            await renderImageSection(
                imageEditList,
                refreshImageEditEmptyState,
                payload.images_edits,
                (item) => buildImageCard(item, {
                    gatewayPlaceholder: 'llmgateway/image-edit-model',
                    defaultTargetPath: '/images/edits',
                    refreshEmptyState: refreshImageEditEmptyState,
                }),
            );
            originalImagesContent = getNormalizedImagesContent();
            renderMessage('success', 'Images Routes loaded successfully.');
        } catch (error) {
            console.error('Error fetching Images Routes:', error);
            renderErrorWithDetails('Error loading Images Routes:', error.message);
            imageGenerationList.textContent = '';
            imageEditList.textContent = '';
            refreshImageGenerationEmptyState();
            refreshImageEditEmptyState();
            originalImagesContent = stableSerialize(buildOperationRoutesPayload({
                images_generations: [],
                images_edits: [],
            }));
        }
    }

    async function saveImages() {
        let payload;
        saveButton.disabled = true;
        renderMessage('info', 'Saving Images Routes...');

        try {
            payload = getImagesPayloadForSave(await fetchOperationRulesPayload());
            const response = await apiFetch('/v1/config/model-operations/structured', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload),
            });

            const body = await response.json().catch(() => ({}));
            if (!response.ok) {
                if (body.detail && Array.isArray(body.detail.errors)) {
                    const errorDetails = body.detail.errors.map(err => {
                        const loc = err.loc ? err.loc.join(' -> ') : 'N/A';
                        return `- Location: ${loc}, Message: ${err.msg}, Type: ${err.type}`;
                    }).join('\n');
                    renderErrorWithDetails(
                        `Validation Error for Images (HTTP ${response.status}):`,
                        `${body.detail.message}\n${errorDetails}`
                    );
                } else {
                    renderErrorWithDetails(
                        `Error saving Images (HTTP ${response.status}):`,
                        body.detail || 'Unknown error'
                    );
                }
                return;
            }

            applyOperationRulesPayload(payload);
            originalImagesContent = stableSerialize(payload);
            renderMessage('success', body.message || 'Images Routes updated successfully.');
        } catch (error) {
            console.error('Error saving Images:', error);
            renderMessage('error', `Error saving Images: ${error.message}`);
        } finally {
            saveButton.disabled = false;
        }
    }

    function getAudioPayloadForSave(basePayload = null) {
        const audio_speech = Array.from(audioSpeechList.querySelectorAll('.rule-card')).map(
            normalizeAudioSpeechCardForSave
        );
        const audio_transcriptions = Array.from(audioTranscriptionsList.querySelectorAll('.rule-card')).map(
            normalizeAudioTranscriptionCardForSave
        );
        return buildOperationRoutesPayload({ audio_speech, audio_transcriptions }, basePayload);
    }

    function getNormalizedAudioContent() {
        return stableSerialize(getAudioPayloadForSave());
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
            custom_body_params: parseObjectTextarea(customBodyParamsInput.value, 'Custom body params'),
            custom_headers: parseObjectTextarea(customHeadersInput.value, 'Custom headers'),
        };
        if (request_format) {
            routePayload.request_format = request_format;
        }
        if (voices_target_path) {
            validateAudioTargetPath(voices_target_path, 'Voices target path');
            routePayload.voices_target_path = voices_target_path;
        }
        applyRetrySettingsToPayload(routePayload, retryDelayInput, retryCountInput);
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

        return {
            gateway_model_name: gatewayModelName,
            routes: routeRows.map(normalizeAudioSpeechRouteForSave),
        };
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

        return {
            gateway_model_name: gatewayModelName,
            routes: routeRows.map(normalizeAudioTranscriptionRouteForSave),
        };
    }

    async function loadAudioEditor() {
        try {
            const payload = await loadOperationRulesPayload('Audio Routes');
            await renderAudioSpeech(payload.audio_speech);
            await renderAudioTranscriptions(payload.audio_transcriptions);
            originalAudioContent = getNormalizedAudioContent();
            renderMessage('success', 'Audio Routes loaded successfully.');
        } catch (error) {
            console.error('Error fetching Audio Routes:', error);
            renderErrorWithDetails('Error loading Audio Routes:', error.message);
            audioSpeechList.textContent = '';
            audioTranscriptionsList.textContent = '';
            refreshAudioSpeechEmptyState();
            refreshAudioTranscriptionsEmptyState();
            originalAudioContent = stableSerialize(
                buildOperationRoutesPayload({ audio_speech: [], audio_transcriptions: [] })
            );
        }
    }

    async function renderAudioSpeech(items) {
        audioSpeechList.textContent = '';
        const modelLoadPromises = [];

        if (!Array.isArray(items) || items.length === 0) {
            refreshAudioSpeechEmptyState();
            return;
        }

        items.forEach(item => {
            const card = buildAudioSpeechCard(item);
            audioSpeechList.appendChild(card);
            modelLoadPromises.push(...card._modelLoadPromises);
        });
        refreshAudioSpeechEmptyState();
        await Promise.all(modelLoadPromises);
    }

    async function renderAudioTranscriptions(items) {
        audioTranscriptionsList.textContent = '';
        const modelLoadPromises = [];

        if (!Array.isArray(items) || items.length === 0) {
            refreshAudioTranscriptionsEmptyState();
            return;
        }

        items.forEach(item => {
            const card = buildAudioTranscriptionCard(item);
            audioTranscriptionsList.appendChild(card);
            modelLoadPromises.push(...card._modelLoadPromises);
        });
        refreshAudioTranscriptionsEmptyState();
        await Promise.all(modelLoadPromises);
    }

    function buildAudioSpeechCard(initialData) {
        return buildAudioCard(initialData, {
            gatewayPlaceholder: 'llmgateway/audio-speech-model',
            addRouteButtonText: 'Add Route',
            refreshEmptyState: refreshAudioSpeechEmptyState,
            buildRouteRow: buildAudioSpeechRouteRow,
        });
    }

    function buildAudioTranscriptionCard(initialData) {
        return buildAudioCard(initialData, {
            gatewayPlaceholder: 'llmgateway/audio-transcription-model',
            addRouteButtonText: 'Add Fallback Route',
            refreshEmptyState: refreshAudioTranscriptionsEmptyState,
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

        const gatewayModelInput = createTextInput('gateway-model-input', options.gatewayPlaceholder);
        gatewayModelInput.value = initialData.gateway_model_name || '';
        titleWrap.appendChild(createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        removeButton.textContent = 'Remove Model';
        removeButton.addEventListener('click', () => {
            card.remove();
            options.refreshEmptyState();
        });

        const routeList = document.createElement('div');
        routeList.className = 'fallback-list';

        const addRouteButton = document.createElement('button');
        addRouteButton.type = 'button';
        addRouteButton.className = 'secondary-button add-fallback-button';
        addRouteButton.textContent = options.addRouteButtonText;
        addRouteButton.addEventListener('click', () => {
            routeList.appendChild(options.buildRouteRow({}));
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
            card.classList.toggle('collapsed');
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
        const modelLoadPromises = [];
        routes.forEach(route => {
            const routeRow = options.buildRouteRow(route);
            routeList.appendChild(routeRow);
            modelLoadPromises.push(routeRow._modelLoadPromise);
        });

        if (routes.length === 0) {
            const routeRow = options.buildRouteRow({});
            routeList.appendChild(routeRow);
            modelLoadPromises.push(routeRow._modelLoadPromise);
        }

        card._modelLoadPromises = modelLoadPromises;
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

        setupRowReordering(row);

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid';

        const providerSelect = createSelect('provider-select');
        setSelectOptions(providerSelect, availableProviders, 'Choose a provider', initialData.provider || '');

        const modelInput = createTextInput('model-input', 'Choose or enter model');
        modelInput.value = initialData.model || '';
        const dataListId = `${options.dataListPrefix}-${Math.random().toString(36).substr(2, 9)}`;
        modelInput.setAttribute('list', dataListId);
        const dataList = document.createElement('datalist');
        dataList.id = dataListId;
        row.appendChild(dataList);

        const targetPathInput = createTextInput('target-path-input', options.defaultTargetPath);
        targetPathInput.value = initialData.target_path || options.defaultTargetPath;
        const { retryDelayInput, retryCountInput } = createRetrySettingsInputs(initialData);

        const customBodyParamsInput = createTextarea('custom-body-params-input', options.customBodyPlaceholder);
        customBodyParamsInput.value = normalizeObjectTextarea(initialData.custom_body_params);

        const customHeadersInput = createTextarea('custom-headers-input', '{"X-Header": "value"}');
        customHeadersInput.value = normalizeObjectTextarea(initialData.custom_headers);

        fieldsGrid.appendChild(createFieldGroup('Provider', providerSelect, 'provider-field'));
        fieldsGrid.appendChild(createFieldGroup('Model', modelInput, 'model-field'));
        fieldsGrid.appendChild(createFieldGroup('Target Path', targetPathInput));

        const modelStatus = document.createElement('div');
        modelStatus.className = 'model-status';
        modelStatus.dataset.state = 'idle';

        const advancedDetails = document.createElement('details');
        advancedDetails.className = 'advanced-options';
        const advancedSummary = document.createElement('summary');
        advancedSummary.textContent = 'Advanced options';
        advancedDetails.appendChild(advancedSummary);

        const advancedGrid = document.createElement('div');
        advancedGrid.className = 'advanced-grid';
        if (options.includeRequestFormat) {
            const requestFormatSelect = createSelect('request-format-select');
            setSelectOptions(
                requestFormatSelect,
                AUDIO_REQUEST_FORMAT_OPTIONS,
                'Default request format',
                initialData.request_format || ''
            );
            advancedGrid.appendChild(createFieldGroup('Request Format', requestFormatSelect));
        }
        if (options.includeVoicesTargetPath) {
            const voicesTargetPathInput = createTextInput('voices-target-path-input', '/voices');
            voicesTargetPathInput.value = initialData.voices_target_path || '';
            advancedGrid.appendChild(createFieldGroup('Voices Target Path', voicesTargetPathInput));
        }
        advancedGrid.appendChild(createFieldGroup('Retry Delay', retryDelayInput));
        advancedGrid.appendChild(createFieldGroup('Retry Count', retryCountInput));
        advancedGrid.appendChild(createFieldGroup('Custom Body Params', customBodyParamsInput, 'textarea-group'));
        advancedGrid.appendChild(createFieldGroup('Custom Headers', customHeadersInput, 'textarea-group'));
        advancedDetails.appendChild(advancedGrid);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        removeButton.textContent = options.removeButtonText;
        removeButton.addEventListener('click', () => {
            row.remove();
        });

        const rowActions = document.createElement('div');
        rowActions.className = 'fallback-row-actions';

        const { moveUpButton, moveDownButton } = createMoveButtons(row);
        rowActions.appendChild(moveUpButton);
        rowActions.appendChild(moveDownButton);
        rowActions.appendChild(removeButton);

        row.appendChild(fieldsGrid);
        row.appendChild(modelStatus);
        row.appendChild(advancedDetails);
        row.appendChild(rowActions);

        const refreshModels = async (providerName) => {
            if (!providerName) {
                dataList.textContent = '';
                return;
            }
            modelStatus.textContent = 'Loading models...';
            modelStatus.dataset.state = 'loading';
            try {
                const models = await getProviderModels(providerName);
                dataList.textContent = '';
                models.forEach(modelId => {
                    const option = document.createElement('option');
                    option.value = modelId;
                    dataList.appendChild(option);
                });
                modelStatus.textContent = `${models.length} models loaded for ${providerName}.`;
                modelStatus.dataset.state = 'success';
            } catch (error) {
                modelStatus.textContent = `Could not load models: ${error.message}`;
                modelStatus.dataset.state = 'error';
            }
        };

        providerSelect.addEventListener('change', () => {
            refreshModels(providerSelect.value);
        });

        row._modelLoadPromise = refreshModels(initialData.provider || '');
        return row;
    }

    async function saveAudio() {
        let payload;
        saveButton.disabled = true;
        renderMessage('info', 'Saving Audio Routes...');

        try {
            payload = getAudioPayloadForSave(await fetchOperationRulesPayload());
            const response = await apiFetch('/v1/config/model-operations/structured', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload),
            });

            const body = await response.json().catch(() => ({}));
            if (!response.ok) {
                if (body.detail && Array.isArray(body.detail.errors)) {
                    const errorDetails = body.detail.errors.map(err => {
                        const loc = err.loc ? err.loc.join(' -> ') : 'N/A';
                        return `- Location: ${loc}, Message: ${err.msg}, Type: ${err.type}`;
                    }).join('\n');
                    renderErrorWithDetails(
                        `Validation Error for Audio Routes (HTTP ${response.status}):`,
                        `${body.detail.message}\n${errorDetails}`
                    );
                } else {
                    renderErrorWithDetails(
                        `Error saving Audio Routes (HTTP ${response.status}):`,
                        body.detail || 'Unknown error'
                    );
                }
                return;
            }

            applyOperationRulesPayload(payload);
            originalAudioContent = stableSerialize(payload);
            renderMessage('success', body.message || 'Audio Routes updated successfully.');
        } catch (error) {
            console.error('Error saving Audio Routes:', error);
            renderMessage('error', `Error saving Audio Routes: ${error.message}`);
        } finally {
            saveButton.disabled = false;
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
        const gatewayModelInput = createTextInput('gateway-model-input', gatewayPlaceholder);
        gatewayModelInput.value = initialData.gateway_model_name || '';
        titleWrap.appendChild(createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const headerLeft = document.createElement('div');
        headerLeft.className = 'rule-card-header-left';
        headerLeft.appendChild(createAccordionToggle(card));
        headerLeft.appendChild(titleWrap);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        removeButton.textContent = removeLabel;
        removeButton.addEventListener('click', () => {
            card.remove();
            refreshEmptyState();
            refreshWebCrossDropdowns();
        });

        cardHeader.appendChild(headerLeft);
        cardHeader.appendChild(removeButton);

        const cardBody = document.createElement('div');
        cardBody.className = 'rule-card-body';
        card.appendChild(cardHeader);
        card.appendChild(cardBody);
        return { card, cardBody, gatewayModelInput };
    }

    function appendFieldHint(fieldGroup, hintText) {
        if (!hintText) return;
        const hint = document.createElement('small');
        hint.className = 'field-hint';
        hint.textContent = hintText;
        fieldGroup.appendChild(hint);
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
        const queryModelSelect = createSelect('query-model-input');
        setModelSelectOptions(queryModelSelect, gatewayModelCatalog.chat, initialData.query_model || '');
        const queryField = createFieldGroup('Query Model (optional)', queryModelSelect, 'model-field');
        appendFieldHint(queryField, 'Gateway chat LLM used to expand the user query into multiple search queries. Leave empty to skip.');
        serviceGrid.appendChild(queryField);
        cardBody.appendChild(serviceGrid);

        card._modelLoadPromises = [];
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
        card._modelLoadPromises = [];
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
        return payload;
    }

    function normalizeWebReadCardForSave(ruleCard) {
        const gatewayModelName = ruleCard.querySelector('.gateway-model-input').value.trim();
        if (!gatewayModelName) {
            throw new Error('Each web read service must have a gateway model name.');
        }
        return { gateway_model_name: gatewayModelName };
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
                control = createSelect(field.className);
                const catalogOptions = gatewayModelCatalog[field.catalog] || [];
                setModelSelectOptions(control, catalogOptions, initialData[field.key] || field.defaultValue || '');
            } else {
                control = createTextInput(field.className, field.placeholder);
                control.value = initialData[field.key] || field.defaultValue || '';
            }
            const group = createFieldGroup(field.label, control, 'model-field');
            appendFieldHint(group, field.hint);
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
        return payload;
    }

    const WEB_SEARCH_CARD_OPTIONS = {
        gatewayPlaceholder: 'llmgateway/web-search',
        refreshEmptyState: refreshWebSearchEmptyState,
    };

    const WEB_READ_CARD_OPTIONS = {
        gatewayPlaceholder: 'llmgateway/web-read',
        refreshEmptyState: refreshWebReadEmptyState,
    };

    const WEB_RESEARCH_CARD_OPTIONS = {
        gatewayPlaceholder: 'llmgateway/web-research',
        serviceLabel: 'web research service',
        refreshEmptyState: refreshWebResearchEmptyState,
        fields: [
            { key: 'search_model', label: 'Search Model', className: 'search-model-input', catalog: 'web_search', required: true, hint: 'Pick one of the Web Search services configured above.' },
            { key: 'read_model', label: 'Read Model', className: 'read-model-input', catalog: 'web_read', required: true, hint: 'Pick one of the Web Read services configured above.' },
            { key: 'rerank_model', label: 'Rerank Model', className: 'rerank-model-input', catalog: 'rerank', required: true, hint: 'Gateway rerank model used to order search results before reading articles.' },
            { key: 'analysis_model', label: 'Analysis Model', className: 'analysis-model-input', catalog: 'chat', required: true, hint: 'Chat LLM from Fallback Rules that composes the final answer.' },
        ],
    };

    const WEB_DEEP_RESEARCH_CARD_OPTIONS = {
        gatewayPlaceholder: 'llmgateway/web-deep-research',
        serviceLabel: 'web deep research service',
        refreshEmptyState: refreshWebDeepResearchEmptyState,
        fields: [
            { key: 'search_model', label: 'Search Model', className: 'search-model-input', catalog: 'web_search', required: true, hint: 'Pick one of the Web Search services configured above.' },
            { key: 'read_model', label: 'Read Model', className: 'read-model-input', catalog: 'web_read', required: true, hint: 'Pick one of the Web Read services configured above.' },
            { key: 'fast_model', label: 'Fast LLM', className: 'fast-model-input', catalog: 'chat', required: true, hint: 'Used for short, cheap calls (query planning, summaries).' },
            { key: 'smart_model', label: 'Smart LLM', className: 'smart-model-input', catalog: 'chat', required: true, hint: 'Used for report writing and quality-sensitive steps.' },
            { key: 'strategic_model', label: 'Strategic LLM', className: 'strategic-model-input', catalog: 'chat', required: true, hint: 'Used for high-level planning; can equal Smart.' },
            { key: 'embedding_model', label: 'Embedding Model', className: 'embedding-model-input', catalog: 'embeddings', hint: 'Optional — used only by GPT Researcher vector stores.' },
            { key: 'image_generation_model', label: 'Image Generation Model', className: 'image-generation-model-input', catalog: 'images_generations', hint: 'Gateway image model used when image_generation=true.' },
            { key: 'image_generation_size', label: 'Image Generation Size', className: 'image-generation-size-input', placeholder: '1024x1024', hint: 'Passed through to /v1/images (WIDTHxHEIGHT).' },
        ],
    };

    function getWebPayloadForSave(basePayload = null) {
        const web_search = Array.from(webSearchList.querySelectorAll('.rule-card')).map(normalizeWebSearchCardForSave);
        const web_read = Array.from(webReadList.querySelectorAll('.rule-card')).map(normalizeWebReadCardForSave);
        const web_research = Array.from(webResearchList.querySelectorAll('.rule-card')).map(
            card => normalizeWebReferenceCardForSave(card, WEB_RESEARCH_CARD_OPTIONS)
        );
        const web_deep_research = Array.from(webDeepResearchList.querySelectorAll('.rule-card')).map(
            card => normalizeWebReferenceCardForSave(card, WEB_DEEP_RESEARCH_CARD_OPTIONS)
        );
        return buildOperationRoutesPayload(
            { web_search, web_read, web_research, web_deep_research },
            basePayload
        );
    }

    function getNormalizedWebContent() {
        return stableSerialize(getWebPayloadForSave());
    }

    async function loadWebEditor() {
        try {
            const payload = await loadOperationRulesPayload('Web Services');
            await loadGatewayModelCatalog();
            applyOperationCatalog(payload);
            await renderWebSections(payload);
            refreshWebCrossDropdowns();
            originalWebContent = getNormalizedWebContent();
            renderMessage('success', 'Web Services loaded successfully.');
        } catch (error) {
            console.error('Error fetching Web Services:', error);
            renderErrorWithDetails('Error loading Web Services:', error.message);
            webSearchList.textContent = '';
            webReadList.textContent = '';
            webResearchList.textContent = '';
            webDeepResearchList.textContent = '';
            refreshWebSearchEmptyState();
            refreshWebReadEmptyState();
            refreshWebResearchEmptyState();
            refreshWebDeepResearchEmptyState();
            originalWebContent = stableSerialize(buildOperationRoutesPayload({
                web_search: [],
                web_read: [],
                web_research: [],
                web_deep_research: [],
            }));
        }
    }

    async function renderWebSections(payload) {
        webSearchList.textContent = '';
        webReadList.textContent = '';
        webResearchList.textContent = '';
        webDeepResearchList.textContent = '';
        const modelLoadPromises = [];

        (payload.web_search || []).forEach(item => {
            const card = buildWebSearchCard(item, WEB_SEARCH_CARD_OPTIONS);
            webSearchList.appendChild(card);
            modelLoadPromises.push(...card._modelLoadPromises);
        });
        (payload.web_read || []).forEach(item => {
            const card = buildWebReadCard(item, WEB_READ_CARD_OPTIONS);
            webReadList.appendChild(card);
            modelLoadPromises.push(...card._modelLoadPromises);
        });
        (payload.web_research || []).forEach(item => {
            webResearchList.appendChild(buildWebReferenceCard(item, WEB_RESEARCH_CARD_OPTIONS));
        });
        (payload.web_deep_research || []).forEach(item => {
            webDeepResearchList.appendChild(buildWebReferenceCard(item, WEB_DEEP_RESEARCH_CARD_OPTIONS));
        });

        refreshWebSearchEmptyState();
        refreshWebReadEmptyState();
        refreshWebResearchEmptyState();
        refreshWebDeepResearchEmptyState();
        await Promise.all(modelLoadPromises);
    }

    async function saveWeb() {
        let payload;
        saveButton.disabled = true;
        renderMessage('info', 'Saving Web Services...');

        try {
            payload = getWebPayloadForSave(await fetchOperationRulesPayload());
            const response = await apiFetch('/v1/config/model-operations/structured', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload),
            });

            const body = await response.json().catch(() => ({}));
            if (!response.ok) {
                if (body.detail && Array.isArray(body.detail.errors)) {
                    const errorDetails = body.detail.errors.map(err => {
                        const loc = err.loc ? err.loc.join(' -> ') : 'N/A';
                        return `- Location: ${loc}, Message: ${err.msg}, Type: ${err.type}`;
                    }).join('\n');
                    renderErrorWithDetails(
                        `Validation Error for Web Services (HTTP ${response.status}):`,
                        `${body.detail.message}\n${errorDetails}`
                    );
                } else {
                    renderErrorWithDetails(
                        `Error saving Web Services (HTTP ${response.status}):`,
                        body.detail || 'Unknown error'
                    );
                }
                return;
            }

            applyOperationRulesPayload(payload);
            originalWebContent = stableSerialize(payload);
            renderMessage('success', body.message || 'Web Services updated successfully.');
        } catch (error) {
            console.error('Error saving Web Services:', error);
            renderMessage('error', `Error saving Web Services: ${error.message}`);
        } finally {
            saveButton.disabled = false;
        }
    }

    function clearRulesCache() {
        providerModelsCache.clear();
        providerModelsRequests.clear();
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

    function normalizeProviderCardForSave(providerCard) {
        const nameInput = providerCard.querySelector('.provider-name-input');
        const baseUrlInput = providerCard.querySelector('.provider-base-url-input');
        const apiKeyInput = providerCard.querySelector('.provider-api-key-input');
        const typeSelect = providerCard.querySelector('.provider-type-select');
        const proxyInput = providerCard.querySelector('.provider-proxy-input');
        const modelsInput = providerCard.querySelector('.provider-models-input');

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
        if (!apikey) {
            throw new Error(`Provider '${name}' must have an API key or environment reference.`);
        }
        if (!['openai', 'anthropic'].includes(type)) {
            throw new Error(`Provider '${name}' must use API type openai or anthropic.`);
        }

        const providerPayload = {
            name,
            baseUrl,
            apikey,
            type,
        };
        if (proxy) {
            providerPayload.proxy = proxy;
        }
        const models = parseProviderModelsMetadata(modelsInput.value);
        if (models !== undefined) {
            providerPayload.models = models;
        }
        return providerPayload;
    }

    function getProvidersPayloadForSave() {
        const providers = Array.from(providersList.querySelectorAll('.provider-card')).map(normalizeProviderCardForSave);
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
        return stableSerialize(getProvidersPayloadForSave());
    }

    function buildProviderCard(initialData = {}) {
        const card = document.createElement('section');
        card.className = 'rule-card provider-card collapsed';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';
        const providerNameInput = createTextInput('provider-name-input', 'openrouter');
        providerNameInput.value = initialData.name || '';
        titleWrap.appendChild(createFieldGroup('Provider Name', providerNameInput, 'gateway-model-field'));

        const headerLeft = document.createElement('div');
        headerLeft.className = 'rule-card-header-left';
        headerLeft.appendChild(createAccordionToggle(card));
        headerLeft.appendChild(titleWrap);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        removeButton.textContent = 'Remove Provider';
        removeButton.addEventListener('click', () => {
            card.remove();
            refreshProvidersEmptyState();
        });

        cardHeader.appendChild(headerLeft);
        cardHeader.appendChild(removeButton);

        const cardBody = document.createElement('div');
        cardBody.className = 'rule-card-body';

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid provider-fields-grid';

        const baseUrlInput = createTextInput('provider-base-url-input', 'https://api.example.com/v1');
        baseUrlInput.value = initialData.baseUrl || '';
        const apiKeyInput = createTextInput('provider-api-key-input', '${APIKEY_PROVIDER}');
        apiKeyInput.value = initialData.apikey || '';
        const typeSelect = createSelect('provider-type-select');
        setSelectOptions(typeSelect, ['openai', 'anthropic'], 'Choose API type', initialData.type || 'openai');
        const proxyInput = createTextInput('provider-proxy-input', '${PROXY_PROVIDER} or https://proxy:8080');
        proxyInput.value = initialData.proxy || '';

        fieldsGrid.appendChild(createFieldGroup('Base URL', baseUrlInput, 'provider-base-url-field'));
        fieldsGrid.appendChild(createFieldGroup('API Key', apiKeyInput, 'provider-api-key-field'));
        fieldsGrid.appendChild(createFieldGroup('API Type', typeSelect, 'provider-type-field'));
        fieldsGrid.appendChild(createFieldGroup('Proxy (optional)', proxyInput, 'provider-proxy-field'));

        const advancedDetails = document.createElement('details');
        advancedDetails.className = 'advanced-options';
        const advancedSummary = document.createElement('summary');
        advancedSummary.textContent = 'Advanced options';
        advancedDetails.appendChild(advancedSummary);

        const modelsInput = createTextarea('provider-models-input', '{"pricing": {"input": 0.1}}');
        modelsInput.value = normalizeProviderModelsMetadata(initialData.models);
        const modelsField = createFieldGroup('Models Metadata (JSON)', modelsInput, 'textarea-group');
        appendFieldHint(modelsField, 'Optional provider-specific metadata stored as the providers.json models field.');
        const advancedGrid = document.createElement('div');
        advancedGrid.className = 'advanced-grid';
        advancedGrid.appendChild(modelsField);
        advancedDetails.appendChild(advancedGrid);

        cardBody.appendChild(fieldsGrid);
        cardBody.appendChild(advancedDetails);
        card.appendChild(cardHeader);
        card.appendChild(cardBody);

        return card;
    }

    async function renderProviders(providers) {
        providersList.textContent = '';
        if (!Array.isArray(providers) || providers.length === 0) {
            refreshProvidersEmptyState();
            return;
        }

        providers.forEach(provider => {
            providersList.appendChild(buildProviderCard(provider));
        });
        refreshProvidersEmptyState();
    }

    async function loadProvidersEditor() {
        renderMessage('info', 'Loading Providers...');
        try {
            const response = await apiFetch('/v1/config/providers/structured');
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.detail || `HTTP ${response.status}`);
            }
            const providers = Array.isArray(payload.providers) ? payload.providers : [];
            await renderProviders(providers);
            availableProviders = providers
                .map(provider => typeof provider.name === 'string' ? provider.name.trim() : '')
                .filter(Boolean);
            originalProvidersContent = getProvidersSnapshotContent();
            renderMessage('success', 'Providers loaded successfully.');
        } catch (error) {
            console.error('Error fetching Providers:', error);
            renderErrorWithDetails('Error loading Providers:', error.message);
            providersList.textContent = '';
            refreshProvidersEmptyState();
            originalProvidersContent = stableSerialize({ providers: [] });
        }
    }

    function appendOpenRouterMeta(parent, label, value) {
        const item = document.createElement('div');
        item.className = 'openrouter-free-meta-item';
        const labelElement = document.createElement('strong');
        labelElement.textContent = label;
        const valueElement = document.createElement('span');
        valueElement.textContent = value;
        item.appendChild(labelElement);
        item.appendChild(valueElement);
        parent.appendChild(item);
    }

    function renderOpenRouterFreeModels(payload) {
        clearElement(openRouterFreeStatus);
        clearElement(openRouterFreeModels);

        const snapshot = payload.snapshot;
        if (!snapshot) {
            openRouterFreeEmptyState.hidden = false;
            appendOpenRouterMeta(openRouterFreeStatus, 'Status', payload.lastError || 'Waiting for first scoring refresh');
            appendOpenRouterMeta(openRouterFreeStatus, 'Next refresh', formatDateTime(payload.nextRefreshAt));
            return;
        }

        openRouterFreeEmptyState.hidden = Array.isArray(snapshot.models) && snapshot.models.length > 0;

        appendOpenRouterMeta(openRouterFreeStatus, 'Refresh mode', snapshot.refreshMode || 'n/a');
        appendOpenRouterMeta(openRouterFreeStatus, 'Last updated', formatDateTime(snapshot.updatedAt));
        appendOpenRouterMeta(openRouterFreeStatus, 'Next refresh', formatDateTime(payload.nextRefreshAt));
        appendOpenRouterMeta(openRouterFreeStatus, 'Catalog models', formatNumber(snapshot.catalogCount));
        appendOpenRouterMeta(openRouterFreeStatus, 'Eligible models', formatNumber(snapshot.eligibleCount));
        appendOpenRouterMeta(openRouterFreeStatus, 'Lite evals', formatNumber(snapshot.evaluatedCount));
        if (payload.lastError) {
            appendOpenRouterMeta(openRouterFreeStatus, 'Last error', payload.lastError);
        }

        (snapshot.models || []).forEach(model => {
            const card = document.createElement('article');
            card.className = 'openrouter-free-card';

            const header = document.createElement('div');
            header.className = 'openrouter-free-card-header';

            const title = document.createElement('div');
            const rank = document.createElement('div');
            rank.className = 'openrouter-free-rank';
            rank.textContent = `#${model.rank || '?'}`;
            const name = document.createElement('strong');
            name.textContent = model.name || model.id || 'Unknown model';
            const id = document.createElement('code');
            id.textContent = model.id || '';
            title.appendChild(rank);
            title.appendChild(name);
            title.appendChild(id);

            const score = document.createElement('div');
            score.className = 'openrouter-free-score';
            score.textContent = formatNumber(model.score);

            header.appendChild(title);
            header.appendChild(score);
            card.appendChild(header);

            const reason = document.createElement('p');
            reason.className = 'openrouter-free-reason';
            reason.textContent = model.reason || 'Free text model';
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
                const metric = document.createElement('span');
                metric.textContent = `${label}: ${typeof value === 'number' ? formatNumber(value) : (value || 'n/a')}`;
                metrics.appendChild(metric);
            });
            card.appendChild(metrics);

            openRouterFreeModels.appendChild(card);
        });
    }

    async function loadOpenRouterFreeModels() {
        renderMessage('info', 'Loading OpenRouter free model ranking...');
        clearElement(openRouterFreeStatus);
        clearElement(openRouterFreeModels);
        openRouterFreeEmptyState.hidden = true;
        try {
            const response = await apiFetch('/v1/openrouter/free-models');
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.detail || `HTTP ${response.status}`);
            }
            if (!payload.configured) {
                tabOpenRouterFree.hidden = true;
                if (activeEditor === 'openrouter-free') {
                    switchTab('rules');
                }
                return;
            }
            renderOpenRouterFreeModels(payload);
            renderMessage('success', 'OpenRouter free model ranking loaded.');
        } catch (error) {
            console.error('Error loading OpenRouter free model ranking:', error);
            renderErrorWithDetails('Error loading OpenRouter free model ranking:', error.message);
            openRouterFreeEmptyState.hidden = false;
        }
    }

    async function initializeOpenRouterFreeTabAvailability() {
        try {
            const response = await apiFetch('/v1/openrouter/free-models');
            const payload = await response.json().catch(() => ({}));
            tabOpenRouterFree.hidden = !response.ok || !payload.configured;
        } catch (error) {
            tabOpenRouterFree.hidden = true;
        }
    }

    async function saveRules() {
        let payload;
        try {
            payload = getRulesPayloadForSave();
        } catch (error) {
            renderMessage('error', error.message);
            return;
        }

        saveButton.disabled = true;
        renderMessage('info', 'Saving Fallback Rules...');

        try {
            const response = await apiFetch('/v1/config/models-rules/structured', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload),
            });

            const body = await response.json().catch(() => ({}));
            if (!response.ok) {
                if (body.detail && Array.isArray(body.errors)) {
                    const errorDetails = body.errors.map(err => {
                        const loc = err.loc ? err.loc.join(' -> ') : 'N/A';
                        return `- Location: ${loc}, Message: ${err.msg}, Type: ${err.type}`;
                    }).join('\n');
                    renderErrorWithDetails(
                        `Validation Error for Fallback Rules (HTTP ${response.status}):`,
                        `${body.detail}\n${errorDetails}`
                    );
                } else {
                    renderErrorWithDetails(
                        `Error saving Fallback Rules (HTTP ${response.status}):`,
                        body.detail || 'Unknown error'
                    );
                }
                return;
            }

            originalRulesContent = stableSerialize(payload);
            renderMessage('success', body.message || 'Fallback Rules updated successfully.');
        } catch (error) {
            console.error('Error saving Fallback Rules:', error);
            renderMessage('error', `Error saving Fallback Rules: ${error.message}`);
        } finally {
            saveButton.disabled = false;
        }
    }

    async function saveProviders() {
        let payload;
        try {
            payload = getProvidersPayloadForSave();
        } catch (error) {
            renderMessage('error', error.message);
            return;
        }

        saveButton.disabled = true;
        renderMessage('info', 'Saving Providers...');

        try {
            const response = await apiFetch('/v1/config/providers/structured', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload),
            });

            const body = await response.json().catch(() => ({}));

            if (response.status === 200 && body.message) {
                const providers = Array.isArray(body.providers) ? body.providers : payload.providers;
                originalProvidersContent = stableSerialize({ providers });
                clearRulesCache();
                availableProviders = providers
                    .map(provider => typeof provider.name === 'string' ? provider.name.trim() : '')
                    .filter(Boolean);
                renderMessage('success', `Providers ${body.message.toLowerCase()}`);
                return;
            }

            if (body.detail && Array.isArray(body.errors)) {
                const errorDetails = body.errors.map(err => {
                    const loc = err.loc ? err.loc.join(' -> ') : 'N/A';
                    return `- Location: ${loc}, Message: ${err.msg}, Type: ${err.type}`;
                }).join('\n');
                renderErrorWithDetails(
                    `Validation Error for Providers (HTTP ${response.status}):`,
                    `${body.detail}\n${errorDetails}`
                );
            } else {
                renderErrorWithDetails(
                    `Error saving Providers (HTTP ${response.status}):`,
                    body.detail || 'Unknown error'
                );
            }
        } catch (error) {
            console.error('Error saving Providers:', error);
            renderMessage('error', `Error saving Providers: ${error.message}`);
        } finally {
            saveButton.disabled = false;
        }
    }

    function switchTab(tabName) {
        if (
            activeEditor === tabName
            && (
                originalRulesContent !== null
                || originalEmbeddingsContent !== null
                || originalRerankContent !== null
                || originalImagesContent !== null
                || originalAudioContent !== null
                || originalWebContent !== null
                || originalProvidersContent !== null
            )
        ) {
            return;
        }

        let hasUnsavedChanges = false;
        if (activeEditor === 'rules' && originalRulesContent !== null) {
            try {
                if (getRulesSnapshotContent() !== originalRulesContent) {
                    hasUnsavedChanges = true;
                }
            } catch (error) {
                hasUnsavedChanges = true;
            }
        } else if (activeEditor === 'embeddings' && originalEmbeddingsContent !== null) {
            try {
                if (getNormalizedEmbeddingsContent() !== originalEmbeddingsContent) {
                    hasUnsavedChanges = true;
                }
            } catch (error) {
                hasUnsavedChanges = true;
            }
        } else if (activeEditor === 'rerank' && originalRerankContent !== null) {
            try {
                if (getNormalizedRerankContent() !== originalRerankContent) {
                    hasUnsavedChanges = true;
                }
            } catch (error) {
                hasUnsavedChanges = true;
            }
        } else if (activeEditor === 'images' && originalImagesContent !== null) {
            try {
                if (getNormalizedImagesContent() !== originalImagesContent) {
                    hasUnsavedChanges = true;
                }
            } catch (error) {
                hasUnsavedChanges = true;
            }
        } else if (activeEditor === 'audio' && originalAudioContent !== null) {
            try {
                if (getNormalizedAudioContent() !== originalAudioContent) {
                    hasUnsavedChanges = true;
                }
            } catch (error) {
                hasUnsavedChanges = true;
            }
        } else if (activeEditor === 'web' && originalWebContent !== null) {
            try {
                if (getNormalizedWebContent() !== originalWebContent) {
                    hasUnsavedChanges = true;
                }
            } catch (error) {
                hasUnsavedChanges = true;
            }
        } else if (activeEditor === 'providers' && originalProvidersContent !== null) {
            try {
                if (getProvidersSnapshotContent() !== originalProvidersContent) {
                    hasUnsavedChanges = true;
                }
            } catch (error) {
                hasUnsavedChanges = true;
            }
        }

        if (hasUnsavedChanges) {
            if (!confirm('You have unsaved changes. Are you sure you want to switch tabs? Your changes will be lost.')) {
                return;
            }
        }

        activeEditor = tabName;
        updateControlsVisibility();
        tabOpenRouterFree.classList.remove('active');
        editorContainerOpenRouterFree.classList.remove('active');
        editorContainerOpenRouterFree.style.display = 'none';

        if (tabName === 'rules') {
            tabRules.classList.add('active');
            tabEmbeddings.classList.remove('active');
            tabRerank.classList.remove('active');
            tabImages.classList.remove('active');
            tabAudio.classList.remove('active');
            tabWeb.classList.remove('active');
            tabProviders.classList.remove('active');
            editorContainerRules.classList.add('active');
            editorContainerRules.style.display = 'flex';
            editorContainerEmbeddings.classList.remove('active');
            editorContainerEmbeddings.style.display = 'none';
            editorContainerRerank.classList.remove('active');
            editorContainerRerank.style.display = 'none';
            editorContainerImages.classList.remove('active');
            editorContainerImages.style.display = 'none';
            editorContainerAudio.classList.remove('active');
            editorContainerAudio.style.display = 'none';
            editorContainerWeb.classList.remove('active');
            editorContainerWeb.style.display = 'none';
            editorContainerProviders.classList.remove('active');
            editorContainerProviders.style.display = 'none';
            loadRulesEditor();
        } else if (tabName === 'embeddings') {
            tabRules.classList.remove('active');
            tabEmbeddings.classList.add('active');
            tabRerank.classList.remove('active');
            tabImages.classList.remove('active');
            tabAudio.classList.remove('active');
            tabWeb.classList.remove('active');
            tabProviders.classList.remove('active');
            editorContainerRules.classList.remove('active');
            editorContainerRules.style.display = 'none';
            editorContainerEmbeddings.classList.add('active');
            editorContainerEmbeddings.style.display = 'flex';
            editorContainerRerank.classList.remove('active');
            editorContainerRerank.style.display = 'none';
            editorContainerImages.classList.remove('active');
            editorContainerImages.style.display = 'none';
            editorContainerAudio.classList.remove('active');
            editorContainerAudio.style.display = 'none';
            editorContainerWeb.classList.remove('active');
            editorContainerWeb.style.display = 'none';
            editorContainerProviders.classList.remove('active');
            editorContainerProviders.style.display = 'none';
            loadEmbeddingsEditor();
        } else if (tabName === 'rerank') {
            tabRules.classList.remove('active');
            tabEmbeddings.classList.remove('active');
            tabRerank.classList.add('active');
            tabImages.classList.remove('active');
            tabAudio.classList.remove('active');
            tabWeb.classList.remove('active');
            tabProviders.classList.remove('active');
            editorContainerRules.classList.remove('active');
            editorContainerRules.style.display = 'none';
            editorContainerEmbeddings.classList.remove('active');
            editorContainerEmbeddings.style.display = 'none';
            editorContainerRerank.classList.add('active');
            editorContainerRerank.style.display = 'flex';
            editorContainerImages.classList.remove('active');
            editorContainerImages.style.display = 'none';
            editorContainerAudio.classList.remove('active');
            editorContainerAudio.style.display = 'none';
            editorContainerWeb.classList.remove('active');
            editorContainerWeb.style.display = 'none';
            editorContainerProviders.classList.remove('active');
            editorContainerProviders.style.display = 'none';
            loadRerankEditor();
        } else if (tabName === 'images') {
            tabRules.classList.remove('active');
            tabEmbeddings.classList.remove('active');
            tabRerank.classList.remove('active');
            tabImages.classList.add('active');
            tabAudio.classList.remove('active');
            tabWeb.classList.remove('active');
            tabProviders.classList.remove('active');
            editorContainerRules.classList.remove('active');
            editorContainerRules.style.display = 'none';
            editorContainerEmbeddings.classList.remove('active');
            editorContainerEmbeddings.style.display = 'none';
            editorContainerRerank.classList.remove('active');
            editorContainerRerank.style.display = 'none';
            editorContainerImages.classList.add('active');
            editorContainerImages.style.display = 'flex';
            editorContainerAudio.classList.remove('active');
            editorContainerAudio.style.display = 'none';
            editorContainerWeb.classList.remove('active');
            editorContainerWeb.style.display = 'none';
            editorContainerProviders.classList.remove('active');
            editorContainerProviders.style.display = 'none';
            loadImagesEditor();
        } else if (tabName === 'audio') {
            tabRules.classList.remove('active');
            tabEmbeddings.classList.remove('active');
            tabRerank.classList.remove('active');
            tabImages.classList.remove('active');
            tabAudio.classList.add('active');
            tabWeb.classList.remove('active');
            tabProviders.classList.remove('active');
            editorContainerRules.classList.remove('active');
            editorContainerRules.style.display = 'none';
            editorContainerEmbeddings.classList.remove('active');
            editorContainerEmbeddings.style.display = 'none';
            editorContainerRerank.classList.remove('active');
            editorContainerRerank.style.display = 'none';
            editorContainerImages.classList.remove('active');
            editorContainerImages.style.display = 'none';
            editorContainerAudio.classList.add('active');
            editorContainerAudio.style.display = 'flex';
            editorContainerWeb.classList.remove('active');
            editorContainerWeb.style.display = 'none';
            editorContainerProviders.classList.remove('active');
            editorContainerProviders.style.display = 'none';
            loadAudioEditor();
        } else if (tabName === 'web') {
            tabRules.classList.remove('active');
            tabEmbeddings.classList.remove('active');
            tabRerank.classList.remove('active');
            tabImages.classList.remove('active');
            tabAudio.classList.remove('active');
            tabWeb.classList.add('active');
            tabProviders.classList.remove('active');
            editorContainerRules.classList.remove('active');
            editorContainerRules.style.display = 'none';
            editorContainerEmbeddings.classList.remove('active');
            editorContainerEmbeddings.style.display = 'none';
            editorContainerRerank.classList.remove('active');
            editorContainerRerank.style.display = 'none';
            editorContainerImages.classList.remove('active');
            editorContainerImages.style.display = 'none';
            editorContainerAudio.classList.remove('active');
            editorContainerAudio.style.display = 'none';
            editorContainerWeb.classList.add('active');
            editorContainerWeb.style.display = 'flex';
            editorContainerProviders.classList.remove('active');
            editorContainerProviders.style.display = 'none';
            loadWebEditor();
        } else if (tabName === 'openrouter-free') {
            tabRules.classList.remove('active');
            tabEmbeddings.classList.remove('active');
            tabRerank.classList.remove('active');
            tabImages.classList.remove('active');
            tabAudio.classList.remove('active');
            tabWeb.classList.remove('active');
            tabOpenRouterFree.classList.add('active');
            tabProviders.classList.remove('active');
            editorContainerRules.classList.remove('active');
            editorContainerRules.style.display = 'none';
            editorContainerEmbeddings.classList.remove('active');
            editorContainerEmbeddings.style.display = 'none';
            editorContainerRerank.classList.remove('active');
            editorContainerRerank.style.display = 'none';
            editorContainerImages.classList.remove('active');
            editorContainerImages.style.display = 'none';
            editorContainerAudio.classList.remove('active');
            editorContainerAudio.style.display = 'none';
            editorContainerWeb.classList.remove('active');
            editorContainerWeb.style.display = 'none';
            editorContainerOpenRouterFree.classList.add('active');
            editorContainerOpenRouterFree.style.display = 'flex';
            editorContainerProviders.classList.remove('active');
            editorContainerProviders.style.display = 'none';
            loadOpenRouterFreeModels();
        } else if (tabName === 'providers') {
            tabRules.classList.remove('active');
            tabEmbeddings.classList.remove('active');
            tabRerank.classList.remove('active');
            tabImages.classList.remove('active');
            tabAudio.classList.remove('active');
            tabWeb.classList.remove('active');
            tabProviders.classList.add('active');
            editorContainerRules.classList.remove('active');
            editorContainerRules.style.display = 'none';
            editorContainerEmbeddings.classList.remove('active');
            editorContainerEmbeddings.style.display = 'none';
            editorContainerRerank.classList.remove('active');
            editorContainerRerank.style.display = 'none';
            editorContainerImages.classList.remove('active');
            editorContainerImages.style.display = 'none';
            editorContainerAudio.classList.remove('active');
            editorContainerAudio.style.display = 'none';
            editorContainerWeb.classList.remove('active');
            editorContainerWeb.style.display = 'none';
            editorContainerProviders.classList.add('active');
            editorContainerProviders.style.display = 'flex';
            loadProvidersEditor();
        }
    }

    tabRules.addEventListener('click', () => switchTab('rules'));
    tabEmbeddings.addEventListener('click', () => switchTab('embeddings'));
    tabRerank.addEventListener('click', () => switchTab('rerank'));
    tabImages.addEventListener('click', () => switchTab('images'));
    tabAudio.addEventListener('click', () => switchTab('audio'));
    tabWeb.addEventListener('click', () => switchTab('web'));
    tabOpenRouterFree.addEventListener('click', () => switchTab('openrouter-free'));
    tabProviders.addEventListener('click', () => switchTab('providers'));
    addProviderButton.addEventListener('click', () => {
        const providerCard = buildProviderCard({});
        providerCard.classList.remove('collapsed');
        providersList.appendChild(providerCard);
        refreshProvidersEmptyState();
    });
    addRuleButton.addEventListener('click', () => {
        const ruleCard = buildRuleCard({});
        ruleCard.classList.remove('collapsed');
        rulesList.appendChild(ruleCard);
        refreshRulesEmptyState();
    });
    addEmbeddingButton.addEventListener('click', () => {
        const embeddingCard = buildEmbeddingCard({});
        embeddingCard.classList.remove('collapsed');
        embeddingsList.appendChild(embeddingCard);
        refreshEmbeddingsEmptyState();
    });
    addRerankButton.addEventListener('click', () => {
        const rerankCard = buildRerankCard({});
        rerankCard.classList.remove('collapsed');
        rerankList.appendChild(rerankCard);
        refreshRerankEmptyState();
    });
    addImageGenerationButton.addEventListener('click', () => {
        const imageGenerationCard = buildImageCard({}, {
            gatewayPlaceholder: 'llmgateway/image-generation-model',
            defaultTargetPath: '/images/generations',
            refreshEmptyState: refreshImageGenerationEmptyState,
        });
        imageGenerationCard.classList.remove('collapsed');
        imageGenerationList.appendChild(imageGenerationCard);
        refreshImageGenerationEmptyState();
    });
    addImageEditButton.addEventListener('click', () => {
        const imageEditCard = buildImageCard({}, {
            gatewayPlaceholder: 'llmgateway/image-edit-model',
            defaultTargetPath: '/images/edits',
            refreshEmptyState: refreshImageEditEmptyState,
        });
        imageEditCard.classList.remove('collapsed');
        imageEditList.appendChild(imageEditCard);
        refreshImageEditEmptyState();
    });
    addAudioSpeechButton.addEventListener('click', () => {
        const audioCard = buildAudioSpeechCard({});
        audioCard.classList.remove('collapsed');
        audioSpeechList.appendChild(audioCard);
        refreshAudioSpeechEmptyState();
    });
    addAudioTranscriptionButton.addEventListener('click', () => {
        const audioCard = buildAudioTranscriptionCard({});
        audioCard.classList.remove('collapsed');
        audioTranscriptionsList.appendChild(audioCard);
        refreshAudioTranscriptionsEmptyState();
    });
    addWebSearchButton.addEventListener('click', () => {
        const searchCard = buildWebSearchCard({}, WEB_SEARCH_CARD_OPTIONS);
        searchCard.classList.remove('collapsed');
        webSearchList.appendChild(searchCard);
        refreshWebSearchEmptyState();
        refreshWebCrossDropdowns();
    });
    addWebReadButton.addEventListener('click', () => {
        const readCard = buildWebReadCard({}, WEB_READ_CARD_OPTIONS);
        readCard.classList.remove('collapsed');
        webReadList.appendChild(readCard);
        refreshWebReadEmptyState();
        refreshWebCrossDropdowns();
    });
    addWebResearchButton.addEventListener('click', () => {
        const researchCard = buildWebReferenceCard({}, WEB_RESEARCH_CARD_OPTIONS);
        researchCard.classList.remove('collapsed');
        webResearchList.appendChild(researchCard);
        refreshWebResearchEmptyState();
    });
    addWebDeepResearchButton.addEventListener('click', () => {
        const deepResearchCard = buildWebReferenceCard({}, WEB_DEEP_RESEARCH_CARD_OPTIONS);
        deepResearchCard.classList.remove('collapsed');
        webDeepResearchList.appendChild(deepResearchCard);
        refreshWebDeepResearchEmptyState();
    });

    saveButton.addEventListener('click', function () {
        if (activeEditor === 'rules') {
            saveRules();
            return;
        }

        if (activeEditor === 'embeddings') {
            saveEmbeddings();
            return;
        }

        if (activeEditor === 'rerank') {
            saveRerank();
            return;
        }

        if (activeEditor === 'images') {
            saveImages();
            return;
        }

        if (activeEditor === 'audio') {
            saveAudio();
            return;
        }

        if (activeEditor === 'web') {
            saveWeb();
            return;
        }

        if (activeEditor === 'providers') {
            saveProviders();
            return;
        }

        renderMessage('error', 'No active editor selected.');
    });

    updateControlsVisibility();
    void initializeOpenRouterFreeTabAvailability();
    void loadRulesEditor();
});
