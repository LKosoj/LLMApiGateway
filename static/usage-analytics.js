(function initUsageAnalytics(global) {
    const SVG_NS = "http://www.w3.org/2000/svg";
    const DASHBOARD_ENDPOINT = "/v1/api/analytics-dashboard";

    const state = {
        initialized: false,
        loaded: false,
        loading: false,
        identity: null,
        data: null,
        els: {},
    };

    function textNode(tagName, text, className) {
        const el = document.createElement(tagName);
        if (className) el.className = className;
        el.textContent = text;
        return el;
    }

    function svgEl(tagName, attrs = {}) {
        const el = document.createElementNS(SVG_NS, tagName);
        Object.entries(attrs).forEach(([key, value]) => {
            el.setAttribute(key, String(value));
        });
        return el;
    }

    function numberValue(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function formatNumber(value) {
        return numberValue(value).toLocaleString();
    }

    function formatMoney(value) {
        return `$${numberValue(value).toFixed(4)}`;
    }

    function formatRate(value) {
        return `${numberValue(value).toFixed(2)}%`;
    }

    function formatDuration(value) {
        const ms = numberValue(value);
        if (!ms) return "N/A";
        if (ms < 1000) return `${formatNumber(ms)}ms`;
        return `${(ms / 1000).toFixed(1)}s`;
    }

    function formatTimestamp(value) {
        const timestamp = typeof value === "string" ? value.trim() : "";
        if (!timestamp) return "N/A";
        return timestamp.split(".")[0] || timestamp;
    }

    function setStatus(message, isError = false) {
        if (!state.els.status) return;
        state.els.status.textContent = message;
        state.els.status.classList.toggle("analytics-error", isError);
    }

    function setLoading(isLoading) {
        state.loading = isLoading;
        if (state.els.refresh) {
            state.els.refresh.disabled = isLoading;
            state.els.refresh.classList.toggle("loading", isLoading);
        }
        if (isLoading) {
            setStatus("Loading analytics...");
            state.els.dashboard.hidden = true;
        }
    }

    function getIdentityRole() {
        return state.identity && state.identity.role ? state.identity.role : "unknown";
    }

    function isMaster() {
        return getIdentityRole() === "master";
    }

    function renderEmpty(container, message) {
        container.replaceChildren(textNode("div", message, "analytics-empty"));
    }

    function replaceOptions(select, values, label, valueKey = null, labelKey = null) {
        if (!select) return;
        const current = select.value;
        select.replaceChildren();
        const allOption = document.createElement("option");
        allOption.value = "";
        allOption.textContent = label;
        select.appendChild(allOption);
        values.forEach(item => {
            const option = document.createElement("option");
            option.value = valueKey ? String(item[valueKey] ?? "") : String(item);
            option.textContent = labelKey ? String(item[labelKey] ?? item[valueKey] ?? "") : String(item);
            select.appendChild(option);
        });
        select.value = Array.from(select.options).some(option => option.value === current) ? current : "";
    }

    function updateFilterOptions(payload) {
        const options = payload.filter_options || {};
        replaceOptions(state.els.operation, options.operations || [], "All operations");
        replaceOptions(state.els.gateway, options.gateway_models || [], "All gateways");
        replaceOptions(state.els.provider, options.providers || [], "All providers");
        replaceOptions(state.els.model, options.models || [], "All models");
        replaceOptions(state.els.xTitle, options.x_titles || [], "All titles");
        replaceOptions(state.els.upstreamKey, options.upstream_keys || [], "All provider keys");
        replaceOptions(state.els.apiKeyId, options.api_keys || [], "Select key", "id", "name");
        updateKeyScopeState();
    }

    function updateKeyScopeState() {
        if (!state.els.keyScope || !state.els.apiKeyId) return;
        const specificKeySelected = state.els.keyScope.value === "api_key";
        state.els.apiKeyId.disabled = !specificKeySelected;
        if (!specificKeySelected) {
            state.els.apiKeyId.value = "";
        }
    }

    function buildDashboardUrl() {
        const url = new URL(DASHBOARD_ENDPOINT, window.location.origin);
        const params = {
            range: state.els.range.value,
            bucket: state.els.bucket.value,
            operation: state.els.operation.value,
            gateway_model: state.els.gateway.value,
            provider: state.els.provider.value,
            model: state.els.model.value,
            x_title: state.els.xTitle.value,
            estimated: state.els.estimated.value,
        };
        Object.entries(params).forEach(([key, value]) => {
            if (value) url.searchParams.set(key, value);
        });
        if (isMaster()) {
            if (state.els.keyScope.value === "unattributed") {
                url.searchParams.set("api_key_scope", "unattributed");
            } else if (state.els.keyScope.value === "api_key") {
                if (!state.els.apiKeyId.value) {
                    throw new Error("Select a virtual key or switch key scope to All keys.");
                }
                url.searchParams.set("api_key_id", state.els.apiKeyId.value);
            }
            if (state.els.upstreamKey.value) {
                url.searchParams.set("upstream_key_fingerprint", state.els.upstreamKey.value);
            }
        }
        return `${url.pathname}${url.search}`;
    }

    function createKpi(label, value) {
        const card = document.createElement("div");
        card.className = "analytics-kpi";
        card.appendChild(textNode("span", label));
        card.appendChild(textNode("strong", value));
        return card;
    }

    function renderKpis(payload) {
        const totals = payload.totals || {};
        const kpis = [
            ["Requests", formatNumber(totals.requests)],
            ["Total Tokens", formatNumber(totals.total_tokens)],
            ["Cost", formatMoney(totals.cost)],
            ["Cost Saved", formatMoney(totals.cost_saved)],
            ["Cost / 1M", formatMoney(totals.cost_per_million_tokens)],
            ["Avg Duration", formatDuration(totals.avg_duration_ms)],
            ["Active", formatNumber(totals.active_requests)],
            ["Fallback Errors", formatNumber(totals.fallback_errors)],
            ["Rejections", formatNumber(totals.rejections)],
            ["Estimated", formatNumber(totals.estimated_count)],
        ];
        state.els.kpis.replaceChildren(...kpis.map(([label, value]) => createKpi(label, value)));
    }

    function parsePeriodTime(value, bucket) {
        const period = String(value || "");
        if (!period) return 0;
        if (bucket === "month" && /^\d{4}-\d{2}$/.test(period)) {
            return Date.parse(`${period}-01T00:00:00Z`);
        }
        if (bucket === "week") {
            const match = period.match(/^(\d{4})-W(\d{2})$/);
            if (match) {
                return Date.UTC(Number(match[1]), 0, 1 + Number(match[2]) * 7);
            }
        }
        if (bucket === "day" && /^\d{4}-\d{2}-\d{2}$/.test(period)) {
            return Date.parse(`${period}T00:00:00Z`);
        }
        return Date.parse(period.replace(" ", "T") + (period.includes("T") ? "" : "Z")) || 0;
    }

    function renderLineChart(payload) {
        const points = ((payload.series && payload.series.usage) || [])
            .slice()
            .sort((a, b) => parsePeriodTime(a.time_period, payload.filters.bucket) - parsePeriodTime(b.time_period, payload.filters.bucket));

        state.els.trendMeta.textContent = `${points.length} buckets`;
        if (points.length === 0) {
            renderEmpty(state.els.lineChart, "No trend data for selected filters.");
            return;
        }

        const width = 720;
        const height = 260;
        const pad = {top: 16, right: 18, bottom: 34, left: 54};
        const chartW = width - pad.left - pad.right;
        const chartH = height - pad.top - pad.bottom;
        const maxY = Math.max(...points.map(point => numberValue(point.total_tokens)), 1);
        const xFor = idx => pad.left + (points.length === 1 ? chartW / 2 : idx / (points.length - 1) * chartW);
        const yFor = value => pad.top + chartH - numberValue(value) / maxY * chartH;
        const linePoints = points.map((point, idx) => `${xFor(idx)},${yFor(point.total_tokens)}`).join(" ");
        const areaPoints = `${pad.left},${pad.top + chartH} ${linePoints} ${pad.left + chartW},${pad.top + chartH}`;

        const svg = svgEl("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Token trend line chart"});
        for (let i = 0; i <= 4; i += 1) {
            const y = pad.top + chartH / 4 * i;
            svg.appendChild(svgEl("line", {x1: pad.left, y1: y, x2: pad.left + chartW, y2: y, class: "analytics-grid-line"}));
        }
        svg.appendChild(svgEl("line", {x1: pad.left, y1: pad.top + chartH, x2: pad.left + chartW, y2: pad.top + chartH, class: "analytics-axis"}));
        svg.appendChild(svgEl("line", {x1: pad.left, y1: pad.top, x2: pad.left, y2: pad.top + chartH, class: "analytics-axis"}));
        svg.appendChild(svgEl("polygon", {points: areaPoints, class: "analytics-area"}));
        svg.appendChild(svgEl("polyline", {points: linePoints, class: "analytics-line"}));
        points.forEach((point, idx) => {
            svg.appendChild(svgEl("circle", {cx: xFor(idx), cy: yFor(point.total_tokens), r: 3.5, class: "analytics-point"}));
        });

        const firstLabel = textNode("text", points[0].time_period, "analytics-chart-label");
        firstLabel.setAttribute("x", String(pad.left));
        firstLabel.setAttribute("y", String(height - 9));
        svg.appendChild(firstLabel);
        const lastLabel = textNode("text", points[points.length - 1].time_period, "analytics-chart-label");
        lastLabel.setAttribute("x", String(pad.left + chartW));
        lastLabel.setAttribute("y", String(height - 9));
        lastLabel.setAttribute("text-anchor", "end");
        svg.appendChild(lastLabel);
        const maxLabel = textNode("text", formatNumber(maxY), "analytics-chart-label");
        maxLabel.setAttribute("x", "6");
        maxLabel.setAttribute("y", String(yFor(maxY) + 4));
        svg.appendChild(maxLabel);

        state.els.lineChart.replaceChildren(svg);
    }

    function renderBarChart(payload) {
        const bars = ((payload.breakdowns && payload.breakdowns.providers) || [])
            .slice()
            .sort((a, b) => numberValue(b.cost) - numberValue(a.cost) || numberValue(b.requests) - numberValue(a.requests))
            .slice(0, 8);

        state.els.costMeta.textContent = `${bars.length} providers`;
        if (bars.length === 0) {
            renderEmpty(state.els.barChart, "No provider data for selected filters.");
            return;
        }

        const width = 720;
        const height = 260;
        const pad = {top: 14, right: 18, bottom: 52, left: 54};
        const chartW = width - pad.left - pad.right;
        const chartH = height - pad.top - pad.bottom;
        const maxValue = Math.max(...bars.map(bar => numberValue(bar.cost)), ...bars.map(bar => numberValue(bar.requests)), 1);
        const gap = 12;
        const barW = Math.max(18, (chartW - gap * (bars.length - 1)) / bars.length);
        const svg = svgEl("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Provider cost bar chart"});
        svg.appendChild(svgEl("line", {x1: pad.left, y1: pad.top + chartH, x2: pad.left + chartW, y2: pad.top + chartH, class: "analytics-axis"}));
        bars.forEach((bar, idx) => {
            const value = numberValue(bar.cost) || numberValue(bar.requests);
            const x = pad.left + idx * (barW + gap);
            const h = Math.max(2, value / maxValue * chartH);
            const y = pad.top + chartH - h;
            svg.appendChild(svgEl("rect", {x, y, width: barW, height: h, rx: 4, class: "analytics-bar"}));
            const valueLabel = textNode("text", numberValue(bar.cost) ? formatMoney(bar.cost) : formatNumber(bar.requests), "analytics-chart-label");
            valueLabel.setAttribute("x", String(x + barW / 2));
            valueLabel.setAttribute("y", String(Math.max(12, y - 6)));
            valueLabel.setAttribute("text-anchor", "middle");
            svg.appendChild(valueLabel);
            const nameLabel = textNode("text", String(bar.label || "unknown").slice(0, 18), "analytics-chart-label");
            nameLabel.setAttribute("x", String(x + barW / 2));
            nameLabel.setAttribute("y", String(height - 22));
            nameLabel.setAttribute("text-anchor", "middle");
            svg.appendChild(nameLabel);
        });
        state.els.barChart.replaceChildren(svg);
    }

    function createTable(columns, rows) {
        const table = document.createElement("table");
        table.className = "analytics-table";
        const thead = document.createElement("thead");
        const headRow = document.createElement("tr");
        columns.forEach(column => headRow.appendChild(textNode("th", column.label)));
        thead.appendChild(headRow);
        table.appendChild(thead);

        const tbody = document.createElement("tbody");
        rows.forEach(row => {
            const tr = document.createElement("tr");
            columns.forEach(column => {
                const td = document.createElement("td");
                td.textContent = column.format ? column.format(row[column.key], row) : String(row[column.key] ?? "N/A");
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        return table;
    }

    function renderBreakdownTable(payload) {
        const rows = ((payload.breakdowns && payload.breakdowns.resolved_targets) || []).slice(0, 25);
        if (rows.length === 0) {
            renderEmpty(state.els.breakdownTable, "No rows match the selected filters.");
            return;
        }
        state.els.breakdownTable.replaceChildren(createTable([
            {key: "label", label: "Resolved Target"},
            {key: "requests", label: "Requests", format: formatNumber},
            {key: "total_tokens", label: "Tokens", format: formatNumber},
            {key: "cost", label: "Cost", format: formatMoney},
            {key: "cost_saved", label: "Saved", format: formatMoney},
            {key: "estimated_count", label: "Estimated", format: formatNumber},
            {key: "avg_duration_ms", label: "Avg Duration", format: formatDuration},
        ], rows));
    }

    function renderXTitleTable(payload) {
        const rows = ((payload.breakdowns && payload.breakdowns.x_titles) || []).slice(0, 20);
        if (rows.length === 0) {
            renderEmpty(state.els.xTitleTable, "No X-Title rows for selected filters.");
            return;
        }
        state.els.xTitleTable.replaceChildren(createTable([
            {key: "label", label: "X-Title"},
            {key: "requests", label: "Requests", format: formatNumber},
            {key: "total_tokens", label: "Tokens", format: formatNumber},
            {key: "cost", label: "Cost", format: formatMoney},
            {key: "cost_saved", label: "Saved", format: formatMoney},
            {key: "estimated_count", label: "Estimated", format: formatNumber},
            {key: "avg_duration_ms", label: "Avg Duration", format: formatDuration},
        ], rows));
    }

    function renderReliabilityTable(payload) {
        const fallback = payload.reliability && payload.reliability.fallback ? payload.reliability.fallback : {};
        const fallbackSummary = fallback.summary || {};
        const rejections = payload.reliability && payload.reliability.rejections ? payload.reliability.rejections : {};
        const summaryRows = [
            {metric: "Fallback attempts", value: formatNumber(fallbackSummary.attempts)},
            {metric: "Fallback errors", value: formatNumber(fallbackSummary.errors)},
            {metric: "Fallback success", value: formatRate(fallbackSummary.success_rate)},
            {metric: "Rejections", value: formatNumber(rejections.summary && rejections.summary.rejections)},
        ];
        const errorRows = (fallback.error_types || []).slice(0, 4).map(row => ({
            metric: `Fallback: ${row.label}`,
            value: formatNumber(row.errors),
        }));
        const rejectionRows = (rejections.categories || []).slice(0, 4).map(row => ({
            metric: `Rejected: ${row.label}`,
            value: formatNumber(row.rejections),
        }));
        state.els.reliabilityTable.replaceChildren(createTable([
            {key: "metric", label: "Metric"},
            {key: "value", label: "Value"},
        ], summaryRows.concat(errorRows, rejectionRows)));
    }

    function renderKeyTable(payload) {
        if (!isMaster()) {
            state.els.keyTable.replaceChildren();
            return;
        }
        const rows = ((payload.breakdowns && payload.breakdowns.api_keys) || []).slice(0, 20);
        if (rows.length === 0) {
            renderEmpty(state.els.keyTable, "No virtual key rows for selected filters.");
            return;
        }
        state.els.keyTable.replaceChildren(createTable([
            {key: "api_key_name", label: "Virtual Key"},
            {key: "requests", label: "Requests", format: formatNumber},
            {key: "total_tokens", label: "Tokens", format: formatNumber},
            {key: "cost", label: "Cost", format: formatMoney},
            {key: "cost_saved", label: "Saved", format: formatMoney},
            {key: "avg_duration_ms", label: "Avg Duration", format: formatDuration},
        ], rows));
    }

    function renderRecentTable(payload) {
        const rows = (payload.recent_records || []).slice(0, 15);
        if (rows.length === 0) {
            renderEmpty(state.els.recentTable, "No recent activity for selected filters.");
            return;
        }
        state.els.recentTable.replaceChildren(createTable([
            {key: "timestamp", label: "Timestamp", format: formatTimestamp},
            {key: "status", label: "Status", format: value => value || "completed"},
            {key: "gateway_model", label: "Gateway", format: value => value || "N/A"},
            {key: "operation", label: "Operation", format: value => value || "N/A"},
            {key: "x_title", label: "X-Title", format: value => value || "N/A"},
            {key: "provider", label: "Provider", format: value => value || "N/A"},
            {key: "model", label: "Model", format: value => value || "N/A"},
            {key: "total_tokens", label: "Tokens", format: formatNumber},
            {key: "cost", label: "Cost", format: formatMoney},
        ], rows));
    }

    function renderDashboard(payload) {
        state.els.dashboard.hidden = false;
        renderKpis(payload);
        renderLineChart(payload);
        renderBarChart(payload);
        renderBreakdownTable(payload);
        renderXTitleTable(payload);
        renderReliabilityTable(payload);
        renderKeyTable(payload);
        renderRecentTable(payload);
        const requestCount = numberValue(payload.totals && payload.totals.requests);
        setStatus(requestCount === 0 ? "No usage for selected filters." : `Analytics loaded: ${formatNumber(requestCount)} requests.`);
    }

    async function ensureIdentity() {
        if (!state.identity && global.gatewayAuth && typeof global.gatewayAuth.fetchIdentity === "function") {
            state.identity = await global.gatewayAuth.fetchIdentity();
        }
        if (!state.identity) {
            state.identity = {role: "unknown"};
        }
    }

    async function loadAnalytics() {
        if (state.loading) return;
        setLoading(true);
        try {
            await ensureIdentity();
            const response = await global.gatewayAuth.apiFetch(buildDashboardUrl());
            const payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.detail || `Analytics dashboard failed: ${response.status}`);
            }
            state.data = payload;
            state.loaded = true;
            updateFilterOptions(payload);
            renderDashboard(payload);
        } catch (error) {
            console.error("Failed to load analytics dashboard:", error);
            state.els.dashboard.hidden = true;
            setStatus(`Failed to load analytics: ${error.message}`, true);
        } finally {
            setLoading(false);
        }
    }

    function bindEvents() {
        state.els.refresh.addEventListener("click", loadAnalytics);
        state.els.keyScope.addEventListener("change", () => {
            updateKeyScopeState();
            loadAnalytics();
        });
        [
            state.els.range,
            state.els.bucket,
            state.els.apiKeyId,
            state.els.upstreamKey,
            state.els.operation,
            state.els.gateway,
            state.els.provider,
            state.els.model,
            state.els.xTitle,
            state.els.estimated,
        ].forEach(el => {
            el.addEventListener("change", loadAnalytics);
        });
        state.els.filters.addEventListener("submit", event => {
            event.preventDefault();
            loadAnalytics();
        });
    }

    function init() {
        if (state.initialized) return;
        state.els = {
            filters: document.getElementById("analyticsFilters"),
            range: document.getElementById("analyticsRange"),
            bucket: document.getElementById("analyticsBucket"),
            keyScope: document.getElementById("analyticsKeyScope"),
            apiKeyId: document.getElementById("analyticsApiKeyId"),
            upstreamKey: document.getElementById("analyticsUpstreamKey"),
            operation: document.getElementById("analyticsOperation"),
            gateway: document.getElementById("analyticsGateway"),
            provider: document.getElementById("analyticsProvider"),
            model: document.getElementById("analyticsModel"),
            xTitle: document.getElementById("analyticsXTitle"),
            estimated: document.getElementById("analyticsEstimated"),
            refresh: document.getElementById("analyticsRefreshButton"),
            status: document.getElementById("analyticsStatus"),
            dashboard: document.getElementById("analyticsDashboard"),
            kpis: document.getElementById("analyticsKpis"),
            lineChart: document.getElementById("analyticsLineChart"),
            barChart: document.getElementById("analyticsBarChart"),
            trendMeta: document.getElementById("analyticsTrendMeta"),
            costMeta: document.getElementById("analyticsCostMeta"),
            breakdownTable: document.getElementById("analyticsBreakdownTable"),
            xTitleTable: document.getElementById("analyticsXTitleTable"),
            reliabilityTable: document.getElementById("analyticsReliabilityTable"),
            keyTable: document.getElementById("analyticsKeyTable"),
            recentTable: document.getElementById("analyticsRecentTable"),
        };
        if (!state.els.filters || !state.els.dashboard) return;
        bindEvents();
        updateKeyScopeState();
        state.initialized = true;
    }

    async function activate() {
        init();
        if (!state.initialized) return;
        if (!state.loaded) {
            await loadAnalytics();
        } else {
            renderDashboard(state.data);
        }
    }

    global.usageAnalyticsDashboard = {
        init,
        activate,
    };
})(window);
