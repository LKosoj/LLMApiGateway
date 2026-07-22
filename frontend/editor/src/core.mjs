export function createEditorElements() {
const messageArea = document.getElementById('messageArea');
const rawDetailElement = document.getElementById('messageRawDetail');
const saveButton = document.getElementById('saveButton');
const conflictState = document.getElementById('editorConflictState');
const reloadEditorDocumentButton = document.getElementById('reloadEditorDocumentButton');
const addRuleButton = document.getElementById('addRuleButton');
const previewRulesButton = document.getElementById('previewRulesButton');
const suggestEvalOrderButton = document.getElementById('suggestEvalOrderButton');
const rulesPreviewArea = document.getElementById('rulesPreviewArea');
const rulesList = document.getElementById('rulesList');
const rulesEmptyState = document.getElementById('rulesEmptyState');

// The top tab bar was removed; the sidebar entity item is the only surface
// that still needs an availability toggle (fallback.mjs sets `.hidden` on it
// once OpenRouter Free is known to be configured or not).
const tabOpenRouterFree = document.querySelector('[data-entity-target="openrouter-free"]');
const editorContainerRules = document.getElementById('editor-container-rules');
const editorContainerEmbeddings = document.getElementById('editor-container-embeddings');
const editorContainerRerank = document.getElementById('editor-container-rerank');
const editorContainerImages = document.getElementById('editor-container-images');
const editorContainerAudio = document.getElementById('editor-container-audio');
const editorContainerWeb = document.getElementById('editor-container-web');
const editorContainerOpenRouterFree = document.getElementById('editor-container-openrouter-free');
const editorContainerFallbackEval = document.getElementById('editor-container-fallback-eval');
const editorContainerProviders = document.getElementById('editor-container-providers');
const editorContainerModelRules = document.getElementById('editor-container-model-rules');
const editorContainerFusion = document.getElementById('editor-container-fusion');
const addFusionButton = document.getElementById('addFusionButton');
const fusionList = document.getElementById('fusionList');
const fusionEmptyState = document.getElementById('fusionEmptyState');
const editorContainerRouter = document.getElementById('editor-container-router');
const addRouterButton = document.getElementById('addRouterButton');
const routerList = document.getElementById('routerList');
const routerEmptyState = document.getElementById('routerEmptyState');
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
const runOpenRouterFreeEvalButton = document.getElementById('runOpenRouterFreeEvalButton');
const runFallbackEvalButton = document.getElementById('runFallbackEvalButton');
const fallbackEvalStatus = document.getElementById('fallbackEvalStatus');
const fallbackEvalModels = document.getElementById('fallbackEvalModels');
const fallbackEvalEmptyState = document.getElementById('fallbackEvalEmptyState');
const modelRulesRawInput = document.getElementById('modelRulesRawInput');

    return {
        messageArea,
        rawDetailElement,
        saveButton,
        conflictState,
        reloadEditorDocumentButton,
        addRuleButton,
        previewRulesButton,
        suggestEvalOrderButton,
        rulesPreviewArea,
        rulesList,
        rulesEmptyState,
        tabOpenRouterFree,
        editorContainerRules,
        editorContainerEmbeddings,
        editorContainerRerank,
        editorContainerImages,
        editorContainerAudio,
        editorContainerWeb,
        editorContainerOpenRouterFree,
        editorContainerFallbackEval,
        editorContainerProviders,
        editorContainerModelRules,
        editorContainerFusion,
        addFusionButton,
        fusionList,
        fusionEmptyState,
        editorContainerRouter,
        addRouterButton,
        routerList,
        routerEmptyState,
        addProviderButton,
        providersList,
        providersEmptyState,
        addEmbeddingButton,
        embeddingsList,
        embeddingsEmptyState,
        addRerankButton,
        rerankList,
        rerankEmptyState,
        addImageGenerationButton,
        imageGenerationList,
        imageGenerationEmptyState,
        addImageEditButton,
        imageEditList,
        imageEditEmptyState,
        addAudioSpeechButton,
        audioSpeechList,
        audioSpeechEmptyState,
        addAudioTranscriptionButton,
        audioTranscriptionsList,
        audioTranscriptionsEmptyState,
        addWebSearchButton,
        webSearchList,
        webSearchEmptyState,
        addWebReadButton,
        webReadList,
        webReadEmptyState,
        addWebResearchButton,
        webResearchList,
        webResearchEmptyState,
        addWebDeepResearchButton,
        webDeepResearchList,
        webDeepResearchEmptyState,
        openRouterFreeStatus,
        openRouterFreeModels,
        openRouterFreeEmptyState,
        runOpenRouterFreeEvalButton,
        runFallbackEvalButton,
        fallbackEvalStatus,
        fallbackEvalModels,
        fallbackEvalEmptyState,
        modelRulesRawInput,
    };
}

