document.addEventListener('DOMContentLoaded', () => {
    'use strict';

    const DASHBOARD_ENDPOINT = '/v1/api/analytics-dashboard?range=24h';
    const COMPARE_QUERY = '&compare=previous';
    const RECORDS_ENDPOINT = '/v1/api/usage-records?limit=50';
    const STATUS_ENDPOINT = '/v1/api/upstream-status';
    const REFRESH_INTERVAL_MS = 15000;
    const LONG_REQUEST_MS = 30000;
    const RUNNING_LIMIT = 20;
    const TARGETS_LIMIT = 15;
    const WHO_LIMIT = 6;
    const { apiFetch, bootstrapRoleUI } = window.gatewayAuth;
    const i18n = window.gatewayI18n;
    const report = window.gatewayReport;
    const { el, cell, headCell, target, badge, numberValue } = report;

    Theme.attachToggle('darkModeToggle');

    const els = {
        updatedAt: document.getElementById('updatedAt'),
        pause: document.getElementById('pauseBtn'),
        refresh: document.getElementById('refreshBtn'),
        kpiRunning: document.getElementById('kpiRunning'),
        kpiRpm: document.getElementById('kpiRpm'),
        kpiTpm: document.getElementById('kpiTpm'),
        kpiCost: document.getElementById('kpiCost'),
        loadMetric: document.getElementById('loadMetric'),
        loadChart: document.getElementById('loadChart'),
        whoGroup: document.getElementById('whoGroup'),
        whoList: document.getElementById('whoList'),
        runningBody: document.getElementById('runningBody'),
        runningEmpty: document.getElementById('runningEmpty'),
        targetsBody: document.getElementById('targetsBody'),
        targetsEmpty: document.getElementById('targetsEmpty'),
        modelDetail: document.getElementById('modelDetail'),
        modelDetailTitle: document.getElementById('modelDetailTitle'),
        modelDetailChart: document.getElementById('modelDetailChart'),
        modelDetailClose: document.getElementById('modelDetailClose'),
        messageArea: document.getElementById('messageArea'),
        messageDetail: document.getElementById('messageDetail'),
    };
    const pageStatus = window.gatewayUi.createStatus(els.messageArea, {
        rawDetailElement: els.messageDetail,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });

    let lastData = null;
    let detailTarget = null;
    let detailData = null;
    let detailGeneration = 0;
    let loadMetric = 'requests';
    let whoGroup = 'keys';
    let paused = false;
    let timer = null;
    let requestGeneration = 0;
    let unsubscribeLocale = null;
    let stopped = false;

    function formatNumber(value) {
        return i18n.formatNumber(numberValue(value));
    }

    function formatCompact(value) {
        return i18n.formatNumber(numberValue(value), { notation: 'compact', maximumFractionDigits: 1 });
    }

    function formatMoney(value) {
        const amount = numberValue(value);
        const digits = amount !== 0 && Math.abs(amount) < 1 ? 4 : 2;
        return i18n.formatCurrency(amount, 'USD', { minimumFractionDigits: 2, maximumFractionDigits: digits });
    }

    function formatDuration(ms) {
        const seconds = numberValue(ms) / 1000;
        if (seconds < 60) {
            return i18n.t('live:values.seconds', { value: i18n.formatNumber(Math.round(seconds)) });
        }
        return i18n.t('live:values.minutes', {
            value: i18n.formatNumber(seconds / 60, { maximumFractionDigits: 1 }),
        });
    }

    function formatCountdown(seconds) {
        const total = Math.max(0, Math.round(numberValue(seconds)));
        return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
    }

    function formatHour(date) {
        return i18n.formatDate(date, { hour: '2-digit', minute: '2-digit' });
    }

    function formatMetric(metric, value) {
        if (metric === 'cost') return formatMoney(value);
        if (metric === 'total_tokens') return formatCompact(value);
        return formatNumber(value);
    }

    function setKpi(card, value, note, tone) {
        card.querySelector('[data-kpi-value]').textContent = value;
        card.querySelector('[data-kpi-note]').textContent = note;
        if (tone) card.setAttribute('data-tone', tone);
        else card.removeAttribute('data-tone');
    }

    // "▲ 12% vs the previous 24 hours"; empty when there is nothing to compare.
    function deltaText(current, previous) {
        const change = report.changeOf(current, previous);
        if (!change) return '';
        const percent = i18n.formatNumber(change.ratio || 0, { style: 'percent', maximumFractionDigits: 0 });
        return i18n.t(`live:delta.${change.direction}`, { percent });
    }

    function keyNames(data) {
        const names = new Map();
        (data.dashboard.breakdowns?.api_keys || []).forEach((row) => {
            if (row.api_key_id !== null && row.api_key_id !== undefined) names.set(row.api_key_id, row.api_key_name);
        });
        return names;
    }

    function keyLabel(apiKeyId, names) {
        if (apiKeyId === null || apiKeyId === undefined) return i18n.t('live:who.masterKey');
        return names.get(apiKeyId) || `#${apiKeyId}`;
    }

    function runningRecords(data) {
        return (data.records || [])
            .filter((row) => row.status === 'running')
            .sort((a, b) => numberValue(b.duration_ms) - numberValue(a.duration_ms));
    }

    function renderKpis(data) {
        const totals = data.dashboard.totals || {};
        const running = runningRecords(data);
        const longest = running[0];
        setKpi(
            els.kpiRunning,
            formatNumber(running.length),
            longest
                ? i18n.t('live:kpis.runningNote', {
                    duration: formatDuration(longest.duration_ms),
                    model: longest.model || longest.gateway_model || i18n.t('live:values.notAvailable'),
                })
                : i18n.t('live:kpis.runningNone'),
            longest && numberValue(longest.duration_ms) >= LONG_REQUEST_MS ? 'warn' : null,
        );

        const rows = data.status || [];
        const rpm = rows.reduce((sum, row) => sum + numberValue(row.requests_last_minute), 0);
        const tpm = rows.reduce((sum, row) => sum + numberValue(row.tokens_last_minute), 0);
        const slots = report.timeSlots(24, 'hour');
        const hourly = report.seriesValues(slots, data.dashboard.series?.usage, 'requests');
        const peak = Math.max(0, ...hourly);
        const peakIndex = hourly.indexOf(peak);
        setKpi(
            els.kpiRpm,
            formatNumber(rpm),
            peak > 0
                ? i18n.t('live:kpis.rpmNote', {
                    requests: formatNumber(totals.requests),
                    peak: formatNumber(peak),
                    hour: formatHour(slots[peakIndex].date),
                })
                : i18n.t('live:kpis.rpmNoteNoPeak', { requests: formatNumber(totals.requests) }),
        );
        setKpi(
            els.kpiTpm,
            formatCompact(tpm),
            i18n.t('live:kpis.tpmNote', { tokens: formatCompact(totals.total_tokens) }),
        );

        const topCost = [...(data.dashboard.breakdowns?.resolved_targets || [])]
            .sort((a, b) => numberValue(b.cost) - numberValue(a.cost))[0];
        setKpi(
            els.kpiCost,
            formatMoney(totals.cost),
            topCost && numberValue(topCost.cost) > 0
                ? i18n.t('live:kpis.costNote', { model: topCost.model, cost: formatMoney(topCost.cost) })
                : i18n.t('live:kpis.costNone'),
        );

        const previous = data.dashboard.previous?.totals;
        report.setKpiDelta(els.kpiRpm, previous ? deltaText(totals.requests, previous.requests) : '');
        report.setKpiDelta(els.kpiTpm, previous ? deltaText(totals.total_tokens, previous.total_tokens) : '');
        report.setKpiDelta(els.kpiCost, previous ? deltaText(totals.cost, previous.cost) : '');
    }

    function renderLoad(data) {
        const slots = report.timeSlots(24, 'hour');
        const values = report.seriesValues(slots, data.dashboard.series?.usage, loadMetric);
        const labels = slots.map((slot) => formatHour(slot.date));
        report.barChart(els.loadChart, {
            values,
            labels,
            highlight: values.length - 1,
            ariaLabel: i18n.t('live:load.aria'),
            formatTick: (value) => formatMetric(loadMetric === 'requests' ? 'total_tokens' : loadMetric, value),
            formatTooltip: (index, value) => i18n.t('live:load.tooltip', {
                hour: labels[index],
                value: formatMetric(loadMetric, value),
            }),
        });
    }

    function renderWho(data) {
        const breakdowns = data.dashboard.breakdowns || {};
        const source = whoGroup === 'keys' ? breakdowns.api_keys : breakdowns.x_titles;
        const rows = (source || []).slice(0, WHO_LIMIT).map((row) => {
            let label = row.label;
            if (whoGroup === 'keys') label = keyLabel(row.api_key_id, new Map([[row.api_key_id, row.api_key_name]]));
            else if (row.label === 'unknown') label = i18n.t('live:who.unknownClient');
            return { label, value: row.requests, display: formatNumber(row.requests) };
        });
        if (rows.length === 0) {
            els.whoList.replaceChildren(el('div', 'rp-muted', i18n.t('live:who.empty')));
            return;
        }
        report.rankList(els.whoList, rows);
    }

    function renderRunning(data) {
        const names = keyNames(data);
        const rows = runningRecords(data).slice(0, RUNNING_LIMIT);
        els.runningEmpty.hidden = rows.length > 0;
        els.runningBody.replaceChildren(...rows.map((row) => {
            const long = numberValue(row.duration_ms) >= LONG_REQUEST_MS;
            const tr = document.createElement('tr');
            tr.append(
                headCell(badge(formatDuration(row.duration_ms), long ? 'warn' : null)),
                cell(i18n.t('live:running.columns.requested'), row.gateway_model || i18n.t('live:values.notAvailable')),
                cell(
                    i18n.t('live:running.columns.executes'),
                    row.model
                        ? target(row.model, row.provider)
                        : el('span', 'rp-muted', i18n.t('live:running.notChosen')),
                ),
                cell(i18n.t('live:running.columns.key'), keyLabel(row.api_key_id, names)),
                cell(i18n.t('live:running.columns.client'), row.x_title || i18n.t('live:values.notAvailable')),
                cell(i18n.t('live:running.columns.operation'), row.operation || i18n.t('live:values.notAvailable')),
            );
            return tr;
        }));
    }

    function targetStatus(row) {
        if (numberValue(row.cooldown_remaining_seconds) > 0) {
            return badge(
                i18n.t('live:targets.status.cooldown', { time: formatCountdown(row.cooldown_remaining_seconds) }),
                'warn',
            );
        }
        if (row.health_status === 'invalid') return badge(i18n.t('live:targets.status.invalid'), 'bad');
        if (row.health_status === 'error') return badge(i18n.t('live:targets.status.error'), 'bad');
        if (row.health_status === 'healthy') return badge(i18n.t('live:targets.status.ok'), 'ok');
        return badge(i18n.t('live:targets.status.unknown'));
    }

    function rpmCell(row) {
        const current = numberValue(row.requests_last_minute);
        const limit = numberValue(row.observed_limits?.rpm?.limit);
        if (!limit) return el('span', null, formatNumber(current));
        const share = current / limit;
        const wrap = el('div', 'rp-meter-cell');
        wrap.append(
            report.meter(share, share >= 0.9 ? 'bad' : share >= 0.7 ? 'warn' : null),
            el('span', null, i18n.t('live:targets.rpmOf', { current: formatNumber(current), limit: formatNumber(limit) })),
        );
        return wrap;
    }

    function renderTargets(data) {
        const rows = (data.status || [])
            .filter((row) => numberValue(row.requests_last_day) > 0)
            .sort((a, b) => (
                numberValue(b.requests_last_minute) - numberValue(a.requests_last_minute)
                || numberValue(b.requests_last_day) - numberValue(a.requests_last_day)
            ))
            .slice(0, TARGETS_LIMIT);
        els.targetsEmpty.hidden = rows.length > 0;
        els.targetsBody.replaceChildren(...rows.map((row) => {
            const tr = document.createElement('tr');
            const open = report.targetButton(row.model, row.provider);
            open.addEventListener('click', () => openDetail(row));
            tr.append(
                headCell(open),
                cell(
                    i18n.t('live:targets.columns.key'),
                    el('span', 'rp-mono', i18n.t('live:targets.keyFingerprint', {
                        fingerprint: String(row.upstream_key_fingerprint || '').slice(-4),
                    })),
                ),
                cell(i18n.t('live:targets.columns.rpm'), rpmCell(row)),
                cell(i18n.t('live:targets.columns.tpm'), formatCompact(row.tokens_last_minute), 'rp-num'),
                cell(i18n.t('live:targets.columns.day'), formatNumber(row.requests_last_day), 'rp-num'),
                cell(i18n.t('live:targets.columns.status'), targetStatus(row)),
            );
            return tr;
        }));
    }

    function detailQuery(row) {
        const params = new URLSearchParams({
            provider: row.provider || '',
            model: row.model || '',
            upstream_key_fingerprint: row.upstream_key_fingerprint || '',
        });
        return `${DASHBOARD_ENDPOINT}&${params}`;
    }

    function renderDetail() {
        if (!detailTarget) return;
        els.modelDetailTitle.textContent = i18n.t('live:detail.heading', {
            model: detailTarget.model,
            provider: detailTarget.provider,
        });
        if (!detailData) return;
        const slots = report.timeSlots(24, 'hour');
        const values = report.seriesValues(slots, detailData.series?.usage, 'requests');
        const labels = slots.map((slot) => formatHour(slot.date));
        report.barChart(els.modelDetailChart, {
            values,
            labels,
            highlight: values.length - 1,
            ariaLabel: i18n.t('live:detail.aria', { model: detailTarget.model }),
            formatTick: (value) => formatCompact(value),
            formatTooltip: (index, value) => i18n.t('live:load.tooltip', {
                hour: labels[index],
                value: formatNumber(value),
            }),
        });
    }

    async function loadDetail() {
        if (!detailTarget) return;
        const generation = ++detailGeneration;
        try {
            const dashboard = await fetchJson(detailQuery(detailTarget));
            if (stopped || generation !== detailGeneration) return;
            detailData = dashboard;
            renderDetail();
        } catch (error) {
            if (stopped || generation !== detailGeneration) return;
            if (error.message === 'Authentication required') return;
            const descriptor = error.descriptor || window.gatewayUi.describeApiError(null);
            showError(
                { key: descriptor.summaryKey, values: descriptor.summaryValues },
                descriptor.rawDetail || error.message,
            );
        }
    }

    function openDetail(row) {
        detailTarget = {
            provider: row.provider,
            model: row.model,
            upstream_key_fingerprint: row.upstream_key_fingerprint,
        };
        detailData = null;
        report.clearChart(els.modelDetailChart, el('div', 'rp-muted', i18n.t('live:detail.loading')));
        els.modelDetail.hidden = false;
        renderDetail();
        els.modelDetailTitle.focus();
        void loadDetail();
    }

    function closeDetail() {
        detailTarget = null;
        detailData = null;
        detailGeneration += 1;
        els.modelDetail.hidden = true;
    }

    function renderUpdated() {
        if (!lastData) {
            els.updatedAt.textContent = '';
            return;
        }
        const time = i18n.formatDate(lastData.loadedAt, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        els.updatedAt.textContent = i18n.t(paused ? 'live:updatedPaused' : 'live:updated', { time });
        els.updatedAt.dataset.state = paused ? 'paused' : 'live';
        els.pause.textContent = i18n.t(paused ? 'live:actions.resume' : 'live:actions.pause');
        els.pause.setAttribute('aria-pressed', String(paused));
    }

    function render() {
        if (!lastData) return;
        renderKpis(lastData);
        renderLoad(lastData);
        renderWho(lastData);
        renderRunning(lastData);
        renderTargets(lastData);
        renderDetail();
        renderUpdated();
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
        const generation = ++requestGeneration;
        void loadDetail();
        try {
            const [dashboard, records, status] = await Promise.all([
                fetchJson(DASHBOARD_ENDPOINT + COMPARE_QUERY),
                fetchJson(RECORDS_ENDPOINT),
                fetchJson(STATUS_ENDPOINT),
            ]);
            if (stopped || generation !== requestGeneration) return;
            lastData = {
                dashboard,
                records: records.records || [],
                status: status.rows || [],
                loadedAt: new Date(),
            };
            clearError();
            render();
        } catch (error) {
            if (stopped || generation !== requestGeneration) return;
            if (error.message === 'Authentication required') return;
            const descriptor = error.descriptor || window.gatewayUi.describeApiError(null);
            showError(
                { key: descriptor.summaryKey, values: descriptor.summaryValues },
                descriptor.rawDetail || error.message,
            );
        }
    }

    function schedule() {
        clearInterval(timer);
        timer = null;
        if (paused || stopped) return;
        timer = setInterval(() => {
            if (!document.hidden) void load();
        }, REFRESH_INTERVAL_MS);
    }

    els.refresh.addEventListener('click', () => void load());
    els.modelDetailClose.addEventListener('click', closeDetail);
    els.pause.addEventListener('click', () => {
        paused = !paused;
        schedule();
        renderUpdated();
    });
    els.loadMetric.addEventListener('click', (event) => {
        const button = event.target.closest('button[data-value]');
        if (!button) return;
        loadMetric = button.dataset.value;
        report.setPressed(els.loadMetric, loadMetric);
        if (lastData) renderLoad(lastData);
    });
    els.whoGroup.addEventListener('click', (event) => {
        const button = event.target.closest('button[data-value]');
        if (!button) return;
        whoGroup = button.dataset.value;
        report.setPressed(els.whoGroup, whoGroup);
        if (lastData) renderWho(lastData);
    });

    window.addEventListener('beforeunload', () => {
        stopped = true;
        requestGeneration += 1;
        clearInterval(timer);
        unsubscribeLocale?.();
    });

    Promise.all([bootstrapRoleUI(), i18n.ready]).then(() => {
        if (stopped) return;
        unsubscribeLocale = i18n.subscribe(() => {
            pageStatus.rerender();
            render();
        });
        void load();
        schedule();
    }).catch(() => undefined);
});
