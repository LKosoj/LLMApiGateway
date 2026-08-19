const ROUTER_COST_HINTS = ['free', 'cheap', 'standard', 'premium'];

export function registerRouter(ctx) {

    function setRouterFallbackIndexOptions(select, gatewayModel, selectedIndex) {
        const chain = Array.isArray(ctx.state.routerFallbackChains[gatewayModel]) ? ctx.state.routerFallbackChains[gatewayModel] : [];
        const currentValue = selectedIndex !== undefined && selectedIndex !== null ? String(selectedIndex) : '';
        select.textContent = '';
        const placeholderOption = document.createElement('option');
        placeholderOption.value = '';
        ctx.bindLocalizedText(placeholderOption, 'editor:placeholders.selectFallbackEntry');
        select.appendChild(placeholderOption);

        chain.forEach(entry => {
            const option = document.createElement('option');
            option.value = String(entry.index);
            ctx.bindLocalizedValue(option, () => {
                const unknown = ctx.t('editor:messages.unknown');
                return `${entry.index} · ${entry.provider || unknown}/${entry.model || unknown}`;
            });
            select.appendChild(option);
        });

        if (currentValue && !chain.some(entry => String(entry.index) === currentValue)) {
            const staleOption = document.createElement('option');
            staleOption.value = currentValue;
            ctx.bindLocalizedText(staleOption, 'editor:placeholders.notConfigured', () => ({value: currentValue}));
            staleOption.dataset.stale = 'true';
            select.appendChild(staleOption);
        }
        select.value = currentValue;
    }

    function buildRouterTargetRow(initialData) {
        const data = initialData || {};
        const row = document.createElement('div');
        row.className = 'fallback-row router-target-row';

        const fieldsGrid = document.createElement('div');
        fieldsGrid.className = 'fallback-row-grid router-target-grid';

        const typeSelect = ctx.createSelect('router-target-type-select');
        ctx.setSelectOptions(typeSelect, ['gateway_model', 'fallback_entry'], 'Choose target type', data.type || 'gateway_model');

        const gatewayTargetSelect = ctx.createSelect('router-gateway-target-select');
        ctx.setModelSelectOptions(gatewayTargetSelect, ctx.state.gatewayModelCatalog.chat, data.model || '');
        const gatewayTargetGroup = ctx.createFieldGroup('Gateway Target', gatewayTargetSelect, 'router-gateway-target-field');
        ctx.appendFieldHint(gatewayTargetGroup, 'editor:hints.routerGatewayTarget');

        const fallbackGatewaySelect = ctx.createSelect('router-fallback-gateway-select');
        ctx.setModelSelectOptions(fallbackGatewaySelect, ctx.state.gatewayModelCatalog.chat, data.gateway_model || '');
        const fallbackGatewayGroup = ctx.createFieldGroup('Fallback Gateway', fallbackGatewaySelect, 'router-fallback-gateway-field');
        ctx.appendFieldHint(fallbackGatewayGroup, 'editor:hints.routerFallbackGateway');

        const fallbackIndexSelect = ctx.createSelect('router-fallback-index-select');
        setRouterFallbackIndexOptions(fallbackIndexSelect, data.gateway_model || '', data.index);
        const fallbackIndexGroup = ctx.createFieldGroup('Start At Entry', fallbackIndexSelect, 'router-fallback-index-field');
        ctx.appendFieldHint(fallbackIndexGroup, 'editor:hints.routerFallbackEntry');

        const descriptionInput = ctx.createTextInput('router-target-description-input', 'What this target is best at');
        descriptionInput.value = data.description || '';
        const descriptionGroup = ctx.createFieldGroup('Target Description', descriptionInput, 'router-target-description-field');
        ctx.appendFieldHint(descriptionGroup, 'editor:hints.routerTargetDescription');

        const costHintSelect = ctx.createSelect('router-target-cost-hint-select');
        ctx.setSelectOptions(costHintSelect, ROUTER_COST_HINTS, 'No cost hint', data.cost_hint || '');
        const costHintGroup = ctx.createFieldGroup('Cost Hint', costHintSelect, 'router-target-cost-hint-field');
        ctx.appendFieldHint(costHintGroup, 'editor:hints.routerCostHint');

        fieldsGrid.appendChild(ctx.createFieldGroup('Target Type', typeSelect, 'router-target-type-field'));
        fieldsGrid.appendChild(gatewayTargetGroup);
        fieldsGrid.appendChild(fallbackGatewayGroup);
        fieldsGrid.appendChild(fallbackIndexGroup);
        fieldsGrid.appendChild(descriptionGroup);
        fieldsGrid.appendChild(costHintGroup);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, 'Remove Target');
        removeButton.addEventListener('click', () => {
            row.remove();
        });
        const rowActions = document.createElement('div');
        rowActions.className = 'fallback-row-actions';
        rowActions.appendChild(removeButton);

        const syncVisibility = () => {
            const isFallbackEntry = typeSelect.value === 'fallback_entry';
            gatewayTargetGroup.style.display = isFallbackEntry ? 'none' : '';
            fallbackGatewayGroup.style.display = isFallbackEntry ? '' : 'none';
            fallbackIndexGroup.style.display = isFallbackEntry ? '' : 'none';
        };
        typeSelect.addEventListener('change', syncVisibility);
        fallbackGatewaySelect.addEventListener('change', () => {
            setRouterFallbackIndexOptions(fallbackIndexSelect, fallbackGatewaySelect.value, '');
        });
        syncVisibility();

        row.appendChild(fieldsGrid);
        row.appendChild(rowActions);
        return row;
    }

    function buildRouterCard(initialData) {
        const data = initialData || {};
        const card = document.createElement('section');
        card.className = 'rule-card router-card collapsed';

        const cardHeader = document.createElement('div');
        cardHeader.className = 'rule-card-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'rule-card-title';
        const gatewayModelInput = ctx.createTextInput('gateway-model-input', 'llmgateway/router');
        gatewayModelInput.value = data.gateway_model_name || '';
        titleWrap.appendChild(ctx.createFieldGroup('Gateway Model Name', gatewayModelInput, 'gateway-model-field'));

        const selectorSelect = ctx.createSelect('router-selector-model-select');
        ctx.setModelSelectOptions(selectorSelect, ctx.state.gatewayModelCatalog.chat, data.selector_model || '');
        const selectorField = ctx.createFieldGroup('Selector Model', selectorSelect, 'router-selector-model-field');
        ctx.appendFieldHint(selectorField, 'editor:hints.routerSelector');
        titleWrap.appendChild(selectorField);

        const headerLeft = document.createElement('div');
        headerLeft.className = 'rule-card-header-left';
        headerLeft.appendChild(ctx.createAccordionToggle(card));
        headerLeft.appendChild(titleWrap);

        const removeButton = document.createElement('button');
        removeButton.type = 'button';
        removeButton.className = 'icon-button danger-button';
        ctx.bindKnownActionText(removeButton, 'Remove Model');
        removeButton.addEventListener('click', () => {
            card.remove();
            ctx.refreshRouterEmptyState();
        });

        cardHeader.appendChild(headerLeft);
        cardHeader.appendChild(removeButton);

        const cardBody = document.createElement('div');
        cardBody.className = 'rule-card-body';

        const routingPolicyInput = ctx.createTextarea('router-routing-policy-input', 'How the selector should compare candidates');
        routingPolicyInput.value = data.routing_policy || '';
        const routingPolicyGroup = ctx.createFieldGroup('Routing Policy', routingPolicyInput, 'router-routing-policy-field');
        ctx.appendFieldHint(routingPolicyGroup, 'editor:hints.routerRoutingPolicy');
        cardBody.appendChild(routingPolicyGroup);

        const targetsList = document.createElement('div');
        targetsList.className = 'fallback-list router-target-list';

        const addTargetButton = document.createElement('button');
        addTargetButton.type = 'button';
        addTargetButton.className = 'secondary-button add-fallback-button';
        ctx.bindKnownActionText(addTargetButton, 'Add Target');
        addTargetButton.addEventListener('click', () => {
            targetsList.appendChild(buildRouterTargetRow({ type: 'gateway_model' }));
        });

        const targets = Array.isArray(data.targets) ? data.targets : [];
        targets.forEach(target => {
            targetsList.appendChild(buildRouterTargetRow(target));
        });
        if (targets.length === 0) {
            targetsList.appendChild(buildRouterTargetRow({ type: 'gateway_model' }));
        }

        cardBody.appendChild(ctx.buildFusionSectionHeading('editor:sections.router.targetsHeading'));
        cardBody.appendChild(targetsList);
        cardBody.appendChild(addTargetButton);
        card.appendChild(cardHeader);
        card.appendChild(cardBody);
        return card;
    }

    function routerTargetHints(row) {
        const hints = {};
        const description = row.querySelector('.router-target-description-input').value.trim();
        if (description) {
            hints.description = description;
        }
        const costHint = row.querySelector('.router-target-cost-hint-select').value.trim();
        if (costHint) {
            hints.cost_hint = costHint;
        }
        return hints;
    }

    function normalizeRouterTargetRow(row, gatewayModelName) {
        const type = row.querySelector('.router-target-type-select').value.trim();
        const hints = routerTargetHints(row);
        if (type === 'gateway_model') {
            const model = row.querySelector('.router-gateway-target-select').value.trim();
            if (!model) {
                throw new Error(`Router model '${gatewayModelName}' has a gateway target without a model.`);
            }
            return { type, model, ...hints };
        }
        if (type === 'fallback_entry') {
            const gatewayModel = row.querySelector('.router-fallback-gateway-select').value.trim();
            const indexRaw = row.querySelector('.router-fallback-index-select').value.trim();
            if (!gatewayModel) {
                throw new Error(`Router model '${gatewayModelName}' has a fallback-entry target without a gateway model.`);
            }
            if (indexRaw === '') {
                throw new Error(`Router model '${gatewayModelName}' has a fallback-entry target without an entry index.`);
            }
            const index = Number.parseInt(indexRaw, 10);
            if (!Number.isFinite(index) || index < 0) {
                throw new Error(`Router model '${gatewayModelName}' has an invalid fallback-entry index.`);
            }
            return { type, gateway_model: gatewayModel, index, ...hints };
        }
        throw new Error(`Router model '${gatewayModelName}' has unsupported target type '${type}'.`);
    }

    function normalizeRouterCardForSave(card) {
        const gatewayModelName = card.querySelector('.gateway-model-input').value.trim();
        if (!gatewayModelName) {
            throw new Error('Each router model must have a gateway model name.');
        }

        const selectorModel = card.querySelector('.router-selector-model-select').value.trim();
        if (!selectorModel) {
            throw new Error(`Router model '${gatewayModelName}' must have a selector model.`);
        }

        const targetRows = Array.from(card.querySelectorAll('.router-target-list > .router-target-row'));
        const targets = targetRows.map(row => normalizeRouterTargetRow(row, gatewayModelName));
        if (targets.length === 0) {
            throw new Error(`Router model '${gatewayModelName}' must have at least one target.`);
        }
        const payload = {
            gateway_model_name: gatewayModelName,
            selector_model: selectorModel,
            targets,
        };
        const routingPolicy = card.querySelector('.router-routing-policy-input').value.trim();
        if (routingPolicy) {
            payload.routing_policy = routingPolicy;
        }
        return payload;
    }

    function getRouterPayloadForSave() {
        const rules = Array.from(ctx.elements.routerList.querySelectorAll('.router-card')).map(normalizeRouterCardForSave);
        return { rules };
    }

    function getNormalizedRouterContent() {
        return ctx.stableSerialize(getRouterPayloadForSave());
    }

    async function renderRouter(rules) {
        ctx.elements.routerList.textContent = '';
        if (Array.isArray(rules)) {
            rules.forEach(rule => {
                ctx.elements.routerList.appendChild(buildRouterCard(rule));
            });
        }
        ctx.refreshRouterEmptyState();
    }

    async function loadRouterEditor() {
        try {
            const loaded = await ctx.loadConfigDocument(
                'router',
                '/v1/config/router-rules/structured',
                {
                    validate: ctx.validateRouterPayload,
                    apply: async payload => {
                        ctx.state.gatewayModelCatalog.chat = payload.chat_models || [];
                        ctx.state.routerFallbackChains = payload.fallback_chains || {};
                        await renderRouter(payload.rules);
                    },
                }
            );
            if (!loaded) {
                ctx.showLocalizedMessage('warning', 'A newer local edit was preserved. Reload again to discard it.');
                return false;
            }
            ctx.state.originalRouterContent = getNormalizedRouterContent();
            ctx.updateSaveButtonDisabledState();
            ctx.showLocalizedMessage('success', 'Router Models loaded successfully.');
            return true;
        } catch (error) {
            console.error('Error fetching Router Models:', error);
            ctx.showLocalizedError('Error loading Router Models:', error);
            ctx.state.originalRouterContent = null;
            ctx.updateSaveButtonDisabledState();
            return false;
        }
    }

    async function saveRouter() {
        ctx.elements.saveButton.disabled = true;
        ctx.showLocalizedMessage('info', 'Saving Router Models...');

        let payload;
        try {
            payload = getRouterPayloadForSave();
        } catch (error) {
            ctx.showClientValidationError(error);
            return;
        }

        try {
            const result = await ctx.saveConfigDocument(
                'router',
                '/v1/config/router-rules/structured',
                payload,
                {
                    errorTitle: 'Error saving Router Models:',
                    validatePublished: ctx.validateRouterPayload,
                }
            );
            if (!result) {
                return;
            }
            if (ctx.state.editorMutationVersion === result.submittedMutationVersion) {
                ctx.state.gatewayModelCatalog.chat = result.payload.chat_models || [];
                ctx.state.routerFallbackChains = result.payload.fallback_chains || {};
                const application = renderRouter(result.payload.rules);
                ctx.syncInteractionLock();
                await application;
                ctx.state.originalRouterContent = getNormalizedRouterContent();
            }
            ctx.showLocalizedMessage(
                'success',
                ctx.safeSuccessMessage(result.body, 'Router Models updated successfully.')
            );
        } catch (error) {
            console.error('Error saving Router:', error);
            ctx.showLocalizedError('Error saving Router Models:', error);
        } finally {
            ctx.updateSaveButtonDisabledState();
        }
    }


    Object.assign(ctx, {
        setRouterFallbackIndexOptions,
        buildRouterTargetRow,
        buildRouterCard,
        routerTargetHints,
        normalizeRouterTargetRow,
        normalizeRouterCardForSave,
        getRouterPayloadForSave,
        getNormalizedRouterContent,
        renderRouter,
        loadRouterEditor,
        saveRouter,
    });
}
