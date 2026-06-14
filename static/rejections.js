document.addEventListener('DOMContentLoaded', () => {
    const { apiFetch, bootstrapRoleUI } = window.gatewayAuth;

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
        tbody: document.getElementById('rejTableBody'),
        prev: document.getElementById('prevPageBtn'),
        next: document.getElementById('nextPageBtn'),
        pageInfo: document.getElementById('pageInfo'),
        messageArea: document.getElementById('messageArea'),
    };

    let offset = 0;

    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function formatTime(ts) {
        if (!ts) return '—';
        const d = new Date(ts);
        return Number.isNaN(d.getTime()) ? escapeHtml(ts) : d.toLocaleString();
    }

    function categoryBadge(category) {
        const klass = KNOWN_CATEGORIES.has(category) ? category : 'unknown';
        return `<span class="cat-badge cat-${klass}">${escapeHtml(category)}</span>`;
    }

    function showError(message) {
        els.messageArea.innerHTML = `<div class="error-banner">${escapeHtml(message)}</div>`;
    }

    function clearError() {
        els.messageArea.innerHTML = '';
    }

    function currentLimit() {
        return parseInt(els.limit.value, 10) || 50;
    }

    function buildQuery() {
        const limit = currentLimit();
        const params = new URLSearchParams();
        params.set('limit', String(limit));
        params.set('offset', String(offset));
        if (els.category.value) params.set('category', els.category.value);
        if (els.keyId.value !== '') params.set('api_key_id', els.keyId.value);
        if (els.since.value) params.set('since', els.since.value);
        return params.toString();
    }

    function renderRows(items) {
        if (!items.length) {
            els.tbody.innerHTML = '<tr><td colspan="9" class="rej-empty">No rejections match the current filters.</td></tr>';
            return;
        }
        els.tbody.innerHTML = items.map((it) => {
            const reqIdTitle = it.request_id ? ` title="request_id: ${escapeHtml(it.request_id)}"` : '';
            return `<tr>
                <td>${formatTime(it.timestamp)}</td>
                <td>${categoryBadge(it.category)}</td>
                <td><span class="status-pill">${escapeHtml(String(it.status_code))}</span></td>
                <td>${escapeHtml(it.method || '—')}</td>
                <td><code>${escapeHtml(it.path || '—')}</code></td>
                <td>${it.api_key_id != null ? '#' + escapeHtml(String(it.api_key_id)) : '—'}</td>
                <td class="rej-title">${escapeHtml(it.x_title || '—')}</td>
                <td>${escapeHtml(it.client_ip || '—')}</td>
                <td class="rej-reason"${reqIdTitle}>${escapeHtml(it.reason || '—')}</td>
            </tr>`;
        }).join('');
    }

    function updatePagination(count, total) {
        const limit = currentLimit();
        const start = total === 0 ? 0 : offset + 1;
        const end = offset + count;
        els.pageInfo.textContent = total === 0 ? '0 of 0' : `${start}–${end} of ${total}`;
        els.prev.disabled = offset <= 0;
        els.next.disabled = end >= total;
        els.meta.textContent = `${total} total rejection${total === 1 ? '' : 's'} · updated ${new Date().toLocaleTimeString()}`;
    }

    async function loadRejections() {
        try {
            const response = await apiFetch(`/v1/admin/rejections?${buildQuery()}`);
            if (response.status === 400) {
                const body = await response.json().catch(() => ({}));
                showError(body.detail || 'Invalid filter values.');
                return;
            }
            if (!response.ok) {
                showError(`Failed to load rejections (HTTP ${response.status}).`);
                return;
            }
            const data = await response.json();
            const items = data.items || [];
            clearError();
            renderRows(items);
            updatePagination(items.length, data.total || 0);
        } catch (err) {
            if (err.message !== 'Authentication required') {
                showError('Network error while loading rejections.');
            }
        }
    }

    els.apply.addEventListener('click', () => { offset = 0; loadRejections(); });
    els.refresh.addEventListener('click', () => { loadRejections(); });
    els.reset.addEventListener('click', () => {
        els.category.value = '';
        els.keyId.value = '';
        els.since.value = '';
        els.limit.value = '50';
        offset = 0;
        loadRejections();
    });
    els.limit.addEventListener('change', () => { offset = 0; loadRejections(); });
    els.prev.addEventListener('click', () => {
        offset = Math.max(0, offset - currentLimit());
        loadRejections();
    });
    els.next.addEventListener('click', () => {
        offset += currentLimit();
        loadRejections();
    });

    bootstrapRoleUI().then(() => loadRejections());
});
