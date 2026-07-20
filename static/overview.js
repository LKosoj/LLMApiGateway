document.addEventListener('DOMContentLoaded', () => {
    'use strict';

    const SVG_NS = 'http://www.w3.org/2000/svg';
    const DASHBOARD_ENDPOINT = '/v1/api/analytics-dashboard';
    const { apiFetch, bootstrapRoleUI } = window.gatewayAuth;
    const i18n = window.gatewayI18n;

    Theme.attachToggle('darkModeToggle');

    const els = {
        range: document.getElementById('rangeSelect'),
        refresh: document.getElementById('refreshBtn'),
        healthBanner: document.getElementById('healthBanner'),
        healthStatus: document.getElementById('healthStatus'),
        healthVersion: document.getElementById('healthVersion'),
        empty: document.getElementById('overviewEmpty'),
        content: document.getElementById('overviewContent'),
        kpiRequests: document.getElementById('kpiRequests'),
        kpiCost: document.getElementById('kpiCost'),
        kpiLatency: document.getElementById('kpiLatency'),
        kpiActive: document.getElementById('kpiActive'),
        kpiErrors: document.getElementById('kpiErrors'),
        kpiRejections: document.getElementById('kpiRejections'),
        trendMetric: document.getElementById('trendMetric'),
        trendMeta: document.getElementById('trendMeta'),
        trendChart: document.getElementById('trendChart'),
        topModelsBody: document.getElementById('topModelsBody'),
        messageArea: document.getElementById('messageArea'),
        messageDetail: document.getElementById('messageDetail'),
    };
    const pageStatus = window.gatewayUi.createStatus(els.messageArea, {
        rawDetailElement: els.messageDetail,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });

    let lastPayload = null;
    let lastHealth = null;
    let lastHealthFailed = false;
    let requestGeneration = 0;
    let unsubscribeLocale = null;
    let stopped = false;

    function numberValue(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function formatNumber(value) {
        return i18n.formatNumber(numberValue(value));
    }

    function formatMoney(value) {
        return i18n.formatCurrency(numberValue(value), 'USD', {
            minimumFractionDigits: 4,
            maximumFractionDigits: 4,
        });
    }

    function formatDuration(value) {
        const ms = numberValue(value);
        if (!ms) return i18n.t('overview:values.notAvailable');
        if (ms < 1000) return i18n.t('overview:format.milliseconds', { value: formatNumber(ms) });
        return i18n.t('overview:format.seconds', {
            value: i18n.formatNumber(ms / 1000, { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
        });
    }

    function svgEl(tagName, attrs = {}) {
        const el = document.createElementNS(SVG_NS, tagName);
        Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, String(value)));
        return el;
    }

    function textNode(tagName, text, className) {
        const el = document.createElement(tagName);
        if (className) el.className = className;
        el.textContent = text;
        return el;
    }

    function renderEmptyChart(container, message) {
        const empty = document.createElement('div');
        empty.className = 'ov-chart-empty';
        empty.textContent = message;
        container.replaceChildren(empty);
    }

    function buildDashboardUrl() {
        const params = new URLSearchParams();
        params.set('range', els.range.value);
        return `${DASHBOARD_ENDPOINT}?${params.toString()}`;
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

    function renderHealth(health, failed) {
        if (failed || !health) {
            els.healthBanner.dataset.healthState = 'error';
            els.healthStatus.textContent = i18n.t('overview:health.unavailable');
            els.healthVersion.textContent = '';
            return;
        }
        const status = health.status === 'ok' ? 'ok' : 'error';
        els.healthBanner.dataset.healthState = status;
        const phaseKey = ['starting', 'ready', 'stopping', 'failed'].includes(health.phase)
            ? health.phase
            : 'unknown';
        els.healthStatus.textContent = i18n.t(`overview:health.status.${status}`, {
            phase: i18n.t(`overview:health.phase.${phaseKey}`),
        });
        els.healthVersion.textContent = health.version
            ? i18n.t('overview:health.version', { version: health.version })
            : '';
    }

    function renderKpis(totals) {
        els.kpiRequests.textContent = formatNumber(totals.requests);
        els.kpiCost.textContent = formatMoney(totals.cost);
        els.kpiLatency.textContent = formatDuration(totals.duration_p95_ms);
        els.kpiActive.textContent = formatNumber(totals.active_requests);
        els.kpiErrors.textContent = formatNumber(totals.fallback_errors);
        els.kpiRejections.textContent = formatNumber(totals.rejections);
    }

    function renderTrend(payload) {
        const metric = els.trendMetric.value === 'cost' ? 'cost' : 'requests';
        const points = ((payload.series && payload.series.usage) || []).slice();
        els.trendMeta.textContent = i18n.t('overview:trend.meta', { count: points.length });
        if (points.length === 0) {
            renderEmptyChart(els.trendChart, i18n.t('overview:trend.empty'));
            return;
        }

        const width = 720;
        const height = 240;
        const pad = { top: 16, right: 18, bottom: 34, left: 60 };
        const chartW = width - pad.left - pad.right;
        const chartH = height - pad.top - pad.bottom;
        const maxY = Math.max(...points.map((point) => numberValue(point[metric])), metric === 'cost' ? 0.0001 : 1);
        const xFor = (idx) => pad.left + (points.length === 1 ? chartW / 2 : (idx / (points.length - 1)) * chartW);
        const yFor = (value) => pad.top + chartH - (numberValue(value) / maxY) * chartH;
        const linePoints = points.map((point, idx) => `${xFor(idx)},${yFor(point[metric])}`).join(' ');
        const areaPoints = `${pad.left},${pad.top + chartH} ${linePoints} ${pad.left + chartW},${pad.top + chartH}`;

        const svg = svgEl('svg', {
            viewBox: `0 0 ${width} ${height}`,
            role: 'img',
            'aria-label': i18n.t('overview:trend.aria'),
        });
        for (let i = 0; i <= 4; i += 1) {
            const y = pad.top + (chartH / 4) * i;
            svg.appendChild(svgEl('line', { x1: pad.left, y1: y, x2: pad.left + chartW, y2: y, class: 'ov-grid-line' }));
        }
        svg.appendChild(svgEl('line', { x1: pad.left, y1: pad.top + chartH, x2: pad.left + chartW, y2: pad.top + chartH, class: 'ov-axis' }));
        svg.appendChild(svgEl('line', { x1: pad.left, y1: pad.top, x2: pad.left, y2: pad.top + chartH, class: 'ov-axis' }));
        svg.appendChild(svgEl('polygon', { points: areaPoints, class: 'ov-area' }));
        svg.appendChild(svgEl('polyline', { points: linePoints, class: 'ov-line' }));
        points.forEach((point, idx) => {
            svg.appendChild(svgEl('circle', { cx: xFor(idx), cy: yFor(point[metric]), r: 3.5, class: 'ov-point' }));
        });

        const firstLabel = textNode('text', points[0].time_period, 'ov-chart-label');
        firstLabel.setAttribute('x', String(pad.left));
        firstLabel.setAttribute('y', String(height - 9));
        svg.appendChild(firstLabel);
        const lastLabel = textNode('text', points[points.length - 1].time_period, 'ov-chart-label');
        lastLabel.setAttribute('x', String(pad.left + chartW));
        lastLabel.setAttribute('y', String(height - 9));
        lastLabel.setAttribute('text-anchor', 'end');
        svg.appendChild(lastLabel);
        const maxLabel = textNode('text', metric === 'cost' ? formatMoney(maxY) : formatNumber(maxY), 'ov-chart-label');
        maxLabel.setAttribute('x', '6');
        maxLabel.setAttribute('y', String(yFor(maxY) + 4));
        svg.appendChild(maxLabel);

        els.trendChart.replaceChildren(svg);
    }

    function renderTopModels(payload) {
        const rows = ((payload.breakdowns && payload.breakdowns.resolved_targets) || []).slice(0, 5);
        if (rows.length === 0) {
            const row = document.createElement('tr');
            const cell = document.createElement('td');
            cell.colSpan = 3;
            cell.className = 'ov-table-empty';
            cell.textContent = i18n.t('overview:topModels.empty');
            row.appendChild(cell);
            els.topModelsBody.replaceChildren(row);
            return;
        }
        const nodes = rows.map((item) => {
            const row = document.createElement('tr');
            const label = document.createElement('td');
            label.textContent = String(item.label || i18n.t('overview:values.unknownLabel'));
            label.lang = 'und';
            label.dir = 'auto';
            row.appendChild(label);
            const requestsCell = document.createElement('td');
            requestsCell.textContent = formatNumber(item.requests);
            row.appendChild(requestsCell);
            const costCell = document.createElement('td');
            costCell.textContent = formatMoney(item.cost);
            row.appendChild(costCell);
            return row;
        });
        els.topModelsBody.replaceChildren(...nodes);
    }

    function renderDashboard(payload) {
        const totals = payload.totals || {};
        const isEmpty = numberValue(totals.requests) === 0;
        els.empty.hidden = !isEmpty;
        els.content.hidden = isEmpty;
        if (isEmpty) return;
        renderKpis(totals);
        renderTrend(payload);
        renderTopModels(payload);
    }

    async function loadHealth() {
        try {
            const response = await apiFetch('/health');
            const body = await response.json().catch(() => null);
            lastHealth = body;
            lastHealthFailed = false;
            renderHealth(lastHealth, lastHealthFailed);
        } catch (error) {
            if (error.message === 'Authentication required') return;
            lastHealth = null;
            lastHealthFailed = true;
            renderHealth(lastHealth, lastHealthFailed);
        }
    }

    async function loadDashboard() {
        const generation = ++requestGeneration;
        try {
            const response = await apiFetch(buildDashboardUrl());
            if (stopped || generation !== requestGeneration) return;

            if (!response.ok) {
                const body = await response.json().catch(() => ({}));
                if (stopped || generation !== requestGeneration) return;
                const descriptor = window.gatewayUi.describeApiError(body, { status: response.status });
                lastPayload = null;
                els.content.hidden = true;
                els.empty.hidden = true;
                showError({ key: descriptor.summaryKey, values: descriptor.summaryValues }, descriptor.rawDetail);
                return;
            }

            const data = await response.json();
            if (stopped || generation !== requestGeneration) return;
            lastPayload = data;
            clearError();
            renderDashboard(data);
        } catch (error) {
            if (stopped || generation !== requestGeneration) return;
            if (error.message === 'Authentication required') return;
            const descriptor = window.gatewayUi.describeApiError(null);
            lastPayload = null;
            els.content.hidden = true;
            els.empty.hidden = true;
            showError({ key: descriptor.summaryKey, values: descriptor.summaryValues }, error.message);
        }
    }

    els.range.addEventListener('change', () => void loadDashboard());
    els.refresh.addEventListener('click', () => {
        void loadDashboard();
        void loadHealth();
    });
    els.trendMetric.addEventListener('change', () => {
        if (lastPayload) renderTrend(lastPayload);
    });

    window.addEventListener('beforeunload', () => {
        stopped = true;
        requestGeneration += 1;
        unsubscribeLocale?.();
    });

    Promise.all([bootstrapRoleUI(), i18n.ready]).then(() => {
        if (stopped) return;
        unsubscribeLocale = i18n.subscribe(() => {
            pageStatus.rerender();
            if (lastPayload) renderDashboard(lastPayload);
            if (lastHealth !== null || lastHealthFailed) renderHealth(lastHealth, lastHealthFailed);
        });
        void loadDashboard();
        void loadHealth();
    }).catch(() => undefined);
});
