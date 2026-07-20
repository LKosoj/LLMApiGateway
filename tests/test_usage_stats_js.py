import os
import json
from pathlib import Path


def test_usage_stats_js_no_double_load():
    js_path = "static/usage-stats.js"
    assert os.path.exists(js_path)
    
    with open(js_path, "r") as f:
        content = f.read()
    
    import re
    calls = re.findall(r'fetchAndRenderStats\(\)', content)

    assert len(calls) == 1
    assert "refreshButton.addEventListener('click', fetchAndRenderStats);" in content
    assert "periodSelector.addEventListener('change', fetchAndRenderStats);" in content
    assert "const pendingLoad = fetchAndRenderStats();" in content
    assert "// Initial load of statistics" not in content


def test_usage_stats_js_formats_resolved_provider_model():
    js_path = "static/usage-stats.js"
    assert os.path.exists(js_path)

    with open(js_path, "r") as f:
        content = f.read()

    assert "formatResolvedTarget" in content
    assert "formatGatewayModel" in content
    assert "formatOperation" in content
    assert "usage-record-running" in content
    assert "return `${provider}/${model}`;" in content
    assert "return gatewayModel || notAvailable();" in content
    assert "return operation || notAvailable();" in content
    assert "tdGatewayModel.textContent = formatGatewayModel(row);" in content
    assert "tdResolvedModel.textContent = formatResolvedTarget(row);" in content
    assert "tdOperation.textContent = formatOperation(row);" in content
    assert "usage:columns.gatewayModel" in content
    assert "usage:columns.resolvedModel" in content
    assert "usage:columns.operationHeader" in content
    assert "'provider', 'request_id'" not in content
    assert "key !== 'id' && key !== 'request_id' && key !== 'provider'" in content


def test_usage_stats_js_hides_provider_column_in_latest_records():
    js_path = "static/usage-stats.js"
    assert os.path.exists(js_path)

    with open(js_path, "r") as f:
        content = f.read()

    assert "'timestamp', 'duration_ms', 'gateway_model', 'operation', 'model'" in content
    assert "'model', 'x_title', 'prompt_tokens'" in content
    assert "x_title: 'usage:columns.xTitleHeader'" in content
    assert "'reasoning_tokens', 'total_tokens', 'cached_tokens', 'cost'" in content
    assert "'provider'" not in content.split("const preferredHeaders = [", 1)[1].split("];", 1)[0]
    assert "'status'" not in content.split("const preferredHeaders = [", 1)[1].split("];", 1)[0]
    assert "key !== 'id' && key !== 'request_id' && key !== 'provider' && key !== 'status'" in content


def test_fallback_chains_render_x_title():
    content = Path("static/usage-stats.js").read_text(encoding="utf-8")

    assert "titleSpan.className = 'chain-title';" in content
    assert "gatewayI18n.t('usage:format.xTitle'" in content


def test_usage_stats_html_contains_upstream_analytics_subtab():
    content = Path("static/usage-stats.html").read_text(encoding="utf-8")

    assert "Upstream Analytics" in content
    assert 'id="upstreamStatsArea"' in content
    assert 'id="upstreamPeriodSelector"' in content
    assert 'id="upstreamRefreshButton"' in content


def test_usage_stats_js_fetches_upstream_stats_for_selected_period():
    content = Path("static/usage-stats.js").read_text(encoding="utf-8")

    assert "fetchAndRenderUpstreamStats" in content
    assert "createUpstreamStatsTable" in content
    assert "upstreamStatsArea.replaceChildren(createUpstreamStatsTable(snapshots.upstream.data));" in content
    assert "apiFetch(`/v1/api/upstream-stats/${selectedPeriod}`)" in content


