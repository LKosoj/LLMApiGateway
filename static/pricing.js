/* Pricing UI — editor + calculator */
(function () {
    'use strict';

    // State: array of {provider, model, input_rate, output_rate, _dirty}
    let state = [];

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
    const toast = document.getElementById('toast');

    // ---------- Dark mode (same as other pages) ----------
    const darkModeToggle = document.getElementById('darkModeToggle');
    if (darkModeToggle) {
        const updateIcon = () => {
            const isDark = document.body.classList.contains('dark-mode');
            darkModeToggle.querySelector('.icon-placeholder').textContent = isDark ? '☀️' : '🌙';
        };
        const savedMode = localStorage.getItem('darkMode');
        if (savedMode === 'true') { document.body.classList.add('dark-mode'); }
        updateIcon();
        darkModeToggle.addEventListener('click', () => {
            document.body.classList.toggle('dark-mode');
            localStorage.setItem('darkMode', document.body.classList.contains('dark-mode'));
            updateIcon();
        });
    }

    // ---------- Toast ----------
    let toastTimer = null;
    function showToast(msg, isError) {
        toast.textContent = msg;
        toast.classList.toggle('error', !!isError);
        toast.classList.add('visible');
        if (toastTimer) { clearTimeout(toastTimer); }
        toastTimer = setTimeout(() => toast.classList.remove('visible'), 3000);
    }

    // ---------- Fetch helper ----------
    async function apiFetch(url, options) {
        const resp = await fetch(url, Object.assign({ credentials: 'include' }, options));
        return resp;
    }

    // ---------- Render table ----------
    function renderTable() {
        tableBody.replaceChildren();
        const hasRows = state.length > 0;
        emptyState.classList.toggle('visible', !hasRows);

        state.forEach((item, idx) => {
            const tr = document.createElement('tr');
            if (item._dirty) { tr.classList.add('dirty'); }

            const makeCell = (field, type) => {
                const td = document.createElement('td');
                const input = document.createElement('input');
                input.type = type || 'text';
                input.value = item[field] !== undefined ? item[field] : '';
                if (type === 'number') {
                    input.min = '0';
                    input.step = 'any';
                }
                input.addEventListener('input', () => {
                    const val = type === 'number' ? parseFloat(input.value) : input.value;
                    state[idx][field] = val;
                    state[idx]._dirty = true;
                    tr.classList.add('dirty');
                });
                td.appendChild(input);
                return td;
            };

            tr.appendChild(makeCell('provider'));
            tr.appendChild(makeCell('model'));
            tr.appendChild(makeCell('input_rate', 'number'));
            tr.appendChild(makeCell('output_rate', 'number'));

            const tdDel = document.createElement('td');
            const delBtn = document.createElement('button');
            delBtn.type = 'button';
            delBtn.className = 'btn-delete';
            delBtn.textContent = 'Delete';
            delBtn.addEventListener('click', () => {
                state.splice(idx, 1);
                renderTable();
                rebuildCalcSelect();
            });
            tdDel.appendChild(delBtn);
            tr.appendChild(tdDel);

            tableBody.appendChild(tr);
        });
    }

    // ---------- Rebuild calculator model select ----------
    function rebuildCalcSelect() {
        const prev = calcModelSel.value;
        calcModelSel.replaceChildren();
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = '— select model —';
        calcModelSel.appendChild(placeholder);
        state.forEach((item) => {
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
        try {
            const resp = await apiFetch('/v1/admin/pricing');
            if (!resp.ok) {
                showToast(`Failed to load pricing: ${resp.status}`, true);
                return;
            }
            const data = await resp.json();
            state = (data.items || []).map(item => Object.assign({}, item, { _dirty: false }));
            renderTable();
            rebuildCalcSelect();
        } catch (err) {
            showToast(`Network error: ${err.message}`, true);
        }
    }

    // ---------- Save pricing ----------
    saveBtn.addEventListener('click', async () => {
        // Validate
        for (let i = 0; i < state.length; i++) {
            const item = state[i];
            if (!item.provider || !String(item.provider).trim()) {
                showToast(`Row ${i + 1}: provider must not be empty`, true);
                return;
            }
            if (!item.model || !String(item.model).trim()) {
                showToast(`Row ${i + 1}: model must not be empty`, true);
                return;
            }
            if (isNaN(item.input_rate) || item.input_rate < 0) {
                showToast(`Row ${i + 1}: input_rate must be >= 0`, true);
                return;
            }
            if (isNaN(item.output_rate) || item.output_rate < 0) {
                showToast(`Row ${i + 1}: output_rate must be >= 0`, true);
                return;
            }
        }

        const payload = {
            items: state.map(item => ({
                provider: String(item.provider).trim(),
                model: String(item.model).trim(),
                input_rate: Number(item.input_rate),
                output_rate: Number(item.output_rate),
            })),
        };

        try {
            saveBtn.disabled = true;
            const resp = await apiFetch('/v1/admin/pricing', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (!resp.ok) {
                const detail = await resp.json().catch(() => ({ detail: resp.statusText }));
                showToast(`Save failed: ${detail.detail || resp.statusText}`, true);
                return;
            }
            const data = await resp.json();
            state = (data.items || []).map(item => Object.assign({}, item, { _dirty: false }));
            renderTable();
            rebuildCalcSelect();
            showToast('Pricing saved successfully');
        } catch (err) {
            showToast(`Network error: ${err.message}`, true);
        } finally {
            saveBtn.disabled = false;
        }
    });

    // ---------- Add row ----------
    addRowBtn.addEventListener('click', () => {
        state.push({ provider: '', model: '', input_rate: 0, output_rate: 0, _dirty: true });
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

    // ---------- Calculator ----------
    calcBtn.addEventListener('click', async () => {
        const key = calcModelSel.value;
        if (!key) {
            showToast('Please select a model', true);
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
                const detail = await resp.json().catch(() => ({ detail: resp.statusText }));
                showToast(`Calculation failed: ${detail.detail || resp.statusText}`, true);
                calcResult.hidden = true;
                return;
            }
            const data = await resp.json();
            calcResult.hidden = false;
            calcResult.replaceChildren();

            const costSpan = document.createElement('span');
            costSpan.className = 'cost-value';
            costSpan.textContent = `$${data.cost_usd.toFixed(6)}`;
            calcResult.appendChild(costSpan);

            const ratesSpan = document.createElement('span');
            ratesSpan.className = 'rates-info';

            const modelLabel = document.createTextNode('Model: ');
            const modelStrong = document.createElement('strong');
            modelStrong.textContent = `${provider} / ${model}`;
            const sep1 = document.createTextNode(' | ');

            const inputLabel = document.createTextNode('Input: ');
            const inputStrong = document.createElement('strong');
            inputStrong.textContent = `$${data.input_rate}/1M`;
            const sep2 = document.createTextNode(' | ');

            const outputLabel = document.createTextNode('Output: ');
            const outputStrong = document.createElement('strong');
            outputStrong.textContent = `$${data.output_rate}/1M`;

            ratesSpan.append(
                modelLabel, modelStrong, sep1,
                inputLabel, inputStrong, sep2,
                outputLabel, outputStrong,
            );
            calcResult.appendChild(ratesSpan);
        } catch (err) {
            showToast(`Network error: ${err.message}`, true);
            calcResult.hidden = true;
        } finally {
            calcBtn.disabled = false;
        }
    });

    // ---------- Init ----------
    loadPricing();
})();
