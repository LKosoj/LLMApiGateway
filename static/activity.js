document.addEventListener('DOMContentLoaded', () => {
    'use strict';

    const USAGE_RECORDS_ENDPOINT = '/v1/api/usage-records';
    const REJECTIONS_ENDPOINT = '/v1/admin/rejections';
    const { apiFetch, bootstrapRoleUI } = window.gatewayAuth;
    const i18n = window.gatewayI18n;

    Theme.attachToggle('darkModeToggle');

    const els = {
        status: document.getElementById('filterStatus'),
        since: document.getElementById('filterSince'),
        provider: document.getElementById('filterProvider'),
        keyId: document.getElementById('filterKeyId'),
        requestId: document.getElementById('filterRequestId'),
        limit: document.getElementById('filterLimit'),
        apply: document.getElementById('applyFiltersBtn'),
        reset: document.getElementById('resetFiltersBtn'),
        refresh: document.getElementById('refreshBtn'),
        meta: document.getElementById('actMetaText'),
        staleBadge: document.getElementById('actStaleBadge'),
        serverErrorEmpty: document.getElementById('serverErrorEmpty'),
        listing: document.getElementById('actListing'),
        tableWrap: document.querySelector('.act-table-wrap'),
        tbody: document.getElementById('actTableBody'),
        prev: document.getElementById('prevPageBtn'),
        next: document.getElementById('nextPageBtn'),
        pageInfo: document.getElementById('pageInfo'),
        messageArea: document.getElementById('messageArea'),
        messageDetail: document.getElementById('messageDetail'),
        drawerMount: document.getElementById('requestDrawerMount'),
    };
    const pageStatus = window.gatewayUi.createStatus(els.messageArea, {
        rawDetailElement: els.messageDetail,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });

    const SNAPSHOT_STORAGE_PREFIX = 'activity:last-snapshot:';

    let offset = 0;
    let snapshot = null;
    let snapshotStale = false;
    let requestGeneration = 0;
    let unsubscribeLocale = null;
    let stopped = false;
    let requestDrawer = null;
    const keyNameResolver = window.RequestDrawer.createKeyNameResolver(apiFetch);

    function currentMode() {
        if (els.status.value === 'rejected') return 'rejected';
        if (els.status.value === 'server_error') return 'server_error';
        return 'usage';
    }

    function currentLimit() {
        return Number.parseInt(els.limit.value, 10) || 50;
    }

    function numberValue(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function formatMoney(value) {
        return i18n.formatCurrency(numberValue(value), 'USD', {
            minimumFractionDigits: 4,
            maximumFractionDigits: 4,
        });
    }

    function formatDuration(value) {
        const ms = numberValue(value);
        if (!ms) return i18n.t('activity:values.notAvailable');
        if (ms < 1000) return i18n.t('activity:format.milliseconds', { value: i18n.formatNumber(ms) });
        return i18n.t('activity:format.seconds', {
            value: i18n.formatNumber(ms / 1000, { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
        });
    }

    function formatTime(value) {
        if (!value) return i18n.t('activity:values.notAvailable');
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return i18n.formatDate(date, { dateStyle: 'medium', timeStyle: 'medium' });
    }

    function formatTimeUtc(value) {
        if (!value) return i18n.t('activity:values.notAvailable');
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return i18n.formatDate(date, { dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC' });
    }

    function buildUsageQuery() {
        const params = new URLSearchParams();
        params.set('limit', String(currentLimit()));
        params.set('offset', String(offset));
        return params.toString();
    }

    function buildRejectionsQuery() {
        const params = new URLSearchParams();
        params.set('limit', String(currentLimit()));
        params.set('offset', String(offset));
        if (els.since.value) params.set('since', els.since.value);
        return params.toString();
    }

    function filtersFromUrl() {
        const params = new URLSearchParams(window.location.search);
        return {
            status: params.get('status') || '',
            since: params.get('since') || '',
            provider: params.get('provider') || '',
            keyId: params.get('key_id') || '',
            requestId: params.get('request_id') || '',
            limit: params.get('limit') || '',
            page: params.get('page') || '',
        };
    }

    function applyUrlFilters(filters) {
        const statusOptions = new Set(Array.from(els.status.options, (option) => option.value));
        if (filters.status && statusOptions.has(filters.status)) {
            els.status.value = filters.status;
        }
        if (filters.since) {
            els.since.value = filters.since;
        }
        if (filters.provider) {
            els.provider.value = filters.provider;
        }
        if (filters.keyId !== '') {
            els.keyId.value = filters.keyId;
        }
        if (filters.requestId) {
            els.requestId.value = filters.requestId;
        }
        const limitOptions = new Set(Array.from(els.limit.options, (option) => option.value));
        if (filters.limit && limitOptions.has(filters.limit)) {
            els.limit.value = filters.limit;
        }
        const page = Number.parseInt(filters.page, 10);
        if (Number.isInteger(page) && page > 1) {
            offset = (page - 1) * currentLimit();
        }
    }

    function syncUrlFromState(push) {
        const params = new URLSearchParams();
        if (els.status.value) params.set('status', els.status.value);
        if (els.since.value) params.set('since', els.since.value);
        if (els.provider.value) params.set('provider', els.provider.value);
        if (els.keyId.value !== '') params.set('key_id', els.keyId.value);
        if (els.requestId.value) params.set('request_id', els.requestId.value);
        params.set('limit', String(currentLimit()));
        const page = Math.floor(offset / currentLimit()) + 1;
        if (page > 1) params.set('page', String(page));
        const query = params.toString();
        const url = `${window.location.pathname}${query ? `?${query}` : ''}`;
        if (push) window.history.pushState(null, '', url);
        else window.history.replaceState(null, '', url);
    }

    function loadStoredSnapshot(mode) {
        try {
            const raw = window.sessionStorage.getItem(`${SNAPSHOT_STORAGE_PREFIX}${mode}`);
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            if (
                !parsed
                || !Array.isArray(parsed.items)
                || !Number.isInteger(parsed.total)
                || !Number.isFinite(parsed.loadedAt)
            ) {
                return null;
            }
            return { items: parsed.items, total: parsed.total, loadedAt: parsed.loadedAt, mode };
        } catch (error) {
            return null;
        }
    }

    function persistSnapshot(mode, value) {
        try {
            window.sessionStorage.setItem(`${SNAPSHOT_STORAGE_PREFIX}${mode}`, JSON.stringify(value));
        } catch (error) {
            // sessionStorage may be unavailable (quota exceeded, private mode);
            // the in-memory snapshot still works for the rest of this session.
        }
    }

    function relativeStaleLabel(loadedAt) {
        const diffMinutes = Math.max(1, Math.round((Date.now() - loadedAt) / 60000));
        const relative = i18n.formatRelativeTime(-diffMinutes, 'minute');
        return i18n.t('common:stale.badge', { time: relative });
    }

    function updateStaleBadge() {
        if (!snapshotStale || snapshot === null || snapshot.mode !== currentMode()) {
            els.staleBadge.hidden = true;
            els.staleBadge.textContent = '';
            return;
        }
        els.staleBadge.hidden = false;
        els.staleBadge.textContent = relativeStaleLabel(snapshot.loadedAt);
    }

    function normalizeUsageItem(record) {
        return {
            kind: 'usage',
            timestamp: record.timestamp,
            request_id: record.request_id,
            api_key_id: record.api_key_id,
            operation: record.operation,
            model: record.model || record.gateway_model,
            provider: record.provider,
            running: record.status === 'running',
            duration_ms: record.duration_ms,
            cost: record.cost,
            raw: record,
        };
    }

    function normalizeRejectionItem(item) {
        return {
            kind: 'rejection',
            timestamp: item.timestamp,
            request_id: item.request_id,
            api_key_id: item.api_key_id,
            operation: null,
            model: null,
            provider: null,
            status_code: item.status_code,
            raw: item,
        };
    }

    function matchesClientFilters(item) {
        const providerFilter = els.provider.value.trim().toLowerCase();
        if (providerFilter && !String(item.provider || '').toLowerCase().includes(providerFilter)) {
            return false;
        }
        const keyFilter = els.keyId.value.trim();
        if (keyFilter && String(item.api_key_id ?? '') !== keyFilter) {
            return false;
        }
        const requestIdFilter = els.requestId.value.trim().toLowerCase();
        if (requestIdFilter && !String(item.request_id || '').toLowerCase().includes(requestIdFilter)) {
            return false;
        }
        return true;
    }

    function createCell(value, className = '') {
        const cell = document.createElement('td');
        if (className) cell.className = className;
        cell.textContent = value;
        return cell;
    }

    function createRequestIdCell(item) {
        const cell = document.createElement('td');
        const wrap = document.createElement('div');
        wrap.className = 'act-request-id';
        const code = document.createElement('code');
        code.textContent = String(item.request_id || i18n.t('activity:values.notAvailable'));
        wrap.appendChild(code);
        if (item.request_id) {
            const copyBtn = document.createElement('button');
            copyBtn.type = 'button';
            copyBtn.className = 'act-copy-btn';
            copyBtn.textContent = i18n.t('activity:actions.copy');
            copyBtn.addEventListener('click', (event) => {
                event.stopPropagation();
                navigator.clipboard?.writeText(String(item.request_id)).then(() => {
                    copyBtn.textContent = '✓';
                    setTimeout(() => {
                        copyBtn.textContent = i18n.t('activity:actions.copy');
                    }, 1200);
                }).catch(() => undefined);
            });
            wrap.appendChild(copyBtn);
        }
        cell.appendChild(wrap);
        return cell;
    }

    function createStatusCell(item) {
        const cell = document.createElement('td');
        const pill = document.createElement('span');
        pill.className = 'status-pill';
        if (item.kind === 'rejection') {
            pill.classList.add('status-rejected');
            pill.textContent = String(item.status_code ?? i18n.t('activity:values.notAvailable'));
        } else if (item.running) {
            pill.classList.add('status-running');
            pill.textContent = i18n.t('activity:status.running');
        } else {
            pill.classList.add('status-success');
            pill.textContent = i18n.t('activity:status.success');
        }
        cell.appendChild(pill);
        return cell;
    }

    function renderRows(items) {
        if (items.length === 0) {
            const row = document.createElement('tr');
            const cell = createCell(i18n.t('activity:table.empty'), 'act-empty-cell');
            cell.colSpan = 9;
            row.appendChild(cell);
            els.tbody.replaceChildren(row);
            return;
        }

        const rows = items.map((item) => {
            const row = document.createElement('tr');
            row.dataset.clickable = 'true';
            row.tabIndex = 0;
            row.setAttribute('role', 'button');
            row.dataset.requestId = item.request_id != null ? String(item.request_id) : '';
            const rowLabelKey = item.kind === 'rejection'
                ? 'activity:row.openRejection'
                : 'activity:row.openUsage';
            row.setAttribute('aria-label', i18n.t(rowLabelKey, {
                requestId: item.request_id || i18n.t('activity:values.notAvailable'),
            }));
            row.addEventListener('click', () => openDrawer(item));
            row.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    openDrawer(item);
                }
            });

            row.appendChild(createCell(formatTime(item.timestamp)));
            row.appendChild(createRequestIdCell(item));
            const keyCell = createCell(item.api_key_id == null ? '—' : `#${item.api_key_id}`);
            keyCell.dataset.masterOnly = '';
            keyCell.hidden = true;
            row.appendChild(keyCell);
            row.appendChild(createCell(String(item.operation || i18n.t('activity:values.notAvailable'))));

            const modelCell = createCell(String(item.model || i18n.t('activity:values.notAvailable')));
            modelCell.lang = 'und';
            modelCell.dir = 'auto';
            row.appendChild(modelCell);

            row.appendChild(createCell(String(item.provider || i18n.t('activity:values.notAvailable'))));
            row.appendChild(createStatusCell(item));
            row.appendChild(createCell(item.kind === 'rejection' ? i18n.t('activity:values.notAvailable') : formatDuration(item.duration_ms)));
            row.appendChild(createCell(item.kind === 'rejection' ? i18n.t('activity:values.notAvailable') : formatMoney(item.cost)));
            return row;
        });
        els.tbody.replaceChildren(...rows);
        window.gatewayUi.applyRoleVisibility(els.tbody, window.gatewayAuth.state);
    }

    function renderPagination(loadedCount, total) {
        const start = total === 0 ? 0 : offset + 1;
        const end = offset + loadedCount;
        els.pageInfo.textContent = total === 0
            ? i18n.t('activity:pagination.empty')
            : i18n.t('activity:pagination.range', {
                start: i18n.formatNumber(start),
                end: i18n.formatNumber(end),
                total: i18n.formatNumber(total),
            });
        els.prev.disabled = offset <= 0;
        els.next.disabled = end >= total;
    }

    function renderSnapshot() {
        if (snapshot === null) return;
        const visible = snapshot.items.filter(matchesClientFilters);
        renderRows(visible);
        renderPagination(snapshot.items.length, snapshot.total);
        els.meta.textContent = i18n.t('activity:summary', {
            visible: i18n.formatNumber(visible.length),
            loaded: i18n.formatNumber(snapshot.items.length),
            total: i18n.formatNumber(snapshot.total),
            time: i18n.formatDate(new Date(snapshot.loadedAt), {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
            }),
        });
        updateStaleBadge();
    }

    function drawerStatusInfo(item) {
        if (item.kind === 'rejection') {
            return { text: String(item.status_code ?? i18n.t('activity:values.notAvailable')), tone: 'rejected' };
        }
        if (item.running) {
            return { text: i18n.t('activity:status.running'), tone: 'running' };
        }
        return { text: i18n.t('activity:status.success'), tone: 'success' };
    }

    function buildDrawerBody(item) {
        const RD = window.RequestDrawer;
        const na = () => i18n.t('activity:values.notAvailable');
        const section = RD.createSection(null);

        section.appendChild(RD.createField(
            i18n.t('activity:drawer.fields.requestId'),
            item.request_id
                ? RD.createCopyableValue(String(item.request_id), i18n.t('common:actions.copy'), i18n.t('common:actions.copied'))
                : na(),
        ));
        section.appendChild(RD.createField(i18n.t('activity:drawer.fields.timeUtc'), formatTimeUtc(item.timestamp)));
        section.appendChild(RD.createField(i18n.t('activity:drawer.fields.timeLocal'), formatTime(item.timestamp)));
        const status = drawerStatusInfo(item);
        section.appendChild(RD.createField(i18n.t('activity:drawer.fields.status'), RD.createStatusPill(status.text, status.tone)));

        const keyField = RD.createField(i18n.t('activity:drawer.fields.virtualKey'), item.api_key_id == null ? na() : `#${item.api_key_id}`);
        keyField.dataset.masterOnly = '';
        keyField.hidden = true;
        section.appendChild(keyField);
        if (item.api_key_id != null && window.gatewayAuth.state === 'master') {
            keyNameResolver.resolve(item.api_key_id).then((name) => {
                if (!name) return;
                const valueEl = keyField.querySelector('.req-drawer-field-value');
                if (valueEl) valueEl.textContent = `#${item.api_key_id} — ${name}`;
            });
        }

        if (item.kind === 'usage') {
            section.appendChild(RD.createField(i18n.t('activity:drawer.fields.operation'), String(item.operation || na())));
            section.appendChild(RD.createField(i18n.t('activity:drawer.fields.requestedModel'), String(item.raw.gateway_model || na())));
            section.appendChild(RD.createField(i18n.t('activity:drawer.fields.provider'), String(item.provider || na())));
            section.appendChild(RD.createField(i18n.t('activity:drawer.fields.resolvedModel'), String(item.raw.model || na())));
            section.appendChild(RD.createField(
                i18n.t('activity:drawer.fields.promptTokens'),
                item.raw.prompt_tokens != null ? i18n.formatNumber(item.raw.prompt_tokens) : na(),
            ));
            section.appendChild(RD.createField(
                i18n.t('activity:drawer.fields.completionTokens'),
                item.raw.completion_tokens != null ? i18n.formatNumber(item.raw.completion_tokens) : na(),
            ));
            section.appendChild(RD.createField(i18n.t('activity:drawer.fields.cost'), formatMoney(item.cost)));
            section.appendChild(RD.createField(i18n.t('activity:drawer.fields.ttft'), na()));
            section.appendChild(RD.createField(i18n.t('activity:drawer.fields.duration'), formatDuration(item.duration_ms)));
        } else {
            const endpoint = `${item.raw.method || ''} ${item.raw.path || ''}`.trim();
            section.appendChild(RD.createField(i18n.t('activity:drawer.fields.endpoint'), endpoint || na()));
            section.appendChild(RD.createField(i18n.t('activity:drawer.fields.clientIp'), String(item.raw.client_ip || na())));
            section.appendChild(RD.createField(i18n.t('activity:drawer.fields.xTitle'), String(item.raw.x_title || na())));
            section.appendChild(RD.createField(i18n.t('activity:drawer.fields.reason'), String(item.raw.reason || na())));
        }

        const timeline = RD.createSection(i18n.t('activity:drawer.timeline.heading'));
        timeline.appendChild(RD.createNote(i18n.t('activity:drawer.timeline.unavailable')));

        const frag = document.createDocumentFragment();
        frag.appendChild(section);
        frag.appendChild(timeline);
        return frag;
    }

    function openDrawer(item) {
        requestDrawer.open(buildDrawerBody(item));
    }

    function showError(message, rawDetail = null) {
        els.messageArea.hidden = false;
        els.messageDetail.hidden = rawDetail === null;
        pageStatus.error(message, { rawDetail });
    }

    function clearError() {
        pageStatus.clear();
        els.messageArea.hidden = true;
        els.messageDetail.hidden = true;
    }

    function captureViewState() {
        const active = document.activeElement;
        const activeRowId = active && active.tagName === 'TR' && active.dataset.requestId
            ? active.dataset.requestId
            : null;
        return {
            activeElement: active,
            activeRowId,
            scrollX: window.scrollX,
            scrollY: window.scrollY,
            tableLeft: els.tableWrap.scrollLeft,
            tableTop: els.tableWrap.scrollTop,
        };
    }

    function restoreViewState(state) {
        if (state.activeRowId) {
            const row = els.tbody.querySelector(
                `tr[data-request-id="${CSS.escape(state.activeRowId)}"]`,
            );
            if (row) row.focus({ preventScroll: true });
        } else if (state.activeElement?.isConnected) {
            state.activeElement.focus({ preventScroll: true });
        }
        els.tableWrap.scrollLeft = state.tableLeft;
        els.tableWrap.scrollTop = state.tableTop;
        window.scrollTo(state.scrollX, state.scrollY);
    }

    function renderServerErrorMode() {
        els.serverErrorEmpty.hidden = false;
        els.listing.hidden = true;
        els.meta.textContent = '';
        els.staleBadge.hidden = true;
        els.staleBadge.textContent = '';
        clearError();
    }

    async function loadActivity() {
        const mode = currentMode();
        if (mode === 'server_error') {
            renderServerErrorMode();
            return;
        }
        els.serverErrorEmpty.hidden = true;
        els.listing.hidden = false;

        const generation = ++requestGeneration;
        try {
            const endpoint = mode === 'rejected' ? REJECTIONS_ENDPOINT : USAGE_RECORDS_ENDPOINT;
            const query = mode === 'rejected' ? buildRejectionsQuery() : buildUsageQuery();
            const response = await apiFetch(`${endpoint}?${query}`);
            if (stopped || generation !== requestGeneration) return;

            if (!response.ok) {
                const body = await response.json().catch(() => ({}));
                if (stopped || generation !== requestGeneration) return;
                const descriptor = window.gatewayUi.describeApiError(body, { status: response.status });
                if (response.status >= 500 && snapshot !== null && snapshot.mode === mode) {
                    snapshotStale = true;
                    renderSnapshot();
                }
                showError(
                    { key: descriptor.summaryKey, values: descriptor.summaryValues },
                    descriptor.rawDetail,
                );
                return;
            }

            const data = await response.json();
            if (stopped || generation !== requestGeneration) return;

            const rawItems = mode === 'rejected' ? data.items : data.records;
            const total = mode === 'rejected' ? data.total : data.total_records;
            if (!Array.isArray(rawItems) || !Number.isInteger(total) || total < 0) {
                showError({ key: 'activity:errors.invalidResponse' }, JSON.stringify(data));
                return;
            }

            snapshot = {
                items: rawItems.map(mode === 'rejected' ? normalizeRejectionItem : normalizeUsageItem),
                total,
                loadedAt: Date.now(),
                mode,
            };
            snapshotStale = false;
            persistSnapshot(mode, snapshot);
            clearError();
            renderSnapshot();
        } catch (error) {
            if (stopped || generation !== requestGeneration) return;
            if (error.message === 'Authentication required') return;
            const descriptor = window.gatewayUi.describeApiError(null);
            if (snapshot !== null && snapshot.mode === mode) {
                snapshotStale = true;
                renderSnapshot();
            }
            showError(
                { key: descriptor.summaryKey, values: descriptor.summaryValues },
                error.message,
            );
        }
    }

    els.status.addEventListener('change', () => {
        offset = 0;
        syncUrlFromState(false);
        void loadActivity();
    });
    els.apply.addEventListener('click', () => {
        offset = 0;
        syncUrlFromState(true);
        renderSnapshot();
        void loadActivity();
    });
    els.refresh.addEventListener('click', () => void loadActivity());
    els.reset.addEventListener('click', () => {
        els.status.value = '';
        els.since.value = '';
        els.provider.value = '';
        els.keyId.value = '';
        els.requestId.value = '';
        els.limit.value = '50';
        offset = 0;
        syncUrlFromState(false);
        void loadActivity();
    });
    els.limit.addEventListener('change', () => {
        offset = 0;
        syncUrlFromState(false);
        void loadActivity();
    });
    els.prev.addEventListener('click', () => {
        offset = Math.max(0, offset - currentLimit());
        syncUrlFromState(false);
        void loadActivity();
    });
    els.next.addEventListener('click', () => {
        offset += currentLimit();
        syncUrlFromState(false);
        void loadActivity();
    });
    window.addEventListener('popstate', () => {
        if (stopped) return;
        applyUrlFilters(filtersFromUrl());
        void loadActivity();
    });
    window.addEventListener('beforeunload', () => {
        stopped = true;
        requestGeneration += 1;
        unsubscribeLocale?.();
    });

    Promise.all([bootstrapRoleUI(), i18n.ready]).then(() => {
        if (stopped) return;
        applyUrlFilters(filtersFromUrl());
        requestDrawer = window.RequestDrawer.create({
            mount: els.drawerMount,
            titleId: 'requestDrawerTitle',
            heading: i18n.t('activity:drawer.heading'),
            closeLabel: i18n.t('activity:drawer.close'),
            inertRoots: Array.from(document.querySelectorAll('.top-nav, .container, [data-gateway-nav-overlay]')),
        });
        unsubscribeLocale = i18n.subscribe(() => {
            const viewState = captureViewState();
            renderSnapshot();
            pageStatus.rerender();
            requestDrawer.setChrome(i18n.t('activity:drawer.heading'), i18n.t('activity:drawer.close'));
            restoreViewState(viewState);
        });
        if (currentMode() !== 'server_error') {
            const stored = loadStoredSnapshot(currentMode());
            if (stored !== null) {
                snapshot = stored;
                snapshotStale = true;
                renderSnapshot();
            }
        }
        void loadActivity();
    }).catch(() => undefined);
});