def test_usage_stats_topology_uses_local_vendor_bundle():
    """Topology tab must load React + ReactFlow from a locally hosted bundle
    (`/static/vendor/topology.bundle.mjs`). Loading from public CDNs is
    forbidden — clients behind firewalls without `esm.sh`/`unpkg` access
    would see "Failed to load resource", and a re-introduction of separate
    CDN imports would also bring back the duplicate-React `useState is null`
    crash.
    """
    bundle_path = Path("static/vendor/topology.bundle.mjs")
    assert bundle_path.exists(), "Vendor bundle missing — run `npm --prefix frontend/topology run build`"
    assert bundle_path.stat().st_size > 50_000, "Vendor bundle suspiciously small"

    # React Flow ships layout-critical CSS as a sibling file. Without it nodes
    # render with `position: static` and stack vertically instead of being
    # placed by their `transform: translate(...)`.
    css_path = Path("static/vendor/topology.bundle.css")
    assert css_path.exists(), "Vendor CSS missing — run `npm --prefix frontend/topology run build`"
    assert css_path.stat().st_size > 5_000, "Vendor CSS suspiciously small"

    html = Path("static/usage-stats.html").read_text(encoding="utf-8")
    assert "esm.sh" not in html
    assert "type=\"importmap\"" not in html

    js = Path("static/usage-stats.js").read_text(encoding="utf-8")
    assert "'/static/vendor/topology.bundle.mjs'" in js
    assert "'/static/vendor/topology.bundle.css'" in js
    # The old CDN imports must not come back — they break inside firewalled networks
    # and re-introduce the duplicate React instance bug.
    assert "esm.sh" not in js
    assert "import('react')" not in js
    assert "import('@xyflow/react')" not in js


def test_usage_stats_html_contains_analytics_dashboard_tab():
    html = Path("static/usage-stats.html").read_text(encoding="utf-8")

    assert "/static/usage-analytics.css" in html
    assert "/static/usage-analytics.js" in html
    assert 'data-tab="analytics"' in html
    assert 'id="analyticsTabContent"' in html
    assert 'id="analyticsRange"' in html
    assert 'id="analyticsBucket"' in html
    assert 'id="analyticsKeyScope"' in html
    assert 'id="analyticsApiKeyId"' not in html
    assert 'id="analyticsUpstreamKey"' in html
    assert 'id="analyticsOperation"' in html
    assert 'id="analyticsGateway"' in html
    assert 'id="analyticsProvider"' in html
    assert 'id="analyticsModel"' in html
    assert 'id="analyticsXTitle"' in html
    assert 'id="analyticsEstimated"' in html
    assert 'id="analyticsKpis"' in html
    assert 'id="analyticsLineChart"' in html
    assert 'id="analyticsBarChart"' in html
    assert 'id="analyticsBreakdownTable"' in html
    assert 'id="analyticsXTitleTable"' in html
    assert (
        'id="analyticsReliabilityTable" class="analytics-table-wrap" tabindex="0"'
        in html
    )
    assert 'id="analyticsKeyTable"' in html
    assert 'id="analyticsRecentTable"' in html
    assert '<label data-master-only hidden>' in html
    assert '<section class="analytics-panel" data-master-only' in html


def test_usage_stats_js_bridges_analytics_tab_without_touching_existing_tabs():
    js = Path("static/usage-stats.js").read_text(encoding="utf-8")

    assert "} else if (tab === 'analytics') {" in js
    assert "window.usageAnalyticsDashboard.activate();" in js
    assert "} else if (tab === 'fallback') {" in js
    assert "reportTabActivation(usageTabsController.activate(usageTabsController.activeKey));" in js


def test_usage_analytics_uses_existing_endpoints_and_no_cdn_or_build_imports():
    js = Path("static/usage-analytics.js").read_text(encoding="utf-8")
    css = Path("static/usage-analytics.css").read_text(encoding="utf-8")

    assert 'const DASHBOARD_ENDPOINT = "/v1/api/analytics-dashboard";' in js
    assert "/v1/api/usage-stats/" not in js
    assert "/v1/api/upstream-stats/" not in js
    assert "document.createElementNS(SVG_NS" in js
    assert "http://www.w3.org/2000/svg" in js
    assert "import(" not in js
    assert "esm.sh" not in js
    assert "unpkg" not in js
    assert "@xyflow" not in js
    assert "url(" not in css


