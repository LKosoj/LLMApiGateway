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
        loadCatalog();
    });
})();
