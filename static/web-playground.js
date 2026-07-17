(function () {
    const SECTION_BUTTONS = Array.from(document.querySelectorAll("[data-playground-section-tab]"));
    const SECTION_PANELS = Array.from(document.querySelectorAll("[data-playground-section-panel]"));
    const WEB_TAB_BUTTONS = Array.from(document.querySelectorAll("[data-web-tab]"));
    const WEB_TAB_PANELS = Array.from(document.querySelectorAll("[data-web-panel]"));
    const MODEL_SELECTS = [
        {id: "chatModel", section: "chat"},
        {id: "searchModel", section: "web_search"},
        {id: "searchReadModel", section: "web_read"},
        {id: "readModel", section: "web_read"},
        {id: "tavilySearchModel", section: "web_search"},
        {id: "tavilySearchReadModel", section: "web_read"},
        {id: "tavilyExtractModel", section: "web_read"},
        {id: "researchModel", section: "web_research"},
        {id: "deepResearchModel", section: "web_deep_research"},
        {id: "audioSpeechModel", section: "audio_speech"},
        {id: "audioTranscriptionModel", section: "audio_transcriptions"},
        {id: "imageGenerationModel", section: "images_generations"},
        {id: "imageEditModel", section: "images_edits"},
        {id: "pdfConversionModel", section: "pdf_conversions"},
    ];
    const STATUS_KINDS = [
        "chat",
        "search",
        "read",
        "tavily-search",
        "tavily-extract",
        "research",
        "deep-research",
        "audio-speech",
        "audio-transcription",
        "image-generation",
        "image-edit",
        "pdf-conversion",
    ];
    const BLOB_URLS = new Map();
    const VOICE_CACHE = new Map();
    let voiceRequestToken = 0;
    const CHAT_CONTEXT_LIMIT = 20;
    const i18n = window.gatewayI18n;
    const KEYS = Object.freeze({
        modelsEmpty: "models.empty",
        modelsFusionSuffix: "models.fusionSuffix",
        modelsSelectFirst: "models.selectFirst",
        modelsLoadingVoices: "models.loadingVoices",
        modelsVoiceUnavailable: "models.voiceUnavailable",
        providerDefault: "options.providerDefault",
        voiceCatalogEmpty: "status.voiceCatalogEmpty",
        voiceLoadFailed: "status.voiceLoadFailed",
        selectModelFirst: "status.selectModelFirst",
        enterMessage: "status.enterMessage",
        running: "status.running",
        uploadingPdf: "status.uploadingPdf",
        pdfRunning: "status.pdfRunning",
        error: "status.error",
        done: "status.done",
        chatDone: "status.chatDone",
        pdfFailed: "status.pdfFailed",
        pdfDone: "status.pdfDone",
        requestFailed: "status.requestFailed",
        chatReset: "status.chatReset",
        modelsLoadFailed: "status.modelsLoadFailed",
        tooManyFiles: "status.tooManyFiles",
    });
    let simpleChatMessages = [];
    let fusionModelNames = new Set();
    let pendingLocaleUiState = null;
    let unsubscribeLocale = null;
    const { apiFetch } = window.gatewayAuth;

    function translate(key, values = {}) {
        return i18n.t(`playground:${key}`, values);
    }

    function numberText(value) {
        const number = Number(value);
        return i18n.formatNumber(Number.isFinite(number) ? number : 0);
    }

    function currencyText(value) {
        return i18n.formatCurrency(Number(value), "USD", {
            minimumFractionDigits: 6,
            maximumFractionDigits: 6,
        });
    }

    function runtimeNumberSpan(value, style = "decimal") {
        const number = Number(value);
        if (!Number.isFinite(number)) {
            return `<span lang="und" dir="auto">${escapeHtml(value)}</span>`;
        }
        const text = style === "currency" ? currencyText(number) : numberText(number);
        return `<span data-playground-number="${style}" data-playground-number-value="${escapeHtml(
            number
        )}">${escapeHtml(text)}</span>`;
    }

    function durationSpan(value) {
        const seconds = Number(value);
        if (!Number.isFinite(seconds)) return "";
        return `<span data-playground-duration data-playground-duration-value="${escapeHtml(
            seconds
        )}">${escapeHtml(formatDuration(seconds))}</span>`;
    }

    function updateRuntimeFormats() {
        document.querySelectorAll("[data-playground-number]").forEach((element) => {
            const number = Number(element.dataset.playgroundNumberValue);
            element.textContent = element.dataset.playgroundNumber === "currency"
                ? currencyText(number)
                : numberText(number);
        });
        document.querySelectorAll("[data-playground-duration]").forEach((element) => {
            element.textContent = formatDuration(Number(element.dataset.playgroundDurationValue));
        });
    }

    function translatedSpan(key, values = {}) {
        return `<span data-playground-i18n="${escapeHtml(key)}" data-playground-i18n-values="${escapeHtml(
            JSON.stringify(values)
        )}">${escapeHtml(translate(key, values))}</span>`;
    }

    function updateDynamicTranslations() {
        document.querySelectorAll("[data-playground-i18n]").forEach((element) => {
            let values = {};
            try {
                values = JSON.parse(element.dataset.playgroundI18nValues || "{}");
            } catch (_error) {
                values = {};
            }
            element.textContent = translate(element.dataset.playgroundI18n, values);
        });
        document.querySelectorAll("[data-playground-i18n-aria]").forEach((element) => {
            element.setAttribute("aria-label", translate(element.dataset.playgroundI18nAria));
        });
    }

    function setupPlaygroundTabs() {
        const sectionTabList = SECTION_BUTTONS[0]?.closest('[role="tablist"]');
        const webTabList = WEB_TAB_BUTTONS[0]?.closest('[role="tablist"]');
        const sectionPanels = SECTION_BUTTONS.map((button) => (
            SECTION_PANELS.find((panel) => (
                panel.dataset.playgroundSectionPanel === button.dataset.playgroundSectionTab
            ))
        ));
        const webPanels = WEB_TAB_BUTTONS.map((button) => (
            WEB_TAB_PANELS.find((panel) => panel.dataset.webPanel === button.dataset.webTab)
        ));
        if (
            !sectionTabList
            || !webTabList
            || sectionPanels.some((panel) => !panel)
            || webPanels.some((panel) => !panel)
        ) return;

        const activateSection = async (context) => {
            SECTION_BUTTONS.forEach((button) => {
                button.classList.toggle("active", button === context.tab);
            });
            if (context.key === "audio-speech") {
                await refreshAudioVoiceSelect(context);
            }
        };
        const activateWebTab = (context) => {
            WEB_TAB_BUTTONS.forEach((button) => {
                button.classList.toggle("active", button === context.tab);
            });
        };

        window.gatewayUi.createTabs(sectionTabList, {
            activation: "manual",
            getKey: (button) => button.dataset.playgroundSectionTab,
            initialKey: "web",
            onActivate: activateSection,
            onReselect: activateSection,
            panels: sectionPanels,
        });
        window.gatewayUi.createTabs(webTabList, {
            activation: "manual",
            getKey: (button) => button.dataset.webTab,
            initialKey: "search",
            onActivate: activateWebTab,
            onReselect: activateWebTab,
            panels: webPanels,
        });
    }

    async function loadModels() {
        const response = await apiFetch("/v1/ui/playground/models");
        if (!response.ok) {
            throw new Error(`Failed to load model lists (status ${response.status})`);
        }
        return response.json();
    }

    function populateSelect(select, models, fusionSet) {
        select.innerHTML = "";
        if (!models || models.length === 0) {
            const opt = document.createElement("option");
            opt.value = "";
            opt.dataset.playgroundOptionKey = KEYS.modelsEmpty;
            opt.textContent = translate(KEYS.modelsEmpty);
            opt.disabled = true;
            opt.selected = true;
            select.appendChild(opt);
            select.disabled = true;
            return;
        }
        select.disabled = false;
        models.forEach((name) => {
            const opt = document.createElement("option");
            opt.value = name;
            const isFusion = fusionSet && fusionSet.has(name);
            opt.dataset.playgroundModelLabel = name;
            opt.textContent = isFusion ? `${name} · ${translate(KEYS.modelsFusionSuffix)}` : name;
            if (isFusion) opt.dataset.fusion = "true";
            select.appendChild(opt);
        });
    }

    function setVoiceSelectState(key, disabled) {
        const select = document.getElementById("audioSpeechVoice");
        if (!select) return;
        select.innerHTML = "";
        const opt = document.createElement("option");
        opt.value = "";
        opt.dataset.playgroundOptionKey = key;
        opt.textContent = translate(key);
        opt.selected = true;
        select.appendChild(opt);
        select.disabled = Boolean(disabled);
    }

    function normalizeVoiceItem(item) {
        if (typeof item === "string") {
            return {id: item, name: item};
        }
        if (!item || typeof item !== "object") return null;
        const id = item.id || item.voice || item.name;
        if (!id) return null;
        return {
            id: String(id),
            name: String(item.name || id),
            language: item.language ? String(item.language) : "",
            gender: item.gender ? String(item.gender) : "",
            source: item.source ? String(item.source) : "",
        };
    }

    function populateVoiceSelect(voices) {
        const select = document.getElementById("audioSpeechVoice");
        if (!select) return;
        select.innerHTML = "";
        const defaultOpt = document.createElement("option");
        defaultOpt.value = "";
        defaultOpt.dataset.playgroundOptionKey = KEYS.providerDefault;
        defaultOpt.textContent = translate(KEYS.providerDefault);
        defaultOpt.selected = true;
        select.appendChild(defaultOpt);
        (voices || [])
            .map(normalizeVoiceItem)
            .filter(Boolean)
            .forEach((voice) => {
                const opt = document.createElement("option");
                const meta = [voice.language, voice.gender, voice.source].filter(Boolean);
                opt.value = voice.id;
                opt.textContent = meta.length ? `${voice.name} (${meta.join(", ")})` : voice.name;
                select.appendChild(opt);
            });
        select.disabled = false;
    }

    async function refreshAudioVoiceSelect(activationContext = null) {
        const requestToken = ++voiceRequestToken;
        const isStale = () => (
            requestToken !== voiceRequestToken
            || (activationContext && (activationContext.signal.aborted || !activationContext.isCurrent()))
        );
        const modelSelect = document.getElementById("audioSpeechModel");
        const model = modelSelect && !modelSelect.disabled ? modelSelect.value : "";
        if (!model) {
            setVoiceSelectState(KEYS.modelsSelectFirst, true);
            return;
        }
        if (VOICE_CACHE.has(model)) {
            populateVoiceSelect(VOICE_CACHE.get(model));
            return;
        }
        setVoiceSelectState(KEYS.modelsLoadingVoices, true);
        try {
            const response = await apiFetch(
                `/v1/audio/voices?model=${encodeURIComponent(model)}`,
                activationContext ? {signal: activationContext.signal} : undefined,
            );
            if (isStale()) return;
            const payload = await response.json().catch(() => ({}));
            if (isStale()) return;
            if (!response.ok) {
                const detail = payload && typeof payload === "object" ? (payload.detail || JSON.stringify(payload)) : "";
                throw new Error(detail || `status ${response.status}`);
            }
            const voices = Array.isArray(payload.data) ? payload.data : [];
            VOICE_CACHE.set(model, voices);
            populateVoiceSelect(voices);
            if (voices.length) clearStatus("audio-speech");
            else setStatus("audio-speech", KEYS.voiceCatalogEmpty, false);
        } catch (err) {
            if (isStale()) return;
            setVoiceSelectState(KEYS.modelsVoiceUnavailable, true);
            setStatus("audio-speech", KEYS.voiceLoadFailed, true, {}, err.message || err);
        }
    }

    function wireAudioVoiceCatalog() {
        const modelSelect = document.getElementById("audioSpeechModel");
        if (!modelSelect) return;
        modelSelect.addEventListener("change", () => {
            refreshAudioVoiceSelect();
        });
    }

    function renderStatusElement(el) {
        const key = el.dataset.statusKey;
        if (!key) return;
        let values = {};
        try {
            values = JSON.parse(el.dataset.statusValues || "{}");
        } catch (_error) {
            values = {};
        }
        const summary = el.querySelector(".playground-status-summary");
        if (summary) summary.textContent = translate(key, values);
    }

    function setStatus(kind, key, isError, values = {}, detail = null) {
        const el = document.querySelector(`[data-status-for="${kind}"]`);
        if (!el) return;
        el.dataset.statusKey = key;
        el.dataset.statusValues = JSON.stringify(values);
        let summary = el.querySelector(".playground-status-summary");
        if (!summary) {
            summary = document.createElement("span");
            summary.className = "playground-status-summary";
            el.appendChild(summary);
        }
        let rawDetail = el.querySelector(".playground-status-detail");
        if (detail != null && String(detail)) {
            if (!rawDetail) {
                rawDetail = document.createElement("span");
                rawDetail.className = "playground-status-detail";
                rawDetail.lang = "und";
                rawDetail.dir = "auto";
                el.append(" — ", rawDetail);
            }
            rawDetail.textContent = String(detail);
        } else if (rawDetail) {
            rawDetail.previousSibling?.remove();
            rawDetail.remove();
        }
        renderStatusElement(el);
        el.classList.toggle("error", Boolean(isError));
    }

    function clearStatus(kind) {
        const el = document.querySelector(`[data-status-for="${kind}"]`);
        if (!el) return;
        delete el.dataset.statusKey;
        delete el.dataset.statusValues;
        el.textContent = "";
        el.classList.remove("error");
    }

    function rerenderStatuses() {
        document.querySelectorAll("[data-status-key]").forEach(renderStatusElement);
    }

    function updateModelOptionTranslations() {
        document.querySelectorAll("option[data-playground-option-key]").forEach((option) => {
            option.textContent = translate(option.dataset.playgroundOptionKey);
        });
        document.querySelectorAll("option[data-playground-model-label]").forEach((option) => {
            const name = option.dataset.playgroundModelLabel;
            option.textContent = option.dataset.fusion === "true"
                ? `${name} · ${translate(KEYS.modelsFusionSuffix)}`
                : name;
        });
    }

    function setResult(kind, html) {
        const el = document.querySelector(`[data-result-for="${kind}"]`);
        if (!el) return;
        el.innerHTML = html;
        el.hidden = false;
    }

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function safeHttpUrl(value) {
        const raw = String(value == null ? "" : value).trim();
        if (!raw) return "";
        try {
            const parsed = new URL(raw, window.location.href);
            if (parsed.protocol === "http:" || parsed.protocol === "https:") {
                return parsed.href;
            }
        } catch (_err) {
            return "";
        }
        return "";
    }

    function safeImageDataUrl(value) {
        const raw = String(value == null ? "" : value).trim();
        if (!raw) return "";
        const compact = raw.replace(/\s+/g, "");
        if (/^data:image\/(?:png|jpeg|jpg|webp|gif);base64,[A-Za-z0-9+/]+={0,2}$/i.test(compact)) {
            return compact;
        }
        return "";
    }

    function safeImageUrl(value) {
        return safeImageDataUrl(value) || safeHttpUrl(value);
    }

    function renderExternalLink(url, label) {
        const text = escapeHtml(label || url || "");
        const safeUrl = safeHttpUrl(url);
        if (!safeUrl) return `<span>${text}</span>`;
        return `<a href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${text}</a>`;
    }

    function revokeBlobUrl(key) {
        if (!BLOB_URLS.has(key)) return;
        URL.revokeObjectURL(BLOB_URLS.get(key));
        BLOB_URLS.delete(key);
    }

    function createDownloadUrl(key, content, contentType) {
        revokeBlobUrl(key);
        const blob = content instanceof Blob ? content : new Blob([content || ""], {type: contentType || "text/plain"});
        const url = URL.createObjectURL(blob);
        BLOB_URLS.set(key, url);
        return url;
    }

    function renderDownloadLink(url, fileName, labelKey = "results.download", labelValues = {}) {
        if (!url) return "";
        const linkLabel = translatedSpan(labelKey, labelValues);
        return `<a class="download-link" href="${escapeHtml(url)}" download="${escapeHtml(fileName)}">${linkLabel}</a>`;
    }

    function extensionFromContentType(contentType, fallback) {
        const value = String(contentType || "").toLowerCase();
        if (value.includes("mpeg")) return "mp3";
        if (value.includes("wav")) return "wav";
        if (value.includes("opus")) return "opus";
        if (value.includes("flac")) return "flac";
        if (value.includes("aac")) return "aac";
        if (value.includes("json")) return "json";
        if (value.includes("html")) return "html";
        if (value.includes("markdown")) return "md";
        if (value.includes("vtt")) return "vtt";
        if (value.includes("srt")) return "srt";
        return fallback || "txt";
    }

    function renderMarkdown(text) {
        if (!text) return "";
        if (typeof window.marked === "undefined" || typeof window.DOMPurify === "undefined") {
            return `<pre class="raw-json">${escapeHtml(text)}</pre>`;
        }
        const html = window.marked.parse(String(text), {breaks: true, gfm: true});
        return window.DOMPurify.sanitize(html, {ADD_ATTR: ["target"]});
    }

    function renderUsage(usage) {
        if (!usage) return "";
        const parts = [];
        if (usage.prompt_tokens != null) {
            parts.push(`${translatedSpan("results.usagePrompt")}: ${runtimeNumberSpan(usage.prompt_tokens)}`);
        }
        if (usage.completion_tokens != null) {
            parts.push(`${translatedSpan("results.usageCompletion")}: ${runtimeNumberSpan(usage.completion_tokens)}`);
        }
        if (usage.total_tokens != null) {
            parts.push(`${translatedSpan("results.usageTotal")}: ${runtimeNumberSpan(usage.total_tokens)}`);
        }
        if (usage.cost != null) {
            parts.push(`${translatedSpan("results.usageCost")}: ${runtimeNumberSpan(usage.cost, "currency")}`);
        }
        if (usage.credits != null) {
            parts.push(`${translatedSpan("results.usageCredits")}: ${runtimeNumberSpan(usage.credits)}`);
        }
        if (parts.length === 0) return "";
        return `<div class="result-meta"><span>${translatedSpan("results.usage")}: ${parts.join(" · ")}</span></div>`;
    }

    function renderRawDetails(payload) {
        return `<details class="raw-details"><summary>${translatedSpan("results.rawJson")}</summary><pre class="raw-json">${escapeHtml(
            JSON.stringify(payload, null, 2)
        )}</pre></details>`;
    }

    function trimSimpleChatMessages(messages) {
        let trimmed = messages.slice(-CHAT_CONTEXT_LIMIT);
        while (trimmed.length > 0 && trimmed[0].role === "assistant") {
            trimmed = trimmed.slice(1);
        }
        return trimmed;
    }

    function renderSimpleChatTranscript() {
        const transcript = document.getElementById("simpleChatTranscript");
        if (!transcript) return;
        transcript.hidden = simpleChatMessages.length === 0;
        transcript.innerHTML = simpleChatMessages.map((message) => {
            const role = message.role === "assistant" ? "assistant" : "user";
            const content = role === "assistant"
                ? renderMarkdown(message.content || "")
                : `<p>${escapeHtml(message.content || "")}</p>`;
            const fusionBlock = role === "assistant" && message.fusion
                ? renderFusionBlock(message.fusion)
                : "";
            return `
                <article class="chat-message ${role}">
                    <div class="chat-role">${translatedSpan(role === "assistant" ? "results.assistant" : "results.user")}</div>
                    <div class="chat-content">${content}</div>
                    ${fusionBlock}
                </article>
            `;
        }).join("");
        transcript.scrollTop = transcript.scrollHeight;
    }

    function renderFusionAttribution(member) {
        if (!member || typeof member !== "object") return "";
        const provider = member.provider || "?";
        const model = member.model || "?";
        return `${escapeHtml(String(provider))}/${escapeHtml(String(model))}`;
    }

    function renderFusionList(titleKey, values) {
        if (!Array.isArray(values) || values.length === 0) return "";
        const items = values.map((value) => `<li>${escapeHtml(String(value))}</li>`).join("");
        return `<div class="fusion-analysis-section"><h4>${translatedSpan(titleKey)}</h4><ul>${items}</ul></div>`;
    }

    function renderFusionAnalysis(analysis) {
        if (!analysis || typeof analysis !== "object") return "";
        if (typeof analysis.raw === "string") {
            return `<div class="fusion-analysis-section"><h4>${translatedSpan("results.analysisRaw")}</h4><pre class="raw-json">${escapeHtml(analysis.raw)}</pre></div>`;
        }
        if (typeof analysis.error === "string") {
            return `<div class="fusion-analysis-section"><h4>${translatedSpan("results.analysisUnavailable")}</h4><p>${escapeHtml(analysis.error)}</p></div>`;
        }
        const insights = Array.isArray(analysis.per_model_insights)
            ? analysis.per_model_insights
                .map((item) => (item && typeof item === "object"
                    ? `${escapeHtml(String(item.model || "?"))}: ${escapeHtml(String(item.insight || ""))}`
                    : ""))
                .filter(Boolean)
            : [];
        return [
            renderFusionList("results.agreements", analysis.agreements),
            renderFusionList("results.disputes", analysis.disputes),
            renderFusionList("results.perModelInsights", insights),
            renderFusionList("results.blindSpots", analysis.blind_spots),
        ].join("");
    }

    function renderFusionPanel(panel) {
        if (!Array.isArray(panel) || panel.length === 0) return "";
        const rows = panel.map((member, index) => {
            if (!member || typeof member !== "object") return "";
            const label = `${translatedSpan("results.modelMember", {number: index + 1})} (${renderFusionAttribution(member)})`;
            let body;
            if (typeof member.error === "string") {
                body = `<p class="fusion-panel-error">${translatedSpan("results.error")}: ${escapeHtml(member.error)}</p>`;
            } else if (typeof member.content === "string") {
                body = `<div class="fusion-panel-content">${renderMarkdown(member.content)}</div>`;
            } else {
                body = `<p class="fusion-panel-content muted">${translatedSpan("results.answerHidden")}</p>`;
            }
            return `<details class="fusion-panel-item"><summary>${label}</summary>${body}</details>`;
        }).join("");
        return `<div class="fusion-panel-section"><h4>${translatedSpan("results.panelAnswers")}</h4>${rows}</div>`;
    }

    function renderFusionBlock(fusion) {
        if (!fusion || typeof fusion !== "object") return "";
        const attribution = `${translatedSpan("results.main")}: ${renderFusionAttribution(fusion.main)} · ${translatedSpan("results.judge")}: ${renderFusionAttribution(fusion.judge)}`;
        const analysis = renderFusionAnalysis(fusion.analysis);
        const panel = renderFusionPanel(fusion.panel);
        return `
            <details class="fusion-block">
                <summary>${translatedSpan("results.fusionSummary")}</summary>
                <div class="fusion-meta">${attribution}</div>
                ${analysis}
                ${panel}
            </details>
        `;
    }

    function extractAssistantMessage(payload) {
        const choice = payload && Array.isArray(payload.choices) ? payload.choices[0] : null;
        const message = choice && choice.message && typeof choice.message === "object" ? choice.message : null;
        const content = message ? message.content : "";
        if (typeof content === "string") return content;
        if (Array.isArray(content)) {
            return content
                .map((part) => {
                    if (typeof part === "string") return part;
                    if (part && typeof part === "object") return part.text || part.content || "";
                    return "";
                })
                .filter(Boolean)
                .join("\n");
        }
        return message ? JSON.stringify(message, null, 2) : JSON.stringify(payload || {}, null, 2);
    }

    function renderImagesSummary(images) {
        if (!Array.isArray(images) || images.length === 0) return "";
        const links = images
            .map((img) => (typeof img === "string" ? img : img && img.url))
            .filter(Boolean);
        if (links.length === 0) return "";
        return `<div class="images-summary">${translatedSpan("results.images")}: ${links
            .map((url) => renderExternalLink(url, url))
            .join(" ")}</div>`;
    }

    function renderSearchResult(payload) {
        const hits = Array.isArray(payload.data) ? payload.data : [];
        const meta = `<div class="result-meta"><span>${translatedSpan("results.model")}: <code>${escapeHtml(payload.model)}</code></span>` +
            `<span>${translatedSpan("results.resultCount", {count: hits.length})}</span></div>`;
        const items = hits.length === 0
            ? `<p><em>${translatedSpan("results.noResults")}</em></p>`
            : hits.map((h) => `
                <div class="search-hit">
                    ${renderExternalLink(h.url, h.title || h.url)}
                    <div class="snippet">${escapeHtml(h.snippet || "")}</div>
                    ${renderImagesSummary(h.images)}
                    ${h.raw_content ? `<details class="raw-content-details"><summary>${translatedSpan("results.rawContent")}</summary><div class="markdown-render">${renderMarkdown(h.raw_content)}</div></details>` : ""}
                </div>
            `).join("");
        return `<h3>${translatedSpan("results.searchTitle")}</h3>${meta}${items}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function renderReadResult(payload) {
        const meta = `<div class="result-meta"><span>${translatedSpan("results.model")}: <code>${escapeHtml(payload.model)}</code></span>` +
            `<span>${translatedSpan("results.url")}: ${renderExternalLink(payload.url, payload.url)}</span></div>`;
        const title = payload.title ? `<h4>${escapeHtml(payload.title)}</h4>` : "";
        const body = `<div class="markdown-render">${renderMarkdown(payload.content || "")}</div>`;
        return `<h3>${translatedSpan("results.fetchedArticle")}</h3>${meta}${title}${renderImagesSummary(payload.images)}${body}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function renderTavilySearchResult(payload) {
        const hits = Array.isArray(payload.results) ? payload.results : [];
        const meta = `<div class="result-meta"><span>${translatedSpan("results.resultCount", {count: hits.length})}</span>` +
            `<span>${translatedSpan("results.responseTime")}: ${runtimeNumberSpan(payload.response_time)} ${translatedSpan("results.secondsUnit")}</span></div>`;
        const topImages = renderImagesSummary(payload.images);
        const items = hits.length === 0
            ? `<p><em>${translatedSpan("results.noResults")}</em></p>`
            : hits.map((h) => `
                <div class="search-hit">
                    ${renderExternalLink(h.url, h.title || h.url)}
                    <div class="snippet">${escapeHtml(h.content || "")}</div>
                    <div class="result-meta"><span>${translatedSpan("results.score")}: ${runtimeNumberSpan(h.score)}</span></div>
                    ${renderImagesSummary(h.images)}
                    ${h.raw_content ? `<details class="raw-content-details"><summary>${translatedSpan("results.rawContent")}</summary><div class="markdown-render">${renderMarkdown(h.raw_content)}</div></details>` : ""}
                </div>
            `).join("");
        return `<h3>${translatedSpan("results.tavilySearchTitle")}</h3>${meta}${topImages}${items}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function renderTavilyExtractResult(payload) {
        const results = Array.isArray(payload.results) ? payload.results : [];
        const failed = Array.isArray(payload.failed_results) ? payload.failed_results : [];
        const meta = `<div class="result-meta"><span>${translatedSpan("results.resultCount", {count: results.length})}</span>` +
            `<span>${translatedSpan("results.failedCount", {count: failed.length})}</span>` +
            `<span>${translatedSpan("results.responseTime")}: ${runtimeNumberSpan(payload.response_time)} ${translatedSpan("results.secondsUnit")}</span></div>`;
        const items = results.length === 0
            ? `<p><em>${translatedSpan("results.noExtractedPages")}</em></p>`
            : results.map((item) => `
                <div class="search-hit">
                    ${renderExternalLink(item.url, item.url)}
                    ${renderImagesSummary(item.images)}
                    <div class="markdown-render">${renderMarkdown(item.raw_content || "")}</div>
                </div>
            `).join("");
        const failures = failed.length
            ? `<h4>${translatedSpan("results.failedUrls")}</h4><ul class="sources-list">${failed
                .map((item) => `<li>${escapeHtml(item.url)} — ${escapeHtml(item.error || "")}</li>`)
                .join("")}</ul>`
            : "";
        return `<h3>${translatedSpan("results.tavilyExtractTitle")}</h3>${meta}${items}${failures}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function renderResearchResult(payload) {
        const meta = `<div class="result-meta"><span>${translatedSpan("results.model")}: <code>${escapeHtml(payload.model)}</code></span>` +
            `<span>${translatedSpan("results.sourcesCount", {count: (payload.sources || []).length})}</span>` +
            `<span>${translatedSpan("results.articlesCount", {count: (payload.articles || []).length})}</span></div>`;
        const output = `<div class="markdown-render">${renderMarkdown(payload.output || "")}</div>`;
        const sources = (payload.sources || []).length
            ? `<h4>${translatedSpan("results.sources")}</h4><ul class="sources-list">${payload.sources
                .map((src) => {
                    const url = typeof src === "string" ? src : (src.url || "");
                    const title = typeof src === "string" ? src : (src.title || src.url || "");
                    return `<li>${renderExternalLink(url, title)}</li>`;
                })
                .join("")}</ul>`
            : "";
        return `<h3>${translatedSpan("results.researchTitle")}</h3>${meta}${output}${sources}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function renderImageGallery(images) {
        if (!images || images.length === 0) return "";
        const cards = images.map((img) => ({...img, safeUrl: safeHttpUrl(img.url)}))
            .filter((img) => img.safeUrl)
            .map((img) => `
            <figure class="image-card">
                <img src="${escapeHtml(img.safeUrl)}" alt="${escapeHtml(img.alt_text || img.prompt || "")}" loading="lazy">
                <figcaption>${escapeHtml(img.prompt || img.alt_text || "")}</figcaption>
            </figure>
        `).join("");
        if (!cards) return "";
        return `<h4>${translatedSpan("results.generatedIllustrations", {count: images.length})}</h4><div class="images-gallery">${cards}</div>`;
    }

    function renderDeepResearchResult(payload) {
        const images = Array.isArray(payload.images) ? payload.images : [];
        const meta = `<div class="result-meta"><span>${translatedSpan("results.model")}: <code>${escapeHtml(payload.model)}</code></span>` +
            `<span>${translatedSpan("results.sourcesCount", {count: (payload.source_urls || []).length})}</span>` +
            `<span>${translatedSpan("results.imagesCount", {count: images.length})}</span></div>`;
        const output = `<div class="markdown-render">${renderMarkdown(payload.output || "")}</div>`;
        const gallery = renderImageGallery(images);
        const sources = (payload.source_urls || []).length
            ? `<h4>${translatedSpan("results.sourceUrls")}</h4><ul class="sources-list">${payload.source_urls
                .map((url) => `<li>${renderExternalLink(url, url)}</li>`)
                .join("")}</ul>`
            : "";
        return `<h3>${translatedSpan("results.deepResearchTitle")}</h3>${meta}${output}${gallery}${sources}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function dataImageUrl(item) {
        if (!item || typeof item !== "object") return "";
        if (item.url) return safeImageUrl(item.url);
        if (item.b64_json) return safeImageDataUrl(`data:image/png;base64,${String(item.b64_json)}`);
        return "";
    }

    function renderImageOperationResult(payload, titleKey) {
        const items = Array.isArray(payload.data) ? payload.data : [];
        const cards = items.map((item, index) => {
            const src = dataImageUrl(item);
            if (!src) return "";
            const caption = item.revised_prompt || item.prompt || "";
            const fileName = `${titleKey.includes("edited") ? "edited" : "generated"}-image-${index + 1}.png`;
            return `
                <figure class="image-card">
                    <img src="${escapeHtml(src)}" alt="${escapeHtml(caption)}" loading="lazy">
                    ${caption ? `<figcaption>${escapeHtml(caption)}</figcaption>` : ""}
                    ${renderDownloadLink(src, fileName, "results.downloadImage")}
                </figure>
            `;
        }).filter(Boolean).join("");
        const gallery = cards ? `<div class="images-gallery">${cards}</div>` : `<p><em>${translatedSpan("results.noImagePayloads")}</em></p>`;
        return `<h3>${translatedSpan(titleKey)}</h3>${gallery}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function renderAudioSpeechResult(blobUrl, contentType, sizeBytes, fileName) {
        return `<h3>${translatedSpan("results.generatedSpeechTitle")}</h3>` +
            `<div class="result-meta"><span>${translatedSpan("results.contentType")}: <code>${escapeHtml(contentType || "audio/mpeg")}</code></span>` +
            `<span>${translatedSpan("results.bytes", {count: Number(sizeBytes || 0)})}</span></div>` +
            `<div class="result-actions">${renderDownloadLink(blobUrl, fileName || "speech.mp3", "results.downloadAudio")}</div>` +
            `<audio class="audio-player" controls src="${escapeHtml(blobUrl)}"></audio>`;
    }

    function renderPlainOrJsonResult(payload, titleKey, download) {
        const actions = download ? `<div class="result-actions">${renderDownloadLink(download.url, download.fileName, download.labelKey)}</div>` : "";
        if (payload && typeof payload === "object" && !payload.__plainText) {
            const text = payload.text || payload.output || payload.content || "";
            const body = text ? `<div class="markdown-render">${renderMarkdown(text)}</div>` : "";
            return `<h3>${translatedSpan(titleKey)}</h3>${actions}${body}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
        }
        return `<h3>${translatedSpan(titleKey)}</h3>${actions}<pre class="raw-json">${escapeHtml(payload && payload.text ? payload.text : "")}</pre>`;
    }

    function formatDuration(seconds) {
        if (seconds == null || !Number.isFinite(Number(seconds))) return "";
        const total = Math.max(0, Math.round(Number(seconds)));
        const minutes = Math.floor(total / 60);
        const rest = total % 60;
        if (minutes <= 0) return translate("results.durationSeconds", {seconds: rest});
        return translate("results.durationMinutes", {minutes, seconds: rest});
    }

    function normalizePercent(value) {
        const numberValue = Number(value || 0);
        if (!Number.isFinite(numberValue)) return 0;
        return Math.max(0, Math.min(100, numberValue));
    }

    function renderPdfDownloadLinks(job, model) {
        const downloads = Array.isArray(job && job.downloads) ? job.downloads : [];
        if (!job || !job.id || downloads.length === 0) return "";
        const links = downloads
            .map((item) => {
                if (!item || !item.artifact) return "";
                const artifact = encodeURIComponent(String(item.artifact));
                const href = `/v1/pdf/jobs/${encodeURIComponent(String(job.id))}/download/${artifact}?model=${encodeURIComponent(model)}`;
                const fileName = item.filename || `${item.artifact}`;
                const labelKey = item.label ? "results.downloadNamedArtifact" : "results.downloadArtifact";
                return renderDownloadLink(href, fileName, labelKey, {name: item.label || ""});
            })
            .filter(Boolean)
            .join("");
        return links ? `<div class="result-actions">${links}</div>` : "";
    }

    function renderPdfProgressEvents(job) {
        const events = Array.isArray(job && job.progress) ? job.progress.slice(-14) : [];
        if (events.length === 0) return "";
        const lines = events.map((item) => {
            const pieces = [item.stage || "progress"];
            if (item.message) pieces.push(item.message);
            if (item.detail) pieces.push(item.detail);
            if (item.current != null && item.total != null) pieces.push(`${item.current}/${item.total}`);
            if (item.percent != null) pieces.push(`${Math.round(normalizePercent(item.percent))}%`);
            return pieces.join(" · ");
        });
        return `<pre class="raw-json progress-events">${escapeHtml(lines.join("\n"))}</pre>`;
    }

    function renderPdfResultPreview(result) {
        if (!result || typeof result !== "object") return "";
        const sections = [];
        if (result.markdown) {
            sections.push(
                `<details class="raw-content-details"><summary>${translatedSpan("results.markdownPreview")}</summary><div class="markdown-render">${renderMarkdown(
                    String(result.markdown).slice(0, 12000)
                )}</div></details>`
            );
        }
        if (result.ocr_preview) {
            sections.push(
                `<details class="raw-content-details"><summary>${translatedSpan("results.ocrPreview")}</summary><pre class="raw-json">${escapeHtml(
                    String(result.ocr_preview)
                )}</pre></details>`
            );
        }
        if (result.mathpix_preview) {
            sections.push(
                `<details class="raw-content-details"><summary>${translatedSpan("results.mathOcrPreview")}</summary><pre class="raw-json">${escapeHtml(
                    typeof result.mathpix_preview === "string"
                        ? result.mathpix_preview
                        : JSON.stringify(result.mathpix_preview, null, 2)
                )}</pre></details>`
            );
        }
        return sections.join("");
    }

    function redactPdfBinaryFields(value) {
        if (!value || typeof value !== "object") return value;
        if (Array.isArray(value)) {
            return value.map(redactPdfBinaryFields);
        }
        const redacted = {};
        Object.entries(value).forEach(([key, item]) => {
            if ((key === "docx_base64" || key === "preprocessed_pdf_base64") && typeof item === "string") {
                redacted[key] = `[base64 omitted, ${item.length} chars]`;
                return;
            }
            redacted[key] = redactPdfBinaryFields(item);
        });
        return redacted;
    }

    function renderPdfJob(job, model, result) {
        const percent = normalizePercent(job && job.percent);
        const elapsed = durationSpan(job && job.elapsed_seconds);
        const eta = durationSpan(job && job.eta_seconds);
        const metaItems = [
            `${translatedSpan("results.status")}: <code>${escapeHtml((job && job.status) || "queued")}</code>`,
            `${translatedSpan("results.stage")}: <code>${escapeHtml((job && job.stage) || "")}</code>`,
            translatedSpan("results.progress", {percent: Math.round(percent)}),
        ];
        if (job && job.current != null && job.total != null) metaItems.push(`${translatedSpan("results.pages")}: ${runtimeNumberSpan(job.current)}/${runtimeNumberSpan(job.total)}`);
        if (elapsed) metaItems.push(`${translatedSpan("results.elapsed")}: ${elapsed}`);
        if (eta) metaItems.push(`${translatedSpan("results.eta")}: ${eta}`);
        const meta = `<div class="result-meta">${metaItems.map((item) => `<span>${item}</span>`).join("")}</div>`;
        const message = job && job.message ? `<p>${escapeHtml(job.message)}</p>` : "";
        const meter = `<div class="progress-meter" aria-label="${escapeHtml(translate("results.pdfProgressAria"))}" data-playground-i18n-aria="results.pdfProgressAria"><span style="width: ${percent}%"></span></div>`;
        const downloads = renderPdfDownloadLinks(job, model);
        const preview = renderPdfResultPreview(result);
        return `<h3>${translatedSpan("results.pdfTitle")}</h3>${downloads}${meta}${meter}${message}${renderPdfProgressEvents(job)}${preview}${renderRawDetails({
            job: redactPdfBinaryFields(job),
            result: redactPdfBinaryFields(result),
        })}`;
    }

    function readFormData(form) {
        const data = {};
        Array.from(form.elements).forEach((el) => {
            if (!el.name) return;
            if (el.type === "file") return;
            if (el.type === "checkbox") {
                data[el.name] = el.checked;
                return;
            }
            if (el.dataset.array === "lines") {
                const values = el.value
                    .split(/[\n,]+/)
                    .map((value) => value.trim())
                    .filter(Boolean);
                if (values.length > 0) data[el.name] = values;
                return;
            }
            if (el.type === "number") {
                if (el.value === "") return;
                data[el.name] = Number(el.value);
                return;
            }
            const value = el.value.trim();
            if (value === "") return;
            data[el.name] = value;
        });
        return data;
    }

    function createLocalizedValidationError(statusKey, statusValues) {
        const error = new Error();
        error.statusKey = statusKey;
        error.statusValues = statusValues;
        return error;
    }

    function readMultipartFormData(form) {
        const formData = new FormData();
        for (const el of Array.from(form.elements)) {
            if (!el.name) continue;
            if (el.type === "file") {
                const files = Array.from(el.files || []);
                const maxFiles = Number(el.dataset.maxFiles || 0);
                if (maxFiles > 0 && files.length > maxFiles) {
                    throw createLocalizedValidationError(KEYS.tooManyFiles, {
                        count: maxFiles,
                        field: el.name,
                    });
                }
                files.forEach((file) => formData.append(el.name, file, file.name));
                continue;
            }
            if (el.type === "checkbox") {
                if (el.checked) formData.append(el.name, "true");
                continue;
            }
            if (el.type === "number") {
                if (el.value !== "") formData.append(el.name, el.value);
                continue;
            }
            const value = el.value.trim();
            if (value !== "") formData.append(el.name, value);
        }
        return formData;
    }

    function selectedFormValue(form, name) {
        const control = form.elements[name];
        if (!control) return "";
        return String(control.value || "").trim();
    }

    function payloadDownloadText(payload, rawText) {
        if (payload && typeof payload === "object" && payload.__plainText) {
            return payload.text || "";
        }
        if (payload && typeof payload === "object") {
            return JSON.stringify(payload, null, 2);
        }
        return rawText || "";
    }

    function buildMultipartDownload(kind, form, payload, rawText, contentType) {
        if (kind === "audio-transcription") {
            const requestedFormat = selectedFormValue(form, "response_format") || extensionFromContentType(contentType, "txt");
            const extension = requestedFormat === "text" ? "txt" : requestedFormat;
            return {
                url: createDownloadUrl(kind, payloadDownloadText(payload, rawText), contentType || "text/plain"),
                fileName: `transcription.${extension}`,
                labelKey: "results.downloadTranscription",
            };
        }
        return null;
    }

    async function submitRequest(kind, url, form, renderer) {
        const body = readFormData(form);
        if (!body.model) {
            setStatus(kind, KEYS.selectModelFirst, true);
            return;
        }
        const button = form.querySelector(".run-button");
        button.disabled = true;
        setStatus(kind, KEYS.running, false);
        const startedAt = performance.now();
        try {
            const response = await apiFetch(url, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(body),
            });
            const text = await response.text();
            let payload;
            try {
                payload = text ? JSON.parse(text) : {};
            } catch (parseErr) {
                payload = {detail: text || parseErr.message};
            }
            if (!response.ok) {
                const detail = typeof payload === "object" && payload ? (payload.detail || JSON.stringify(payload)) : String(payload);
                setStatus(kind, KEYS.error, true, {status: response.status}, detail);
                setResult(kind, `<pre class="raw-json">${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`);
                return;
            }
            const durationMs = Math.round(performance.now() - startedAt);
            setStatus(kind, KEYS.done, false, {seconds: durationMs / 1000});
            setResult(kind, renderer(payload));
        } catch (err) {
            setStatus(kind, KEYS.requestFailed, true, {}, err.message || err);
        } finally {
            button.disabled = false;
        }
    }

    async function submitSimpleChatRequest(form) {
        const model = selectedFormValue(form, "model");
        const input = form.elements.message;
        const content = input ? String(input.value || "").trim() : "";
        if (!model) {
            setStatus("chat", KEYS.selectModelFirst, true);
            return;
        }
        if (!content) {
            setStatus("chat", KEYS.enterMessage, true);
            return;
        }

        const requestMessages = trimSimpleChatMessages([
            ...simpleChatMessages,
            {role: "user", content},
        ]);
        const button = form.querySelector(".run-button");
        button.disabled = true;
        setStatus("chat", KEYS.running, false);
        const startedAt = performance.now();
        try {
            // Only standard {role, content} fields go upstream; any locally
            // attached Fusion render data is stripped here.
            const requestBody = {
                model,
                messages: requestMessages.map((m) => ({role: m.role, content: m.content})),
                stream: false,
            };
            if (fusionModelNames.has(model)) {
                const detailsCheckbox = document.getElementById("chatFusionDetails");
                requestBody.fusion = {include_details: detailsCheckbox ? detailsCheckbox.checked : true};
            }
            const response = await apiFetch("/v1/chat/completions", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(requestBody),
            });
            const text = await response.text();
            let payload;
            try {
                payload = text ? JSON.parse(text) : {};
            } catch (parseErr) {
                payload = {detail: text || parseErr.message};
            }
            if (!response.ok) {
                const detail = typeof payload === "object" && payload ? (payload.detail || JSON.stringify(payload)) : String(payload);
                setStatus("chat", KEYS.error, true, {status: response.status}, detail);
                return;
            }

            const assistantContent = extractAssistantMessage(payload);
            const assistantMessage = {role: "assistant", content: assistantContent};
            if (payload && payload.fusion && typeof payload.fusion === "object") {
                assistantMessage.fusion = payload.fusion;
            }
            simpleChatMessages = trimSimpleChatMessages([
                ...requestMessages,
                assistantMessage,
            ]);
            if (input) input.value = "";
            renderSimpleChatTranscript();
            const durationMs = Math.round(performance.now() - startedAt);
            setStatus("chat", KEYS.chatDone, false, {
                seconds: durationMs / 1000,
                count: simpleChatMessages.length,
                limit: CHAT_CONTEXT_LIMIT,
            });
        } catch (err) {
            setStatus("chat", KEYS.requestFailed, true, {}, err.message || err);
        } finally {
            button.disabled = false;
        }
    }

    async function submitMultipartRequest(kind, url, form, renderer) {
        const button = form.querySelector(".run-button");
        let body;
        try {
            body = readMultipartFormData(form);
        } catch (error) {
            setStatus(
                kind,
                error.statusKey || KEYS.requestFailed,
                true,
                error.statusValues || {},
                error.statusKey ? null : (error.message || String(error)),
            );
            return;
        }
        if (!body.get("model")) {
            setStatus(kind, KEYS.selectModelFirst, true);
            return;
        }
        button.disabled = true;
        setStatus(kind, KEYS.running, false);
        const startedAt = performance.now();
        try {
            const response = await apiFetch(url, {
                method: "POST",
                body,
            });
            const contentType = response.headers.get("content-type") || "";
            const text = await response.text();
            let payload;
            if (contentType.includes("application/json")) {
                try {
                    payload = text ? JSON.parse(text) : {};
                } catch (parseErr) {
                    payload = {detail: text || parseErr.message};
                }
            } else {
                payload = {__plainText: true, text};
            }
            if (!response.ok) {
                const detail = payload && typeof payload === "object" ? (payload.detail || payload.text || JSON.stringify(payload)) : String(payload);
                setStatus(kind, KEYS.error, true, {status: response.status}, detail);
                setResult(kind, `<pre class="raw-json">${escapeHtml(typeof payload === "string" ? payload : JSON.stringify(payload, null, 2))}</pre>`);
                return;
            }
            const durationMs = Math.round(performance.now() - startedAt);
            setStatus(kind, KEYS.done, false, {seconds: durationMs / 1000});
            const download = buildMultipartDownload(kind, form, payload, text, contentType);
            setResult(kind, renderer(payload, download));
        } catch (err) {
            setStatus(kind, KEYS.requestFailed, true, {}, err.message || err);
        } finally {
            button.disabled = false;
        }
    }

    async function readJsonResponse(response) {
        const text = await response.text();
        if (!text) return {};
        try {
            return JSON.parse(text);
        } catch (err) {
            throw new Error(`Invalid JSON response: ${err.message || err}`);
        }
    }

    async function fetchPdfJobJson(url, options) {
        const response = await apiFetch(url, options);
        const payload = await readJsonResponse(response);
        if (!response.ok) {
            const detail = payload && typeof payload === "object" ? (payload.detail || JSON.stringify(payload)) : String(payload);
            throw new Error(`Error ${response.status}: ${detail}`);
        }
        return payload;
    }

    async function submitPdfJobRequest(kind, form) {
        const button = form.querySelector(".run-button");
        let body;
        try {
            body = readMultipartFormData(form);
        } catch (error) {
            setStatus(
                kind,
                error.statusKey || KEYS.requestFailed,
                true,
                error.statusValues || {},
                error.statusKey ? null : (error.message || String(error)),
            );
            return;
        }
        const model = String(body.get("model") || "").trim();
        if (!model) {
            setStatus(kind, KEYS.selectModelFirst, true);
            return;
        }

        button.disabled = true;
        setStatus(kind, KEYS.uploadingPdf, false);
        const startedAt = performance.now();
        try {
            let job = await fetchPdfJobJson("/v1/pdf/jobs", {
                method: "POST",
                body,
            });
            setResult(kind, renderPdfJob(job, model));

            while (job.status === "queued" || job.status === "running") {
                await new Promise((resolve) => setTimeout(resolve, 1000));
                job = await fetchPdfJobJson(
                    `/v1/pdf/jobs/${encodeURIComponent(String(job.id))}?model=${encodeURIComponent(model)}`
                );
                setStatus(
                    kind,
                    KEYS.pdfRunning,
                    false,
                    {percent: Math.round(normalizePercent(job.percent))},
                    job.message || job.status || null,
                );
                setResult(kind, renderPdfJob(job, model));
            }

            const durationMs = Math.round(performance.now() - startedAt);
            if (job.status === "failed") {
                const detail = job.error ? JSON.stringify(job.error) : (job.message || "Conversion failed");
                setStatus(
                    kind,
                    KEYS.pdfFailed,
                    true,
                    {seconds: durationMs / 1000},
                    detail,
                );
                setResult(kind, renderPdfJob(job, model));
                return;
            }

            let result = null;
            if (job.result_available) {
                result = await fetchPdfJobJson(
                    `/v1/pdf/jobs/${encodeURIComponent(String(job.id))}/result?model=${encodeURIComponent(model)}`
                );
            }
            const downloadCount = Array.isArray(job.downloads) ? job.downloads.length : 0;
            setStatus(kind, KEYS.pdfDone, downloadCount === 0, {
                seconds: durationMs / 1000,
                count: downloadCount,
            });
            setResult(kind, renderPdfJob(job, model, result));
        } catch (err) {
            setStatus(kind, KEYS.requestFailed, true, {}, err.message || err);
        } finally {
            button.disabled = false;
        }
    }

    async function submitAudioSpeechRequest(kind, url, form) {
        const body = readFormData(form);
        if (!body.model) {
            setStatus(kind, KEYS.selectModelFirst, true);
            return;
        }
        const button = form.querySelector(".run-button");
        button.disabled = true;
        setStatus(kind, KEYS.running, false);
        const startedAt = performance.now();
        try {
            const response = await apiFetch(url, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(body),
            });
            const contentType = response.headers.get("content-type") || "audio/mpeg";
            if (!response.ok) {
                const text = await response.text();
                let payload;
                try {
                    payload = text ? JSON.parse(text) : {};
                } catch (_err) {
                    payload = {detail: text};
                }
                const detail = payload.detail || JSON.stringify(payload);
                setStatus(kind, KEYS.error, true, {status: response.status}, detail);
                setResult(kind, `<pre class="raw-json">${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`);
                return;
            }
            const blob = await response.blob();
            const blobUrl = createDownloadUrl(kind, blob, contentType);
            const extension = body.response_format || extensionFromContentType(contentType, "mp3");
            const fileName = `speech.${extension}`;
            const durationMs = Math.round(performance.now() - startedAt);
            setStatus(kind, KEYS.done, false, {seconds: durationMs / 1000});
            setResult(kind, renderAudioSpeechResult(blobUrl, contentType, blob.size, fileName));
        } catch (err) {
            setStatus(kind, KEYS.requestFailed, true, {}, err.message || err);
        } finally {
            button.disabled = false;
        }
    }

    function wireForm(kind, formId, endpoint, renderer) {
        const form = document.getElementById(formId);
        if (!form) return;
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            submitRequest(kind, endpoint, form, renderer);
        });
    }

    function syncChatFusionOptions() {
        const select = document.getElementById("chatModel");
        const options = document.getElementById("chatFusionOptions");
        if (!select || !options) return;
        options.hidden = !fusionModelNames.has(select.value);
    }

    function wireSimpleChatForm() {
        const form = document.getElementById("simpleChatForm");
        if (!form) return;
        const resetButton = document.getElementById("resetSimpleChatButton");
        const modelSelect = document.getElementById("chatModel");
        if (modelSelect) modelSelect.addEventListener("change", syncChatFusionOptions);
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            submitSimpleChatRequest(form);
        });
        if (resetButton) {
            resetButton.addEventListener("click", () => {
                simpleChatMessages = [];
                const input = form.elements.message;
                if (input) input.value = "";
                renderSimpleChatTranscript();
                setStatus("chat", KEYS.chatReset, false);
            });
        }
    }

    function wireMultipartForm(kind, formId, endpoint, renderer) {
        const form = document.getElementById(formId);
        if (!form) return;
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            submitMultipartRequest(kind, endpoint, form, renderer);
        });
    }

    function wireAudioSpeechForm() {
        const form = document.getElementById("audioSpeechForm");
        if (!form) return;
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            submitAudioSpeechRequest("audio-speech", "/v1/audio/speech", form);
        });
    }

    function wirePdfConversionForm() {
        const form = document.getElementById("pdfConversionForm");
        if (!form) return;
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            submitPdfJobRequest("pdf-conversion", form);
        });
    }

    function captureLocaleUiState() {
        const activeElement = document.activeElement;
        const scrollElements = Array.from(document.querySelectorAll(
            ".playground-result, .chat-transcript"
        ));
        return {
            activeElement,
            selectionStart: typeof activeElement?.selectionStart === "number"
                ? activeElement.selectionStart
                : null,
            selectionEnd: typeof activeElement?.selectionEnd === "number"
                ? activeElement.selectionEnd
                : null,
            windowX: window.scrollX,
            windowY: window.scrollY,
            scrollPositions: scrollElements.map((element) => ({
                element,
                left: element.scrollLeft,
                top: element.scrollTop,
            })),
        };
    }

    function restoreLocaleUiState(state) {
        state.scrollPositions.forEach(({element, left, top}) => {
            if (!element.isConnected) return;
            element.scrollLeft = left;
            element.scrollTop = top;
        });
        window.scrollTo(state.windowX, state.windowY);
        if (state.activeElement?.isConnected) {
            state.activeElement.focus({preventScroll: true});
            if (state.selectionStart !== null && typeof state.activeElement.setSelectionRange === "function") {
                state.activeElement.setSelectionRange(state.selectionStart, state.selectionEnd);
            }
        }
    }

    function rerenderLocale() {
        const uiState = pendingLocaleUiState || captureLocaleUiState();
        pendingLocaleUiState = null;
        updateDynamicTranslations();
        updateRuntimeFormats();
        updateModelOptionTranslations();
        rerenderStatuses();
        restoreLocaleUiState(uiState);
    }

    function captureLocaleChange(event) {
        if (event.target?.id === "localeSelect") {
            pendingLocaleUiState = captureLocaleUiState();
        }
    }

    async function bootstrap() {
        await i18n.ready;
        Theme.attachToggle("darkModeToggle");
        setupPlaygroundTabs();
        wireSimpleChatForm();
        wireForm("search", "searchForm", "/v1/web/search", renderSearchResult);
        wireForm("read", "readForm", "/v1/web/read", renderReadResult);
        wireForm("tavily-search", "tavilySearchForm", "/v1/tavily/search", renderTavilySearchResult);
        wireForm("tavily-extract", "tavilyExtractForm", "/v1/tavily/extract", renderTavilyExtractResult);
        wireForm("research", "researchForm", "/v1/web/research", renderResearchResult);
        wireForm("deep-research", "deepResearchForm", "/v1/web/deep-research", renderDeepResearchResult);
        wireAudioVoiceCatalog();
        wireAudioSpeechForm();
        wirePdfConversionForm();
        wireMultipartForm("audio-transcription", "audioTranscriptionForm", "/v1/audio/transcriptions", (payload, download) => renderPlainOrJsonResult(payload, "results.transcriptionTitle", download));
        wireForm("image-generation", "imageGenerationForm", "/v1/images/generations", (payload) => renderImageOperationResult(payload, "results.generatedImagesTitle"));
        wireMultipartForm("image-edit", "imageEditForm", "/v1/images/edits", (payload) => renderImageOperationResult(payload, "results.editedImagesTitle"));
        document.addEventListener("change", captureLocaleChange, {capture: true});
        unsubscribeLocale = i18n.subscribe(rerenderLocale);
        try {
            const models = await loadModels();
            fusionModelNames = new Set(models.fusion || []);
            MODEL_SELECTS.forEach(({id, section}) => {
                const select = document.getElementById(id);
                if (select) populateSelect(select, models[section] || [], section === "chat" ? fusionModelNames : null);
            });
            syncChatFusionOptions();
        } catch (err) {
            STATUS_KINDS.forEach((kind) => {
                setStatus(kind, KEYS.modelsLoadFailed, true, {}, err.message || err);
            });
        }
    }

    window.addEventListener("beforeunload", () => {
        unsubscribeLocale?.();
        document.removeEventListener("change", captureLocaleChange, {capture: true});
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
