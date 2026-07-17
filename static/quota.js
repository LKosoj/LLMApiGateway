document.addEventListener('DOMContentLoaded', () => {
    'use strict';

    const { apiFetch, bootstrapRoleUI } = window.gatewayAuth;
    const i18n = window.gatewayI18n;
    const quotaGrid = document.getElementById('quota-grid');
    const statusEl = document.getElementById('quota-status');
    const retryButton = document.getElementById('quota-retry');
    const upstreamSection = document.getElementById('upstream-quotas-section');
    const upstreamGrid = document.getElementById('upstream-quotas-grid');
    const POLL_INTERVAL_MS = 5000;
    const UPSTREAM_KIND_KEYS = Object.freeze({
        github_copilot: 'quota:upstreamKinds.githubCopilot',
        gemini_cli: 'quota:upstreamKinds.geminiCli',
        antigravity: 'quota:upstreamKinds.antigravity',
    });

    Theme.attachToggle('darkModeToggle');

    const quotaStatus = window.gatewayUi.createStatus(statusEl, {
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });
    const countdownTimers = {};
    let quotaSnapshot = null;
    let quotaUpdatedAt = null;
    let quotaState = 'loading';
    let pollTimer = null;
    let inFlight = false;
    let stopped = false;
    let upstreamSnapshots = null;
    let unsubscribeLocale = null;

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

    function numberText(value) {
        const number = Number(value);
        return i18n.formatNumber(Number.isFinite(number) ? number : 0);
    }

    function formatUsd(value) {
        if (value == null) return '—';
        return i18n.formatCurrency(Number(value), 'USD', {
            minimumFractionDigits: 4,
            maximumFractionDigits: 4,
        });
    }

    function formatUpdatedAt() {
        return i18n.formatDate(new Date(quotaUpdatedAt), {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        });
    }

    function formatResetDate(value) {
        const raw = typeof value === 'string' ? value.trim() : '';
        if (!raw) return '';
        const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(raw) ? `${raw}T00:00:00Z` : raw);
        if (Number.isNaN(date.getTime())) return raw;
        return i18n.formatDate(date, {dateStyle: 'medium', timeZone: 'UTC'});
    }

    function upstreamKindLabel(kind) {
        const raw = typeof kind === 'string' ? kind : '';
        return Object.hasOwn(UPSTREAM_KIND_KEYS, raw)
            ? i18n.t(UPSTREAM_KIND_KEYS[raw])
            : raw;
    }

    function formatCountdown(seconds) {
        const remaining = Math.max(0, Math.round(seconds));
        const minutes = String(Math.floor(remaining / 60)).padStart(2, '0');
        const rest = String(remaining % 60).padStart(2, '0');
        return `${minutes}:${rest}`;
    }

    function clearCountdownTimers() {
        Object.keys(countdownTimers).forEach((cardId) => {
            clearInterval(countdownTimers[cardId]);
            delete countdownTimers[cardId];
        });
    }

    function startCountdownTimer(element, initialSeconds) {
        const cardId = element.dataset.countdownId;
        if (!cardId) return;
        if (countdownTimers[cardId]) clearInterval(countdownTimers[cardId]);

        let remaining = Math.max(0, Math.round(initialSeconds));
        element.textContent = formatCountdown(remaining);
        if (remaining === 0) return;

        const timer = setInterval(() => {
            remaining = Math.max(0, remaining - 1);
            element.textContent = formatCountdown(remaining);
            if (remaining === 0) {
                clearInterval(timer);
                delete countdownTimers[cardId];
            }
        }, 1000);
        countdownTimers[cardId] = timer;
    }

    function colorClass(current, limit) {
        if (limit == null || limit <= 0) return 'color-neutral';
        const ratio = current / limit;
        if (ratio >= 0.85) return 'color-danger';
        if (ratio >= 0.60) return 'color-warn';
        return 'color-ok';
    }

    function elapsedSnapshotSeconds() {
        if (quotaUpdatedAt === null) return 0;
        return Math.max(0, Math.floor((Date.now() - quotaUpdatedAt) / 1000));
    }

    function updateLimitLocale(node, limit) {
        if (limit != null) {
            node.textContent = `/ ${numberText(limit)}`;
            return;
        }
        node.replaceChildren(createNode('span', 'no-limit-label', i18n.t('quota:noLimit')));
    }

    function updateKeyCardLocale(card, keyData) {
        card.querySelector('[data-quota-field="key-id"]').textContent = `#${numberText(keyData.key_id)}`;
        const disabled = card.querySelector('[data-quota-field="disabled"]');
        if (disabled) disabled.textContent = i18n.t('quota:disabled');

        card.querySelector('[data-quota-field="rpm-label"]').textContent = i18n.t('quota:requestsPerMinute');
        card.querySelector('[data-quota-field="rpm-current"]').textContent = numberText(keyData.rpm.current);
        updateLimitLocale(card.querySelector('[data-quota-field="rpm-limit"]'), keyData.rpm.limit);

        card.querySelector('[data-quota-field="tpm-label"]').textContent = i18n.t('quota:tokensPerMinute');
        card.querySelector('[data-quota-field="tpm-current"]').textContent = numberText(keyData.tpm.current || 0);
        updateLimitLocale(card.querySelector('[data-quota-field="tpm-limit"]'), keyData.tpm.limit);

        card.querySelector('[data-quota-field="budget-label"]').textContent = i18n.t('quota:budget');
        const budgetValue = card.querySelector('[data-quota-field="budget-value"]');
        if (keyData.budget.limit_usd == null) {
            budgetValue.replaceChildren(
                document.createTextNode(`${i18n.t('quota:spent', {amount: formatUsd(keyData.budget.spent_usd)})} `),
                createNode('span', 'no-limit-label', `(${i18n.t('quota:noLimit')})`),
            );
        } else {
            budgetValue.textContent = `${formatUsd(keyData.budget.spent_usd)} / ${formatUsd(keyData.budget.limit_usd)}`;
        }

        const fallbackLabel = card.querySelector('[data-quota-field="fallback-label"]');
        if (fallbackLabel) fallbackLabel.textContent = i18n.t('quota:fallbacks24h');
        const fallbackCount = card.querySelector('[data-quota-field="fallback-count"]');
        if (fallbackCount) fallbackCount.textContent = numberText(keyData.fallback_events_24h);
    }

    function createMetric(prefix, reset, color, percentage, countdownId) {
        const metric = createNode('div', 'quota-metric');
        const labelRow = createNode('div', 'quota-metric-label');
        const label = createNode('span');
        label.dataset.quotaField = `${prefix}-label`;
        const countdown = createNode('span', 'countdown-badge', formatCountdown(reset));
        countdown.dataset.countdownId = `${countdownId}-${prefix}`;
        labelRow.append(label, countdown);

        const values = createNode('div', 'quota-metric-values');
        const countClass = prefix === 'rpm'
            ? `quota-metric-count ${color.replace('color-', 'text-')}`
            : 'quota-metric-count';
        const count = createNode('span', countClass);
        count.dataset.quotaField = `${prefix}-current`;
        const limitNode = createNode('span', 'quota-metric-limit');
        limitNode.dataset.quotaField = `${prefix}-limit`;
        values.append(count, limitNode);

        const track = createNode('div', 'progress-bar-track');
        const fill = createNode('div', `progress-bar-fill ${color}`);
        fill.style.width = `${percentage}%`;
        track.appendChild(fill);
        metric.append(labelRow, values, track);
        return metric;
    }

    function renderKeyCard(keyData) {
        const cardId = `quota-card-${keyData.key_id}`;
        let card = document.getElementById(cardId);
        if (!card) {
            card = createNode('div', 'quota-card');
            card.id = cardId;
            quotaGrid.appendChild(card);
        }
        card.dataset.quotaKeyId = String(keyData.key_id);

        const rpm = keyData.rpm;
        const tpm = keyData.tpm;
        const budget = keyData.budget;
        const elapsed = elapsedSnapshotSeconds();
        const rpmReset = Math.max(0, (rpm.reset_in_seconds ?? 60) - elapsed);
        const tpmReset = Math.max(0, (tpm.reset_in_seconds ?? rpm.reset_in_seconds ?? 60) - elapsed);
        const rpmPct = rpm.limit != null && rpm.limit > 0
            ? Math.min(100, Math.round(rpm.current / rpm.limit * 100)) : 0;
        const tpmPct = tpm.limit != null && tpm.limit > 0
            ? Math.min(100, Math.round(tpm.current / tpm.limit * 100)) : 0;
        const rpmColor = colorClass(rpm.current, rpm.limit);
        const tpmColor = colorClass(tpm.current, tpm.limit);
        const countdownId = `cd-${keyData.key_id}`;

        const header = createNode('div', 'quota-card-header');
        const name = markTechnical(createNode('span', 'quota-card-name', keyData.name));
        name.title = String(keyData.name);
        const keyId = createNode('span', 'badge-id');
        keyId.dataset.quotaField = 'key-id';
        header.append(name, keyId);
        if (keyData.disabled) {
            const disabled = createNode('span', 'badge-disabled');
            disabled.dataset.quotaField = 'disabled';
            header.appendChild(disabled);
        }

        const rpmMetric = createMetric('rpm', rpmReset, rpmColor, rpmPct, countdownId);
        const tpmMetric = createMetric('tpm', tpmReset, tpmColor, tpmPct, countdownId);

        const budgetRow = createNode('div', 'quota-budget-row');
        const budgetLabel = createNode('span', 'quota-budget-label');
        budgetLabel.dataset.quotaField = 'budget-label';
        const budgetValue = createNode('span', 'quota-budget-value');
        budgetValue.dataset.quotaField = 'budget-value';
        budgetRow.append(budgetLabel, budgetValue);

        const content = [header, rpmMetric, tpmMetric, budgetRow];
        if (budget.limit_usd != null) {
            const budgetPct = budget.limit_usd > 0
                ? Math.min(100, Math.round(budget.spent_usd / budget.limit_usd * 100)) : 0;
            const budgetTrack = createNode('div', 'progress-bar-track');
            budgetTrack.style.marginTop = '4px';
            const budgetFill = createNode('div', `progress-bar-fill ${colorClass(budget.spent_usd, budget.limit_usd)}`);
            budgetFill.style.width = `${budgetPct}%`;
            budgetTrack.appendChild(budgetFill);
            content.push(budgetTrack);
        }
        if (keyData.fallback_events_24h > 0) {
            const footer = createNode('div', 'quota-footer-row');
            const fallback = createNode('span', 'fallback-count');
            const fallbackLabel = createNode('span');
            fallbackLabel.dataset.quotaField = 'fallback-label';
            const fallbackCount = createNode('span', 'fallback-count-badge');
            fallbackCount.dataset.quotaField = 'fallback-count';
            fallback.append(fallbackLabel, document.createTextNode(' '), fallbackCount);
            footer.appendChild(fallback);
            content.push(footer);
        }

        card.replaceChildren(...content);
        updateKeyCardLocale(card, keyData);
        startCountdownTimer(card.querySelector(`[data-countdown-id="${countdownId}-rpm"]`), rpmReset);
        startCountdownTimer(card.querySelector(`[data-countdown-id="${countdownId}-tpm"]`), tpmReset);
    }

    function renderEmptyState() {
        const empty = createNode('div', 'quota-empty');
        const title = createNode('strong', '', i18n.t('quota:emptyTitle'));
        title.dataset.quotaField = 'empty-title';
        const hint = createNode('span', '', i18n.t('quota:emptyHint'));
        hint.dataset.quotaField = 'empty-hint';
        const action = createNode('a', '', i18n.t('quota:emptyAction'));
        action.dataset.quotaField = 'empty-action';
        action.href = '/v1/ui/api-keys';
        empty.append(title, hint, action);
        quotaGrid.replaceChildren(empty);
    }

    function renderCards(keys) {
        clearCountdownTimers();
        if (keys.length === 0) {
            renderEmptyState();
            return;
        }

        for (const child of [...quotaGrid.children]) {
            if (!child.id?.startsWith('quota-card-')) child.remove();
        }
        const seenIds = new Set();
        keys.forEach((keyData) => {
            seenIds.add(`quota-card-${keyData.key_id}`);
            renderKeyCard(keyData);
        });
        for (const card of [...quotaGrid.children]) {
            if (card.id?.startsWith('quota-card-') && !seenIds.has(card.id)) card.remove();
        }
    }

    function publishQuotaStatus() {
        if (quotaState === 'loading') {
            quotaStatus.busy({key: 'quota:loading'});
        } else if (quotaState === 'initial-error') {
            quotaStatus.error({key: 'quota:initialError'});
        } else if (quotaState === 'stale') {
            quotaStatus.error({key: 'quota:stale', values: {time: formatUpdatedAt()}});
        } else {
            quotaStatus.polite({key: 'quota:lastUpdated', values: {time: formatUpdatedAt()}});
        }
    }

    function renderQuotaState({preserveCards = false} = {}) {
        quotaGrid.dataset.quotaState = quotaState;
        retryButton.textContent = i18n.t('quota:retry');
        retryButton.hidden = quotaState !== 'initial-error';
        if (quotaState === 'loading' || quotaState === 'initial-error') {
            clearCountdownTimers();
            const stateMessage = quotaState === 'loading'
                ? i18n.t('quota:loading')
                : i18n.t('quota:initialError');
            const messageNode = createNode('p', 'quota-empty', stateMessage);
            messageNode.dataset.quotaField = 'state-message';
            quotaGrid.replaceChildren(messageNode);
        } else if (!preserveCards) {
            renderCards(quotaSnapshot);
        }
        publishQuotaStatus();
    }

    function updateQuotaCardsLocale() {
        if (!Array.isArray(quotaSnapshot)) return;
        if (quotaSnapshot.length === 0) {
            const title = quotaGrid.querySelector('[data-quota-field="empty-title"]');
            const hint = quotaGrid.querySelector('[data-quota-field="empty-hint"]');
            const action = quotaGrid.querySelector('[data-quota-field="empty-action"]');
            if (title) title.textContent = i18n.t('quota:emptyTitle');
            if (hint) hint.textContent = i18n.t('quota:emptyHint');
            if (action) action.textContent = i18n.t('quota:emptyAction');
            return;
        }
        quotaSnapshot.forEach((keyData) => {
            const card = document.getElementById(`quota-card-${keyData.key_id}`);
            if (card) updateKeyCardLocale(card, keyData);
        });
    }

    function updateQuotaStateLocale() {
        quotaGrid.dataset.quotaState = quotaState;
        retryButton.textContent = i18n.t('quota:retry');
        retryButton.hidden = quotaState !== 'initial-error';
        const stateMessage = quotaGrid.querySelector('[data-quota-field="state-message"]');
        if (stateMessage) {
            stateMessage.textContent = i18n.t(
                quotaState === 'loading' ? 'quota:loading' : 'quota:initialError',
            );
        }
    }

    function clearPollTimer() {
        if (pollTimer !== null) clearTimeout(pollTimer);
        pollTimer = null;
    }

    function schedulePoll() {
        clearPollTimer();
        if (stopped) return;
        pollTimer = window.setTimeout(() => {
            pollTimer = null;
            void fetchQuotaData();
        }, POLL_INTERVAL_MS);
    }

    async function fetchQuotaData() {
        if (inFlight || stopped) return;
        clearPollTimer();
        inFlight = true;
        let shouldPoll = false;
        try {
            const response = await apiFetch('/v1/api/quota/keys');
            if (stopped) return;
            if (!response.ok) throw new Error(`Quota request failed with status ${response.status}`);
            const data = await response.json();
            if (stopped) return;
            if (!Array.isArray(data)) throw new Error('Quota response must be an array');
            quotaSnapshot = data;
            quotaUpdatedAt = Date.now();
            quotaState = data.length === 0 ? 'empty' : 'ready';
            shouldPoll = true;
            renderQuotaState();
        } catch (error) {
            if (stopped) return;
            if (error.message === 'Authentication required') return;
            quotaState = quotaSnapshot === null ? 'initial-error' : 'stale';
            shouldPoll = quotaSnapshot !== null;
            renderQuotaState({preserveCards: quotaSnapshot !== null});
            console.warn('Quota data request failed');
        } finally {
            inFlight = false;
            if (shouldPoll) schedulePoll();
        }
    }

    function formatUpstreamCategory(category) {
        if (category.unlimited) return i18n.t('quota:unlimited');
        if (category.total > 0) {
            const remaining = category.remaining != null
                ? ` (${i18n.t('quota:left', {count: numberText(category.remaining)})})`
                : '';
            return `${numberText(category.used)} / ${numberText(category.total)}${remaining}`;
        }
        return `${numberText(category.used)} / ${numberText(0)}`;
    }

    function updateUpstreamCardLocale(card, snapshot) {
        const errorBadge = card.querySelector('[data-upstream-field="error-badge"]');
        if (errorBadge) errorBadge.textContent = i18n.t('quota:upstreamError');

        const kind = card.querySelector('[data-upstream-field="kind"]');
        if (kind) {
            kind.textContent = upstreamKindLabel(snapshot.kind);
            if (Object.hasOwn(UPSTREAM_KIND_KEYS, snapshot.kind)) {
                kind.removeAttribute('lang');
                kind.removeAttribute('dir');
            } else {
                markTechnical(kind);
            }
        }

        const plan = card.querySelector('[data-upstream-field="plan"]');
        if (plan) plan.textContent = i18n.t('quota:plan', {plan: snapshot.plan});
        const reset = card.querySelector('[data-upstream-field="reset-value"]');
        if (reset) reset.textContent = i18n.t('quota:resets', {date: formatResetDate(snapshot.reset_date)});

        const summary = card.querySelector('.upstream-card-error-summary');
        if (summary) summary.textContent = i18n.t('quota:upstreamErrorSummary');
        const detail = card.querySelector('.upstream-card-error-detail');
        if (detail) {
            detail.textContent = String(snapshot.error);
            detail.lang = 'und';
            detail.dir = 'auto';
        }

        const tracked = card.querySelector('[data-upstream-field="tracked-externally"]');
        if (tracked) tracked.textContent = i18n.t('quota:upstreamTrackedExternally');
        Object.values(snapshot.categories || {}).forEach((category, index) => {
            const count = card.querySelector(`[data-upstream-category-index="${index}"]`);
            if (count) count.textContent = formatUpstreamCategory(category);
        });
    }

    function renderUpstreamCard(snapshot, index) {
        const card = createNode('div', 'upstream-card');
        card.dataset.upstreamIndex = String(index);

        const header = createNode('div', 'upstream-card-header');
        const provider = markTechnical(createNode('span', 'upstream-card-provider', snapshot.provider));
        provider.title = String(snapshot.provider);
        header.appendChild(provider);
        if (snapshot.error) {
            const badge = createNode('span', 'upstream-badge-error');
            badge.dataset.upstreamField = 'error-badge';
            header.appendChild(badge);
        } else {
            const kind = createNode('span', 'upstream-badge-kind');
            kind.dataset.upstreamField = 'kind';
            header.appendChild(kind);
        }
        card.appendChild(header);

        if (snapshot.plan || snapshot.reset_date) {
            const meta = createNode('div', 'upstream-card-meta');
            if (snapshot.plan) {
                const plan = createNode('span');
                plan.dataset.upstreamField = 'plan';
                meta.appendChild(plan);
            }
            if (snapshot.plan && snapshot.reset_date) {
                meta.appendChild(document.createTextNode(' · '));
            }
            if (snapshot.reset_date) {
                const reset = createNode('span');
                reset.dataset.upstreamField = 'reset-value';
                meta.appendChild(reset);
            }
            card.appendChild(meta);
        }

        if (snapshot.error) {
            const summary = createNode('div', 'upstream-card-error-summary');
            summary.setAttribute('role', 'alert');
            const detail = createNode('div', 'upstream-card-error-detail');
            card.append(summary, detail);
        } else {
            const categories = snapshot.categories || {};
            const entries = Object.entries(categories);
            if (entries.length === 0) {
                const tracked = createNode('div', 'upstream-no-categories');
                tracked.dataset.upstreamField = 'tracked-externally';
                card.appendChild(tracked);
            } else {
                entries.forEach(([categoryName, category], categoryIndex) => {
                    let percentage = 0;
                    let color = 'color-neutral';
                    if (category.unlimited) {
                        percentage = 100;
                        color = 'color-ok';
                    } else if (category.total > 0) {
                        percentage = Math.min(100, Math.round(category.used / category.total * 100));
                        color = percentage >= 85
                            ? 'color-danger'
                            : percentage >= 60 ? 'color-warn' : 'color-ok';
                    }
                    const categoryNode = createNode('div', 'upstream-category');
                    const label = createNode('div', 'upstream-category-label');
                    const name = markTechnical(createNode('span', '', categoryName));
                    const count = createNode('span', 'upstream-category-count');
                    count.dataset.upstreamCategoryIndex = String(categoryIndex);
                    label.append(name, count);
                    const track = createNode('div', 'progress-bar-track');
                    const fill = createNode('div', `progress-bar-fill ${color}`);
                    fill.style.width = `${percentage}%`;
                    track.appendChild(fill);
                    categoryNode.append(label, track);
                    card.appendChild(categoryNode);
                });
            }
        }
        updateUpstreamCardLocale(card, snapshot);
        return card;
    }

    function renderUpstreamSnapshots() {
        if (!upstreamSnapshots || upstreamSnapshots.length === 0) {
            upstreamSection.style.display = 'none';
            return;
        }
        upstreamSection.style.display = '';
        upstreamGrid.replaceChildren(...upstreamSnapshots.map(renderUpstreamCard));
    }

    function updateUpstreamCardsLocale() {
        if (!Array.isArray(upstreamSnapshots)) return;
        upstreamSnapshots.forEach((snapshot, index) => {
            const card = upstreamGrid.querySelector(`[data-upstream-index="${index}"]`);
            if (card) updateUpstreamCardLocale(card, snapshot);
        });
    }

    async function loadUpstreamQuotas() {
        if (stopped) return;
        try {
            const response = await apiFetch('/v1/admin/upstream-quotas');
            if (stopped) return;
            if (response.status === 403 || response.status === 401) {
                upstreamSection.style.display = 'none';
                return;
            }
            if (!response.ok) {
                console.warn('Upstream quota fetch failed:', response.status);
                return;
            }
            const data = await response.json();
            if (stopped) return;
            upstreamSnapshots = data.snapshots || [];
            renderUpstreamSnapshots();
        } catch (error) {
            if (stopped) return;
            if (error.message !== 'Authentication required') {
                console.error('Upstream quota fetch error:', error);
            }
            upstreamSection.style.display = 'none';
        }
    }

    function captureLocaleUiState() {
        const scrollElements = [
            quotaGrid,
            upstreamGrid,
            ...quotaGrid.querySelectorAll('.quota-card'),
            ...upstreamGrid.querySelectorAll('.upstream-card'),
        ];
        return {
            activeElement: document.activeElement,
            windowX: window.scrollX,
            windowY: window.scrollY,
            scrollPositions: scrollElements.map((element) => ({
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
            window.scrollTo(state.windowX, state.windowY);
            if (state.activeElement?.isConnected) {
                state.activeElement.focus({preventScroll: true});
            }
        });
    }

    function rerenderLocale() {
        const uiState = captureLocaleUiState();
        updateQuotaStateLocale();
        updateQuotaCardsLocale();
        updateUpstreamCardsLocale();
        publishQuotaStatus();
        restoreLocaleUiState(uiState);
    }

    retryButton.addEventListener('click', () => {
        if (inFlight) return;
        quotaState = 'loading';
        renderQuotaState();
        void fetchQuotaData();
    });

    window.addEventListener('beforeunload', () => {
        stopped = true;
        clearPollTimer();
        clearCountdownTimers();
        unsubscribeLocale?.();
    });

    Promise.all([bootstrapRoleUI(), i18n.ready]).then(([identity]) => {
        if (stopped) return;
        unsubscribeLocale = i18n.subscribe(rerenderLocale);
        renderQuotaState();
        void fetchQuotaData();
        if (identity.role === 'master') {
            void loadUpstreamQuotas();
        }
    }).catch(() => undefined);
});