export function registerCore(ctx) {
    const { gatewayI18n } = ctx;

    function t(key, values = {}) {
        return gatewayI18n.t(key, values);
    }

    function renderLocalizedBinding(binding) {
        if (!binding.element.isConnected && binding.wasConnected) {
            ctx.state.localizedBindings.delete(binding);
            return;
        }
        binding.wasConnected = binding.element.isConnected;
        if (typeof binding.render === 'function') {
            binding.element.textContent = binding.render();
            return;
        }
        const values = typeof binding.getValues === 'function'
            ? binding.getValues()
            : (binding.getValues || {});
        const key = typeof binding.key === 'function' ? binding.key() : binding.key;
        const value = t(key, values);
        if (binding.attribute) {
            binding.element.setAttribute(binding.attribute, value);
        } else {
            binding.element.textContent = value;
        }
    }

    function bindLocalizedText(element, key, getValues) {
        const binding = { element, key, getValues };
        ctx.state.localizedBindings.add(binding);
        renderLocalizedBinding(binding);
        return element;
    }

    function bindLocalizedAttribute(element, attribute, key, getValues) {
        const binding = { element, attribute, key, getValues };
        ctx.state.localizedBindings.add(binding);
        renderLocalizedBinding(binding);
        return element;
    }

    function bindLocalizedValue(element, render) {
        const binding = { element, render };
        ctx.state.localizedBindings.add(binding);
        renderLocalizedBinding(binding);
        return element;
    }

    function setRawDetail(rawDetail) {
        const detail = boundedSafeText(rawDetail);
        ctx.elements.rawDetailElement.textContent = detail;
        ctx.elements.rawDetailElement.hidden = !detail;
        ctx.elements.rawDetailElement.setAttribute('lang', 'und');
        ctx.elements.rawDetailElement.setAttribute('dir', 'auto');
    }

    const CONFIG_NAME_KEYS = new Map([
        ['Fallback Rules', 'editor:tabs.rules'],
        ['Embeddings Routes', 'editor:sections.embeddings.title'],
        ['Rerank Routes', 'editor:sections.rerank.title'],
        ['Images Routes', 'editor:messages.imagesName'],
        ['Audio Routes', 'editor:messages.audioName'],
        ['Web Services', 'editor:messages.webName'],
        ['Fusion Models', 'editor:sections.fusion.title'],
        ['Router Models', 'editor:sections.router.title'],
        ['Providers', 'editor:sections.providers.title'],
        ['Model Rules', 'editor:sections.modelRules.title'],
    ]);

    const EXACT_MESSAGE_KEYS = new Map([
        ['A newer local edit was preserved. Reload again to discard it.', 'editor:messages.newerEdit'],
        ['No changes detected.', 'editor:messages.noChanges'],
        ['The configuration is invalid.', 'editor:errors.genericInvalid'],
        ['Cannot save Providers: provider configuration has not loaded successfully.', 'editor:errors.providerNotReady'],
        ['Cannot add Provider: provider configuration has not loaded successfully.', 'editor:errors.providerNotReady'],
        ['No active editor selected.', 'editor:errors.noActiveEditor'],
        ['OpenRouter free model ranking loaded.', 'editor:eval.loadedOpenrouter'],
        ['Loading OpenRouter free model ranking...', 'editor:eval.loadingOpenrouter'],
        ['Starting OpenRouter free model full eval...', 'editor:eval.startingOpenrouter'],
        ['OpenRouter free model full eval started.', 'editor:eval.startedOpenrouter'],
        ['Fallback model eval status loaded.', 'editor:eval.loadedFallback'],
        ['Loading fallback model eval status...', 'editor:eval.loadingFallback'],
        ['Starting fallback model eval...', 'editor:eval.startingFallback'],
        ['Fallback model eval started.', 'editor:eval.startedFallback'],
        ['Model Rules saved successfully.', 'editor:messages.modelRulesSaved'],
    ]);

    function localizedMessageDescriptor(message, suppliedValues = {}) {
        if (typeof message === 'string' && message.startsWith('editor:')) {
            return { key: message, values: suppliedValues, rawDetail: '' };
        }
        const exactKey = EXACT_MESSAGE_KEYS.get(message);
        if (exactKey) {
            return { key: exactKey, values: {}, rawDetail: '' };
        }
        for (const [pattern, key] of [
            [/^Loading (.+)\.\.\.$/, 'editor:messages.loading'],
            [/^Saving (.+)\.\.\.$/, 'editor:messages.saving'],
            [/^(.+) loaded successfully\.$/, 'editor:messages.loaded'],
            [/^(.+) updated successfully\.$/, 'editor:messages.saved'],
        ]) {
            const match = typeof message === 'string' ? message.match(pattern) : null;
            const nameKey = match ? CONFIG_NAME_KEYS.get(match[1]) : null;
            if (nameKey) {
                return {
                    key,
                    values: () => ({name: t(nameKey)}),
                    rawDetail: '',
                };
            }
        }
        const warningPrefix = 'Fallback Rules loaded with warnings. ';
        if (typeof message === 'string' && message.startsWith(warningPrefix)) {
            return {
                key: 'editor:messages.loadedWithWarnings',
                values: {details: message.slice(warningPrefix.length)},
                rawDetail: '',
            };
        }
        const panelLimit = typeof message === 'string'
            ? message.match(/^A Fusion panel can have at most (\d+) models\.$/)
            : null;
        if (panelLimit) {
            return {
                key: 'editor:errors.panelLimit',
                values: {count: Number(panelLimit[1])},
                rawDetail: '',
            };
        }
        const reserveLimit = typeof message === 'string'
            ? message.match(/^A Fusion reserve can have at most (\d+) models\.$/)
            : null;
        if (reserveLimit) {
            return {
                key: 'editor:errors.reserveLimit',
                values: {count: Number(reserveLimit[1])},
                rawDetail: '',
            };
        }
        return {
            key: typeForUnknownMessage(message),
            values: {},
            rawDetail: typeof message === 'string' ? message : '',
        };
    }

    function typeForUnknownMessage(message) {
        return typeof message === 'string' && message.includes('invalid')
            ? 'editor:errors.genericInvalid'
            : 'editor:errors.genericRequest';
    }

    function resolveDescriptorValues(descriptor) {
        return typeof descriptor.values === 'function'
            ? descriptor.values()
            : descriptor.values;
    }

    function showLocalizedMessage(type, message, values = {}, rawDetail = '') {
        const descriptor = localizedMessageDescriptor(message, values);
        if (rawDetail) {
            descriptor.rawDetail = rawDetail;
        }
        ctx.state.currentMessage = { type, ...descriptor };
        ctx.elements.messageArea.className = type;
        ctx.elements.messageArea.textContent = t(descriptor.key, resolveDescriptorValues(descriptor));
        setRawDetail(descriptor.rawDetail);
        // The notice bar is sticky under the top nav, so it's already in
        // view — but if the user is deep in a long tab, scrollIntoView makes
        // sure the message lands at the top of the viewport instead of being
        // pinned only after the next scroll gesture.
        if (type === 'error' || type === 'warning') {
            ctx.elements.messageArea.scrollIntoView({ block: 'nearest' });
        }
    }

    function rerenderLocale() {
        Array.from(ctx.state.localizedBindings).forEach(renderLocalizedBinding);
        if (ctx.state.currentMessage) {
            ctx.elements.messageArea.textContent = t(
                ctx.state.currentMessage.key,
                resolveDescriptorValues(ctx.state.currentMessage),
            );
        }
        ctx.rerenderProviderCatalogStatuses();
        updateControlsVisibility();
    }

    function boundedSafeText(value) {
        if (typeof value !== 'string') {
            return '';
        }
        const normalized = value.replace(/[\u0000-\u001F\u007F]/g, ' ').trim();
        if (normalized.length <= ctx.constants.MAX_SAFE_ERROR_LENGTH) {
            return normalized;
        }
        return `${normalized.slice(0, ctx.constants.MAX_SAFE_ERROR_LENGTH - 1)}…`;
    }

    function ruleValidationMessages(detail) {
        // rule_validation and preflight both carry self-contained sentences —
        // the loader names which gateway model rejected which target, and the
        // preflight names the (provider, model) pair that /models did not list.
        // Every other entry needs its loc path to make sense, so it stays
        // behind detail.message.
        return Array.isArray(detail.errors)
            ? detail.errors
                .filter((error) => error
                    && typeof error === 'object'
                    && (error.type === 'rule_validation' || error.type === 'preflight')
                    && typeof error.msg === 'string')
                .map((error) => error.msg)
            : [];
    }

    function safeResponseError(response, body) {
        const detail = body
            && typeof body === 'object'
            && body.detail
            && typeof body.detail === 'object'
            ? body.detail
            : null;
        const parts = detail
            ? [
                typeof detail.message === 'string' ? detail.message : '',
                ...ruleValidationMessages(detail),
            ]
            : [];
        const detailMessage = boundedSafeText(parts.filter(Boolean).join(' '));
        return detailMessage || t('editor:errors.requestFailed', {status: response.status});
    }

    function safeClientError(error) {
        return error instanceof ConfigUiError
            ? error.message
            : t('editor:errors.genericRequest');
    }

    function safeSuccessMessage(body, fallback) {
        return fallback;
    }

    class ConfigUiError extends Error {}

    class LocalizedUiError extends ConfigUiError {
        constructor(key, values = {}) {
            super(key);
            this.key = key;
            this.values = values;
        }
    }

    const KNOWN_CONFIG_ERROR_KEYS = new Map([
        ['The configuration response is not valid JSON.', 'editor:errors.invalidJson'],
        ['The configuration response has an invalid shape.', 'editor:errors.invalidShape'],
        ['The configuration response omitted a valid revision.', 'editor:errors.invalidRevision'],
        ['Reload this configuration before saving.', 'editor:errors.reloadBeforeSave'],
    ]);

    function showClientValidationError(error) {
        if (error instanceof LocalizedUiError) {
            showLocalizedMessage('error', error.key, error.values);
            return;
        }
        showLocalizedMessage(
            'error',
            'editor:errors.genericInvalid',
            {},
            boundedSafeText(error && error.message),
        );
    }

    function cloneConfigPayload(payload) {
        return typeof payload === 'string'
            ? payload
            : JSON.parse(JSON.stringify(payload));
    }

    function requireStrongEtag(response) {
        const etag = response.headers.get('ETag');
        if (typeof etag !== 'string' || !ctx.constants.STRONG_ETAG_PATTERN.test(etag)) {
            throw new ConfigUiError('The configuration response omitted a valid revision.');
        }
        return etag;
    }

    function commitDocumentBase(documentName, payload, etag) {
        if (!ctx.state.documentBases.has(documentName)) {
            throw new TypeError(`Unknown configuration document: ${documentName}`);
        }
        if (!ctx.constants.STRONG_ETAG_PATTERN.test(etag)) {
            throw new ConfigUiError('The configuration response omitted a valid revision.');
        }
        ctx.state.documentBases.set(documentName, {
            payload: cloneConfigPayload(payload),
            etag,
        });
    }

    function getDocumentBase(documentName) {
        const base = ctx.state.documentBases.get(documentName);
        if (!base) {
            throw new ConfigUiError('Reload this configuration before saving.');
        }
        return {
            payload: cloneConfigPayload(base.payload),
            etag: base.etag,
        };
    }

    function getOperationBasePayload() {
        return getDocumentBase('operation').payload;
    }

    function isRecord(value) {
        return value !== null && typeof value === 'object' && !Array.isArray(value);
    }

    function requireArrayProperty(payload, propertyName) {
        if (!isRecord(payload) || !Array.isArray(payload[propertyName])) {
            throw new ConfigUiError('The configuration response has an invalid shape.');
        }
    }

    function validateFallbackPayload(payload, includeProviders = true) {
        requireArrayProperty(payload, 'rules');
        if (includeProviders) {
            requireArrayProperty(payload, 'providers');
        }
        return payload;
    }

    function validateOperationPayload(payload) {
        if (!isRecord(payload)) {
            throw new ConfigUiError('The configuration response has an invalid shape.');
        }
        ['embeddings', 'rerank', 'images_generations', 'images_edits'].forEach(sectionName => {
            requireArrayProperty(payload, sectionName);
        });
        return ctx.normalizeOperationRulesPayload(payload);
    }

    function validateFusionPayload(payload, includeProviders = false) {
        requireArrayProperty(payload, 'rules');
        if (includeProviders) {
            requireArrayProperty(payload, 'providers');
        }
        return payload;
    }

    function validateRouterPayload(payload) {
        requireArrayProperty(payload, 'rules');
        if (Object.prototype.hasOwnProperty.call(payload, 'chat_models')) {
            requireArrayProperty(payload, 'chat_models');
        }
        if (
            Object.prototype.hasOwnProperty.call(payload, 'fallback_chains')
            && !isRecord(payload.fallback_chains)
        ) {
            throw new ConfigUiError('The configuration response has an invalid shape.');
        }
        return payload;
    }

    function validateProvidersPayload(payload) {
        requireArrayProperty(payload, 'providers');
        return payload;
    }

    async function readJsonBody(response) {
        try {
            return await response.json();
        } catch (error) {
            throw new ConfigUiError(
                'The configuration response is not valid JSON.',
                { cause: error }
            );
        }
    }

    function showConflict(documentName) {
        ctx.elements.conflictState.dataset.document = documentName;
        ctx.elements.conflictState.hidden = false;
        ctx.elements.conflictState.focus();
    }

    function clearConflict() {
        ctx.elements.conflictState.hidden = true;
        delete ctx.elements.conflictState.dataset.document;
    }

    function currentDocumentName() {
        if (['embeddings', 'rerank', 'images', 'audio', 'web'].includes(ctx.state.activeEditor)) {
            return 'operation';
        }
        return {
            rules: 'fallback',
            fusion: 'fusion',
            router: 'router',
            providers: 'providers',
            'model-rules': 'model',
        }[ctx.state.activeEditor] || null;
    }

    function isInteractionLocked() {
        return ctx.state.saveInFlight || ctx.state.busyDocuments.size > 0;
    }

    function syncInteractionLock() {
        if (isInteractionLocked()) {
            const controls = document.querySelectorAll(
                '#reloadEditorDocumentButton, .editor-entity-item'
            );
            controls.forEach(control => {
                if (!ctx.state.lockedControls.has(control)) {
                    ctx.state.lockedControls.set(control, control.disabled);
                }
                control.disabled = true;
            });

            document.querySelectorAll('.editor-tab-content.active').forEach(subtree => {
                if (!ctx.state.lockedSubtrees.has(subtree)) {
                    ctx.state.lockedSubtrees.set(subtree, subtree.inert);
                }
                subtree.inert = true;
                subtree.querySelectorAll('[draggable]').forEach(row => {
                    if (!ctx.state.lockedDraggables.has(row)) {
                        ctx.state.lockedDraggables.set(row, row.draggable);
                    }
                    row.draggable = false;
                });
            });
            return;
        }

        ctx.state.lockedControls.forEach((wasDisabled, control) => {
            if (control.isConnected) {
                control.disabled = wasDisabled;
            }
        });
        ctx.state.lockedControls.clear();
        ctx.state.lockedDraggables.forEach((wasDraggable, row) => {
            if (row.isConnected) {
                row.draggable = wasDraggable;
            }
        });
        ctx.state.lockedDraggables.clear();
        ctx.state.lockedSubtrees.forEach((wasInert, subtree) => {
            if (subtree.isConnected) {
                subtree.inert = wasInert;
            }
        });
        ctx.state.lockedSubtrees.clear();
    }

    function setDocumentBusy(documentName, busy) {
        if (busy) {
            ctx.state.busyDocuments.add(documentName);
        } else {
            ctx.state.busyDocuments.delete(documentName);
        }
        updateSaveButtonDisabledState();
        syncInteractionLock();
    }

    async function loadConfigDocument(documentName, url, options) {
        ctx.invalidateProviderCatalogRows();
        const requestId = (ctx.state.loadRequestIds.get(documentName) || 0) + 1;
        const requestMutationVersion = ctx.state.editorMutationVersion;
        ctx.state.loadRequestIds.set(documentName, requestId);
        setDocumentBusy(documentName, true);
        try {
            const response = await ctx.apiFetch(url);
            const payload = options.responseType === 'text'
                ? await response.text()
                : await readJsonBody(response);
            if (!response.ok) {
                throw new ConfigUiError(
                    options.responseType === 'text'
                        ? `Request failed (HTTP ${response.status}).`
                        : safeResponseError(response, payload)
                );
            }
            const etag = requireStrongEtag(response);
            const validatedPayload = options.validate(payload);
            if (
                ctx.state.loadRequestIds.get(documentName) !== requestId
                || ctx.state.editorMutationVersion !== requestMutationVersion
            ) {
                return false;
            }
            const application = options.apply(validatedPayload);
            syncInteractionLock();
            await application;
            if (
                ctx.state.loadRequestIds.get(documentName) !== requestId
                || ctx.state.editorMutationVersion !== requestMutationVersion
            ) {
                return false;
            }
            commitDocumentBase(documentName, validatedPayload, etag);
            clearConflict();
            return true;
        } catch (error) {
            if (ctx.state.loadRequestIds.get(documentName) === requestId) {
                ctx.state.documentBases.set(documentName, null);
            }
            throw error;
        } finally {
            if (ctx.state.loadRequestIds.get(documentName) === requestId) {
                setDocumentBusy(documentName, false);
            }
        }
    }

    async function saveConfigDocument(documentName, url, payload, options = {}) {
        ctx.invalidateProviderCatalogRows();
        let base;
        try {
            base = getDocumentBase(documentName);
        } catch (error) {
            showLocalizedError(options.errorTitle, error);
            return null;
        }

        const submittedMutationVersion = ctx.state.editorMutationVersion;
        const response = await ctx.apiFetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': options.contentType || 'application/json',
                'If-Match': base.etag,
            },
            body: options.body === undefined ? JSON.stringify(payload) : options.body,
        });
        const body = await readJsonBody(response).catch(() => ({}));
        if (response.status === 409) {
            showConflict(documentName);
            return null;
        }
        if (!response.ok) {
            showLocalizedError(options.errorTitle, safeResponseError(response, body));
            return null;
        }

        let etag;
        let publishedPayload;
        try {
            etag = requireStrongEtag(response);
            publishedPayload = options.extractPublishedPayload
                ? options.extractPublishedPayload(body, payload)
                : body;
            publishedPayload = options.validatePublished
                ? options.validatePublished(publishedPayload)
                : publishedPayload;
        } catch (error) {
            showLocalizedError(options.errorTitle, error);
            return null;
        }
        commitDocumentBase(documentName, publishedPayload, etag);
        clearConflict();
        return {
            body,
            payload: cloneConfigPayload(publishedPayload),
            submittedMutationVersion,
        };
    }

    function showLocalizedError(title, detail) {
        if (detail instanceof LocalizedUiError) {
            showLocalizedMessage('error', detail.key, detail.values);
            return;
        }
        if (detail instanceof ConfigUiError) {
            const key = KNOWN_CONFIG_ERROR_KEYS.get(detail.message);
            if (key) {
                showLocalizedMessage('error', key);
                return;
            }
            detail = safeClientError(detail);
        }
        const match = typeof title === 'string'
            ? title.match(/^Error (loading|saving) (.+):$/)
            : null;
        const nameKey = match ? CONFIG_NAME_KEYS.get(match[2]) : null;
        if (match && nameKey) {
            const key = match[1] === 'loading'
                ? 'editor:errors.loadTitle'
                : 'editor:errors.saveTitle';
            showLocalizedMessage(
                'error',
                key,
                () => ({name: t(nameKey)}),
                detail,
            );
            return;
        }
        showLocalizedMessage(
            'error',
            'editor:errors.genericRequest',
            {},
            [title, detail].filter(Boolean).join(' '),
        );
    }

    function clearElement(element) {
        while (element.firstChild) {
            element.removeChild(element.firstChild);
        }
    }

    function formatDateTime(value) {
        if (!value) {
            return t('editor:messages.notAvailable');
        }
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            return t('editor:messages.notAvailable');
        }
        return ctx.gatewayI18n.formatDate(date, {dateStyle: 'medium', timeStyle: 'medium'});
    }

    function formatNumber(value) {
        if (typeof value !== 'number' || !Number.isFinite(value)) {
            return t('editor:messages.notApplicable');
        }
        return ctx.gatewayI18n.formatNumber(value);
    }

    function updateControlsVisibility() {
        ctx.elements.saveButton.hidden = ctx.state.activeEditor === 'openrouter-free' || ctx.state.activeEditor === 'fallback-eval';
        const key = {
            rules: 'editor:actions.saveFallback',
            embeddings: 'editor:actions.saveEmbeddings',
            rerank: 'editor:actions.saveRerank',
            images: 'editor:actions.saveImages',
            audio: 'editor:actions.saveAudio',
            web: 'editor:actions.saveWeb',
            fusion: 'editor:actions.saveFusion',
            router: 'editor:actions.saveRouter',
            'model-rules': 'editor:actions.saveModelRules',
        }[ctx.state.activeEditor] || 'editor:actions.saveConfiguration';
        ctx.elements.saveButton.textContent = ctx.elements.saveButton.hidden ? '' : gatewayI18n.t(key);
        updateSaveButtonDisabledState();
        updateProvidersControlsState();
    }

    function isActiveEditorLoaded() {
        return {
            rules: ctx.state.originalRulesContent,
            embeddings: ctx.state.originalEmbeddingsContent,
            rerank: ctx.state.originalRerankContent,
            images: ctx.state.originalImagesContent,
            audio: ctx.state.originalAudioContent,
            web: ctx.state.originalWebContent,
            fusion: ctx.state.originalFusionContent,
            router: ctx.state.originalRouterContent,
            providers: ctx.state.originalProvidersContent,
            'model-rules': ctx.state.originalModelRulesContent,
        }[ctx.state.activeEditor] !== null;
    }

    function updateSaveButtonDisabledState() {
        const documentName = currentDocumentName();
        if (ctx.state.saveInFlight || ctx.state.busyDocuments.size > 0) {
            ctx.elements.saveButton.disabled = true;
        } else if (!documentName) {
            ctx.elements.saveButton.disabled = false;
        } else if (ctx.state.activeEditor === 'providers') {
            ctx.elements.saveButton.disabled = (
                ctx.state.providersLoadState !== 'ready'
                || !ctx.state.documentBases.get(documentName)
                || !isActiveEditorLoaded()
            );
        } else {
            ctx.elements.saveButton.disabled = !ctx.state.documentBases.get(documentName) || !isActiveEditorLoaded();
        }
        syncInteractionLock();
        updateDirtyIndicator();
    }

    function updateDirtyIndicator() {
        const dirty = isCurrentEditorDirty();
        ctx.elements.saveButton.setAttribute('data-editor-dirty', dirty ? 'true' : 'false');
    }

    function updateProvidersControlsState() {
        ctx.elements.addProviderButton.disabled = ctx.state.providersLoadState !== 'ready';
        updateSaveButtonDisabledState();
    }

    function setProvidersLoadState(state) {
        ctx.state.providersLoadState = state;
        updateProvidersControlsState();
    }

    function refreshRulesEmptyState() {
        ctx.elements.rulesEmptyState.hidden = ctx.elements.rulesList.children.length !== 0;
    }

    function refreshEmbeddingsEmptyState() {
        ctx.elements.embeddingsEmptyState.hidden = ctx.elements.embeddingsList.children.length !== 0;
    }

    function refreshRerankEmptyState() {
        ctx.elements.rerankEmptyState.hidden = ctx.elements.rerankList.children.length !== 0;
    }

    function refreshImageGenerationEmptyState() {
        ctx.elements.imageGenerationEmptyState.hidden = ctx.elements.imageGenerationList.children.length !== 0;
    }

    function refreshImageEditEmptyState() {
        ctx.elements.imageEditEmptyState.hidden = ctx.elements.imageEditList.children.length !== 0;
    }

    function refreshAudioSpeechEmptyState() {
        ctx.elements.audioSpeechEmptyState.hidden = ctx.elements.audioSpeechList.children.length !== 0;
    }

    function refreshAudioTranscriptionsEmptyState() {
        ctx.elements.audioTranscriptionsEmptyState.hidden = ctx.elements.audioTranscriptionsList.children.length !== 0;
    }

    function refreshWebSearchEmptyState() {
        ctx.elements.webSearchEmptyState.hidden = ctx.elements.webSearchList.children.length !== 0;
    }

    function refreshWebReadEmptyState() {
        ctx.elements.webReadEmptyState.hidden = ctx.elements.webReadList.children.length !== 0;
    }

    function refreshWebResearchEmptyState() {
        ctx.elements.webResearchEmptyState.hidden = ctx.elements.webResearchList.children.length !== 0;
    }

    function refreshWebDeepResearchEmptyState() {
        ctx.elements.webDeepResearchEmptyState.hidden = ctx.elements.webDeepResearchList.children.length !== 0;
    }

    function refreshProvidersEmptyState() {
        ctx.elements.providersEmptyState.hidden = ctx.elements.providersList.children.length !== 0;
    }

    function refreshFusionEmptyState() {
        ctx.elements.fusionEmptyState.hidden = ctx.elements.fusionList.children.length !== 0;
    }

    function refreshRouterEmptyState() {
        ctx.elements.routerEmptyState.hidden = ctx.elements.routerList.children.length !== 0;
    }

    const FIELD_LABEL_KEYS = new Map([
        ['Provider', 'editor:fields.provider'],
        ['Model', 'editor:fields.model'],
        ['Gateway Model Name', 'editor:fields.gatewayModelName'],
        ['Provider Order', 'editor:fields.providerOrder'],
        ['Upstream Key Pool', 'editor:fields.upstreamKeyPool'],
        ['Retry Delay', 'editor:fields.retryDelay'],
        ['Retry Count', 'editor:fields.retryCount'],
        ['Custom Body Params', 'editor:fields.customBodyParams'],
        ['Custom Headers', 'editor:fields.customHeaders'],
        ['Payload Transforms', 'editor:fields.payloadTransforms'],
        ['Target Path', 'editor:fields.targetPath'],
        ['Request Format', 'editor:fields.requestFormat'],
        ['Response Format', 'editor:fields.responseFormat'],
        ['Response Output Format', 'editor:fields.responseOutputFormat'],
        ['Request Mapping', 'editor:fields.requestMapping'],
        ['Response Mapping', 'editor:fields.responseMapping'],
        ['Temperature', 'editor:fields.temperature'],
        ['Max Completion Tokens', 'editor:fields.maxCompletionTokens'],
        ['Reasoning (JSON)', 'editor:fields.reasoningJson'],
        ['Search model (required)', 'editor:fields.searchModelRequired'],
        ['Read model (optional)', 'editor:fields.readModelOptional'],
        ['Max tool calls per panel model', 'editor:fields.maxToolCalls'],
        ['Max iterations per panel model', 'editor:fields.maxIterations'],
        ['Max results per search', 'editor:fields.maxResults'],
        ['Gateway Target', 'editor:fields.gatewayTarget'],
        ['Fallback Gateway', 'editor:fields.fallbackGateway'],
        ['Start At Entry', 'editor:fields.startAtEntry'],
        ['Target Type', 'editor:fields.targetType'],
        ['Selector Model', 'editor:fields.selectorModel'],
        ['Voices Target Path', 'editor:fields.voicesTargetPath'],
        ['Query Model (optional)', 'editor:fields.queryModelOptional'],
        ['Provider Name', 'editor:fields.providerName'],
        ['Base URL', 'editor:fields.baseUrl'],
        ['API Key', 'editor:fields.apiKey'],
        ['API Type', 'editor:fields.apiType'],
        ['Proxy (optional)', 'editor:fields.proxyOptional'],
        ['Models Metadata (JSON)', 'editor:fields.modelsMetadata'],
        ['Available Models (optional)', 'editor:fields.availableModels'],
        ['Routing Policy (JSON)', 'editor:fields.routingPolicy'],
        ['Upstream Key Pools (JSON)', 'editor:fields.upstreamKeyPools'],
        ['Cost per successful request (USD)', 'editor:fields.costPerRequest'],
        ['Max Total Attempts (chain budget)', 'editor:fields.maxAttempts'],
        ['Search Model', 'editor:fields.searchModel'],
        ['Read Model', 'editor:fields.readModel'],
        ['Rerank Model', 'editor:fields.rerankModel'],
        ['Analysis Model', 'editor:fields.analysisModel'],
        ['Fast LLM', 'editor:fields.fastModel'],
        ['Smart LLM', 'editor:fields.smartModel'],
        ['Strategic LLM', 'editor:fields.strategicModel'],
        ['Embedding Model', 'editor:fields.embeddingModel'],
        ['Image Generation Model', 'editor:fields.imageGenerationModel'],
        ['Image Generation Size', 'editor:fields.imageGenerationSize'],
        ['Custom body params', 'editor:fields.customBodyParams'],
        ['Custom headers', 'editor:fields.customHeaders'],
        ['Payload transforms', 'editor:fields.payloadTransforms'],
        ['Request mapping', 'editor:fields.requestMapping'],
        ['Response mapping', 'editor:fields.responseMapping'],
        ['Vision Support', 'editor:fields.supportsVision'],
        ['Tools Support', 'editor:fields.supportsTools'],
        ['Context Window (tokens)', 'editor:fields.contextWindow'],
    ]);

    const PLACEHOLDER_KEYS = new Map([
        ['Choose or enter model', 'editor:placeholders.chooseOrEnterModel'],
        ['Select a gateway model', 'editor:placeholders.chooseGatewayModel'],
        ['default', 'editor:placeholders.defaultValue'],
        ['Retry delay (seconds)', 'editor:placeholders.retryDelay'],
        ['Retry count', 'editor:placeholders.retryCount'],
        ['unlimited', 'editor:placeholders.unlimited'],
        ['Choose API type', 'editor:placeholders.chooseApiType'],
        ['Choose a provider', 'editor:placeholders.chooseProvider'],
        ['Choose a provider first', 'editor:placeholders.chooseProviderFirst'],
        ['Choose a model', 'editor:placeholders.chooseModel'],
        ['No models available', 'editor:placeholders.noModels'],
        ['Loading models...', 'editor:placeholders.loadingModels'],
        ['Loading models…', 'editor:placeholders.loadingModels'],
        ['Select fallback entry', 'editor:placeholders.selectFallbackEntry'],
        ['e.g. 128000', 'editor:placeholders.contextWindow'],
    ]);

    const ACTION_TEXT_KEYS = new Map([
        ['Remove Fallback', 'editor:actions.removeFallback'],
        ['Disable Special Fallback', 'editor:actions.removeFallback'],
        ['Remove Rule', 'editor:actions.removeRule'],
        ['Remove Model', 'editor:actions.removeModel'],
        ['Remove Fallback Route', 'editor:actions.removeRoute'],
        ['Remove Route', 'editor:actions.removeRoute'],
        ['Remove Service', 'editor:actions.removeRoute'],
        ['Remove Target', 'editor:actions.removeTarget'],
        ['Remove Provider', 'editor:actions.removeProvider'],
        ['Remove Panel Model', 'editor:actions.removePanelModel'],
        ['Remove Reserve Model', 'editor:actions.removeReserveModel'],
        ['Add Fallback Model', 'editor:actions.addFallbackModel'],
        ['Add Fallback Route', 'editor:actions.addFallbackRoute'],
        ['Add Route', 'editor:actions.addRoute'],
        ['Add Target', 'editor:actions.addTarget'],
        ['Add Panel Model', 'editor:actions.addPanelModel'],
        ['Add Reserve Model', 'editor:actions.addReserveModel'],
        ['Add Model', 'editor:actions.addModel'],
        ['Remove', 'editor:actions.remove'],
    ]);

    function bindKnownActionText(element, text) {
        const key = ACTION_TEXT_KEYS.get(text);
        if (key) {
            bindLocalizedText(element, key);
        } else {
            element.textContent = text;
        }
        return element;
    }

    function bindKnownText(element, text) {
        const key = FIELD_LABEL_KEYS.get(text);
        if (key) {
            bindLocalizedText(element, key);
        } else {
            element.textContent = text;
        }
        return element;
    }

    function bindKnownPlaceholder(element, placeholder) {
        const key = PLACEHOLDER_KEYS.get(placeholder);
        if (key) {
            bindLocalizedAttribute(element, 'placeholder', key);
        } else {
            element.placeholder = placeholder;
        }
        return element;
    }

    function createFieldGroup(labelText, inputElement, className) {
        const group = document.createElement('label');
        group.className = `field-group ${className || ''}`.trim();
        const label = document.createElement('span');
        label.className = 'field-label';
        const labelTextElement = document.createElement('span');
        labelTextElement.className = 'field-label-text';
        bindKnownText(labelTextElement, labelText);
        label.appendChild(labelTextElement);
        group.appendChild(label);
        group.appendChild(inputElement);
        return group;
    }

    function createTextInput(className, placeholder) {
        const input = document.createElement('input');
        input.type = 'text';
        input.className = className;
        bindKnownPlaceholder(input, placeholder);
        return input;
    }

    function createNumberInput(className, placeholder) {
        const input = document.createElement('input');
        input.type = 'number';
        input.min = '0';
        input.step = '1';
        input.className = className;
        bindKnownPlaceholder(input, placeholder);
        return input;
    }

    function createOperationCostCalculatorField(initialData = {}) {
        const rateInput = createNumberInput('cost-calculator-rate-input', '0.1');
        rateInput.step = 'any';
        const configuredRate = initialData.cost_calculator?.rate_usd;
        if (configuredRate !== undefined && configuredRate !== null) {
            rateInput.value = String(configuredRate);
        }
        const field = createFieldGroup(
            'Cost per successful request (USD)',
            rateInput,
            'cost-calculator-field'
        );
        ctx.appendFieldHint(
            field,
            'editor:hints.cost',
        );
        return field;
    }

    function applyOperationCostCalculator(payload, ruleCard) {
        const rateInput = ruleCard.querySelector('.cost-calculator-rate-input');
        const rawRate = rateInput?.value.trim() || '';
        if (!rawRate) {
            return payload;
        }
        const rateUsd = Number(rawRate);
        if (!Number.isFinite(rateUsd) || rateUsd < 0) {
            throw new LocalizedUiError('editor:errors.costRate');
        }
        payload.cost_calculator = {
            unit: 'operation',
            rate_usd: rateUsd,
        };
        return payload;
    }

    function createTextarea(className, placeholder) {
        const textarea = document.createElement('textarea');
        textarea.className = className;
        bindKnownPlaceholder(textarea, placeholder);
        return textarea;
    }

    function createSelect(className) {
        const select = document.createElement('select');
        select.className = className;
        return select;
    }

    function createTriStateSelect(className) {
        const select = createSelect(className);
        const unknownOption = document.createElement('option');
        unknownOption.value = '';
        bindLocalizedText(unknownOption, 'editor:capability.unknown');
        select.appendChild(unknownOption);

        const supportedOption = document.createElement('option');
        supportedOption.value = 'true';
        bindLocalizedText(supportedOption, 'editor:capability.supported');
        select.appendChild(supportedOption);

        const unsupportedOption = document.createElement('option');
        unsupportedOption.value = 'false';
        bindLocalizedText(unsupportedOption, 'editor:capability.unsupported');
        select.appendChild(unsupportedOption);

        return select;
    }

    function capabilityAutofillSourceFor(status, gatewayModelName, index, fieldName) {
        const resolutions = status && typeof status === 'object' ? status.resolutions : null;
        const entries = resolutions ? resolutions[gatewayModelName] : null;
        if (!Array.isArray(entries)) {
            return null;
        }
        const entry = entries.find(candidate => candidate && candidate.index === index);
        const source = entry && entry.fields ? entry.fields[fieldName]?.source : null;
        return source === 'provider' || source === 'openrouter' ? source : null;
    }

    // Wraps a capability control (tri-state select or number input) that may be
    // owned by F-auto (capabilities_autofilled). When locked, the control is
    // hidden (its value stays authoritative and round-trips unchanged) and a
    // read-only badge is shown instead; the badge's "Edit" button unlocks the
    // control in place, which is how a manual edit permanently disowns the
    // field (see applyCapabilityFieldsToPayload).
    function wrapCapabilityField({ fieldName, control, kind, locked, source }) {
        const wrapper = document.createElement('div');
        wrapper.className = 'capability-field-slot';
        control.dataset.capabilityLocked = locked ? 'true' : 'false';
        wrapper.appendChild(control);

        if (!locked) {
            return wrapper;
        }

        control.hidden = true;

        const badge = document.createElement('span');
        badge.className = 'capability-badge';
        badge.dataset.capabilityField = fieldName;

        const valueText = document.createElement('span');
        valueText.className = 'capability-badge-value';
        if (kind === 'boolean') {
            bindLocalizedText(
                valueText,
                control.value === 'true' ? 'editor:capability.supported' : 'editor:capability.unsupported',
            );
        } else {
            valueText.textContent = control.value;
        }
        badge.appendChild(valueText);

        const autoLabel = document.createElement('span');
        autoLabel.className = 'capability-badge-auto';
        bindLocalizedText(autoLabel, 'editor:capability.autofilled');
        badge.appendChild(autoLabel);

        if (source === 'provider' || source === 'openrouter') {
            const sourceLabel = document.createElement('span');
            sourceLabel.className = 'capability-badge-source';
            bindLocalizedText(sourceLabel, `editor:capability.source.${source}`);
            badge.appendChild(sourceLabel);
        }

        const editButton = document.createElement('button');
        editButton.type = 'button';
        editButton.className = 'capability-badge-edit secondary-button';
        bindLocalizedText(editButton, 'editor:capability.override');
        editButton.addEventListener('click', () => {
            control.dataset.capabilityLocked = 'false';
            control.hidden = false;
            badge.remove();
        });
        badge.appendChild(editButton);

        wrapper.appendChild(badge);
        return wrapper;
    }

    function sortProviderModelIds(modelIds) {
        return [...modelIds].sort((left, right) => {
            const comparison = ctx.gatewayI18n.getCollator({
                numeric: true,
                sensitivity: 'base',
            }).compare(left, right);
            if (comparison !== 0) {
                return comparison;
            }
            return left < right ? -1 : left > right ? 1 : 0;
        });
    }

    function setSelectOptions(select, options, placeholder, selectedValue) {
        select.textContent = '';
        const placeholderOption = document.createElement('option');
        placeholderOption.value = '';
        const placeholderKey = PLACEHOLDER_KEYS.get(placeholder);
        if (placeholderKey) {
            bindLocalizedText(placeholderOption, placeholderKey);
        } else {
            placeholderOption.textContent = placeholder;
        }
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
        const placeholderKey = PLACEHOLDER_KEYS.get(placeholder || 'Select a gateway model');
        if (placeholderKey) {
            bindLocalizedText(placeholderOption, placeholderKey);
        } else {
            placeholderOption.textContent = placeholder;
        }
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
            bindLocalizedText(
                staleOption,
                'editor:placeholders.notConfigured',
                () => ({value: currentValue}),
            );
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
            const fieldKey = FIELD_LABEL_KEYS.get(fieldLabel) || 'editor:fields.field';
            throw new LocalizedUiError(
                'editor:errors.fieldJson',
                () => ({field: t(fieldKey)}),
            );
        }

        if (!parsedValue || Array.isArray(parsedValue) || typeof parsedValue !== 'object') {
            const fieldKey = FIELD_LABEL_KEYS.get(fieldLabel) || 'editor:fields.field';
            throw new LocalizedUiError(
                'editor:errors.fieldJson',
                () => ({field: t(fieldKey)}),
            );
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

        const unknownProviders = providerNames.filter(providerName => !ctx.state.availableProviders.includes(providerName));
        if (unknownProviders.length > 0) {
            throw new LocalizedUiError(
                'editor:errors.unknownProviders',
                {providers: unknownProviders.join(', ')},
            );
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

    function applyCapabilityFieldsToPayload(payload, visionSelect, toolsSelect, contextWindowInput) {
        const autofilledFields = [];

        if (visionSelect.value !== '') {
            payload.supports_vision = visionSelect.value === 'true';
            if (visionSelect.dataset?.capabilityLocked === 'true') {
                autofilledFields.push('supports_vision');
            }
        }

        if (toolsSelect.value !== '') {
            payload.supports_tools = toolsSelect.value === 'true';
            if (toolsSelect.dataset?.capabilityLocked === 'true') {
                autofilledFields.push('supports_tools');
            }
        }

        if (contextWindowInput.value.trim() !== '') {
            payload.context_window = Number.parseInt(contextWindowInput.value, 10);
            if (contextWindowInput.dataset?.capabilityLocked === 'true') {
                autofilledFields.push('context_window');
            }
        }

        // A field stays in the list only while its control is still locked
        // (untouched autofill); clicking the badge's "Edit" button clears the
        // dataset flag, which permanently drops the field here on save.
        if (autofilledFields.length > 0) {
            payload.capabilities_autofilled = autofilledFields;
        }
    }

    function setupRowReordering(row) {
        let dragOriginParent = null;
        let dragOriginIndex = -1;
        row.draggable = true;
        if (isInteractionLocked()) {
            ctx.state.lockedDraggables.set(row, true);
            row.draggable = false;
        }

        row.addEventListener('dragstart', (e) => {
            if (isInteractionLocked()) {
                e.preventDefault();
                e.stopPropagation();
                window._draggedRow = null;
                return;
            }
            if (['input', 'textarea', 'select', 'button'].includes(e.target.tagName.toLowerCase())) {
                e.preventDefault();
                return;
            }
            dragOriginParent = row.parentNode;
            dragOriginIndex = Array.from(dragOriginParent.children).indexOf(row);
            row.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
            setTimeout(() => {
                if (window._draggedRow === row) {
                    row.style.opacity = '0.5';
                }
            }, 0);
            window._draggedRow = row;
        });

        row.addEventListener('dragend', () => {
            const currentParent = row.parentNode;
            const currentIndex = currentParent
                ? Array.from(currentParent.children).indexOf(row)
                : -1;
            const orderChanged = dragOriginParent !== null && (
                currentParent !== dragOriginParent || currentIndex !== dragOriginIndex
            );
            row.classList.remove('dragging');
            row.style.opacity = '';
            window._draggedRow = null;
            dragOriginParent = null;
            dragOriginIndex = -1;
            if (orderChanged) {
                ctx.state.editorMutationVersion += 1;
                updateDirtyIndicator();
            }
        });

        row.addEventListener('dragover', (e) => {
            if (isInteractionLocked()) {
                e.preventDefault();
                e.stopPropagation();
                return;
            }
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
        bindLocalizedAttribute(moveUpButton, 'title', 'editor:actions.moveUp');
        moveUpButton.addEventListener('click', () => {
            if (row.previousElementSibling) {
                row.parentNode.insertBefore(row, row.previousElementSibling);
            }
        });

        const moveDownButton = document.createElement('button');
        moveDownButton.type = 'button';
        moveDownButton.className = 'icon-button move-down-button';
        moveDownButton.textContent = '↓';
        bindLocalizedAttribute(moveDownButton, 'title', 'editor:actions.moveDown');
        moveDownButton.addEventListener('click', () => {
            if (row.nextElementSibling) {
                row.parentNode.insertBefore(row.nextElementSibling, row);
            }
        });

        return { moveUpButton, moveDownButton };
    }

    function isCurrentEditorDirty() {
        if (ctx.state.activeEditor === 'rules' && ctx.state.originalRulesContent !== null) {
            try {
                return ctx.getRulesSnapshotContent() !== ctx.state.originalRulesContent;
            } catch (error) {
                return true;
            }
        }
        if (ctx.state.activeEditor === 'embeddings' && ctx.state.originalEmbeddingsContent !== null) {
            try {
                return ctx.getNormalizedEmbeddingsContent() !== ctx.state.originalEmbeddingsContent;
            } catch (error) {
                return true;
            }
        }
        if (ctx.state.activeEditor === 'rerank' && ctx.state.originalRerankContent !== null) {
            try {
                return ctx.getNormalizedRerankContent() !== ctx.state.originalRerankContent;
            } catch (error) {
                return true;
            }
        }
        if (ctx.state.activeEditor === 'images' && ctx.state.originalImagesContent !== null) {
            try {
                return ctx.getNormalizedImagesContent() !== ctx.state.originalImagesContent;
            } catch (error) {
                return true;
            }
        }
        if (ctx.state.activeEditor === 'audio' && ctx.state.originalAudioContent !== null) {
            try {
                return ctx.getNormalizedAudioContent() !== ctx.state.originalAudioContent;
            } catch (error) {
                return true;
            }
        }
        if (ctx.state.activeEditor === 'web' && ctx.state.originalWebContent !== null) {
            try {
                return ctx.getNormalizedWebContent() !== ctx.state.originalWebContent;
            } catch (error) {
                return true;
            }
        }
        if (ctx.state.activeEditor === 'providers' && ctx.state.originalProvidersContent !== null) {
            try {
                return ctx.getProvidersSnapshotContent() !== ctx.state.originalProvidersContent;
            } catch (error) {
                return true;
            }
        }
        if (ctx.state.activeEditor === 'fusion' && ctx.state.originalFusionContent !== null) {
            try {
                return ctx.getNormalizedFusionContent() !== ctx.state.originalFusionContent;
            } catch (error) {
                return true;
            }
        }
        if (ctx.state.activeEditor === 'router' && ctx.state.originalRouterContent !== null) {
            try {
                return ctx.getNormalizedRouterContent() !== ctx.state.originalRouterContent;
            } catch (error) {
                return true;
            }
        }
        if (ctx.state.activeEditor === 'model-rules' && ctx.state.originalModelRulesContent !== null) {
            return ctx.elements.modelRulesRawInput.value !== ctx.state.originalModelRulesContent;
        }
        return false;
    }

    function beforeRulesTabActivate(context) {
        if (
            context.reason !== 'repair'
            && context.previousKey !== context.key
            && isCurrentEditorDirty()
        ) {
            return confirm(gatewayI18n.t('editor:errors.unsavedConfirm'));
        }
        return true;
    }

    function shouldReloadSelectedRulesTab(tabName) {
        if (tabName === 'providers' && ctx.state.providersLoadState === 'loading') {
            return false;
        }
        if (tabName === 'providers' && ctx.state.providersLoadState === 'error') {
            return true;
        }
        return !(
            ctx.state.originalRulesContent !== null
            || ctx.state.originalEmbeddingsContent !== null
            || ctx.state.originalRerankContent !== null
            || ctx.state.originalImagesContent !== null
            || ctx.state.originalAudioContent !== null
            || ctx.state.originalWebContent !== null
            || ctx.state.originalProvidersContent !== null
            || ctx.state.originalFusionContent !== null
            || ctx.state.originalRouterContent !== null
            || ctx.state.originalModelRulesContent !== null
        );
    }

    function activateRulesTab(context) {
        const tabName = context.key;
        const previousEditor = context.previousKey || ctx.state.activeEditor;
        if (previousEditor !== tabName) {
            if (previousEditor === 'openrouter-free') {
                ctx.stopOpenRouterFreePolling();
            } else if (previousEditor === 'fallback-eval') {
                ctx.stopFallbackEvalPolling();
            }
        }

        ctx.state.activeEditor = tabName;
        ctx.state.activeRulesTabContext = context;
        // The sidebar (registerEntityPanel) owns the active-entity highlight
        // and footer label; it no longer observes a top tab bar, so it must
        // be told explicitly whenever the active editor changes.
        ctx.renderActiveEntity?.();
        updateControlsVisibility();
        ctx.elements.editorContainerOpenRouterFree.classList.remove('active');
        ctx.elements.editorContainerOpenRouterFree.style.display = 'none';
        ctx.elements.editorContainerFallbackEval.classList.remove('active');
        ctx.elements.editorContainerFallbackEval.style.display = 'none';
        ctx.elements.editorContainerFusion.classList.remove('active');
        ctx.elements.editorContainerFusion.style.display = 'none';
        ctx.elements.editorContainerRouter.classList.remove('active');
        ctx.elements.editorContainerRouter.style.display = 'none';
        ctx.elements.editorContainerModelRules.classList.remove('active');
        ctx.elements.editorContainerModelRules.style.display = 'none';

        if (tabName === 'rules') {
            ctx.elements.editorContainerRules.classList.add('active');
            ctx.elements.editorContainerRules.style.display = 'flex';
            ctx.elements.editorContainerEmbeddings.classList.remove('active');
            ctx.elements.editorContainerEmbeddings.style.display = 'none';
            ctx.elements.editorContainerRerank.classList.remove('active');
            ctx.elements.editorContainerRerank.style.display = 'none';
            ctx.elements.editorContainerImages.classList.remove('active');
            ctx.elements.editorContainerImages.style.display = 'none';
            ctx.elements.editorContainerAudio.classList.remove('active');
            ctx.elements.editorContainerAudio.style.display = 'none';
            ctx.elements.editorContainerWeb.classList.remove('active');
            ctx.elements.editorContainerWeb.style.display = 'none';
            ctx.elements.editorContainerFallbackEval.classList.remove('active');
            ctx.elements.editorContainerFallbackEval.style.display = 'none';
            ctx.elements.editorContainerProviders.classList.remove('active');
            ctx.elements.editorContainerProviders.style.display = 'none';
            return ctx.loadRulesEditor();
        } else if (tabName === 'embeddings') {
            ctx.elements.editorContainerRules.classList.remove('active');
            ctx.elements.editorContainerRules.style.display = 'none';
            ctx.elements.editorContainerEmbeddings.classList.add('active');
            ctx.elements.editorContainerEmbeddings.style.display = 'flex';
            ctx.elements.editorContainerRerank.classList.remove('active');
            ctx.elements.editorContainerRerank.style.display = 'none';
            ctx.elements.editorContainerImages.classList.remove('active');
            ctx.elements.editorContainerImages.style.display = 'none';
            ctx.elements.editorContainerAudio.classList.remove('active');
            ctx.elements.editorContainerAudio.style.display = 'none';
            ctx.elements.editorContainerWeb.classList.remove('active');
            ctx.elements.editorContainerWeb.style.display = 'none';
            ctx.elements.editorContainerFallbackEval.classList.remove('active');
            ctx.elements.editorContainerFallbackEval.style.display = 'none';
            ctx.elements.editorContainerProviders.classList.remove('active');
            ctx.elements.editorContainerProviders.style.display = 'none';
            return ctx.loadEmbeddingsEditor();
        } else if (tabName === 'rerank') {
            ctx.elements.editorContainerRules.classList.remove('active');
            ctx.elements.editorContainerRules.style.display = 'none';
            ctx.elements.editorContainerEmbeddings.classList.remove('active');
            ctx.elements.editorContainerEmbeddings.style.display = 'none';
            ctx.elements.editorContainerRerank.classList.add('active');
            ctx.elements.editorContainerRerank.style.display = 'flex';
            ctx.elements.editorContainerImages.classList.remove('active');
            ctx.elements.editorContainerImages.style.display = 'none';
            ctx.elements.editorContainerAudio.classList.remove('active');
            ctx.elements.editorContainerAudio.style.display = 'none';
            ctx.elements.editorContainerWeb.classList.remove('active');
            ctx.elements.editorContainerWeb.style.display = 'none';
            ctx.elements.editorContainerFallbackEval.classList.remove('active');
            ctx.elements.editorContainerFallbackEval.style.display = 'none';
            ctx.elements.editorContainerProviders.classList.remove('active');
            ctx.elements.editorContainerProviders.style.display = 'none';
            return ctx.loadRerankEditor();
        } else if (tabName === 'images') {
            ctx.elements.editorContainerRules.classList.remove('active');
            ctx.elements.editorContainerRules.style.display = 'none';
            ctx.elements.editorContainerEmbeddings.classList.remove('active');
            ctx.elements.editorContainerEmbeddings.style.display = 'none';
            ctx.elements.editorContainerRerank.classList.remove('active');
            ctx.elements.editorContainerRerank.style.display = 'none';
            ctx.elements.editorContainerImages.classList.add('active');
            ctx.elements.editorContainerImages.style.display = 'flex';
            ctx.elements.editorContainerAudio.classList.remove('active');
            ctx.elements.editorContainerAudio.style.display = 'none';
            ctx.elements.editorContainerWeb.classList.remove('active');
            ctx.elements.editorContainerWeb.style.display = 'none';
            ctx.elements.editorContainerFallbackEval.classList.remove('active');
            ctx.elements.editorContainerFallbackEval.style.display = 'none';
            ctx.elements.editorContainerProviders.classList.remove('active');
            ctx.elements.editorContainerProviders.style.display = 'none';
            return ctx.loadImagesEditor();
        } else if (tabName === 'audio') {
            ctx.elements.editorContainerRules.classList.remove('active');
            ctx.elements.editorContainerRules.style.display = 'none';
            ctx.elements.editorContainerEmbeddings.classList.remove('active');
            ctx.elements.editorContainerEmbeddings.style.display = 'none';
            ctx.elements.editorContainerRerank.classList.remove('active');
            ctx.elements.editorContainerRerank.style.display = 'none';
            ctx.elements.editorContainerImages.classList.remove('active');
            ctx.elements.editorContainerImages.style.display = 'none';
            ctx.elements.editorContainerAudio.classList.add('active');
            ctx.elements.editorContainerAudio.style.display = 'flex';
            ctx.elements.editorContainerWeb.classList.remove('active');
            ctx.elements.editorContainerWeb.style.display = 'none';
            ctx.elements.editorContainerFallbackEval.classList.remove('active');
            ctx.elements.editorContainerFallbackEval.style.display = 'none';
            ctx.elements.editorContainerProviders.classList.remove('active');
            ctx.elements.editorContainerProviders.style.display = 'none';
            return ctx.loadAudioEditor();
        } else if (tabName === 'web') {
            ctx.elements.editorContainerRules.classList.remove('active');
            ctx.elements.editorContainerRules.style.display = 'none';
            ctx.elements.editorContainerEmbeddings.classList.remove('active');
            ctx.elements.editorContainerEmbeddings.style.display = 'none';
            ctx.elements.editorContainerRerank.classList.remove('active');
            ctx.elements.editorContainerRerank.style.display = 'none';
            ctx.elements.editorContainerImages.classList.remove('active');
            ctx.elements.editorContainerImages.style.display = 'none';
            ctx.elements.editorContainerAudio.classList.remove('active');
            ctx.elements.editorContainerAudio.style.display = 'none';
            ctx.elements.editorContainerWeb.classList.add('active');
            ctx.elements.editorContainerWeb.style.display = 'flex';
            ctx.elements.editorContainerFallbackEval.classList.remove('active');
            ctx.elements.editorContainerFallbackEval.style.display = 'none';
            ctx.elements.editorContainerProviders.classList.remove('active');
            ctx.elements.editorContainerProviders.style.display = 'none';
            return ctx.loadWebEditor();
        } else if (tabName === 'openrouter-free') {
            ctx.elements.editorContainerRules.classList.remove('active');
            ctx.elements.editorContainerRules.style.display = 'none';
            ctx.elements.editorContainerEmbeddings.classList.remove('active');
            ctx.elements.editorContainerEmbeddings.style.display = 'none';
            ctx.elements.editorContainerRerank.classList.remove('active');
            ctx.elements.editorContainerRerank.style.display = 'none';
            ctx.elements.editorContainerImages.classList.remove('active');
            ctx.elements.editorContainerImages.style.display = 'none';
            ctx.elements.editorContainerAudio.classList.remove('active');
            ctx.elements.editorContainerAudio.style.display = 'none';
            ctx.elements.editorContainerWeb.classList.remove('active');
            ctx.elements.editorContainerWeb.style.display = 'none';
            ctx.elements.editorContainerOpenRouterFree.classList.add('active');
            ctx.elements.editorContainerOpenRouterFree.style.display = 'flex';
            ctx.elements.editorContainerFallbackEval.classList.remove('active');
            ctx.elements.editorContainerFallbackEval.style.display = 'none';
            ctx.elements.editorContainerProviders.classList.remove('active');
            ctx.elements.editorContainerProviders.style.display = 'none';
            return ctx.loadOpenRouterFreeModels(true, context);
        } else if (tabName === 'fallback-eval') {
            ctx.elements.editorContainerRules.classList.remove('active');
            ctx.elements.editorContainerRules.style.display = 'none';
            ctx.elements.editorContainerEmbeddings.classList.remove('active');
            ctx.elements.editorContainerEmbeddings.style.display = 'none';
            ctx.elements.editorContainerRerank.classList.remove('active');
            ctx.elements.editorContainerRerank.style.display = 'none';
            ctx.elements.editorContainerImages.classList.remove('active');
            ctx.elements.editorContainerImages.style.display = 'none';
            ctx.elements.editorContainerAudio.classList.remove('active');
            ctx.elements.editorContainerAudio.style.display = 'none';
            ctx.elements.editorContainerWeb.classList.remove('active');
            ctx.elements.editorContainerWeb.style.display = 'none';
            ctx.elements.editorContainerOpenRouterFree.classList.remove('active');
            ctx.elements.editorContainerOpenRouterFree.style.display = 'none';
            ctx.elements.editorContainerFallbackEval.classList.add('active');
            ctx.elements.editorContainerFallbackEval.style.display = 'flex';
            ctx.elements.editorContainerProviders.classList.remove('active');
            ctx.elements.editorContainerProviders.style.display = 'none';
            return ctx.loadFallbackModelEvals(true, context);
        } else if (tabName === 'providers') {
            ctx.elements.editorContainerRules.classList.remove('active');
            ctx.elements.editorContainerRules.style.display = 'none';
            ctx.elements.editorContainerEmbeddings.classList.remove('active');
            ctx.elements.editorContainerEmbeddings.style.display = 'none';
            ctx.elements.editorContainerRerank.classList.remove('active');
            ctx.elements.editorContainerRerank.style.display = 'none';
            ctx.elements.editorContainerImages.classList.remove('active');
            ctx.elements.editorContainerImages.style.display = 'none';
            ctx.elements.editorContainerAudio.classList.remove('active');
            ctx.elements.editorContainerAudio.style.display = 'none';
            ctx.elements.editorContainerWeb.classList.remove('active');
            ctx.elements.editorContainerWeb.style.display = 'none';
            ctx.elements.editorContainerFallbackEval.classList.remove('active');
            ctx.elements.editorContainerFallbackEval.style.display = 'none';
            ctx.elements.editorContainerProviders.classList.add('active');
            ctx.elements.editorContainerProviders.style.display = 'flex';
            return ctx.loadProvidersEditor();
        } else if (tabName === 'fusion') {
            ctx.elements.editorContainerRules.classList.remove('active');
            ctx.elements.editorContainerRules.style.display = 'none';
            ctx.elements.editorContainerEmbeddings.classList.remove('active');
            ctx.elements.editorContainerEmbeddings.style.display = 'none';
            ctx.elements.editorContainerRerank.classList.remove('active');
            ctx.elements.editorContainerRerank.style.display = 'none';
            ctx.elements.editorContainerImages.classList.remove('active');
            ctx.elements.editorContainerImages.style.display = 'none';
            ctx.elements.editorContainerAudio.classList.remove('active');
            ctx.elements.editorContainerAudio.style.display = 'none';
            ctx.elements.editorContainerWeb.classList.remove('active');
            ctx.elements.editorContainerWeb.style.display = 'none';
            ctx.elements.editorContainerFallbackEval.classList.remove('active');
            ctx.elements.editorContainerFallbackEval.style.display = 'none';
            ctx.elements.editorContainerProviders.classList.remove('active');
            ctx.elements.editorContainerProviders.style.display = 'none';
            ctx.elements.editorContainerFusion.classList.add('active');
            ctx.elements.editorContainerFusion.style.display = 'flex';
            return ctx.loadFusionEditor();
        } else if (tabName === 'router') {
            ctx.elements.editorContainerRules.classList.remove('active');
            ctx.elements.editorContainerRules.style.display = 'none';
            ctx.elements.editorContainerEmbeddings.classList.remove('active');
            ctx.elements.editorContainerEmbeddings.style.display = 'none';
            ctx.elements.editorContainerRerank.classList.remove('active');
            ctx.elements.editorContainerRerank.style.display = 'none';
            ctx.elements.editorContainerImages.classList.remove('active');
            ctx.elements.editorContainerImages.style.display = 'none';
            ctx.elements.editorContainerAudio.classList.remove('active');
            ctx.elements.editorContainerAudio.style.display = 'none';
            ctx.elements.editorContainerWeb.classList.remove('active');
            ctx.elements.editorContainerWeb.style.display = 'none';
            ctx.elements.editorContainerFallbackEval.classList.remove('active');
            ctx.elements.editorContainerFallbackEval.style.display = 'none';
            ctx.elements.editorContainerProviders.classList.remove('active');
            ctx.elements.editorContainerProviders.style.display = 'none';
            ctx.elements.editorContainerFusion.classList.remove('active');
            ctx.elements.editorContainerFusion.style.display = 'none';
            ctx.elements.editorContainerRouter.classList.add('active');
            ctx.elements.editorContainerRouter.style.display = 'flex';
            return ctx.loadRouterEditor();
        } else if (tabName === 'model-rules') {
            ctx.elements.editorContainerRules.classList.remove('active');
            ctx.elements.editorContainerRules.style.display = 'none';
            ctx.elements.editorContainerEmbeddings.classList.remove('active');
            ctx.elements.editorContainerEmbeddings.style.display = 'none';
            ctx.elements.editorContainerRerank.classList.remove('active');
            ctx.elements.editorContainerRerank.style.display = 'none';
            ctx.elements.editorContainerImages.classList.remove('active');
            ctx.elements.editorContainerImages.style.display = 'none';
            ctx.elements.editorContainerAudio.classList.remove('active');
            ctx.elements.editorContainerAudio.style.display = 'none';
            ctx.elements.editorContainerWeb.classList.remove('active');
            ctx.elements.editorContainerWeb.style.display = 'none';
            ctx.elements.editorContainerFallbackEval.classList.remove('active');
            ctx.elements.editorContainerFallbackEval.style.display = 'none';
            ctx.elements.editorContainerProviders.classList.remove('active');
            ctx.elements.editorContainerProviders.style.display = 'none';
            ctx.elements.editorContainerFusion.classList.remove('active');
            ctx.elements.editorContainerFusion.style.display = 'none';
            ctx.elements.editorContainerRouter.classList.remove('active');
            ctx.elements.editorContainerRouter.style.display = 'none';
            ctx.elements.editorContainerModelRules.classList.add('active');
            ctx.elements.editorContainerModelRules.style.display = 'flex';
            return ctx.loadModelRulesEditor();
        }
    }

    function reselectRulesTab(context) {
        ctx.state.activeRulesTabContext = context;
        if (
            ctx.state.evalTabLoadStates.has(context.key)
            && ctx.state.evalTabLoadStates.get(context.key) !== 'ready'
        ) {
            return activateRulesTab(context);
        }
        if (context.key === 'openrouter-free' && ctx.state.openRouterFreePollingEnabled) {
            clearTimeout(ctx.state.openRouterFreePollTimer);
            ctx.state.openRouterFreePollTimer = window.setTimeout(() => {
                void ctx.loadOpenRouterFreeModels(false, ctx.state.activeRulesTabContext);
            }, 3000);
        } else if (context.key === 'fallback-eval' && ctx.state.fallbackEvalPollingEnabled) {
            clearTimeout(ctx.state.fallbackEvalPollTimer);
            ctx.state.fallbackEvalPollTimer = window.setTimeout(() => {
                void ctx.loadFallbackModelEvals(false, ctx.state.activeRulesTabContext);
            }, 3000);
        }
        if (!shouldReloadSelectedRulesTab(context.key)) {
            return false;
        }
        return activateRulesTab(context);
    }

    async function reloadActiveDocument() {
        if (ctx.state.activeEditor === 'rules') {
            return ctx.loadRulesEditor();
        }
        if (ctx.state.activeEditor === 'embeddings') {
            return ctx.loadEmbeddingsEditor();
        }
        if (ctx.state.activeEditor === 'rerank') {
            return ctx.loadRerankEditor();
        }
        if (ctx.state.activeEditor === 'images') {
            return ctx.loadImagesEditor();
        }
        if (ctx.state.activeEditor === 'audio') {
            return ctx.loadAudioEditor();
        }
        if (ctx.state.activeEditor === 'web') {
            return ctx.loadWebEditor();
        }
        if (ctx.state.activeEditor === 'fusion') {
            return ctx.loadFusionEditor();
        }
        if (ctx.state.activeEditor === 'router') {
            return ctx.loadRouterEditor();
        }
        if (ctx.state.activeEditor === 'providers') {
            return ctx.loadProvidersEditor();
        }
        if (ctx.state.activeEditor === 'model-rules') {
            return ctx.loadModelRulesEditor();
        }
        return false;
    }


    Object.assign(ctx, {
        t,
        renderLocalizedBinding,
        bindLocalizedText,
        bindLocalizedAttribute,
        bindLocalizedValue,
        setRawDetail,
        localizedMessageDescriptor,
        typeForUnknownMessage,
        resolveDescriptorValues,
        showLocalizedMessage,
        rerenderLocale,
        boundedSafeText,
        safeResponseError,
        safeClientError,
        safeSuccessMessage,
        showClientValidationError,
        cloneConfigPayload,
        requireStrongEtag,
        commitDocumentBase,
        getDocumentBase,
        getOperationBasePayload,
        isRecord,
        requireArrayProperty,
        validateFallbackPayload,
        validateOperationPayload,
        validateFusionPayload,
        validateRouterPayload,
        validateProvidersPayload,
        readJsonBody,
        showConflict,
        clearConflict,
        currentDocumentName,
        isInteractionLocked,
        syncInteractionLock,
        setDocumentBusy,
        loadConfigDocument,
        saveConfigDocument,
        showLocalizedError,
        clearElement,
        formatDateTime,
        formatNumber,
        updateControlsVisibility,
        isActiveEditorLoaded,
        updateSaveButtonDisabledState,
        updateDirtyIndicator,
        updateProvidersControlsState,
        setProvidersLoadState,
        refreshRulesEmptyState,
        refreshEmbeddingsEmptyState,
        refreshRerankEmptyState,
        refreshImageGenerationEmptyState,
        refreshImageEditEmptyState,
        refreshAudioSpeechEmptyState,
        refreshAudioTranscriptionsEmptyState,
        refreshWebSearchEmptyState,
        refreshWebReadEmptyState,
        refreshWebResearchEmptyState,
        refreshWebDeepResearchEmptyState,
        refreshProvidersEmptyState,
        refreshFusionEmptyState,
        refreshRouterEmptyState,
        bindKnownActionText,
        bindKnownText,
        bindKnownPlaceholder,
        createFieldGroup,
        createTextInput,
        createNumberInput,
        createOperationCostCalculatorField,
        applyOperationCostCalculator,
        createTextarea,
        createSelect,
        createTriStateSelect,
        capabilityAutofillSourceFor,
        wrapCapabilityField,
        sortProviderModelIds,
        setSelectOptions,
        setModelSelectOptions,
        normalizeObjectTextarea,
        parseObjectTextarea,
        parseProvidersOrder,
        createRetrySettingsInputs,
        applyRetrySettingsToPayload,
        applyCapabilityFieldsToPayload,
        setupRowReordering,
        createMoveButtons,
        isCurrentEditorDirty,
        beforeRulesTabActivate,
        shouldReloadSelectedRulesTab,
        activateRulesTab,
        reselectRulesTab,
        reloadActiveDocument,
        ConfigUiError,
        LocalizedUiError,
    });
}

// Entity-first sidebar navigation, sticky footer status, and inspector.
// The sidebar is the only tablist-equivalent UI for the rules editor (the
// former top `.tabs` bar was removed): sidebar buttons drive the same
// `rulesTabsController.activate()` API the tab bar used to, so ETag/CAS, the
// dirty-confirm guard, and conflict handling are untouched. `renderActiveEntity`
// is invoked directly by `activateRulesTab()` on every tab switch (exposed via
// `ctx.renderActiveEntity`), and a MutationObserver on the sidebar's own list
// keeps it in sync with the openrouter-free entity's dynamic `hidden` toggle
// (set by fallback.mjs) and the Save button's `data-editor-dirty` state.
const ENTITY_DOCUMENT_LABEL_KEYS = {
    rules: 'editor:tabs.rules',
    embeddings: 'editor:tabs.embeddings',
    rerank: 'editor:tabs.rerank',
    images: 'editor:tabs.images',
    audio: 'editor:tabs.audio',
    web: 'editor:tabs.web',
    fusion: 'editor:tabs.fusion',
    router: 'editor:tabs.router',
    'model-rules': 'editor:tabs.modelRules',
    'openrouter-free': 'editor:tabs.openrouterFree',
    'fallback-eval': 'editor:tabs.fallbackEval',
    providers: 'editor:tabs.providers',
};

const ENTITY_VALIDATION_LABEL_KEYS = {
    dirty: 'editor:footer.unsavedChanges',
    clean: 'editor:footer.allSaved',
};

export function registerEntityPanel(ctx) {
    const entityNavList = document.getElementById('entityNavList');
    const entityItems = Array.from(document.querySelectorAll('.editor-entity-item'));
    const entityGroups = Array.from(document.querySelectorAll('.editor-entity-group'));
    const searchInput = document.getElementById('entitySearchInput');
    const searchEmptyState = document.getElementById('entityListEmptyState');
    const footerDocument = document.getElementById('editorFooterDocument');
    const footerValidation = document.getElementById('editorFooterValidation');
    const footerPreviewButton = document.getElementById('footerPreviewButton');

    if (!entityNavList || entityItems.length === 0) {
        return;
    }

    function t(key, values = {}) {
        return ctx.gatewayI18n.t(key, values);
    }

    function renderValidationSummary() {
        if (!footerValidation || ctx.gatewayI18n.currentLocale === null) return;
        const dirty = ctx.isCurrentEditorDirty();
        const labelKey = ENTITY_VALIDATION_LABEL_KEYS[dirty ? 'dirty' : 'clean'];
        footerValidation.textContent = t(labelKey);
        footerValidation.classList.toggle('is-dirty', dirty);
    }

    function renderActiveEntity() {
        // `t()` throws until the i18n runtime finishes loading its catalogs;
        // this can run before that (e.g. the initial tablist commit fires
        // synchronously, well before `initEditor()`'s `await gatewayI18n.ready`
        // in index.mjs). Skip translated rendering until ready; the readiness
        // subscription below re-renders once it is.
        if (ctx.gatewayI18n.currentLocale === null) return;
        const key = ctx.state.activeEditor || null;
        entityItems.forEach((item) => {
            const isActive = key !== null && item.dataset.entityTarget === key;
            item.classList.toggle('active', isActive);
            item.setAttribute('aria-current', isActive ? 'true' : 'false');
        });
        const labelKey = key ? ENTITY_DOCUMENT_LABEL_KEYS[key] : null;
        const label = labelKey ? t(labelKey) : '';
        if (footerDocument) {
            footerDocument.textContent = label;
        }
        if (footerPreviewButton) {
            footerPreviewButton.hidden = key !== 'rules';
        }
        renderValidationSummary();
    }

    function applySearchFilter() {
        const query = (searchInput?.value || '').trim().toLowerCase();
        let anyGroupVisible = false;
        entityGroups.forEach((group) => {
            let groupHasVisible = false;
            group.querySelectorAll('.editor-entity-item').forEach((item) => {
                if (item.hidden) {
                    item.classList.remove('is-filtered');
                    return;
                }
                const matches = !query || item.textContent.toLowerCase().includes(query);
                item.classList.toggle('is-filtered', !matches);
                if (matches) groupHasVisible = true;
            });
            group.hidden = !groupHasVisible;
            if (groupHasVisible) anyGroupVisible = true;
        });
        if (searchEmptyState) {
            searchEmptyState.hidden = anyGroupVisible;
        }
    }

    entityItems.forEach((item) => {
        item.addEventListener('click', () => {
            const target = item.dataset.entityTarget;
            const controller = ctx.state.rulesTabsController;
            if (!target || !controller) return;
            void controller.activate(target, {reason: 'pointer', focus: false});
        });
    });

    if (searchInput) {
        // Filtering the entity list is not a document edit: stop these events
        // from reaching the `.container` delegate listener in index.mjs so
        // they never bump `editorMutationVersion` (the save/load staleness
        // guard) or the beforeunload dirty check.
        ['input', 'change'].forEach((eventName) => {
            searchInput.addEventListener(eventName, (event) => {
                event.stopPropagation();
                applySearchFilter();
            });
        });
    }

    if (footerPreviewButton) {
        footerPreviewButton.addEventListener('click', () => {
            ctx.elements.previewRulesButton?.click();
        });
    }

    // Observe only the entity buttons themselves (not `subtree` over all of
    // `entityNavList`): `applySearchFilter()` below also toggles `hidden` on
    // the wrapping `.editor-entity-group` elements, which live in that same
    // subtree, so a subtree-wide observer would re-trigger itself on every
    // search-filter pass and spin forever.
    const entityAvailabilityObserver = new MutationObserver(() => {
        renderActiveEntity();
        applySearchFilter();
    });
    entityItems.forEach((item) => {
        entityAvailabilityObserver.observe(item, {
            attributes: true,
            attributeFilter: ['hidden'],
        });
    });

    if (ctx.elements.saveButton) {
        const dirtyObserver = new MutationObserver(renderValidationSummary);
        dirtyObserver.observe(ctx.elements.saveButton, {
            attributes: true,
            attributeFilter: ['data-editor-dirty'],
        });
    }

    ctx.gatewayI18n.subscribe(renderActiveEntity);
    // The very first render can land before the i18n runtime is ready (see
    // the guard in renderActiveEntity); re-render once it is so the footer
    // and inspector are never left blank.
    ctx.gatewayI18n.ready.then(renderActiveEntity).catch(() => undefined);

    renderActiveEntity();
    applySearchFilter();

    Object.assign(ctx, { renderActiveEntity });
}

// Bespoke replacement for `window.gatewayUi.createTabs()` now that the rules
// editor no longer has a top `.tabs` tablist for that shared helper to bind
// to (it requires pre-existing `[role="tab"]` markup). This mirrors just the
// contract `activateRulesTab`/`beforeRulesTabActivate`/`reselectRulesTab`
// need: a manual-activation `.activate(key, {reason, focus})` call that runs
// the veto hook before committing, and a `.repair()` that reselects the
// nearest visible sidebar entity when the active one becomes hidden (e.g.
// OpenRouter Free toggling unavailable). The pending-vs-active operation
// split below (`beginOperation`/`isPending`/`isActive`/`promote`) preserves
// the same race-safety `tabs.mjs` provides: a stale in-flight activation
// (superseded by a newer click, or abandoned via a cancelled unsaved-changes
// confirm) never clobbers state that a later, still-current activation owns.
export function createRulesTabsController(ctx) {
    // Mirrors `createTabs()`'s synchronous `commit(initial, false)` at
    // construction time: `activeKey` must already equal the intended initial
    // tab *before* any async activation runs, so a `repair()` call that races
    // ahead of the real `.activate(initialKey, {reason: 'initial'})` call
    // (e.g. from `initializeOpenRouterFreeTabAvailability()`, which starts
    // concurrently in `initEditor()`) sees the correct current key instead of
    // treating "not yet activated" as "hidden" and jumping to another entity.
    let activeKey = ctx.state.activeEditor ?? null;
    let sequence = 0;
    let pendingAbort = null;
    let activeAbort = null;

    function getEntityItems() {
        return Array.from(document.querySelectorAll('.editor-entity-item'));
    }

    function beginOperation() {
        pendingAbort?.abort();
        pendingAbort = new AbortController();
        sequence += 1;
        return { abortController: pendingAbort, token: sequence };
    }

    function isPending(abortController, token) {
        return pendingAbort === abortController && sequence === token && !abortController.signal.aborted;
    }

    function isActive(abortController) {
        return activeAbort === abortController && !abortController.signal.aborted;
    }

    function promote(abortController) {
        activeAbort?.abort();
        activeAbort = abortController;
        pendingAbort = null;
    }

    async function activate(key, activateOptions = {}) {
        const reason = activateOptions.reason ?? 'programmatic';
        const previousKey = activeKey;
        const { abortController, token } = beginOperation();
        const context = Object.freeze({
            key,
            previousKey,
            reason,
            signal: abortController.signal,
            isCurrent: () => isPending(abortController, token) || isActive(abortController),
        });

        if (key === activeKey) {
            promote(abortController);
            if (!ctx.reselectRulesTab) return true;
            await ctx.reselectRulesTab(context);
            return isActive(abortController);
        }

        if (ctx.beforeRulesTabActivate) {
            const permitted = await ctx.beforeRulesTabActivate(context);
            if (!isPending(abortController, token)) return false;
            if (permitted === false) {
                pendingAbort = null;
                abortController.abort();
                return false;
            }
        }
        if (!isPending(abortController, token)) return false;
        promote(abortController);
        activeKey = key;
        if (ctx.activateRulesTab) await ctx.activateRulesTab(context);
        return isActive(abortController);
    }

    async function repair() {
        const items = getEntityItems();
        const index = items.findIndex((el) => el.dataset.entityTarget === activeKey);
        const activeItem = items[index];
        if (activeItem && !activeItem.hidden) return false;
        const forward = items.slice(index + 1).find((el) => !el.hidden);
        const backward = items
            .slice(0, Math.max(index, 0))
            .reverse()
            .find((el) => !el.hidden);
        const replacement = forward || backward;
        if (!replacement) return false;
        return activate(replacement.dataset.entityTarget, { reason: 'repair', focus: false });
    }

    return Object.freeze({
        activate,
        repair,
        get activeKey() {
            return activeKey;
        },
    });
}
