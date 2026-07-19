(function initUsageAnalytics(global) {
    const SVG_NS = "http://www.w3.org/2000/svg";
    const DASHBOARD_ENDPOINT = "/v1/api/analytics-dashboard";
    const RECENT_STATUS_KEYS = Object.freeze({
        completed: "usage:values.completedLabel",
        running: "usage:values.runningLabel",
        in_progress: "usage:values.inProgressLabel",
        failed: "usage:values.failedLabel",
    });
    const i18n = global.gatewayI18n;

    const state = {
        initialized: false,
        loaded: false,
        loading: false,
        identity: null,
        data: null,
        els: {},
        requestGeneration: 0,
        requestId: null,
        hasError: false,
        statusController: null,
    };

    function t(key, values = {}) {
        return i18n.t(key, values);
    }

    function message(key, values = {}) {
        return Object.freeze({key, values});
    }

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
        return i18n.formatNumber(numberValue(value));
    }

    function formatMoney(value) {
        return i18n.formatCurrency(numberValue(value), "USD", {
            minimumFractionDigits: 4,
            maximumFractionDigits: 4,
        });
    }

    function formatRate(value) {
        return i18n.formatNumber(numberValue(value) / 100, {
            style: "percent",
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
    }

    function formatThroughput(value) {
        const parsed = Number(value);
        if (value == null || !Number.isFinite(parsed)) return t("usage:values.notAvailable");
        return t("usage:format.tokensPerSecond", {
            value: i18n.formatNumber(parsed, {
                minimumFractionDigits: 1,
                maximumFractionDigits: 1,
            }),
        });
    }

    function formatOptionalRate(value) {
        const parsed = Number(value);
        if (value == null || !Number.isFinite(parsed)) return t("usage:values.notAvailable");
        return formatRate(parsed);
    }

    function formatDuration(value) {
        const ms = numberValue(value);
        if (!ms) return t("usage:values.notAvailable");
        if (ms < 1000) return t("usage:format.milliseconds", {value: formatNumber(ms)});
        return t("usage:format.seconds", {
            value: i18n.formatNumber(ms / 1000, {
                minimumFractionDigits: 1,
                maximumFractionDigits: 1,
            }),
        });
    }

    function formatTimestamp(value) {
        const timestamp = typeof value === "string" ? value.trim() : "";
        if (!timestamp) return t("usage:values.notAvailable");
        const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(timestamp);
        const parsed = new Date(hasZone ? timestamp : `${timestamp.replace(" ", "T")}Z`);
        if (Number.isNaN(parsed.getTime())) return timestamp;
        return i18n.formatDate(parsed, {
            dateStyle: "medium",
            timeStyle: "medium",
            timeZone: "UTC",
        });
    }

    function renderRequestId(requestId) {
        state.requestId = requestId;
        if (!state.els.requestId) return;
        state.els.requestId.hidden = requestId === null;
        state.els.requestId.textContent = requestId === null
            ? ""
            : i18n.t("usage:status.requestId", {id: requestId});
        if (requestId === null) {
            state.els.requestId.removeAttribute("lang");
            state.els.requestId.removeAttribute("dir");
        } else {
            state.els.requestId.setAttribute("lang", "und");
            state.els.requestId.setAttribute("dir", "auto");
        }
    }

    function publishStatus(statusMessage, kind = "polite", options = {}) {
        if (!state.statusController) return;
        state.statusController[kind](statusMessage, {
            rawDetail: options.rawDetail ?? null,
        });
        renderRequestId(options.requestId ?? null);
    }

    function setLoading(isLoading) {
        state.loading = isLoading;
        if (state.els.refresh) {
            state.els.refresh.disabled = isLoading;
            state.els.refresh.classList.toggle("loading", isLoading);
        }
        if (isLoading) {
            publishStatus(message("usage:status.analyticsLoading"), "busy");
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
            option.setAttribute("lang", "und");
            option.setAttribute("dir", "auto");
            select.appendChild(option);
        });
        select.value = Array.from(select.options).some(option => option.value === current) ? current : "";
    }

    function updateFilterOptions(payload) {
        const options = payload.filter_options || {};
        replaceOptions(state.els.operation, options.operations || [], t("usage:filters.allOperations"));
        replaceOptions(state.els.gateway, options.gateway_models || [], t("usage:filters.allGateways"));
        replaceOptions(state.els.provider, options.providers || [], t("usage:filters.allProviders"));
        replaceOptions(state.els.model, options.models || [], t("usage:filters.allModels"));
        replaceOptions(state.els.xTitle, options.x_titles || [], t("usage:filters.allTitles"));
        replaceOptions(state.els.upstreamKey, options.upstream_keys || [], t("usage:filters.allProviderKeys"));
    }

    function updateFilterLabels() {
        const labels = [
            [state.els.operation, "usage:filters.allOperations"],
            [state.els.gateway, "usage:filters.allGateways"],
            [state.els.provider, "usage:filters.allProviders"],
            [state.els.model, "usage:filters.allModels"],
            [state.els.xTitle, "usage:filters.allTitles"],
            [state.els.upstreamKey, "usage:filters.allProviderKeys"],
        ];
        labels.forEach(([select, key]) => {
            if (select?.options?.length) select.options[0].textContent = t(key);
        });
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

    function formatPinHonored(totals) {
        const tracked = numberValue(totals.fallback_tracked_requests);
        if (!tracked) return t("usage:values.notAvailable");
        return formatRate(numberValue(totals.first_attempt_requests) / tracked * 100);
    }

    function renderKpis(payload) {
        const totals = payload.totals || {};
        const kpis = [
            [t("usage:kpis.requestsLabel"), formatNumber(totals.requests)],
            [t("usage:kpis.totalTokens"), formatNumber(totals.total_tokens)],
            [t("usage:kpis.costLabel"), formatMoney(totals.cost)],
            [t("usage:kpis.costSaved"), formatMoney(totals.cost_saved)],
            [t("usage:kpis.costPerMillion"), formatMoney(totals.cost_per_million_tokens)],
            [t("usage:kpis.tokensPerSecond"), formatThroughput(totals.tokens_per_second)],
            [t("usage:kpis.pinHonored"), formatPinHonored(totals)],
            [t("usage:kpis.averageDuration"), formatDuration(totals.avg_duration_ms)],
            [t("usage:kpis.durationP50"), formatDuration(totals.duration_p50_ms)],
            [t("usage:kpis.durationP95"), formatDuration(totals.duration_p95_ms)],
            [t("usage:kpis.ttftAvg"), formatDuration(totals.ttft_avg_ms)],
            [t("usage:kpis.ttftP50"), formatDuration(totals.ttft_p50_ms)],
            [t("usage:kpis.ttftP95"), formatDuration(totals.ttft_p95_ms)],
            [t("usage:kpis.activeLabel"), formatNumber(totals.active_requests)],
            [t("usage:kpis.fallbackErrors"), formatNumber(totals.fallback_errors)],
            [t("usage:kpis.rejectionsLabel"), formatNumber(totals.rejections)],
            [t("usage:kpis.estimatedLabel"), formatNumber(totals.estimated_count)],
        ];
        if (payload.lifetime) {
            kpis.push(
                [t("usage:kpis.lifetimeRequests"), formatNumber(payload.lifetime.requests)],
                [t("usage:kpis.lifetimeTokens"), formatNumber(payload.lifetime.total_tokens)],
                [t("usage:kpis.lifetimeCost"), formatMoney(payload.lifetime.cost)],
            );
        }
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

        state.els.trendMeta.textContent = i18n.t("usage:counts.buckets", {count: points.length});
        if (points.length === 0) {
            renderEmpty(state.els.lineChart, t("usage:empty.trend"));
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

        const svg = svgEl("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": t("usage:charts.tokenTrendAria")});
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

        state.els.costMeta.textContent = i18n.t("usage:counts.providers", {count: bars.length});
        if (bars.length === 0) {
            renderEmpty(state.els.barChart, t("usage:empty.providers"));
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
        const svg = svgEl("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": t("usage:charts.providerCostAria")});
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
            const nameLabel = textNode("text", String(bar.label || t("usage:values.unknownLabel")).slice(0, 18), "analytics-chart-label");
            nameLabel.setAttribute("lang", "und");
            nameLabel.setAttribute("dir", "auto");
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
                const value = row[column.key];
                td.textContent = column.format
                    ? column.format(value, row)
                    : String(value ?? i18n.t("usage:values.notAvailable"));
                const technical = typeof column.technical === "function"
                    ? column.technical(value, row)
                    : column.technical;
                if (technical) {
                    td.setAttribute("lang", "und");
                    td.setAttribute("dir", "auto");
                }
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
            renderEmpty(state.els.breakdownTable, t("usage:empty.breakdown"));
            return;
        }
        state.els.breakdownTable.replaceChildren(createTable([
            {key: "label", label: t("usage:columns.resolvedTarget"), technical: true},
            {key: "requests", label: t("usage:columns.requestsHeader"), format: formatNumber},
            {key: "total_tokens", label: t("usage:columns.tokensHeader"), format: formatNumber},
            {key: "cost", label: t("usage:columns.costHeader"), format: formatMoney},
            {key: "cost_saved", label: t("usage:columns.costSaved"), format: formatMoney},
            {key: "estimated_count", label: t("usage:columns.estimatedHeader"), format: formatNumber},
            {key: "avg_duration_ms", label: t("usage:columns.averageDuration"), format: formatDuration},
            {key: "tokens_per_second", label: t("usage:columns.tokensPerSecond"), format: formatThroughput},
            {key: "fallback_errors", label: t("usage:columns.fallbackErrors"), format: formatNumber},
            {key: "fallback_success_rate", label: t("usage:columns.successRate"), format: formatOptionalRate},
        ], rows));
    }

    function renderProviderTable(payload) {
        const rows = ((payload.breakdowns && payload.breakdowns.providers) || []).slice(0, 20);
        if (rows.length === 0) {
            renderEmpty(state.els.providerTable, t("usage:empty.providers"));
            return;
        }
        state.els.providerTable.replaceChildren(createTable([
            {key: "label", label: t("usage:columns.providerHeader"), technical: true},
            {key: "requests", label: t("usage:columns.requestsHeader"), format: formatNumber},
            {key: "total_tokens", label: t("usage:columns.tokensHeader"), format: formatNumber},
            {key: "cost", label: t("usage:columns.costHeader"), format: formatMoney},
            {key: "tokens_per_second", label: t("usage:columns.tokensPerSecond"), format: formatThroughput},
            {key: "ttft_avg_ms", label: t("usage:columns.ttftAvg"), format: formatDuration},
            {key: "duration_p95_ms", label: t("usage:columns.durationP95"), format: formatDuration},
            {key: "fallback_errors", label: t("usage:columns.fallbackErrors"), format: formatNumber},
            {key: "fallback_success_rate", label: t("usage:columns.successRate"), format: formatOptionalRate},
        ], rows));
    }

    function renderXTitleTable(payload) {
        const rows = ((payload.breakdowns && payload.breakdowns.x_titles) || []).slice(0, 20);
        if (rows.length === 0) {
            renderEmpty(state.els.xTitleTable, t("usage:empty.xTitles"));
            return;
        }
        state.els.xTitleTable.replaceChildren(createTable([
            {key: "label", label: t("usage:columns.xTitleHeader"), technical: true},
            {key: "requests", label: t("usage:columns.requestsHeader"), format: formatNumber},
            {key: "total_tokens", label: t("usage:columns.tokensHeader"), format: formatNumber},
            {key: "cost", label: t("usage:columns.costHeader"), format: formatMoney},
            {key: "cost_saved", label: t("usage:columns.costSaved"), format: formatMoney},
            {key: "estimated_count", label: t("usage:columns.estimatedHeader"), format: formatNumber},
            {key: "avg_duration_ms", label: t("usage:columns.averageDuration"), format: formatDuration},
        ], rows));
    }

    function renderReliabilityTable(payload) {
        const fallback = payload.reliability && payload.reliability.fallback ? payload.reliability.fallback : {};
        const fallbackSummary = fallback.summary || {};
        const rejections = payload.reliability && payload.reliability.rejections ? payload.reliability.rejections : {};
        const summaryRows = [
            {metric: t("usage:reliability.fallbackAttempts"), value: formatNumber(fallbackSummary.attempts)},
            {metric: t("usage:reliability.fallbackErrors"), value: formatNumber(fallbackSummary.errors)},
            {metric: t("usage:reliability.fallbackSuccess"), value: formatRate(fallbackSummary.success_rate)},
            {metric: t("usage:reliability.rejectionsMetric"), value: formatNumber(rejections.summary && rejections.summary.rejections)},
        ];
        const errorRows = (fallback.error_types || []).slice(0, 4).map(row => ({
            metric: t("usage:format.fallbackMetric", {value: row.label}),
            value: formatNumber(row.errors),
        }));
        const rejectionRows = (rejections.categories || []).slice(0, 4).map(row => ({
            metric: t("usage:format.rejectedMetric", {value: row.label}),
            value: formatNumber(row.rejections),
        }));
        state.els.reliabilityTable.replaceChildren(createTable([
            {key: "metric", label: t("usage:columns.metricHeader")},
            {key: "value", label: t("usage:columns.valueHeader")},
        ], summaryRows.concat(errorRows, rejectionRows)));
    }

    function formatApiKeyName(value, row) {
        if (row.label === "unattributed") return t("usage:filters.unattributed");
        if (!row.label && row.api_key_id == null) return t("usage:values.unknownLabel");
        if (
            row.api_key_id != null
            && value === `Virtual key #${row.api_key_id}`
        ) {
            return t("usage:values.virtualKey", {id: row.api_key_id});
        }
        return String(value || t("usage:values.unknownLabel"));
    }

    function renderKeyTable(payload) {
        if (!isMaster()) {
            state.els.keyTable.replaceChildren();
            return;
        }
        const rows = ((payload.breakdowns && payload.breakdowns.api_keys) || []).slice(0, 20);
        if (rows.length === 0) {
            renderEmpty(state.els.keyTable, t("usage:empty.virtualKeys"));
            return;
        }
        state.els.keyTable.replaceChildren(createTable([
            {key: "api_key_name", label: t("usage:columns.virtualKey"), format: formatApiKeyName, technical: true},
            {key: "requests", label: t("usage:columns.requestsHeader"), format: formatNumber},
            {key: "total_tokens", label: t("usage:columns.tokensHeader"), format: formatNumber},
            {key: "cost", label: t("usage:columns.costHeader"), format: formatMoney},
            {key: "cost_saved", label: t("usage:columns.costSaved"), format: formatMoney},
            {key: "avg_duration_ms", label: t("usage:columns.averageDuration"), format: formatDuration},
        ], rows));
    }

    function normalizeRecentStatus(value) {
        return typeof value === "string" ? value.trim().toLowerCase() : "";
    }

    function isKnownRecentStatus(value) {
        const normalized = normalizeRecentStatus(value);
        return normalized === "" || Object.hasOwn(RECENT_STATUS_KEYS, normalized);
    }

    function formatRecentStatus(value) {
        const normalized = normalizeRecentStatus(value);
        if (normalized === "") return t("usage:values.completedLabel");
        if (Object.hasOwn(RECENT_STATUS_KEYS, normalized)) {
            return t(RECENT_STATUS_KEYS[normalized]);
        }
        return String(value);
    }

    function renderRecentTable(payload) {
        const rows = (payload.recent_records || []).slice(0, 15);
        if (rows.length === 0) {
            renderEmpty(state.els.recentTable, t("usage:empty.recent"));
            return;
        }
        state.els.recentTable.replaceChildren(createTable([
            {key: "timestamp", label: t("usage:columns.timestamp"), format: formatTimestamp},
            {
                key: "status",
                label: t("usage:columns.statusHeader"),
                format: formatRecentStatus,
                technical: value => !isKnownRecentStatus(value),
            },
            {key: "gateway_model", label: t("usage:columns.gatewayHeader"), format: value => value || t("usage:values.notAvailable"), technical: true},
            {key: "operation", label: t("usage:columns.operationHeader"), format: value => value || t("usage:values.notAvailable"), technical: true},
            {key: "x_title", label: t("usage:columns.xTitleHeader"), format: value => value || t("usage:values.notAvailable"), technical: true},
            {key: "provider", label: t("usage:columns.providerHeader"), format: value => value || t("usage:values.notAvailable"), technical: true},
            {key: "model", label: t("usage:columns.modelHeader"), format: value => value || t("usage:values.notAvailable"), technical: true},
            {key: "client_ip", label: t("usage:columns.clientIp"), format: value => value || t("usage:values.notAvailable"), technical: true},
            {key: "client_user_agent", label: t("usage:columns.userAgent"), format: value => (value ? String(value).slice(0, 40) : t("usage:values.notAvailable")), technical: true},
            {key: "total_tokens", label: t("usage:columns.tokensHeader"), format: formatNumber},
            {key: "cost", label: t("usage:columns.costHeader"), format: formatMoney},
        ], rows));
    }

    function renderDashboard(payload) {
        state.els.dashboard.hidden = false;
        renderKpis(payload);
        renderLineChart(payload);
        renderBarChart(payload);
        renderBreakdownTable(payload);
        renderProviderTable(payload);
        renderXTitleTable(payload);
        renderReliabilityTable(payload);
        renderKeyTable(payload);
        renderRecentTable(payload);
        const requestCount = numberValue(payload.totals && payload.totals.requests);
        publishStatus(
            requestCount === 0
                ? message("usage:empty.analytics")
                : message("usage:status.analyticsLoaded", {count: requestCount})
        );
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
        const requestGeneration = ++state.requestGeneration;
        state.hasError = false;
        setLoading(true);
        try {
            await ensureIdentity();
            const response = await global.gatewayAuth.apiFetch(buildDashboardUrl());
            const payload = await response.json().catch(() => ({}));
            if (requestGeneration !== state.requestGeneration) return;
            if (!response.ok) {
                state.hasError = true;
                const descriptor = global.gatewayUi.describeApiError(payload, {
                    status: response.status,
                    requestId: response.headers.get("X-Request-ID"),
                });
                state.els.dashboard.hidden = true;
                publishStatus(
                    message(descriptor.summaryKey, descriptor.summaryValues),
                    "error",
                    descriptor,
                );
                return;
            }
            state.data = payload;
            state.loaded = true;
            updateFilterOptions(payload);
            renderDashboard(payload);
        } catch (error) {
            if (requestGeneration !== state.requestGeneration) return;
            state.hasError = true;
            console.error("Failed to load analytics dashboard:", error);
            state.els.dashboard.hidden = true;
            const descriptor = global.gatewayUi.describeApiError(null);
            publishStatus(
                message(descriptor.summaryKey, descriptor.summaryValues),
                "error",
                {...descriptor, rawDetail: error.message},
            );
        } finally {
            if (requestGeneration === state.requestGeneration) setLoading(false);
        }
    }

    function bindEvents() {
        state.els.refresh.addEventListener("click", loadAnalytics);
        state.els.keyScope.addEventListener("change", loadAnalytics);
        [
            state.els.range,
            state.els.bucket,
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
            upstreamKey: document.getElementById("analyticsUpstreamKey"),
            operation: document.getElementById("analyticsOperation"),
            gateway: document.getElementById("analyticsGateway"),
            provider: document.getElementById("analyticsProvider"),
            model: document.getElementById("analyticsModel"),
            xTitle: document.getElementById("analyticsXTitle"),
            estimated: document.getElementById("analyticsEstimated"),
            refresh: document.getElementById("analyticsRefreshButton"),
            status: document.getElementById("analyticsStatus"),
            rawDetail: document.getElementById("analyticsRawDetail"),
            requestId: document.getElementById("analyticsRequestId"),
            dashboard: document.getElementById("analyticsDashboard"),
            kpis: document.getElementById("analyticsKpis"),
            lineChart: document.getElementById("analyticsLineChart"),
            barChart: document.getElementById("analyticsBarChart"),
            trendMeta: document.getElementById("analyticsTrendMeta"),
            costMeta: document.getElementById("analyticsCostMeta"),
            breakdownTable: document.getElementById("analyticsBreakdownTable"),
            providerTable: document.getElementById("analyticsProviderTable"),
            xTitleTable: document.getElementById("analyticsXTitleTable"),
            reliabilityTable: document.getElementById("analyticsReliabilityTable"),
            keyTable: document.getElementById("analyticsKeyTable"),
            recentTable: document.getElementById("analyticsRecentTable"),
        };
        if (!state.els.filters || !state.els.dashboard) return;
        state.statusController = global.gatewayUi.createStatus(state.els.status, {
            rawDetailElement: state.els.rawDetail,
            renderMessage: (statusMessage) => t(statusMessage.key, statusMessage.values || {}),
        });
        bindEvents();
        state.initialized = true;
    }

    async function activate() {
        init();
        if (!state.initialized) return;
        if (!state.loaded || state.hasError) {
            await loadAnalytics();
        } else {
            renderDashboard(state.data);
        }
    }

    function rerenderLocale() {
        if (!state.initialized) return;
        updateFilterLabels();
        if (state.loaded && state.data && !state.loading && !state.hasError) {
            renderDashboard(state.data);
        }
        state.statusController.rerender();
        renderRequestId(state.requestId);
    }

    global.usageAnalyticsDashboard = Object.freeze({
        init,
        activate,
        rerenderLocale,
    });
})(window);
