(function () {
    const apiFetch = window.gatewayAuth.apiFetch;

    const keysArea = document.getElementById("keysArea");
    const messageArea = document.getElementById("messageArea");

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
    let modalReturnFocus = null;

    function showMessage(text, isError = false) {
        messageArea.textContent = text;
        messageArea.style.color = isError ? "#b94a48" : "#2d7a2d";
        if (!text) return;
        setTimeout(() => {
            messageArea.textContent = "";
        }, 5000);
    }

    function formatNumber(value, decimals = 4) {
        if (value === null || value === undefined) return "—";
        return Number(value).toFixed(decimals);
    }

    function formatInteger(value) {
        if (value === null || value === undefined) return "—";
        return Number(value).toLocaleString();
    }

    function escapeHtml(s) {
        if (s === null || s === undefined) return "";
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function maskSecret(value) {
        const text = String(value || "");
        if (!text) return "—";
        if (text.length <= 8) return "••••";
        return `${text.slice(0, 4)}…${text.slice(-4)}`;
    }

    function renderKeysTable(keys) {
        if (!keys.length) {
            keysArea.innerHTML = "<p>No API keys yet. Use <em>Create API Key</em> to add one.</p>";
            return;
        }
        const rows = keys.map((k) => {
            const statusBadge = k.disabled
                ? '<span class="badge-disabled">disabled</span>'
                : '<span class="badge-active">active</span>';
            const budget = k.budget_usd === null || k.budget_usd === undefined
                ? "—"
                : Number(k.budget_usd) < 0
                    ? "unlimited"
                    : formatNumber(k.budget_usd);
            const spent = formatNumber(k.spent_usd);
            const rpm = k.rpm == null ? "—" : k.rpm;
            const tpm = k.tpm == null ? "—" : k.tpm;
            const allowed = k.allowed_models && k.allowed_models.length
                ? k.allowed_models.map(escapeHtml).join(", ")
                : '<span class="muted-inline">all</span>';
            const lastUsed = k.last_used_at ? escapeHtml(k.last_used_at.replace("T", " ").slice(0, 19)) : "—";
            const resetPeriod = (k.budget_period && k.budget_period !== "none") ? escapeHtml(k.budget_period) : '<span class="muted-inline">—</span>';
            const keyCode = `<code class="masked-secret" title="Full key is shown only immediately after creation">${escapeHtml(maskSecret(k.api_key))}</code>`;
            return `
                <tr data-key-id="${k.id}">
                    <td><strong>${escapeHtml(k.name)}</strong><br><small>id ${k.id}</small></td>
                    <td>${keyCode}</td>
                    <td>${statusBadge}</td>
                    <td>${spent} / ${budget}</td>
                    <td>${resetPeriod}</td>
                    <td>${rpm} / ${tpm}</td>
                    <td>${allowed}</td>
                    <td>${lastUsed}</td>
                    <td class="key-actions">
                        <button data-action="edit">Edit</button>
                        <button data-action="delete">Delete</button>
                    </td>
                </tr>
            `;
        }).join("");
        keysArea.innerHTML = `
            <div class="keys-table-wrap">
                <table class="keys-table">
                    <thead>
                        <tr>
                            <th>Name</th>
                            <th>Key</th>
                            <th>Status</th>
                            <th>Spent / Budget, $</th>
                            <th>Reset</th>
                            <th>RPM / TPM</th>
                            <th>Allowed Models</th>
                            <th>Last used</th>
                            <th></th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        `;
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
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            currentKeys = data.keys || [];
            renderKeysTable(currentKeys);
        } catch (error) {
            console.error(error);
            showMessage(`Failed to load keys: ${error.message}`, true);
        }
    }

    async function loadAvailableModels() {
        try {
            const response = await apiFetch("/v1/models");
            if (!response.ok) return;
            const data = await response.json();
            const list = (data && (data.data || data.models)) || [];
            availableModels = list.map((item) => item.id || item.name || item).filter(Boolean);
            availableModels = Array.from(new Set(availableModels)).sort();
        } catch (error) {
            console.warn("Could not load model list:", error);
            availableModels = [];
        }
    }

    function renderAllowedModelsField(selected) {
        const chosen = new Set(selected || []);
        if (!availableModels.length) {
            allowedModelsList.innerHTML = '<span class="hint">No models detected. Use the Rules Editor to add models.</span>';
            return;
        }
        allowedModelsList.innerHTML = availableModels.map((m) => `
            <label><input type="checkbox" value="${escapeHtml(m)}" ${chosen.has(m) ? "checked" : ""}> ${escapeHtml(m)}</label>
        `).join("");
    }

    function readAllowedModels() {
        return Array.from(allowedModelsList.querySelectorAll("input[type=checkbox]:checked"))
            .map((el) => el.value);
    }

    function openCreateModal() {
        modalReturnFocus = document.activeElement;
        editingKeyId = null;
        modalTitle.textContent = "Create API Key";
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
        renderAllowedModelsField([]);
        modalOverlay.classList.add("visible");
        fieldName.focus();
    }

    function openEditModal(record) {
        modalReturnFocus = document.activeElement;
        editingKeyId = record.id;
        modalTitle.textContent = `Edit: ${record.name}`;
        editOnlyFields.style.display = "flex";
        newKeyNotice.style.display = "block";
        newKeyValue.textContent = "Only shown immediately after key creation.";
        fieldName.value = record.name;
        fieldBudget.value = record.budget_usd != null ? record.budget_usd : "";
        fieldBudgetPeriod.value = record.budget_period || "none";
        fieldRpm.value = record.rpm != null ? record.rpm : "";
        fieldTpm.value = record.tpm != null ? record.tpm : "";
        fieldMetadata.value = record.metadata ? JSON.stringify(record.metadata, null, 2) : "";
        fieldDisabled.checked = !!record.disabled;
        fieldResetSpent.checked = false;
        renderAllowedModelsField(record.allowed_models || []);
        modalOverlay.classList.add("visible");
        fieldName.focus();
    }

    function closeModal() {
        modalOverlay.classList.remove("visible");
        if (modalReturnFocus && typeof modalReturnFocus.focus === "function") {
            modalReturnFocus.focus();
        }
        modalReturnFocus = null;
    }

    function getModalFocusableElements() {
        return Array.from(modalOverlay.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        )).filter((element) => !element.disabled && element.offsetParent !== null);
    }

    function handleModalKeydown(event) {
        if (!modalOverlay.classList.contains("visible")) return;
        if (event.key === "Escape") {
            event.preventDefault();
            closeModal();
            return;
        }
        if (event.key !== "Tab") return;
        const focusable = getModalFocusableElements();
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }

    function parseOptionalNumber(value) {
        if (value === null || value === undefined || String(value).trim() === "") return null;
        const n = Number(value);
        if (Number.isNaN(n)) throw new Error(`Invalid number: ${value}`);
        return n;
    }

    function parseOptionalInt(value) {
        if (value === null || value === undefined || String(value).trim() === "") return null;
        const n = parseInt(value, 10);
        if (Number.isNaN(n)) throw new Error(`Invalid integer: ${value}`);
        return n;
    }

    function parseMetadata(value) {
        const trimmed = value.trim();
        if (!trimmed) return null;
        try {
            const parsed = JSON.parse(trimmed);
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
                throw new Error("Metadata must be a JSON object.");
            }
            return parsed;
        } catch (error) {
            throw new Error(`Invalid metadata JSON: ${error.message}`);
        }
    }

    async function saveKey() {
        try {
            const name = fieldName.value.trim();
            if (!name) throw new Error("Name is required.");
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
                const body = await response.json().catch(() => ({}));
                throw new Error(body.detail || `HTTP ${response.status}`);
            }
            const data = await response.json();
            if (editingKeyId === null) {
                newKeyNotice.style.display = "block";
                newKeyValue.textContent = data.api_key;
                editingKeyId = data.id;
                modalTitle.textContent = `Edit: ${data.name}`;
                editOnlyFields.style.display = "flex";
                showMessage(`API key "${data.name}" created.`);
            } else {
                showMessage(`API key "${data.name}" updated.`);
                closeModal();
            }
            await loadKeys();
        } catch (error) {
            showMessage(error.message, true);
        }
    }

    async function deleteKey(record) {
        if (!confirm(`Delete API key "${record.name}"? This cannot be undone.`)) return;
        try {
            const response = await apiFetch(`/v1/admin/api-keys/${record.id}`, {method: "DELETE"});
            if (!response.ok && response.status !== 204) {
                const body = await response.json().catch(() => ({}));
                throw new Error(body.detail || `HTTP ${response.status}`);
            }
            showMessage(`API key "${record.name}" deleted.`);
            await loadKeys();
        } catch (error) {
            showMessage(error.message, true);
        }
    }

    createKeyBtn.addEventListener("click", openCreateModal);
    refreshKeysBtn.addEventListener("click", loadKeys);
    saveKeyBtn.addEventListener("click", saveKey);
    cancelKeyBtn.addEventListener("click", closeModal);
    modalOverlay.addEventListener("click", (event) => {
        if (event.target === modalOverlay) closeModal();
    });
    modalOverlay.addEventListener("keydown", handleModalKeydown);

    Theme.attachToggle("darkModeToggle");

    (async function init() {
        await loadAvailableModels();
        await loadKeys();
    })();
})();
