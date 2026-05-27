document.addEventListener('DOMContentLoaded', () => {
    const { apiFetch, bootstrapRoleUI } = window.gatewayAuth;

    // ── Dark mode ──────────────────────────────────────────────────────────
    Theme.attachToggle('darkModeToggle');

    // ── State ──────────────────────────────────────────────────────────────
    const quotaGrid = document.getElementById('quota-grid');
    const statusEl = document.getElementById('quota-status');

    // Track running countdown timers per card so we can clear them on re-render
    const _countdownTimers = {};

    // ── Helpers ────────────────────────────────────────────────────────────

    function formatCountdown(seconds) {
        const s = Math.max(0, Math.round(seconds));
        const mm = String(Math.floor(s / 60)).padStart(2, '0');
        const ss = String(s % 60).padStart(2, '0');
        return `${mm}:${ss}`;
    }

    function startCountdownTimer(element, initialSeconds) {
        const cardId = element.dataset.countdownId;
        if (!cardId) return;

        // Clear any previous timer for this element
        if (_countdownTimers[cardId]) {
            clearInterval(_countdownTimers[cardId]);
        }

        let remaining = Math.max(0, Math.round(initialSeconds));
        element.textContent = formatCountdown(remaining);

        const timer = setInterval(() => {
            remaining = Math.max(0, remaining - 1);
            element.textContent = formatCountdown(remaining);
            if (remaining <= 0) {
                clearInterval(timer);
                delete _countdownTimers[cardId];
            }
        }, 1000);

        _countdownTimers[cardId] = timer;
    }

    function colorClass(current, limit) {
        if (limit == null || limit <= 0) return 'color-neutral';
        const pct = current / limit;
        if (pct >= 0.85) return 'color-danger';
        if (pct >= 0.60) return 'color-warn';
        return 'color-ok';
    }

    function renderProgressBar(current, limit, trackEl) {
        let pct = 0;
        let cls = 'color-neutral';
        if (limit != null && limit > 0) {
            pct = Math.min(100, Math.round((current / limit) * 100));
            cls = colorClass(current, limit);
        }
        trackEl.innerHTML = `<div class="progress-bar-fill ${cls}" style="width:${pct}%"></div>`;
    }

    function formatUsd(value) {
        if (value == null) return '—';
        return '$' + Number(value).toFixed(4);
    }

    // ── Card rendering ─────────────────────────────────────────────────────

    function renderKeyCard(keyData) {
        const cardId = `quota-card-${keyData.key_id}`;
        let card = document.getElementById(cardId);
        const isNew = !card;

        if (isNew) {
            card = document.createElement('div');
            card.className = 'quota-card';
            card.id = cardId;
            quotaGrid.appendChild(card);
        }

        const rpm = keyData.rpm;
        const tpm = keyData.tpm;
        const budget = keyData.budget;
        const resetSec = rpm.reset_in_seconds ?? 60;

        const rpmLimitLabel = rpm.limit != null
            ? `/ ${numText(rpm.limit)}`
            : '<span class="no-limit-label">no limit</span>';
        const tpmLimitLabel = tpm.limit != null
            ? `/ ${numText(tpm.limit)}`
            : '<span class="no-limit-label">no limit</span>';

        const rpmPct = rpm.limit != null && rpm.limit > 0
            ? Math.min(100, Math.round(rpm.current / rpm.limit * 100)) : 0;
        const tpmPct = tpm.limit != null && tpm.limit > 0
            ? Math.min(100, Math.round(tpm.current / tpm.limit * 100)) : 0;

        const rpmColorCls = colorClass(rpm.current, rpm.limit);
        const tpmColorCls = colorClass(tpm.current, tpm.limit);

        const budgetHtml = (() => {
            if (budget.limit_usd == null) {
                return `<div class="quota-budget-row">
                    <span class="quota-budget-label">Budget</span>
                    <span class="quota-budget-value">${formatUsd(budget.spent_usd)} spent <span class="no-limit-label">(no limit)</span></span>
                </div>`;
            }
            const bPct = budget.limit_usd > 0
                ? Math.min(100, Math.round(budget.spent_usd / budget.limit_usd * 100)) : 0;
            const bCls = colorClass(budget.spent_usd, budget.limit_usd);
            return `<div class="quota-budget-row">
                <span class="quota-budget-label">Budget</span>
                <span class="quota-budget-value">${formatUsd(budget.spent_usd)} / ${formatUsd(budget.limit_usd)}</span>
            </div>
            <div class="progress-bar-track" style="margin-top:4px;">
                <div class="progress-bar-fill ${bCls}" style="width:${bPct}%"></div>
            </div>`;
        })();

        const fallbackHtml = keyData.fallback_events_24h > 0
            ? `<div class="quota-footer-row">
                <span class="fallback-count">
                    Fallbacks (24h):
                    <span class="fallback-count-badge">${numText(keyData.fallback_events_24h)}</span>
                </span>
            </div>`
            : '';

        const countdownId = `cd-${numText(keyData.key_id)}`;

        card.innerHTML = `
            <div class="quota-card-header">
                <span class="quota-card-name" title="${escapeHtml(keyData.name)}">${escapeHtml(keyData.name)}</span>
                <span class="badge-id">#${numText(keyData.key_id)}</span>
                ${keyData.disabled ? '<span class="badge-disabled">disabled</span>' : ''}
            </div>

            <div class="quota-metric">
                <div class="quota-metric-label">
                    <span>Requests / min (RPM)</span>
                    <span class="countdown-badge" data-countdown-id="${countdownId}-rpm">${formatCountdown(resetSec)}</span>
                </div>
                <div class="quota-metric-values">
                    <span class="quota-metric-count ${rpmColorCls.replace('color-', 'text-')}">${numText(rpm.current)}</span>
                    <span class="quota-metric-limit">${rpmLimitLabel}</span>
                </div>
                <div class="progress-bar-track">
                    <div class="progress-bar-fill ${rpmColorCls}" style="width:${rpmPct}%"></div>
                </div>
            </div>

            <div class="quota-metric">
                <div class="quota-metric-label">
                    <span>Tokens / min (TPM)</span>
                    <span class="countdown-badge" data-countdown-id="${countdownId}-tpm">${formatCountdown(resetSec)}</span>
                </div>
                <div class="quota-metric-values">
                    <span class="quota-metric-count">${Number(tpm.current || 0).toLocaleString()}</span>
                    <span class="quota-metric-limit">${tpmLimitLabel}</span>
                </div>
                <div class="progress-bar-track">
                    <div class="progress-bar-fill ${tpmColorCls}" style="width:${tpmPct}%"></div>
                </div>
            </div>

            ${budgetHtml}
            ${fallbackHtml}
        `;

        // Start countdown timers for this card
        const rpmCd = card.querySelector(`[data-countdown-id="${countdownId}-rpm"]`);
        const tpmCd = card.querySelector(`[data-countdown-id="${countdownId}-tpm"]`);
        if (rpmCd) startCountdownTimer(rpmCd, resetSec);
        if (tpmCd) startCountdownTimer(tpmCd, resetSec);

        return card;
    }

    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function numText(value) {
        const n = Number(value);
        return Number.isFinite(n) ? String(n) : '0';
    }

    // ── Render all cards ───────────────────────────────────────────────────

    function renderAllCards(keys) {
        if (!keys || keys.length === 0) {
            quotaGrid.innerHTML = '<p class="quota-empty">No API keys found.</p>';
            return;
        }

        const seenIds = new Set();
        for (const k of keys) {
            seenIds.add(`quota-card-${k.key_id}`);
            renderKeyCard(k);
        }

        // Remove cards for keys that no longer exist
        for (const card of [...quotaGrid.children]) {
            if (card.id && card.id.startsWith('quota-card-') && !seenIds.has(card.id)) {
                card.remove();
            }
        }
    }

    // ── Polling ────────────────────────────────────────────────────────────

    async function fetchQuotaData() {
        try {
            const response = await apiFetch('/v1/api/quota/keys');
            if (!response.ok) {
                console.warn('Quota fetch failed:', response.status);
                return;
            }
            const data = await response.json();
            renderAllCards(data);
            if (statusEl) {
                statusEl.textContent = `Last updated: ${new Date().toLocaleTimeString()}`;
            }
        } catch (err) {
            if (err.message !== 'Authentication required') {
                console.error('Quota fetch error:', err);
            }
        }
    }

    function initPolling() {
        fetchQuotaData();
        const _pollingInterval = setInterval(fetchQuotaData, 5000);
        window.addEventListener('beforeunload', () => clearInterval(_pollingInterval));
    }

    // ── Upstream Subscriptions ─────────────────────────────────────────────

    const upstreamSection = document.getElementById('upstream-quotas-section');
    const upstreamGrid = document.getElementById('upstream-quotas-grid');

    function renderUpstreamCard(snapshot) {
        const card = document.createElement('div');
        card.className = 'upstream-card';

        const metaParts = [];
        if (snapshot.plan) metaParts.push(`Plan: ${escapeHtml(snapshot.plan)}`);
        if (snapshot.reset_date) metaParts.push(`Resets: ${escapeHtml(snapshot.reset_date)}`);
        const metaHtml = metaParts.length
            ? `<div class="upstream-card-meta">${metaParts.join(' &nbsp;·&nbsp; ')}</div>`
            : '';

        let bodyHtml = '';
        if (snapshot.error) {
            bodyHtml = `<div class="upstream-card-error">${escapeHtml(snapshot.error)}</div>`;
        } else {
            const categories = snapshot.categories || {};
            const catKeys = Object.keys(categories);
            if (catKeys.length === 0) {
                bodyHtml = `<div class="upstream-no-categories">Quota tracked via provider console.</div>`;
            } else {
                for (const catName of catKeys) {
                    const cat = categories[catName];
                    let pct = 0;
                    let cls = 'color-neutral';
                    let countLabel = '';
                    if (cat.unlimited) {
                        cls = 'color-ok';
                        pct = 100;
                        countLabel = '<span style="opacity:0.6">unlimited</span>';
                    } else if (cat.total > 0) {
                        pct = Math.min(100, Math.round(cat.used / cat.total * 100));
                        cls = pct >= 85 ? 'color-danger' : pct >= 60 ? 'color-warn' : 'color-ok';
                        const rem = cat.remaining != null ? ` / ${cat.total} (${cat.remaining} left)` : ` / ${cat.total}`;
                        countLabel = `${cat.used}${rem}`;
                    } else {
                        countLabel = `${cat.used} / 0`;
                    }
                    bodyHtml += `
                        <div class="upstream-category">
                            <div class="upstream-category-label">
                                <span>${escapeHtml(catName)}</span>
                                <span class="upstream-category-count">${countLabel}</span>
                            </div>
                            <div class="progress-bar-track">
                                <div class="progress-bar-fill ${cls}" style="width:${pct}%"></div>
                            </div>
                        </div>`;
                }
            }
        }

        card.innerHTML = `
            <div class="upstream-card-header">
                <span class="upstream-card-provider" title="${escapeHtml(snapshot.provider)}">${escapeHtml(snapshot.provider)}</span>
                ${snapshot.error
                    ? '<span class="upstream-badge-error">error</span>'
                    : `<span class="upstream-badge-kind">${escapeHtml(snapshot.kind)}</span>`
                }
            </div>
            ${metaHtml}
            ${bodyHtml}
        `;
        return card;
    }

    async function loadUpstreamQuotas() {
        if (!upstreamGrid || !upstreamSection) return;
        try {
            const response = await apiFetch('/v1/admin/upstream-quotas');
            if (response.status === 403 || response.status === 401) {
                // Virtual key or unauthenticated — hide section
                upstreamSection.style.display = 'none';
                return;
            }
            if (!response.ok) {
                console.warn('Upstream quota fetch failed:', response.status);
                return;
            }
            const data = await response.json();
            const snapshots = data.snapshots || [];
            if (snapshots.length === 0) {
                upstreamSection.style.display = 'none';
                return;
            }
            upstreamSection.style.display = '';
            upstreamGrid.innerHTML = '';
            for (const s of snapshots) {
                upstreamGrid.appendChild(renderUpstreamCard(s));
            }
        } catch (err) {
            if (err.message !== 'Authentication required') {
                console.error('Upstream quota fetch error:', err);
            }
            upstreamSection.style.display = 'none';
        }
    }

    // ── Bootstrap ──────────────────────────────────────────────────────────
    bootstrapRoleUI().then(() => {
        initPolling();
        loadUpstreamQuotas();
    });
});
