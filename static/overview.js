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
        kpiRequestsDelta: document.getElementById('kpiRequestsDelta'),
        kpiTokens: document.getElementById('kpiTokens'),
        kpiTokensDelta: document.getElementById('kpiTokensDelta'),
        kpiCost: document.getElementById('kpiCost'),
        kpiCostDelta: document.getElementById('kpiCostDelta'),
        kpiCostSaved: document.getElementById('kpiCostSaved'),
        kpiCostSavedDelta: document.getElementById('kpiCostSavedDelta'),
        kpiThroughput: document.getElementById('kpiThroughput'),
        kpiThroughputDelta: document.getElementById('kpiThroughputDelta'),
        kpiTtft: document.getElementById('kpiTtft'),
        kpiLatency: document.getElementById('kpiLatency'),
        kpiLatencyDelta: document.getElementById('kpiLatencyDelta'),
        kpiActive: document.getElementById('kpiActive'),
        kpiErrors: document.getElementById('kpiErrors'),
        kpiErrorsDelta: document.getElementById('kpiErrorsDelta'),
        kpiRejections: document.getElementById('kpiRejections'),
        lifetime: document.getElementById('overviewLifetime'),
        trendMetric: document.getElementById('trendMetric'),
        trendMeta: document.getElementById('trendMeta'),
        trendChart: document.getElementById('trendChart'),
        breakdownTabs: document.getElementById('breakdownTabs'),
        breakdownsHead: document.getElementById('breakdownsHead'),
        topModelsBody: document.getElementById('topModelsBody'),
        messageArea: document.getElementById('messageArea'),
        messageDetail: document.getElementById('messageDetail'),
    };
    const drillCards = Array.from(document.querySelectorAll('[data-drill-card]'));
    const tabButtons = Array.from(els.breakdownTabs.querySelectorAll('[role="tab"]'));

    const pageStatus = window.gatewayUi.createStatus(els.messageArea, {
        rawDetailElement: els.messageDetail,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });

    let currentRole = null;
    let activeTab = 'resolved_targets';
    let lastPayload = null;
    let lastHealth = null;
    let lastHealthFailed = false;
    let requestGeneration = 0;
    let unsubscribeLocale = null;
    let stopped = false;

    function isMaster() {
        return currentRole === 'master';
    }

    function applyMasterRoleUI() {
        const master = isMaster();
        drillCards.forEach((card) => {
            card.classList.toggle('ov-kpi-linkable', master);
            const link = card.querySelector('.ov-kpi-drill');
            if (link) link.hidden = !master;
        });
        const keysTab = els.breakdownTabs.querySelector('[data-tab="api_keys"]');
        if (keysTab) keysTab.hidden = !master;
        if (!master && activeTab === 'api_keys') {
            setActiveTab('resolved_targets');
        }
    }

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
        if (value == null) return i18n.t('overview:values.notAvailable');
        const ms = numberValue(value);
        if (!ms) return i18n.t('overview:values.notAvailable');
        if (ms < 1000) return i18n.t('overview:format.milliseconds', { value: formatNumber(ms) });
        return i18n.t('overview:format.seconds', {
            value: i18n.formatNumber(ms / 1000, { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
        });
    }

    function formatThroughput(value) {
        if (value == null) return i18n.t('overview:values.notAvailable');
        const num = Number(value);
        if (!Number.isFinite(num) || num <= 0) return i18n.t('overview:values.notAvailable');
        return i18n.t('overview:format.tokensPerSecond', {
            value: i18n.formatNumber(num, { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
        });
    }

    function formatPercent(value) {
        if (value == null) return i18n.t('overview:values.notAvailable');
        const num = Number(value);
        if (!Number.isFinite(num)) return i18n.t('overview:values.notAvailable');
        return i18n.t('overview:format.percent', {
            value: i18n.formatNumber(num, { maximumFractionDigits: 1 }),
        });
    }

    function formatTtftPair(p50, p95) {
        const hasP50 = p50 != null && numberValue(p50) > 0;
        const hasP95 = p95 != null && numberValue(p95) > 0;
        if (!hasP50 && !hasP95) return i18n.t('overview:values.notAvailable');
        return i18n.t('overview:format.pair', {
            first: formatDuration(p50),
            second: formatDuration(p95),
        });
    }

    function formatDateShort(isoString) {
        if (!isoString) return '';
        const parsed = new Date(isoString);
        if (Number.isNaN(parsed.getTime())) return String(isoString);
        return i18n.formatDate(parsed, {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
        });
    }

    function renderDelta(el, current, previous, { upIsBad = null } = {}) {
        if (!el) return;
        if (previous == null || current == null) {
            el.hidden = true;
            el.textContent = '';
            return;
        }
        const cur = Number(current);
        const prev = Number(previous);
        if (!Number.isFinite(cur) || !Number.isFinite(prev)) {
            el.hidden = true;
            el.textContent = '';
            return;
        }

        let text = '';
        let tone = 'neutral';
        if (prev === 0) {
            if (cur === 0) {
                text = i18n.t('overview:delta.flat');
            } else if (cur > 0) {
                text = i18n.t('overview:delta.new');
                tone = upIsBad === true ? 'bad' : upIsBad === false ? 'ok' : 'neutral';
            } else {
                text = i18n.t('overview:delta.flat');
            }
        } else {
            const pct = ((cur - prev) / Math.abs(prev)) * 100;
            const rounded = Math.round(pct * 10) / 10;
            if (rounded === 0) {
                text = i18n.t('overview:delta.flat');
            } else {
                const value = i18n.formatNumber(Math.abs(rounded), { maximumFractionDigits: 1 });
                text = i18n.t(rounded > 0 ? 'overview:delta.up' : 'overview:delta.down', { value });
                if (upIsBad === true) {
                    tone = rounded > 0 ? 'bad' : 'ok';
                } else if (upIsBad === false) {
                    tone = rounded > 0 ? 'ok' : 'bad';
                }
            }
        }

        el.dataset.tone = tone;
        el.textContent = text;
        el.hidden = false;
    }

    function svgEl(tagName, attrs = {}) {
        const el = document.createElementNS(SVG_NS, tagName);
        Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, String(value)));
        return el;
    }

    function svgText(text, attrs = {}) {
        const el = svgEl('text', attrs);
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
        params.set('compare', 'previous');
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

    function renderKpis(payload) {
        const totals = payload.totals || {};
        const prevTotals = payload.previous && payload.previous.totals ? payload.previous.totals : null;
        const prevFallbackSummary =
            payload.previous && payload.previous.fallback && payload.previous.fallback.summary
                ? payload.previous.fallback.summary
                : null;

        els.kpiRequests.textContent = formatNumber(totals.requests);
        renderDelta(els.kpiRequestsDelta, totals.requests, prevTotals ? prevTotals.requests : null, {
            upIsBad: null,
        });

        els.kpiTokens.textContent = formatNumber(totals.total_tokens);
        renderDelta(els.kpiTokensDelta, totals.total_tokens, prevTotals ? prevTotals.total_tokens : null, {
            upIsBad: null,
        });

        els.kpiCost.textContent = formatMoney(totals.cost);
        renderDelta(els.kpiCostDelta, totals.cost, prevTotals ? prevTotals.cost : null, {
            upIsBad: true,
        });

        els.kpiCostSaved.textContent = formatMoney(totals.cost_saved);
        renderDelta(els.kpiCostSavedDelta, totals.cost_saved, prevTotals ? prevTotals.cost_saved : null, {
            upIsBad: false,
        });

        els.kpiThroughput.textContent = formatThroughput(totals.tokens_per_second);
        const throughputCurrent =
            totals.tokens_per_second != null && numberValue(totals.tokens_per_second) > 0
                ? totals.tokens_per_second
                : null;
        renderDelta(
            els.kpiThroughputDelta,
            throughputCurrent,
            prevTotals ? prevTotals.tokens_per_second : null,
            { upIsBad: false }
        );

        els.kpiTtft.textContent = formatTtftPair(totals.ttft_p50_ms, totals.ttft_p95_ms);

        els.kpiLatency.textContent = formatDuration(totals.duration_p95_ms);
        const latencyCurrent =
            totals.duration_p95_ms != null && numberValue(totals.duration_p95_ms) > 0
                ? totals.duration_p95_ms
                : null;
        renderDelta(
            els.kpiLatencyDelta,
            latencyCurrent,
            prevTotals ? prevTotals.duration_p95_ms : null,
            { upIsBad: true }
        );

        els.kpiActive.textContent = formatNumber(totals.active_requests);

        els.kpiErrors.textContent = formatNumber(totals.fallback_errors);
        renderDelta(
            els.kpiErrorsDelta,
            totals.fallback_errors,
            prevFallbackSummary ? prevFallbackSummary.errors : null,
            { upIsBad: true }
        );

        els.kpiRejections.textContent = formatNumber(totals.rejections);
    }

    function renderLifetime(lifetime) {
        if (!lifetime || typeof lifetime !== 'object') {
            els.lifetime.hidden = true;
            els.lifetime.replaceChildren();
            return;
        }
        const items = [];
        const label = document.createElement('span');
        label.className = 'ov-lifetime-label';
        label.textContent = i18n.t('overview:lifetime.label');
        items.push(label);

        const metrics = [
            i18n.t('overview:lifetime.requests', { value: formatNumber(lifetime.requests) }),
            i18n.t('overview:lifetime.tokens', { value: formatNumber(lifetime.total_tokens) }),
            i18n.t('overview:lifetime.cost', { value: formatMoney(lifetime.cost) }),
            i18n.t('overview:lifetime.costSaved', { value: formatMoney(lifetime.cost_saved) }),
        ];
        metrics.forEach((text) => {
            const span = document.createElement('span');
            span.className = 'ov-lifetime-item';
            span.textContent = text;
            items.push(span);
        });

        if (lifetime.first_event_at) {
            const since = document.createElement('span');
            since.className = 'ov-lifetime-since';
            since.textContent = i18n.t('overview:lifetime.since', {
                date: formatDateShort(lifetime.first_event_at),
            });
            items.push(since);
        }

        els.lifetime.replaceChildren(...items);
        els.lifetime.hidden = false;
    }

    function formatTrendMetric(metric, value) {
        if (metric === 'cost') return formatMoney(value);
        return formatNumber(value);
    }

    function pickXLabelIndices(count) {
        if (count <= 0) return [];
        if (count <= 6) {
            return Array.from({ length: count }, (_, idx) => idx);
        }
        const indices = new Set([0, count - 1]);
        const slots = 5;
        for (let i = 1; i < slots; i += 1) {
            indices.add(Math.round((i * (count - 1)) / slots));
        }
        return Array.from(indices).sort((a, b) => a - b);
    }

    function renderTrend(payload) {
        const rawMetric = els.trendMetric.value;
        const metric = rawMetric === 'cost' || rawMetric === 'total_tokens' ? rawMetric : 'requests';
        const points = ((payload.series && payload.series.usage) || []).slice();
        els.trendMeta.textContent = i18n.t('overview:trend.meta', { count: points.length });
        if (points.length === 0) {
            renderEmptyChart(els.trendChart, i18n.t('overview:trend.empty'));
            return;
        }

        const width = 720;
        const height = 240;
        const pad = { top: 16, right: 18, bottom: 34, left: 64 };
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
        if (points.length > 1) {
            svg.appendChild(svgEl('polygon', { points: areaPoints, class: 'ov-area' }));
            svg.appendChild(svgEl('polyline', { points: linePoints, class: 'ov-line' }));
        }

        const crosshair = svgEl('line', {
            x1: pad.left,
            y1: pad.top,
            x2: pad.left,
            y2: pad.top + chartH,
            class: 'ov-crosshair',
            visibility: 'hidden',
        });
        svg.appendChild(crosshair);

        const activePoint = svgEl('circle', {
            cx: pad.left,
            cy: pad.top + chartH,
            r: 5.5,
            class: 'ov-point-active',
            visibility: 'hidden',
        });
        svg.appendChild(activePoint);

        const tooltip = document.createElement('div');
        tooltip.className = 'ov-chart-tooltip';
        tooltip.hidden = true;
        const tooltipTime = document.createElement('div');
        tooltipTime.className = 'ov-chart-tooltip-time';
        const tooltipVal = document.createElement('div');
        tooltipVal.className = 'ov-chart-tooltip-val';
        tooltip.append(tooltipTime, tooltipVal);

        const showPointTooltip = (point, idx) => {
            const cx = xFor(idx);
            const cy = yFor(point[metric]);
            crosshair.setAttribute('x1', String(cx));
            crosshair.setAttribute('x2', String(cx));
            crosshair.setAttribute('visibility', 'visible');
            activePoint.setAttribute('cx', String(cx));
            activePoint.setAttribute('cy', String(cy));
            activePoint.setAttribute('visibility', 'visible');

            tooltipTime.textContent = String(point.time_period || '');
            tooltipVal.textContent = formatTrendMetric(metric, point[metric]);
            tooltip.hidden = false;

            const chartRect = els.trendChart.getBoundingClientRect();
            const svgRect = svg.getBoundingClientRect();
            const pointX = (svgRect.left - chartRect.left) + (cx / width) * svgRect.width;
            const pointY = (svgRect.top - chartRect.top) + (cy / height) * svgRect.height;
            const halfW = tooltip.offsetWidth / 2;
            const minLeft = halfW + 8;
            const maxLeft = Math.max(minLeft, els.trendChart.clientWidth - halfW - 8);
            const clampedLeft = Math.min(maxLeft, Math.max(minLeft, pointX));
            const flipBelow = pointY - tooltip.offsetHeight - 12 < 0;
            tooltip.classList.toggle('is-below', flipBelow);
            tooltip.style.left = `${clampedLeft}px`;
            tooltip.style.top = `${pointY}px`;
        };

        const hidePointTooltip = () => {
            crosshair.setAttribute('visibility', 'hidden');
            activePoint.setAttribute('visibility', 'hidden');
            tooltip.hidden = true;
        };

        points.forEach((point, idx) => {
            const cx = xFor(idx);
            const cy = yFor(point[metric]);
            svg.appendChild(svgEl('circle', { cx, cy, r: 3.5, class: 'ov-point' }));

            const labelText = i18n.t('overview:trend.pointLabel', {
                period: String(point.time_period || ''),
                value: formatTrendMetric(metric, point[metric]),
            });
            const hit = svgEl('circle', {
                cx,
                cy,
                r: 14,
                class: 'ov-hit',
                tabindex: '0',
                role: 'button',
                'aria-label': labelText,
            });
            const title = svgEl('title');
            title.textContent = labelText;
            hit.appendChild(title);
            hit.addEventListener('mouseenter', () => showPointTooltip(point, idx));
            hit.addEventListener('focus', () => showPointTooltip(point, idx));
            hit.addEventListener('mouseleave', hidePointTooltip);
            hit.addEventListener('blur', hidePointTooltip);
            svg.appendChild(hit);
        });

        const labelIndices = pickXLabelIndices(points.length);
        labelIndices.forEach((idx) => {
            const anchor =
                points.length === 1 ? 'middle' : idx === 0 ? 'start' : idx === points.length - 1 ? 'end' : 'middle';
            svg.appendChild(
                svgText(String(points[idx].time_period || ''), {
                    x: xFor(idx),
                    y: height - 9,
                    class: 'ov-chart-label',
                    'text-anchor': anchor,
                })
            );
        });

        svg.appendChild(
            svgText(formatTrendMetric(metric, maxY), {
                x: 6,
                y: yFor(maxY) + 4,
                class: 'ov-chart-label',
            })
        );

        els.trendChart.replaceChildren(svg, tooltip);
    }

    const BREAKDOWN_CONFIG = {
        resolved_targets: {
            columns: [
                { key: 'overview:breakdowns.columns.model', numeric: false, render: (item) => item.label },
                { key: 'overview:breakdowns.columns.requests', numeric: true, render: (item) => formatNumber(item.requests) },
                { key: 'overview:breakdowns.columns.tokens', numeric: true, render: (item) => formatNumber(item.total_tokens) },
                { key: 'overview:breakdowns.columns.cost', numeric: true, render: (item) => formatMoney(item.cost) },
                { key: 'overview:breakdowns.columns.costSaved', numeric: true, render: (item) => formatMoney(item.cost_saved) },
                { key: 'overview:breakdowns.columns.throughput', numeric: true, render: (item) => formatThroughput(item.tokens_per_second) },
                { key: 'overview:breakdowns.columns.successRate', numeric: true, render: (item) => formatPercent(item.fallback_success_rate) },
            ],
        },
        providers: {
            columns: [
                { key: 'overview:breakdowns.columns.provider', numeric: false, render: (item) => item.label },
                { key: 'overview:breakdowns.columns.requests', numeric: true, render: (item) => formatNumber(item.requests) },
                { key: 'overview:breakdowns.columns.tokens', numeric: true, render: (item) => formatNumber(item.total_tokens) },
                { key: 'overview:breakdowns.columns.cost', numeric: true, render: (item) => formatMoney(item.cost) },
                { key: 'overview:breakdowns.columns.costSaved', numeric: true, render: (item) => formatMoney(item.cost_saved) },
                { key: 'overview:breakdowns.columns.throughput', numeric: true, render: (item) => formatThroughput(item.tokens_per_second) },
                { key: 'overview:breakdowns.columns.ttft', numeric: true, render: (item) => formatDuration(item.ttft_avg_ms) },
                { key: 'overview:breakdowns.columns.p95', numeric: true, render: (item) => formatDuration(item.duration_p95_ms) },
                { key: 'overview:breakdowns.columns.successRate', numeric: true, render: (item) => formatPercent(item.fallback_success_rate) },
            ],
        },
        operations: {
            columns: [
                { key: 'overview:breakdowns.columns.operation', numeric: false, render: (item) => item.label },
                { key: 'overview:breakdowns.columns.requests', numeric: true, render: (item) => formatNumber(item.requests) },
                { key: 'overview:breakdowns.columns.tokens', numeric: true, render: (item) => formatNumber(item.total_tokens) },
                { key: 'overview:breakdowns.columns.cost', numeric: true, render: (item) => formatMoney(item.cost) },
                { key: 'overview:breakdowns.columns.costSaved', numeric: true, render: (item) => formatMoney(item.cost_saved) },
                { key: 'overview:breakdowns.columns.throughput', numeric: true, render: (item) => formatThroughput(item.tokens_per_second) },
                { key: 'overview:breakdowns.columns.avgDuration', numeric: true, render: (item) => formatDuration(item.avg_duration_ms) },
            ],
        },
        api_keys: {
            columns: [
                { key: 'overview:breakdowns.columns.key', numeric: false, render: (item) => item.api_key_name || item.label },
                { key: 'overview:breakdowns.columns.requests', numeric: true, render: (item) => formatNumber(item.requests) },
                { key: 'overview:breakdowns.columns.tokens', numeric: true, render: (item) => formatNumber(item.total_tokens) },
                { key: 'overview:breakdowns.columns.cost', numeric: true, render: (item) => formatMoney(item.cost) },
                { key: 'overview:breakdowns.columns.costSaved', numeric: true, render: (item) => formatMoney(item.cost_saved) },
                { key: 'overview:breakdowns.columns.throughput', numeric: true, render: (item) => formatThroughput(item.tokens_per_second) },
            ],
        },
        x_titles: {
            columns: [
                { key: 'overview:breakdowns.columns.client', numeric: false, render: (item) => item.label },
                { key: 'overview:breakdowns.columns.requests', numeric: true, render: (item) => formatNumber(item.requests) },
                { key: 'overview:breakdowns.columns.tokens', numeric: true, render: (item) => formatNumber(item.total_tokens) },
                { key: 'overview:breakdowns.columns.cost', numeric: true, render: (item) => formatMoney(item.cost) },
                { key: 'overview:breakdowns.columns.costSaved', numeric: true, render: (item) => formatMoney(item.cost_saved) },
                { key: 'overview:breakdowns.columns.throughput', numeric: true, render: (item) => formatThroughput(item.tokens_per_second) },
            ],
        },
    };

    function setActiveTab(tabName, { focus = false } = {}) {
        if (!BREAKDOWN_CONFIG[tabName]) return;
        if (tabName === 'api_keys' && !isMaster()) return;
        activeTab = tabName;
        let activeBtn = null;
        tabButtons.forEach((btn) => {
            const selected = btn.dataset.tab === activeTab;
            btn.setAttribute('aria-selected', selected ? 'true' : 'false');
            btn.tabIndex = selected ? 0 : -1;
            if (selected) activeBtn = btn;
        });
        if (focus && activeBtn) activeBtn.focus();
        if (lastPayload) renderBreakdowns(lastPayload);
    }

    function renderBreakdowns(payload) {
        const effectiveTab = activeTab === 'api_keys' && !isMaster() ? 'resolved_targets' : activeTab;
        const config = BREAKDOWN_CONFIG[effectiveTab] || BREAKDOWN_CONFIG.resolved_targets;
        const columns = config.columns;

        const headRow = document.createElement('tr');
        columns.forEach((col) => {
            const th = document.createElement('th');
            th.textContent = i18n.t(col.key);
            if (col.numeric) th.className = 'ov-num';
            headRow.appendChild(th);
        });
        els.breakdownsHead.replaceChildren(headRow);

        const rows = ((payload.breakdowns && payload.breakdowns[effectiveTab]) || []).slice(0, 10);
        if (rows.length === 0) {
            const row = document.createElement('tr');
            const cell = document.createElement('td');
            cell.colSpan = columns.length;
            cell.className = 'ov-table-empty';
            cell.textContent = i18n.t('overview:breakdowns.empty');
            row.appendChild(cell);
            els.topModelsBody.replaceChildren(row);
            return;
        }

        const nodes = rows.map((item) => {
            const row = document.createElement('tr');
            columns.forEach((col, colIdx) => {
                const cell = document.createElement('td');
                if (colIdx === 0) {
                    cell.textContent = String(col.render(item) || i18n.t('overview:values.unknownLabel'));
                    cell.lang = 'und';
                    cell.dir = 'auto';
                } else {
                    cell.textContent = col.render(item);
                    if (col.numeric) cell.className = 'ov-num';
                }
                row.appendChild(cell);
            });
            return row;
        });
        els.topModelsBody.replaceChildren(...nodes);
    }

    function renderDashboard(payload) {
        if (!currentRole && payload.scope && payload.scope.role) {
            currentRole = payload.scope.role;
            applyMasterRoleUI();
        }
        const totals = payload.totals || {};
        const isEmpty = numberValue(totals.requests) === 0;
        els.empty.hidden = !isEmpty;
        els.content.hidden = isEmpty;
        if (isEmpty) return;
        renderKpis(payload);
        renderLifetime(payload.lifetime);
        renderTrend(payload);
        renderBreakdowns(payload);
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
    tabButtons.forEach((btn) => {
        btn.addEventListener('click', () => {
            setActiveTab(btn.dataset.tab);
        });
    });
    els.breakdownTabs.addEventListener('keydown', (event) => {
        const visibleTabs = tabButtons.filter((btn) => !btn.hidden);
        if (visibleTabs.length === 0) return;
        const currentIndex = visibleTabs.indexOf(document.activeElement);
        if (currentIndex === -1) return;
        let nextIndex = null;
        if (event.key === 'ArrowRight') {
            nextIndex = (currentIndex + 1) % visibleTabs.length;
        } else if (event.key === 'ArrowLeft') {
            nextIndex = (currentIndex - 1 + visibleTabs.length) % visibleTabs.length;
        } else if (event.key === 'Home') {
            nextIndex = 0;
        } else if (event.key === 'End') {
            nextIndex = visibleTabs.length - 1;
        }
        if (nextIndex !== null) {
            event.preventDefault();
            setActiveTab(visibleTabs[nextIndex].dataset.tab, { focus: true });
        }
    });

    window.addEventListener('beforeunload', () => {
        stopped = true;
        requestGeneration += 1;
        unsubscribeLocale?.();
    });

    Promise.all([bootstrapRoleUI(), i18n.ready]).then(([identity]) => {
        if (stopped) return;
        currentRole = identity && identity.role ? identity.role : null;
        applyMasterRoleUI();
        unsubscribeLocale = i18n.subscribe(() => {
            pageStatus.rerender();
            if (lastPayload) renderDashboard(lastPayload);
            if (lastHealth !== null || lastHealthFailed) renderHealth(lastHealth, lastHealthFailed);
        });
        void loadDashboard();
        void loadHealth();
    }).catch(() => undefined);
});
