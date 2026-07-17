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
        meta: document.getElementById('rejMeta'),
        tableWrap: document.querySelector('.rej-table-wrap'),
        tbody: document.getElementById('rejTableBody'),
        prev: document.getElementById('prevPageBtn'),
        next: document.getElementById('nextPageBtn'),
        pageInfo: document.getElementById('pageInfo'),
        messageArea: document.getElementById('messageArea'),
        messageDetail: document.getElementById('messageDetail'),
    };
    const pageStatus = window.gatewayUi.createStatus(els.messageArea, {
        rawDetailElement: els.messageDetail,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });

    let offset = 0;
    let snapshot = null;
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
        return {
            activeElement: document.activeElement,
            scrollX: window.scrollX,
            scrollY: window.scrollY,
            tableLeft: els.tableWrap.scrollLeft,
            tableTop: els.tableWrap.scrollTop,
        };
    }

    function restoreViewState(state) {
        if (state.activeElement?.isConnected) {
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
            clearError();
            renderSnapshot();
        } catch (error) {
            if (stopped || generation !== requestGeneration) return;
            if (error.message === 'Authentication required') return;
            const descriptor = window.gatewayUi.describeApiError(null);
            showError(
                {key: descriptor.summaryKey, values: descriptor.summaryValues},
                error.message,
            );
        }
    }

    els.apply.addEventListener('click', () => {
        offset = 0;
        void loadRejections();
    });
    els.refresh.addEventListener('click', () => void loadRejections());
    els.reset.addEventListener('click', () => {
        els.category.value = '';
        els.keyId.value = '';
        els.since.value = '';
        els.limit.value = '50';
        offset = 0;
        void loadRejections();
    });
    els.limit.addEventListener('change', () => {
        offset = 0;
        void loadRejections();
    });
    els.prev.addEventListener('click', () => {
        offset = Math.max(0, offset - currentLimit());
        void loadRejections();
    });
    els.next.addEventListener('click', () => {
        offset += currentLimit();
        void loadRejections();
    });

    window.addEventListener('beforeunload', () => {
        stopped = true;
        requestGeneration += 1;
        unsubscribeLocale?.();
    });

    Promise.all([bootstrapRoleUI(), i18n.ready]).then(() => {
        if (stopped) return;
        unsubscribeLocale = i18n.subscribe(() => {
            const viewState = captureViewState();
            renderSnapshot();
            pageStatus.rerender();
            restoreViewState(viewState);
        });
        void loadRejections();
    }).catch(() => undefined);
});
