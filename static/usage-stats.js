document.addEventListener('DOMContentLoaded', () => {
    const { apiFetch } = window.gatewayAuth;

    const periodSelector = document.getElementById('periodSelector');
    const refreshButton = document.getElementById('refreshButton');
    const statsArea = document.getElementById('statsArea');
    const messageArea = document.getElementById('messageArea');
    Theme.attachToggle('darkModeToggle');

    // --- Fetch and Render Statistics Logic ---
    const showMessage = (message, isError = false) => {
        messageArea.textContent = message;
        messageArea.className = isError ? 'error' : '';
    };

    const formatResolvedTarget = (row) => {
        const provider = typeof row?.provider === 'string' ? row.provider.trim() : '';
        const model = typeof row?.model === 'string' ? row.model.trim() : '';

        if (provider && model) {
            return `${provider}/${model}`;
        }
        return model || provider || 'N/A';
    };

    const formatGatewayModel = (row) => {
        const gatewayModel = typeof row?.gateway_model === 'string' ? row.gateway_model.trim() : '';
        return gatewayModel || 'N/A';
    };

    const formatOperation = (row) => {
        const operation = typeof row?.operation === 'string' ? row.operation.trim() : '';
        return operation || 'N/A';
    };

    const formatTimestamp = (value) => {
        const timestamp = typeof value === 'string' ? value.trim() : '';
        if (!timestamp) {
            return 'N/A';
        }
        return timestamp.split('.')[0] || timestamp;
    };

    const isRunningUsageRecord = (row) => {
        const status = typeof row?.status === 'string' ? row.status.trim().toLowerCase() : '';
        return status === 'running' || status === 'in_progress';
    };

    const createStatsTable = (data) => {
        const fragment = document.createDocumentFragment();
        if (!data || data.length === 0) {
            const p = document.createElement('p');
            p.className = 'empty-state';
            p.textContent = 'No data available for the selected period.';
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
            h2.textContent = `Period: ${periodKey}`;
            fragment.appendChild(h2);

            const table = document.createElement('table');
            const thead = document.createElement('thead');
            const trHead = document.createElement('tr');
            
            const thGatewayModel = document.createElement('th');
            thGatewayModel.textContent = 'Gateway Model';
            trHead.appendChild(thGatewayModel);

            const thResolvedModel = document.createElement('th');
            thResolvedModel.textContent = 'Resolved Model';
            trHead.appendChild(thResolvedModel);

            const thOperation = document.createElement('th');
            thOperation.textContent = 'Operation';
            trHead.appendChild(thOperation);

            metrics.forEach(metric => {
                const th = document.createElement('th');
                let displayMetric = metric.replace('_', ' ').split(' ') 
                                      .map(word => word.charAt(0).toUpperCase() + word.slice(1))
                                      .join(' ');
                if (metric === 'cost_per_million') {
                    displayMetric = 'Cost/Million';
                }
                if (metric === 'cost_saved') {
                    displayMetric = 'Cost Saved ($)';
                }
                if (metric === 'cost') {
                    displayMetric = 'Cost ($)';
                }
                th.textContent = displayMetric;
                trHead.appendChild(th);
            });
            thead.appendChild(trHead);
            table.appendChild(thead);

            const tbody = document.createElement('tbody');
            groupedByPeriod[periodKey].forEach(row => {
                const tr = document.createElement('tr');
                const tdGatewayModel = document.createElement('td');
                tdGatewayModel.textContent = formatGatewayModel(row);
                tr.appendChild(tdGatewayModel);

                const tdResolvedModel = document.createElement('td');
                tdResolvedModel.textContent = formatResolvedTarget(row);
                tr.appendChild(tdResolvedModel);

                const tdOperation = document.createElement('td');
                tdOperation.textContent = formatOperation(row);
                tr.appendChild(tdOperation);

                metrics.forEach(metric => {
                    let value = row[metric];
                    if (metric === 'cost_per_million') {
                        const total_cost = row.cost || 0;
                        const total_tokens = row.total_tokens || 0;
                        const costPerMillion = total_tokens > 0 ? (total_cost / total_tokens) * 1000000 : 0;
                        value = costPerMillion.toFixed(4);
                    }
                    if (metric === 'cost' && typeof value === 'number') {
                        value = value.toFixed(4);
                    }
                    if (metric === 'cost_saved' && typeof value === 'number') {
                        value = value.toFixed(4);
                    }
                    const td = document.createElement('td');
                    td.textContent = typeof value === 'number' ? value.toLocaleString() : value;
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
        const selectedPeriod = periodSelector.value;
        statsArea.textContent = 'Loading statistics...';
        showMessage('');
        
        refreshButton.disabled = true;
        refreshButton.classList.add('loading');

        try {
            const response = await apiFetch(`/v1/api/usage-stats/${selectedPeriod}`);
            const data = await response.json();

            if (!response.ok) {
                const errorMessage = data.detail ? data.detail : `Error: ${response.status} ${response.statusText}`;
                showMessage(errorMessage, true);
                statsArea.textContent = '';
                return;
            }
            
            statsArea.textContent = "";
            statsArea.appendChild(createStatsTable(data));
            if (data.length === 0) {
                 showMessage('No data available for the selected period.', false);
            } else {
                showMessage('Statistics loaded successfully.');
            }

        } catch (error) {
            console.error('Failed to fetch usage statistics:', error);
            showMessage(`Failed to load statistics: ${error.message}`, true);
            statsArea.textContent = '';
        } finally {
            refreshButton.disabled = false;
            refreshButton.classList.remove('loading');
        }
    };

    // Event Listeners
    refreshButton.addEventListener('click', fetchAndRenderStats);
    periodSelector.addEventListener('change', fetchAndRenderStats);

    // --- Tab Switching Logic ---
    const tabButtons = document.querySelectorAll('.tab-button');
    const tabContents = document.querySelectorAll('.tab-content');

    tabButtons.forEach(button => {
        button.addEventListener('click', () => {
            const tab = button.dataset.tab;

            tabButtons.forEach(btn => btn.classList.remove('active'));
            tabContents.forEach(content => content.classList.remove('active'));

            button.classList.add('active');
            document.getElementById(`${tab}TabContent`).classList.add('active');

            // Load data for the active tab
            if (tab === 'records') {
                currentPage = 1;
                fetchAndRenderRecords();
            } else if (tab === 'analytics') {
                window.usageAnalyticsDashboard.activate();
            } else if (tab === 'fallback') {
                loadActiveFallbackSubTab();
            } else if (tab === 'topology') {
                loadTopology();
            } else {
                fetchAndRenderStats();
            }
        });
    });

    // --- Usage Records Logic (New) ---
    const recordsArea = document.getElementById('recordsArea');
    const prevPageButton = document.getElementById('prevPage');
    const nextPageButton = document.getElementById('nextPage');
    const recordRefreshButton = document.getElementById('recordRefreshButton');
    const pageInfoSpan = document.getElementById('pageInfo');
    const recordsPerPage = 25; // As per the task description
    let currentPage = 1;

    const createRecordsTable = (data) => {
        const fragment = document.createDocumentFragment();
        if (!data || data.length === 0) {
            const p = document.createElement('p');
            p.className = 'empty-state';
            p.textContent = 'No usage records available.';
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
            let displayMetric = header.replace('_', ' ').split(' ')
                                 .map(word => word.charAt(0).toUpperCase() + word.slice(1))
                                 .join(' ');
            if (header === 'gateway_model') {
                displayMetric = 'Gateway Model';
            }
            if (header === 'model') {
                displayMetric = 'Resolved Model';
            }
            if (header === 'x_title') {
                displayMetric = 'X-Title';
            }
            if (header === 'operation') {
                displayMetric = 'Operation';
            }
            if (header === 'duration_ms') {
                displayMetric = 'Duration (ms)';
            }
            if (header === 'cost') {
                displayMetric = 'Cost ($)';
            }
            if (header === 'cost_saved') {
                displayMetric = 'Cost Saved ($)';
            }
            if (header === 'is_estimated') {
                displayMetric = 'Estimated';
            }
            th.textContent = displayMetric;
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
                    // Example: '0.01' -> '0.0100'
                    if (typeof formattedValue === 'number') {
                        formattedValue = formattedValue.toFixed(4);
                    }
                } else if (header === 'is_estimated') {
                    formattedValue = (formattedValue === 1 || formattedValue === true) ? '≈' : '';
                }
                const td = document.createElement('td');
                const displayValue = typeof formattedValue === 'number' ? formattedValue.toLocaleString() : formattedValue;
                td.textContent = displayValue !== null && displayValue !== undefined ? displayValue : 'N/A';
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        fragment.appendChild(table);

        return fragment;
    };

    const updatePaginationControls = (totalRecords) => {
        const totalPages = Math.ceil(totalRecords / recordsPerPage);
        pageInfoSpan.textContent = `Page ${currentPage} of ${totalPages || 1}`;
        prevPageButton.disabled = currentPage === 1;
        nextPageButton.disabled = currentPage === totalPages || totalRecords === 0;
    };

    const fetchAndRenderRecords = async () => {
        recordsArea.textContent = 'Loading usage records...';
        showMessage('');

        const offset = (currentPage - 1) * recordsPerPage;
        recordRefreshButton.disabled = true;
        recordRefreshButton.classList.add('loading');

        try {
            const response = await apiFetch(`/v1/api/usage-records?limit=${recordsPerPage}&offset=${offset}`);
            const responseData = await response.json(); // Renamed to avoid confusion with internal 'data'

            if (!response.ok) {
                const errorMessage = responseData.detail ? responseData.detail : `Error: ${response.status} ${response.statusText}`;
                showMessage(errorMessage, true);
                recordsArea.textContent = '';
                updatePaginationControls(0);
                return;
            }

            const records = responseData.records;
            const totalRecords = responseData.total_records;

            recordsArea.textContent = "";
            recordsArea.appendChild(createRecordsTable(records));
            updatePaginationControls(totalRecords);

            if (records.length === 0 && currentPage === 1) {
                showMessage('No usage records found.', false);
            } else {
                showMessage('Usage records loaded successfully.');
            }

        } catch (error) {
            console.error('Failed to fetch usage records:', error);
            showMessage(`Failed to load usage records: ${error.message}`, true);
            recordsArea.textContent = '';
            updatePaginationControls(0);
        } finally {
            recordRefreshButton.disabled = false;
            recordRefreshButton.classList.remove('loading');
        }
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

    // Sub-tab switching
    const fallbackSubTabs = document.querySelectorAll('.fallback-sub-tab');
    const fallbackSubContents = document.querySelectorAll('.fallback-sub-content');

    fallbackSubTabs.forEach(btn => {
        btn.addEventListener('click', () => {
            const subtab = btn.dataset.subtab;
            fallbackSubTabs.forEach(b => b.classList.remove('active'));
            fallbackSubContents.forEach(c => c.classList.remove('active'));
            btn.classList.add('active');
            const contentBySubTab = {
                summary: 'fallbackSummaryContent',
                chains: 'fallbackChainsContent',
                upstream: 'upstreamAnalyticsContent',
            };
            document.getElementById(contentBySubTab[subtab] || 'fallbackSummaryContent').classList.add('active');
            if (subtab === 'chains') {
                fallbackCurrentPage = 1;
                fetchAndRenderFallbackChains();
            } else if (subtab === 'upstream') {
                fetchAndRenderUpstreamStats();
            } else {
                fetchAndRenderFallbackStats();
            }
        });
    });

    const loadActiveFallbackSubTab = () => {
        const activeSubTab = document.querySelector('.fallback-sub-tab.active');
        if (activeSubTab && activeSubTab.dataset.subtab === 'chains') {
            fetchAndRenderFallbackChains();
        } else if (activeSubTab && activeSubTab.dataset.subtab === 'upstream') {
            fetchAndRenderUpstreamStats();
        } else {
            fetchAndRenderFallbackStats();
        }
    };

    const ERROR_TYPE_LABELS = {
        'http_429': '429 Rate Limit',
        'http_400': '400 Bad Request',
        'http_401': '401 Unauthorized',
        'http_403': '403 Forbidden',
        'http_404': '404 Not Found',
        'http_500': '500 Server Error',
        'http_502': '502 Bad Gateway',
        'http_503': '503 Unavailable',
        'read_timeout': 'Read Timeout',
        'connect_timeout': 'Connect Timeout',
        'connect_error': 'Connect Error',
        'context_overflow': 'Context Overflow',
        'unknown': 'Unknown',
    };

    const ERROR_TYPE_CLASSES = {
        'http_429': 'error-badge-rate-limit',
        'read_timeout': 'error-badge-timeout',
        'connect_timeout': 'error-badge-timeout',
        'connect_error': 'error-badge-connect',
        'context_overflow': 'error-badge-overflow',
    };

    const getErrorBadgeClass = (errorType) => {
        return ERROR_TYPE_CLASSES[errorType] || 'error-badge-default';
    };

    const formatErrorType = (errorType) => {
        return ERROR_TYPE_LABELS[errorType] || errorType || 'N/A';
    };

    const formatDuration = (ms) => {
        if (ms < 1000) return `${ms}ms`;
        return `${(ms / 1000).toFixed(1)}s`;
    };

    const createFallbackStatsTable = (data) => {
        const fragment = document.createDocumentFragment();
        if (!data || data.length === 0) {
            const p = document.createElement('p');
            p.className = 'empty-state';
            p.textContent = 'No fallback failures for the selected period.';
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
            h2.textContent = `Period: ${periodKey}`;
            fragment.appendChild(h2);

            const table = document.createElement('table');
            const thead = document.createElement('thead');
            const trHead = document.createElement('tr');
            ['Gateway Model', 'Provider', 'Model', 'Error Type', 'Count', 'Avg Duration'].forEach(text => {
                const th = document.createElement('th');
                th.textContent = text;
                trHead.appendChild(th);
            });
            thead.appendChild(trHead);
            table.appendChild(thead);

            const tbody = document.createElement('tbody');
            groupedByPeriod[periodKey].forEach(row => {
                const tr = document.createElement('tr');

                const cells = [
                    row.gateway_model || 'N/A',
                    row.provider || 'N/A',
                    row.model || 'N/A',
                ];
                cells.forEach(text => {
                    const td = document.createElement('td');
                    td.textContent = text;
                    tr.appendChild(td);
                });

                // Error type badge
                const tdError = document.createElement('td');
                const badge = document.createElement('span');
                badge.className = `error-badge ${getErrorBadgeClass(row.error_type)}`;
                badge.textContent = formatErrorType(row.error_type);
                tdError.appendChild(badge);
                tr.appendChild(tdError);

                const tdCount = document.createElement('td');
                tdCount.textContent = row.count;
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

    const fetchAndRenderFallbackStats = async () => {
        const selectedPeriod = fallbackPeriodSelector.value;
        fallbackStatsArea.textContent = 'Loading fallback statistics...';
        showMessage('');
        fallbackRefreshButton.disabled = true;
        fallbackRefreshButton.classList.add('loading');

        try {
            const response = await apiFetch(`/v1/api/fallback-stats/${selectedPeriod}`);
            const data = await response.json();

            if (!response.ok) {
                showMessage(data.detail || `Error: ${response.status}`, true);
                fallbackStatsArea.textContent = '';
                return;
            }

            fallbackStatsArea.textContent = '';
            fallbackStatsArea.appendChild(createFallbackStatsTable(data));
            showMessage(data.length === 0 ? 'No fallback failures for the selected period.' : 'Fallback statistics loaded.');
        } catch (error) {
            console.error('Failed to fetch fallback stats:', error);
            showMessage(`Failed to load fallback statistics: ${error.message}`, true);
            fallbackStatsArea.textContent = '';
        } finally {
            fallbackRefreshButton.disabled = false;
            fallbackRefreshButton.classList.remove('loading');
        }
    };

    const createFallbackChainsView = (records) => {
        const fragment = document.createDocumentFragment();
        if (!records || records.length === 0) {
            const p = document.createElement('p');
            p.className = 'empty-state';
            p.textContent = 'No fallback chains found.';
            fragment.appendChild(p);
            return fragment;
        }

        records.forEach(record => {
            const card = document.createElement('div');
            card.className = 'chain-card';

            // Header (clickable to expand)
            const header = document.createElement('div');
            header.className = 'chain-header';

            const summary = document.createElement('div');
            summary.className = 'chain-summary';

            const tsSpan = document.createElement('span');
            tsSpan.className = 'chain-timestamp';
            tsSpan.textContent = formatTimestamp(record.timestamp);
            summary.appendChild(tsSpan);

            const modelSpan = document.createElement('span');
            modelSpan.className = 'chain-model';
            modelSpan.textContent = record.gateway_model || 'N/A';
            summary.appendChild(modelSpan);

            const titleSpan = document.createElement('span');
            titleSpan.className = 'chain-title';
            titleSpan.textContent = `X-Title: ${record.x_title || 'N/A'}`;
            summary.appendChild(titleSpan);

            const attSpan = document.createElement('span');
            attSpan.className = 'chain-attempts';
            attSpan.textContent = `${record.total_attempts} attempts`;
            summary.appendChild(attSpan);

            const durSpan = document.createElement('span');
            durSpan.className = 'chain-duration';
            durSpan.textContent = formatDuration(record.total_duration_ms);
            summary.appendChild(durSpan);

            const resSpan = document.createElement('span');
            resSpan.className = `chain-result ${record.success ? 'chain-success' : 'chain-failure'}`;
            resSpan.textContent = record.success ? 'Success' : 'Failed';
            summary.appendChild(resSpan);

            header.appendChild(summary);

            const toggle = document.createElement('span');
            toggle.className = 'chain-toggle';
            toggle.textContent = '\u25BC';
            header.appendChild(toggle);

            const details = document.createElement('div');
            details.className = 'chain-details';
            details.style.display = 'none';

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
                targetWrap.appendChild(provSpan);

                if (!attempt.success && attempt.error_message) {
                    const errMessage = document.createElement('div');
                    errMessage.className = 'attempt-error-message';
                    errMessage.textContent = attempt.error_message;
                    targetWrap.appendChild(errMessage);
                }

                row.appendChild(targetWrap);

                const errSpan = document.createElement('span');
                errSpan.className = 'attempt-error';
                if (!attempt.success) {
                    const badge = document.createElement('span');
                    badge.className = `error-badge ${getErrorBadgeClass(attempt.error_type)}`;
                    badge.textContent = formatErrorType(attempt.error_type);
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

            header.addEventListener('click', () => {
                const isOpen = details.style.display !== 'none';
                details.style.display = isOpen ? 'none' : 'block';
                toggle.textContent = isOpen ? '\u25BC' : '\u25B2';
                card.classList.toggle('chain-card-open', !isOpen);
            });

            card.appendChild(header);
            card.appendChild(details);
            fragment.appendChild(card);
        });

        return fragment;
    };

    const updateFallbackPagination = (totalRecords) => {
        const totalPages = Math.ceil(totalRecords / fallbackRecordsPerPage);
        fallbackPageInfo.textContent = `Page ${fallbackCurrentPage} of ${totalPages || 1}`;
        fallbackPrevPage.disabled = fallbackCurrentPage === 1;
        fallbackNextPage.disabled = fallbackCurrentPage === totalPages || totalRecords === 0;
    };

    const fetchAndRenderFallbackChains = async () => {
        fallbackChainsArea.textContent = 'Loading fallback chains...';
        showMessage('');
        fallbackChainsRefreshButton.disabled = true;
        fallbackChainsRefreshButton.classList.add('loading');

        const offset = (fallbackCurrentPage - 1) * fallbackRecordsPerPage;

        try {
            const response = await apiFetch(`/v1/api/fallback-records?limit=${fallbackRecordsPerPage}&offset=${offset}`);
            const responseData = await response.json();

            if (!response.ok) {
                showMessage(responseData.detail || `Error: ${response.status}`, true);
                fallbackChainsArea.textContent = '';
                updateFallbackPagination(0);
                return;
            }

            fallbackChainsArea.textContent = '';
            fallbackChainsArea.appendChild(createFallbackChainsView(responseData.records));
            updateFallbackPagination(responseData.total_records);
            showMessage(responseData.records.length === 0 ? 'No fallback chains found.' : 'Fallback chains loaded.');
        } catch (error) {
            console.error('Failed to fetch fallback chains:', error);
            showMessage(`Failed to load fallback chains: ${error.message}`, true);
            fallbackChainsArea.textContent = '';
            updateFallbackPagination(0);
        } finally {
            fallbackChainsRefreshButton.disabled = false;
            fallbackChainsRefreshButton.classList.remove('loading');
        }
    };

    const createUpstreamStatsTable = (data) => {
        const fragment = document.createDocumentFragment();
        if (!data || data.length === 0) {
            const p = document.createElement('p');
            p.className = 'empty-state';
            p.textContent = 'No upstream analytics for the selected period.';
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
            h2.textContent = `Period: ${periodKey}`;
            fragment.appendChild(h2);

            const table = document.createElement('table');
            const thead = document.createElement('thead');
            const trHead = document.createElement('tr');
            [
                'Gateway Model',
                'Provider',
                'Model',
                'Operation',
                'Key',
                'Attempts',
                'Success',
                'Errors',
                'Success Rate',
                'Avg Duration',
                'Max Duration',
            ].forEach(text => {
                const th = document.createElement('th');
                th.textContent = text;
                trHead.appendChild(th);
            });
            thead.appendChild(trHead);
            table.appendChild(thead);

            const tbody = document.createElement('tbody');
            groupedByPeriod[periodKey].forEach(row => {
                const tr = document.createElement('tr');
                const successRate = typeof row.success_rate === 'number' ? `${row.success_rate.toFixed(1)}%` : 'N/A';
                const cells = [
                    row.gateway_model || 'N/A',
                    row.provider || 'N/A',
                    row.model || 'N/A',
                    row.operation || 'N/A',
                    row.upstream_key_fingerprint || 'N/A',
                    row.attempts,
                    row.successes,
                    row.errors,
                    successRate,
                    formatDuration(row.avg_duration_ms || 0),
                    formatDuration(row.max_duration_ms || 0),
                ];
                cells.forEach(value => {
                    const td = document.createElement('td');
                    td.textContent = typeof value === 'number' ? value.toLocaleString() : value;
                    tr.appendChild(td);
                });
                tbody.appendChild(tr);
            });
            table.appendChild(tbody);
            fragment.appendChild(table);
        }
        return fragment;
    };

    const fetchAndRenderUpstreamStats = async () => {
        const selectedPeriod = upstreamPeriodSelector.value;
        upstreamStatsArea.textContent = 'Loading upstream analytics...';
        showMessage('');
        upstreamRefreshButton.disabled = true;
        upstreamRefreshButton.classList.add('loading');

        try {
            const response = await apiFetch(`/v1/api/upstream-stats/${selectedPeriod}`);
            const data = await response.json();

            if (!response.ok) {
                showMessage(data.detail || `Error: ${response.status}`, true);
                upstreamStatsArea.textContent = '';
                return;
            }

            upstreamStatsArea.textContent = '';
            upstreamStatsArea.appendChild(createUpstreamStatsTable(data));
            showMessage(data.length === 0 ? 'No upstream analytics for the selected period.' : 'Upstream analytics loaded.');
        } catch (error) {
            console.error('Failed to fetch upstream analytics:', error);
            showMessage(`Failed to load upstream analytics: ${error.message}`, true);
            upstreamStatsArea.textContent = '';
        } finally {
            upstreamRefreshButton.disabled = false;
            upstreamRefreshButton.classList.remove('loading');
        }
    };

    fallbackRefreshButton.addEventListener('click', fetchAndRenderFallbackStats);
    fallbackPeriodSelector.addEventListener('change', fetchAndRenderFallbackStats);
    upstreamRefreshButton.addEventListener('click', fetchAndRenderUpstreamStats);
    upstreamPeriodSelector.addEventListener('change', fetchAndRenderUpstreamStats);

    fallbackChainsRefreshButton.addEventListener('click', () => {
        fallbackCurrentPage = 1;
        fetchAndRenderFallbackChains();
    });

    fallbackPrevPage.addEventListener('click', () => {
        if (fallbackCurrentPage > 1) {
            fallbackCurrentPage--;
            fetchAndRenderFallbackChains();
        }
    });

    fallbackNextPage.addEventListener('click', () => {
        fallbackCurrentPage++;
        fetchAndRenderFallbackChains();
    });

    // Ensure initial load respects the default tab
    const activeTab = document.querySelector('.tab-button.active');
    if (activeTab && activeTab.dataset.tab === 'records') {
        fetchAndRenderRecords();
    } else if (activeTab && activeTab.dataset.tab === 'analytics') {
        window.usageAnalyticsDashboard.activate();
    } else if (activeTab && activeTab.dataset.tab === 'fallback') {
        loadActiveFallbackSubTab();
    } else if (activeTab && activeTab.dataset.tab === 'topology') {
        loadTopology();
    } else {
        fetchAndRenderStats();
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
    const EDGE_COLOR = { alias: '#94a3b8', fallback: '#0d9488', context_overflow: '#d97706' };
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

    function showTopologyFatalError(message, onRetry) {
        topologyContainer.replaceChildren();
        const errDiv = document.createElement('div');
        errDiv.className = 'topology-error';
        errDiv.textContent = message;
        const retryBtn = document.createElement('button');
        retryBtn.textContent = 'Retry';
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
            lines.push(`${steps} step${steps === 1 ? '' : 's'}`);
            if (hasActive) lines.push(`▶ ${active} active`);
        } else if (!isCentral) {
            lines.push(`Health: ${health}${d.configured === false ? ' (not configured)' : ''}`);
            if (hasActive) lines.push(`▶ ${active} active`);
            lines.push(`Penalty: ${typeof d.penalty === 'number' ? d.penalty.toFixed(2) : 0}`);
        }

        const label = h('div', null,
            h('div', { className: 'topology-node-title' }, node.label || node.id),
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
            label = 'ctx';
        }

        const emphasised = (chain && inChain) || edge.animated;
        const baseWidth = isAlias ? 1.5 : 2;

        return {
            id: edge.id,
            source: edge.source,
            target: edge.target,
            type: 'default',
            animated: !!edge.animated,
            label,
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
        const kv = (k, v) => h('div', { className: 'topology-detail-row', key: k },
            h('span', { className: 'topology-detail-key' }, k),
            h('span', { className: 'topology-detail-val' }, v));

        let body;
        if (detail.kind === 'provider') {
            body = [
                kv('Health', d.health),
                kv('Configured', d.configured === false ? 'no' : 'yes'),
                kv('Active requests', String(d.active_requests || 0)),
                kv('Penalty', typeof d.penalty === 'number' ? d.penalty.toFixed(2) : '0'),
                (d.models && d.models.length) ? kv('Models', d.models.join(', ')) : null,
            ];
        } else {
            const flags = d.flags || {};
            const flagList = Object.keys(flags).filter(k => flags[k]);
            const chainItems = (d.chain || []).map((step, i) =>
                h('li', { key: i, className: 'topology-chain-step' },
                    h('span', { className: 'topology-chain-prio' }, `#${step.priority}`),
                    h('span', { className: 'topology-chain-target' }, `${step.provider || '?'} / ${step.model || '?'}`),
                    (step.retry_count != null || step.retry_delay != null)
                        ? h('span', { className: 'topology-chain-retry' },
                            `retry: ${step.retry_count != null ? step.retry_count : '-'}${step.retry_delay != null ? ` (${step.retry_delay}ms)` : ''}`)
                        : null
                )
            );
            body = [
                kv('Active requests', String(d.active_requests || 0)),
                (d.max_total_attempts != null) ? kv('Max total attempts', String(d.max_total_attempts)) : null,
                flagList.length ? kv('Flags', flagList.join(', ')) : null,
                d.context_overflow ? kv('Context overflow', `${d.context_overflow.provider} / ${d.context_overflow.model || '?'}`) : null,
                h('div', { className: 'topology-detail-subhead', key: 'chainhead' }, 'Fallback chain'),
                h('ol', { className: 'topology-chain-list', key: 'chain' },
                    chainItems.length ? chainItems : h('li', { key: 'empty' }, '— нет целей —')),
            ];
        }

        return h('div', { className: 'topology-detail' },
            h('div', { className: 'topology-detail-header' },
                h('span', { className: 'topology-detail-title' }, node.label || node.id),
                h('button', { className: 'topology-detail-close', onClick: onClose, title: 'Close' }, '×')
            ),
            h('div', { className: 'topology-detail-body' }, body.filter(Boolean))
        );
    }

    function TopologyApp(props) {
        const { React, ReactFlow, Background, Controls } = props;
        const { useState, useEffect, useCallback, useMemo, createElement: h } = React;

        const [graph, setGraph] = useState(null);
        const [error, setError] = useState(null);
        const [selectedModel, setSelectedModel] = useState(null);
        const [detail, setDetail] = useState(null);
        const [auto, setAuto] = useState(topologyAutoRefreshInput ? topologyAutoRefreshInput.checked : true);
        const [size, setSize] = useState({
            w: topologyContainer.clientWidth || 1000,
            h: topologyContainer.clientHeight || 520,
        });

        const fetchGraph = useCallback(async () => {
            try {
                const resp = await apiFetch('/v1/topology');
                if (!resp.ok) {
                    const errBody = await resp.json().catch(() => ({}));
                    setError(`Ошибка загрузки топологии: ${errBody.detail || resp.status}`);
                    return;
                }
                setGraph(await resp.json());
                setError(null);
            } catch (e) {
                console.error('Failed to fetch topology:', e);
                setError(`Ошибка загрузки топологии: ${e.message}`);
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
            _topologyApi = { refresh: fetchGraph, setAuto };
            return () => { _topologyApi = null; };
        }, [fetchGraph]);

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
            [React, graph, selectedModel, size]
        );

        if (error) {
            return h('div', { className: 'topology-error' },
                error, h('button', { onClick: fetchGraph }, 'Retry'));
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
                    }, h(Background, null), h(Controls, null))
                    : h('div', { className: 'topology-loading' }, 'Loading topology...')
            ),
            detail ? topologyDetailPanel(React, detail, () => { setDetail(null); setSelectedModel(null); }) : null
        );
    }

    async function loadTopology() {
        ensureTopologyStylesLoaded();
        // Mount once; subsequent activations just trigger a refresh so the
        // viewport and any selected chain survive tab switches.
        if (_topologyMounted) {
            if (_topologyApi) _topologyApi.refresh();
            return;
        }
        topologyContainer.textContent = 'Loading topology...';

        let bundle;
        try {
            bundle = await import('/static/vendor/topology.bundle.mjs');
        } catch (bundleError) {
            console.error('Topology bundle load failed:', bundleError);
            showTopologyFatalError('Не удалось загрузить визуализацию (bundle недоступен).', loadTopology);
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

});
