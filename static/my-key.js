document.addEventListener('DOMContentLoaded', () => {
    'use strict';

    const QUOTA_ENDPOINT = '/v1/api/quota/keys';
    const DASHBOARD_ENDPOINT = '/v1/api/analytics-dashboard?range=30d';
    const API_BASE_PATH = '/v1';
    const SPEND_DAYS = 14;
    const PACE_DAYS = 7;
    const LOW_BUDGET_SHARE = 0.8;
    const COPIED_RESET_MS = 1500;
    const KNOWN_CATEGORIES = new Set([
        'auth_invalid', 'key_disabled', 'model_not_allowed', 'budget_exhausted',
        'rate_limited', 'master_only', 'unauthorized', 'ip_blocked',
    ]);
    const KNOWN_PERIODS = new Set(['none', 'daily', 'monthly']);
    const { apiFetch, bootstrapRoleUI } = window.gatewayAuth;
    const i18n = window.gatewayI18n;
    const report = window.gatewayReport;
    const { el, cell, headCell, badge, numberValue } = report;

    Theme.attachToggle('darkModeToggle');

    const els = {
        heading: document.getElementById('keyHeading'),
        masterNote: document.getElementById('masterNote'),
        content: document.getElementById('keyContent'),
        callout: document.getElementById('callout'),
        calloutTitle: document.getElementById('calloutTitle'),
        calloutText: document.getElementById('calloutText'),
        budgetHeading: document.getElementById('budgetHeading'),
        budgetReset: document.getElementById('budgetReset'),
        budgetValue: document.getElementById('budgetValue'),
        budgetMeter: document.getElementById('budgetMeter'),
        budgetLeft: document.getElementById('budgetLeft'),
        factRpm: document.getElementById('factRpm'),
        factTpm: document.getElementById('factTpm'),
        factToday: document.getElementById('factToday'),
        baseUrl: document.getElementById('baseUrl'),
        modelList: document.getElementById('modelList'),
        spendChart: document.getElementById('spendChart'),
        rejectionsHint: document.getElementById('rejectionsHint'),
        rejectionsBody: document.getElementById('rejectionsBody'),
        rejectionsEmpty: document.getElementById('rejectionsEmpty'),
        messageArea: document.getElementById('messageArea'),
        messageDetail: document.getElementById('messageDetail'),
    };
    const pageStatus = window.gatewayUi.createStatus(els.messageArea, {
        rawDetailElement: els.messageDetail,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });

    let lastData = null;
    let unsubscribeLocale = null;
    let stopped = false;

    function formatNumber(value) {
        return i18n.formatNumber(numberValue(value));
    }

    function formatMoney(value) {
        const amount = numberValue(value);
        const digits = amount !== 0 && Math.abs(amount) < 1 ? 4 : 2;
        return i18n.formatCurrency(amount, 'USD', { minimumFractionDigits: 2, maximumFractionDigits: digits });
    }

    function formatResetDate(iso) {
        const date = iso ? new Date(iso) : null;
        if (!date || Number.isNaN(date.getTime())) return null;
        return i18n.formatDate(date, { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
    }

    function limitFact(current, limit, format) {
        if (limit === null || limit === undefined) {
            return i18n.t('my_key:facts.noLimit', { current: format(current) });
        }
        return i18n.t('my_key:facts.ofLimit', { current: format(current), limit: format(limit) });
    }

    // Budget state shared by the callout and the budget panel. `days` is how
    // long the remainder lasts at the average daily spend of the last week.
    function budgetState(data) {
        const budget = data.card.budget || {};
        const limited = budget.limit_usd !== null && budget.limit_usd !== undefined;
        const spent = numberValue(budget.spent_usd);
        const limit = numberValue(budget.limit_usd);
        const left = Math.max(0, limit - spent);
        const slots = report.timeSlots(PACE_DAYS, 'day');
        const pace = report.seriesValues(slots, data.dashboard.series?.usage, 'cost')
            .reduce((sum, value) => sum + value, 0) / PACE_DAYS;
        return {
            limited,
            spent,
            limit,
            left,
            share: limited ? (limit > 0 ? spent / limit : 1) : 0,
            exhausted: limited && spent >= limit,
            days: limited && pace > 0 ? Math.max(1, Math.floor(left / pace)) : null,
            resetDate: budget.period !== 'none' ? formatResetDate(budget.reset_at) : null,
        };
    }

    function renderCallout(data, state) {
        let tone = null;
        let title = '';
        let text = '';
        if (data.card.disabled) {
            tone = 'bad';
            title = i18n.t('my_key:callout.disabledTitle');
            text = i18n.t('my_key:callout.disabledText');
        } else if (state.exhausted) {
            tone = 'bad';
            title = i18n.t('my_key:callout.exhaustedTitle');
            text = state.resetDate
                ? i18n.t('my_key:callout.exhaustedText', { date: state.resetDate })
                : i18n.t('my_key:callout.exhaustedNoReset');
        } else if (state.limited && state.share >= LOW_BUDGET_SHARE) {
            tone = 'warn';
            const left = formatMoney(state.left);
            title = state.days === null
                ? i18n.t('my_key:callout.lowTitle', { left })
                : i18n.t('my_key:callout.lowTitleDays', { left, days: state.days });
            text = state.resetDate
                ? i18n.t('my_key:callout.lowText', { date: state.resetDate })
                : i18n.t('my_key:callout.lowNoReset');
        }
        els.callout.hidden = tone === null;
        els.calloutTitle.textContent = title;
        els.calloutText.textContent = text;
        if (tone === 'bad') els.callout.setAttribute('data-tone', 'bad');
        else els.callout.removeAttribute('data-tone');
    }

    function renderBudget(data, state) {
        const period = data.card.budget?.period;
        els.budgetHeading.textContent = KNOWN_PERIODS.has(period)
            ? i18n.t(`my_key:budget.periods.${period}`)
            : i18n.t('my_key:budget.heading');
        els.budgetReset.textContent = state.resetDate ? i18n.t('my_key:budget.resetsAt', { date: state.resetDate }) : '';

        els.budgetValue.replaceChildren(
            formatMoney(state.spent),
            el('small', null, state.limited
                ? i18n.t('my_key:budget.of', { limit: formatMoney(state.limit) })
                : i18n.t('my_key:budget.unlimited')),
        );
        if (state.limited) {
            const bar = report.meter(state.share, state.exhausted ? 'bad' : state.share >= LOW_BUDGET_SHARE ? 'warn' : null);
            bar.classList.add('rp-big-meter');
            els.budgetMeter.replaceChildren(bar);
            const left = formatMoney(state.left);
            els.budgetLeft.textContent = state.days === null
                ? i18n.t('my_key:budget.left', { left })
                : i18n.t('my_key:budget.leftDays', { left, days: state.days });
        } else {
            els.budgetMeter.replaceChildren();
            els.budgetLeft.textContent = '';
        }

        const card = data.card;
        els.factRpm.textContent = limitFact(card.rpm?.current, card.rpm?.limit, formatNumber);
        els.factTpm.textContent = limitFact(
            card.tpm?.current,
            card.tpm?.limit,
            (value) => i18n.formatNumber(numberValue(value), { notation: 'compact', maximumFractionDigits: 1 }),
        );
        const today = report.seriesValues(report.timeSlots(1, 'day'), data.dashboard.series?.usage, 'requests')[0];
        els.factToday.textContent = formatNumber(today);
    }

    function renderConnect(data) {
        els.baseUrl.textContent = new URL(API_BASE_PATH, window.location.origin).href;
        const models = data.card.allowed_models || [];
        const copyModels = document.querySelector('[data-copy="modelList"]');
        copyModels.hidden = models.length === 0;
        els.modelList.textContent = models.length ? models.join(', ') : i18n.t('my_key:connect.allModels');
        els.modelList.setAttribute('title', els.modelList.textContent);
    }

    function renderSpend(data) {
        const slots = report.timeSlots(SPEND_DAYS, 'day');
        const values = report.seriesValues(slots, data.dashboard.series?.usage, 'cost');
        const labels = slots.map((slot) => i18n.formatDate(slot.date, { day: 'numeric', month: 'short' }));
        if (values.every((value) => value === 0)) {
            report.clearChart(els.spendChart, el('div', 'rp-muted', i18n.t('my_key:spend.empty')));
            return;
        }
        report.barChart(els.spendChart, {
            values,
            labels,
            highlight: values.length - 1,
            ariaLabel: i18n.t('my_key:spend.aria'),
            formatTick: formatMoney,
            formatTooltip: (index, value) => i18n.t('my_key:spend.tooltip', { day: labels[index], value: formatMoney(value) }),
        });
    }

    function renderRejections(data) {
        const rejections = data.dashboard.reliability?.rejections || {};
        const rows = rejections.recent || [];
        els.rejectionsHint.textContent = i18n.t('my_key:rejections.hint', {
            count: numberValue(rejections.summary?.rejections),
        });
        els.rejectionsEmpty.hidden = rows.length > 0;
        els.rejectionsBody.replaceChildren(...rows.map((row) => {
            const category = KNOWN_CATEGORIES.has(row.category) ? row.category : 'other';
            const tr = document.createElement('tr');
            tr.append(
                headCell(el('span', null, i18n.formatDate(new Date(row.timestamp), {
                    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
                }))),
                cell(
                    i18n.t('my_key:rejections.columns.what'),
                    badge(i18n.t(`my_key:rejections.categories.${category}`), category === 'rate_limited' ? 'warn' : 'bad'),
                ),
                cell(
                    i18n.t('my_key:rejections.columns.reason'),
                    el('span', 'rp-muted', row.reason || i18n.t('my_key:values.notAvailable')),
                ),
                cell(i18n.t('my_key:rejections.columns.action'), i18n.t(`my_key:rejections.actions.${category}`)),
            );
            return tr;
        }));
    }

    function render() {
        if (!lastData) return;
        els.heading.textContent = lastData.card.name
            ? i18n.t('my_key:headingNamed', { name: lastData.card.name })
            : i18n.t('my_key:heading');
        const state = budgetState(lastData);
        renderCallout(lastData, state);
        renderBudget(lastData, state);
        renderConnect(lastData);
        renderSpend(lastData);
        renderRejections(lastData);
    }

    function showError(message, rawDetail = null) {
        els.messageArea.hidden = false;
        els.messageDetail.hidden = rawDetail === null;
        pageStatus.error(message, { rawDetail });
    }

    async function fetchJson(url) {
        const response = await apiFetch(url);
        const body = await response.json().catch(() => null);
        if (!response.ok) {
            const descriptor = window.gatewayUi.describeApiError(body, { status: response.status });
            const error = new Error(descriptor.rawDetail || `HTTP ${response.status}`);
            error.descriptor = descriptor;
            throw error;
        }
        return body;
    }

    async function load() {
        try {
            const [quota, dashboard] = await Promise.all([
                fetchJson(QUOTA_ENDPOINT),
                fetchJson(DASHBOARD_ENDPOINT),
            ]);
            if (stopped) return;
            const card = (Array.isArray(quota) ? quota : [])[0];
            if (!card) throw new Error('Quota card for the current key is missing');
            lastData = { card, dashboard };
            els.content.hidden = false;
            render();
        } catch (error) {
            if (stopped || error.message === 'Authentication required') return;
            const descriptor = error.descriptor || window.gatewayUi.describeApiError(null);
            showError({ key: descriptor.summaryKey, values: descriptor.summaryValues }, descriptor.rawDetail || error.message);
        }
    }

    document.querySelectorAll('[data-copy]').forEach((button) => {
        button.addEventListener('click', () => {
            const source = document.getElementById(button.dataset.copy);
            navigator.clipboard?.writeText(source.textContent).then(() => {
                button.textContent = i18n.t('my_key:connect.copied');
                setTimeout(() => {
                    button.textContent = i18n.t('my_key:connect.copy');
                }, COPIED_RESET_MS);
            }).catch(() => undefined);
        });
    });

    window.addEventListener('beforeunload', () => {
        stopped = true;
        unsubscribeLocale?.();
    });

    Promise.all([bootstrapRoleUI(), i18n.ready]).then(([identity]) => {
        if (stopped) return;
        unsubscribeLocale = i18n.subscribe(() => {
            pageStatus.rerender();
            render();
        });
        if (identity.role === 'master') {
            els.masterNote.hidden = false;
            return;
        }
        void load();
    }).catch(() => undefined);
});
