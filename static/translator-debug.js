"use strict";

// ── Dark-mode toggle (unified Theme manager from theme.js, as in quota.js) ───
document.addEventListener("DOMContentLoaded", () => {
    Theme.attachToggle("darkModeToggle");
    document.getElementById("runBtn").addEventListener("click", () => {
        void runTranslation();
    });
    document.getElementById("openaiSampleBtn").addEventListener("click", loadOpenAISample);
    document.getElementById("anthropicSampleBtn").addEventListener("click", loadAnthropicSample);
    document.getElementById("toolSampleBtn").addEventListener("click", loadToolSample);
    document.getElementById("stepsContainer").addEventListener("click", (event) => {
        const copyButton = event.target.closest(".btn-copy");
        if (copyButton) {
            copyStep(copyButton);
        }
    });
});

// ── Sample payloads ───────────────────────────────────────────────────────────
function loadOpenAISample() {
    document.getElementById("sourceFormat").value = "openai";
    document.getElementById("targetFormat").value = "anthropic";
    document.getElementById("requestInput").value = JSON.stringify(
        {
            model: "gpt-4o",
            messages: [
                { role: "system", content: "You are a helpful assistant." },
                { role: "user", content: "Hello, who are you?" },
            ],
            max_tokens: 256,
            temperature: 0.7,
        },
        null,
        2
    );
    document.getElementById("mockResponse").value = "";
}

function loadAnthropicSample() {
    document.getElementById("sourceFormat").value = "anthropic";
    document.getElementById("targetFormat").value = "openai";
    document.getElementById("requestInput").value = JSON.stringify(
        {
            model: "claude-3-5-sonnet-20241022",
            system: "You are a helpful assistant.",
            messages: [{ role: "user", content: "Hello, who are you?" }],
            max_tokens: 256,
        },
        null,
        2
    );
    document.getElementById("mockResponse").value = "";
}

function loadToolSample() {
    document.getElementById("sourceFormat").value = "openai";
    document.getElementById("targetFormat").value = "anthropic";
    document.getElementById("requestInput").value = JSON.stringify(
        {
            model: "gpt-4o",
            messages: [{ role: "user", content: "What is the weather in Paris?" }],
            tools: [
                {
                    type: "function",
                    function: {
                        name: "get_weather",
                        description: "Get current weather for a city",
                        parameters: {
                            type: "object",
                            properties: {
                                city: { type: "string", description: "City name" },
                            },
                            required: ["city"],
                        },
                    },
                },
            ],
            tool_choice: "auto",
        },
        null,
        2
    );
    document.getElementById("mockResponse").value = "";
}

// ── Main translation runner ───────────────────────────────────────────────────
async function runTranslation() {
    const runBtn = document.getElementById("runBtn");
    const errorBanner = document.getElementById("errorBanner");
    const stepsContainer = document.getElementById("stepsContainer");

    // Parse inputs
    const sourceFormat = document.getElementById("sourceFormat").value;
    const targetFormat = document.getElementById("targetFormat").value;
    const requestText = document.getElementById("requestInput").value.trim();
    const mockText = document.getElementById("mockResponse").value.trim();

    errorBanner.classList.remove("visible");
    errorBanner.textContent = "";

    // Validate request body
    let requestBody;
    try {
        requestBody = JSON.parse(requestText);
    } catch (e) {
        showError("Invalid JSON in request body: " + e.message);
        return;
    }

    // Parse optional mock
    let mockResponse = null;
    if (mockText) {
        try {
            mockResponse = JSON.parse(mockText);
        } catch (e) {
            showError("Invalid JSON in mock response: " + e.message);
            return;
        }
    }

    // Show loading state
    runBtn.disabled = true;
    runBtn.innerHTML = '<span class="spinner"></span>Running…';
    stepsContainer.innerHTML = "";

    try {
        const response = await gatewayAuth.apiFetch("/v1/admin/translator/debug", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                source_format: sourceFormat,
                target_format: targetFormat,
                request_body: requestBody,
                mock_provider_response: mockResponse,
            }),
        });

        if (!response.ok) {
            let detail = `HTTP ${response.status}`;
            try {
                const err = await response.json();
                detail = err.detail || detail;
            } catch (_) {}
            showError("Request failed: " + detail);
            return;
        }

        const data = await response.json();
        renderSteps(data.steps || []);
    } catch (err) {
        if (err.message !== "Authentication required") {
            showError("Network error: " + err.message);
        }
    } finally {
        runBtn.disabled = false;
        runBtn.textContent = "Run translation";
    }
}

function showError(msg) {
    const banner = document.getElementById("errorBanner");
    banner.textContent = msg;
    banner.classList.add("visible");
}

function renderSteps(steps) {
    const container = document.getElementById("stepsContainer");
    container.innerHTML = "";

    steps.forEach((step) => {
        const card = document.createElement("div");
        card.className = "step-card";

        const prettyPayload =
            step.payload !== null && typeof step.payload === "object"
                ? JSON.stringify(step.payload, null, 2)
                : step.payload !== null
                ? String(step.payload)
                : "";

        const notesHtml = step.notes
            ? `<details class="step-notes">
                <summary>Notes</summary>
                <p>${escapeHtml(step.notes)}</p>
               </details>`
            : "";

        card.innerHTML = `
            <div class="step-header">
                <span class="step-label">Step ${step.step}: ${escapeHtml(step.title)}</span>
                <button type="button" class="btn-copy">Copy</button>
            </div>
            <div class="step-card-body">
                <textarea readonly rows="8" spellcheck="false">${escapeHtml(prettyPayload)}</textarea>
                ${notesHtml}
            </div>`;

        container.appendChild(card);
    });
}

function copyStep(btn) {
    const textarea = btn.closest(".step-card").querySelector("textarea");
    navigator.clipboard.writeText(textarea.value).then(() => {
        const orig = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(() => {
            btn.textContent = orig;
        }, 1500);
    });
}

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}
