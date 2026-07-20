(function initGatewayDocs() {
    const { apiFetch } = window.gatewayAuth;
    const i18n = window.gatewayI18n;
    const catalogStatusElement = document.getElementById("catalogStatus");
    const catalogStatusDetail = document.getElementById("catalogStatusDetail");
    const freeTierStatusElement = document.getElementById("freeTierDocStatus");
    const freeTierStatusDetail = document.getElementById("freeTierStatusDetail");
    const catalogStatus = window.gatewayUi.createStatus(catalogStatusElement, {
        rawDetailElement: catalogStatusDetail,
    });
    const freeTierStatus = window.gatewayUi.createStatus(freeTierStatusElement, {
        rawDetailElement: freeTierStatusDetail,
    });
    const apiBase = `${window.location.origin}/v1`;
    const freeTierCache = new Map();
    const freeTierPending = new Map();
    const catalogState = {
        phase: "idle",
        payload: null,
        rawError: null,
    };
    let freeTierGeneration = 0;
    let pendingLocaleUiState = null;
    let unsubscribeLocale = null;
    let destroyed = false;

    const GROUPS = [
        {
            key: "chat",
            catalogKey: "chat",
        },
        {
            key: "embeddings",
            catalogKey: "embeddings",
        },
        {
            key: "rerank",
            catalogKey: "rerank",
        },
        {
            key: "images",
            catalogKey: "images",
        },
        {
            key: "audio_transcription",
            catalogKey: "audioTranscription",
        },
        {
            key: "audio_speech",
            catalogKey: "audioSpeech",
        },
        {
            key: "pdf_conversion",
            catalogKey: "pdfConversion",
        },
        {
            key: "web_search",
            catalogKey: "webSearch",
        },
        {
            key: "web_read",
            catalogKey: "webRead",
        },
        {
            key: "web_research",
            catalogKey: "webResearch",
        },
        {
            key: "web_deep_research",
            catalogKey: "webDeepResearch",
        },
    ];
    const GROUPS_BY_KEY = new Map(GROUPS.map((group) => [group.key, group]));

    function setStatus(message, state = "busy", rawDetail = null) {
        catalogStatusElement.classList.remove("ready");
        catalogStatusDetail.hidden = rawDetail === null;
        if (state === "error") {
            if (rawDetail === null) catalogStatus.error(message);
            else catalogStatus.error(message, { rawDetail });
        } else if (state === "ready") {
            catalogStatus.polite(message);
            catalogStatusElement.classList.add("ready");
        } else {
            catalogStatus.busy(message);
        }
    }

    function setFreeTierStatus(message, state = "busy", rawDetail = null) {
        if (!message) {
            freeTierStatus.clear();
            freeTierStatusElement.hidden = true;
            freeTierStatusDetail.hidden = true;
            return;
        }
        freeTierStatusElement.hidden = false;
        freeTierStatusDetail.hidden = rawDetail === null;
        if (state === "error") {
            if (rawDetail === null) freeTierStatus.error(message);
            else freeTierStatus.error(message, { rawDetail });
        } else {
            freeTierStatus.busy(message);
        }
    }

    function renderApiBase() {
        document.querySelectorAll("[data-docs-api-base]").forEach((element) => {
            element.textContent = apiBase;
        });
    }

    function isLoopbackHostname(hostname) {
        const normalized = hostname
            .toLowerCase()
            .replace(/^\[|\]$/g, "")
            .replace(/\.$/, "");
        return normalized === "localhost"
            || normalized.endsWith(".localhost")
            || normalized === "::1"
            || /^127(?:\.\d{1,3}){3}$/.test(normalized);
    }

    function renderTransportWarning() {
        const warning = document.getElementById("insecureTransportWarning");
        if (!warning) return;
        const shouldWarn = window.location.protocol === "http:"
            && !isLoopbackHostname(window.location.hostname);
        warning.hidden = !shouldWarn;
        warning.textContent = shouldWarn
            ? i18n.t("docs:transport.insecureHttpWarning")
            : "";
    }

    function setupDocsTabs() {
        const buttons = Array.from(document.querySelectorAll(".docs-tab-button"));
        const tabList = buttons[0]?.closest('[role="tablist"]');
        const panels = [
            document.getElementById("apiDocsPanel"),
            document.getElementById("freeTierDocsPanel"),
        ];
        if (!tabList || panels.some((panel) => !panel)) return;

        const activateDocsTab = async (context) => {
            buttons.forEach((button) => {
                button.classList.toggle("active", button === context.tab);
            });
            panels.forEach((panel) => {
                panel.classList.toggle("active", panel === context.panel);
            });
            if (context.key === "free-tier") {
                await loadFreeTierDoc(context);
            } else {
                invalidateFreeTierLoads();
            }
        };

        window.gatewayUi.createTabs(tabList, {
            activation: "manual",
            getKey: (button) => button.dataset.docsTab || "api",
            initialKey: "api",
            onActivate: activateDocsTab,
            onReselect: activateDocsTab,
            panels,
        });
    }

    function setupCopyButtons() {
        document.querySelectorAll(".docs-code-block .btn-copy").forEach((button) => {
            const codeElement = button.closest(".docs-code-block")?.querySelector("pre code");
            if (!codeElement) return;
            let resetTimer = null;
            button.addEventListener("click", () => {
                navigator.clipboard.writeText(codeElement.textContent).then(() => {
                    if (resetTimer) window.clearTimeout(resetTimer);
                    button.classList.add("copied");
                    const copiedLabel = i18n.t("common:actions.copied");
                    button.setAttribute("aria-label", copiedLabel);
                    button.setAttribute("title", copiedLabel);
                    resetTimer = window.setTimeout(() => {
                        resetTimer = null;
                        button.classList.remove("copied");
                        const copyLabel = i18n.t("common:actions.copy");
                        button.setAttribute("aria-label", copyLabel);
                        button.setAttribute("title", copyLabel);
                    }, 2000);
                }).catch(() => undefined);
            });
        });
    }

    function setupToc() {
        const tocNav = document.querySelector("[data-docs-toc]");
        const headings = Array.from(document.querySelectorAll(".docs-main h2"));
        if (!tocNav || headings.length === 0) return;

        const tocLinks = new Map(
            Array.from(tocNav.querySelectorAll('a[href^="#"]')).map(
                (link) => [link.getAttribute("href").slice(1), link],
            ),
        );
        if (tocLinks.size === 0) return;

        let currentId = null;
        const setCurrent = (id) => {
            if (id === currentId) return;
            if (currentId && tocLinks.has(currentId)) {
                tocLinks.get(currentId).classList.remove("docs-toc-current");
            }
            currentId = id;
            if (id && tocLinks.has(id)) {
                tocLinks.get(id).classList.add("docs-toc-current");
            }
        };

        const observer = new IntersectionObserver(
            (entries) => {
                const visible = entries
                    .filter((entry) => entry.isIntersecting)
                    .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
                if (visible.length > 0) {
                    setCurrent(visible[0].target.closest(".docs-section")?.id || null);
                }
            },
            { rootMargin: "-96px 0px -70% 0px", threshold: 0 },
        );
        headings.forEach((heading) => observer.observe(heading));
    }

    function appendModelChip(parent, name, modelsById) {
        const chip = document.createElement("span");
        chip.className = "model-chip";
        chip.textContent = name;
        chip.lang = "und";
        chip.dir = "auto";

        const model = modelsById.get(name);
        if (model) {
            const details = [];
            if (Array.isArray(model.image_operations) && model.image_operations.length) {
                details.push(`images: ${model.image_operations.join(", ")}`);
            }
            if (Array.isArray(model.web_operations) && model.web_operations.length) {
                details.push(`web: ${model.web_operations.join(", ")}`);
            }
            if (details.length) chip.title = details.join("; ");
        }

        parent.appendChild(chip);
    }

    function appendInlineMarkdown(parent, text) {
        const pattern = /(`[^`]+`|\[[^\]]+\]\([^)]+\)|https?:\/\/[^\s)]+)/g;
        let lastIndex = 0;
        let match = pattern.exec(text);
        while (match) {
            if (match.index > lastIndex) {
                parent.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
            }
            const token = match[0];
            if (token.startsWith("`")) {
                const code = document.createElement("code");
                code.textContent = token.slice(1, -1);
                parent.appendChild(code);
            } else if (token.startsWith("http://") || token.startsWith("https://")) {
                const link = document.createElement("a");
                link.href = token;
                link.rel = "noopener noreferrer";
                link.target = "_blank";
                link.textContent = token;
                parent.appendChild(link);
            } else {
                const linkMatch = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
                const label = linkMatch ? linkMatch[1] : token;
                const url = linkMatch ? linkMatch[2] : "";
                if (/^https?:\/\//.test(url)) {
                    const link = document.createElement("a");
                    link.href = url;
                    link.rel = "noopener noreferrer";
                    link.target = "_blank";
                    link.textContent = label;
                    parent.appendChild(link);
                } else {
                    parent.appendChild(document.createTextNode(label));
                }
            }
            lastIndex = pattern.lastIndex;
            match = pattern.exec(text);
        }
        if (lastIndex < text.length) {
            parent.appendChild(document.createTextNode(text.slice(lastIndex)));
        }
    }

    function parseTableRow(line) {
        return line
            .trim()
            .replace(/^\|/, "")
            .replace(/\|$/, "")
            .split("|")
            .map((cell) => cell.trim());
    }

    function isTableSeparator(line) {
        return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
    }

    function appendParagraph(container, lines) {
        if (lines.length === 0) return;
        const paragraph = document.createElement("p");
        appendInlineMarkdown(paragraph, lines.join(" "));
        container.appendChild(paragraph);
    }

    function appendList(container, lines, ordered) {
        const list = document.createElement(ordered ? "ol" : "ul");
        lines.forEach((line) => {
            const item = document.createElement("li");
            const text = ordered ? line.replace(/^\s*\d+\.\s+/, "") : line.replace(/^\s*[-*]\s+/, "");
            appendInlineMarkdown(item, text);
            list.appendChild(item);
        });
        container.appendChild(list);
    }

    function appendTable(container, lines) {
        if (lines.length < 2 || !isTableSeparator(lines[1])) {
            appendParagraph(container, lines);
            return;
        }
        const table = document.createElement("table");
        const thead = document.createElement("thead");
        const headerRow = document.createElement("tr");
        parseTableRow(lines[0]).forEach((cell) => {
            const th = document.createElement("th");
            appendInlineMarkdown(th, cell);
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        const tbody = document.createElement("tbody");
        lines.slice(2).forEach((line) => {
            const row = document.createElement("tr");
            parseTableRow(line).forEach((cell) => {
                const td = document.createElement("td");
                appendInlineMarkdown(td, cell);
                row.appendChild(td);
            });
            tbody.appendChild(row);
        });
        table.appendChild(tbody);
        container.appendChild(table);
    }

    function renderMarkdown(markdown) {
        const container = document.getElementById("freeTierMarkdown");
        if (!container) return;
        container.replaceChildren();

        const lines = markdown.replace(/\r\n/g, "\n").split("\n");
        let index = 0;
        while (index < lines.length) {
            const line = lines[index];
            if (!line.trim()) {
                index += 1;
                continue;
            }

            if (line.startsWith("```")) {
                const codeLines = [];
                index += 1;
                while (index < lines.length && !lines[index].startsWith("```")) {
                    codeLines.push(lines[index]);
                    index += 1;
                }
                if (index < lines.length) index += 1;
                const pre = document.createElement("pre");
                const code = document.createElement("code");
                code.textContent = codeLines.join("\n");
                pre.appendChild(code);
                container.appendChild(pre);
                continue;
            }

            const headingMatch = line.match(/^(#{1,3})\s+(.+)$/);
            if (headingMatch) {
                const heading = document.createElement(`h${headingMatch[1].length}`);
                appendInlineMarkdown(heading, headingMatch[2]);
                container.appendChild(heading);
                index += 1;
                continue;
            }

            if (/^\s*\|/.test(line) && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
                const tableLines = [];
                while (index < lines.length && /^\s*\|/.test(lines[index])) {
                    tableLines.push(lines[index]);
                    index += 1;
                }
                appendTable(container, tableLines);
                continue;
            }

            if (/^\s*[-*]\s+/.test(line)) {
                const listLines = [];
                while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
                    listLines.push(lines[index]);
                    index += 1;
                }
                appendList(container, listLines, false);
                continue;
            }

            if (/^\s*\d+\.\s+/.test(line)) {
                const listLines = [];
                while (index < lines.length && /^\s*\d+\.\s+/.test(lines[index])) {
                    listLines.push(lines[index]);
                    index += 1;
                }
                appendList(container, listLines, true);
                continue;
            }

            const paragraphLines = [];
            while (
                index < lines.length
                && lines[index].trim()
                && !lines[index].startsWith("```")
                && !/^(#{1,3})\s+/.test(lines[index])
                && !/^\s*\|/.test(lines[index])
                && !/^\s*[-*]\s+/.test(lines[index])
                && !/^\s*\d+\.\s+/.test(lines[index])
            ) {
                paragraphLines.push(lines[index]);
                index += 1;
            }
            appendParagraph(container, paragraphLines);
        }
    }

    function freeTierPanelIsActive() {
        const panel = document.getElementById("freeTierDocsPanel");
        return Boolean(panel && !panel.hidden && panel.classList.contains("active"));
    }

    function abortPendingDocsExcept(locale = null) {
        for (const [pendingLocale, request] of freeTierPending) {
            if (pendingLocale === locale) continue;
            freeTierPending.delete(pendingLocale);
            request.controller.abort();
        }
    }

    function invalidateFreeTierLoads() {
        freeTierGeneration += 1;
        abortPendingDocsExcept();
    }

    function requestFreeTierDoc(locale) {
        if (freeTierCache.has(locale)) {
            return Promise.resolve(freeTierCache.get(locale));
        }
        const pending = freeTierPending.get(locale);
        if (pending) return pending.promise;

        const request = {
            controller: new AbortController(),
            promise: null,
        };
        request.promise = window.fetch(
            `/static/locales/${encodeURIComponent(locale)}/free-tier-providers.md`,
            {
                credentials: "same-origin",
                signal: request.controller.signal,
            },
        ).then(async (response) => {
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            const markdown = await response.text();
            if (!markdown.trim()) {
                throw new Error("empty document");
            }
            freeTierCache.set(locale, markdown);
            return markdown;
        }).finally(() => {
            if (freeTierPending.get(locale) === request) {
                freeTierPending.delete(locale);
            }
        });
        freeTierPending.set(locale, request);
        return request.promise;
    }

    function freeTierLoadIsCurrent(locale, generation, activationContext) {
        const pageStateIsCurrent = !destroyed
            && generation === freeTierGeneration
            && i18n.currentLocale === locale
            && freeTierPanelIsActive();
        if (!pageStateIsCurrent || !activationContext) return pageStateIsCurrent;
        return !(activationContext.signal.aborted || !activationContext.isCurrent());
    }

    function renderFreeTierError() {
        const container = document.getElementById("freeTierMarkdown");
        if (!container) return;
        container.replaceChildren();
        const message = document.createElement("p");
        message.className = "markdown-error";
        message.textContent = i18n.t("docs:freeTier.errorHint");
        container.appendChild(message);
    }

    function captureLocaleUiState() {
        const container = document.getElementById("freeTierMarkdown");
        const panel = document.getElementById("freeTierDocsPanel");
        const activeElement = document.activeElement;
        return {
            activeElement,
            focusWasInMarkdown: Boolean(container && activeElement && container.contains(activeElement)),
            panelScrollLeft: panel?.scrollLeft || 0,
            panelScrollTop: panel?.scrollTop || 0,
            restored: false,
            retryTimer: null,
            windowScrollX: window.scrollX,
            windowScrollY: window.scrollY,
        };
    }

    function restoreLocaleUiState(state) {
        if (!state) return;
        const panel = document.getElementById("freeTierDocsPanel");
        const panelIsUnchanged = !state.restored || (
            panel?.scrollLeft === state.panelScrollLeft
            && panel?.scrollTop === state.panelScrollTop
        );
        if (panel && panelIsUnchanged) {
            panel.scrollLeft = state.panelScrollLeft;
            panel.scrollTop = state.panelScrollTop;
        }
        if (!state.focusTarget) {
            state.focusTarget = state.activeElement?.isConnected
                ? state.activeElement
                : document.querySelector('[data-docs-tab="free-tier"]');
        }
        const currentFocus = document.activeElement;
        if (
            state.focusTarget
            && (!state.restored || currentFocus === document.body || currentFocus === state.focusTarget)
            && !state.focusTarget.disabled
        ) {
            state.focusTarget.focus({ preventScroll: true });
        }
        const scrollIsUnchanged = !state.restored || (
            window.scrollX === state.windowScrollX
            && window.scrollY === state.windowScrollY
        );
        if (scrollIsUnchanged) {
            window.scrollTo({
                behavior: "instant",
                left: state.windowScrollX,
                top: state.windowScrollY,
            });
        }
        state.restored = true;
        if (state.focusTarget?.disabled && state.retryTimer === null) {
            state.retryTimer = window.setTimeout(() => {
                state.retryTimer = null;
                restoreLocaleUiState(state);
            }, 0);
        }
    }

    async function loadFreeTierDoc(activationContext, localeUiState = null) {
        try {
            await i18n.ready;
        } catch {
            return;
        }
        if (destroyed || !freeTierPanelIsActive()) return;
        const locale = i18n.currentLocale;
        const generation = ++freeTierGeneration;
        abortPendingDocsExcept(locale);
        setFreeTierStatus(i18n.t("docs:freeTier.loading"));
        document.getElementById("freeTierMarkdown")?.replaceChildren();
        restoreLocaleUiState(localeUiState);
        try {
            const markdown = await requestFreeTierDoc(locale);
            if (!freeTierLoadIsCurrent(locale, generation, activationContext)) return;
            renderMarkdown(markdown);
            setFreeTierStatus("");
            restoreLocaleUiState(localeUiState);
        } catch (error) {
            if (!freeTierLoadIsCurrent(locale, generation, activationContext)) return;
            setFreeTierStatus(
                i18n.t("docs:freeTier.unavailable"),
                "error",
                error.message,
            );
            renderFreeTierError();
            restoreLocaleUiState(localeUiState);
        }
    }

    function renderCatalog(payload) {
        const container = document.getElementById("modelCatalog");
        if (!container) return;

        catalogState.phase = "ready";
        catalogState.payload = payload;
        catalogState.rawError = null;
        container.replaceChildren();
        const groups = payload.groups || {};
        const modelsById = new Map((payload.models || []).map((model) => [model.id, model]));

        GROUPS.forEach((group) => {
            const panel = document.createElement("article");
            panel.className = "model-group";
            panel.dataset.catalogGroup = group.key;

            const title = document.createElement("h3");
            title.dataset.catalogCopy = "title";
            panel.appendChild(title);

            const hint = document.createElement("p");
            hint.dataset.catalogCopy = "hint";
            panel.appendChild(hint);

            const names = groups[group.key] || [];
            if (names.length === 0) {
                const empty = document.createElement("span");
                empty.className = "model-empty";
                empty.dataset.catalogCopy = "empty";
                panel.appendChild(empty);
            } else {
                const list = document.createElement("div");
                list.className = "model-chip-list";
                names.forEach((name) => appendModelChip(list, name, modelsById));
                panel.appendChild(list);
            }

            container.appendChild(panel);
        });

        renderCatalogLocale();
    }

    function renderCatalogStatus() {
        if (catalogState.phase === "loading") {
            setStatus(i18n.t("docs:catalog.loading"));
        } else if (catalogState.phase === "ready") {
            setStatus(
                i18n.t("docs:catalog.ready", {
                    count: (catalogState.payload?.models || []).length,
                }),
                "ready",
            );
        } else if (catalogState.phase === "error") {
            setStatus(
                i18n.t("docs:catalog.unavailable"),
                "error",
                catalogState.rawError,
            );
        }
    }

    function renderCatalogLocale() {
        const container = document.getElementById("modelCatalog");
        if (!container) return;
        container.querySelectorAll("[data-catalog-group]").forEach((panel) => {
            const group = GROUPS_BY_KEY.get(panel.dataset.catalogGroup);
            if (!group) return;
            const prefix = `docs:catalog.groups.${group.catalogKey}`;
            const title = panel.querySelector('[data-catalog-copy="title"]');
            const hint = panel.querySelector('[data-catalog-copy="hint"]');
            const empty = panel.querySelector('[data-catalog-copy="empty"]');
            if (title) title.textContent = i18n.t(`${prefix}.title`);
            if (hint) hint.textContent = i18n.t(`${prefix}.hint`);
            if (empty) empty.textContent = i18n.t("docs:catalog.empty");
        });
        const errorHint = container.querySelector("[data-catalog-error-hint]");
        if (errorHint) errorHint.textContent = i18n.t("docs:catalog.errorHint");
        renderCatalogStatus();
    }

    async function loadCatalog() {
        catalogState.phase = "loading";
        catalogState.payload = null;
        catalogState.rawError = null;
        setStatus(i18n.t("docs:catalog.loading"));
        try {
            const response = await apiFetch("/v1/ui/docs/models");
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            renderCatalog(await response.json());
        } catch (error) {
            catalogState.phase = "error";
            catalogState.payload = null;
            catalogState.rawError = error.message;
            setStatus(i18n.t("docs:catalog.unavailable"), "error", error.message);
            const container = document.getElementById("modelCatalog");
            if (container) {
                const hint = document.createElement("p");
                hint.dataset.catalogErrorHint = "";
                hint.textContent = i18n.t("docs:catalog.errorHint");
                container.replaceChildren(hint);
            }
        }
    }

    function rerenderLocale() {
        const uiState = pendingLocaleUiState || captureLocaleUiState();
        pendingLocaleUiState = null;
        renderCatalogLocale();
        renderTransportWarning();
        freeTierStatus.rerender();
        if (freeTierPanelIsActive()) {
            void loadFreeTierDoc(null, uiState);
        } else {
            invalidateFreeTierLoads();
            restoreLocaleUiState(uiState);
        }
    }

    function captureLocaleChange(event) {
        if (event.target?.id === "localeSelect") {
            pendingLocaleUiState = captureLocaleUiState();
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        Theme.attachToggle("darkModeToggle");
        renderApiBase();
        setupDocsTabs();
        setupCopyButtons();
        setupToc();
        document.addEventListener("change", captureLocaleChange, { capture: true });
        void i18n.ready.then(
            () => {
                if (destroyed) return;
                renderTransportWarning();
                void loadCatalog();
                unsubscribeLocale = i18n.subscribe(rerenderLocale);
            },
            () => undefined,
        );
        window.addEventListener("beforeunload", () => {
            destroyed = true;
            unsubscribeLocale?.();
            document.removeEventListener("change", captureLocaleChange, { capture: true });
            invalidateFreeTierLoads();
        });
    });
})();
