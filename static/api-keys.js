(function () {
    const apiFetch = window.gatewayAuth.apiFetch;

    const keysArea = document.getElementById("keysArea");
    const keyUsagePanel = document.getElementById("keyUsagePanel");
    const messageArea = document.getElementById("messageArea");

    const modalOverlay = document.getElementById("keyModal");
    const modalTitle = document.getElementById("modalTitle");
    const fieldName = document.getElementById("fieldName");
    const fieldBudget = document.getElementById("fieldBudget");
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
    let currentUsageKeyId = null;
    let availableModels = [];

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

    function formatTimestamp(value) {
        const timestamp = typeof value === "string" ? value.trim() : "";
        if (!timestamp) return "—";
        return timestamp.split(".")[0] || timestamp;
    }

    function formatResolvedTarget(row) {
        const provider = typeof row?.provider === "string" ? row.provider.trim() : "";
        const model = typeof row?.model === "string" ? row.model.trim() : "";
        if (provider && model) return `${provider}/${model}`;
        return model || provider || "—";
    }

    function sumMetric(rows, metric) {
        return rows.reduce((total, row) => total + (Number(row[metric]) || 0), 0);
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
                : '<span style="opacity:0.6">all</span>';
            const lastUsed = k.last_used_at ? escapeHtml(k.last_used_at.replace("T", " ").slice(0, 19)) : "—";
            const keyCode = `<code>${escapeHtml(k.api_key)}</code>`;
            return `
                <tr data-key-id="${k.id}">
                    <td><strong>${escapeHtml(k.name)}</strong><br><small>id ${k.id}</small></td>
                    <td>${keyCode}</td>
                    <td>${statusBadge}</td>
                    <td>${spent} / ${budget}</td>
                    <td>${rpm} / ${tpm}</td>
                    <td>${allowed}</td>
                    <td>${lastUsed}</td>
                    <td class="key-actions">
                        <button data-action="edit">Edit</button>
                        <button data-action="usage">Usage</button>
                        <button data-action="delete">Delete</button>
                    </td>
                </tr>
            `;
        }).join("");
        keysArea.innerHTML = `
            <table class="keys-table">
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Key</th>
                        <th>Status</th>
                        <th>Spent / Budget, $</th>
                        <th>RPM / TPM</th>
                        <th>Allowed Models</th>
                        <th>Last used</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
        keysArea.querySelectorAll("button[data-action]").forEach((btn) => {
            btn.addEventListener("click", (event) => {
                const tr = event.currentTarget.closest("tr");
                const keyId = Number(tr.getAttribute("data-key-id"));
                const action = event.currentTarget.getAttribute("data-action");
                const record = currentKeys.find((k) => k.id === keyId);
                if (!record) return;
                if (action === "edit") openEditModal(record);
                if (action === "usage") loadKeyUsage(record);
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
            if (currentUsageKeyId !== null && !currentKeys.some((k) => k.id === currentUsageKeyId)) {
                hideKeyUsage();
            }
        } catch (error) {
            console.error(error);
            showMessage(`Failed to load keys: ${error.message}`, true);
        }
    }

    function hideKeyUsage() {
        currentUsageKeyId = null;
        keyUsagePanel.classList.remove("visible");
        keyUsagePanel.innerHTML = "";
    }

    function renderKeyUsageLoading(record) {
        keyUsagePanel.classList.add("visible");
        keyUsagePanel.innerHTML = `
            <div class="key-usage-header">
                <div>
                    <h2>Usage for ${escapeHtml(record.name)}</h2>
                    <small>id ${escapeHtml(record.id)} · ${escapeHtml(record.api_key)}</small>
                </div>
            </div>
            <p>Loading usage statistics...</p>
        `;
    }

    function renderKeyUsageError(record, message) {
        keyUsagePanel.classList.add("visible");
        keyUsagePanel.innerHTML = `
            <div class="key-usage-header">
                <div>
                    <h2>Usage for ${escapeHtml(record.name)}</h2>
                    <small>id ${escapeHtml(record.id)} · ${escapeHtml(record.api_key)}</small>
                </div>
                <div class="key-usage-actions">
                    <button data-usage-action="close">Close</button>
                </div>
            </div>
            <p class="error">${escapeHtml(message)}</p>
        `;
        keyUsagePanel.querySelector('button[data-usage-action="close"]').addEventListener("click", hideKeyUsage);
    }

    function renderKeyUsage(record, statsRows, recordsPayload) {
        const stats = Array.isArray(statsRows) ? statsRows : [];
        const records = Array.isArray(recordsPayload?.records) ? recordsPayload.records : [];
        const totalRecords = recordsPayload?.total_records ?? records.length;
        const requests = sumMetric(stats, "count");
        const totalTokens = sumMetric(stats, "total_tokens");
        const promptTokens = sumMetric(stats, "prompt_tokens");
        const completionTokens = sumMetric(stats, "completion_tokens");
        const cost = sumMetric(stats, "cost");
        const costSaved = sumMetric(stats, "cost_saved");
        const recordRows = records.length
            ? records.map((row) => `
                <tr>
                    <td>${escapeHtml(formatTimestamp(row.timestamp))}</td>
                    <td>${escapeHtml(row.operation || "—")}</td>
                    <td>${escapeHtml(row.gateway_model || "—")}</td>
                    <td>${escapeHtml(formatResolvedTarget(row))}</td>
                    <td>${escapeHtml(formatInteger(row.total_tokens))}</td>
                    <td>${escapeHtml(formatNumber(row.cost))}</td>
                    <td>${escapeHtml(formatInteger(row.duration_ms))}</td>
                </tr>
            `).join("")
            : '<tr><td colspan="7">No usage records for this key.</td></tr>';

        keyUsagePanel.classList.add("visible");
        keyUsagePanel.innerHTML = `
            <div class="key-usage-header">
                <div>
                    <h2>Usage for ${escapeHtml(record.name)}</h2>
                    <small>id ${escapeHtml(record.id)} · ${escapeHtml(record.api_key)}</small>
                </div>
                <div class="key-usage-actions">
                    <button data-usage-action="refresh">Refresh</button>
                    <button data-usage-action="close">Close</button>
                </div>
            </div>
            <div class="key-usage-summary">
                <div class="key-usage-metric"><span>Stored records</span><strong>${escapeHtml(formatInteger(totalRecords))}</strong></div>
                <div class="key-usage-metric"><span>Requests, last 12 months</span><strong>${escapeHtml(formatInteger(requests))}</strong></div>
                <div class="key-usage-metric"><span>Total tokens</span><strong>${escapeHtml(formatInteger(totalTokens))}</strong></div>
                <div class="key-usage-metric"><span>Prompt / completion</span><strong>${escapeHtml(formatInteger(promptTokens))} / ${escapeHtml(formatInteger(completionTokens))}</strong></div>
                <div class="key-usage-metric"><span>Cost, $</span><strong>${escapeHtml(formatNumber(cost))}</strong></div>
                <div class="key-usage-metric"><span>Cost saved, $</span><strong>${escapeHtml(formatNumber(costSaved))}</strong></div>
            </div>
            <h3>Latest usage records</h3>
            <div class="key-usage-records">
                <table>
                    <thead>
                        <tr>
                            <th>Timestamp</th>
                            <th>Operation</th>
                            <th>Gateway Model</th>
                            <th>Resolved Model</th>
                            <th>Total Tokens</th>
                            <th>Cost ($)</th>
                            <th>Duration (ms)</th>
                        </tr>
                    </thead>
                    <tbody>${recordRows}</tbody>
                </table>
            </div>
        `;
        keyUsagePanel.querySelector('button[data-usage-action="refresh"]').addEventListener("click", () => {
            const latestRecord = currentKeys.find((key) => key.id === record.id) || record;
            loadKeyUsage(latestRecord);
        });
        keyUsagePanel.querySelector('button[data-usage-action="close"]').addEventListener("click", hideKeyUsage);
    }

    async function loadKeyUsage(record) {
        currentUsageKeyId = record.id;
        renderKeyUsageLoading(record);
        try {
            const keyId = encodeURIComponent(record.id);
            const [statsResponse, recordsResponse] = await Promise.all([
                apiFetch(`/v1/api/usage-stats/month?api_key_id=${keyId}`),
                apiFetch(`/v1/api/usage-records?limit=25&offset=0&api_key_id=${keyId}`),
            ]);
            const statsData = await statsResponse.json().catch(() => null);
            const recordsData = await recordsResponse.json().catch(() => null);
            if (!statsResponse.ok) {
                throw new Error(statsData?.detail || `HTTP ${statsResponse.status}`);
            }
            if (!recordsResponse.ok) {
                throw new Error(recordsData?.detail || `HTTP ${recordsResponse.status}`);
            }
            renderKeyUsage(record, statsData, recordsData);
        } catch (error) {
            console.error(error);
            renderKeyUsageError(record, `Failed to load usage: ${error.message}`);
            showMessage(`Failed to load usage: ${error.message}`, true);
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
        editingKeyId = null;
        modalTitle.textContent = "Create API Key";
        editOnlyFields.style.display = "none";
        newKeyNotice.style.display = "none";
        fieldName.value = "";
        fieldBudget.value = "";
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
        editingKeyId = record.id;
        modalTitle.textContent = `Edit: ${record.name}`;
        editOnlyFields.style.display = "flex";
        newKeyNotice.style.display = "block";
        newKeyValue.textContent = record.api_key;
        fieldName.value = record.name;
        fieldBudget.value = record.budget_usd != null ? record.budget_usd : "";
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

    Theme.attachToggle("darkModeToggle");

    (async function init() {
        await loadAvailableModels();
        await loadKeys();
    })();
})();
