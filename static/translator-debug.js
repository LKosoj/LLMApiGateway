"use strict";

document.addEventListener("DOMContentLoaded", () => {
    const {apiFetch, bootstrapRoleUI} = window.gatewayAuth;
    const i18n = window.gatewayI18n;
    const EXPECTED_STEP_NAMES = [
        "client_request",
        "detected_format",
        "intermediate_openai_request",
        "target_request",
        "provider_response_mock",
        "intermediate_openai_response",
        "client_response",
    ];
    const STEP_CONFIG = {
        client_request: {
            titleKey: "translator:steps.clientRequest.title",
            noteKey: () => "translator:steps.clientRequest.notes",
        },
        detected_format: {
            titleKey: "translator:steps.detectedFormat.title",
            noteKey: () => "translator:steps.detectedFormat.notes",
        },
        intermediate_openai_request: {
            titleKey: "translator:steps.intermediateOpenaiRequest.title",
            noteKey: (context) => context.sourceFormat === "anthropic"
                ? "translator:steps.intermediateOpenaiRequest.notesFromAnthropic"
                : "translator:steps.intermediateOpenaiRequest.notesAlreadyOpenai",
        },
        target_request: {
            titleKey: "translator:steps.targetRequest.title",
            noteKey: (context) => context.targetFormat === "anthropic"
                ? "translator:steps.targetRequest.notesToAnthropic"
                : "translator:steps.targetRequest.notesAlreadyOpenai",
        },
        provider_response_mock: {
            titleKey: "translator:steps.providerResponseMock.title",
            noteKey: (context) => {
                if (context.hasMock) return "translator:steps.providerResponseMock.notesProvided";
                return context.targetFormat === "anthropic"
                    ? "translator:steps.providerResponseMock.notesDefaultAnthropic"
                    : "translator:steps.providerResponseMock.notesDefaultOpenai";
            },
        },
        intermediate_openai_response: {
            titleKey: "translator:steps.intermediateOpenaiResponse.title",
            noteKey: (context) => context.targetFormat === "anthropic"
                ? "translator:steps.intermediateOpenaiResponse.notesFromAnthropic"
                : "translator:steps.intermediateOpenaiResponse.notesAlreadyOpenai",
        },
        client_response: {
            titleKey: "translator:steps.clientResponse.title",
            noteKey: (context) => context.sourceFormat === "anthropic"
                ? "translator:steps.clientResponse.notesToAnthropic"
                : "translator:steps.clientResponse.notesAlreadyOpenai",
        },
    };
    const els = {
        sourceFormat: document.getElementById("sourceFormat"),
        targetFormat: document.getElementById("targetFormat"),
        requestInput: document.getElementById("requestInput"),
        mockResponse: document.getElementById("mockResponse"),
        runButton: document.getElementById("runBtn"),
        openaiSample: document.getElementById("openaiSampleBtn"),
        anthropicSample: document.getElementById("anthropicSampleBtn"),
        toolSample: document.getElementById("toolSampleBtn"),
        errorBanner: document.getElementById("errorBanner"),
        errorDetail: document.getElementById("errorDetail"),
        steps: document.getElementById("stepsContainer"),
    };
    const errorStatus = window.gatewayUi.createStatus(els.errorBanner, {
        rawDetailElement: els.errorDetail,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });

    let stepsSnapshot = null;
    let submitContext = null;
    let running = false;
    let stopped = false;
    let unsubscribeLocale = null;
    const cards = new Map();
    const copiedSteps = new Set();
    const copyTimers = new Map();

    Theme.attachToggle("darkModeToggle");

    function showError(message, rawDetail = null) {
        els.errorDetail.hidden = rawDetail === null;
        errorStatus.error(message, {rawDetail});
    }

    function clearError() {
        errorStatus.clear();
        els.errorDetail.hidden = true;
    }

    function renderRunButton() {
        els.runButton.disabled = running;
        els.runButton.setAttribute("aria-busy", String(running));
        els.steps.setAttribute("aria-busy", String(running));
        if (!running) {
            els.runButton.textContent = i18n.t("translator:actions.run");
            return;
        }
        els.runButton.textContent = i18n.t("translator:actions.running");
    }

    function loadOpenAISample() {
        els.sourceFormat.value = "openai";
        els.targetFormat.value = "anthropic";
        els.requestInput.value = JSON.stringify(
            {
                model: "gpt-4o",
                messages: [
                    {role: "system", content: "You are a helpful assistant."},
                    {role: "user", content: "Hello, who are you?"},
                ],
                max_tokens: 256,
                temperature: 0.7,
            },
            null,
            2,
        );
        els.mockResponse.value = "";
    }

    function loadAnthropicSample() {
        els.sourceFormat.value = "anthropic";
        els.targetFormat.value = "openai";
        els.requestInput.value = JSON.stringify(
            {
                model: "claude-3-5-sonnet-20241022",
                system: "You are a helpful assistant.",
                messages: [{role: "user", content: "Hello, who are you?"}],
                max_tokens: 256,
            },
            null,
            2,
        );
        els.mockResponse.value = "";
    }

    function loadToolSample() {
        els.sourceFormat.value = "openai";
        els.targetFormat.value = "anthropic";
        els.requestInput.value = JSON.stringify(
            {
                model: "gpt-4o",
                messages: [{role: "user", content: "What is the weather in Paris?"}],
                tools: [
                    {
                        type: "function",
                        function: {
                            name: "get_weather",
                            description: "Get current weather for a city",
                            parameters: {
                                type: "object",
                                properties: {
                                    city: {type: "string", description: "City name"},
                                },
                                required: ["city"],
                            },
                        },
                    },
                ],
                tool_choice: "auto",
            },
            null,
            2,
        );
        els.mockResponse.value = "";
    }

    function prettyPayload(payload) {
        if (payload !== null && typeof payload === "object") {
            return JSON.stringify(payload, null, 2);
        }
        return payload === null ? "" : String(payload);
    }

    function validateSteps(value) {
        if (!Array.isArray(value) || value.length !== EXPECTED_STEP_NAMES.length) {
            return {
                errorKey: "translator:errors.invalidResponse",
                rawDetail: JSON.stringify(value),
            };
        }
        for (let index = 0; index < EXPECTED_STEP_NAMES.length; index += 1) {
            const step = value[index];
            const expectedName = EXPECTED_STEP_NAMES[index];
            if (!step || typeof step !== "object" || Array.isArray(step)) {
                return {
                    errorKey: "translator:errors.invalidResponse",
                    rawDetail: JSON.stringify(step),
                };
            }
            if (step.name !== expectedName) {
                return {
                    errorKey: "translator:errors.unsupportedStep",
                    rawDetail: typeof step.name === "string"
                        ? step.name
                        : JSON.stringify(step),
                };
            }
            if (step.step !== index + 1 || !Object.hasOwn(step, "payload")) {
                return {
                    errorKey: "translator:errors.invalidResponse",
                    rawDetail: JSON.stringify(step),
                };
            }
        }
        return {steps: value};
    }

    function createStepCard(step) {
        const card = document.createElement("div");
        card.className = "step-card";
        card.dataset.stepName = step.name;

        const header = document.createElement("div");
        header.className = "step-header";
        const label = document.createElement("span");
        label.className = "step-label";
        label.id = `translator-step-${step.step}-label`;
        const copyButton = document.createElement("button");
        copyButton.type = "button";
        copyButton.className = "btn-copy";
        copyButton.dataset.stepName = step.name;
        header.append(label, copyButton);

        const body = document.createElement("div");
        body.className = "step-card-body";
        const payload = document.createElement("textarea");
        payload.readOnly = true;
        payload.rows = 8;
        payload.spellcheck = false;
        payload.setAttribute("aria-labelledby", label.id);
        payload.value = prettyPayload(step.payload);

        const notes = document.createElement("details");
        notes.className = "step-notes";
        const summary = document.createElement("summary");
        const noteText = document.createElement("p");
        notes.append(summary, noteText);
        body.append(payload, notes);
        card.append(header, body);
        cards.set(step.name, {card, label, copyButton, summary, noteText});
        return card;
    }

    function updateStepChrome(step) {
        const config = STEP_CONFIG[step.name];
        const card = cards.get(step.name);
        card.label.textContent = i18n.t("translator:steps.stepHeading", {
            step: i18n.formatNumber(step.step),
            title: i18n.t(config.titleKey),
        });
        card.summary.textContent = i18n.t("translator:steps.notes");
        card.noteText.textContent = i18n.t(config.noteKey(submitContext));
        card.copyButton.textContent = i18n.t(
            copiedSteps.has(step.name)
                ? "translator:actions.copied"
                : "translator:actions.copy",
        );
    }

    function renderSteps(steps) {
        cards.clear();
        const elements = steps.map(createStepCard);
        els.steps.replaceChildren(...elements);
        steps.forEach(updateStepChrome);
    }

    function clearCopyTimers() {
        for (const timer of copyTimers.values()) window.clearTimeout(timer);
        copyTimers.clear();
        copiedSteps.clear();
    }

    async function copyStep(button) {
        const name = button.dataset.stepName;
        const card = cards.get(name);
        const textarea = card.card.querySelector("textarea");
        try {
            await navigator.clipboard.writeText(textarea.value);
            if (stopped || !card.card.isConnected) return;
            copiedSteps.add(name);
            updateStepChrome(stepsSnapshot.find((step) => step.name === name));
            if (copyTimers.has(name)) window.clearTimeout(copyTimers.get(name));
            copyTimers.set(name, window.setTimeout(() => {
                copyTimers.delete(name);
                copiedSteps.delete(name);
                const step = stepsSnapshot?.find((item) => item.name === name);
                if (!stopped && step && card.card.isConnected) updateStepChrome(step);
            }, 1500));
        } catch (error) {
            showError({key: "translator:errors.copyFailed"}, error.message);
        }
    }

    function captureViewState() {
        const active = document.activeElement;
        const selection = active instanceof HTMLTextAreaElement
            ? {
                start: active.selectionStart,
                end: active.selectionEnd,
                direction: active.selectionDirection,
            }
            : null;
        return {
            active,
            selection,
            scrollX: window.scrollX,
            scrollY: window.scrollY,
            panels: [...document.querySelectorAll(".panel")].map((panel) => ({
                panel,
                left: panel.scrollLeft,
                top: panel.scrollTop,
            })),
        };
    }

    function restoreViewState(state) {
        if (state.active?.isConnected) {
            state.active.focus({preventScroll: true});
            if (state.selection && state.active instanceof HTMLTextAreaElement) {
                state.active.setSelectionRange(
                    state.selection.start,
                    state.selection.end,
                    state.selection.direction,
                );
            }
        }
        state.panels.forEach(({panel, left, top}) => {
            panel.scrollLeft = left;
            panel.scrollTop = top;
        });
        window.scrollTo(state.scrollX, state.scrollY);
    }

    async function runTranslation() {
        if (running || stopped) return;
        clearError();

        const sourceFormat = els.sourceFormat.value;
        const targetFormat = els.targetFormat.value;
        const requestText = els.requestInput.value.trim();
        const mockText = els.mockResponse.value.trim();
        let requestBody;
        let mockResponse = null;
        try {
            requestBody = JSON.parse(requestText);
        } catch (error) {
            showError({key: "translator:errors.invalidRequestJson"}, error.message);
            return;
        }
        if (mockText) {
            try {
                mockResponse = JSON.parse(mockText);
            } catch (error) {
                showError({key: "translator:errors.invalidMockJson"}, error.message);
                return;
            }
        }

        const requestContext = {
            sourceFormat,
            targetFormat,
            hasMock: mockResponse !== null,
        };
        running = true;
        clearCopyTimers();
        stepsSnapshot = null;
        submitContext = null;
        cards.clear();
        els.steps.replaceChildren();
        renderRunButton();

        try {
            const response = await apiFetch("/v1/admin/translator/debug", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    source_format: sourceFormat,
                    target_format: targetFormat,
                    request_body: requestBody,
                    mock_provider_response: mockResponse,
                }),
            });
            if (stopped) return;
            let data;
            try {
                data = await response.json();
            } catch (error) {
                showError({key: "translator:errors.invalidResponse"}, error.message);
                return;
            }
            if (stopped) return;
            if (!response.ok) {
                const descriptor = window.gatewayUi.describeApiError(data, {
                    status: response.status,
                });
                showError(
                    {key: descriptor.summaryKey, values: descriptor.summaryValues},
                    descriptor.rawDetail,
                );
                return;
            }

            const result = validateSteps(data.steps);
            if (!result.steps) {
                showError({key: result.errorKey}, result.rawDetail);
                return;
            }
            submitContext = requestContext;
            stepsSnapshot = result.steps;
            renderSteps(stepsSnapshot);
        } catch (error) {
            if (stopped || error.message === "Authentication required") return;
            const descriptor = window.gatewayUi.describeApiError(null);
            showError(
                {key: descriptor.summaryKey, values: descriptor.summaryValues},
                error.message,
            );
        } finally {
            running = false;
            if (!stopped) renderRunButton();
        }
    }

    window.addEventListener("beforeunload", () => {
        stopped = true;
        clearCopyTimers();
        unsubscribeLocale?.();
    });

    Promise.all([bootstrapRoleUI(), i18n.ready]).then(() => {
        if (stopped) return;
        els.runButton.addEventListener("click", () => void runTranslation());
        els.openaiSample.addEventListener("click", loadOpenAISample);
        els.anthropicSample.addEventListener("click", loadAnthropicSample);
        els.toolSample.addEventListener("click", loadToolSample);
        els.steps.addEventListener("click", (event) => {
            const copyButton = event.target.closest(".btn-copy");
            if (copyButton) void copyStep(copyButton);
        });
        renderRunButton();
        unsubscribeLocale = i18n.subscribe(() => {
            const viewState = captureViewState();
            renderRunButton();
            stepsSnapshot?.forEach(updateStepChrome);
            errorStatus.rerender();
            restoreViewState(viewState);
        });
    }).catch(() => undefined);
});
