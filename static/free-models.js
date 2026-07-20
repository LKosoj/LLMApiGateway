document.addEventListener('DOMContentLoaded', () => {
    'use strict';

    const { apiFetch, bootstrapRoleUI } = window.gatewayAuth;
    const i18n = window.gatewayI18n;

    const freeModelsList = document.getElementById('free-models-list');
    const sourceMetaEl = document.getElementById('free-models-source-meta');
    const filterHintEl = document.getElementById('free-models-filter-hint');
    const statusEl = document.getElementById('free-models-status');
    const errorDetailEl = document.getElementById('free-models-error-detail');
    const retryButton = document.getElementById('free-models-retry');
    const controlsEl = document.getElementById('free-models-controls');
    const searchInput = document.getElementById('free-models-search');
    const searchClearButton = document.getElementById('free-models-search-clear');
    const statusFilterEl = document.getElementById('free-models-status-filter');
    const statusFilterRadios = Array.from(
        document.querySelectorAll('input[name="free-models-status-filter"]')
    );

    const LIMIT_KEYS = Object.freeze({
        rpm: 'free_models:limits.rpm',
        rpd: 'free_models:limits.rpd',
        tpm: 'free_models:limits.tpm',
        tpd: 'free_models:limits.tpd',
    });

    Theme.attachToggle('darkModeToggle');

    const freeModelsStatus = window.gatewayUi.createStatus(statusEl, {
        rawDetailElement: errorDetailEl,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });

    const copyTimers = new Map();
    let snapshot = null;
    let pageState = 'loading';
    let displayError = null;
    let inFlight = false;
    let stopped = false;
    let unsubscribeLocale = null;
    // Set of provider-config model ids ("configured") once known, or `null`
    // when unknown -- either because the caller's role cannot read
    // provider config (virtual keys get a 403 on `/v1/config/*`, which is
    // expected and not an error) or the lookup itself failed. `null` means
    // the catalog renders as a single unsplit list, exactly as before this
    // feature existed.
    let configuredModelIds = null;
    let searchQuery = '';
    let searchDebounceTimer = null;
    let statusFilter = 'all';

    function createNode(tagName, className = '', text = null) {
        const node = document.createElement(tagName);
        if (className) node.className = className;
        if (text !== null) node.textContent = String(text);
        return node;
    }

    function markTechnical(node) {
        node.lang = 'und';
        node.dir = 'auto';
        return node;
    }

    function formatCompactNumber(value) {
        const number = Number(value);
        if (!Number.isFinite(number)) return '';
        const abs = Math.abs(number);
        if (abs >= 1_000_000) return `${i18n.formatNumber(Math.round(number / 1_000_000))}M`;
        if (abs >= 1_000) return `${i18n.formatNumber(Math.round(number / 1_000))}K`;
        return i18n.formatNumber(number);
    }

    function formatUpdatedAt() {
        if (!snapshot || snapshot.updatedAt === null) return '';
        const date = new Date(snapshot.updatedAt);
        if (Number.isNaN(date.getTime())) return String(snapshot.updatedAt);
        return i18n.formatDate(date, {dateStyle: 'medium', timeStyle: 'medium'});
    }

    function clearCopyTimers() {
        copyTimers.forEach((timer) => window.clearTimeout(timer));
        copyTimers.clear();
    }

    async function copyModelId(button, modelId) {
        try {
            await navigator.clipboard.writeText(modelId);
            if (stopped || !button.isConnected) return;
            button.textContent = i18n.t('free_models:modelIdCopied');
        } catch (error) {
            if (stopped || !button.isConnected) return;
            button.textContent = i18n.t('free_models:modelIdCopyFailed');
        }
        if (copyTimers.has(button)) window.clearTimeout(copyTimers.get(button));
        copyTimers.set(button, window.setTimeout(() => {
            copyTimers.delete(button);
            if (!stopped && button.isConnected) button.textContent = i18n.t('free_models:modelIdCopy');
        }, 1500));
    }

    function createCopyButton(modelId) {
        const button = createNode('button', 'free-models-copy', i18n.t('free_models:modelIdCopy'));
        button.type = 'button';
        button.addEventListener('click', () => void copyModelId(button, modelId));
        return button;
    }

    function renderLimits(limits) {
        const wrap = createNode('div', 'free-models-limits');
        Object.keys(LIMIT_KEYS).forEach((key) => {
            const value = limits ? limits[key] : null;
            if (value === null || value === undefined) return;
            const badge = createNode('span', 'free-models-limit');
            badge.textContent = `${i18n.t(LIMIT_KEYS[key])} ${i18n.formatNumber(Number(value))}`;
            wrap.appendChild(badge);
        });
        return wrap;
    }

    function renderBadges(model) {
        const badges = [];
        if (model.supportsVision) badges.push(createNode('span', 'free-models-badge', i18n.t('free_models:vision')));
        if (model.supportsTools) badges.push(createNode('span', 'free-models-badge', i18n.t('free_models:tools')));
        if (badges.length === 0) return null;
        const wrap = createNode('div', 'free-models-badges');
        wrap.append(...badges);
        return wrap;
    }

    function renderQuirks(quirks) {
        const details = document.createElement('details');
        details.className = 'free-models-quirks';
        details.appendChild(createNode('summary', '', i18n.t('free_models:quirksSummary', {count: quirks.length})));
        quirks.forEach((quirk) => {
            const item = createNode('div', 'free-models-quirk');
            item.appendChild(createNode('div', 'free-models-quirk-title', quirk.title));
            if (quirk.body) item.appendChild(createNode('p', 'free-models-quirk-body', quirk.body));
            details.appendChild(item);
        });
        return details;
    }

    function isModelConfigured(model) {
        return configuredModelIds !== null && configuredModelIds.has(model.modelId);
    }

    function renderConfigBadge(model) {
        const configured = isModelConfigured(model);
        return createNode(
            'span',
            `free-models-config-badge ${configured ? 'badge-configured' : 'badge-external'}`,
            i18n.t(configured ? 'free_models:configuredBadge' : 'free_models:externalBadge'),
        );
    }

    function renderModelRow(model) {
        const row = createNode('div', 'free-models-model');
        row.dataset.freeModelsModelId = model.modelId;

        const header = createNode('div', 'free-models-model-header');
        header.appendChild(createNode('span', 'free-models-model-name', model.displayName));
        if (configuredModelIds !== null) header.appendChild(renderConfigBadge(model));
        header.appendChild(markTechnical(createNode('code', 'free-models-model-id', model.modelId)));
        header.appendChild(createCopyButton(model.modelId));
        row.appendChild(header);

        row.appendChild(createNode(
            'div',
            'free-models-budget',
            `${i18n.t('free_models:budgetLabel')} ${model.monthlyTokenBudget}`,
        ));

        const meta = createNode('div', 'free-models-meta-row');
        if (model.contextWindow !== null && model.contextWindow !== undefined) {
            meta.appendChild(createNode(
                'span',
                'free-models-context',
                `${i18n.t('free_models:contextLabel')} ${formatCompactNumber(model.contextWindow)}`,
            ));
        }
        const badges = renderBadges(model);
        if (badges) meta.appendChild(badges);
        if (meta.childNodes.length > 0) row.appendChild(meta);

        row.appendChild(renderLimits(model.limits));

        if (Array.isArray(model.quirks) && model.quirks.length > 0) {
            row.appendChild(renderQuirks(model.quirks));
        }

        return row;
    }

    function renderProviderCard(provider) {
        const card = createNode('div', 'free-models-provider');
        card.dataset.freeModelsProviderId = provider.id;
        const header = createNode('div', 'free-models-provider-header');
        header.appendChild(createNode('span', 'free-models-provider-name', provider.name));
        card.appendChild(header);
        provider.models.forEach((model) => card.appendChild(renderModelRow(model)));
        return card;
    }

    function renderStateMessage(text) {
        const node = createNode('p', 'free-models-empty', text);
        node.dataset.freeModelsField = 'state-message';
        freeModelsList.replaceChildren(node);
    }

    function renderProviders(providers) {
        clearCopyTimers();
        freeModelsList.replaceChildren(...providers.map(renderProviderCard));
    }

    function normalizeQuery(value) {
        return value.trim().toLowerCase();
    }

    function modelMatchesSearch(model, providerName, query) {
        if (!query) return true;
        return (
            model.displayName.toLowerCase().includes(query)
            || model.modelId.toLowerCase().includes(query)
            || providerName.toLowerCase().includes(query)
        );
    }

    function filterProviders(providers, query, modelPredicate) {
        const result = [];
        providers.forEach((provider) => {
            const models = provider.models.filter(
                (model) => modelMatchesSearch(model, provider.name, query) && modelPredicate(model)
            );
            if (models.length > 0) result.push({...provider, models});
        });
        return result;
    }

    function countModels(providers) {
        return providers.reduce((total, provider) => total + provider.models.length, 0);
    }

    function buildSearchEmptyMessage() {
        const node = createNode('p', 'free-models-empty', i18n.t('free_models:searchEmpty'));
        node.dataset.freeModelsField = 'search-empty';
        return node;
    }

    function buildSection(kind, count, providers) {
        const section = createNode('section', 'free-models-section');
        section.dataset.freeModelsSection = kind;
        section.appendChild(
            createNode('h2', 'free-models-section-heading', i18n.t(`free_models:sections.${kind}`, {count}))
        );
        providers.forEach((provider) => section.appendChild(renderProviderCard(provider)));
        return section;
    }

    function renderCatalog() {
        const query = normalizeQuery(searchQuery);
        const providers = snapshot.providers;

        if (configuredModelIds === null) {
            const filtered = filterProviders(providers, query, () => true);
            if (filtered.length === 0) {
                clearCopyTimers();
                freeModelsList.replaceChildren(buildSearchEmptyMessage());
            } else {
                renderProviders(filtered);
            }
            return;
        }

        const showConfigured = statusFilter !== 'external';
        const showExternal = statusFilter !== 'configured';
        const configuredProviders = filterProviders(providers, query, isModelConfigured);
        const externalProviders = filterProviders(providers, query, (model) => !isModelConfigured(model));
        const configuredCount = countModels(configuredProviders);
        const externalCount = countModels(externalProviders);
        const visibleCount = (showConfigured ? configuredCount : 0) + (showExternal ? externalCount : 0);

        clearCopyTimers();
        if (visibleCount === 0) {
            freeModelsList.replaceChildren(buildSearchEmptyMessage());
            return;
        }

        const sections = [];
        if (showConfigured) sections.push(buildSection('configured', configuredCount, configuredProviders));
        if (showExternal) sections.push(buildSection('external', externalCount, externalProviders));
        freeModelsList.replaceChildren(...sections);
    }

    function renderControlsVisibility() {
        const hasData = (pageState === 'ready' || pageState === 'stale')
            && snapshot !== null
            && snapshot.providers.length > 0;
        controlsEl.hidden = !hasData;
        statusFilterEl.hidden = !hasData || configuredModelIds === null;
    }

    function renderListContent() {
        if (pageState === 'loading') {
            renderStateMessage(i18n.t('free_models:loading'));
        } else if (pageState === 'unavailable') {
            renderStateMessage(i18n.t('free_models:unavailable'));
        } else if (pageState === 'empty') {
            renderStateMessage(i18n.t('free_models:empty'));
        } else {
            renderCatalog();
        }
    }

    function updateSourceMeta() {
        if (snapshot && snapshot.sourceVersion !== null && snapshot.sourceGeneratedAt !== null) {
            const date = new Date(snapshot.sourceGeneratedAt);
            const formatted = Number.isNaN(date.getTime())
                ? snapshot.sourceGeneratedAt
                : i18n.formatDate(date, {dateStyle: 'medium', timeStyle: 'short'});
            sourceMetaEl.textContent = i18n.t('free_models:sourceMeta', {
                version: snapshot.sourceVersion,
                date: formatted,
            });
        } else {
            sourceMetaEl.textContent = '';
        }
    }

    function updateFilterHint() {
        if (snapshot && Number.isFinite(Number(snapshot.minMonthlyTokens))) {
            filterHintEl.textContent = i18n.t('free_models:filterHint', {
                threshold: formatCompactNumber(snapshot.minMonthlyTokens),
            });
        } else {
            filterHintEl.textContent = '';
        }
    }

    function publishStatus() {
        if (pageState === 'loading') {
            freeModelsStatus.busy({key: 'free_models:loading'});
        } else if (pageState === 'unavailable') {
            freeModelsStatus.error({key: 'free_models:unavailable'}, {rawDetail: displayError});
        } else if (pageState === 'stale') {
            freeModelsStatus.error(
                {key: 'free_models:stale', values: {time: formatUpdatedAt()}},
                {rawDetail: displayError},
            );
        } else {
            freeModelsStatus.polite({key: 'free_models:updated', values: {time: formatUpdatedAt()}});
        }
    }

    function renderChrome() {
        freeModelsList.dataset.freeModelsState = pageState;
        retryButton.hidden = !(pageState === 'unavailable' || pageState === 'stale');
        retryButton.textContent = i18n.t('free_models:retry');
        updateSourceMeta();
        updateFilterHint();
        renderControlsVisibility();
        renderListContent();
    }

    function render() {
        renderChrome();
        publishStatus();
    }

    function captureViewState() {
        return {
            activeElement: document.activeElement,
            scrollX: window.scrollX,
            scrollY: window.scrollY,
        };
    }

    function restoreViewState(state) {
        if (state.activeElement?.isConnected) {
            state.activeElement.focus({preventScroll: true});
        }
        window.scrollTo(state.scrollX, state.scrollY);
    }

    function rerenderLocale() {
        const viewState = captureViewState();
        renderChrome();
        freeModelsStatus.rerender();
        restoreViewState(viewState);
    }

    function applySnapshotState(data) {
        snapshot = data;
        if (snapshot.updatedAt === null) {
            pageState = 'unavailable';
            displayError = snapshot.lastError;
        } else if (snapshot.lastError !== null) {
            pageState = 'stale';
            displayError = snapshot.lastError;
        } else if (snapshot.providers.length === 0) {
            pageState = 'empty';
            displayError = null;
        } else {
            pageState = 'ready';
            displayError = null;
        }
        render();
    }

    function applyNetworkErrorState(message) {
        pageState = snapshot !== null && snapshot.updatedAt !== null ? 'stale' : 'unavailable';
        displayError = message;
        render();
    }

    // Best-effort lookup of the provider-config model ids ("configured").
    // A 403 is the expected outcome for virtual-key sessions -- `/v1/config/*`
    // is master-only -- and is treated the same as any other lookup failure:
    // the split/search badges fall back to the unsplit list, never an error.
    async function fetchConfiguredModelIds() {
        try {
            const response = await apiFetch('/v1/config/providers/structured');
            if (response.status === 403) return null;
            if (!response.ok) {
                console.warn(`Configured models lookup failed with status ${response.status}`);
                return null;
            }
            const data = await response.json();
            if (!data || !Array.isArray(data.providers)) return null;
            const ids = new Set();
            data.providers.forEach((provider) => {
                const models = provider && typeof provider === 'object' ? provider.models : null;
                if (models && typeof models === 'object') {
                    Object.keys(models).forEach((modelId) => ids.add(modelId));
                }
            });
            return ids;
        } catch (error) {
            if (stopped || error.message === 'Authentication required') return null;
            console.warn('Configured models lookup failed');
            return null;
        }
    }

    async function fetchFreeModels() {
        if (inFlight || stopped) return;
        inFlight = true;
        retryButton.disabled = true;
        try {
            const [response, configured] = await Promise.all([
                apiFetch('/v1/api/free-models'),
                fetchConfiguredModelIds(),
            ]);
            if (stopped) return;
            configuredModelIds = configured;
            if (!response.ok) throw new Error(`Free models request failed with status ${response.status}`);
            const data = await response.json();
            if (stopped) return;
            if (!data || typeof data !== 'object' || !Array.isArray(data.providers)) {
                throw new Error('Free models response has an unexpected shape');
            }
            applySnapshotState(data);
        } catch (error) {
            if (stopped) return;
            if (error.message === 'Authentication required') return;
            applyNetworkErrorState(error.message);
            console.warn('Free models data request failed');
        } finally {
            inFlight = false;
            retryButton.disabled = false;
        }
    }

    retryButton.addEventListener('click', () => {
        if (inFlight) return;
        if (snapshot === null) {
            pageState = 'loading';
            render();
        }
        void fetchFreeModels();
    });

    function refreshCatalogView() {
        renderControlsVisibility();
        renderListContent();
    }

    function applySearchQuery(value) {
        searchQuery = value;
        refreshCatalogView();
    }

    searchInput.addEventListener('input', () => {
        searchClearButton.hidden = searchInput.value.length === 0;
        if (searchDebounceTimer !== null) window.clearTimeout(searchDebounceTimer);
        const value = searchInput.value;
        searchDebounceTimer = window.setTimeout(() => {
            searchDebounceTimer = null;
            applySearchQuery(value);
        }, 200);
    });

    searchClearButton.addEventListener('click', () => {
        if (searchDebounceTimer !== null) {
            window.clearTimeout(searchDebounceTimer);
            searchDebounceTimer = null;
        }
        searchInput.value = '';
        searchClearButton.hidden = true;
        applySearchQuery('');
        searchInput.focus();
    });

    statusFilterRadios.forEach((radio) => {
        radio.addEventListener('change', () => {
            if (!radio.checked) return;
            statusFilter = radio.value;
            refreshCatalogView();
        });
    });

    window.addEventListener('beforeunload', () => {
        stopped = true;
        clearCopyTimers();
        if (searchDebounceTimer !== null) window.clearTimeout(searchDebounceTimer);
        unsubscribeLocale?.();
    });

    Promise.all([bootstrapRoleUI(), i18n.ready]).then(() => {
        if (stopped) return;
        unsubscribeLocale = i18n.subscribe(rerenderLocale);
        render();
        void fetchFreeModels();
    }).catch(() => undefined);
});
