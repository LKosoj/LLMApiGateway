(function () {
    const SECTION_BUTTONS = Array.from(document.querySelectorAll("[data-playground-section-tab]"));
    const SECTION_PANELS = Array.from(document.querySelectorAll("[data-playground-section-panel]"));
    const WEB_TAB_BUTTONS = Array.from(document.querySelectorAll("[data-web-tab]"));
    const WEB_TAB_PANELS = Array.from(document.querySelectorAll("[data-web-panel]"));
    const MODEL_SELECTS = [
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
    const { apiFetch } = window.gatewayAuth;

    function activateSection(name) {
        SECTION_BUTTONS.forEach((btn) => {
            btn.classList.toggle("active", btn.dataset.playgroundSectionTab === name);
        });
        SECTION_PANELS.forEach((panel) => {
            panel.hidden = panel.dataset.playgroundSectionPanel !== name;
        });
    }

    function activateWebTab(name) {
        WEB_TAB_BUTTONS.forEach((btn) => {
            btn.classList.toggle("active", btn.dataset.webTab === name);
        });
        WEB_TAB_PANELS.forEach((panel) => {
            panel.hidden = panel.dataset.webPanel !== name;
        });
    }

    SECTION_BUTTONS.forEach((btn) => {
        btn.addEventListener("click", () => activateSection(btn.dataset.playgroundSectionTab));
    });

    WEB_TAB_BUTTONS.forEach((btn) => {
        btn.addEventListener("click", () => activateWebTab(btn.dataset.webTab));
    });

    async function loadModels() {
        const response = await apiFetch("/v1/ui/playground/models");
        if (!response.ok) {
            throw new Error(`Failed to load model lists (status ${response.status})`);
        }
        return response.json();
    }

    function populateSelect(select, models) {
        select.innerHTML = "";
        if (!models || models.length === 0) {
            const opt = document.createElement("option");
            opt.value = "";
            opt.textContent = "— No models configured —";
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
            opt.textContent = name;
            select.appendChild(opt);
        });
    }

    function setVoiceSelectState(text, disabled) {
        const select = document.getElementById("audioSpeechVoice");
        if (!select) return;
        select.innerHTML = "";
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = text;
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
        defaultOpt.textContent = "provider default";
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

    async function refreshAudioVoiceSelect() {
        const modelSelect = document.getElementById("audioSpeechModel");
        const model = modelSelect && !modelSelect.disabled ? modelSelect.value : "";
        if (!model) {
            setVoiceSelectState("Select a model first", true);
            return;
        }
        if (VOICE_CACHE.has(model)) {
            populateVoiceSelect(VOICE_CACHE.get(model));
            return;
        }
        setVoiceSelectState("Loading voices...", true);
        try {
            const response = await apiFetch(`/v1/audio/voices?model=${encodeURIComponent(model)}`);
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) {
                const detail = payload && typeof payload === "object" ? (payload.detail || JSON.stringify(payload)) : "";
                throw new Error(detail || `status ${response.status}`);
            }
            const voices = Array.isArray(payload.data) ? payload.data : [];
            VOICE_CACHE.set(model, voices);
            populateVoiceSelect(voices);
            setStatus("audio-speech", voices.length ? "" : "Voice catalog is empty; provider default will be used.", false);
        } catch (err) {
            setVoiceSelectState("Voice list unavailable", true);
            setStatus("audio-speech", `Failed to load voices: ${err.message || err}`, true);
        }
    }

    function wireAudioVoiceCatalog() {
        const modelSelect = document.getElementById("audioSpeechModel");
        if (!modelSelect) return;
        modelSelect.addEventListener("change", () => {
            refreshAudioVoiceSelect();
        });
    }

    function setStatus(kind, text, isError) {
        const el = document.querySelector(`[data-status-for="${kind}"]`);
        if (!el) return;
        el.textContent = text || "";
        el.classList.toggle("error", Boolean(isError));
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

    function renderDownloadLink(url, fileName, label) {
        if (!url) return "";
        return `<a class="download-link" href="${escapeHtml(url)}" download="${escapeHtml(fileName)}">${escapeHtml(label || "Download")}</a>`;
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
        if (usage.prompt_tokens != null) parts.push(`prompt=${escapeHtml(usage.prompt_tokens)}`);
        if (usage.completion_tokens != null) parts.push(`completion=${escapeHtml(usage.completion_tokens)}`);
        if (usage.total_tokens != null) parts.push(`total=${escapeHtml(usage.total_tokens)}`);
        if (usage.cost != null) {
            const cost = Number(usage.cost);
            parts.push(Number.isFinite(cost) ? `cost=$${cost.toFixed(6)}` : `cost=${escapeHtml(usage.cost)}`);
        }
        if (usage.credits != null) parts.push(`credits=${escapeHtml(usage.credits)}`);
        if (parts.length === 0) return "";
        return `<div class="result-meta"><span>Usage: ${parts.join(" · ")}</span></div>`;
    }

    function renderRawDetails(payload) {
        return `<details class="raw-details"><summary>Raw JSON response</summary><pre class="raw-json">${escapeHtml(
            JSON.stringify(payload, null, 2)
        )}</pre></details>`;
    }

    function renderImagesSummary(images) {
        if (!Array.isArray(images) || images.length === 0) return "";
        const links = images
            .map((img) => (typeof img === "string" ? img : img && img.url))
            .filter(Boolean);
        if (links.length === 0) return "";
        return `<div class="images-summary">Images: ${links
            .map((url) => renderExternalLink(url, url))
            .join(" ")}</div>`;
    }

    function renderSearchResult(payload) {
        const hits = Array.isArray(payload.data) ? payload.data : [];
        const meta = `<div class="result-meta"><span>Model: <code>${escapeHtml(payload.model)}</code></span>` +
            `<span>Results: ${hits.length}</span></div>`;
        const items = hits.length === 0
            ? "<p><em>No results.</em></p>"
            : hits.map((h) => `
                <div class="search-hit">
                    ${renderExternalLink(h.url, h.title || h.url)}
                    <div class="snippet">${escapeHtml(h.snippet || "")}</div>
                    ${renderImagesSummary(h.images)}
                    ${h.raw_content ? `<details class="raw-content-details"><summary>Raw content</summary><div class="markdown-render">${renderMarkdown(h.raw_content)}</div></details>` : ""}
                </div>
            `).join("");
        return `<h3>Search results</h3>${meta}${items}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function renderReadResult(payload) {
        const meta = `<div class="result-meta"><span>Model: <code>${escapeHtml(payload.model)}</code></span>` +
            `<span>URL: ${renderExternalLink(payload.url, payload.url)}</span></div>`;
        const title = payload.title ? `<h4>${escapeHtml(payload.title)}</h4>` : "";
        const body = `<div class="markdown-render">${renderMarkdown(payload.content || "")}</div>`;
        return `<h3>Fetched article</h3>${meta}${title}${renderImagesSummary(payload.images)}${body}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function renderTavilySearchResult(payload) {
        const hits = Array.isArray(payload.results) ? payload.results : [];
        const meta = `<div class="result-meta"><span>Results: ${hits.length}</span>` +
            `<span>Response time: ${escapeHtml(payload.response_time || "")}s</span></div>`;
        const topImages = renderImagesSummary(payload.images);
        const items = hits.length === 0
            ? "<p><em>No results.</em></p>"
            : hits.map((h) => `
                <div class="search-hit">
                    ${renderExternalLink(h.url, h.title || h.url)}
                    <div class="snippet">${escapeHtml(h.content || "")}</div>
                    <div class="result-meta"><span>Score: ${escapeHtml(h.score || "")}</span></div>
                    ${renderImagesSummary(h.images)}
                    ${h.raw_content ? `<details class="raw-content-details"><summary>Raw content</summary><div class="markdown-render">${renderMarkdown(h.raw_content)}</div></details>` : ""}
                </div>
            `).join("");
        return `<h3>Tavily search results</h3>${meta}${topImages}${items}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function renderTavilyExtractResult(payload) {
        const results = Array.isArray(payload.results) ? payload.results : [];
        const failed = Array.isArray(payload.failed_results) ? payload.failed_results : [];
        const meta = `<div class="result-meta"><span>Results: ${results.length}</span>` +
            `<span>Failed: ${failed.length}</span>` +
            `<span>Response time: ${escapeHtml(payload.response_time || "")}s</span></div>`;
        const items = results.length === 0
            ? "<p><em>No extracted pages.</em></p>"
            : results.map((item) => `
                <div class="search-hit">
                    ${renderExternalLink(item.url, item.url)}
                    ${renderImagesSummary(item.images)}
                    <div class="markdown-render">${renderMarkdown(item.raw_content || "")}</div>
                </div>
            `).join("");
        const failures = failed.length
            ? `<h4>Failed URLs</h4><ul class="sources-list">${failed
                .map((item) => `<li>${escapeHtml(item.url)} — ${escapeHtml(item.error || "")}</li>`)
                .join("")}</ul>`
            : "";
        return `<h3>Tavily extract results</h3>${meta}${items}${failures}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function renderResearchResult(payload) {
        const meta = `<div class="result-meta"><span>Model: <code>${escapeHtml(payload.model)}</code></span>` +
            `<span>Sources: ${(payload.sources || []).length}</span>` +
            `<span>Articles: ${(payload.articles || []).length}</span></div>`;
        const output = `<div class="markdown-render">${renderMarkdown(payload.output || "")}</div>`;
        const sources = (payload.sources || []).length
            ? `<h4>Sources</h4><ul class="sources-list">${payload.sources
                .map((src) => {
                    const url = typeof src === "string" ? src : (src.url || "");
                    const title = typeof src === "string" ? src : (src.title || src.url || "");
                    return `<li>${renderExternalLink(url, title)}</li>`;
                })
                .join("")}</ul>`
            : "";
        return `<h3>Research output</h3>${meta}${output}${sources}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
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
        return `<h4>Generated illustrations (${images.length})</h4><div class="images-gallery">${cards}</div>`;
    }

    function renderDeepResearchResult(payload) {
        const images = Array.isArray(payload.images) ? payload.images : [];
        const meta = `<div class="result-meta"><span>Model: <code>${escapeHtml(payload.model)}</code></span>` +
            `<span>Sources: ${(payload.source_urls || []).length}</span>` +
            `<span>Images: ${images.length}</span></div>`;
        const output = `<div class="markdown-render">${renderMarkdown(payload.output || "")}</div>`;
        const gallery = renderImageGallery(images);
        const sources = (payload.source_urls || []).length
            ? `<h4>Source URLs</h4><ul class="sources-list">${payload.source_urls
                .map((url) => `<li>${renderExternalLink(url, url)}</li>`)
                .join("")}</ul>`
            : "";
        return `<h3>Deep research report</h3>${meta}${output}${gallery}${sources}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function dataImageUrl(item) {
        if (!item || typeof item !== "object") return "";
        if (item.url) return safeImageUrl(item.url);
        if (item.b64_json) return safeImageDataUrl(`data:image/png;base64,${String(item.b64_json)}`);
        return "";
    }

    function renderImageOperationResult(payload, title) {
        const items = Array.isArray(payload.data) ? payload.data : [];
        const cards = items.map((item, index) => {
            const src = dataImageUrl(item);
            if (!src) return "";
            const caption = item.revised_prompt || item.prompt || "";
            const fileName = `${title.toLowerCase().includes("edit") ? "edited" : "generated"}-image-${index + 1}.png`;
            return `
                <figure class="image-card">
                    <img src="${escapeHtml(src)}" alt="${escapeHtml(caption)}" loading="lazy">
                    ${caption ? `<figcaption>${escapeHtml(caption)}</figcaption>` : ""}
                    ${renderDownloadLink(src, fileName, "Download image")}
                </figure>
            `;
        }).filter(Boolean).join("");
        const gallery = cards ? `<div class="images-gallery">${cards}</div>` : "<p><em>No image payloads in response.</em></p>";
        return `<h3>${escapeHtml(title)}</h3>${gallery}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
    }

    function renderAudioSpeechResult(blobUrl, contentType, sizeBytes, fileName) {
        return `<h3>Generated speech</h3>` +
            `<div class="result-meta"><span>Content-Type: <code>${escapeHtml(contentType || "audio/mpeg")}</code></span>` +
            `<span>Bytes: ${Number(sizeBytes || 0).toLocaleString()}</span></div>` +
            `<div class="result-actions">${renderDownloadLink(blobUrl, fileName || "speech.mp3", "Download audio")}</div>` +
            `<audio class="audio-player" controls src="${escapeHtml(blobUrl)}"></audio>`;
    }

    function renderPlainOrJsonResult(payload, title, download) {
        const actions = download ? `<div class="result-actions">${renderDownloadLink(download.url, download.fileName, download.label)}</div>` : "";
        if (payload && typeof payload === "object" && !payload.__plainText) {
            const text = payload.text || payload.output || payload.content || "";
            const body = text ? `<div class="markdown-render">${renderMarkdown(text)}</div>` : "";
            return `<h3>${escapeHtml(title)}</h3>${actions}${body}${renderUsage(payload.usage)}${renderRawDetails(payload)}`;
        }
        return `<h3>${escapeHtml(title)}</h3>${actions}<pre class="raw-json">${escapeHtml(payload && payload.text ? payload.text : "")}</pre>`;
    }

    function formatDuration(seconds) {
        if (seconds == null || !Number.isFinite(Number(seconds))) return "";
        const total = Math.max(0, Math.round(Number(seconds)));
        const minutes = Math.floor(total / 60);
        const rest = total % 60;
        if (minutes <= 0) return `${rest}s`;
        return `${minutes}m ${String(rest).padStart(2, "0")}s`;
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
                const label = item.label ? `Download ${item.label}` : "Download artifact";
                return renderDownloadLink(href, fileName, label);
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
                `<details class="raw-content-details"><summary>Markdown preview</summary><div class="markdown-render">${renderMarkdown(
                    String(result.markdown).slice(0, 12000)
                )}</div></details>`
            );
        }
        if (result.ocr_preview) {
            sections.push(
                `<details class="raw-content-details"><summary>OCR preview</summary><pre class="raw-json">${escapeHtml(
                    String(result.ocr_preview)
                )}</pre></details>`
            );
        }
        if (result.mathpix_preview) {
            sections.push(
                `<details class="raw-content-details"><summary>Math OCR preview</summary><pre class="raw-json">${escapeHtml(
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
        const elapsed = formatDuration(job && job.elapsed_seconds);
        const eta = formatDuration(job && job.eta_seconds);
        const metaItems = [
            `Status: <code>${escapeHtml((job && job.status) || "queued")}</code>`,
            `Stage: <code>${escapeHtml((job && job.stage) || "")}</code>`,
            `Progress: ${Math.round(percent)}%`,
        ];
        if (job && job.current != null && job.total != null) metaItems.push(`Pages: ${escapeHtml(job.current)}/${escapeHtml(job.total)}`);
        if (elapsed) metaItems.push(`Elapsed: ${escapeHtml(elapsed)}`);
        if (eta) metaItems.push(`ETA: ${escapeHtml(eta)}`);
        const meta = `<div class="result-meta">${metaItems.map((item) => `<span>${item}</span>`).join("")}</div>`;
        const message = job && job.message ? `<p>${escapeHtml(job.message)}</p>` : "";
        const meter = `<div class="progress-meter" aria-label="PDF conversion progress"><span style="width: ${percent}%"></span></div>`;
        const downloads = renderPdfDownloadLinks(job, model);
        const preview = renderPdfResultPreview(result);
        return `<h3>PDF conversion job</h3>${downloads}${meta}${meter}${message}${renderPdfProgressEvents(job)}${preview}${renderRawDetails({
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

    function readMultipartFormData(form) {
        const formData = new FormData();
        for (const el of Array.from(form.elements)) {
            if (!el.name) continue;
            if (el.type === "file") {
                const files = Array.from(el.files || []);
                const maxFiles = Number(el.dataset.maxFiles || 0);
                if (maxFiles > 0 && files.length > maxFiles) {
                    throw new Error(`Select no more than ${maxFiles} files for ${el.name}.`);
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
                label: "Download transcription",
            };
        }
        return null;
    }

    async function submitRequest(kind, url, form, renderer) {
        const body = readFormData(form);
        if (!body.model) {
            setStatus(kind, "Select a model first.", true);
            return;
        }
        const button = form.querySelector(".run-button");
        button.disabled = true;
        setStatus(kind, "Running…", false);
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
                setStatus(kind, `Error ${response.status}: ${detail}`, true);
                setResult(kind, `<pre class="raw-json">${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`);
                return;
            }
            const durationMs = Math.round(performance.now() - startedAt);
            setStatus(kind, `Done in ${(durationMs / 1000).toFixed(2)}s`, false);
            setResult(kind, renderer(payload));
        } catch (err) {
            setStatus(kind, `Request failed: ${err.message || err}`, true);
        } finally {
            button.disabled = false;
        }
    }

    async function submitMultipartRequest(kind, url, form, renderer) {
        const button = form.querySelector(".run-button");
        let body;
        try {
            body = readMultipartFormData(form);
        } catch (err) {
            setStatus(kind, err.message || String(err), true);
            return;
        }
        if (!body.get("model")) {
            setStatus(kind, "Select a model first.", true);
            return;
        }
        button.disabled = true;
        setStatus(kind, "Running…", false);
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
                setStatus(kind, `Error ${response.status}: ${detail}`, true);
                setResult(kind, `<pre class="raw-json">${escapeHtml(typeof payload === "string" ? payload : JSON.stringify(payload, null, 2))}</pre>`);
                return;
            }
            const durationMs = Math.round(performance.now() - startedAt);
            setStatus(kind, `Done in ${(durationMs / 1000).toFixed(2)}s`, false);
            const download = buildMultipartDownload(kind, form, payload, text, contentType);
            setResult(kind, renderer(payload, download));
        } catch (err) {
            setStatus(kind, `Request failed: ${err.message || err}`, true);
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
        } catch (err) {
            setStatus(kind, err.message || String(err), true);
            return;
        }
        const model = String(body.get("model") || "").trim();
        if (!model) {
            setStatus(kind, "Select a model first.", true);
            return;
        }

        button.disabled = true;
        setStatus(kind, "Uploading PDF…", false);
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
                setStatus(kind, `${job.message || job.status || "Running"} · ${Math.round(normalizePercent(job.percent))}%`, false);
                setResult(kind, renderPdfJob(job, model));
            }

            const durationMs = Math.round(performance.now() - startedAt);
            if (job.status === "failed") {
                const detail = job.error ? JSON.stringify(job.error) : (job.message || "Conversion failed");
                setStatus(kind, `Failed in ${(durationMs / 1000).toFixed(2)}s: ${detail}`, true);
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
            setStatus(kind, `Done in ${(durationMs / 1000).toFixed(2)}s · downloads: ${downloadCount}`, downloadCount === 0);
            setResult(kind, renderPdfJob(job, model, result));
        } catch (err) {
            setStatus(kind, `Request failed: ${err.message || err}`, true);
        } finally {
            button.disabled = false;
        }
    }

    async function submitAudioSpeechRequest(kind, url, form) {
        const body = readFormData(form);
        if (!body.model) {
            setStatus(kind, "Select a model first.", true);
            return;
        }
        const button = form.querySelector(".run-button");
        button.disabled = true;
        setStatus(kind, "Running…", false);
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
                setStatus(kind, `Error ${response.status}: ${detail}`, true);
                setResult(kind, `<pre class="raw-json">${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`);
                return;
            }
            const blob = await response.blob();
            const blobUrl = createDownloadUrl(kind, blob, contentType);
            const extension = body.response_format || extensionFromContentType(contentType, "mp3");
            const fileName = `speech.${extension}`;
            const durationMs = Math.round(performance.now() - startedAt);
            setStatus(kind, `Done in ${(durationMs / 1000).toFixed(2)}s`, false);
            setResult(kind, renderAudioSpeechResult(blobUrl, contentType, blob.size, fileName));
        } catch (err) {
            setStatus(kind, `Request failed: ${err.message || err}`, true);
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

    async function bootstrap() {
        wireForm("search", "searchForm", "/v1/web/search", renderSearchResult);
        wireForm("read", "readForm", "/v1/web/read", renderReadResult);
        wireForm("tavily-search", "tavilySearchForm", "/v1/tavily/search", renderTavilySearchResult);
        wireForm("tavily-extract", "tavilyExtractForm", "/v1/tavily/extract", renderTavilyExtractResult);
        wireForm("research", "researchForm", "/v1/web/research", renderResearchResult);
        wireForm("deep-research", "deepResearchForm", "/v1/web/deep-research", renderDeepResearchResult);
        wireAudioVoiceCatalog();
        wireAudioSpeechForm();
        wirePdfConversionForm();
        wireMultipartForm("audio-transcription", "audioTranscriptionForm", "/v1/audio/transcriptions", (payload, download) => renderPlainOrJsonResult(payload, "Transcription result", download));
        wireForm("image-generation", "imageGenerationForm", "/v1/images/generations", (payload) => renderImageOperationResult(payload, "Generated images"));
        wireMultipartForm("image-edit", "imageEditForm", "/v1/images/edits", (payload) => renderImageOperationResult(payload, "Edited images"));
        try {
            const models = await loadModels();
            MODEL_SELECTS.forEach(({id, section}) => {
                const select = document.getElementById(id);
                if (select) populateSelect(select, models[section] || []);
            });
            refreshAudioVoiceSelect();
        } catch (err) {
            STATUS_KINDS.forEach((kind) => {
                setStatus(kind, `Failed to load models: ${err.message || err}`, true);
            });
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
