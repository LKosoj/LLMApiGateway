(function () {
    const apiFetch = window.gatewayAuth.apiFetch;
    const i18n = window.gatewayI18n;

    const keysArea = document.getElementById("keysArea");
    const messageArea = document.getElementById("messageArea");
    const messageDetail = document.getElementById("messageDetail");

    const modalOverlay = document.getElementById("keyModal");
    const modalTitle = document.getElementById("modalTitle");
    const fieldName = document.getElementById("fieldName");
    const fieldBudget = document.getElementById("fieldBudget");
    const fieldBudgetPeriod = document.getElementById("fieldBudgetPeriod");
    const fieldRpm = document.getElementById("fieldRpm");
    const fieldTpm = document.getElementById("fieldTpm");
    const fieldMetadata = document.getElementById("fieldMetadata");
    const fieldDisabled = document.getElementById("fieldDisabled");
    const fieldResetSpent = document.getElementById("fieldResetSpent");
    const editOnlyFields = document.getElementById("editOnlyFields");
    const allowedModelsList = document.getElementById("allowedModelsList");
    const newKeyNotice = document.getElementById("newKeyNotice");
    const newKeyValue = document.getElementById("newKeyValue");

    const createKeyBtn = document.getElementById("createKeyBtn");
    const refreshKeysBtn = document.getElementById("refreshKeysBtn");
    const saveKeyBtn = document.getElementById("saveKeyBtn");
    const cancelKeyBtn = document.getElementById("cancelKeyBtn");

    let editingKeyId = null;
    let availableModels = [];
    let modelCatalogState = "idle";
    let modelCatalogPromise = null;
    let selectedAllowedModels = new Set();
    let modalFocusOrigin = {kind: "create", keyId: null};
    let modalTitleName = null;
    let unsubscribeLocale = null;
    let destroyed = false;

    const messageStatus = window.gatewayUi.createStatus(messageArea, {
        defaultTimeoutMs: 5000,
        rawDetailElement: messageDetail,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });
    const dialogController = window.gatewayUi.createDialog({
        overlay: modalOverlay,
        dialog: modalOverlay.querySelector(".modal"),
        inertRoots: Array.from(document.querySelectorAll(".top-nav, .container")),
        labelledBy: "modalTitle",
        initialFocus: () => fieldName,
        openClass: "visible",
        restoreFocus: () => {
            if (modalFocusOrigin.kind === "edit" && modalFocusOrigin.keyId !== null) {
                return keysArea.querySelector(
                    `tr[data-key-id="${modalFocusOrigin.keyId}"] button[data-action="edit"]`
                );
            }
            return createKeyBtn;
        },
        restoreFocusFallback: () => createKeyBtn,
    });

    function translate(key, values = {}) {
        return i18n.t(`api_keys:${key}`, values);
    }

    function publishStatus(message, isError = false, options = {}) {
        messageDetail.hidden = !Object.hasOwn(options, "rawDetail") || options.rawDetail === null;
        if (isError) {
            messageStatus.error(message, options);
        } else {
            messageStatus.polite(message, options);
        }
    }

    function showApiError(payload, response) {
        const descriptor = window.gatewayUi.describeApiError(payload, {
            status: response.status,
            requestId: response.headers.get("X-Request-ID"),
        });
        publishStatus(
            {key: descriptor.summaryKey, values: descriptor.summaryValues},
            true,
            {rawDetail: descriptor.rawDetail},
        );
    }

    async function showResponseError(response) {
        const payload = await response.json().catch(() => null);
        showApiError(payload, response);
    }

    function showNetworkError(error) {
        const descriptor = window.gatewayUi.describeApiError(null);
        publishStatus(
            {key: descriptor.summaryKey, values: descriptor.summaryValues},
            true,
            {rawDetail: error.message},
        );
    }

    class ApiKeysValidationError extends Error {
        constructor(key, rawDetail = null) {
            super(key);
            this.key = key;
            this.rawDetail = rawDetail;
        }
    }

    function formatCurrency(value) {
        if (value === null || value === undefined) return translate("table.values.notAvailable");
        const number = Number(value);
        if (!Number.isFinite(number)) return translate("table.values.notAvailable");
        return i18n.formatCurrency(number, "USD", {
            minimumFractionDigits: 4,
            maximumFractionDigits: 4,
        });
    }

    function formatInteger(value) {
        if (value === null || value === undefined) return translate("table.values.notAvailable");
        const number = Number(value);
        if (!Number.isFinite(number)) return translate("table.values.notAvailable");
        return i18n.formatNumber(number, {maximumFractionDigits: 0});
    }

    function formatDate(value) {
        if (!value) return translate("table.values.notAvailable");
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return translate("table.values.notAvailable");
        return i18n.formatDate(date, {dateStyle: "medium", timeStyle: "medium"});
    }

    function maskSecret(value) {
        const text = String(value || "");
        if (!text) return "—";
        if (text.length <= 8) return "••••";
        return `${text.slice(0, 4)}…${text.slice(-4)}`;
    }

    function resetPeriodLabel(value) {
        const keys = {
            none: "table.values.resetNone",
            daily: "table.values.resetDaily",
            monthly: "table.values.resetMonthly",
        };
        return translate(keys[value || "none"] || "table.values.resetUnknown");
    }

    function localizedRowValues(record) {
        const budget = record.budget_usd === null || record.budget_usd === undefined
            ? translate("table.values.notAvailable")
            : Number(record.budget_usd) < 0
                ? translate("table.values.unlimited")
                : formatCurrency(record.budget_usd);
        const allowedModels = record.allowed_models || [];
        return {
            status: translate(record.disabled ? "table.status.disabled" : "table.status.active"),
            spentBudget: `${formatCurrency(record.spent_usd)} / ${budget}`,
            reset: resetPeriodLabel(record.budget_period),
            rates: `${formatInteger(record.rpm)} / ${formatInteger(record.tpm)}`,
            allowed: allowedModels.length
                ? allowedModels.join(", ")
                : translate("table.values.allModels"),
            lastUsed: formatDate(record.last_used_at),
        };
    }

    function updateRowLocale(row, record) {
        const values = localizedRowValues(record);
        row.querySelector('[data-key-field="id"]').textContent = i18n.t("api_keys:table.values.keyId", {
            id: i18n.formatNumber(record.id),
        });
        const keyCode = row.querySelector('[data-key-field="key"]');
        keyCode.title = i18n.t("api_keys:table.fullKeyTitle");
        for (const [field, value] of Object.entries(values)) {
            row.querySelector(`[data-key-field="${field}"]`).textContent = value;
        }
        row.querySelector('[data-action="edit"]').textContent = i18n.t("api_keys:table.actions.edit");
        row.querySelector('[data-action="delete"]').textContent = i18n.t("api_keys:table.actions.delete");
    }

    function updateKeysTableLocale() {
        const emptyState = keysArea.querySelector(".api-keys-empty");
        if (emptyState) emptyState.textContent = i18n.t("api_keys:table.empty");

        const tableWrap = keysArea.querySelector(".keys-table-wrap");
        if (tableWrap) {
            tableWrap.setAttribute("data-scroll-hint", i18n.t("api_keys:table.scrollHint"));
        }

        keysArea.querySelectorAll("[data-key-column]").forEach((heading) => {
            heading.textContent = i18n.t(`api_keys:table.columns.${heading.dataset.keyColumn}`);
        });
        keysArea.querySelectorAll("tr[data-key-id]").forEach((row) => {
            const keyId = Number(row.dataset.keyId);
            const record = currentKeys.find((item) => item.id === keyId);
            if (record) updateRowLocale(row, record);
        });
    }

    function renderKeysTable(keys) {
        if (!keys.length) {
            const emptyState = document.createElement("p");
            emptyState.className = "api-keys-empty";
            emptyState.textContent = i18n.t("api_keys:table.empty");
            keysArea.replaceChildren(emptyState);
            return;
        }

        const tableWrap = document.createElement("div");
        tableWrap.className = "keys-table-wrap";
        tableWrap.setAttribute("data-scroll-hint", i18n.t("api_keys:table.scrollHint"));
        const table = document.createElement("table");
        table.className = "keys-table";
        const head = document.createElement("thead");
        const headingRow = document.createElement("tr");
        for (const column of [
            "name",
            "key",
            "status",
            "spentBudget",
            "reset",
            "rateLimits",
            "allowedModels",
            "lastUsed",
        ]) {
            const heading = document.createElement("th");
            heading.dataset.keyColumn = column;
            heading.textContent = i18n.t(`api_keys:table.columns.${column}`);
            headingRow.appendChild(heading);
        }
        headingRow.appendChild(document.createElement("th"));
        head.appendChild(headingRow);

        const body = document.createElement("tbody");
        for (const record of keys) {
            const row = document.createElement("tr");
            row.dataset.keyId = String(record.id);

            const nameCell = document.createElement("td");
            const name = document.createElement("strong");
            name.setAttribute("lang", "und");
            name.setAttribute("dir", "auto");
            name.textContent = record.name;
            const id = document.createElement("small");
            id.dataset.keyField = "id";
            nameCell.append(name, document.createElement("br"), id);
            row.appendChild(nameCell);

            const keyCell = document.createElement("td");
            const keyCode = document.createElement("code");
            keyCode.className = "masked-secret";
            keyCode.dataset.keyField = "key";
            keyCode.setAttribute("lang", "und");
            keyCode.setAttribute("dir", "auto");
            keyCode.textContent = maskSecret(record.api_key);
            keyCell.appendChild(keyCode);
            row.appendChild(keyCell);

            const statusCell = document.createElement("td");
            const status = document.createElement("span");
            status.className = record.disabled ? "badge-disabled" : "badge-active";
            status.dataset.keyField = "status";
            statusCell.appendChild(status);
            row.appendChild(statusCell);

            for (const [field, className] of [
                ["spentBudget", ""],
                ["reset", "muted-inline"],
                ["rates", ""],
                ["allowed", ""],
                ["lastUsed", ""],
            ]) {
                const cell = document.createElement("td");
                cell.dataset.keyField = field;
                cell.className = className;
                if (field === "allowed" && (record.allowed_models || []).length) {
                    cell.setAttribute("lang", "und");
                    cell.setAttribute("dir", "auto");
                }
                row.appendChild(cell);
            }

            const actions = document.createElement("td");
            actions.className = "key-actions";
            for (const action of ["edit", "delete"]) {
                const button = document.createElement("button");
                button.dataset.action = action;
                actions.appendChild(button);
            }
            row.appendChild(actions);
            updateRowLocale(row, record);
            body.appendChild(row);
        }
        table.append(head, body);
        tableWrap.appendChild(table);
        keysArea.replaceChildren(tableWrap);
        keysArea.querySelectorAll("button[data-action]").forEach((btn) => {
            btn.addEventListener("click", (event) => {
                const tr = event.currentTarget.closest("tr");
                const keyId = Number(tr.getAttribute("data-key-id"));
                const action = event.currentTarget.getAttribute("data-action");
                const record = currentKeys.find((k) => k.id === keyId);
                if (!record) return;
                if (action === "edit") openEditModal(record);
                if (action === "delete") deleteKey(record);
            });
        });
    }

    let currentKeys = [];

    async function loadKeys() {
        try {
            const response = await apiFetch("/v1/admin/api-keys");
            if (!response.ok) {
                await showResponseError(response);
                return;
            }
            const data = await response.json();
            currentKeys = data.keys || [];
            renderKeysTable(currentKeys);
        } catch (error) {
            console.error(error);
            showNetworkError(error);
        }
    }

    function catalogModelsForSelection() {
        return Array.from(new Set([...availableModels, ...selectedAllowedModels])).sort();
    }

    function createModelCatalogCopy(key) {
        const copy = document.createElement("span");
        copy.className = "hint model-catalog-copy";
        copy.setAttribute("role", "status");
        copy.setAttribute("aria-live", "polite");
        copy.setAttribute("aria-atomic", "true");
        copy.textContent = i18n.t(`api_keys:${key}`);
        return copy;
    }

    function renderModelCatalog() {
        allowedModelsList.dataset.modelCatalogState = modelCatalogState;
        if (destroyed || dialogController.state !== "open" || i18n.currentLocale === null) {
            return;
        }

        if (modelCatalogState === "loading") {
            allowedModelsList.replaceChildren(createModelCatalogCopy("modelCatalogLoading"));
            return;
        }
        if (modelCatalogState === "error") {
            const retryButton = document.createElement("button");
            retryButton.id = "retryModelsBtn";
            retryButton.type = "button";
            retryButton.textContent = i18n.t("api_keys:modelCatalogRetry");
            retryButton.addEventListener("click", () => {
                void loadAvailableModels({retry: true});
            });
            allowedModelsList.replaceChildren(
                createModelCatalogCopy("modelCatalogError"),
                retryButton,
            );
            return;
        }
        if (modelCatalogState !== "ready") return;

        const models = catalogModelsForSelection();
        if (!models.length) {
            allowedModelsList.replaceChildren(createModelCatalogCopy("modelCatalogEmpty"));
            return;
        }
        const fields = models.map((model) => {
            const label = document.createElement("label");
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.value = model;
            checkbox.checked = selectedAllowedModels.has(model);
            label.append(checkbox, document.createTextNode(` ${model}`));
            return label;
        });
        allowedModelsList.replaceChildren(...fields);
    }

    function rerenderModelCatalogCopy() {
        if (destroyed || dialogController.state !== "open" || i18n.currentLocale === null) {
            return;
        }
        const copy = allowedModelsList.querySelector(".model-catalog-copy");
        if (copy) {
            const key = modelCatalogState === "loading"
                ? "modelCatalogLoading"
                : modelCatalogState === "error"
                    ? "modelCatalogError"
                    : "modelCatalogEmpty";
            copy.textContent = i18n.t(`api_keys:${key}`);
        }
        const retryButton = allowedModelsList.querySelector("#retryModelsBtn");
        if (retryButton) retryButton.textContent = i18n.t("api_keys:modelCatalogRetry");
    }

    function captureLocaleViewState() {
        const activeElement = document.activeElement;
        const hasSelection = typeof activeElement?.selectionStart === "number";
        return {
            activeElement,
            selectionStart: hasSelection ? activeElement.selectionStart : null,
            selectionEnd: hasSelection ? activeElement.selectionEnd : null,
            windowX: window.scrollX,
            windowY: window.scrollY,
            tableWrap: keysArea.querySelector(".keys-table-wrap"),
            tableLeft: keysArea.querySelector(".keys-table-wrap")?.scrollLeft || 0,
            tableTop: keysArea.querySelector(".keys-table-wrap")?.scrollTop || 0,
            modalTop: modalOverlay.querySelector(".modal").scrollTop,
            catalogTop: allowedModelsList.scrollTop,
        };
    }

    function restoreLocaleViewState(state) {
        if (state.activeElement?.isConnected) {
            state.activeElement.focus({preventScroll: true});
            if (state.selectionStart !== null && state.activeElement.setSelectionRange) {
                state.activeElement.setSelectionRange(state.selectionStart, state.selectionEnd);
            }
        }
        if (state.tableWrap?.isConnected) {
            state.tableWrap.scrollLeft = state.tableLeft;
            state.tableWrap.scrollTop = state.tableTop;
        }
        modalOverlay.querySelector(".modal").scrollTop = state.modalTop;
        allowedModelsList.scrollTop = state.catalogTop;
        window.scrollTo(state.windowX, state.windowY);
    }

    function rerenderLocale() {
        if (destroyed || i18n.currentLocale === null) return;
        const viewState = captureLocaleViewState();
        updateKeysTableLocale();
        modalTitle.textContent = modalTitleName === null
            ? i18n.t("api_keys:modal.createTitle")
            : i18n.t("api_keys:modal.editTitle", {name: modalTitleName});
        rerenderModelCatalogCopy();
        messageStatus.rerender();
        restoreLocaleViewState(viewState);
    }

    function loadAvailableModels({retry = false} = {}) {
        if (destroyed) return Promise.resolve();
        if (modelCatalogState === "loading" && modelCatalogPromise !== null) {
            return modelCatalogPromise;
        }
        if (modelCatalogState === "ready" || (modelCatalogState === "error" && !retry)) {
            return Promise.resolve();
        }

        modelCatalogState = "loading";
        renderModelCatalog();
        const request = (async () => {
            try {
                const response = await apiFetch("/v1/models");
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();
                const list = (data && (data.data || data.models)) || [];
                const models = list
                    .map((item) => item.id || item.name || item)
                    .filter((model) => typeof model === "string" && model);
                if (destroyed) return;
                availableModels = Array.from(new Set(models)).sort();
                modelCatalogState = "ready";
                renderModelCatalog();
            } catch (_error) {
                if (destroyed) return;
                modelCatalogState = "error";
                renderModelCatalog();
            } finally {
                if (modelCatalogPromise === request) modelCatalogPromise = null;
            }
        })();
        modelCatalogPromise = request;
        return request;
    }

    function readAllowedModels() {
        return Array.from(selectedAllowedModels).sort();
    }

    function openCreateModal() {
        modalFocusOrigin = {kind: "create", keyId: null};
        editingKeyId = null;
        modalTitleName = null;
        modalTitle.textContent = i18n.t("api_keys:modal.createTitle");
        editOnlyFields.style.display = "none";
        newKeyNotice.style.display = "none";
        fieldName.value = "";
        fieldBudget.value = "";
        fieldBudgetPeriod.value = "none";
        fieldRpm.value = "";
        fieldTpm.value = "";
        fieldMetadata.value = "";
        fieldDisabled.checked = false;
        fieldResetSpent.checked = false;
        selectedAllowedModels = new Set();
        dialogController.open();
        renderModelCatalog();
        void loadAvailableModels();
    }

    function openEditModal(record) {
        modalFocusOrigin = {kind: "edit", keyId: record.id};
        editingKeyId = record.id;
        modalTitleName = record.name;
        modalTitle.textContent = i18n.t("api_keys:modal.editTitle", {name: modalTitleName});
        editOnlyFields.style.display = "flex";
        newKeyNotice.style.display = "block";
        newKeyValue.textContent = record.api_key || "";
        fieldName.value = record.name;
        fieldBudget.value = record.budget_usd != null ? record.budget_usd : "";
        fieldBudgetPeriod.value = record.budget_period || "none";
        fieldRpm.value = record.rpm != null ? record.rpm : "";
        fieldTpm.value = record.tpm != null ? record.tpm : "";
        fieldMetadata.value = record.metadata ? JSON.stringify(record.metadata, null, 2) : "";
        fieldDisabled.checked = !!record.disabled;
        fieldResetSpent.checked = false;
        selectedAllowedModels = new Set(record.allowed_models || []);
        dialogController.open();
        renderModelCatalog();
        void loadAvailableModels();
        void revealFullApiKey(record.id);
    }

    async function revealFullApiKey(keyId) {
        // The list response only carries a masked api_key; fetch the single
        // record to reveal the real value on demand while the modal is open.
        try {
            const response = await apiFetch(`/v1/admin/api-keys/${keyId}`);
            if (!response.ok) {
                await showResponseError(response);
                return;
            }
            const fullRecord = await response.json();
            if (editingKeyId !== keyId) return;
            newKeyValue.textContent = fullRecord.api_key || "";
        } catch (error) {
            console.error(error);
            showNetworkError(error);
        }
    }

    function closeModal(reason = "cancel") {
        dialogController.close(reason);
    }

    function parseOptionalNumber(value) {
        if (value === null || value === undefined || String(value).trim() === "") return null;
        const n = Number(value);
        if (Number.isNaN(n)) {
            throw new ApiKeysValidationError("api_keys:validation.invalidNumber", String(value));
        }
        return n;
    }

    function parseOptionalInt(value) {
        if (value === null || value === undefined || String(value).trim() === "") return null;
        const n = parseInt(value, 10);
        if (Number.isNaN(n)) {
            throw new ApiKeysValidationError("api_keys:validation.invalidInteger", String(value));
        }
        return n;
    }

    function parseMetadata(value) {
        const trimmed = value.trim();
        if (!trimmed) return null;
        let parsed;
        try {
            parsed = JSON.parse(trimmed);
        } catch (error) {
            throw new ApiKeysValidationError(
                "api_keys:validation.invalidMetadata",
                error.message,
            );
        }
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
            throw new ApiKeysValidationError("api_keys:validation.metadataObject");
        }
        return parsed;
    }

    async function saveKey() {
        saveKeyBtn.disabled = true;
        try {
            const name = fieldName.value.trim();
            if (!name) throw new ApiKeysValidationError("api_keys:validation.nameRequired");
            const payload = {
                name,
                budget_usd: parseOptionalNumber(fieldBudget.value),
                budget_period: fieldBudgetPeriod.value,
                rpm: parseOptionalInt(fieldRpm.value),
                tpm: parseOptionalInt(fieldTpm.value),
                allowed_models: readAllowedModels(),
                metadata: parseMetadata(fieldMetadata.value),
            };
            let response;
            if (editingKeyId === null) {
                response = await apiFetch("/v1/admin/api-keys", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify(payload),
                });
            } else {
                payload.disabled = fieldDisabled.checked;
                payload.reset_spent = fieldResetSpent.checked;
                response = await apiFetch(`/v1/admin/api-keys/${editingKeyId}`, {
                    method: "PATCH",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify(payload),
                });
            }
            if (!response.ok) {
                await showResponseError(response);
                return;
            }
            const data = await response.json();
            if (editingKeyId === null) {
                newKeyNotice.style.display = "block";
                newKeyValue.textContent = data.api_key;
                editingKeyId = data.id;
                modalTitleName = data.name;
                modalTitle.textContent = i18n.t("api_keys:modal.editTitle", {name: modalTitleName});
                editOnlyFields.style.display = "flex";
                publishStatus({key: "api_keys:status.created", values: {name: data.name}});
            } else {
                publishStatus({key: "api_keys:status.updated", values: {name: data.name}});
                await loadKeys();
                closeModal("submit");
                return;
            }
            await loadKeys();
        } catch (error) {
            if (error instanceof ApiKeysValidationError) {
                publishStatus(
                    {key: error.key},
                    true,
                    {rawDetail: error.rawDetail},
                );
            } else {
                showNetworkError(error);
            }
        } finally {
            saveKeyBtn.disabled = false;
        }
    }

    async function deleteKey(record) {
        if (!confirm(i18n.t("api_keys:confirm.delete", {name: record.name}))) return;
        try {
            const response = await apiFetch(`/v1/admin/api-keys/${record.id}`, {method: "DELETE"});
            if (!response.ok && response.status !== 204) {
                await showResponseError(response);
                return;
            }
            publishStatus({key: "api_keys:status.deleted", values: {name: record.name}});
            await loadKeys();
        } catch (error) {
            showNetworkError(error);
        }
    }

    createKeyBtn.addEventListener("click", openCreateModal);
    refreshKeysBtn.addEventListener("click", loadKeys);
    saveKeyBtn.addEventListener("click", saveKey);
    cancelKeyBtn.setAttribute("data-dialog-close", "cancel");
    allowedModelsList.addEventListener("change", (event) => {
        const checkbox = event.target.closest('input[type="checkbox"]');
        if (!checkbox || !allowedModelsList.contains(checkbox)) return;
        if (checkbox.checked) selectedAllowedModels.add(checkbox.value);
        else selectedAllowedModels.delete(checkbox.value);
    });

    Theme.attachToggle("darkModeToggle");

    window.addEventListener("beforeunload", () => {
        destroyed = true;
        unsubscribeLocale?.();
    });

    void i18n.ready.then(
        () => {
            if (destroyed) return;
            unsubscribeLocale = i18n.subscribe(rerenderLocale);
            renderModelCatalog();
            void loadKeys();
        },
        () => undefined,
    );
})();
