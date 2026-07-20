document.addEventListener('DOMContentLoaded', () => {
    'use strict';

    const { apiFetch, bootstrapRoleUI } = window.gatewayAuth;
    const i18n = window.gatewayI18n;

    Theme.attachToggle('darkModeToggle');

    const KNOWN_CATEGORIES = new Set([
        'auth_invalid', 'key_disabled', 'model_not_allowed', 'budget_exhausted',
        'rate_limited', 'master_only', 'unauthorized', 'ip_blocked',
    ]);
    const els = {
        category: document.getElementById('filterCategory'),
        keyId: document.getElementById('filterKeyId'),
        since: document.getElementById('filterSince'),
        limit: document.getElementById('filterLimit'),
        apply: document.getElementById('applyFiltersBtn'),
        reset: document.getElementById('resetFiltersBtn'),
        refresh: document.getElementById('refreshBtn'),
        meta: document.getElementById('rejMetaText'),
        staleBadge: document.getElementById('rejStaleBadge'),
        tableWrap: document.querySelector('.rej-table-wrap'),
        tbody: document.getElementById('rejTableBody'),
        prev: document.getElementById('prevPageBtn'),
        next: document.getElementById('nextPageBtn'),
        pageInfo: document.getElementById('pageInfo'),
        messageArea: document.getElementById('messageArea'),
        messageDetail: document.getElementById('messageDetail'),
        detail: document.getElementById('rejDetailSheet'),
        detailBackdrop: document.getElementById('rejDetailBackdrop'),
        detailClose: document.getElementById('rejDetailClose'),
        detailFields: document.getElementById('rejDetailFields'),
    };
    const pageStatus = window.gatewayUi.createStatus(els.messageArea, {
        rawDetailElement: els.messageDetail,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });

    const SNAPSHOT_STORAGE_KEY = 'rejections:last-snapshot';

    let offset = 0;
    let snapshot = null;
    let snapshotStale = false;
    let requestGeneration = 0;
    let unsubscribeLocale = null;
    let stopped = false;

    function currentLimit() {
        return Number.parseInt(els.limit.value, 10) || 50;
    }

    function buildQuery() {
        const params = new URLSearchParams();
        params.set('limit', String(currentLimit()));
        params.set('offset', String(offset));
        if (els.category.value) params.set('category', els.category.value);
        if (els.keyId.value !== '') params.set('api_key_id', els.keyId.value);
        if (els.since.value) params.set('since', els.since.value);
        return params.toString();
    }

    function filtersFromUrl() {
        const params = new URLSearchParams(window.location.search);
        return {
            category: params.get('category') || '',
            keyId: params.get('key_id') || '',
            since: params.get('since') || '',
            limit: params.get('limit') || '',
            page: params.get('page') || '',
        };
    }

    function applyUrlFilters(filters) {
        if (filters.category && KNOWN_CATEGORIES.has(filters.category)) {
            els.category.value = filters.category;
        }
        if (filters.keyId !== '') {
            els.keyId.value = filters.keyId;
        }
        if (filters.since) {
            els.since.value = filters.since;
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
        if (els.category.value) params.set('category', els.category.value);
        if (els.keyId.value !== '') params.set('key_id', els.keyId.value);
        if (els.since.value) params.set('since', els.since.value);
        params.set('limit', String(currentLimit()));
        const page = Math.floor(offset / currentLimit()) + 1;
        if (page > 1) params.set('page', String(page));
        const query = params.toString();
        const url = `${window.location.pathname}${query ? `?${query}` : ''}`;
        if (push) window.history.pushState(null, '', url);
        else window.history.replaceState(null, '', url);
    }

    function loadStoredSnapshot() {
        try {
            const raw = window.sessionStorage.getItem(SNAPSHOT_STORAGE_KEY);
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
            return { items: parsed.items, total: parsed.total, loadedAt: parsed.loadedAt };
        } catch (error) {
            return null;
        }
    }

    function persistSnapshot(value) {
        try {
            window.sessionStorage.setItem(SNAPSHOT_STORAGE_KEY, JSON.stringify(value));
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
        if (!snapshotStale || snapshot === null) {
            els.staleBadge.hidden = true;
            els.staleBadge.textContent = '';
            return;
        }
        els.staleBadge.hidden = false;
        els.staleBadge.textContent = relativeStaleLabel(snapshot.loadedAt);
    }

    function formatTime(value) {
        if (!value) return '—';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return i18n.formatDate(date, {dateStyle: 'medium', timeStyle: 'medium'});
    }

    function createCell(value, className = '') {
        const cell = document.createElement('td');
        if (className) cell.className = className;
        cell.textContent = value;
        return cell;
    }

    function createCategoryCell(category) {
        const cell = document.createElement('td');
        const badge = document.createElement('span');
        const classCategory = KNOWN_CATEGORIES.has(category) ? category : 'unknown';
        badge.className = `cat-badge cat-${classCategory}`;
        badge.textContent = String(category ?? '');
        cell.appendChild(badge);
        return cell;
    }

    function detailField(dl, labelKey, value) {
        const dt = document.createElement('dt');
        dt.textContent = i18n.t(labelKey);
        const dd = document.createElement('dd');
        dd.textContent = value;
        dl.appendChild(dt);
        dl.appendChild(dd);
    }

    function openDetailSheet(item) {
        els.detailFields.replaceChildren();
        detailField(els.detailFields, 'rejections:table.time', formatTime(item.timestamp));
        detailField(els.detailFields, 'rejections:table.category', String(item.category ?? '—'));
        detailField(els.detailFields, 'rejections:table.status', String(item.status_code ?? '—'));
        detailField(els.detailFields, 'rejections:table.method', String(item.method || '—'));
        detailField(els.detailFields, 'rejections:table.path', String(item.path || '—'));
        detailField(els.detailFields, 'rejections:table.key', item.api_key_id == null ? '—' : `#${item.api_key_id}`);
        detailField(els.detailFields, 'rejections:detail.xTitle', String(item.x_title || '—'));
        detailField(els.detailFields, 'rejections:table.clientIp', String(item.client_ip || '—'));
        detailField(els.detailFields, 'rejections:table.reason', String(item.reason || '—'));
        detailField(els.detailFields, 'rejections:detail.requestId', String(item.request_id || '—'));
        els.detail.hidden = false;
        els.detailClose.focus();
    }

    function closeDetailSheet() {
        els.detail.hidden = true;
    }

    function renderRows(items) {
        if (items.length === 0) {
            const row = document.createElement('tr');
            const cell = createCell(i18n.t('rejections:empty'), 'rej-empty');
            cell.colSpan = 9;
            row.appendChild(cell);
            els.tbody.replaceChildren(row);
            return;
        }

        const rows = items.map((item) => {
            const row = document.createElement('tr');
            row.tabIndex = 0;
            row.dataset.clickable = 'true';
            row.setAttribute('role', 'button');
            row.dataset.requestId = item.request_id != null ? String(item.request_id) : '';
            row.setAttribute('aria-label', i18n.t('rejections:row.open', {
                requestId: item.request_id || '—',
            }));
            row.addEventListener('click', () => openDetailSheet(item));
            row.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    openDetailSheet(item);
                }
            });

            row.appendChild(createCell(formatTime(item.timestamp)));
            row.appendChild(createCategoryCell(item.category));

            const statusCell = document.createElement('td');
            const status = document.createElement('span');
            status.className = 'status-pill';
            status.textContent = String(item.status_code ?? '—');
            statusCell.appendChild(status);
            row.appendChild(statusCell);

            row.appendChild(createCell(String(item.method || '—')));
            const pathCell = document.createElement('td');
            const path = document.createElement('code');
            path.textContent = String(item.path || '—');
            pathCell.appendChild(path);
            row.appendChild(pathCell);
            row.appendChild(createCell(item.api_key_id == null ? '—' : `#${item.api_key_id}`));
            row.appendChild(createCell(String(item.x_title || '—'), 'rej-title'));
            row.appendChild(createCell(String(item.client_ip || '—')));

            const reason = createCell(String(item.reason || '—'), 'rej-reason');
            if (item.request_id) reason.title = String(item.request_id);
            row.appendChild(reason);
            return row;
        });
        els.tbody.replaceChildren(...rows);
    }

    function renderPagination(itemCount, total) {
        const start = total === 0 ? 0 : offset + 1;
        const end = offset + itemCount;
        els.pageInfo.textContent = total === 0
            ? i18n.t('rejections:pagination.empty')
            : i18n.t('rejections:pagination.range', {
                start: i18n.formatNumber(start),
                end: i18n.formatNumber(end),
                total: i18n.formatNumber(total),
            });
        els.prev.disabled = offset <= 0;
        els.next.disabled = end >= total;
        els.meta.textContent = i18n.t('rejections:summary', {
            count: total,
            time: i18n.formatDate(new Date(snapshot.loadedAt), {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
            }),
        });
    }

    function renderSnapshot() {
        if (snapshot === null) return;
        renderRows(snapshot.items);
        renderPagination(snapshot.items.length, snapshot.total);
        updateStaleBadge();
    }

    function showError(message, rawDetail = null) {
        els.messageArea.hidden = false;
        els.messageDetail.hidden = rawDetail === null;
        pageStatus.error(message, {rawDetail});
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
            if (row) row.focus({preventScroll: true});
        } else if (state.activeElement?.isConnected) {
            state.activeElement.focus({preventScroll: true});
        }
        els.tableWrap.scrollLeft = state.tableLeft;
        els.tableWrap.scrollTop = state.tableTop;
        window.scrollTo(state.scrollX, state.scrollY);
    }

    async function loadRejections() {
        const generation = ++requestGeneration;
        try {
            const response = await apiFetch(`/v1/admin/rejections?${buildQuery()}`);
            if (stopped || generation !== requestGeneration) return;

            if (!response.ok) {
                const body = await response.json().catch(() => ({}));
                if (stopped || generation !== requestGeneration) return;
                if (response.status === 400) {
                    showError(
                        {key: 'rejections:errors.invalidFilters'},
                        window.gatewayUi.describeApiError(body).rawDetail,
                    );
                    return;
                }
                const descriptor = window.gatewayUi.describeApiError(body, {
                    status: response.status,
                });
                if (response.status >= 500 && snapshot !== null) {
                    snapshotStale = true;
                    renderSnapshot();
                }
                showError(
                    {key: descriptor.summaryKey, values: descriptor.summaryValues},
                    descriptor.rawDetail,
                );
                return;
            }

            const data = await response.json();
            if (stopped || generation !== requestGeneration) return;
            if (!Array.isArray(data.items) || !Number.isInteger(data.total) || data.total < 0) {
                showError(
                    {key: 'rejections:errors.invalidResponse'},
                    JSON.stringify(data),
                );
                return;
            }
            snapshot = {
                items: data.items,
                total: data.total,
                loadedAt: Date.now(),
            };
            snapshotStale = false;
            persistSnapshot(snapshot);
            clearError();
            renderSnapshot();
        } catch (error) {
            if (stopped || generation !== requestGeneration) return;
            if (error.message === 'Authentication required') return;
            const descriptor = window.gatewayUi.describeApiError(null);
            if (snapshot !== null) {
                snapshotStale = true;
                renderSnapshot();
            }
            showError(
                {key: descriptor.summaryKey, values: descriptor.summaryValues},
                error.message,
            );
        }
    }

    els.apply.addEventListener('click', () => {
        offset = 0;
        syncUrlFromState(true);
        void loadRejections();
    });
    els.refresh.addEventListener('click', () => void loadRejections());
    els.reset.addEventListener('click', () => {
        els.category.value = '';
        els.keyId.value = '';
        els.since.value = '';
        els.limit.value = '50';
        offset = 0;
        syncUrlFromState(false);
        void loadRejections();
    });
    els.limit.addEventListener('change', () => {
        offset = 0;
        syncUrlFromState(false);
        void loadRejections();
    });
    els.prev.addEventListener('click', () => {
        offset = Math.max(0, offset - currentLimit());
        syncUrlFromState(false);
        void loadRejections();
    });
    els.next.addEventListener('click', () => {
        offset += currentLimit();
        syncUrlFromState(false);
        void loadRejections();
    });
    els.detailClose.addEventListener('click', closeDetailSheet);
    els.detailBackdrop.addEventListener('click', closeDetailSheet);
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !els.detail.hidden) closeDetailSheet();
    });

    window.addEventListener('popstate', () => {
        if (stopped) return;
        applyUrlFilters(filtersFromUrl());
        void loadRejections();
    });

    window.addEventListener('beforeunload', () => {
        stopped = true;
        requestGeneration += 1;
        unsubscribeLocale?.();
    });

    Promise.all([bootstrapRoleUI(), i18n.ready]).then(() => {
        if (stopped) return;
        applyUrlFilters(filtersFromUrl());
        const stored = loadStoredSnapshot();
        if (stored !== null) {
            snapshot = stored;
            snapshotStale = true;
        }
        unsubscribeLocale = i18n.subscribe(() => {
            const viewState = captureViewState();
            renderSnapshot();
            pageStatus.rerender();
            restoreViewState(viewState);
        });
        renderSnapshot();
        void loadRejections();
    }).catch(() => undefined);
});