def test_usage_analytics_reads_fallback_summary_from_api_shape():
    js = Path("static/usage-analytics.js").read_text(encoding="utf-8")

    assert "const fallbackSummary = fallback.summary || {};" in js
    assert "formatNumber(fallbackSummary.attempts)" in js
    assert "formatNumber(fallbackSummary.errors)" in js
    assert "formatRate(fallbackSummary.success_rate)" in js


def test_usage_analytics_does_not_send_api_key_id():
    js = Path("static/usage-analytics.js").read_text(encoding="utf-8")

    assert "function isMaster()" in js
    assert 'if (isMaster()) {' in js
    assert 'url.searchParams.set("api_key_id"' not in js
    assert "state.els.apiKeyId" not in js
    assert "updateKeyScopeState" not in js
    assert 'url.searchParams.set("upstream_key_fingerprint", state.els.upstreamKey.value);' in js
    assert "fetchIdentity" in js


def test_usage_analytics_exposes_x_title_filter_and_breakdown():
    js = Path("static/usage-analytics.js").read_text(encoding="utf-8")
    html = Path("static/usage-stats.html").read_text(encoding="utf-8")

    assert 'id="analyticsXTitle"' in html
    assert 'id="analyticsXTitleTable"' in html
    assert 'replaceOptions(state.els.xTitle, options.x_titles || [], t("usage:filters.allTitles"));' in js
    assert "x_title: state.els.xTitle.value" in js
    assert "function renderXTitleTable(payload)" in js
    assert "payload.breakdowns && payload.breakdowns.x_titles" in js
    assert '{key: "x_title", label: t("usage:columns.xTitleHeader")' in js
    assert "renderXTitleTable(payload);" in js


def test_usage_analytics_bar_chart_cost_scale_is_independent_of_request_count():
    """The "Cost by Provider" bar chart must scale bar height by cost alone.

    Regression guard for a bug where the chart's max was computed jointly
    across cost (dollars, e.g. 0.01) and request counts (e.g. 5), so a
    provider with a tiny but real cost got squashed to a near-invisible bar
    whenever any provider had a larger request count. Request counts may
    still surface as a text label next to the bar (when cost is zero), but
    must never feed the shared scale or the bar's own height.
    """
    js = Path("static/usage-analytics.js").read_text(encoding="utf-8")

    assert "const maxValue = Math.max(...bars.map(bar => numberValue(bar.cost)), 1);" in js
    assert "...bars.map(bar => numberValue(bar.requests))" not in js
    assert "const value = numberValue(bar.cost);" in js
    assert "const value = numberValue(bar.cost) || numberValue(bar.requests);" not in js
    assert 'numberValue(bar.cost) ? formatMoney(bar.cost) : formatNumber(bar.requests)' in js


def test_usage_page_declares_localized_bindings_and_separate_raw_error_sinks():
    html = Path("static/usage-stats.html").read_text(encoding="utf-8")

    assert 'data-i18n="usage:page.heading"' in html
    assert 'data-i18n="usage:tabs.analytics"' in html
    assert 'data-i18n="usage:tabs.statistics"' in html
    assert 'data-i18n="topology:toolbar.autoRefresh"' in html
    assert 'id="messageRawDetail"' in html
    assert 'id="analyticsRawDetail"' in html
    assert html.index('id="messageArea"') < html.index('id="messageRawDetail"')
    assert html.index('id="analyticsStatus"') < html.index('id="analyticsRawDetail"')


