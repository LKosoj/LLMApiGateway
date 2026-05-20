(function initGatewayDocs() {
    const { apiFetch } = window.gatewayAuth;

    const GROUPS = [
        {
            key: "chat",
            title: "Chat",
            hint: "Модели из fallback rules для /v1/chat/completions, /v1/responses и /v1/messages.",
        },
        {
            key: "embeddings",
            title: "Embeddings",
            hint: "Operation routes для /v1/embeddings.",
        },
        {
            key: "rerank",
            title: "Rerank",
            hint: "Operation routes для /v1/rerank и внутреннего ранжирования web research.",
        },
        {
            key: "images",
            title: "Images",
            hint: "Модели генерации и редактирования для /v1/images*.",
        },
        {
            key: "audio_transcription",
            title: "Audio",
            hint: "Модели транскрибации для /v1/audio/transcriptions.",
        },
        {
            key: "audio_speech",
            title: "Speech",
            hint: "Модели генерации речи для /v1/audio/speech и /v1/audio/voices.",
        },
        {
            key: "pdf_conversion",
            title: "PDF",
            hint: "Модели конвертации PDF для /v1/pdf/*.",
        },
        {
            key: "web_search",
            title: "Web search",
            hint: "Сервисные модели для /v1/web/search.",
        },
        {
            key: "web_read",
            title: "Web read",
            hint: "Сервисные модели для /v1/web/read.",
        },
        {
            key: "web_research",
            title: "Web research",
            hint: "Сервисные модели, связывающие search/read/rerank/analysis.",
        },
        {
            key: "web_deep_research",
            title: "Deep research",
            hint: "Сервисные модели GPT Researcher с LLM, embedding и image-настройками gateway.",
        },
    ];

    function setupThemeToggle() {
        const toggle = document.getElementById("darkModeToggle");
        if (!toggle) return;

        const stored = localStorage.getItem("darkMode");
        if (stored === "1") document.body.classList.add("dark-mode");

        toggle.addEventListener("click", () => {
            const enabled = document.body.classList.toggle("dark-mode");
            localStorage.setItem("darkMode", enabled ? "1" : "0");
        });
    }

    function setStatus(message, className) {
        const status = document.getElementById("catalogStatus");
        if (!status) return;
        status.textContent = message;
        status.classList.remove("ready", "error");
        if (className) status.classList.add(className);
    }

    function setFreeTierStatus(message, className) {
        const status = document.getElementById("freeTierDocStatus");
        if (!status) return;
        status.textContent = message;
        status.classList.remove("ready", "error");
        if (className) status.classList.add(className);
    }

    function setupDocsTabs() {
        const buttons = document.querySelectorAll(".docs-tab-button");
        const panels = {
            api: document.getElementById("apiDocsPanel"),
            "free-tier": document.getElementById("freeTierDocsPanel"),
        };
        buttons.forEach((button) => {
            button.addEventListener("click", () => {
                const tab = button.dataset.docsTab || "api";
                buttons.forEach((item) => {
                    const isActive = item === button;
                    item.classList.toggle("active", isActive);
                    item.setAttribute("aria-selected", isActive ? "true" : "false");
                });
                Object.entries(panels).forEach(([key, panel]) => {
                    if (!panel) return;
                    const isActive = key === tab;
                    panel.hidden = !isActive;
                    panel.classList.toggle("active", isActive);
                });
                if (tab === "free-tier") {
                    loadFreeTierDoc();
                }
            });
        });
    }

    function appendModelChip(parent, name, modelsById) {
        const chip = document.createElement("span");
        chip.className = "model-chip";
        chip.textContent = name;

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

    let freeTierDocLoaded = false;
    async function loadFreeTierDoc() {
        if (freeTierDocLoaded) return;
        setFreeTierStatus("Загрузка free-tier каталога...");
        try {
            const response = await apiFetch("/v1/ui/docs/free-tier-providers.md");
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            renderMarkdown(await response.text());
            freeTierDocLoaded = true;
            setFreeTierStatus("Каталог загружен из examples/free-tier-providers.md", "ready");
        } catch (error) {
            setFreeTierStatus(`Каталог free-tier недоступен: ${error.message}`, "error");
            const container = document.getElementById("freeTierMarkdown");
            if (container) {
                container.replaceChildren();
                const message = document.createElement("p");
                message.className = "markdown-error";
                message.textContent = "Проверьте, что файл examples/free-tier-providers.md существует и доступен gateway.";
                container.appendChild(message);
            }
        }
    }

    function renderCatalog(payload) {
        const container = document.getElementById("modelCatalog");
        if (!container) return;

        container.replaceChildren();
        const groups = payload.groups || {};
        const modelsById = new Map((payload.models || []).map((model) => [model.id, model]));

        GROUPS.forEach((group) => {
            const panel = document.createElement("article");
            panel.className = "model-group";

            const title = document.createElement("h3");
            title.textContent = group.title;
            panel.appendChild(title);

            const hint = document.createElement("p");
            hint.textContent = group.hint;
            panel.appendChild(hint);

            const names = groups[group.key] || [];
            if (names.length === 0) {
                const empty = document.createElement("span");
                empty.className = "model-empty";
                empty.textContent = "Нет сконфигурированных gateway-моделей.";
                panel.appendChild(empty);
            } else {
                const list = document.createElement("div");
                list.className = "model-chip-list";
                names.forEach((name) => appendModelChip(list, name, modelsById));
                panel.appendChild(list);
            }

            container.appendChild(panel);
        });

        setStatus(`Загружено gateway-моделей: ${(payload.models || []).length}`, "ready");
    }

    async function loadCatalog() {
        try {
            const response = await apiFetch("/v1/ui/docs/models");
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            renderCatalog(await response.json());
        } catch (error) {
            setStatus(`Каталог моделей недоступен: ${error.message}`, "error");
            const container = document.getElementById("modelCatalog");
            if (container) {
                container.textContent = "Откройте Rules Editor или проверьте, что gateway загрузил конфигурацию.";
            }
        }
    }

    document.addEventListener("DOMContentLoaded", () => {
        setupThemeToggle();
        setupDocsTabs();
        loadCatalog();
    });
})();
