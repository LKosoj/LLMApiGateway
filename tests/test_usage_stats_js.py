import os
from pathlib import Path


def test_usage_stats_js_no_double_load():
    """
    Regression test for Task 7: Ensure fetchAndRenderStats() is not called twice on page load.
    The function should be:
    1. Defined
    2. Added to refreshButton listener
    3. Added to periodSelector listener
    4. Called in tab switching logic
    5. Called in the final initial load logic
    
    It should NOT be called unconditionally in the middle of the script.
    """
    js_path = "static/usage-stats.js"
    assert os.path.exists(js_path)
    
    with open(js_path, "r") as f:
        content = f.read()
    
    # Count occurrences of fetchAndRenderStats
    # We expect exactly 5 occurrences in the current version:
    # 1. Definition: const fetchAndRenderStats = async () => {
    # 2. Listener: refreshButton.addEventListener('click', fetchAndRenderStats);
    # 3. Listener: periodSelector.addEventListener('change', fetchAndRenderStats);
    # 4. Tab switch: } else { fetchAndRenderStats(); }
    # 5. Final load: } else { fetchAndRenderStats(); }
    
    # Note: Using .count() is simple but might be fragile if comments contain the name.
    # However, for this specific file, it should work.
    
    # To be more precise, let's find all calls fetchAndRenderStats()
    import re
    calls = re.findall(r'fetchAndRenderStats\(\)', content)
    
    # We expect 2 calls with parentheses:
    # 1. Inside tab switch button listener
    # 2. Inside the final initial load check
    assert len(calls) == 2, f"Expected 2 calls to fetchAndRenderStats(), found {len(calls)}: {calls}"
    
    # Also check total occurrences of the name (definition + listeners + calls)
    name_occurrences = re.findall(r'fetchAndRenderStats', content)
    assert len(name_occurrences) == 5, f"Expected 5 occurrences of 'fetchAndRenderStats', found {len(name_occurrences)}"

    # Ensure there is no 'Initial load of statistics' comment followed by a call, 
    # which was the signature of the double-load bug.
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
    assert "return gatewayModel || 'N/A';" in content
    assert "return operation || 'N/A';" in content
    assert "tdGatewayModel.textContent = formatGatewayModel(row);" in content
    assert "tdResolvedModel.textContent = formatResolvedTarget(row);" in content
    assert "tdOperation.textContent = formatOperation(row);" in content
    assert "Gateway Model" in content
    assert "Resolved Model" in content
    assert "Operation" in content
    assert "'provider', 'request_id'" not in content
    assert "key !== 'id' && key !== 'request_id' && key !== 'provider'" in content


def test_usage_stats_js_hides_provider_column_in_latest_records():
    js_path = "static/usage-stats.js"
    assert os.path.exists(js_path)

    with open(js_path, "r") as f:
        content = f.read()

    assert "'timestamp', 'duration_ms', 'gateway_model', 'operation', 'model'" in content
    assert "'model', 'x_title', 'prompt_tokens'" in content
    assert "displayMetric = 'X-Title';" in content
    assert "'reasoning_tokens', 'total_tokens', 'cached_tokens', 'cost'" in content
    assert "'provider'" not in content.split("const preferredHeaders = [", 1)[1].split("];", 1)[0]
    assert "'status'" not in content.split("const preferredHeaders = [", 1)[1].split("];", 1)[0]
    assert "key !== 'id' && key !== 'request_id' && key !== 'provider' && key !== 'status'" in content


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
    assert "upstreamStatsArea.appendChild(createUpstreamStatsTable(data));" in content
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
    assert 'id="analyticsApiKeyId"' in html
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
    assert 'id="analyticsReliabilityTable"' in html
    assert 'id="analyticsKeyTable"' in html
    assert 'id="analyticsRecentTable"' in html
    assert '<label data-master-only>' in html
    assert '<section class="analytics-panel" data-master-only' in html


def test_usage_stats_js_bridges_analytics_tab_without_touching_existing_tabs():
    js = Path("static/usage-stats.js").read_text(encoding="utf-8")

    assert "} else if (tab === 'analytics') {" in js
    assert "window.usageAnalyticsDashboard.activate();" in js
    assert "} else if (tab === 'fallback') {" in js
    assert "} else if (activeTab && activeTab.dataset.tab === 'analytics') {" in js


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


def test_usage_analytics_does_not_send_api_key_id_for_non_master_identity():
    js = Path("static/usage-analytics.js").read_text(encoding="utf-8")

    assert "function isMaster()" in js
    assert 'if (isMaster()) {' in js
    assert 'url.searchParams.set("api_key_id", state.els.apiKeyId.value);' in js
    assert 'url.searchParams.set("upstream_key_fingerprint", state.els.upstreamKey.value);' in js
    assert "fetchIdentity" in js


def test_usage_analytics_exposes_x_title_filter_and_breakdown():
    js = Path("static/usage-analytics.js").read_text(encoding="utf-8")
    html = Path("static/usage-stats.html").read_text(encoding="utf-8")

    assert 'id="analyticsXTitle"' in html
    assert 'id="analyticsXTitleTable"' in html
    assert "replaceOptions(state.els.xTitle, options.x_titles || [], \"All titles\");" in js
    assert "x_title: state.els.xTitle.value" in js
    assert "function renderXTitleTable(payload)" in js
    assert "payload.breakdowns && payload.breakdowns.x_titles" in js
    assert "{key: \"x_title\", label: \"X-Title\"" in js
    assert "renderXTitleTable(payload);" in js
