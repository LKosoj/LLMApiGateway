import os

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
    assert "'reasoning_tokens', 'total_tokens', 'cached_tokens', 'cost'" in content
    assert "'provider'" not in content.split("const preferredHeaders = [", 1)[1].split("];", 1)[0]
    assert "'status'" not in content.split("const preferredHeaders = [", 1)[1].split("];", 1)[0]
    assert "key !== 'id' && key !== 'request_id' && key !== 'provider' && key !== 'status'" in content
