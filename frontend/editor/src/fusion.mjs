export function registerFusion(ctx) {

    const FUSION_PANEL_MAX = 8;

    function buildFusionMemberRow(initialData, options) {
        options = options || {};
        const data = initialData || {};
        const row = document.createElement('div');
        row.className = 'fallback-row fusion-member-row';

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid';

        const providerSelect = ctx.createSelect('provider-select');
        ctx.setSelectOptions(providerSelect, ctx.state.availableProviders, 'Choose a provider', data.provider || '');

        const modelInput = ctx.createTextInput('model-input', 'Choose or enter model');
        modelInput.value = data.model || '';
        const dataListId = `fusion-models-list-${Math.random().toString(36).substr(2, 9)}`;
        modelInput.setAttribute('list', dataListId);
        const dataList = document.createElement('datalist');
        dataList.id = dataListId;
        row.appendChild(dataList);

        const temperatureInput = document.createElement('input');
        temperatureInput.type = 'number';
        temperatureInput.className = 'fusion-temperature-input';
        temperatureInput.min = '0';
        temperatureInput.max = '2';
        temperatureInput.step = '0.1';
        ctx.bindKnownPlaceholder(temperatureInput, 'default');
        if (data.temperature !== undefined && data.temperature !== null) {
            temperatureInput.value = data.temperature;
        }

        const maxTokensInput = ctx.createNumberInput('fusion-max-tokens-input', 'default');
        if (data.max_completion_tokens !== undefined && data.max_completion_tokens !== null) {
            maxTokensInput.value = data.max_completion_tokens;
        }

        const reasoningInput = ctx.createTextarea('fusion-reasoning-input', '{"effort": "medium"}');
        reasoningInput.value = data.reasoning ? ctx.normalizeObjectTextarea(data.reasoning) : '';

        fieldsGrid.appendChild(ctx.createFieldGroup('Provider', providerSelect, 'provider-field'));
        fieldsGrid.appendChild(ctx.createFieldGroup('Model', modelInput, 'model-field'));
        fieldsGrid.appendChild(ctx.createFieldGroup('Temperature', temperatureInput));

        const modelStatus = document.createElement('div');
        modelStatus.className = 'model-status';
        modelStatus.dataset.state = 'idle';

        const advancedDetails = document.createElement('details');
        advancedDetails.className = 'advanced-options';
        const advancedSummary = document.createElement('summary');
        ctx.bindLocalizedText(advancedSummary, 'editor:actions.advanced');
        advancedDetails.appendChild(advancedSummary);
        const advancedGrid = document.createElement('div');
        advancedGrid.className = 'advanced-grid';
        advancedGrid.appendChild(ctx.createFieldGroup('Max Completion Tokens', maxTokensInput));
        advancedGrid.appendChild(ctx.createFieldGroup('Reasoning (JSON)', reasoningInput, 'textarea-group'));
        advancedDetails.appendChild(advancedGrid);

        row.appendChild(fieldsGrid);
        row.appendChild(modelStatus);
        row.appendChild(advancedDetails);

        if (options.removable) {
            const removeButton = document.createElement('button');
            removeButton.type = 'button';
            removeButton.className = 'icon-button danger-button';
            ctx.bindKnownActionText(removeButton, options.removeLabel || 'Remove Panel Model');
            removeButton.addEventListener('click', () => {
                row.remove();
            });
            const rowActions = document.createElement('div');
            rowActions.className = 'fallback-row-actions';
            rowActions.appendChild(removeButton);
            row.appendChild(rowActions);
        }

        ctx.createLazyProviderCatalogRowController({
            row,
            providerSelect,
            modelControl: modelInput,
            dataList,
            modelStatus,
        });
        return row;
    }

    function buildFusionSectionHeading(key) {
        const heading = document.createElement('div');
        heading.className = 'fusion-section-heading';
        ctx.bindLocalizedText(heading, key);
        return heading;
    }

    function buildFusionWebToolsSection(initialWebTools) {
        const data = initialWebTools || null;
        const wrap = document.createElement('div');
        wrap.className = 'fusion-web-tools';

        const enableLabel = document.createElement('label');
        enableLabel.className = 'field-group fusion-include-details';
        const enableCheckbox = document.createElement('input');
        enableCheckbox.type = 'checkbox';
        enableCheckbox.className = 'fusion-web-tools-enabled';
        enableCheckbox.checked = Boolean(data);
        const enableText = document.createElement('span');
        enableText.className = 'field-label';
        ctx.bindLocalizedText(enableText, 'editor:toggles.fusionWeb');
        enableLabel.appendChild(enableCheckbox);
        enableLabel.appendChild(enableText);

        const fields = document.createElement('div');
        fields.className = 'fusion-web-tools-fields fallback-list';

        const searchModelInput = ctx.createTextInput('fusion-web-search-model', 'gateway web_search model (e.g. llmgateway/web-search)');
        searchModelInput.value = data && data.search_model ? data.search_model : '';
        const readModelInput = ctx.createTextInput('fusion-web-read-model', 'gateway web_read model (optional — enables web_fetch)');
        readModelInput.value = data && data.read_model ? data.read_model : '';
        const maxToolCallsInput = ctx.createNumberInput('fusion-web-max-tool-calls', '6');
        maxToolCallsInput.value = data && data.max_tool_calls != null ? data.max_tool_calls : '';
        const maxIterationsInput = ctx.createNumberInput('fusion-web-max-iterations', '4');
        maxIterationsInput.value = data && data.max_iterations != null ? data.max_iterations : '';
        const maxResultsInput = ctx.createNumberInput('fusion-web-max-results', '5');
        maxResultsInput.value = data && data.max_results != null ? data.max_results : '';

        fields.appendChild(ctx.createFieldGroup('Search model (required)', searchModelInput));
        fields.appendChild(ctx.createFieldGroup('Read model (optional)', readModelInput));
        fields.appendChild(ctx.createFieldGroup('Max tool calls per panel model', maxToolCallsInput));
        fields.appendChild(ctx.createFieldGroup('Max iterations per panel model', maxIterationsInput));
        fields.appendChild(ctx.createFieldGroup('Max results per search', maxResultsInput));

        const syncVisibility = () => {
            fields.style.display = enableCheckbox.checked ? '' : 'none';
        };
        enableCheckbox.addEventListener('change', syncVisibility);
        syncVisibility();

        wrap.appendChild(enableLabel);
        wrap.appendChild(fields);
        return wrap;
    }

    function buildFusionCard(initialData) {
        const data = initialData || {};
        const card = document.createElement('section');
        card.className = 'rule-card fusion-card';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';
        const gatewayModelInput = ctx.createTextInput('gateway-model-input', 'llmgateway/fusion-quality');
        gatewayModelInput.value = data.gateway_model_name || '';
        titleWrap.appendChild(ctx.createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, 'Remove Model');
        removeButton.addEventListener('click', () => {
            card.remove();
            ctx.refreshFusionEmptyState();
        });

        const accordionToggle = document.createElement('button');
        accordionToggle.type = 'button';
        accordionToggle.className = 'accordion-toggle';
        const svgNS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('width', '20');
        svg.setAttribute('height', '20');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        const polyline = document.createElementNS(svgNS, 'polyline');
        polyline.setAttribute('points', '6 9 12 15 18 9');
        svg.appendChild(polyline);
        accordionToggle.appendChild(svg);
        accordionToggle.addEventListener('click', () => {
            ctx.toggleProviderCatalogCard(card);
        });

        const headerLeft = document.createElement('div');
        headerLeft.className = 'rule-card-header-left';
        headerLeft.appendChild(accordionToggle);
        headerLeft.appendChild(titleWrap);
        cardHeader.appendChild(headerLeft);
        cardHeader.appendChild(removeButton);

        const cardBody = document.createElement('div');
        cardBody.className = 'rule-card-body';

        const mainList = document.createElement('div');
        mainList.className = 'fallback-list fusion-main-list';
        const mainRow = buildFusionMemberRow(data.main_model || {}, { removable: false });
        mainList.appendChild(mainRow);

        const judgeList = document.createElement('div');
        judgeList.className = 'fallback-list fusion-judge-list';
        const judgeRow = buildFusionMemberRow(data.judge_model || {}, { removable: false });
        judgeList.appendChild(judgeRow);

        const panelListEl = document.createElement('div');
        panelListEl.className = 'fallback-list fusion-panel-list';

        const addPanelButton = document.createElement('button');
        addPanelButton.type = 'button';
        addPanelButton.className = 'secondary-button add-fallback-button';
        ctx.bindKnownActionText(addPanelButton, 'Add Panel Model');
        addPanelButton.addEventListener('click', () => {
            if (panelListEl.children.length >= FUSION_PANEL_MAX) {
                ctx.showLocalizedMessage('error', `A Fusion panel can have at most ${FUSION_PANEL_MAX} models.`);
                return;
            }
            panelListEl.appendChild(buildFusionMemberRow({}, { removable: true }));
        });

        const panelMembers = Array.isArray(data.panel) ? data.panel : [];
        panelMembers.forEach(member => {
            const memberRow = buildFusionMemberRow(member, { removable: true });
            panelListEl.appendChild(memberRow);
        });
        if (panelMembers.length === 0) {
            const memberRow = buildFusionMemberRow({}, { removable: true });
            panelListEl.appendChild(memberRow);
        }

        const reserveListEl = document.createElement('div');
        reserveListEl.className = 'fallback-list fusion-reserve-list';

        const addReserveButton = document.createElement('button');
        addReserveButton.type = 'button';
        addReserveButton.className = 'secondary-button add-fallback-button';
        ctx.bindKnownActionText(addReserveButton, 'Add Reserve Model');
        addReserveButton.addEventListener('click', () => {
            if (reserveListEl.children.length >= FUSION_PANEL_MAX) {
                ctx.showLocalizedMessage('error', `A Fusion reserve can have at most ${FUSION_PANEL_MAX} models.`);
                return;
            }
            reserveListEl.appendChild(buildFusionMemberRow({}, { removable: true, removeLabel: 'Remove Reserve Model' }));
        });

        const reserveMembers = Array.isArray(data.reserve) ? data.reserve : [];
        reserveMembers.forEach(member => {
            const memberRow = buildFusionMemberRow(member, { removable: true, removeLabel: 'Remove Reserve Model' });
            reserveListEl.appendChild(memberRow);
        });

        const includeDetailsLabel = document.createElement('label');
        includeDetailsLabel.className = 'field-group fusion-include-details';
        const includeDetailsCheckbox = document.createElement('input');
        includeDetailsCheckbox.type = 'checkbox';
        includeDetailsCheckbox.className = 'fusion-include-details-input';
        includeDetailsCheckbox.checked = data.include_details_default !== false;
        const includeDetailsText = document.createElement('span');
        includeDetailsText.className = 'field-label';
        ctx.bindLocalizedText(includeDetailsText, 'editor:toggles.fusionFull');
        includeDetailsLabel.appendChild(includeDetailsCheckbox);
        includeDetailsLabel.appendChild(includeDetailsText);

        cardBody.appendChild(buildFusionSectionHeading('editor:sections.fusion.mainHeading'));
        cardBody.appendChild(mainList);
        cardBody.appendChild(buildFusionSectionHeading('editor:sections.fusion.judgeHeading'));
        cardBody.appendChild(judgeList);
        cardBody.appendChild(buildFusionSectionHeading('editor:sections.fusion.panelHeading'));
        cardBody.appendChild(panelListEl);
        cardBody.appendChild(addPanelButton);
        const reserveHeading = buildFusionSectionHeading('editor:sections.fusion.reserveHeading');
        ctx.appendFieldHint(reserveHeading, 'editor:sections.fusion.reserveHint');
        cardBody.appendChild(reserveHeading);
        cardBody.appendChild(reserveListEl);
        cardBody.appendChild(addReserveButton);
        cardBody.appendChild(includeDetailsLabel);
        cardBody.appendChild(buildFusionSectionHeading('editor:sections.fusion.webHeading'));
        cardBody.appendChild(buildFusionWebToolsSection(data.web_tools));

        card.classList.add('collapsed');
        card.appendChild(cardHeader);
        card.appendChild(cardBody);

        return card;
    }

    function normalizeFusionMemberRow(row, settings) {
        const required = settings.required;
        const roleLabel = settings.roleLabel;
        const providerSelect = row.querySelector('.provider-select');
        const modelInput = row.querySelector('.model-input');
        const temperatureInput = row.querySelector('.fusion-temperature-input');
        const maxTokensInput = row.querySelector('.fusion-max-tokens-input');
        const reasoningInput = row.querySelector('.fusion-reasoning-input');

        const provider = providerSelect.value.trim();
        const model = modelInput.value.trim();

        if (!provider && !model) {
            if (required) {
                throw new Error(`${roleLabel} requires a provider and a model.`);
            }
            return null;
        }
        if (!provider) {
            throw new Error(`${roleLabel} must have a provider selected.`);
        }
        if (!model) {
            throw new Error(`${roleLabel} must have a model for provider '${provider}'.`);
        }

        const member = { provider, model };
        const temperatureRaw = temperatureInput.value.trim();
        if (temperatureRaw !== '') {
            const temperature = Number(temperatureRaw);
            if (Number.isNaN(temperature)) {
                throw new Error(`${roleLabel} has an invalid temperature.`);
            }
            member.temperature = temperature;
        }
        const maxTokensRaw = maxTokensInput.value.trim();
        if (maxTokensRaw !== '') {
            const maxTokens = parseInt(maxTokensRaw, 10);
            if (Number.isNaN(maxTokens)) {
                throw new Error(`${roleLabel} has invalid max completion tokens.`);
            }
            member.max_completion_tokens = maxTokens;
        }
        const reasoningRaw = reasoningInput.value.trim();
        if (reasoningRaw !== '') {
            member.reasoning = ctx.parseObjectTextarea(reasoningInput.value, `${roleLabel} reasoning`);
        }
        return member;
    }

    function normalizeFusionCardForSave(card) {
        const gatewayModelInput = card.querySelector('.gateway-model-input');
        const gatewayModelName = gatewayModelInput.value.trim();
        if (!gatewayModelName) {
            throw new Error('Each fusion model must have a gateway model name.');
        }

        const mainRow = card.querySelector('.fusion-main-list > .fusion-member-row');
        const main_model = normalizeFusionMemberRow(mainRow, {
            required: true,
            roleLabel: `Fusion '${gatewayModelName}' main model`,
        });

        const judgeRow = card.querySelector('.fusion-judge-list > .fusion-member-row');
        const judge_model = normalizeFusionMemberRow(judgeRow, {
            required: false,
            roleLabel: `Fusion '${gatewayModelName}' judge model`,
        });

        const panelRows = Array.from(card.querySelectorAll('.fusion-panel-list > .fusion-member-row'));
        const panel = panelRows
            .map(rowEl => normalizeFusionMemberRow(rowEl, {
                required: true,
                roleLabel: `Fusion '${gatewayModelName}' panel model`,
            }))
            .filter(Boolean);
        if (panel.length === 0) {
            throw new Error(`Fusion model '${gatewayModelName}' must have at least one panel model.`);
        }
        if (panel.length > FUSION_PANEL_MAX) {
            throw new Error(`Fusion model '${gatewayModelName}' can have at most ${FUSION_PANEL_MAX} panel models.`);
        }

        const reserveRows = Array.from(card.querySelectorAll('.fusion-reserve-list > .fusion-member-row'));
        const reserve = reserveRows
            .map(rowEl => normalizeFusionMemberRow(rowEl, {
                required: false,
                roleLabel: `Fusion '${gatewayModelName}' reserve model`,
            }))
            .filter(Boolean);
        if (reserve.length > FUSION_PANEL_MAX) {
            throw new Error(`A Fusion reserve can have at most ${FUSION_PANEL_MAX} models.`);
        }

        const includeDetailsCheckbox = card.querySelector('.fusion-include-details-input');
        const rule = {
            gateway_model_name: gatewayModelName,
            panel,
            main_model,
            include_details_default: includeDetailsCheckbox ? includeDetailsCheckbox.checked : true,
        };
        if (judge_model) {
            rule.judge_model = judge_model;
        }
        // Always send reserve, even [], so a save that clears every reserve
        // row is not silently dropped and the saved payload always matches
        // what the server persists (see the server round-trip below).
        rule.reserve = reserve;

        const webToolsEnabled = card.querySelector('.fusion-web-tools-enabled');
        if (webToolsEnabled && webToolsEnabled.checked) {
            const searchModel = card.querySelector('.fusion-web-search-model').value.trim();
            if (!searchModel) {
                throw new Error(`Fusion model '${gatewayModelName}' web tools require a search model.`);
            }
            const webTools = { search_model: searchModel };
            const readModel = card.querySelector('.fusion-web-read-model').value.trim();
            if (readModel) {
                webTools.read_model = readModel;
            }
            const numericFields = [
                ['.fusion-web-max-tool-calls', 'max_tool_calls', 'max tool calls'],
                ['.fusion-web-max-iterations', 'max_iterations', 'max iterations'],
                ['.fusion-web-max-results', 'max_results', 'max results'],
            ];
            numericFields.forEach(([selector, key, label]) => {
                const raw = card.querySelector(selector).value.trim();
                if (raw === '') {
                    return;
                }
                const value = parseInt(raw, 10);
                if (Number.isNaN(value) || value <= 0) {
                    throw new Error(`Fusion model '${gatewayModelName}' ${label} must be a positive integer.`);
                }
                webTools[key] = value;
            });
            rule.web_tools = webTools;
        }
        return rule;
    }

    function getFusionPayloadForSave() {
        const rules = Array.from(ctx.elements.fusionList.querySelectorAll('.fusion-card')).map(normalizeFusionCardForSave);
        return { rules };
    }

    function getNormalizedFusionContent() {
        return ctx.stableSerialize(getFusionPayloadForSave());
    }

    function renderFusion(rules) {
        ctx.elements.fusionList.textContent = '';
        if (Array.isArray(rules)) {
            rules.forEach(rule => {
                const card = buildFusionCard(rule);
                ctx.elements.fusionList.appendChild(card);
            });
        }
        ctx.refreshFusionEmptyState();
    }

    async function loadFusionEditor() {
        try {
            const loaded = await ctx.loadConfigDocument(
                'fusion',
                '/v1/config/fusion-rules/structured',
                {
                    validate: payload => ctx.validateFusionPayload(payload, true),
                    apply: async payload => {
                        ctx.state.availableProviders = payload.providers;
                        await renderFusion(payload.rules);
                    },
                }
            );
            if (!loaded) {
                ctx.showLocalizedMessage('warning', 'A newer local edit was preserved. Reload again to discard it.');
                return false;
            }
            ctx.state.originalFusionContent = getNormalizedFusionContent();
            ctx.updateSaveButtonDisabledState();
            ctx.showLocalizedMessage('success', 'Fusion Models loaded successfully.');
            return true;
        } catch (error) {
            console.error('Error fetching Fusion Models:', error);
            ctx.showLocalizedError('Error loading Fusion Models:', error);
            ctx.state.originalFusionContent = null;
            ctx.updateSaveButtonDisabledState();
            return false;
        }
    }

    async function saveFusion() {
        ctx.elements.saveButton.disabled = true;
        ctx.showLocalizedMessage('info', 'Saving Fusion Models...');

        let payload;
        try {
            payload = getFusionPayloadForSave();
        } catch (error) {
            ctx.showClientValidationError(error);
            return;
        }

        try {
            const result = await ctx.saveConfigDocument(
                'fusion',
                '/v1/config/fusion-rules/structured',
                payload,
                {
                    errorTitle: 'Error saving Fusion Models:',
                    extractPublishedPayload: body => ({ rules: body.rules }),
                    validatePublished: published => ctx.validateFusionPayload(published, false),
                }
            );
            if (!result) {
                return;
            }
            if (ctx.state.editorMutationVersion === result.submittedMutationVersion) {
                const application = renderFusion(result.payload.rules);
                ctx.syncInteractionLock();
                await application;
                ctx.state.originalFusionContent = getNormalizedFusionContent();
            }
            ctx.showLocalizedMessage(
                'success',
                ctx.safeSuccessMessage(result.body, 'Fusion Models updated successfully.')
            );
        } catch (error) {
            console.error('Error saving Fusion:', error);
            ctx.showLocalizedError('Error saving Fusion Models:', error);
        } finally {
            ctx.updateSaveButtonDisabledState();
        }
    }


    Object.assign(ctx, {
        buildFusionMemberRow,
        buildFusionSectionHeading,
        buildFusionWebToolsSection,
        buildFusionCard,
        normalizeFusionMemberRow,
        normalizeFusionCardForSave,
        getFusionPayloadForSave,
        getNormalizedFusionContent,
        renderFusion,
        loadFusionEditor,
        saveFusion,
    });
}
