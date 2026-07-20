/* Pricing UI — editor + calculator */
(function () {
    'use strict';

    // State: array of {provider, model, input_rate, output_rate, source,
    // default_cost_per_request, _dirty, _manual}
    let state = [];
    let baseEtag = null;
    let dirty = false;
    let editVersion = 0;
    let loading = true;
    let saving = false;
    let calculationSnapshot = null;
    let unsubscribeLocale = null;
    let detailIdx = null;

    // ---------- DOM references ----------
    const tableBody = document.getElementById('pricingTableBody');
    const emptyState = document.getElementById('pricingEmptyState');
    const addRowBtn = document.getElementById('addRowBtn');
    const saveBtn = document.getElementById('saveBtn');
    const calcModelSel = document.getElementById('calcModel');
    const calcPromptInput = document.getElementById('calcPromptTokens');
    const calcCompletionInput = document.getElementById('calcCompletionTokens');
    const calcBtn = document.getElementById('calcBtn');
    const calcResult = document.getElementById('calcResult');
    const calcCostValue = document.getElementById('calcCostValue');
    const calcModelValue = document.getElementById('calcModelValue');
    const calcInputRateValue = document.getElementById('calcInputRateValue');
    const calcOutputRateValue = document.getElementById('calcOutputRateValue');
    const toast = document.getElementById('toast');
    const rawDetail = document.getElementById('pricingRawDetail');
    const conflictState = document.getElementById('pricingConflictState');
    const reloadPricingBtn = document.getElementById('reloadPricingBtn');
    const detailSheet = document.getElementById('pricingDetailSheet');
    const detailBackdrop = document.getElementById('pricingDetailBackdrop');
    const detailClose = document.getElementById('pricingDetailClose');
    const detailProvider = document.getElementById('pricingDetailProvider');
    const detailModel = document.getElementById('pricingDetailModel');
    const detailInputRate = document.getElementById('pricingDetailInputRate');
    const detailOutputRate = document.getElementById('pricingDetailOutputRate');
    const { apiFetch } = window.gatewayAuth;
    const i18n = window.gatewayI18n;

    // ---------- Theme ----------
    Theme.attachToggle('darkModeToggle');

    // ---------- Toast ----------
    const toastStatus = window.gatewayUi.createStatus(toast, {
        defaultTimeoutMs: 3000,
        rawDetailElement: rawDetail,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });

    function publishStatus(message, isError, publishOptions = {}) {
        if (isError) {
            toastStatus.error(message, publishOptions);
        } else {
            toastStatus.polite(message, publishOptions);
        }
    }

    function showApiError(payload, response) {
        const descriptor = window.gatewayUi.describeApiError(payload, {
            status: response.status,
            requestId: response.headers.get('X-Request-ID'),
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

    class PricingContractError extends Error {
        constructor(key) {
            super(key);
            this.key = key;
        }
    }

    function markDirty() {
        dirty = true;
        editVersion += 1;
    }

    function syncEditorDisabled() {
        const disabled = loading || saving || baseEtag === null;
        addRowBtn.disabled = disabled;
        saveBtn.disabled = disabled;
        tableBody.querySelectorAll('input, button').forEach((control) => {
            control.disabled = disabled || control.dataset.forceDisabled === 'true';
        });
        reloadPricingBtn.disabled = loading || saving;
    }

    // A row is editable once it carries (or has been given) a real rate:
    // an operator-configured rate, an OpenRouter-autofilled rate, or a rate
    // the user just chose to set manually (see makeSetManualPriceCell). Rows
    // still reporting one of the other statuses stay text-only until then.
    function isEditableRow(item) {
        return (
            item._manual === true
            || !item.source
            || item.source === 'configured'
            || item.source === 'openrouter_autofill'
        );
    }

    function formatOperationDefaultNote(amount) {
        return i18n.t('pricing:editor.status.operationDefault', {
            amount: i18n.formatCurrency(Number(amount), 'USD', {
                minimumFractionDigits: 2,
                maximumFractionDigits: 6,
            }),
        });
    }

    function showConflict() {
        conflictState.hidden = false;
        conflictState.focus();
    }

    function clearConflict() {
        conflictState.hidden = true;
    }

    async function parsePricingResponse(response) {
        const etag = response.headers.get('ETag');
        if (!etag) {
            throw new PricingContractError('pricing:contract.missingEtag');
        }
        const data = await response.json();
        if (!data || !Array.isArray(data.items)) {
            throw new PricingContractError('pricing:contract.invalidShape');
        }
        return { etag, items: data.items };
    }

    // ---------- Render table ----------
    function renderTable() {
        tableBody.replaceChildren();
        const hasRows = state.length > 0;
        emptyState.classList.toggle('visible', !hasRows);

        state.forEach((item, idx) => {
            const tr = document.createElement('tr');
            if (item._dirty) { tr.classList.add('dirty'); }

            if (!isEditableRow(item)) {
                tr.classList.add('pricing-row-readonly');

                const providerTd = document.createElement('td');
                providerTd.textContent = item.provider || '';
                tr.appendChild(providerTd);

                const modelTd = document.createElement('td');
                modelTd.textContent = item.model || '';
                tr.appendChild(modelTd);

                const noteTd = document.createElement('td');
                noteTd.colSpan = 2;
                noteTd.className = 'pricing-source-note';
                if (item.source === 'operation_default') {
                    noteTd.setAttribute('data-pricing-default-cost', String(item.default_cost_per_request));
                    noteTd.textContent = formatOperationDefaultNote(item.default_cost_per_request);
                } else if (item.source === 'awaiting_openrouter_catalog') {
                    noteTd.setAttribute('data-i18n', 'pricing:editor.status.awaitingCatalog');
                    noteTd.textContent = i18n.t('pricing:editor.status.awaitingCatalog');
                } else {
                    noteTd.setAttribute('data-i18n', 'pricing:editor.status.upstreamOnly');
                    noteTd.textContent = i18n.t('pricing:editor.status.upstreamOnly');
                }
                tr.appendChild(noteTd);

                const actionTd = document.createElement('td');
                const manualBtn = document.createElement('button');
                manualBtn.type = 'button';
                manualBtn.className = 'secondary-button pricing-set-manual';
                manualBtn.setAttribute('data-i18n', 'pricing:editor.setManualPrice');
                manualBtn.textContent = i18n.t('pricing:editor.setManualPrice');
                if (item.source === 'awaiting_openrouter_catalog') {
                    manualBtn.dataset.forceDisabled = 'true';
                    manualBtn.disabled = true;
                }
                manualBtn.addEventListener('click', () => {
                    state[idx]._manual = true;
                    state[idx].input_rate = 0;
                    state[idx].output_rate = 0;
                    state[idx]._dirty = true;
                    markDirty();
                    renderTable();
                    rebuildCalcSelect();
                });
                actionTd.appendChild(manualBtn);
                tr.appendChild(actionTd);

                tableBody.appendChild(tr);
                return;
            }

            const makeCell = (field, labelKey, type = 'text') => {
                const td = document.createElement('td');
                const input = document.createElement('input');
                input.type = type;
                input.setAttribute('data-i18n-aria-label', labelKey);
                input.setAttribute('aria-label', i18n.t(labelKey));
                input.value = item[field] !== undefined ? item[field] : '';
                if (type === 'number') {
                    input.min = '0';
                    input.step = 'any';
                }
                input.addEventListener('input', () => {
                    const val = type === 'number'
                        ? (input.value === '' ? Number.NaN : Number(input.value))
                        : input.value;
                    state[idx][field] = val;
                    state[idx]._dirty = true;
                    tr.classList.add('dirty');
                    markDirty();
                });
                td.appendChild(input);
                return td;
            };

            tr.appendChild(makeCell('provider', 'pricing:editor.table.provider'));
            const modelTd = makeCell('model', 'pricing:editor.table.model');
            if (item.source === 'openrouter_autofill') {
                const badge = document.createElement('span');
                badge.className = 'pricing-badge';
                badge.setAttribute('data-i18n', 'pricing:editor.autofillBadge');
                badge.textContent = i18n.t('pricing:editor.autofillBadge');
                modelTd.appendChild(badge);
            }
            tr.appendChild(modelTd);
            tr.appendChild(makeCell(
                'input_rate',
                'pricing:editor.table.inputRate',
                'number',
            ));
            tr.appendChild(makeCell(
                'output_rate',
                'pricing:editor.table.outputRate',
                'number',
            ));

            const tdDel = document.createElement('td');
            const delBtn = document.createElement('button');
            delBtn.type = 'button';
            delBtn.className = 'btn-delete';
            delBtn.setAttribute('data-i18n', 'pricing:editor.table.delete');
            delBtn.textContent = i18n.t('pricing:editor.table.delete');
            delBtn.addEventListener('click', () => {
                state.splice(idx, 1);
                markDirty();
                renderTable();
                rebuildCalcSelect();
            });
            tdDel.appendChild(delBtn);

            const expandBtn = document.createElement('button');
            expandBtn.type = 'button';
            expandBtn.className = 'btn-expand';
            expandBtn.setAttribute('data-i18n-aria-label', 'pricing:detail.open');
            expandBtn.setAttribute('aria-label', i18n.t('pricing:detail.open'));
            expandBtn.addEventListener('click', () => openDetailSheet(idx));
            tdDel.appendChild(expandBtn);

            tr.appendChild(tdDel);

            tableBody.appendChild(tr);
        });
        syncEditorDisabled();
    }

    // ---------- Rebuild calculator model select ----------
    function rebuildCalcSelect() {
        const prev = calcModelSel.value;
        calcModelSel.replaceChildren();
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.setAttribute('data-i18n', 'pricing:calculator.selectModel');
        placeholder.textContent = i18n.t('pricing:calculator.selectModel');
        calcModelSel.appendChild(placeholder);
        state.filter(isEditableRow).forEach((item) => {
            const opt = document.createElement('option');
            const key = `${item.provider}::${item.model}`;
            opt.value = key;
            opt.textContent = `${item.provider} / ${item.model}`;
            calcModelSel.appendChild(opt);
        });
        if (prev) { calcModelSel.value = prev; }
    }

    // ---------- Load pricing ----------
    async function loadPricing() {
        const requestEditVersion = editVersion;
        loading = true;
        syncEditorDisabled();
        try {
            const resp = await apiFetch('/v1/admin/pricing');
            if (!resp.ok) {
                await showResponseError(resp);
                return;
            }
            const loaded = await parsePricingResponse(resp);
            if (editVersion !== requestEditVersion) {
                return;
            }
            state = loaded.items.map(item => Object.assign({}, item, { _dirty: false }));
            baseEtag = loaded.etag;
            dirty = false;
            editVersion += 1;
            clearConflict();
            renderTable();
            rebuildCalcSelect();
        } catch (err) {
            if (err instanceof PricingContractError) {
                publishStatus({key: err.key}, true);
            } else {
                showNetworkError(err);
            }
        } finally {
            loading = false;
            syncEditorDisabled();
        }
    }

    // ---------- Save pricing ----------
    saveBtn.addEventListener('click', async () => {
        if (!baseEtag) {
            publishStatus({key: 'pricing:status.mustLoad'}, true);
            return;
        }

        // Validate (rows the gateway reports as used-but-unpriced are read-only
        // until the operator explicitly opts in via "Set manual price", so they
        // never block Save and are never sent below).
        for (let i = 0; i < state.length; i++) {
            const item = state[i];
            if (!isEditableRow(item)) { continue; }
            if (!item.provider || !String(item.provider).trim()) {
                publishStatus({
                    key: 'pricing:validation.providerRequired',
                    values: {row: i + 1},
                }, true);
                return;
            }
            if (!item.model || !String(item.model).trim()) {
                publishStatus({
                    key: 'pricing:validation.modelRequired',
                    values: {row: i + 1},
                }, true);
                return;
            }
            if (!Number.isFinite(Number(item.input_rate)) || Number(item.input_rate) < 0) {
                publishStatus({
                    key: 'pricing:validation.inputRateInvalid',
                    values: {row: i + 1},
                }, true);
                return;
            }
            if (!Number.isFinite(Number(item.output_rate)) || Number(item.output_rate) < 0) {
                publishStatus({
                    key: 'pricing:validation.outputRateInvalid',
                    values: {row: i + 1},
                }, true);
                return;
            }
        }

        const payload = {
            items: state.filter(isEditableRow).map(item => ({
                provider: String(item.provider).trim(),
                model: String(item.model).trim(),
                input_rate: Number(item.input_rate),
                output_rate: Number(item.output_rate),
            })),
        };

        try {
            saving = true;
            syncEditorDisabled();
            const submittedVersion = editVersion;
            const resp = await apiFetch('/v1/admin/pricing', {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    'If-Match': baseEtag,
                },
                body: JSON.stringify(payload),
            });
            if (resp.status === 409) {
                showConflict();
                return;
            }
            if (!resp.ok) {
                await showResponseError(resp);
                return;
            }
            const saved = await parsePricingResponse(resp);
            baseEtag = saved.etag;
            clearConflict();
            if (editVersion === submittedVersion) {
                state = saved.items.map(item => Object.assign({}, item, { _dirty: false }));
                dirty = false;
                editVersion += 1;
                renderTable();
                rebuildCalcSelect();
            }
            publishStatus({key: 'pricing:status.saved'});
        } catch (err) {
            if (err instanceof PricingContractError) {
                publishStatus({key: err.key}, true);
            } else {
                showNetworkError(err);
            }
        } finally {
            saving = false;
            syncEditorDisabled();
        }
    });

    // ---------- Add row ----------
    addRowBtn.addEventListener('click', () => {
        state.push({ provider: '', model: '', input_rate: 0, output_rate: 0, _dirty: true });
        markDirty();
        renderTable();
        rebuildCalcSelect();
        // Focus on first input of new row
        const rows = tableBody.querySelectorAll('tr');
        if (rows.length > 0) {
            const lastRow = rows[rows.length - 1];
            const firstInput = lastRow.querySelector('input');
            if (firstInput) { firstInput.focus(); }
        }
    });

    reloadPricingBtn.addEventListener('click', async () => {
        await loadPricing();
    });

    window.addEventListener('beforeunload', (event) => {
        if (!dirty) { return; }
        event.preventDefault();
        event.returnValue = '';
    });

    window.addEventListener('pagehide', () => {
        if (unsubscribeLocale) {
            unsubscribeLocale();
            unsubscribeLocale = null;
        }
    });

    // ---------- Calculator ----------
    function formatRate(rate) {
        return i18n.t('pricing:calculator.result.ratePerMillion', {
            amount: i18n.formatNumber(Number(rate), {
                minimumFractionDigits: 0,
                maximumFractionDigits: 6,
            }),
        });
    }

    function renderCalculationResult() {
        if (calculationSnapshot === null) {
            calcResult.hidden = true;
            return;
        }
        calcCostValue.textContent = i18n.formatCurrency(
            Number(calculationSnapshot.costUsd),
            'USD',
            {minimumFractionDigits: 6, maximumFractionDigits: 6},
        );
        calcModelValue.textContent = (
            `${calculationSnapshot.provider} / ${calculationSnapshot.model}`
        );
        calcInputRateValue.textContent = formatRate(calculationSnapshot.inputRate);
        calcOutputRateValue.textContent = formatRate(calculationSnapshot.outputRate);
        calcResult.hidden = false;
    }

    function renderLocalizedState() {
        const windowX = window.scrollX;
        const windowY = window.scrollY;
        toastStatus.rerender();
        renderCalculationResult();
        // operation_default rows interpolate a formatted currency amount into
        // their status note; the global data-i18n rebind only re-applies
        // static per-root text, so the interpolated copy is re-rendered here.
        tableBody.querySelectorAll('[data-pricing-default-cost]').forEach((td) => {
            td.textContent = formatOperationDefaultNote(
                td.getAttribute('data-pricing-default-cost'),
            );
        });
        window.requestAnimationFrame(() => window.scrollTo(windowX, windowY));
    }

    calcBtn.addEventListener('click', async () => {
        const key = calcModelSel.value;
        if (!key) {
            publishStatus({key: 'pricing:status.selectModel'}, true);
            return;
        }
        const [provider, model] = key.split('::');
        const promptTokens = parseInt(calcPromptInput.value, 10) || 0;
        const completionTokens = parseInt(calcCompletionInput.value, 10) || 0;

        try {
            calcBtn.disabled = true;
            const resp = await apiFetch('/v1/admin/pricing/calculate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    provider,
                    model,
                    prompt_tokens: promptTokens,
                    completion_tokens: completionTokens,
                }),
            });
            if (!resp.ok) {
                await showResponseError(resp);
                calculationSnapshot = null;
                renderCalculationResult();
                return;
            }
            const data = await resp.json();
            calculationSnapshot = {
                provider,
                model,
                costUsd: data.cost_usd,
                inputRate: data.input_rate,
                outputRate: data.output_rate,
            };
            renderCalculationResult();
        } catch (err) {
            showNetworkError(err);
            calculationSnapshot = null;
            renderCalculationResult();
        } finally {
            calcBtn.disabled = false;
        }
    });

    // ---------- Row detail sheet (mobile) ----------
    function syncDetailFields() {
        if (detailIdx === null) { return; }
        const item = state[detailIdx];
        detailProvider.textContent = item.provider || '—';
        detailModel.textContent = item.model || '—';
        detailInputRate.value = item.input_rate !== undefined ? item.input_rate : '';
        detailOutputRate.value = item.output_rate !== undefined ? item.output_rate : '';
    }

    function openDetailSheet(idx) {
        detailIdx = idx;
        syncDetailFields();
        detailSheet.hidden = false;
        detailClose.focus();
    }

    function closeDetailSheet() {
        detailSheet.hidden = true;
        detailIdx = null;
    }

    function applyDetailRate(field, input) {
        const val = input.value === '' ? Number.NaN : Number(input.value);
        state[detailIdx][field] = val;
        state[detailIdx]._dirty = true;
        markDirty();
        renderTable();
    }

    detailInputRate.addEventListener('input', () => applyDetailRate('input_rate', detailInputRate));
    detailOutputRate.addEventListener('input', () => applyDetailRate('output_rate', detailOutputRate));
    detailClose.addEventListener('click', closeDetailSheet);
    detailBackdrop.addEventListener('click', closeDetailSheet);
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !detailSheet.hidden) { closeDetailSheet(); }
    });

    // ---------- Init ----------
    async function init() {
        await i18n.ready;
        i18n.bind(document);
        unsubscribeLocale = i18n.subscribe(renderLocalizedState);
        await loadPricing();
    }

    syncEditorDisabled();
    init().catch(() => undefined);
})();
