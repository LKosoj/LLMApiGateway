import subprocess
import os

def test_no_inner_html_in_ui():
    """Check that innerHTML is not used in UI JS files."""
    for filepath in ['static/editor.js', 'static/usage-stats.js', 'static/gateway-docs.js']:
        with open(filepath, 'r') as f:
            lines = f.readlines()
            for line in lines:
                # Allow commented out innerHTML
                if 'innerHTML' in line and not line.strip().startswith('//'):
                    assert False, f"Found active innerHTML in {filepath}: {line.strip()}"


def test_web_playground_usage_values_are_escaped_before_inner_html_render():
    with open("static/web-playground.js", "r", encoding="utf-8") as file:
        content = file.read()

    for field in ("prompt_tokens", "completion_tokens", "total_tokens", "credits"):
        assert f"escapeHtml(usage.{field})" in content
    assert "cost=${escapeHtml(usage.cost)}" in content


def test_xss_prevention_functional():
    """
    Extract createRecordsTable and createStatsTable and run them in Node.js
    with a mock DOM to verify that HTML tags in data are treated as textContent.
    """
    js_test_code = """
    const fs = require('fs');

    // 1. Mock DOM
    class MockElement {
        constructor(tag) {
            this.tagName = tag;
            this.children = [];
            this._textContent = "";
        }
        appendChild(child) {
            this.children.push(child);
        }
        set textContent(val) {
            this._textContent = String(val);
        }
        get textContent() {
            return this._textContent;
        }
        
        // helper to find elements with specific text content
        findText(text) {
            if (this._textContent.includes(text)) return true;
            for (let child of this.children) {
                if (child.findText(text)) return true;
            }
            return false;
        }
    }

    const document = {
        createDocumentFragment: () => new MockElement('fragment'),
        createElement: (tag) => new MockElement(tag),
        getElementById: () => new MockElement('div'),
        body: new MockElement('body'),
        addEventListener: (event, cb) => {
            if (event === 'DOMContentLoaded') {
                // Execute the callback to load the functions into scope
                cb();
            }
        }
    };
    
    // Mock global objects
    global.document = document;
    global.sessionStorage = { getItem: () => null, setItem: () => {} };
    global.prompt = () => "mock-token";
    global.window = { matchMedia: () => ({ matches: false, addEventListener: () => {} }) };
    global.localStorage = { getItem: () => null, setItem: () => {} };

    // 2. Load the JS file
    const code = fs.readFileSync('static/usage-stats.js', 'utf8');
    
    // Evaluate the code
    eval(code);

    // Now we need to test if the created table safely handles XSS strings.
    // The functions are scoped inside the DOMContentLoaded listener. 
    // To test them, we'll redefine a small wrapper or just extract the functions.
    // Let's extract the functions instead of full eval.
    """
    
    # Let's do a pure JS test script
    js_test_code = """
    const fs = require('fs');

    // MOCK DOM
    class Node {
        constructor(tag) {
            this.tagName = tag;
            this.children = [];
            this._textContent = "";
        }
        appendChild(child) {
            this.children.push(child);
        }
        set textContent(val) {
            this._textContent = String(val);
        }
        get textContent() {
            return this._textContent;
        }
        addEventListener() {}
    }

    global.document = {
        createElement: (tag) => new Node(tag),
        createDocumentFragment: () => new Node('fragment'),
        getElementById: () => new Node('div'),
        querySelectorAll: () => [],
        querySelector: () => {
            const n = new Node('div');
            n.dataset = {};
            return n;
        },
        body: { classList: { add: () => {}, remove: () => {} } },
        addEventListener: (event, cb) => { if (event === 'DOMContentLoaded') global.domReady = cb; }
    };
    global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
    global.localStorage = { getItem: () => null, setItem: () => {} };
    global.prompt = () => "mock-token";
    global.Theme = { attachToggle: () => {}, init: () => {}, get: () => ({ mode: 'system', effective: 'light' }), set: () => {}, cycle: () => {} };
    global.window = {
        gatewayAuth: {
            apiFetch: async () => ({
                ok: true,
                json: async () => [],
                status: 200,
                statusText: "OK"
            })
        }
    };

    let code = fs.readFileSync('static/usage-stats.js', 'utf8');
    
    // We want to expose createStatsTable and createRecordsTable to global
    code = code.replace("const createStatsTable = (data) =>", "global.createStatsTable = (data) =>");
    code = code.replace("const createRecordsTable = (data) =>", "global.createRecordsTable = (data) =>");
    
    eval(code);
    global.domReady(); // trigger DOMContentLoaded

    const xssPayload = "<script>alert('XSS')</script>";
    
    // Test createStatsTable
    const statsData = [{
        time_period: "2023-10",
        model: xssPayload,
        prompt_tokens: 10,
        completion_tokens: 20,
        reasoning_tokens: 0,
        total_tokens: 30,
        cached_tokens: 0,
        count: 1,
        cost: 0.05,
        cost_per_million: 10
    }];

    const statsFragment = global.createStatsTable(statsData);
    if (!statsFragment) throw new Error("createStatsTable did not return a fragment");
    if (statsFragment.tagName !== 'fragment') throw new Error("createStatsTable did not return DocumentFragment");
    
    let foundText = false;
    function walk(node) {
        if (node._textContent === xssPayload) foundText = true;
        for (let child of node.children) walk(child);
    }
    walk(statsFragment);

    if (!foundText) throw new Error("XSS payload not found as textContent in createStatsTable result");

    // Test createRecordsTable
    const recordsData = [{
        timestamp: "2023-10-01",
        gateway_model: xssPayload,
        model: "provider/model",
        prompt_tokens: 10,
        completion_tokens: 20,
        reasoning_tokens: 0,
        total_tokens: 30,
        cached_tokens: 0,
        cost: 0.05,
        provider: xssPayload,
        request_id: xssPayload
    }];

    const recordsFragment = global.createRecordsTable(recordsData);
    if (!recordsFragment) throw new Error("createRecordsTable did not return a fragment");
    if (recordsFragment.tagName !== 'fragment') throw new Error("createRecordsTable did not return DocumentFragment");

    foundText = false;
    walk(recordsFragment);
    if (!foundText) throw new Error("XSS payload not found as textContent in createRecordsTable result");
    
    console.log("XSS prevention functional test passed!");
    """
    
    test_file = "test_xss.js"
    with open(test_file, 'w') as f:
        f.write(js_test_code)

    try:
        # Use check=False because Node may exit with error due to missing DOM for some scripts
        # but the core XSS prevention test may still pass
        result = subprocess.run(["node", test_file], check=False, capture_output=True, text=True)
        # Check if the success message is in stdout (the test passed before any DOM errors)
        if "XSS prevention functional test passed!" not in result.stdout:
            print("Stdout:", result.stdout)
            print("Stderr:", result.stderr)
            assert False, "Functional XSS test failed"
    finally:
        if os.path.exists(test_file):
            os.remove(test_file)