def test_usage_scripts_use_runtime_i18n_and_locale_only_snapshot_rerender():
    stats = Path("static/usage-stats.js").read_text(encoding="utf-8")
    analytics = Path("static/usage-analytics.js").read_text(encoding="utf-8")
    combined = f"{stats}\n{analytics}"

    assert ".toLocaleString(" not in combined
    assert "new Intl." not in combined
    assert "gatewayI18n.formatNumber" in combined
    assert "gatewayI18n.formatCurrency" in combined
    assert "gatewayI18n.formatDate" in combined
    assert "gatewayI18n.subscribe(rerenderLocale)" in stats
    assert "function rerenderLocale()" in stats
    assert "window.usageAnalyticsDashboard.rerenderLocale();" in stats
    assert "rerenderLocale" in analytics
    assert "state.data" in analytics
    assert "requestGeneration" in analytics

    locale_body = stats.split("function rerenderLocale()", 1)[1].split("\n    }", 1)[0]
    assert "apiFetch(" not in locale_body
    assert "fetchAndRender" not in locale_body
    assert "loadTopology(" not in locale_body
    assert "activate(" not in locale_body


def test_usage_errors_use_shared_descriptor_and_structurally_separate_raw_text():
    stats = Path("static/usage-stats.js").read_text(encoding="utf-8")
    analytics = Path("static/usage-analytics.js").read_text(encoding="utf-8")

    assert "gatewayUi.describeApiError" in stats
    assert "gatewayUi.createStatus(messageArea" in stats
    assert "rawDetailElement: messageRawDetail" in stats
    assert "gatewayUi.describeApiError" in analytics
    assert "gatewayUi.createStatus(state.els.status" in analytics
    assert "rawDetailElement: state.els.rawDetail" in analytics
    assert "innerHTML" not in analytics


def test_usage_recent_statuses_use_a_closed_catalog_map_and_keep_unknown_values_raw():
    analytics = Path("static/usage-analytics.js").read_text(encoding="utf-8")

    assert "const RECENT_STATUS_KEYS = Object.freeze({" in analytics
    for status, key in {
        "completed": "usage:values.completedLabel",
        "running": "usage:values.runningLabel",
        "in_progress": "usage:values.inProgressLabel",
        "failed": "usage:values.failedLabel",
    }.items():
        assert f'{status}: "{key}"' in analytics
    assert "function formatRecentStatus(value)" in analytics
    assert "Object.hasOwn(RECENT_STATUS_KEYS, normalized)" in analytics
    assert "technical: value => !isKnownRecentStatus(value)" in analytics
    assert "`usage:values.${" not in analytics


def test_usage_topology_rerenders_locale_without_replacing_react_root():
    stats = Path("static/usage-stats.js").read_text(encoding="utf-8")

    assert "localeRevision" in stats
    assert "rerenderLocale:" in stats
    assert "setLocaleRevision" in stats
    assert "createRoot(mountEl)" in stats
    assert "topology:controls.zoomInAction" in stats
    assert "topology:controls.zoomOutAction" in stats
    assert "topology:controls.fitViewAction" in stats
    assert "topology:controls.toggleInteractivityAction" in stats


def test_usage_and_topology_catalogs_have_exact_ru_en_semantic_parity():
    for namespace in ("usage", "topology"):
        en = json.loads(
            Path(f"static/locales/en/{namespace}.json").read_text(encoding="utf-8")
        )
        ru = json.loads(
            Path(f"static/locales/ru/{namespace}.json").read_text(encoding="utf-8")
        )

        def flatten(value, prefix=""):
            result = {}
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else key
                if isinstance(child, dict):
                    result.update(flatten(child, path))
                else:
                    result[path] = child
            return result

        en_flat = flatten(en)
        ru_flat = flatten(ru)
        assert set(en_flat) == set(ru_flat)
        assert all(isinstance(value, str) and value for value in en_flat.values())
        assert all(isinstance(value, str) and value for value in ru_flat.values())

    assert "{count, plural," in en_flat["node.steps"]
    assert "{count, plural," in ru_flat["node.steps"]
