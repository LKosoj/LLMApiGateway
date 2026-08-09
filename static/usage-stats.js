document.addEventListener('DOMContentLoaded', () => {
    void window.gatewayI18n.ready.then(initializeUsagePage, () => undefined);
});

function initializeUsagePage() {
    const { apiFetch } = window.gatewayAuth;
    const gatewayI18n = window.gatewayI18n;
    const gatewayUi = window.gatewayUi;

    const periodSelector = document.getElementById('periodSelector');
    const refreshButton = document.getElementById('refreshButton');
    const statsArea = document.getElementById('statsArea');
    const messageArea = document.getElementById('messageArea');
    const messageRawDetail = document.getElementById('messageRawDetail');
    const messageRequestId = document.getElementById('messageRequestId');
    Theme.attachToggle('darkModeToggle');

    const snapshots = {
        stats: null,
        records: null,
        fallbackStats: null,
        fallbackChains: null,
        upstream: null,
    };
    const requestGenerations = {
        stats: 0,
        records: 0,
        fallbackStats: 0,
        fallbackChains: 0,
        upstream: 0,
    };
    const loadingGenerations = {
        stats: null,
        records: null,
        fallbackStats: null,
        fallbackChains: null,
        upstream: null,
    };
    const expandedFallbackChains = new Set();
    let currentRequestId = null;
    let unsubscribeLocale = null;

    const t = (key, values = {}) => gatewayI18n.t(key, values);
    const message = (key, values = {}) => Object.freeze({key, values});
    const formatNumber = (value, options = {}) => gatewayI18n.formatNumber(
        Number.isFinite(Number(value)) ? Number(value) : 0,
        options,
    );
    const formatCurrency = (value) => gatewayI18n.formatCurrency(
        Number.isFinite(Number(value)) ? Number(value) : 0,
        'USD',
        {minimumFractionDigits: 4, maximumFractionDigits: 4},
    );
    const notAvailable = () => t('usage:values.notAvailable');

    const pageStatus = gatewayUi.createStatus(messageArea, {
        rawDetailElement: messageRawDetail,
        renderMessage: (statusMessage) => t(statusMessage.key, statusMessage.values || {}),
    });

    const renderRequestId = (requestId) => {
        currentRequestId = requestId;
        messageRequestId.hidden = requestId === null;
        messageRequestId.textContent = requestId === null
            ? ''
            : gatewayI18n.t('usage:status.requestId', {id: requestId});
        if (requestId === null) {
            messageRequestId.removeAttribute('lang');
            messageRequestId.removeAttribute('dir');
        } else {
            messageRequestId.setAttribute('lang', 'und');
            messageRequestId.setAttribute('dir', 'auto');
        }
    };

    const showMessage = (statusMessage, kind = 'polite', options = {}) => {
        if (!statusMessage) {
            pageStatus.clear();
            renderRequestId(null);
            return;
        }
        pageStatus[kind](statusMessage, {rawDetail: options.rawDetail ?? null});
        renderRequestId(options.requestId ?? null);
    };

    const showApiError = (payload, response = null, error = null) => {
        const descriptor = gatewayUi.describeApiError(payload, {
            status: response?.status,
            requestId: response?.headers?.get('X-Request-ID') || null,
        });
        showMessage(
            message(descriptor.summaryKey, descriptor.summaryValues),
            'error',
            {
                rawDetail: descriptor.rawDetail ?? error?.message ?? null,
                requestId: descriptor.requestId,
            },
        );
    };

    const startRequest = (name) => {
        requestGenerations[name] += 1;
        return requestGenerations[name];
    };
    const startLoading = (name, generation) => {
        loadingGenerations[name] = generation;
    };
    const finishLoading = (name, generation) => {
        if (loadingGenerations[name] === generation) {
            loadingGenerations[name] = null;
        }
    };
    const isCurrentRequest = (name, generation, activationContext = null) => (
        requestGenerations[name] === generation
        && isActivationCurrent(activationContext)
    );

    const isActivationContext = (value) => Boolean(
        value
        && value.signal
        && typeof value.isCurrent === 'function'
    );
    const isActivationCurrent = (value) => (
        !isActivationContext(value)
        || (!value.signal.aborted && value.isCurrent())
    );
    const reportTabActivation = (promise) => {
        promise.catch((error) => {
            queueMicrotask(() => { throw error; });
        });
    };
    let statsActivationContext = null;

    const formatResolvedTarget = (row) => {
        const provider = typeof row?.provider === 'string' ? row.provider.trim() : '';
        const model = typeof row?.model === 'string' ? row.model.trim() : '';

        if (provider && model) {
            return `${provider}/${model}`;
        }
        return model || provider || notAvailable();
    };

    const formatGatewayModel = (row) => {
        const gatewayModel = typeof row?.gateway_model === 'string' ? row.gateway_model.trim() : '';
        return gatewayModel || notAvailable();
    };

    const formatOperation = (row) => {
        const operation = typeof row?.operation === 'string' ? row.operation.trim() : '';
        return operation || notAvailable();
    };

    const formatTimestamp = (value) => {
        const timestamp = typeof value === 'string' ? value.trim() : '';
        if (!timestamp) {
            return notAvailable();
        }
        const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(timestamp);
        const parsed = new Date(hasZone ? timestamp : `${timestamp.replace(' ', 'T')}Z`);
        if (Number.isNaN(parsed.getTime())) return timestamp;
        return gatewayI18n.formatDate(parsed, {
            dateStyle: 'medium',
            timeStyle: 'medium',
            timeZone: 'UTC',
        });
    };

    const isRunningUsageRecord = (row) => {
        const status = typeof row?.status === 'string' ? row.status.trim().toLowerCase() : '';
        return status === 'running' || status === 'in_progress';
    };

    const metricColumnKeys = Object.freeze({
        prompt_tokens: 'usage:columns.promptTokens',
        completion_tokens: 'usage:columns.completionTokens',
        reasoning_tokens: 'usage:columns.reasoningTokens',
        total_tokens: 'usage:columns.totalTokens',
        cached_tokens: 'usage:columns.cachedTokens',
        count: 'usage:columns.countHeader',
        cost: 'usage:columns.costUsd',
        cost_saved: 'usage:columns.costSavedUsd',
        cost_per_million: 'usage:columns.costPerMillion',
    });

    const markTechnical = (element) => {
        element.setAttribute('lang', 'und');
        element.setAttribute('dir', 'auto');
        return element;
    };

    const createStatsTable = (data) => {
        const fragment = document.createDocumentFragment();
        if (!data || data.length === 0) {
            const p = document.createElement('p');
            p.className = 'empty-state';
            p.textContent = gatewayI18n.t('usage:empty.statistics');
            fragment.appendChild(p);
            return fragment;
        }

        const metrics = [
            'prompt_tokens', 'completion_tokens', 'reasoning_tokens',
            'total_tokens', 'cached_tokens', 'count', 'cost', 'cost_saved', 'cost_per_million'
        ];

        const groupedByPeriod = data.reduce((acc, current) => {
            const periodKey = current.time_period;
            if (!acc[periodKey]) {
                acc[periodKey] = [];
            }
            acc[periodKey].push(current);
            return acc;
        }, {});

        for (const periodKey in groupedByPeriod) {
            const h2 = document.createElement('h2');
            h2.textContent = gatewayI18n.t('usage:period.label', {period: periodKey});
            fragment.appendChild(h2);

            const table = document.createElement('table');
            const thead = document.createElement('thead');
            const trHead = document.createElement('tr');
            
            const thGatewayModel = document.createElement('th');
            thGatewayModel.textContent = gatewayI18n.t('usage:columns.gatewayModel');
            trHead.appendChild(thGatewayModel);

            const thResolvedModel = document.createElement('th');
            thResolvedModel.textContent = gatewayI18n.t('usage:columns.resolvedModel');
            trHead.appendChild(thResolvedModel);

            const thOperation = document.createElement('th');
            thOperation.textContent = gatewayI18n.t('usage:columns.operationHeader');
            trHead.appendChild(thOperation);

            metrics.forEach(metric => {
                const th = document.createElement('th');
                th.textContent = t(metricColumnKeys[metric]);
                trHead.appendChild(th);
            });
            thead.appendChild(trHead);
            table.appendChild(thead);

            const tbody = document.createElement('tbody');
            groupedByPeriod[periodKey].forEach(row => {
                const tr = document.createElement('tr');
                const tdGatewayModel = document.createElement('td');
                tdGatewayModel.textContent = formatGatewayModel(row);
                markTechnical(tdGatewayModel);
                tr.appendChild(tdGatewayModel);

                const tdResolvedModel = document.createElement('td');
                tdResolvedModel.textContent = formatResolvedTarget(row);
                markTechnical(tdResolvedModel);
                tr.appendChild(tdResolvedModel);

                const tdOperation = document.createElement('td');
                tdOperation.textContent = formatOperation(row);
                markTechnical(tdOperation);
                tr.appendChild(tdOperation);

                metrics.forEach(metric => {
                    let value = row[metric];
                    if (metric === 'cost_per_million') {
                        const total_cost = row.cost || 0;
                        const total_tokens = row.total_tokens || 0;
                        value = total_tokens > 0 ? (total_cost / total_tokens) * 1000000 : 0;
                    }
                    const td = document.createElement('td');
                    td.textContent = ['cost', 'cost_saved', 'cost_per_million'].includes(metric)
                        ? formatCurrency(value)
                        : formatNumber(value);
                    tr.appendChild(td);
                });
                tbody.appendChild(tr);
            });
            table.appendChild(tbody);
            fragment.appendChild(table);
        }
        return fragment;
    };

    const fetchAndRenderStats = async () => {
        const activationContext = statsActivationContext;
        const requestGeneration = startRequest('stats');
        startLoading('stats', requestGeneration);
        const selectedPeriod = periodSelector.value;
        statsArea.textContent = gatewayI18n.t('usage:status.statisticsLoading');
        showMessage(message('usage:status.statisticsLoading'), 'busy');
        
        refreshButton.disabled = true;
        refreshButton.classList.add('loading');

        try {
            const response = await apiFetch(`/v1/api/usage-stats/${selectedPeriod}`);
            if (!isCurrentRequest('stats', requestGeneration, activationContext)) return;
            const data = await response.json().catch(() => ({}));
            if (!isCurrentRequest('stats', requestGeneration, activationContext)) return;

            if (!response.ok) {
                showApiError(data, response);
                statsArea.textContent = '';
                return;
            }
            snapshots.stats = Object.freeze({period: selectedPeriod, data});
            renderStatsSnapshot();
            if (data.length === 0) {
                showMessage(null);
            } else {
                showMessage(message('usage:status.statisticsLoaded'));
            }

        } catch (error) {
            if (!isCurrentRequest('stats', requestGeneration, activationContext)) return;
            console.error('Failed to fetch usage statistics:', error);
            showApiError(null, null, error);
            statsArea.textContent = '';
        } finally {
            finishLoading('stats', requestGeneration);
            if (!isCurrentRequest('stats', requestGeneration, activationContext)) return;
            refreshButton.disabled = false;
            refreshButton.classList.remove('loading');
        }
    };

    const renderStatsSnapshot = () => {
        if (!snapshots.stats) return;
        statsArea.replaceChildren(createStatsTable(snapshots.stats.data));
    };

    // Event Listeners
    refreshButton.addEventListener('click', fetchAndRenderStats);
    periodSelector.addEventListener('change', fetchAndRenderStats);

    // --- Tab Switching Logic ---
    const tabButtons = document.querySelectorAll('.tab-button');
    const tabContents = document.querySelectorAll('.tab-content');
    const orderedTabContents = Array.from(tabButtons, (button) => (
        document.getElementById(`${button.dataset.tab}TabContent`)
    ));

    const tabsContainer = tabButtons[0]?.closest('[role="tablist"]');
    let fallbackSubTabsController = null;
    let usageTabsController = null;
    const activateUsageTab = async (context) => {
        const tab = context.key;
        tabButtons.forEach((button) => {
            button.classList.toggle('active', button === context.tab);
        });
        tabContents.forEach((content) => {
            content.classList.toggle('active', content === context.panel);
        });

        if (tab === 'records') {
            currentPage = 1;
            await fetchAndRenderRecords(context);
        } else if (tab === 'analytics') {
            if (context.signal.aborted || !context.isCurrent()) return;
            await window.usageAnalyticsDashboard.activate();
        } else if (tab === 'fallback') {
            if (fallbackSubTabsController) {
                await fallbackSubTabsController.activate(fallbackSubTabsController.activeKey);
            }
        } else if (tab === 'topology') {
            await loadTopology(context);
        } else {
            statsActivationContext = context;
            const pendingLoad = fetchAndRenderStats();
            statsActivationContext = null;
            await pendingLoad;
        }
    };
    if (tabsContainer && orderedTabContents.every(Boolean)) {
        usageTabsController = window.gatewayUi.createTabs(tabsContainer, {
            activation: 'manual',
            getKey: (button) => button.dataset.tab,
            initialKey: 'analytics',
            onActivate: activateUsageTab,
            onReselect: activateUsageTab,
            panels: orderedTabContents,
        });
    }

    // --- Usage Records Logic (New) ---
    const recordsArea = document.getElementById('recordsArea');
    const prevPageButton = document.getElementById('prevPage');
    const nextPageButton = document.getElementById('nextPage');
    const recordRefreshButton = document.getElementById('recordRefreshButton');
    const pageInfoSpan = document.getElementById('pageInfo');
    const recordsPerPage = 25; // As per the task description
    let currentPage = 1;
    const recordColumnKeys = Object.freeze({
        timestamp: 'usage:columns.timestamp',
        duration_ms: 'usage:columns.durationMs',
        gateway_model: 'usage:columns.gatewayModel',
        operation: 'usage:columns.operationHeader',
        model: 'usage:columns.resolvedModel',
        x_title: 'usage:columns.xTitleHeader',
        prompt_tokens: 'usage:columns.promptTokens',
        completion_tokens: 'usage:columns.completionTokens',
        reasoning_tokens: 'usage:columns.reasoningTokens',
        total_tokens: 'usage:columns.totalTokens',
        cached_tokens: 'usage:columns.cachedTokens',
        cost: 'usage:columns.costUsd',
        cost_saved: 'usage:columns.costSavedUsd',
        is_estimated: 'usage:columns.estimatedHeader',
    });

    const createRecordsTable = (data) => {
        const fragment = document.createDocumentFragment();
        if (!data || data.length === 0) {
            const p = document.createElement('p');
            p.className = 'empty-state';
            p.textContent = gatewayI18n.t('usage:empty.records');
            fragment.appendChild(p);
            return fragment;
        }

        // Define preferred order for records table headers
        const preferredHeaders = [
            'timestamp', 'duration_ms', 'gateway_model', 'operation', 'model', 'x_title', 'prompt_tokens', 'completion_tokens',
            'reasoning_tokens', 'total_tokens', 'cached_tokens', 'cost', 'cost_saved', 'is_estimated'
        ];

        // Use preferred order, but fallback to actual keys if some are missing or extra ones exist
        const actualKeys = Object.keys(data[0]);
        const headers = preferredHeaders.filter(key => actualKeys.includes(key));
        actualKeys.forEach(key => {
            if (!headers.includes(key) && key !== 'id' && key !== 'request_id' && key !== 'provider' && key !== 'status') {
                headers.push(key);
            }
        });

        const table = document.createElement('table');
        const thead = document.createElement('thead');
        const trHead = document.createElement('tr');

        headers.forEach(header => {
            const th = document.createElement('th');
            const translationKey = recordColumnKeys[header];
            th.textContent = translationKey ? t(translationKey) : header;
            if (!translationKey) markTechnical(th);
            trHead.appendChild(th);
        });
        thead.appendChild(trHead);
        table.appendChild(thead);

        const tbody = document.createElement('tbody');

        data.forEach(row => {
            const tr = document.createElement('tr');
            if (isRunningUsageRecord(row)) {
                tr.classList.add('usage-record-running');
            }
            headers.forEach(header => {
                let formattedValue = row[header];
                if (header === 'gateway_model') {
                    formattedValue = formatGatewayModel(row);
                }
                if (header === 'model') {
                    formattedValue = formatResolvedTarget(row);
                }
                if (header === 'operation') {
                    formattedValue = formatOperation(row);
                }
                if (header === 'timestamp') {
                    formattedValue = formatTimestamp(formattedValue);
                } else if (header === 'cost' || header === 'cost_saved') {
                    formattedValue = formatCurrency(formattedValue);
                } else if (header === 'is_estimated') {
                    formattedValue = (formattedValue === 1 || formattedValue === true) ? '≈' : '';
                } else if (typeof formattedValue === 'number') {
                    formattedValue = formatNumber(formattedValue);
                }
                const td = document.createElement('td');
                td.textContent = formattedValue !== null && formattedValue !== undefined
                    ? formattedValue
                    : notAvailable();
                if (['gateway_model', 'operation', 'model', 'x_title'].includes(header)) {
                    markTechnical(td);
                }
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        fragment.appendChild(table);

        return fragment;
    };

    const updatePaginationControls = (totalRecords, page = currentPage) => {
        const totalPages = Math.ceil(totalRecords / recordsPerPage);
        pageInfoSpan.textContent = gatewayI18n.t('usage:pagination.page', {current: page, total: totalPages || 1});
        prevPageButton.disabled = page === 1;
        nextPageButton.disabled = page === totalPages || totalRecords === 0;
    };

    const fetchAndRenderRecords = async (activationContext = null) => {
        const requestGeneration = startRequest('records');
        startLoading('records', requestGeneration);
        const requestedPage = currentPage;
        recordsArea.textContent = gatewayI18n.t('usage:status.recordsLoading');
        showMessage(message('usage:status.recordsLoading'), 'busy');

        const offset = (requestedPage - 1) * recordsPerPage;
        recordRefreshButton.disabled = true;
        recordRefreshButton.classList.add('loading');

        try {
            const response = await apiFetch(`/v1/api/usage-records?limit=${recordsPerPage}&offset=${offset}`);
            if (!isCurrentRequest('records', requestGeneration, activationContext)) return;
            const responseData = await response.json().catch(() => ({}));
            if (!isCurrentRequest('records', requestGeneration, activationContext)) return;

            if (!response.ok) {
                showApiError(responseData, response);
                recordsArea.textContent = '';
                updatePaginationControls(0);
                return;
            }

            const records = responseData.records;
            const totalRecords = responseData.total_records;
            currentPage = requestedPage;
            snapshots.records = Object.freeze({records, totalRecords, page: requestedPage});
            renderRecordsSnapshot();

            if (records.length === 0 && requestedPage === 1) {
                showMessage(message('usage:empty.recordsFound'));
            } else {
                showMessage(message('usage:status.recordsLoaded'));
            }

        } catch (error) {
            if (!isCurrentRequest('records', requestGeneration, activationContext)) return;
            console.error('Failed to fetch usage records:', error);
            showApiError(null, null, error);
            recordsArea.textContent = '';
            updatePaginationControls(0);
        } finally {
            finishLoading('records', requestGeneration);
            if (!isCurrentRequest('records', requestGeneration, activationContext)) return;
            recordRefreshButton.disabled = false;
            recordRefreshButton.classList.remove('loading');
        }
    };

    const renderRecordsSnapshot = () => {
        if (!snapshots.records) return;
        currentPage = snapshots.records.page;
        recordsArea.replaceChildren(createRecordsTable(snapshots.records.records));
        updatePaginationControls(snapshots.records.totalRecords, snapshots.records.page);
    };

    prevPageButton.addEventListener('click', () => {
        if (currentPage > 1) {
            currentPage--;
            fetchAndRenderRecords();
        }
    });

    nextPageButton.addEventListener('click', () => {
        currentPage++; // We will check bounds in updatePaginationControls or based on API response
        fetchAndRenderRecords();
    });

    recordRefreshButton.addEventListener('click', () => {
        currentPage = 1; // Reset to the first page on refresh
        fetchAndRenderRecords();
    });

    // --- Fallback Analytics Logic ---
    const fallbackPeriodSelector = document.getElementById('fallbackPeriodSelector');
    const fallbackRefreshButton = document.getElementById('fallbackRefreshButton');
    const fallbackStatsArea = document.getElementById('fallbackStatsArea');
    const fallbackChainsArea = document.getElementById('fallbackChainsArea');
    const fallbackChainsRefreshButton = document.getElementById('fallbackChainsRefreshButton');
    const fallbackPrevPage = document.getElementById('fallbackPrevPage');
    const fallbackNextPage = document.getElementById('fallbackNextPage');
    const fallbackPageInfo = document.getElementById('fallbackPageInfo');
    const upstreamPeriodSelector = document.getElementById('upstreamPeriodSelector');
    const upstreamRefreshButton = document.getElementById('upstreamRefreshButton');
    const upstreamStatsArea = document.getElementById('upstreamStatsArea');
    const fallbackRecordsPerPage = 25;
    let fallbackCurrentPage = 1;
    let fallbackChainsActivationContext = null;

    // Sub-tab switching
    const fallbackSubTabs = document.querySelectorAll('.fallback-sub-tab');
    const fallbackSubContents = document.querySelectorAll('.fallback-sub-content');
    const fallbackPanelIds = {
        summary: 'fallbackSummaryContent',
        chains: 'fallbackChainsContent',
        upstream: 'upstreamAnalyticsContent',
    };
    const orderedFallbackSubContents = Array.from(fallbackSubTabs, (button) => (
        document.getElementById(fallbackPanelIds[button.dataset.subtab])
    ));
    const fallbackSubTabsContainer = fallbackSubTabs[0]?.closest('[role="tablist"]');
    const isFallbackLoadCurrent = (activationContext) => (
        isActivationCurrent(activationContext)
        && isActivationContext(activationContext)
        && fallbackSubTabsController?.activeKey === activationContext.key
        && !document.getElementById('fallbackTabContent').hidden
    );
    const activateFallbackSubTab = async (context) => {
        const subtab = context.key;
        fallbackSubTabs.forEach((button) => {
            button.classList.toggle('active', button === context.tab);
        });
        fallbackSubContents.forEach((content) => {
            content.classList.toggle('active', content === context.panel);
        });
        if (subtab === 'chains') {
            fallbackCurrentPage = 1;
            fallbackChainsActivationContext = context;
            await fetchAndRenderFallbackChains(context);
        } else if (subtab === 'upstream') {
            await fetchAndRenderUpstreamStats(context);
        } else {
            await fetchAndRenderFallbackStats(context);
        }
    };
    if (fallbackSubTabsContainer && orderedFallbackSubContents.every(Boolean)) {
        fallbackSubTabsController = window.gatewayUi.createTabs(fallbackSubTabsContainer, {
            activation: 'manual',
            getKey: (button) => button.dataset.subtab,
            initialKey: 'summary',
            onActivate: activateFallbackSubTab,
            onReselect: activateFallbackSubTab,
            panels: orderedFallbackSubContents,
        });
    }

    const reloadFallbackSubTab = (key) => {
        if (!fallbackSubTabsController) return;
        reportTabActivation(fallbackSubTabsController.activate(key));
    };

    const ERROR_TYPE_LABELS = Object.freeze({
        'http_429': 'usage:errorTypes.http429',
        'http_400': 'usage:errorTypes.http400',
        'http_401': 'usage:errorTypes.http401',
        'http_403': 'usage:errorTypes.http403',
        'http_404': 'usage:errorTypes.http404',
        'http_500': 'usage:errorTypes.http500',
        'http_502': 'usage:errorTypes.http502',
        'http_503': 'usage:errorTypes.http503',
        'read_timeout': 'usage:errorTypes.readTimeout',
        'connect_timeout': 'usage:errorTypes.connectTimeout',
        'connect_error': 'usage:errorTypes.connectError',
        'context_overflow': 'usage:errorTypes.contextOverflow',
        'unknown': 'usage:errorTypes.unknownLabel',
    });

    const ERROR_TYPE_CLASSES = {
        'http_429': 'error-badge-rate-limit',
        'http_401': 'error-badge-access',
        'http_403': 'error-badge-access',
        'read_timeout': 'error-badge-timeout',
        'connect_timeout': 'error-badge-timeout',
        'connect_error': 'error-badge-connect',
        'context_overflow': 'error-badge-overflow',
    };

    // The gateway puts an upstream that answers 401/403 on cooldown, so the
    // target silently disappears from the chain for the next few minutes.
    // Surface it on the collapsed card: the operator has to fix the key or
    // the plan, and nothing else in the UI says so.
    const ACCESS_DENIED_ERROR_TYPES = Object.freeze(['http_401', 'http_403']);

    const accessDeniedTargets = (attempts) => {
        if (!Array.isArray(attempts)) return [];
        const targets = [];
        attempts.forEach((attempt) => {
            if (!ACCESS_DENIED_ERROR_TYPES.includes(attempt?.error_type)) return;
            const target = `${attempt.provider}/${attempt.model}`;
            if (!targets.includes(target)) targets.push(target);
        });
        return targets;
    };

    const getErrorBadgeClass = (errorType) => {
        return ERROR_TYPE_CLASSES[errorType] || 'error-badge-default';
    };

    const formatErrorType = (errorType) => {
        const key = ERROR_TYPE_LABELS[errorType];
        return key ? t(key) : (errorType || notAvailable());
    };

    const formatDuration = (ms) => {
        const duration = Number.isFinite(Number(ms)) ? Number(ms) : 0;
        if (duration < 1000) {
            return t('usage:format.milliseconds', {value: formatNumber(duration)});
        }
        return t('usage:format.seconds', {
            value: formatNumber(duration / 1000, {
                minimumFractionDigits: 1,
                maximumFractionDigits: 1,
            }),
        });
    };

    const createFallbackStatsTable = (data) => {
        const fragment = document.createDocumentFragment();
        if (!data || data.length === 0) {
            const p = document.createElement('p');
            p.className = 'empty-state';
            p.textContent = gatewayI18n.t('usage:empty.fallback');
            fragment.appendChild(p);
            return fragment;
        }

        const groupedByPeriod = data.reduce((acc, row) => {
            const key = row.time_period;
            if (!acc[key]) acc[key] = [];
            acc[key].push(row);
            return acc;
        }, {});

        for (const periodKey in groupedByPeriod) {
            const h2 = document.createElement('h2');
            h2.textContent = gatewayI18n.t('usage:period.label', {period: periodKey});
            fragment.appendChild(h2);

            const table = document.createElement('table');
            const thead = document.createElement('thead');
            const trHead = document.createElement('tr');
            [
                'usage:columns.gatewayModel',
                'usage:columns.providerHeader',
                'usage:columns.modelHeader',
                'usage:columns.errorType',
                'usage:columns.countHeader',
                'usage:columns.averageDuration',
            ].forEach(key => {
                const th = document.createElement('th');
                th.textContent = t(key);
                trHead.appendChild(th);
            });
            thead.appendChild(trHead);
            table.appendChild(thead);

            const tbody = document.createElement('tbody');
            groupedByPeriod[periodKey].forEach(row => {
                const tr = document.createElement('tr');

                const cells = [
                    row.gateway_model || notAvailable(),
                    row.provider || notAvailable(),
                    row.model || notAvailable(),
                ];
                cells.forEach(text => {
                    const td = document.createElement('td');
                    td.textContent = text;
                    markTechnical(td);
                    tr.appendChild(td);
                });

                // Error type badge
                const tdError = document.createElement('td');
                const badge = document.createElement('span');
                badge.className = `error-badge ${getErrorBadgeClass(row.error_type)}`;
                badge.textContent = formatErrorType(row.error_type);
                if (!ERROR_TYPE_LABELS[row.error_type]) markTechnical(badge);
                tdError.appendChild(badge);
                tr.appendChild(tdError);

                const tdCount = document.createElement('td');
                tdCount.textContent = formatNumber(row.count);
                tr.appendChild(tdCount);

                const tdDuration = document.createElement('td');
                tdDuration.textContent = formatDuration(row.avg_duration_ms);
                tr.appendChild(tdDuration);

                tbody.appendChild(tr);
            });
            table.appendChild(tbody);
            fragment.appendChild(table);
        }
        return fragment;
    };

    const fetchAndRenderFallbackStats = async (activationContext = null) => {
        const requestGeneration = startRequest('fallbackStats');
        startLoading('fallbackStats', requestGeneration);
        const selectedPeriod = fallbackPeriodSelector.value;
        fallbackStatsArea.textContent = gatewayI18n.t('usage:status.fallbackLoading');
        showMessage(message('usage:status.fallbackLoading'), 'busy');
        fallbackRefreshButton.disabled = true;
        fallbackRefreshButton.classList.add('loading');

        try {
            const response = await apiFetch(`/v1/api/fallback-stats/${selectedPeriod}`);
            if (!isCurrentRequest('fallbackStats', requestGeneration, activationContext) || !isFallbackLoadCurrent(activationContext)) return;
            const data = await response.json().catch(() => ({}));
            if (!isCurrentRequest('fallbackStats', requestGeneration, activationContext) || !isFallbackLoadCurrent(activationContext)) return;

            if (!response.ok) {
                showApiError(data, response);
                fallbackStatsArea.textContent = '';
                return;
            }

            snapshots.fallbackStats = Object.freeze({period: selectedPeriod, data});
            renderFallbackStatsSnapshot();
            showMessage(message(data.length === 0 ? 'usage:empty.fallback' : 'usage:status.fallbackLoaded'));
        } catch (error) {
            if (!isCurrentRequest('fallbackStats', requestGeneration, activationContext) || !isFallbackLoadCurrent(activationContext)) return;
            console.error('Failed to fetch fallback stats:', error);
            showApiError(null, null, error);
            fallbackStatsArea.textContent = '';
        } finally {
            finishLoading('fallbackStats', requestGeneration);
            if (!isCurrentRequest('fallbackStats', requestGeneration, activationContext) || !isFallbackLoadCurrent(activationContext)) return;
            fallbackRefreshButton.disabled = false;
            fallbackRefreshButton.classList.remove('loading');
        }
    };

    const renderFallbackStatsSnapshot = () => {
        if (!snapshots.fallbackStats) return;
        fallbackStatsArea.replaceChildren(createFallbackStatsTable(snapshots.fallbackStats.data));
    };

    const createFallbackChainsView = (records) => {
        const fragment = document.createDocumentFragment();
        if (!records || records.length === 0) {
            const p = document.createElement('p');
            p.className = 'empty-state';
            p.textContent = gatewayI18n.t('usage:empty.chains');
            fragment.appendChild(p);
            return fragment;
        }

        records.forEach((record, recordIndex) => {
            const chainId = String(record.request_id || `${fallbackCurrentPage}-${recordIndex}`);
            const detailsId = `fallback-chain-${fallbackCurrentPage}-${recordIndex}`;
            const initiallyOpen = expandedFallbackChains.has(chainId);
            const card = document.createElement('div');
            card.className = 'chain-card';
            card.classList.toggle('chain-card-open', initiallyOpen);

            // Header (clickable to expand)
            const header = document.createElement('div');
            header.className = 'chain-header';
            header.setAttribute('role', 'button');
            header.setAttribute('tabindex', '0');
            header.setAttribute('aria-expanded', String(initiallyOpen));
            header.setAttribute('aria-controls', detailsId);
            header.dataset.focusKey = `fallback-chain:${chainId}`;

            const summary = document.createElement('div');
            summary.className = 'chain-summary';

            const tsSpan = document.createElement('span');
            tsSpan.className = 'chain-timestamp';
            tsSpan.textContent = formatTimestamp(record.timestamp);
            summary.appendChild(tsSpan);

            const modelSpan = document.createElement('span');
            modelSpan.className = 'chain-model';
            modelSpan.textContent = record.gateway_model || notAvailable();
            markTechnical(modelSpan);
            summary.appendChild(modelSpan);

            const titleSpan = document.createElement('span');
            titleSpan.className = 'chain-title';
            titleSpan.textContent = gatewayI18n.t('usage:format.xTitle', {value: record.x_title || notAvailable()});
            titleSpan.setAttribute('dir', 'auto');
            summary.appendChild(titleSpan);

            const attSpan = document.createElement('span');
            attSpan.className = 'chain-attempts';
            attSpan.textContent = gatewayI18n.t('usage:counts.attempts', {count: Number(record.total_attempts) || 0});
            summary.appendChild(attSpan);

            const durSpan = document.createElement('span');
            durSpan.className = 'chain-duration';
            durSpan.textContent = formatDuration(record.total_duration_ms);
            summary.appendChild(durSpan);

            const resSpan = document.createElement('span');
            resSpan.className = `chain-result ${record.success ? 'chain-success' : 'chain-failure'}`;
            resSpan.textContent = record.success
                ? gatewayI18n.t('usage:values.successLabel')
                : gatewayI18n.t('usage:values.failedLabel');
            summary.appendChild(resSpan);

            const deniedTargets = accessDeniedTargets(record.attempts);
            if (deniedTargets.length > 0) {
                const deniedSpan = document.createElement('span');
                deniedSpan.className = 'chain-access-denied';
                deniedSpan.textContent = gatewayI18n.t('usage:values.accessDeniedChain', {
                    targets: deniedTargets.join(', '),
                });
                deniedSpan.setAttribute('dir', 'auto');
                summary.appendChild(deniedSpan);
            }

            header.appendChild(summary);

            const toggle = document.createElement('span');
            toggle.className = 'chain-toggle';
            toggle.textContent = initiallyOpen ? '\u25B2' : '\u25BC';
            header.appendChild(toggle);

            const details = document.createElement('div');
            details.className = 'chain-details';
            details.id = detailsId;
            details.style.display = initiallyOpen ? 'block' : 'none';

            // Find max duration for proportional bars
            const maxDuration = Math.max(...record.attempts.map(a => a.duration_ms), 1);

            record.attempts.forEach(attempt => {
                const row = document.createElement('div');
                row.className = `chain-attempt ${attempt.success ? 'attempt-success' : 'attempt-failure'}`;

                const barWidth = Math.max((attempt.duration_ms / maxDuration) * 100, 2);

                const numSpan = document.createElement('span');
                numSpan.className = 'attempt-number';
                numSpan.textContent = attempt.attempt_number;
                row.appendChild(numSpan);

                const targetWrap = document.createElement('div');
                targetWrap.className = 'attempt-target';

                const provSpan = document.createElement('span');
                provSpan.className = 'attempt-provider';
                provSpan.textContent = `${attempt.provider}/${attempt.model}`;
                markTechnical(provSpan);
                targetWrap.appendChild(provSpan);

                if (!attempt.success && attempt.error_message) {
                    const errMessage = document.createElement('div');
                    errMessage.className = 'attempt-error-message';
                    errMessage.textContent = attempt.error_message;
                    markTechnical(errMessage);
                    targetWrap.appendChild(errMessage);
                }

                row.appendChild(targetWrap);

                const errSpan = document.createElement('span');
                errSpan.className = 'attempt-error';
                if (!attempt.success) {
                    const badge = document.createElement('span');
                    badge.className = `error-badge ${getErrorBadgeClass(attempt.error_type)}`;
                    badge.textContent = formatErrorType(attempt.error_type);
                    if (!ERROR_TYPE_LABELS[attempt.error_type]) markTechnical(badge);
                    errSpan.appendChild(badge);
                }
                row.appendChild(errSpan);

                const barSpan = document.createElement('span');
                barSpan.className = 'attempt-duration-bar';
                const fill = document.createElement('span');
                fill.className = `duration-fill ${attempt.success ? 'fill-success' : 'fill-failure'}`;
                fill.style.width = `${barWidth}%`;
                barSpan.appendChild(fill);
                row.appendChild(barSpan);

                const durTextSpan = document.createElement('span');
                durTextSpan.className = 'attempt-duration-text';
                durTextSpan.textContent = formatDuration(attempt.duration_ms);
                row.appendChild(durTextSpan);

                const resAttemptSpan = document.createElement('span');
                resAttemptSpan.className = 'attempt-result';
                resAttemptSpan.textContent = attempt.success ? '\u2713' : '\u2717';
                row.appendChild(resAttemptSpan);

                details.appendChild(row);
            });

            const toggleDetails = () => {
                const isOpen = details.style.display !== 'none';
                details.style.display = isOpen ? 'none' : 'block';
                toggle.textContent = isOpen ? '\u25BC' : '\u25B2';
                card.classList.toggle('chain-card-open', !isOpen);
                header.setAttribute('aria-expanded', String(!isOpen));
                if (isOpen) expandedFallbackChains.delete(chainId);
                else expandedFallbackChains.add(chainId);
            };
            header.addEventListener('click', toggleDetails);
            header.addEventListener('keydown', (event) => {
                if (event.key !== 'Enter' && event.key !== ' ') return;
                event.preventDefault();
                toggleDetails();
            });

            card.appendChild(header);
            card.appendChild(details);
            fragment.appendChild(card);
        });

        return fragment;
    };

    const updateFallbackPagination = (totalRecords, page = fallbackCurrentPage) => {
        const totalPages = Math.ceil(totalRecords / fallbackRecordsPerPage);
        fallbackPageInfo.textContent = gatewayI18n.t('usage:pagination.page', {current: page, total: totalPages || 1});
        fallbackPrevPage.disabled = page === 1;
        fallbackNextPage.disabled = page === totalPages || totalRecords === 0;
    };

    const fetchAndRenderFallbackChains = async (activationContext = null) => {
        const requestGeneration = startRequest('fallbackChains');
        startLoading('fallbackChains', requestGeneration);
        const requestedPage = fallbackCurrentPage;
        fallbackChainsArea.textContent = gatewayI18n.t('usage:status.chainsLoading');
        showMessage(message('usage:status.chainsLoading'), 'busy');
        fallbackChainsRefreshButton.disabled = true;
        fallbackChainsRefreshButton.classList.add('loading');

        const offset = (requestedPage - 1) * fallbackRecordsPerPage;

        try {
            const response = await apiFetch(`/v1/api/fallback-records?limit=${fallbackRecordsPerPage}&offset=${offset}`);
            if (!isCurrentRequest('fallbackChains', requestGeneration, activationContext) || !isFallbackLoadCurrent(activationContext)) return;
            const responseData = await response.json().catch(() => ({}));
            if (!isCurrentRequest('fallbackChains', requestGeneration, activationContext) || !isFallbackLoadCurrent(activationContext)) return;

            if (!response.ok) {
                showApiError(responseData, response);
                fallbackChainsArea.textContent = '';
                updateFallbackPagination(0);
                return;
            }

            fallbackCurrentPage = requestedPage;
            snapshots.fallbackChains = Object.freeze({
                records: responseData.records,
                totalRecords: responseData.total_records,
                page: requestedPage,
            });
            renderFallbackChainsSnapshot();
            showMessage(message(responseData.records.length === 0 ? 'usage:empty.chains' : 'usage:status.chainsLoaded'));
        } catch (error) {
            if (!isCurrentRequest('fallbackChains', requestGeneration, activationContext) || !isFallbackLoadCurrent(activationContext)) return;
            console.error('Failed to fetch fallback chains:', error);
            showApiError(null, null, error);
            fallbackChainsArea.textContent = '';
            updateFallbackPagination(0);
        } finally {
            finishLoading('fallbackChains', requestGeneration);
            if (!isCurrentRequest('fallbackChains', requestGeneration, activationContext) || !isFallbackLoadCurrent(activationContext)) return;
            fallbackChainsRefreshButton.disabled = false;
            fallbackChainsRefreshButton.classList.remove('loading');
        }
    };

    const renderFallbackChainsSnapshot = () => {
        if (!snapshots.fallbackChains) return;
        fallbackCurrentPage = snapshots.fallbackChains.page;
        fallbackChainsArea.replaceChildren(createFallbackChainsView(snapshots.fallbackChains.records));
        updateFallbackPagination(
            snapshots.fallbackChains.totalRecords,
            snapshots.fallbackChains.page,
        );
    };

    const createUpstreamStatsTable = (data) => {
        const fragment = document.createDocumentFragment();
        if (!data || data.length === 0) {
            const p = document.createElement('p');
            p.className = 'empty-state';
            p.textContent = gatewayI18n.t('usage:empty.upstream');
            fragment.appendChild(p);
            return fragment;
        }

        const groupedByPeriod = data.reduce((acc, row) => {
            const key = row.time_period;
            if (!acc[key]) acc[key] = [];
            acc[key].push(row);
            return acc;
        }, {});

        for (const periodKey in groupedByPeriod) {
            const h2 = document.createElement('h2');
            h2.textContent = gatewayI18n.t('usage:period.label', {period: periodKey});
            fragment.appendChild(h2);

            const table = document.createElement('table');
            const thead = document.createElement('thead');
            const trHead = document.createElement('tr');
            [
                'usage:columns.gatewayModel',
                'usage:columns.providerHeader',
                'usage:columns.modelHeader',
                'usage:columns.operationHeader',
                'usage:columns.key',
                'usage:columns.attemptsHeader',
                'usage:columns.successHeader',
                'usage:columns.errorsHeader',
                'usage:columns.successRate',
                'usage:columns.averageDuration',
                'usage:columns.maxDuration',
            ].forEach(key => {
                const th = document.createElement('th');
                th.textContent = t(key);
                trHead.appendChild(th);
            });
            thead.appendChild(trHead);
            table.appendChild(thead);

            const tbody = document.createElement('tbody');
            groupedByPeriod[periodKey].forEach(row => {
                const tr = document.createElement('tr');
                const successRate = typeof row.success_rate === 'number'
                    ? formatNumber(row.success_rate / 100, {
                        style: 'percent',
                        minimumFractionDigits: 1,
                        maximumFractionDigits: 1,
                    })
                    : notAvailable();
                const cells = [
                    row.gateway_model || notAvailable(),
                    row.provider || notAvailable(),
                    row.model || notAvailable(),
                    row.operation || notAvailable(),
                    row.upstream_key_fingerprint || notAvailable(),
                    formatNumber(row.attempts),
                    formatNumber(row.successes),
                    formatNumber(row.errors),
                    successRate,
                    formatDuration(row.avg_duration_ms || 0),
                    formatDuration(row.max_duration_ms || 0),
                ];
                cells.forEach((value, index) => {
                    const td = document.createElement('td');
                    td.textContent = value;
                    if (index < 5) markTechnical(td);
                    tr.appendChild(td);
                });
                tbody.appendChild(tr);
            });
            table.appendChild(tbody);
            fragment.appendChild(table);
        }
        return fragment;
    };

    const fetchAndRenderUpstreamStats = async (activationContext = null) => {
        const requestGeneration = startRequest('upstream');
        startLoading('upstream', requestGeneration);
        const selectedPeriod = upstreamPeriodSelector.value;
        upstreamStatsArea.textContent = gatewayI18n.t('usage:status.upstreamLoading');
        showMessage(message('usage:status.upstreamLoading'), 'busy');
        upstreamRefreshButton.disabled = true;
        upstreamRefreshButton.classList.add('loading');

        try {
            const response = await apiFetch(`/v1/api/upstream-stats/${selectedPeriod}`);
            if (!isCurrentRequest('upstream', requestGeneration, activationContext) || !isFallbackLoadCurrent(activationContext)) return;
            const data = await response.json().catch(() => ({}));
            if (!isCurrentRequest('upstream', requestGeneration, activationContext) || !isFallbackLoadCurrent(activationContext)) return;

            if (!response.ok) {
                showApiError(data, response);
                upstreamStatsArea.textContent = '';
                return;
            }

            snapshots.upstream = Object.freeze({period: selectedPeriod, data});
            renderUpstreamSnapshot();
            showMessage(message(data.length === 0 ? 'usage:empty.upstream' : 'usage:status.upstreamLoaded'));
        } catch (error) {
            if (!isCurrentRequest('upstream', requestGeneration, activationContext) || !isFallbackLoadCurrent(activationContext)) return;
            console.error('Failed to fetch upstream analytics:', error);
            showApiError(null, null, error);
            upstreamStatsArea.textContent = '';
        } finally {
            finishLoading('upstream', requestGeneration);
            if (!isCurrentRequest('upstream', requestGeneration, activationContext) || !isFallbackLoadCurrent(activationContext)) return;
            upstreamRefreshButton.disabled = false;
            upstreamRefreshButton.classList.remove('loading');
        }
    };

    const renderUpstreamSnapshot = () => {
        if (!snapshots.upstream) return;
        upstreamStatsArea.replaceChildren(createUpstreamStatsTable(snapshots.upstream.data));
    };

    fallbackRefreshButton.addEventListener('click', () => reloadFallbackSubTab('summary'));
    fallbackPeriodSelector.addEventListener('change', () => reloadFallbackSubTab('summary'));
    upstreamRefreshButton.addEventListener('click', () => reloadFallbackSubTab('upstream'));
    upstreamPeriodSelector.addEventListener('change', () => reloadFallbackSubTab('upstream'));

    fallbackChainsRefreshButton.addEventListener('click', () => {
        fallbackCurrentPage = 1;
        reloadFallbackSubTab('chains');
    });

    fallbackPrevPage.addEventListener('click', () => {
        if (fallbackCurrentPage > 1) {
            fallbackCurrentPage--;
            fetchAndRenderFallbackChains(fallbackChainsActivationContext);
        }
    });

    fallbackNextPage.addEventListener('click', () => {
        const totalPages = Math.ceil((snapshots.fallbackChains?.totalRecords || 0) / fallbackRecordsPerPage);
        // Fail closed: block navigation until page count is known.
        if (!totalPages || fallbackCurrentPage >= totalPages) return;
        fallbackCurrentPage++;
        fetchAndRenderFallbackChains(fallbackChainsActivationContext);
    });

    // Ensure initial load respects the default tab
    if (usageTabsController) {
        reportTabActivation(usageTabsController.activate(usageTabsController.activeKey));
    }

    // ── Topology tab: routing map + live monitor ───────────────────────────────
    const topologyContainer = document.getElementById('topology-container');
    const topologyRefreshButton = document.getElementById('topologyRefreshButton');
    const topologyAutoRefreshInput = document.getElementById('topologyAutoRefresh');

    const HEALTH_BORDER_COLOR = {
        ok: 'var(--success)',
        error: 'var(--error)',
        invalid: 'var(--border-strong)',
    };
    // Edge colors must stay concrete hex: ReactFlow writes them into SVG
    // marker attributes where CSS variables are not substituted.
    const EDGE_COLOR = { alias: '#94a3b8', fallback: '#4f46e5', context_overflow: '#d97706' };
    const TOPOLOGY_REFRESH_MS = 5000;

    let _topologyRootInstance = null;
    let _topologyMounted = false;
    // Published by the mounted React app so the external toolbar (Refresh button,
    // Auto-refresh checkbox) can drive it without remounting (which would reset
    // the pan/zoom viewport and any selected chain).
    let _topologyApi = null;

    // React Flow ships its layout-critical CSS as a sibling file alongside the
    // JS bundle. Without it nodes render with `position: static` and stack
    // vertically instead of honouring their `transform: translate(...)`.
    function ensureTopologyStylesLoaded() {
        if (document.querySelector('link[data-topology-styles]')) return;
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = '/static/vendor/topology.bundle.css';
        link.setAttribute('data-topology-styles', '1');
        document.head.appendChild(link);
    }

    function showTopologyFatalError(summaryKey, rawDetail, onRetry) {
        topologyContainer.replaceChildren();
        const errDiv = document.createElement('div');
        errDiv.className = 'topology-error';
        errDiv.setAttribute('role', 'alert');
        errDiv.textContent = t(summaryKey);
        if (rawDetail) {
            const detail = document.createElement('div');
            detail.textContent = rawDetail;
            markTechnical(detail);
            errDiv.appendChild(detail);
        }
        const retryBtn = document.createElement('button');
        retryBtn.textContent = gatewayI18n.t('topology:status.retryAction');
        retryBtn.addEventListener('click', onRetry);
        errDiv.appendChild(retryBtn);
        topologyContainer.appendChild(errDiv);
    }

    // Layered left→right layout in container-pixel space: gateway | models |
    // providers. Computed at zoom=1 so it reads correctly without fitView
    // (which races with the display:none → visible tab transition).
    function topologyLayout(graph, size) {
        const nodes = graph.nodes || [];
        const central = nodes.filter(n => n.type === 'central');
        const models = nodes.filter(n => n.type === 'model');
        const providers = nodes.filter(n => n.type === 'provider');
        const NODE_W = 170;
        const W = size.w || 1000;
        const H = size.h || 520;
        const gatewayX = 24;
        const modelX = Math.max(210, Math.round(W * 0.40));
        const providerX = Math.max(modelX + 220, W - NODE_W - 30);
        const pos = new Map();
        const place = (list, x) => {
            const gap = 92;
            const totalH = Math.max(0, (list.length - 1) * gap);
            const startY = Math.max(16, (H - totalH) / 2 - 24);
            list.forEach((n, i) => pos.set(n.id, { x, y: Math.round(startY + i * gap) }));
        };
        place(central, gatewayX);
        place(models, modelX);
        place(providers, providerX);
        return pos;
    }

    // Which nodes/edges belong to the selected gateway-model's chain.
    function topologyChainSets(graph, selectedModel) {
        if (!selectedModel) return null;
        const edgeIds = new Set();
        const nodeIds = new Set([selectedModel, 'gateway']);
        (graph.edges || []).forEach(e => {
            if (e.source === selectedModel) { edgeIds.add(e.id); nodeIds.add(e.target); }
            if (e.target === selectedModel && e.source === 'gateway') edgeIds.add(e.id);
        });
        return { edgeIds, nodeIds };
    }

    function topologyStyledNode(React, node, position, dimmed, selected) {
        const h = React.createElement;
        const isCentral = node.type === 'central';
        const isModel = node.type === 'model';
        const d = node.data || {};
        const health = d.health || 'ok';
        const active = d.active_requests || 0;
        const hasActive = active > 0;

        const borderColor = isCentral ? 'var(--accent-hover)'
            : isModel ? 'var(--accent)'
            : (HEALTH_BORDER_COLOR[health] || 'var(--border-strong)');

        const lines = [];
        if (isModel) {
            const steps = (d.chain || []).length;
            lines.push(t('topology:node.steps', {count: steps}));
            if (hasActive) lines.push(`▶ ${t('topology:node.active', {count: active})}`);
        } else if (!isCentral) {
            const healthKeys = {
                ok: 'topology:health.healthyLabel',
                error: 'topology:health.errorLabel',
                invalid: 'topology:health.invalidLabel',
            };
            const healthText = healthKeys[health] ? t(healthKeys[health]) : health;
            const configuredSuffix = d.configured === false
                ? ` (${t('topology:node.notConfiguredSuffix')})`
                : '';
            lines.push(t('topology:node.health', {value: `${healthText}${configuredSuffix}`}));
            if (hasActive) lines.push(`▶ ${t('topology:node.active', {count: active})}`);
            lines.push(t('topology:node.penalty', {
                value: formatNumber(typeof d.penalty === 'number' ? d.penalty : 0, {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                }),
            }));
        }

        const label = h('div', null,
            h('div', { className: 'topology-node-title', lang: 'und', dir: 'auto' }, node.label || node.id),
            lines.length
                ? h('div', { className: 'topology-node-metrics' },
                    lines.map((line, idx) => h('div', { key: idx }, line)))
                : null
        );

        return {
            id: node.id,
            type: isCentral ? 'input' : (isModel ? 'default' : 'output'),
            position: position || node.position || { x: 0, y: 0 },
            sourcePosition: 'right',
            targetPosition: 'left',
            connectable: false,
            ariaLabel: t('topology:node.aria', {
                label: node.label || node.id,
                details: lines.join('. '),
            }),
            data: { label },
            style: {
                background: isCentral ? 'var(--accent)' : (isModel ? 'var(--accent-soft)' : undefined),
                borderColor,
                borderWidth: 2,
                borderStyle: 'solid',
                borderRadius: 8,
                padding: '8px 14px',
                width: 170,
                textAlign: 'center',
                color: isCentral ? 'var(--accent-contrast)' : undefined,
                fontWeight: isCentral ? 700 : 600,
                fontSize: 13,
                cursor: isCentral ? 'default' : 'pointer',
                opacity: dimmed ? 0.25 : 1,
                transition: 'opacity 0.2s, box-shadow 0.2s',
                boxShadow: selected ? '0 0 0 3px color-mix(in srgb, var(--accent) 45%, transparent)' : undefined,
                animation: (hasActive && !isCentral) ? 'topology-pulse 1.8s ease-out infinite' : undefined,
            },
        };
    }

    function topologyStyledEdge(edge, chain) {
        const d = edge.data || {};
        const kind = edge.type;
        const inChain = chain ? chain.edgeIds.has(edge.id) : true;
        const dimmed = chain && !inChain;
        const isCtx = kind === 'context_overflow';
        const isAlias = kind === 'alias';
        const stroke = EDGE_COLOR[kind] || EDGE_COLOR.fallback;

        let label;
        if (kind === 'fallback') {
            label = (d.steps && d.steps.length) ? d.steps.join(',') : String(d.priority || '');
        } else if (isCtx) {
            label = t('topology:edge.contextOverflowShort');
        }

        const kindKeys = {
            alias: 'topology:edge.aliasLabel',
            fallback: 'topology:edge.fallbackLabel',
            context_overflow: 'topology:edge.contextOverflowLabel',
        };
        const kindLabel = kindKeys[kind] ? t(kindKeys[kind]) : kind;

        const emphasised = (chain && inChain) || edge.animated;
        const baseWidth = isAlias ? 1.5 : 2;

        return {
            id: edge.id,
            source: edge.source,
            target: edge.target,
            type: 'default',
            animated: !!edge.animated,
            label,
            ariaLabel: t('topology:edge.aria', {
                source: edge.source,
                target: edge.target,
                kind: kindLabel,
            }),
            labelStyle: { fontSize: 11, fontWeight: 700, fill: stroke },
            labelBgStyle: { fill: 'var(--bg-elevated)', fillOpacity: 0.85 },
            labelBgPadding: [4, 2],
            labelBgBorderRadius: 4,
            markerEnd: { type: 'arrowclosed', color: stroke },
            style: {
                stroke,
                strokeWidth: emphasised ? baseWidth + 1.5 : baseWidth,
                strokeDasharray: isCtx ? '6 4' : undefined,
                opacity: dimmed ? 0.15 : 1,
                transition: 'opacity 0.2s',
            },
        };
    }

    function topologyBuildGraph(React, graph, selectedModel, size) {
        if (!graph) return { nodes: [], edges: [] };
        const pos = topologyLayout(graph, size);
        const chain = topologyChainSets(graph, selectedModel);
        const nodes = (graph.nodes || []).map(node =>
            topologyStyledNode(React, node, pos.get(node.id),
                chain && !chain.nodeIds.has(node.id), node.id === selectedModel));
        const edges = (graph.edges || []).map(edge => topologyStyledEdge(edge, chain));
        return { nodes, edges };
    }

    function topologyDetailPanel(React, detail, onClose) {
        const h = React.createElement;
        const node = detail.node;
        const d = node.data || {};
        const kv = (k, v, technical = false) => h('div', { className: 'topology-detail-row', key: k },
            h('span', { className: 'topology-detail-key' }, k),
            h('span', {
                className: 'topology-detail-val',
                lang: technical ? 'und' : undefined,
                dir: technical ? 'auto' : undefined,
            }, v));

        let body;
        if (detail.kind === 'provider') {
            const healthKey = {
                ok: 'topology:health.healthyLabel',
                error: 'topology:health.errorLabel',
                invalid: 'topology:health.invalidLabel',
            }[d.health];
            body = [
                kv(
                    t('topology:details.healthLabel'),
                    healthKey ? t(healthKey) : String(d.health || notAvailable()),
                    !healthKey,
                ),
                kv(t('topology:details.configuredLabel'), d.configured === false
                    ? t('topology:values.noLabel')
                    : t('topology:values.yesLabel')),
                kv(t('topology:details.activeRequests'), formatNumber(d.active_requests || 0)),
                kv(t('topology:details.penaltyLabel'), formatNumber(
                    typeof d.penalty === 'number' ? d.penalty : 0,
                    {minimumFractionDigits: 2, maximumFractionDigits: 2},
                )),
                (d.models && d.models.length)
                    ? kv(t('topology:details.modelsLabel'), d.models.join(', '), true)
                    : null,
            ];
        } else {
            const flags = d.flags || {};
            const flagList = Object.keys(flags).filter(k => flags[k]);
            const chainItems = (d.chain || []).map((step, i) =>
                h('li', { key: i, className: 'topology-chain-step' },
                    h('span', { className: 'topology-chain-prio' }, `#${step.priority}`),
                    h('span', { className: 'topology-chain-target', lang: 'und', dir: 'auto' }, `${step.provider || '?'} / ${step.model || '?'}`),
                    (step.retry_count != null || step.retry_delay != null)
                        ? h('span', { className: 'topology-chain-retry' },
                            step.retry_delay != null
                                ? t('topology:details.retryWithDelay', {
                                    count: step.retry_count != null ? formatNumber(step.retry_count) : '-',
                                    delay: t('usage:format.milliseconds', {value: formatNumber(step.retry_delay)}),
                                })
                                : t('topology:details.retry', {
                                    count: step.retry_count != null ? formatNumber(step.retry_count) : '-',
                                }))
                        : null
                )
            );
            body = [
                kv(t('topology:details.activeRequests'), formatNumber(d.active_requests || 0)),
                (d.max_total_attempts != null)
                    ? kv(t('topology:details.maxAttempts'), formatNumber(d.max_total_attempts))
                    : null,
                flagList.length ? kv(t('topology:details.flagsLabel'), flagList.join(', '), true) : null,
                d.context_overflow
                    ? kv(
                        t('topology:details.contextOverflowLabel'),
                        `${d.context_overflow.provider} / ${d.context_overflow.model || '?'}`,
                        true,
                    )
                    : null,
                h('div', { className: 'topology-detail-subhead', key: 'chainhead' }, t('topology:details.fallbackChainLabel')),
                h('ol', { className: 'topology-chain-list', key: 'chain' },
                    chainItems.length ? chainItems : h('li', { key: 'empty' }, t('topology:details.emptyChain'))),
            ];
        }

        return h('div', { className: 'topology-detail' },
            h('div', { className: 'topology-detail-header' },
                h('span', { className: 'topology-detail-title', lang: 'und', dir: 'auto' }, node.label || node.id),
                h('button', {
                    className: 'topology-detail-close',
                    onClick: onClose,
                    title: t('topology:details.closeAction'),
                    'aria-label': t('topology:details.closeAction'),
                }, '×')
            ),
            h('div', { className: 'topology-detail-body' }, body.filter(Boolean))
        );
    }

    function TopologyApp(props) {
        const { React, ReactFlow, Background, Controls } = props;
        const {
            useState,
            useEffect,
            useLayoutEffect,
            useCallback,
            useMemo,
            useRef,
            createElement: h,
        } = React;

        const [graph, setGraph] = useState(null);
        const [error, setError] = useState(null);
        const [selectedModel, setSelectedModel] = useState(null);
        const [detail, setDetail] = useState(null);
        const [auto, setAuto] = useState(topologyAutoRefreshInput ? topologyAutoRefreshInput.checked : true);
        const [localeRevision, setLocaleRevision] = useState(0);
        const fetchGeneration = useRef(0);
        const [size, setSize] = useState({
            w: topologyContainer.clientWidth || 1000,
            h: topologyContainer.clientHeight || 520,
        });

        const fetchGraph = useCallback(async () => {
            const requestGeneration = ++fetchGeneration.current;
            try {
                const resp = await apiFetch('/v1/topology');
                if (requestGeneration !== fetchGeneration.current) return;
                if (!resp.ok) {
                    const errBody = await resp.json().catch(() => ({}));
                    if (requestGeneration !== fetchGeneration.current) return;
                    setError(gatewayUi.describeApiError(errBody, {
                        status: resp.status,
                        requestId: resp.headers.get('X-Request-ID'),
                    }));
                    return;
                }
                const nextGraph = await resp.json();
                if (requestGeneration !== fetchGeneration.current) return;
                setGraph(nextGraph);
                setError(null);
            } catch (e) {
                if (requestGeneration !== fetchGeneration.current) return;
                console.error('Failed to fetch topology:', e);
                const descriptor = gatewayUi.describeApiError(null);
                setError({...descriptor, rawDetail: e.message});
            }
        }, []);

        useEffect(() => { fetchGraph(); }, [fetchGraph]);

        useEffect(() => {
            if (!auto) return undefined;
            const id = setInterval(() => {
                // Skip polling while the tab is hidden (display:none → offsetParent null).
                if (topologyContainer.offsetParent === null) return;
                fetchGraph();
            }, TOPOLOGY_REFRESH_MS);
            return () => clearInterval(id);
        }, [auto, fetchGraph]);

        useEffect(() => {
            const measure = () => setSize({
                w: topologyContainer.clientWidth || 1000,
                h: topologyContainer.clientHeight || 520,
            });
            measure();
            window.addEventListener('resize', measure);
            return () => window.removeEventListener('resize', measure);
        }, []);

        // Expose refresh / auto-toggle to the external toolbar controls.
        useEffect(() => {
            _topologyApi = {
                refresh: fetchGraph,
                setAuto,
                rerenderLocale: () => setLocaleRevision(revision => revision + 1),
            };
            return () => { _topologyApi = null; };
        }, [fetchGraph]);

        useLayoutEffect(() => {
            const controls = topologyContainer.querySelector('.react-flow__controls');
            if (!controls) return;
            controls.setAttribute('aria-label', gatewayI18n.t('topology:controls.groupLabel'));
            const labels = [
                ['.react-flow__controls-zoomin', 'topology:controls.zoomInAction'],
                ['.react-flow__controls-zoomout', 'topology:controls.zoomOutAction'],
                ['.react-flow__controls-fitview', 'topology:controls.fitViewAction'],
                ['.react-flow__controls-interactive', 'topology:controls.toggleInteractivityAction'],
            ];
            labels.forEach(([selector, key]) => {
                const button = controls.querySelector(selector);
                if (!button) return;
                const label = t(key);
                button.setAttribute('aria-label', label);
                button.setAttribute('title', label);
            });
        }, [graph, localeRevision]);

        const onNodeClick = useCallback((_evt, node) => {
            const raw = ((graph && graph.nodes) || []).find(n => n.id === node.id);
            if (!raw) return;
            if (raw.type === 'model') {
                setSelectedModel(raw.id);
                setDetail({ kind: 'model', node: raw });
            } else if (raw.type === 'provider') {
                setSelectedModel(null);
                setDetail({ kind: 'provider', node: raw });
            } else {
                setSelectedModel(null);
                setDetail(null);
            }
        }, [graph]);

        const onPaneClick = useCallback(() => {
            setSelectedModel(null);
            setDetail(null);
        }, []);

        const styled = useMemo(
            () => topologyBuildGraph(React, graph, selectedModel, size),
            [React, graph, selectedModel, size, localeRevision]
        );

        if (error) {
            return h('div', { className: 'topology-error' },
                h('div', {role: 'alert'}, t(error.summaryKey, error.summaryValues)),
                error.rawDetail
                    ? h('div', {lang: 'und', dir: 'auto'}, error.rawDetail)
                    : null,
                error.requestId
                    ? h('div', {lang: 'und', dir: 'auto'}, t('usage:status.requestId', {id: error.requestId}))
                    : null,
                h('button', { onClick: fetchGraph }, t('topology:status.retryAction')));
        }

        return h('div', { className: 'topology-body' },
            h('div', { className: 'topology-flow' },
                graph
                    ? h(ReactFlow, {
                        nodes: styled.nodes,
                        edges: styled.edges,
                        nodesDraggable: false,
                        minZoom: 0.2,
                        onNodeClick,
                        onPaneClick,
                        attributionPosition: 'bottom-right',
                    }, h(Background, null), h(Controls, {
                        'aria-label': gatewayI18n.t('topology:controls.groupLabel'),
                    }))
                    : h('div', { className: 'topology-loading' }, t('topology:status.loading'))
            ),
            detail ? topologyDetailPanel(React, detail, () => { setDetail(null); setSelectedModel(null); }) : null
        );
    }

    async function loadTopology(activationContext = null) {
        ensureTopologyStylesLoaded();
        // Mount once; subsequent activations just trigger a refresh so the
        // viewport and any selected chain survive tab switches.
        if (_topologyMounted) {
            if (_topologyApi) _topologyApi.refresh();
            return;
        }
        topologyContainer.textContent = gatewayI18n.t('topology:status.loading');

        let bundle;
        try {
            bundle = await import('/static/vendor/topology.bundle.mjs');
            if (!isActivationCurrent(activationContext)) return;
        } catch (bundleError) {
            if (!isActivationCurrent(activationContext)) return;
            console.error('Topology bundle load failed:', bundleError);
            showTopologyFatalError('topology:status.bundleFailed', bundleError.message, loadTopology);
            return;
        }

        const { React, createRoot, ReactFlow, Background, Controls } = bundle;
        topologyContainer.replaceChildren();
        const mountEl = document.createElement('div');
        mountEl.style.width = '100%';
        mountEl.style.height = '100%';
        topologyContainer.appendChild(mountEl);

        _topologyRootInstance = createRoot(mountEl);
        _topologyRootInstance.render(
            React.createElement(TopologyApp, { React, ReactFlow, Background, Controls })
        );
        _topologyMounted = true;
    }

    topologyRefreshButton.addEventListener('click', () => {
        if (_topologyApi) _topologyApi.refresh(); else loadTopology();
    });
    if (topologyAutoRefreshInput) {
        topologyAutoRefreshInput.addEventListener('change', () => {
            if (_topologyApi) _topologyApi.setAuto(topologyAutoRefreshInput.checked);
        });
    }

    function captureLocaleUiState() {
        const activeElement = document.activeElement;
        const focusId = activeElement?.id || null;
        const focusKey = activeElement?.dataset?.focusKey || null;
        const scrollElements = [
            statsArea,
            recordsArea,
            fallbackStatsArea,
            fallbackChainsArea,
            upstreamStatsArea,
            ...document.querySelectorAll('.analytics-table-wrap'),
        ];
        return {
            activeElement,
            focusId,
            focusKey,
            scrollX: window.scrollX,
            scrollY: window.scrollY,
            scrollPositions: scrollElements.map(element => ({
                element,
                left: element.scrollLeft,
                top: element.scrollTop,
            })),
        };
    }

    function restoreLocaleUiState(state) {
        state.scrollPositions.forEach(({element, left, top}) => {
            if (!element.isConnected) return;
            element.scrollLeft = left;
            element.scrollTop = top;
        });
        window.requestAnimationFrame(() => {
            window.scrollTo(state.scrollX, state.scrollY);
            let focusTarget = state.activeElement?.isConnected ? state.activeElement : null;
            if (!focusTarget && state.focusId) focusTarget = document.getElementById(state.focusId);
            if (!focusTarget && state.focusKey) {
                focusTarget = Array.from(document.querySelectorAll('[data-focus-key]'))
                    .find(element => element.dataset.focusKey === state.focusKey) || null;
            }
            focusTarget?.focus({preventScroll: true});
        });
    }

    function rerenderLocale() {
        const uiState = captureLocaleUiState();
        if (loadingGenerations.stats !== null) {
            statsArea.textContent = gatewayI18n.t('usage:status.statisticsLoading');
        } else {
            renderStatsSnapshot();
        }
        if (loadingGenerations.records !== null) {
            recordsArea.textContent = gatewayI18n.t('usage:status.recordsLoading');
        } else {
            renderRecordsSnapshot();
        }
        if (loadingGenerations.fallbackStats !== null) {
            fallbackStatsArea.textContent = gatewayI18n.t('usage:status.fallbackLoading');
        } else {
            renderFallbackStatsSnapshot();
        }
        if (loadingGenerations.fallbackChains !== null) {
            fallbackChainsArea.textContent = gatewayI18n.t('usage:status.chainsLoading');
        } else {
            renderFallbackChainsSnapshot();
        }
        if (loadingGenerations.upstream !== null) {
            upstreamStatsArea.textContent = gatewayI18n.t('usage:status.upstreamLoading');
        } else {
            renderUpstreamSnapshot();
        }
        window.usageAnalyticsDashboard.rerenderLocale();
        _topologyApi?.rerenderLocale();
        pageStatus.rerender();
        renderRequestId(currentRequestId);
        restoreLocaleUiState(uiState);
    }

    updatePaginationControls(0);
    updateFallbackPagination(0);
    unsubscribeLocale = gatewayI18n.subscribe(rerenderLocale);
    window.addEventListener('pagehide', () => unsubscribeLocale?.(), {once: true});

}
