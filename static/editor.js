/* Generated from frontend/editor/src; do not edit directly. */
(() => {
  // src/fallback.mjs
  function registerFallback(ctx) {
    const ConfigUiError = ctx.ConfigUiError;
    const LocalizedUiError = ctx.LocalizedUiError;
    function clearUnavailableFallbackModelMetadata(fallbackRow) {
      delete fallbackRow.dataset.unavailableModel;
      delete fallbackRow.dataset.unavailableProvider;
    }
    function getUnavailableFallbackModelDetails(fallbackRow) {
      const unavailableModel = fallbackRow.dataset.unavailableModel?.trim();
      if (!unavailableModel) {
        return null;
      }
      const providerSelect = fallbackRow.querySelector(".provider-select");
      const gatewayModelInput = fallbackRow.closest(".rule-card")?.querySelector(".gateway-model-input");
      return {
        model: unavailableModel,
        provider: (fallbackRow.dataset.unavailableProvider || providerSelect?.value || "").trim(),
        gatewayModel: (gatewayModelInput?.value || "").trim()
      };
    }
    function collectUnavailableFallbackModels(containerElement) {
      return Array.from(containerElement.querySelectorAll(".fallback-row")).map(getUnavailableFallbackModelDetails).filter(Boolean);
    }
    function formatUnavailableFallbackModelsDetails(unavailableModels) {
      if (!Array.isArray(unavailableModels) || unavailableModels.length === 0) {
        return "";
      }
      const formatTarget = ({ provider, model }) => provider ? `${provider}.${model}` : `${model}`;
      const groups = /* @__PURE__ */ new Map();
      unavailableModels.forEach((details) => {
        const gatewayModel = (details.gatewayModel || "").trim();
        if (!groups.has(gatewayModel)) {
          groups.set(gatewayModel, /* @__PURE__ */ new Set());
        }
        groups.get(gatewayModel).add(formatTarget(details));
      });
      const formattedGroups = Array.from(groups.entries()).map(([gatewayModel, targets]) => {
        const models = Array.from(targets).join(", ");
        return gatewayModel ? `${gatewayModel}: ${models}` : models;
      });
      return formattedGroups.join("; ");
    }
    function stableSerialize(value) {
      return JSON.stringify(value, null, 2);
    }
    function renderRulesPreview(titleKey, lines, payload) {
      if (!ctx.elements.rulesPreviewArea) return;
      ctx.clearElement(ctx.elements.rulesPreviewArea);
      const heading = document.createElement("strong");
      ctx.bindLocalizedText(heading, titleKey);
      ctx.elements.rulesPreviewArea.appendChild(heading);
      const list = document.createElement("ul");
      const entries = lines.length > 0 ? lines : [{ key: "editor:messages.noChanges", values: {} }];
      entries.forEach(({ key, values = {} }) => {
        const item = document.createElement("li");
        ctx.bindLocalizedText(item, key, values);
        list.appendChild(item);
      });
      ctx.elements.rulesPreviewArea.appendChild(list);
      if (payload) {
        const pre = document.createElement("pre");
        pre.textContent = stableSerialize(payload);
        pre.setAttribute("lang", "und");
        pre.setAttribute("dir", "auto");
        ctx.elements.rulesPreviewArea.appendChild(pre);
      }
      ctx.elements.rulesPreviewArea.hidden = false;
    }
    function routeKey(route) {
      return `${route.provider || ""}/${route.model || ""}`;
    }
    function previewRulesChanges() {
      let currentPayload;
      try {
        currentPayload = getRulesPayloadForSave();
      } catch (error) {
        ctx.showClientValidationError(error);
        return;
      }
      const previousPayload = ctx.state.originalRulesContent ? JSON.parse(ctx.state.originalRulesContent) : { rules: [] };
      const previousByModel = new Map((previousPayload.rules || []).map((rule) => [rule.gateway_model_name, rule]));
      const currentByModel = new Map((currentPayload.rules || []).map((rule) => [rule.gateway_model_name, rule]));
      const lines = [];
      currentByModel.forEach((rule, gatewayModel) => {
        const previousRule = previousByModel.get(gatewayModel);
        if (!previousRule) {
          lines.push({
            key: "editor:preview.added",
            values: { model: gatewayModel }
          });
          return;
        }
        const previousOrder = (previousRule.fallback_models || []).map(routeKey).join(" -> ");
        const currentOrder = (rule.fallback_models || []).map(routeKey).join(" -> ");
        if (previousOrder !== currentOrder) {
          lines.push({
            key: "editor:preview.order",
            values: () => ({
              model: gatewayModel,
              previous: previousOrder || ctx.t("editor:preview.empty"),
              current: currentOrder || ctx.t("editor:preview.empty")
            })
          });
        }
        if (Boolean(previousRule.dynamic_penalty) !== Boolean(rule.dynamic_penalty)) {
          lines.push({
            key: "editor:preview.penalty",
            values: () => ({
              model: gatewayModel,
              previous: ctx.t(Boolean(previousRule.dynamic_penalty) ? "editor:preview.enabled" : "editor:preview.disabled"),
              current: ctx.t(Boolean(rule.dynamic_penalty) ? "editor:preview.enabled" : "editor:preview.disabled")
            })
          });
        }
      });
      previousByModel.forEach((_rule, gatewayModel) => {
        if (!currentByModel.has(gatewayModel)) {
          lines.push({
            key: "editor:preview.removed",
            values: { model: gatewayModel }
          });
        }
      });
      renderRulesPreview("editor:preview.fallbackTitle", lines, currentPayload);
    }
    async function renderSuggestedFallbackOrder() {
      let currentPayload;
      try {
        currentPayload = getRulesPayloadForSave();
      } catch (error) {
        ctx.showClientValidationError(error);
        return;
      }
      try {
        const response = await ctx.apiFetch("/v1/fallback-model-evals");
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || `HTTP ${response.status}`);
        }
        const models = payload.snapshot && Array.isArray(payload.snapshot.models) ? payload.snapshot.models : [];
        const scoreByTarget = new Map(models.map((model) => [`${model.provider}/${model.model}`, Number(model.score) || 0]));
        const suggestions = currentPayload.rules.map((rule) => {
          const currentOrder = rule.fallback_models || [];
          const suggestedOrder = [...currentOrder].sort((left, right) => (scoreByTarget.get(routeKey(right)) || 0) - (scoreByTarget.get(routeKey(left)) || 0));
          return {
            gateway_model_name: rule.gateway_model_name,
            current_order: currentOrder.map(routeKey),
            suggested_order: suggestedOrder.map(routeKey)
          };
        });
        const lines = suggestions.filter((item) => item.current_order.join("|") !== item.suggested_order.join("|")).map((item) => ({
          key: "editor:preview.order",
          values: () => ({
            model: item.gateway_model_name,
            previous: item.current_order.join(" → ") || ctx.t("editor:preview.empty"),
            current: item.suggested_order.join(" → ") || ctx.t("editor:preview.noSuggestion")
          })
        }));
        renderRulesPreview("editor:preview.suggestedTitle", lines, { suggestions });
      } catch (error) {
        ctx.showLocalizedError("Error loading fallback eval suggestions:", error.message);
      }
    }
    function normalizeFallbackModelForSave(fallbackRow) {
      const providerSelect = fallbackRow.querySelector(".provider-select");
      const modelSelect = fallbackRow.querySelector(".model-select");
      const modelStatus = fallbackRow.querySelector(".model-status");
      const useProviderOrderCheckbox = fallbackRow.querySelector(".use-provider-order-checkbox");
      const providersOrderInput = fallbackRow.querySelector(".providers-order-input");
      const upstreamKeyPoolInput = fallbackRow.querySelector(".upstream-key-pool-input");
      const retryDelayInput = fallbackRow.querySelector(".retry-delay-input");
      const retryCountInput = fallbackRow.querySelector(".retry-count-input");
      const customBodyParamsInput = fallbackRow.querySelector(".custom-body-params-input");
      const customHeadersInput = fallbackRow.querySelector(".custom-headers-input");
      const payloadTransformsInput = fallbackRow.querySelector(".payload-transforms-input");
      const supportsVisionSelect = fallbackRow.querySelector(".supports-vision-select");
      const supportsToolsSelect = fallbackRow.querySelector(".supports-tools-select");
      const contextWindowInput = fallbackRow.querySelector(".context-window-input");
      const provider = providerSelect.value.trim();
      const model = modelSelect.value.trim();
      const unavailableFallbackModel = getUnavailableFallbackModelDetails(fallbackRow);
      if (!provider) {
        throw new LocalizedUiError("editor:errors.chooseProvider");
      }
      if (fallbackRow.dataset.modelsLoadError === "true") {
        if (unavailableFallbackModel) {
          throw new LocalizedUiError("editor:errors.chooseAvailableModel", { provider });
        }
        throw new ConfigUiError(
          modelStatus.textContent || `Could not load models for provider '${provider}'.`
        );
      }
      if (!model) {
        throw new LocalizedUiError("editor:errors.chooseAvailableModel", { provider });
      }
      const fallbackModel = {
        provider,
        model,
        use_provider_order_as_fallback: useProviderOrderCheckbox.checked,
        custom_body_params: ctx.parseObjectTextarea(customBodyParamsInput.value, "Custom body params"),
        custom_headers: ctx.parseObjectTextarea(customHeadersInput.value, "Custom headers")
      };
      const payloadTransforms = ctx.parseObjectTextarea(payloadTransformsInput ? payloadTransformsInput.value : "", "Payload transforms");
      if (Object.keys(payloadTransforms).length > 0) {
        fallbackModel.payload_transforms = payloadTransforms;
      }
      const providersOrder = ctx.parseProvidersOrder(providersOrderInput.value);
      if (providersOrder) {
        fallbackModel.providers_order = providersOrder;
      }
      const upstreamKeyPool = upstreamKeyPoolInput ? upstreamKeyPoolInput.value.trim() : "";
      if (upstreamKeyPool) {
        fallbackModel.upstream_key_pool = upstreamKeyPool;
      }
      ctx.applyRetrySettingsToPayload(fallbackModel, retryDelayInput, retryCountInput);
      ctx.applyCapabilityFieldsToPayload(fallbackModel, supportsVisionSelect, supportsToolsSelect, contextWindowInput);
      return fallbackModel;
    }
    function normalizeRuleCardForSave(ruleCard) {
      const gatewayModelInput = ruleCard.querySelector(".gateway-model-input");
      const rotateModelsCheckbox = ruleCard.querySelector(".rotate-models-checkbox");
      const dynamicPenaltyCheckbox = ruleCard.querySelector(".dynamic-penalty-checkbox");
      const stripThinkTagsCheckbox = ruleCard.querySelector(".strip-think-tags-checkbox");
      const compressToolResultsCheckbox = ruleCard.querySelector(".compress-tool-results-checkbox");
      const toolCallRescueCheckbox = ruleCard.querySelector(".tool-call-rescue-checkbox");
      const maxTotalAttemptsInput = ruleCard.querySelector(".max-total-attempts-input");
      const contextOverflowEnabledCheckbox = ruleCard.querySelector(".context-overflow-enabled-checkbox");
      const contextOverflowRuleSlot = ruleCard.querySelector(".context-overflow-rule-slot");
      const fallbackRows = Array.from(ruleCard.querySelectorAll(".fallback-list > .fallback-row"));
      const gatewayModelName = gatewayModelInput.value.trim();
      if (!gatewayModelName) {
        throw new LocalizedUiError("editor:errors.gatewayName");
      }
      if (fallbackRows.length === 0) {
        throw new LocalizedUiError(
          "editor:errors.fallbackRequired",
          { model: gatewayModelName }
        );
      }
      const normalizedRule = {
        gateway_model_name: gatewayModelName,
        rotate_models: rotateModelsCheckbox.checked,
        dynamic_penalty: Boolean(dynamicPenaltyCheckbox?.checked),
        strip_think_tags: Boolean(stripThinkTagsCheckbox?.checked),
        compress_tool_results: Boolean(compressToolResultsCheckbox?.checked),
        tool_call_rescue: Boolean(toolCallRescueCheckbox?.checked),
        fallback_models: fallbackRows.map(normalizeFallbackModelForSave)
      };
      if (maxTotalAttemptsInput && maxTotalAttemptsInput.value.trim() !== "") {
        const parsed = Number.parseInt(maxTotalAttemptsInput.value, 10);
        if (!Number.isFinite(parsed) || parsed < 0) {
          throw new Error(`Gateway model '${gatewayModelName}' has invalid max_total_attempts (must be a non-negative integer).`);
        }
        normalizedRule.max_total_attempts = parsed;
      }
      if (contextOverflowEnabledCheckbox?.checked) {
        const contextOverflowRow = contextOverflowRuleSlot?.querySelector(".fallback-row");
        if (!contextOverflowRow) {
          throw new Error(`Gateway model '${gatewayModelName}' must define a context overflow fallback model when the special fallback is enabled.`);
        }
        normalizedRule.context_overflow_fallback = normalizeFallbackModelForSave(contextOverflowRow);
      }
      return normalizedRule;
    }
    function getRulesPayloadForSave() {
      const rules = Array.from(ctx.elements.rulesList.querySelectorAll(".rule-card")).map(normalizeRuleCardForSave);
      return { rules };
    }
    function getNormalizedRulesContent() {
      return stableSerialize(getRulesPayloadForSave());
    }
    function snapshotFallbackModelState(fallbackRow) {
      const providerSelect = fallbackRow.querySelector(".provider-select");
      const modelSelect = fallbackRow.querySelector(".model-select");
      const useProviderOrderCheckbox = fallbackRow.querySelector(".use-provider-order-checkbox");
      const providersOrderInput = fallbackRow.querySelector(".providers-order-input");
      const upstreamKeyPoolInput = fallbackRow.querySelector(".upstream-key-pool-input");
      const retryDelayInput = fallbackRow.querySelector(".retry-delay-input");
      const retryCountInput = fallbackRow.querySelector(".retry-count-input");
      const customBodyParamsInput = fallbackRow.querySelector(".custom-body-params-input");
      const customHeadersInput = fallbackRow.querySelector(".custom-headers-input");
      const payloadTransformsInput = fallbackRow.querySelector(".payload-transforms-input");
      const supportsVisionSelect = fallbackRow.querySelector(".supports-vision-select");
      const supportsToolsSelect = fallbackRow.querySelector(".supports-tools-select");
      const contextWindowInput = fallbackRow.querySelector(".context-window-input");
      const unavailableFallbackModel = getUnavailableFallbackModelDetails(fallbackRow);
      const fallbackModel = {
        provider: providerSelect.value.trim(),
        model: modelSelect.value.trim() || unavailableFallbackModel?.model || "",
        use_provider_order_as_fallback: useProviderOrderCheckbox.checked,
        custom_body_params: ctx.parseObjectTextarea(customBodyParamsInput.value, "Custom body params"),
        custom_headers: ctx.parseObjectTextarea(customHeadersInput.value, "Custom headers")
      };
      const payloadTransforms = ctx.parseObjectTextarea(payloadTransformsInput ? payloadTransformsInput.value : "", "Payload transforms");
      if (Object.keys(payloadTransforms).length > 0) {
        fallbackModel.payload_transforms = payloadTransforms;
      }
      const providersOrder = ctx.parseProvidersOrder(providersOrderInput.value);
      if (providersOrder) {
        fallbackModel.providers_order = providersOrder;
      }
      const upstreamKeyPool = upstreamKeyPoolInput ? upstreamKeyPoolInput.value.trim() : "";
      if (upstreamKeyPool) {
        fallbackModel.upstream_key_pool = upstreamKeyPool;
      }
      ctx.applyRetrySettingsToPayload(fallbackModel, retryDelayInput, retryCountInput);
      ctx.applyCapabilityFieldsToPayload(fallbackModel, supportsVisionSelect, supportsToolsSelect, contextWindowInput);
      return fallbackModel;
    }
    function getRulesSnapshotPayload() {
      const rules = Array.from(ctx.elements.rulesList.querySelectorAll(".rule-card")).map((ruleCard) => {
        const gatewayModelInput = ruleCard.querySelector(".gateway-model-input");
        const rotateModelsCheckbox = ruleCard.querySelector(".rotate-models-checkbox");
        const dynamicPenaltyCheckbox = ruleCard.querySelector(".dynamic-penalty-checkbox");
        const stripThinkTagsCheckbox = ruleCard.querySelector(".strip-think-tags-checkbox");
        const compressToolResultsCheckbox = ruleCard.querySelector(".compress-tool-results-checkbox");
        const toolCallRescueCheckbox = ruleCard.querySelector(".tool-call-rescue-checkbox");
        const maxTotalAttemptsInput = ruleCard.querySelector(".max-total-attempts-input");
        const contextOverflowEnabledCheckbox = ruleCard.querySelector(".context-overflow-enabled-checkbox");
        const contextOverflowRuleSlot = ruleCard.querySelector(".context-overflow-rule-slot");
        const fallbackRows = Array.from(ruleCard.querySelectorAll(".fallback-list > .fallback-row"));
        const normalizedRule = {
          gateway_model_name: gatewayModelInput.value.trim(),
          rotate_models: rotateModelsCheckbox.checked,
          dynamic_penalty: Boolean(dynamicPenaltyCheckbox?.checked),
          strip_think_tags: Boolean(stripThinkTagsCheckbox?.checked),
          compress_tool_results: Boolean(compressToolResultsCheckbox?.checked),
          tool_call_rescue: Boolean(toolCallRescueCheckbox?.checked),
          fallback_models: fallbackRows.map(snapshotFallbackModelState)
        };
        if (maxTotalAttemptsInput && maxTotalAttemptsInput.value.trim() !== "") {
          const parsed = Number.parseInt(maxTotalAttemptsInput.value, 10);
          if (Number.isFinite(parsed) && parsed >= 0) {
            normalizedRule.max_total_attempts = parsed;
          }
        }
        if (contextOverflowEnabledCheckbox?.checked) {
          const contextOverflowRow = contextOverflowRuleSlot?.querySelector(".fallback-row");
          if (contextOverflowRow) {
            normalizedRule.context_overflow_fallback = snapshotFallbackModelState(contextOverflowRow);
          }
        }
        return normalizedRule;
      });
      return { rules };
    }
    function getRulesSnapshotContent() {
      return stableSerialize(getRulesSnapshotPayload());
    }
    function setFallbackRowStatus(fallbackRow, statusText, state) {
      const modelStatus = fallbackRow.querySelector(".model-status");
      modelStatus.textContent = statusText || "";
      modelStatus.dataset.state = state || "idle";
    }
    function buildFallbackRow(initialData, options = {}) {
      const fallbackRow = document.createElement("div");
      fallbackRow.className = "fallback-row";
      fallbackRow.dataset.modelsLoadError = "false";
      ctx.setupRowReordering(fallbackRow);
      const fieldsGrid = document.createElement("div");
      fieldsGrid.className = "fallback-row-grid";
      const providerSelect = ctx.createSelect("provider-select");
      ctx.setSelectOptions(providerSelect, ctx.state.availableProviders, "Choose a provider", initialData.provider || "");
      const modelSelect = ctx.createSelect("model-select");
      modelSelect.disabled = !initialData.provider;
      ctx.setSelectOptions(
        modelSelect,
        initialData.model ? [initialData.model] : [],
        initialData.provider ? "Choose a model" : "Choose a provider first",
        initialData.model || ""
      );
      const useProviderOrderCheckbox = document.createElement("input");
      useProviderOrderCheckbox.type = "checkbox";
      useProviderOrderCheckbox.className = "use-provider-order-checkbox";
      useProviderOrderCheckbox.checked = Boolean(initialData.use_provider_order_as_fallback);
      const rotateToggle = document.createElement("label");
      rotateToggle.className = "toggle-field";
      rotateToggle.appendChild(useProviderOrderCheckbox);
      const toggleText = document.createElement("span");
      ctx.bindLocalizedText(toggleText, "editor:toggles.providerOrder");
      rotateToggle.appendChild(toggleText);
      const providersOrderInput = ctx.createTextInput("providers-order-input", "provider-a, provider-b");
      providersOrderInput.value = Array.isArray(initialData.providers_order) ? initialData.providers_order.join(", ") : "";
      const upstreamKeyPoolInput = ctx.createTextInput("upstream-key-pool-input", "main");
      upstreamKeyPoolInput.value = initialData.upstream_key_pool || "";
      const retryDelayInput = ctx.createNumberInput("retry-delay-input", "Retry delay (seconds)");
      retryDelayInput.value = initialData.retry_delay ?? "";
      const retryCountInput = ctx.createNumberInput("retry-count-input", "Retry count");
      retryCountInput.value = initialData.retry_count ?? "";
      const customBodyParamsInput = ctx.createTextarea("custom-body-params-input", '{"temperature": 0.2}');
      customBodyParamsInput.value = ctx.normalizeObjectTextarea(initialData.custom_body_params);
      const customHeadersInput = ctx.createTextarea("custom-headers-input", '{"X-Provider": "value"}');
      customHeadersInput.value = ctx.normalizeObjectTextarea(initialData.custom_headers);
      const payloadTransformsInput = ctx.createTextarea("payload-transforms-input", '{"defaults": {"top_p": 0.9}, "overrides": {}, "filters": ["seed"]}');
      payloadTransformsInput.value = ctx.normalizeObjectTextarea(initialData.payload_transforms);
      const autofilledCapabilityFields = new Set(
        Array.isArray(initialData.capabilities_autofilled) ? initialData.capabilities_autofilled : []
      );
      const resolveAutofillSource = typeof options.autofillSource === "function" ? options.autofillSource : () => null;
      const supportsVisionSelect = ctx.createTriStateSelect("supports-vision-select");
      supportsVisionSelect.value = initialData.supports_vision === true ? "true" : initialData.supports_vision === false ? "false" : "";
      const supportsVisionField = ctx.wrapCapabilityField({
        fieldName: "supports_vision",
        control: supportsVisionSelect,
        kind: "boolean",
        locked: autofilledCapabilityFields.has("supports_vision"),
        source: resolveAutofillSource("supports_vision")
      });
      const supportsToolsSelect = ctx.createTriStateSelect("supports-tools-select");
      supportsToolsSelect.value = initialData.supports_tools === true ? "true" : initialData.supports_tools === false ? "false" : "";
      const supportsToolsField = ctx.wrapCapabilityField({
        fieldName: "supports_tools",
        control: supportsToolsSelect,
        kind: "boolean",
        locked: autofilledCapabilityFields.has("supports_tools"),
        source: resolveAutofillSource("supports_tools")
      });
      const contextWindowInput = ctx.createNumberInput("context-window-input", "e.g. 128000");
      contextWindowInput.value = initialData.context_window ?? "";
      const contextWindowField = ctx.wrapCapabilityField({
        fieldName: "context_window",
        control: contextWindowInput,
        kind: "number",
        locked: autofilledCapabilityFields.has("context_window"),
        source: resolveAutofillSource("context_window")
      });
      fieldsGrid.appendChild(ctx.createFieldGroup("Provider", providerSelect, "provider-field"));
      fieldsGrid.appendChild(ctx.createFieldGroup("Model", modelSelect, "model-field"));
      fieldsGrid.appendChild(ctx.createFieldGroup("Provider Order", providersOrderInput));
      fieldsGrid.appendChild(ctx.createFieldGroup("Upstream Key Pool", upstreamKeyPoolInput));
      fieldsGrid.appendChild(ctx.createFieldGroup("Retry Delay", retryDelayInput));
      fieldsGrid.appendChild(ctx.createFieldGroup("Retry Count", retryCountInput));
      const modelStatus = document.createElement("div");
      modelStatus.className = "model-status";
      modelStatus.dataset.state = "idle";
      const advancedDetails = document.createElement("details");
      advancedDetails.className = "advanced-options";
      const advancedSummary = document.createElement("summary");
      ctx.bindLocalizedText(advancedSummary, "editor:actions.advanced");
      advancedDetails.appendChild(advancedSummary);
      const advancedGrid = document.createElement("div");
      advancedGrid.className = "advanced-grid";
      advancedGrid.appendChild(ctx.createFieldGroup("", rotateToggle, "toggle-group"));
      advancedGrid.appendChild(ctx.createFieldGroup("Vision Support", supportsVisionField));
      advancedGrid.appendChild(ctx.createFieldGroup("Tools Support", supportsToolsField));
      advancedGrid.appendChild(ctx.createFieldGroup("Context Window (tokens)", contextWindowField));
      advancedGrid.appendChild(ctx.createFieldGroup("Custom Body Params", customBodyParamsInput, "textarea-group"));
      advancedGrid.appendChild(ctx.createFieldGroup("Custom Headers", customHeadersInput, "textarea-group"));
      advancedGrid.appendChild(ctx.createFieldGroup("Payload Transforms", payloadTransformsInput, "textarea-group"));
      advancedDetails.appendChild(advancedGrid);
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, options.removeButtonLabel || "Remove Fallback");
      removeButton.addEventListener("click", () => {
        if (typeof options.onRemove === "function") {
          options.onRemove(fallbackRow);
          return;
        }
        const parentRuleCard = fallbackRow.closest(".rule-card");
        fallbackRow.remove();
        if (parentRuleCard) {
          const fallbackContainer = parentRuleCard.querySelector(".fallback-list");
          if (fallbackContainer.children.length === 0) {
            const addFallbackButton = parentRuleCard.querySelector(".add-fallback-button");
            if (addFallbackButton) {
              addFallbackButton.focus();
            }
          }
        }
      });
      const rowActions = document.createElement("div");
      rowActions.className = "fallback-row-actions";
      const { moveUpButton, moveDownButton } = ctx.createMoveButtons(fallbackRow);
      rowActions.appendChild(moveUpButton);
      rowActions.appendChild(moveDownButton);
      rowActions.appendChild(removeButton);
      fallbackRow.appendChild(fieldsGrid);
      fallbackRow.appendChild(modelStatus);
      fallbackRow.appendChild(advancedDetails);
      fallbackRow.appendChild(rowActions);
      const catalogController = ctx.createLazyProviderCatalogRowController({
        row: fallbackRow,
        providerSelect,
        modelControl: modelSelect,
        modelStatus,
        requireListedModel: true
      });
      modelSelect.addEventListener("change", () => {
        if (modelSelect.value) {
          catalogController.markSelected(modelSelect.value);
        }
      });
      return fallbackRow;
    }
    function buildRuleCard(initialData) {
      const ruleCard = document.createElement("section");
      ruleCard.className = "rule-card";
      const gatewayModelName = initialData.gateway_model_name || "";
      const autofillSourceForIndex = (index) => (fieldName) => ctx.capabilityAutofillSourceFor(
        ctx.state.capabilityAutofillStatus,
        gatewayModelName,
        index,
        fieldName
      );
      const cardHeader = document.createElement("div");
      cardHeader.className = "rule-card-header";
      const titleWrap = document.createElement("div");
      titleWrap.className = "rule-card-title";
      const gatewayModelInput = ctx.createTextInput("gateway-model-input", "llmgateway/model-name");
      gatewayModelInput.value = initialData.gateway_model_name || "";
      titleWrap.appendChild(ctx.createFieldGroup("Gateway Model Name", gatewayModelInput, "gateway-model-field"));
      const rotateModelsCheckbox = document.createElement("input");
      rotateModelsCheckbox.type = "checkbox";
      rotateModelsCheckbox.className = "rotate-models-checkbox";
      rotateModelsCheckbox.checked = Boolean(initialData.rotate_models);
      const rotateToggle = document.createElement("label");
      rotateToggle.className = "toggle-field rotate-toggle";
      rotateToggle.appendChild(rotateModelsCheckbox);
      const rotateLabel = document.createElement("span");
      ctx.bindLocalizedText(rotateLabel, "editor:toggles.rotate");
      rotateToggle.appendChild(rotateLabel);
      titleWrap.appendChild(rotateToggle);
      const dynamicPenaltyCheckbox = document.createElement("input");
      dynamicPenaltyCheckbox.type = "checkbox";
      dynamicPenaltyCheckbox.className = "dynamic-penalty-checkbox";
      dynamicPenaltyCheckbox.checked = Boolean(initialData.dynamic_penalty);
      const dynamicPenaltyToggle = document.createElement("label");
      dynamicPenaltyToggle.className = "toggle-field";
      dynamicPenaltyToggle.appendChild(dynamicPenaltyCheckbox);
      const dynamicPenaltyLabel = document.createElement("span");
      ctx.bindLocalizedText(dynamicPenaltyLabel, "editor:toggles.dynamicPenalty");
      dynamicPenaltyToggle.appendChild(dynamicPenaltyLabel);
      titleWrap.appendChild(dynamicPenaltyToggle);
      const stripThinkTagsCheckbox = document.createElement("input");
      stripThinkTagsCheckbox.type = "checkbox";
      stripThinkTagsCheckbox.className = "strip-think-tags-checkbox";
      stripThinkTagsCheckbox.checked = Boolean(initialData.strip_think_tags);
      const stripThinkTagsToggle = document.createElement("label");
      stripThinkTagsToggle.className = "toggle-field";
      stripThinkTagsToggle.appendChild(stripThinkTagsCheckbox);
      const stripThinkTagsLabel = document.createElement("span");
      ctx.bindLocalizedText(stripThinkTagsLabel, "editor:toggles.stripThink");
      stripThinkTagsToggle.appendChild(stripThinkTagsLabel);
      titleWrap.appendChild(stripThinkTagsToggle);
      const compressToolResultsCheckbox = document.createElement("input");
      compressToolResultsCheckbox.type = "checkbox";
      compressToolResultsCheckbox.className = "compress-tool-results-checkbox";
      compressToolResultsCheckbox.checked = Boolean(initialData.compress_tool_results);
      const compressToolResultsToggle = document.createElement("label");
      compressToolResultsToggle.className = "toggle-field";
      compressToolResultsToggle.appendChild(compressToolResultsCheckbox);
      const compressToolResultsLabel = document.createElement("span");
      ctx.bindLocalizedText(compressToolResultsLabel, "editor:toggles.compressTools");
      compressToolResultsToggle.appendChild(compressToolResultsLabel);
      titleWrap.appendChild(compressToolResultsToggle);
      const toolCallRescueCheckbox = document.createElement("input");
      toolCallRescueCheckbox.type = "checkbox";
      toolCallRescueCheckbox.className = "tool-call-rescue-checkbox";
      toolCallRescueCheckbox.checked = Boolean(initialData.tool_call_rescue);
      const toolCallRescueToggle = document.createElement("label");
      toolCallRescueToggle.className = "toggle-field";
      toolCallRescueToggle.appendChild(toolCallRescueCheckbox);
      const toolCallRescueLabel = document.createElement("span");
      ctx.bindLocalizedText(toolCallRescueLabel, "editor:toggles.toolCallRescue");
      toolCallRescueToggle.appendChild(toolCallRescueLabel);
      titleWrap.appendChild(toolCallRescueToggle);
      const maxTotalAttemptsInput = document.createElement("input");
      maxTotalAttemptsInput.type = "number";
      maxTotalAttemptsInput.className = "max-total-attempts-input";
      maxTotalAttemptsInput.min = "0";
      maxTotalAttemptsInput.step = "1";
      ctx.bindKnownPlaceholder(maxTotalAttemptsInput, "unlimited");
      if (Number.isFinite(initialData.max_total_attempts)) {
        maxTotalAttemptsInput.value = String(initialData.max_total_attempts);
      }
      titleWrap.appendChild(
        ctx.createFieldGroup(
          "Max Total Attempts (chain budget)",
          maxTotalAttemptsInput,
          "max-total-attempts-field"
        )
      );
      const removeRuleButton = document.createElement("button");
      removeRuleButton.type = "button";
      removeRuleButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeRuleButton, "Remove Rule");
      removeRuleButton.addEventListener("click", () => {
        ruleCard.remove();
        ctx.refreshRulesEmptyState();
      });
      cardHeader.appendChild(titleWrap);
      cardHeader.appendChild(removeRuleButton);
      const fallbackList = document.createElement("div");
      fallbackList.className = "fallback-list";
      const contextOverflowSection = document.createElement("section");
      contextOverflowSection.className = "context-overflow-section";
      const contextOverflowHeader = document.createElement("div");
      contextOverflowHeader.className = "context-overflow-header";
      const contextOverflowCopy = document.createElement("div");
      contextOverflowCopy.className = "context-overflow-copy";
      const contextOverflowTitle = document.createElement("strong");
      ctx.bindLocalizedText(contextOverflowTitle, "editor:toggles.contextOverflow");
      const contextOverflowDescription = document.createElement("span");
      ctx.bindLocalizedText(contextOverflowDescription, "editor:hints.contextOverflow");
      contextOverflowCopy.appendChild(contextOverflowTitle);
      contextOverflowCopy.appendChild(contextOverflowDescription);
      const contextOverflowEnabledCheckbox = document.createElement("input");
      contextOverflowEnabledCheckbox.type = "checkbox";
      contextOverflowEnabledCheckbox.className = "context-overflow-enabled-checkbox";
      contextOverflowEnabledCheckbox.checked = Boolean(initialData.context_overflow_fallback);
      const contextOverflowToggle = document.createElement("label");
      contextOverflowToggle.className = "toggle-field";
      contextOverflowToggle.appendChild(contextOverflowEnabledCheckbox);
      const contextOverflowToggleLabel = document.createElement("span");
      ctx.bindLocalizedText(contextOverflowToggleLabel, "editor:toggles.contextOverflowEnable");
      contextOverflowToggle.appendChild(contextOverflowToggleLabel);
      contextOverflowHeader.appendChild(contextOverflowCopy);
      contextOverflowHeader.appendChild(contextOverflowToggle);
      const contextOverflowRuleSlot = document.createElement("div");
      contextOverflowRuleSlot.className = "context-overflow-rule-slot";
      contextOverflowRuleSlot.hidden = !initialData.context_overflow_fallback;
      let contextOverflowRow = null;
      const ensureContextOverflowRow = () => {
        if (contextOverflowRow) {
          return contextOverflowRow;
        }
        contextOverflowRow = buildFallbackRow(initialData.context_overflow_fallback || {}, {
          removeButtonLabel: "Disable Special Fallback",
          onRemove: (row) => {
            row.remove();
            contextOverflowRow = null;
            contextOverflowEnabledCheckbox.checked = false;
            contextOverflowRuleSlot.hidden = true;
          },
          autofillSource: autofillSourceForIndex("context_overflow_fallback")
        });
        contextOverflowRuleSlot.appendChild(contextOverflowRow);
        return contextOverflowRow;
      };
      const addFallbackButton = document.createElement("button");
      addFallbackButton.type = "button";
      addFallbackButton.className = "secondary-button add-fallback-button";
      ctx.bindKnownActionText(addFallbackButton, "Add Fallback Model");
      addFallbackButton.addEventListener("click", () => {
        fallbackList.appendChild(buildFallbackRow({}));
      });
      const cardBody = document.createElement("div");
      cardBody.className = "rule-card-body";
      cardBody.appendChild(fallbackList);
      cardBody.appendChild(addFallbackButton);
      cardBody.appendChild(contextOverflowSection);
      const accordionToggle = document.createElement("button");
      accordionToggle.type = "button";
      accordionToggle.className = "accordion-toggle";
      const svgNS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("width", "20");
      svg.setAttribute("height", "20");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("fill", "none");
      svg.setAttribute("stroke", "currentColor");
      svg.setAttribute("stroke-width", "2");
      svg.setAttribute("stroke-linecap", "round");
      svg.setAttribute("stroke-linejoin", "round");
      const polyline = document.createElementNS(svgNS, "polyline");
      polyline.setAttribute("points", "6 9 12 15 18 9");
      svg.appendChild(polyline);
      accordionToggle.appendChild(svg);
      accordionToggle.addEventListener("click", () => {
        ctx.toggleProviderCatalogCard(ruleCard);
      });
      const headerLeft = document.createElement("div");
      headerLeft.className = "rule-card-header-left";
      headerLeft.appendChild(accordionToggle);
      headerLeft.appendChild(titleWrap);
      while (cardHeader.firstChild) {
        cardHeader.removeChild(cardHeader.firstChild);
      }
      cardHeader.appendChild(headerLeft);
      cardHeader.appendChild(removeRuleButton);
      ruleCard.classList.add("collapsed");
      ruleCard.appendChild(cardHeader);
      ruleCard.appendChild(cardBody);
      const fallbackModels = Array.isArray(initialData.fallback_models) ? initialData.fallback_models : [];
      fallbackModels.forEach((fallbackModel, index) => {
        const fallbackRow = buildFallbackRow(fallbackModel, {
          autofillSource: autofillSourceForIndex(index)
        });
        fallbackList.appendChild(fallbackRow);
      });
      contextOverflowSection.appendChild(contextOverflowHeader);
      contextOverflowSection.appendChild(contextOverflowRuleSlot);
      if (initialData.context_overflow_fallback) {
        ensureContextOverflowRow();
      }
      contextOverflowEnabledCheckbox.addEventListener("change", () => {
        if (contextOverflowEnabledCheckbox.checked) {
          ensureContextOverflowRow();
          contextOverflowRuleSlot.hidden = false;
          return;
        }
        contextOverflowRuleSlot.hidden = true;
        return void 0;
      });
      if (fallbackModels.length === 0) {
        const fallbackRow = buildFallbackRow({});
        fallbackList.appendChild(fallbackRow);
      }
      return ruleCard;
    }
    function renderRules(rules) {
      ctx.elements.rulesList.textContent = "";
      if (!Array.isArray(rules) || rules.length === 0) {
        ctx.refreshRulesEmptyState();
        return;
      }
      rules.forEach((rule) => {
        const ruleCard = buildRuleCard(rule);
        ctx.elements.rulesList.appendChild(ruleCard);
      });
      ctx.refreshRulesEmptyState();
    }
    async function loadCapabilityAutofillStatus() {
      try {
        const response = await ctx.apiFetch("/v1/capability-autofill");
        if (!response.ok) {
          return;
        }
        const payload = await response.json().catch(() => null);
        if (payload && typeof payload === "object") {
          ctx.state.capabilityAutofillStatus = payload;
        }
      } catch (error) {
      }
    }
    async function loadRulesEditor() {
      ctx.showLocalizedMessage("info", "Loading Fallback Rules...");
      await loadCapabilityAutofillStatus();
      try {
        const loaded = await ctx.loadConfigDocument(
          "fallback",
          "/v1/config/models-rules/structured",
          {
            validate: (payload) => ctx.validateFallbackPayload(payload, true),
            apply: async (payload) => {
              ctx.state.availableProviders = payload.providers;
              await renderRules(payload.rules);
            }
          }
        );
        if (!loaded) {
          if (ctx.state.activeEditor === "rules") {
            ctx.showLocalizedMessage("warning", "A newer local edit was preserved. Reload again to discard it.");
          }
          return false;
        }
        ctx.state.originalRulesContent = getRulesSnapshotContent();
        ctx.updateSaveButtonDisabledState();
        const unavailableFallbackModels = collectUnavailableFallbackModels(ctx.elements.rulesList);
        if (unavailableFallbackModels.length > 0) {
          if (ctx.state.activeEditor === "rules") {
            const unavailableDetails = formatUnavailableFallbackModelsDetails(
              unavailableFallbackModels
            );
            ctx.showLocalizedMessage(
              "warning",
              "editor:messages.loadedWithWarnings",
              () => ({
                details: ctx.t("editor:messages.unavailableModels", {
                  details: unavailableDetails
                })
              })
            );
          }
          return true;
        }
        if (ctx.state.activeEditor === "rules") {
          ctx.showLocalizedMessage("success", "Fallback Rules loaded successfully.");
        }
        return true;
      } catch (error) {
        console.error("Error fetching Fallback Rules:", error);
        if (ctx.state.activeEditor === "rules") {
          ctx.showLocalizedError("Error loading Fallback Rules:", error);
        }
        ctx.state.originalRulesContent = null;
        ctx.state.documentBases.set("fallback", null);
        ctx.updateSaveButtonDisabledState();
        return false;
      }
    }
    async function ensureAvailableProvidersLoaded() {
      if (ctx.state.availableProviders.length !== 0) {
        return;
      }
      const rulesResp = await ctx.apiFetch("/v1/config/models-rules/structured");
      const rulesPayload = await rulesResp.json();
      if (!rulesResp.ok) {
        throw new Error(rulesPayload.detail || `HTTP ${rulesResp.status}`);
      }
      ctx.state.availableProviders = Array.isArray(rulesPayload.providers) ? rulesPayload.providers : [];
    }
    const EVAL_LABEL_KEYS = /* @__PURE__ */ new Map([
      ["Status", "editor:eval.status"],
      ["Last checked", "editor:eval.lastChecked"],
      ["Last updated", "editor:eval.lastUpdated"],
      ["Next refresh", "editor:eval.nextRefresh"],
      ["Catalog models", "editor:eval.catalogModels"],
      ["Eligible models", "editor:eval.eligibleModels"],
      ["Unique targets", "editor:eval.uniqueTargets"],
      ["Lite evals", "editor:eval.liteEvals"],
      ["Last error", "editor:eval.lastError"],
      ["Refresh mode", "editor:eval.refreshMode"],
      ["Manual refresh", "editor:eval.manualRefresh"],
      ["metadata", "editor:eval.metadata"],
      ["health", "editor:eval.health"],
      ["latency", "editor:eval.latency"],
      ["eval", "editor:eval.eval"],
      ["penalty", "editor:eval.penalty"],
      ["latency ms", "editor:eval.latencyMs"],
      ["context", "editor:eval.context"],
      ["health status", "editor:eval.healthStatus"]
    ]);
    const EVAL_STATE_KEYS = /* @__PURE__ */ new Map([
      ["Running", "editor:messages.statusRunning"],
      ["Idle", "editor:messages.statusIdle"],
      ["n/a", "editor:messages.notApplicable"],
      ["passed", "editor:eval.statePassed"],
      ["failed", "editor:eval.stateFailed"],
      ["error", "editor:eval.stateError"],
      ["imperfect", "editor:eval.stateImperfect"],
      ["timeout", "editor:eval.stateTimeout"],
      ["http_429", "editor:eval.stateRateLimited"],
      ["rate_limited", "editor:eval.stateRateLimited"],
      ["missing_provider", "editor:eval.stateMissingProvider"],
      ["not_probed", "editor:eval.stateNotProbed"],
      ["fullEval", "editor:eval.refreshFullEval"],
      ["latencyOnly", "editor:eval.refreshLatencyOnly"],
      ["manualEval", "editor:eval.refreshManualEval"]
    ]);
    function formatEvalValue(value) {
      const resolved = typeof value === "function" ? value() : value;
      const stateKey = EVAL_STATE_KEYS.get(resolved);
      return stateKey ? ctx.t(stateKey) : String(resolved ?? "");
    }
    function appendOpenRouterMeta(parent, label, value, raw = false) {
      const item = document.createElement("div");
      item.className = "openrouter-free-meta-item";
      const labelElement = document.createElement("strong");
      const labelKey = EVAL_LABEL_KEYS.get(label);
      if (labelKey) {
        ctx.bindLocalizedText(labelElement, labelKey);
      } else {
        labelElement.textContent = label;
      }
      const valueElement = document.createElement("span");
      ctx.bindLocalizedValue(valueElement, () => formatEvalValue(value));
      if (raw) {
        valueElement.setAttribute("lang", "und");
        valueElement.setAttribute("dir", "auto");
      }
      item.appendChild(labelElement);
      item.appendChild(valueElement);
      parent.appendChild(item);
    }
    function appendEvalMetric(parent, label, value) {
      const metric = document.createElement("span");
      const labelElement = document.createElement("span");
      ctx.bindLocalizedText(labelElement, EVAL_LABEL_KEYS.get(label));
      const separator = document.createTextNode(": ");
      const valueElement = document.createElement("span");
      ctx.bindLocalizedValue(valueElement, () => typeof value === "number" ? ctx.formatNumber(value) : formatEvalValue(value || "n/a"));
      if (typeof value !== "number" && !EVAL_STATE_KEYS.has(value) && value) {
        valueElement.setAttribute("lang", "und");
        valueElement.setAttribute("dir", "auto");
      }
      metric.appendChild(labelElement);
      metric.appendChild(separator);
      metric.appendChild(valueElement);
      parent.appendChild(metric);
    }
    function buildEvalTaskSummary(models) {
      const summary = /* @__PURE__ */ new Map();
      (models || []).forEach((model) => {
        const tasks = model && model.evalSummary && model.evalSummary.tasks || [];
        tasks.forEach((task) => {
          if (!task || !task.id) {
            return;
          }
          let entry = summary.get(task.id);
          if (!entry) {
            entry = { id: task.id, evaluated: 0, passed: 0, points: 0, maxPoints: 0, failedChecks: /* @__PURE__ */ new Map() };
            summary.set(task.id, entry);
          }
          entry.evaluated += 1;
          entry.points += Number(task.points) || 0;
          entry.maxPoints += Number(task.maxPoints) || 0;
          if (task.status === "passed") {
            entry.passed += 1;
          }
          Object.entries(task.details || {}).forEach(([check, value]) => {
            if (value === false) {
              entry.failedChecks.set(check, (entry.failedChecks.get(check) || 0) + 1);
            }
          });
        });
      });
      return Array.from(summary.values());
    }
    function topFailedChecks(entry, limit = 3) {
      return Array.from(entry.failedChecks.entries()).sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0])).slice(0, limit);
    }
    function renderEvalTaskSummary(container, models) {
      const rows = buildEvalTaskSummary(models);
      if (!rows.length) {
        return;
      }
      const section = document.createElement("section");
      section.className = "eval-task-summary";
      const heading = document.createElement("h3");
      ctx.bindLocalizedText(heading, "editor:eval.taskSummaryTitle");
      section.appendChild(heading);
      rows.forEach((row) => {
        const item = document.createElement("div");
        item.className = "eval-task-summary-row";
        const name = document.createElement("code");
        name.textContent = row.id;
        item.appendChild(name);
        const passed = document.createElement("span");
        ctx.bindLocalizedText(passed, "editor:eval.taskSummaryPassed", () => ({
          passed: ctx.formatNumber(row.passed),
          total: ctx.formatNumber(row.evaluated)
        }));
        item.appendChild(passed);
        const score = document.createElement("span");
        ctx.bindLocalizedText(score, "editor:eval.taskSummaryScore", () => ({
          percent: ctx.formatNumber(row.maxPoints ? Math.round(row.points / row.maxPoints * 100) : 0)
        }));
        item.appendChild(score);
        const failures = document.createElement("span");
        failures.className = "eval-task-summary-failures";
        const failed = topFailedChecks(row);
        if (failed.length) {
          failures.textContent = failed.map(([check, count]) => `${check} ×${count}`).join(", ");
          failures.setAttribute("lang", "und");
          failures.setAttribute("dir", "auto");
        } else {
          ctx.bindLocalizedText(failures, "editor:eval.taskSummaryNoFailures");
        }
        item.appendChild(failures);
        section.appendChild(item);
      });
      container.appendChild(section);
    }
    function appendEvalTaskDetails(card, model) {
      const tasks = model && model.evalSummary && model.evalSummary.tasks || [];
      if (!tasks.length) {
        return;
      }
      const wrapper = document.createElement("details");
      wrapper.className = "eval-task-details";
      const summary = document.createElement("summary");
      ctx.bindLocalizedText(summary, "editor:eval.taskDetails");
      wrapper.appendChild(summary);
      tasks.forEach((task) => {
        const row = document.createElement("div");
        row.className = "eval-task-detail-row";
        const head = document.createElement("div");
        head.className = "eval-task-detail-head";
        const name = document.createElement("code");
        name.textContent = task.id || "";
        head.appendChild(name);
        const points = document.createElement("span");
        ctx.bindLocalizedValue(
          points,
          () => `${ctx.formatNumber(task.points || 0)} / ${ctx.formatNumber(task.maxPoints || 0)}`
        );
        head.appendChild(points);
        const status = document.createElement("span");
        ctx.bindLocalizedValue(status, () => formatEvalValue(task.status || "n/a"));
        head.appendChild(status);
        row.appendChild(head);
        const failed = Object.entries(task.details || {}).filter(([, value]) => value === false).map(([check]) => check);
        if (failed.length) {
          const checks = document.createElement("div");
          checks.className = "eval-task-detail-checks";
          checks.textContent = failed.join(", ");
          checks.setAttribute("lang", "und");
          checks.setAttribute("dir", "auto");
          row.appendChild(checks);
        }
        const rawOutput = task.details && task.details.rawOutput;
        if (rawOutput) {
          const output = document.createElement("pre");
          output.className = "eval-task-raw-output";
          output.textContent = rawOutput;
          output.setAttribute("lang", "und");
          output.setAttribute("dir", "auto");
          row.appendChild(output);
        }
        wrapper.appendChild(row);
      });
      card.appendChild(wrapper);
    }
    function renderOpenRouterFreeModels(payload) {
      ctx.clearElement(ctx.elements.openRouterFreeStatus);
      ctx.clearElement(ctx.elements.openRouterFreeModels);
      const snapshot = payload.snapshot;
      if (!snapshot) {
        ctx.elements.openRouterFreeEmptyState.hidden = false;
        appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, "Status", () => payload.lastError || ctx.t("editor:eval.waitingFirst"), Boolean(payload.lastError));
        appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, "Next refresh", () => ctx.formatDateTime(payload.nextRefreshAt));
        return;
      }
      ctx.elements.openRouterFreeEmptyState.hidden = Array.isArray(snapshot.models) && snapshot.models.length > 0;
      appendOpenRouterMeta(
        ctx.elements.openRouterFreeStatus,
        "Refresh mode",
        () => snapshot.refreshMode || "n/a",
        Boolean(snapshot.refreshMode && !EVAL_STATE_KEYS.has(snapshot.refreshMode))
      );
      appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, "Manual refresh", () => payload.manualRefreshRunning ? "Running" : "Idle");
      appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, "Last updated", () => ctx.formatDateTime(snapshot.updatedAt));
      appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, "Next refresh", () => ctx.formatDateTime(payload.nextRefreshAt));
      appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, "Catalog models", () => ctx.formatNumber(snapshot.catalogCount));
      appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, "Eligible models", () => ctx.formatNumber(snapshot.eligibleCount));
      appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, "Lite evals", () => ctx.formatNumber(snapshot.evaluatedCount));
      if (payload.lastError) {
        appendOpenRouterMeta(ctx.elements.openRouterFreeStatus, "Last error", payload.lastError, true);
      }
      renderEvalTaskSummary(ctx.elements.openRouterFreeModels, snapshot.models);
      (snapshot.models || []).forEach((model) => {
        const card = document.createElement("article");
        card.className = "openrouter-free-card";
        const header = document.createElement("div");
        header.className = "openrouter-free-card-header";
        const title = document.createElement("div");
        const rank = document.createElement("div");
        rank.className = "openrouter-free-rank";
        ctx.bindLocalizedValue(rank, () => Number.isFinite(Number(model.rank)) ? `#${ctx.formatNumber(Number(model.rank))}` : "#?");
        const name = document.createElement("strong");
        ctx.bindLocalizedValue(name, () => model.name || model.id || ctx.t("editor:messages.unknown"));
        name.setAttribute("lang", "und");
        name.setAttribute("dir", "auto");
        const id = document.createElement("code");
        id.textContent = model.id || "";
        title.appendChild(rank);
        title.appendChild(name);
        title.appendChild(id);
        const score = document.createElement("div");
        score.className = "openrouter-free-score";
        ctx.bindLocalizedValue(score, () => ctx.formatNumber(model.score));
        header.appendChild(title);
        header.appendChild(score);
        card.appendChild(header);
        const reason = document.createElement("p");
        reason.className = "openrouter-free-reason";
        if (model.reason) {
          reason.textContent = model.reason;
          reason.setAttribute("lang", "und");
          reason.setAttribute("dir", "auto");
        } else {
          ctx.bindLocalizedText(reason, "editor:eval.freeTextModel");
        }
        card.appendChild(reason);
        const metrics = document.createElement("div");
        metrics.className = "openrouter-free-metrics";
        [
          ["metadata", model.metadataScore],
          ["health", model.healthScore],
          ["latency", model.latencyScore],
          ["eval", model.liteEvalScore],
          ["penalty", model.instabilityPenalty],
          ["latency ms", model.latencyMs],
          ["context", model.contextLength],
          ["health status", model.healthStatus]
        ].forEach(([label, value]) => {
          appendEvalMetric(metrics, label, value);
        });
        card.appendChild(metrics);
        appendEvalTaskDetails(card, model);
        ctx.elements.openRouterFreeModels.appendChild(card);
      });
    }
    function stopOpenRouterFreePolling() {
      ctx.state.openRouterFreePollingEnabled = false;
      if (ctx.state.openRouterFreePollTimer) {
        clearTimeout(ctx.state.openRouterFreePollTimer);
        ctx.state.openRouterFreePollTimer = null;
      }
    }
    function isRulesTabContextCurrent(tabName, context) {
      return ctx.state.activeEditor === tabName && context !== null && !context.signal.aborted && context.isCurrent();
    }
    function scheduleOpenRouterFreePolling(payload, context) {
      stopOpenRouterFreePolling();
      if (!isRulesTabContextCurrent("openrouter-free", context)) {
        return;
      }
      if (ctx.elements.runOpenRouterFreeEvalButton) {
        ctx.elements.runOpenRouterFreeEvalButton.disabled = Boolean(payload.manualRefreshRunning);
      }
      if (payload.manualRefreshRunning) {
        ctx.state.openRouterFreePollingEnabled = true;
        ctx.state.openRouterFreePollTimer = window.setTimeout(() => {
          void loadOpenRouterFreeModels(false, ctx.state.activeRulesTabContext);
        }, 3e3);
      }
    }
    async function loadOpenRouterFreeModels(showMessage = true, context = ctx.state.activeRulesTabContext) {
      if (!isRulesTabContextCurrent("openrouter-free", context)) {
        return false;
      }
      if (showMessage) {
        ctx.state.evalTabLoadStates.set("openrouter-free", "loading");
        ctx.showLocalizedMessage("info", "Loading OpenRouter free model ranking...");
        ctx.clearElement(ctx.elements.openRouterFreeStatus);
        ctx.clearElement(ctx.elements.openRouterFreeModels);
        ctx.elements.openRouterFreeEmptyState.hidden = true;
      }
      try {
        const response = await ctx.apiFetch("/v1/openrouter/free-models");
        const payload = await response.json();
        if (!isRulesTabContextCurrent("openrouter-free", context)) {
          return false;
        }
        if (!response.ok) {
          throw new Error(payload.detail || `HTTP ${response.status}`);
        }
        if (!payload.configured) {
          ctx.state.evalTabLoadStates.set("openrouter-free", "idle");
          ctx.elements.tabOpenRouterFree.hidden = true;
          await ctx.state.rulesTabsController.repair();
          return false;
        }
        renderOpenRouterFreeModels(payload);
        ctx.state.evalTabLoadStates.set("openrouter-free", "ready");
        scheduleOpenRouterFreePolling(payload, context);
        if (showMessage) {
          ctx.showLocalizedMessage("success", "OpenRouter free model ranking loaded.");
        }
        return true;
      } catch (error) {
        if (!isRulesTabContextCurrent("openrouter-free", context)) {
          return false;
        }
        ctx.state.evalTabLoadStates.set("openrouter-free", "error");
        ctx.state.openRouterFreePollingEnabled = false;
        console.error("Error loading OpenRouter free model ranking:", error);
        ctx.showLocalizedError("Error loading OpenRouter free model ranking:", error.message);
        ctx.elements.openRouterFreeEmptyState.hidden = false;
        if (ctx.elements.runOpenRouterFreeEvalButton) {
          ctx.elements.runOpenRouterFreeEvalButton.disabled = false;
        }
        return false;
      }
    }
    async function runOpenRouterFreeEval() {
      if (!ctx.elements.runOpenRouterFreeEvalButton) return;
      ctx.elements.runOpenRouterFreeEvalButton.disabled = true;
      ctx.showLocalizedMessage("info", "Starting OpenRouter free model full eval...");
      try {
        const response = await ctx.apiFetch("/v1/openrouter/free-models/run", { method: "POST" });
        const payload = await response.json().catch(() => ({}));
        const context = ctx.state.activeRulesTabContext;
        if (!isRulesTabContextCurrent("openrouter-free", context)) {
          return;
        }
        if (!response.ok) {
          throw new Error(payload.detail || `HTTP ${response.status}`);
        }
        renderOpenRouterFreeModels(payload);
        scheduleOpenRouterFreePolling(payload, context);
        ctx.showLocalizedMessage("success", "OpenRouter free model full eval started.");
      } catch (error) {
        const context = ctx.state.activeRulesTabContext;
        if (!isRulesTabContextCurrent("openrouter-free", context)) {
          return;
        }
        console.error("Error starting OpenRouter free model full eval:", error);
        ctx.showLocalizedError("Error starting OpenRouter free model full eval:", error.message);
        ctx.elements.runOpenRouterFreeEvalButton.disabled = false;
      }
    }
    async function initializeOpenRouterFreeTabAvailability() {
      try {
        const response = await ctx.apiFetch("/v1/openrouter/free-models");
        const payload = await response.json().catch(() => ({}));
        ctx.elements.tabOpenRouterFree.hidden = !response.ok || !payload.configured;
      } catch (error) {
        ctx.elements.tabOpenRouterFree.hidden = true;
      }
      await ctx.state.rulesTabsController.repair();
    }
    function renderFallbackEvalModels(payload) {
      ctx.clearElement(ctx.elements.fallbackEvalStatus);
      ctx.clearElement(ctx.elements.fallbackEvalModels);
      const snapshot = payload.snapshot;
      ctx.elements.fallbackEvalEmptyState.hidden = Boolean(snapshot && Array.isArray(snapshot.models) && snapshot.models.length > 0);
      appendOpenRouterMeta(ctx.elements.fallbackEvalStatus, "Status", () => payload.running ? "Running" : "Idle");
      appendOpenRouterMeta(ctx.elements.fallbackEvalStatus, "Last checked", () => ctx.formatDateTime(payload.lastCheckedAt));
      if (snapshot) {
        appendOpenRouterMeta(ctx.elements.fallbackEvalStatus, "Unique targets", () => ctx.formatNumber(snapshot.configuredCount));
        appendOpenRouterMeta(ctx.elements.fallbackEvalStatus, "Lite evals", () => ctx.formatNumber(snapshot.evaluatedCount));
        appendOpenRouterMeta(ctx.elements.fallbackEvalStatus, "Last updated", () => ctx.formatDateTime(snapshot.updatedAt));
      }
      if (payload.lastError) {
        appendOpenRouterMeta(ctx.elements.fallbackEvalStatus, "Last error", payload.lastError, true);
      }
      if (!snapshot) {
        return;
      }
      renderEvalTaskSummary(ctx.elements.fallbackEvalModels, snapshot.models);
      (snapshot.models || []).forEach((model) => {
        const card = document.createElement("article");
        card.className = "openrouter-free-card";
        const header = document.createElement("div");
        header.className = "openrouter-free-card-header";
        const title = document.createElement("div");
        const rank = document.createElement("div");
        rank.className = "openrouter-free-rank";
        ctx.bindLocalizedValue(rank, () => Number.isFinite(Number(model.rank)) ? `#${ctx.formatNumber(Number(model.rank))}` : "#?");
        const name = document.createElement("strong");
        ctx.bindLocalizedValue(
          name,
          () => model.name || model.model || model.id || ctx.t("editor:messages.unknown")
        );
        name.setAttribute("lang", "und");
        name.setAttribute("dir", "auto");
        const id = document.createElement("code");
        id.textContent = `${model.provider || ""} / ${model.model || model.id || ""}`;
        title.appendChild(rank);
        title.appendChild(name);
        title.appendChild(id);
        const score = document.createElement("div");
        score.className = "openrouter-free-score";
        ctx.bindLocalizedValue(score, () => ctx.formatNumber(model.score));
        header.appendChild(title);
        header.appendChild(score);
        card.appendChild(header);
        const reason = document.createElement("p");
        reason.className = "openrouter-free-reason";
        if (Array.isArray(model.gatewayModels) && model.gatewayModels.length > 0) {
          const gateways = document.createElement("span");
          ctx.bindLocalizedText(
            gateways,
            "editor:eval.gatewayModels",
            () => ({ models: model.gatewayModels.join(", ") })
          );
          reason.appendChild(gateways);
        }
        if (model.reason) {
          const rawReason = document.createElement("span");
          rawReason.textContent = model.reason;
          rawReason.setAttribute("lang", "und");
          rawReason.setAttribute("dir", "auto");
          reason.appendChild(document.createTextNode(" "));
          reason.appendChild(rawReason);
        }
        if (!reason.childNodes.length) {
          ctx.bindLocalizedText(reason, "editor:eval.configuredTarget");
        }
        card.appendChild(reason);
        const metrics = document.createElement("div");
        metrics.className = "openrouter-free-metrics";
        [
          ["metadata", model.metadataScore],
          ["health", model.healthScore],
          ["latency", model.latencyScore],
          ["eval", model.liteEvalScore],
          ["penalty", model.instabilityPenalty],
          ["latency ms", model.latencyMs],
          ["context", model.contextLength],
          ["health status", model.healthStatus]
        ].forEach(([label, value]) => {
          appendEvalMetric(metrics, label, value);
        });
        card.appendChild(metrics);
        appendEvalTaskDetails(card, model);
        ctx.elements.fallbackEvalModels.appendChild(card);
      });
    }
    function stopFallbackEvalPolling() {
      ctx.state.fallbackEvalPollingEnabled = false;
      if (ctx.state.fallbackEvalPollTimer) {
        clearTimeout(ctx.state.fallbackEvalPollTimer);
        ctx.state.fallbackEvalPollTimer = null;
      }
    }
    function scheduleFallbackEvalPolling(payload, context) {
      stopFallbackEvalPolling();
      if (!isRulesTabContextCurrent("fallback-eval", context)) {
        return;
      }
      ctx.elements.runFallbackEvalButton.disabled = Boolean(payload.running);
      if (payload.running) {
        ctx.state.fallbackEvalPollingEnabled = true;
        ctx.state.fallbackEvalPollTimer = window.setTimeout(() => {
          void loadFallbackModelEvals(false, ctx.state.activeRulesTabContext);
        }, 3e3);
      }
    }
    async function loadFallbackModelEvals(showMessage = true, context = ctx.state.activeRulesTabContext) {
      if (!isRulesTabContextCurrent("fallback-eval", context)) {
        return false;
      }
      if (showMessage) {
        ctx.state.evalTabLoadStates.set("fallback-eval", "loading");
        ctx.showLocalizedMessage("info", "Loading fallback model eval status...");
      }
      try {
        const response = await ctx.apiFetch("/v1/fallback-model-evals");
        const payload = await response.json();
        if (!isRulesTabContextCurrent("fallback-eval", context)) {
          return false;
        }
        if (!response.ok) {
          throw new Error(payload.detail || `HTTP ${response.status}`);
        }
        renderFallbackEvalModels(payload);
        ctx.state.evalTabLoadStates.set("fallback-eval", "ready");
        scheduleFallbackEvalPolling(payload, context);
        if (showMessage) {
          ctx.showLocalizedMessage("success", "Fallback model eval status loaded.");
        }
        return true;
      } catch (error) {
        if (!isRulesTabContextCurrent("fallback-eval", context)) {
          return false;
        }
        ctx.state.evalTabLoadStates.set("fallback-eval", "error");
        ctx.state.fallbackEvalPollingEnabled = false;
        console.error("Error loading fallback model eval status:", error);
        ctx.showLocalizedError("Error loading fallback model eval status:", error.message);
        ctx.elements.fallbackEvalEmptyState.hidden = false;
        ctx.elements.runFallbackEvalButton.disabled = false;
        return false;
      }
    }
    async function runFallbackModelEval() {
      ctx.elements.runFallbackEvalButton.disabled = true;
      ctx.showLocalizedMessage("info", "Starting fallback model eval...");
      try {
        const response = await ctx.apiFetch("/v1/fallback-model-evals/run", { method: "POST" });
        const payload = await response.json().catch(() => ({}));
        const context = ctx.state.activeRulesTabContext;
        if (!isRulesTabContextCurrent("fallback-eval", context)) {
          return;
        }
        if (!response.ok) {
          throw new Error(payload.detail || `HTTP ${response.status}`);
        }
        renderFallbackEvalModels(payload);
        scheduleFallbackEvalPolling(payload, context);
        ctx.showLocalizedMessage("success", "Fallback model eval started.");
      } catch (error) {
        const context = ctx.state.activeRulesTabContext;
        if (!isRulesTabContextCurrent("fallback-eval", context)) {
          return;
        }
        console.error("Error starting fallback model eval:", error);
        ctx.showLocalizedError("Error starting fallback model eval:", error.message);
        ctx.elements.runFallbackEvalButton.disabled = false;
      }
    }
    async function saveRules() {
      const unavailableFallbackModels = collectUnavailableFallbackModels(ctx.elements.rulesList);
      if (unavailableFallbackModels.length > 0) {
        const unavailableDetails = formatUnavailableFallbackModelsDetails(
          unavailableFallbackModels
        );
        ctx.showLocalizedMessage(
          "error",
          "editor:messages.unavailableModels",
          { details: unavailableDetails }
        );
        return;
      }
      let payload;
      try {
        payload = getRulesPayloadForSave();
      } catch (error) {
        ctx.showClientValidationError(error);
        return;
      }
      ctx.elements.saveButton.disabled = true;
      ctx.showLocalizedMessage("info", "Saving Fallback Rules...");
      try {
        const result = await ctx.saveConfigDocument(
          "fallback",
          "/v1/config/models-rules/structured",
          payload,
          {
            errorTitle: "Error saving Fallback Rules:",
            extractPublishedPayload: (body) => ({ rules: body.rules }),
            validatePublished: (published) => ctx.validateFallbackPayload(published, false)
          }
        );
        if (!result) {
          return;
        }
        if (ctx.state.editorMutationVersion === result.submittedMutationVersion) {
          const application = renderRules(result.payload.rules);
          ctx.syncInteractionLock();
          await application;
          ctx.state.originalRulesContent = getRulesSnapshotContent();
        }
        ctx.showLocalizedMessage(
          "success",
          ctx.safeSuccessMessage(result.body, "Fallback Rules updated successfully.")
        );
      } catch (error) {
        console.error("Error saving Fallback Rules:", error);
        ctx.showLocalizedError("Error saving Fallback Rules:", error);
      } finally {
        ctx.updateSaveButtonDisabledState();
      }
    }
    Object.assign(ctx, {
      clearUnavailableFallbackModelMetadata,
      getUnavailableFallbackModelDetails,
      collectUnavailableFallbackModels,
      formatUnavailableFallbackModelsDetails,
      stableSerialize,
      renderRulesPreview,
      routeKey,
      previewRulesChanges,
      renderSuggestedFallbackOrder,
      normalizeFallbackModelForSave,
      normalizeRuleCardForSave,
      getRulesPayloadForSave,
      getNormalizedRulesContent,
      snapshotFallbackModelState,
      getRulesSnapshotPayload,
      getRulesSnapshotContent,
      setFallbackRowStatus,
      buildFallbackRow,
      buildRuleCard,
      renderRules,
      loadCapabilityAutofillStatus,
      loadRulesEditor,
      ensureAvailableProvidersLoaded,
      formatEvalValue,
      appendOpenRouterMeta,
      appendEvalMetric,
      buildEvalTaskSummary,
      renderEvalTaskSummary,
      appendEvalTaskDetails,
      renderOpenRouterFreeModels,
      stopOpenRouterFreePolling,
      isRulesTabContextCurrent,
      scheduleOpenRouterFreePolling,
      loadOpenRouterFreeModels,
      runOpenRouterFreeEval,
      initializeOpenRouterFreeTabAvailability,
      renderFallbackEvalModels,
      stopFallbackEvalPolling,
      scheduleFallbackEvalPolling,
      loadFallbackModelEvals,
      runFallbackModelEval,
      saveRules
    });
  }

  // src/fusion.mjs
  function registerFusion(ctx) {
    const FUSION_PANEL_MAX = 8;
    function buildFusionMemberRow(initialData, options) {
      options = options || {};
      const data = initialData || {};
      const row = document.createElement("div");
      row.className = "fallback-row fusion-member-row";
      const fieldsGrid = document.createElement("div");
      fieldsGrid.className = "fallback-row-grid";
      const providerSelect = ctx.createSelect("provider-select");
      ctx.setSelectOptions(providerSelect, ctx.state.availableProviders, "Choose a provider", data.provider || "");
      const modelInput = ctx.createTextInput("model-input", "Choose or enter model");
      modelInput.value = data.model || "";
      const dataListId = `fusion-models-list-${Math.random().toString(36).substr(2, 9)}`;
      modelInput.setAttribute("list", dataListId);
      const dataList = document.createElement("datalist");
      dataList.id = dataListId;
      row.appendChild(dataList);
      const temperatureInput = document.createElement("input");
      temperatureInput.type = "number";
      temperatureInput.className = "fusion-temperature-input";
      temperatureInput.min = "0";
      temperatureInput.max = "2";
      temperatureInput.step = "0.1";
      ctx.bindKnownPlaceholder(temperatureInput, "default");
      if (data.temperature !== void 0 && data.temperature !== null) {
        temperatureInput.value = data.temperature;
      }
      const maxTokensInput = ctx.createNumberInput("fusion-max-tokens-input", "default");
      if (data.max_completion_tokens !== void 0 && data.max_completion_tokens !== null) {
        maxTokensInput.value = data.max_completion_tokens;
      }
      const reasoningInput = ctx.createTextarea("fusion-reasoning-input", '{"effort": "medium"}');
      reasoningInput.value = data.reasoning ? ctx.normalizeObjectTextarea(data.reasoning) : "";
      fieldsGrid.appendChild(ctx.createFieldGroup("Provider", providerSelect, "provider-field"));
      fieldsGrid.appendChild(ctx.createFieldGroup("Model", modelInput, "model-field"));
      fieldsGrid.appendChild(ctx.createFieldGroup("Temperature", temperatureInput));
      const modelStatus = document.createElement("div");
      modelStatus.className = "model-status";
      modelStatus.dataset.state = "idle";
      const advancedDetails = document.createElement("details");
      advancedDetails.className = "advanced-options";
      const advancedSummary = document.createElement("summary");
      ctx.bindLocalizedText(advancedSummary, "editor:actions.advanced");
      advancedDetails.appendChild(advancedSummary);
      const advancedGrid = document.createElement("div");
      advancedGrid.className = "advanced-grid";
      advancedGrid.appendChild(ctx.createFieldGroup("Max Completion Tokens", maxTokensInput));
      advancedGrid.appendChild(ctx.createFieldGroup("Reasoning (JSON)", reasoningInput, "textarea-group"));
      advancedDetails.appendChild(advancedGrid);
      row.appendChild(fieldsGrid);
      row.appendChild(modelStatus);
      row.appendChild(advancedDetails);
      if (options.removable) {
        const removeButton = document.createElement("button");
        removeButton.type = "button";
        removeButton.className = "icon-button danger-button";
        ctx.bindKnownActionText(removeButton, options.removeLabel || "Remove Panel Model");
        removeButton.addEventListener("click", () => {
          row.remove();
        });
        const rowActions = document.createElement("div");
        rowActions.className = "fallback-row-actions";
        rowActions.appendChild(removeButton);
        row.appendChild(rowActions);
      }
      ctx.createLazyProviderCatalogRowController({
        row,
        providerSelect,
        modelControl: modelInput,
        dataList,
        modelStatus
      });
      return row;
    }
    function buildFusionSectionHeading(key) {
      const heading = document.createElement("div");
      heading.className = "fusion-section-heading";
      ctx.bindLocalizedText(heading, key);
      return heading;
    }
    function buildFusionWebToolsSection(initialWebTools) {
      const data = initialWebTools || null;
      const wrap = document.createElement("div");
      wrap.className = "fusion-web-tools";
      const enableLabel = document.createElement("label");
      enableLabel.className = "field-group fusion-include-details";
      const enableCheckbox = document.createElement("input");
      enableCheckbox.type = "checkbox";
      enableCheckbox.className = "fusion-web-tools-enabled";
      enableCheckbox.checked = Boolean(data);
      const enableText = document.createElement("span");
      enableText.className = "field-label";
      ctx.bindLocalizedText(enableText, "editor:toggles.fusionWeb");
      enableLabel.appendChild(enableCheckbox);
      enableLabel.appendChild(enableText);
      const fields = document.createElement("div");
      fields.className = "fusion-web-tools-fields fallback-list";
      const searchModelInput = ctx.createTextInput("fusion-web-search-model", "gateway web_search model (e.g. llmgateway/web-search)");
      searchModelInput.value = data && data.search_model ? data.search_model : "";
      const readModelInput = ctx.createTextInput("fusion-web-read-model", "gateway web_read model (optional — enables web_fetch)");
      readModelInput.value = data && data.read_model ? data.read_model : "";
      const maxToolCallsInput = ctx.createNumberInput("fusion-web-max-tool-calls", "6");
      maxToolCallsInput.value = data && data.max_tool_calls != null ? data.max_tool_calls : "";
      const maxIterationsInput = ctx.createNumberInput("fusion-web-max-iterations", "4");
      maxIterationsInput.value = data && data.max_iterations != null ? data.max_iterations : "";
      const maxResultsInput = ctx.createNumberInput("fusion-web-max-results", "5");
      maxResultsInput.value = data && data.max_results != null ? data.max_results : "";
      fields.appendChild(ctx.createFieldGroup("Search model (required)", searchModelInput));
      fields.appendChild(ctx.createFieldGroup("Read model (optional)", readModelInput));
      fields.appendChild(ctx.createFieldGroup("Max tool calls per panel model", maxToolCallsInput));
      fields.appendChild(ctx.createFieldGroup("Max iterations per panel model", maxIterationsInput));
      fields.appendChild(ctx.createFieldGroup("Max results per search", maxResultsInput));
      const syncVisibility = () => {
        fields.style.display = enableCheckbox.checked ? "" : "none";
      };
      enableCheckbox.addEventListener("change", syncVisibility);
      syncVisibility();
      wrap.appendChild(enableLabel);
      wrap.appendChild(fields);
      return wrap;
    }
    function buildFusionCard(initialData) {
      const data = initialData || {};
      const card = document.createElement("section");
      card.className = "rule-card fusion-card";
      const cardHeader = document.createElement("div");
      cardHeader.className = "rule-card-header";
      const titleWrap = document.createElement("div");
      titleWrap.className = "rule-card-title";
      const gatewayModelInput = ctx.createTextInput("gateway-model-input", "llmgateway/fusion-quality");
      gatewayModelInput.value = data.gateway_model_name || "";
      titleWrap.appendChild(ctx.createFieldGroup("Gateway Model Name", gatewayModelInput, "gateway-model-field"));
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, "Remove Model");
      removeButton.addEventListener("click", () => {
        card.remove();
        ctx.refreshFusionEmptyState();
      });
      const accordionToggle = document.createElement("button");
      accordionToggle.type = "button";
      accordionToggle.className = "accordion-toggle";
      const svgNS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("width", "20");
      svg.setAttribute("height", "20");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("fill", "none");
      svg.setAttribute("stroke", "currentColor");
      svg.setAttribute("stroke-width", "2");
      svg.setAttribute("stroke-linecap", "round");
      svg.setAttribute("stroke-linejoin", "round");
      const polyline = document.createElementNS(svgNS, "polyline");
      polyline.setAttribute("points", "6 9 12 15 18 9");
      svg.appendChild(polyline);
      accordionToggle.appendChild(svg);
      accordionToggle.addEventListener("click", () => {
        ctx.toggleProviderCatalogCard(card);
      });
      const headerLeft = document.createElement("div");
      headerLeft.className = "rule-card-header-left";
      headerLeft.appendChild(accordionToggle);
      headerLeft.appendChild(titleWrap);
      cardHeader.appendChild(headerLeft);
      cardHeader.appendChild(removeButton);
      const cardBody = document.createElement("div");
      cardBody.className = "rule-card-body";
      const mainList = document.createElement("div");
      mainList.className = "fallback-list fusion-main-list";
      const mainRow = buildFusionMemberRow(data.main_model || {}, { removable: false });
      mainList.appendChild(mainRow);
      const judgeList = document.createElement("div");
      judgeList.className = "fallback-list fusion-judge-list";
      const judgeRow = buildFusionMemberRow(data.judge_model || {}, { removable: false });
      judgeList.appendChild(judgeRow);
      const panelListEl = document.createElement("div");
      panelListEl.className = "fallback-list fusion-panel-list";
      const addPanelButton = document.createElement("button");
      addPanelButton.type = "button";
      addPanelButton.className = "secondary-button add-fallback-button";
      ctx.bindKnownActionText(addPanelButton, "Add Panel Model");
      addPanelButton.addEventListener("click", () => {
        if (panelListEl.children.length >= FUSION_PANEL_MAX) {
          ctx.showLocalizedMessage("error", `A Fusion panel can have at most ${FUSION_PANEL_MAX} models.`);
          return;
        }
        panelListEl.appendChild(buildFusionMemberRow({}, { removable: true }));
      });
      const panelMembers = Array.isArray(data.panel) ? data.panel : [];
      panelMembers.forEach((member) => {
        const memberRow = buildFusionMemberRow(member, { removable: true });
        panelListEl.appendChild(memberRow);
      });
      if (panelMembers.length === 0) {
        const memberRow = buildFusionMemberRow({}, { removable: true });
        panelListEl.appendChild(memberRow);
      }
      const reserveListEl = document.createElement("div");
      reserveListEl.className = "fallback-list fusion-reserve-list";
      const addReserveButton = document.createElement("button");
      addReserveButton.type = "button";
      addReserveButton.className = "secondary-button add-fallback-button";
      ctx.bindKnownActionText(addReserveButton, "Add Reserve Model");
      addReserveButton.addEventListener("click", () => {
        if (reserveListEl.children.length >= FUSION_PANEL_MAX) {
          ctx.showLocalizedMessage("error", `A Fusion reserve can have at most ${FUSION_PANEL_MAX} models.`);
          return;
        }
        reserveListEl.appendChild(buildFusionMemberRow({}, { removable: true, removeLabel: "Remove Reserve Model" }));
      });
      const reserveMembers = Array.isArray(data.reserve) ? data.reserve : [];
      reserveMembers.forEach((member) => {
        const memberRow = buildFusionMemberRow(member, { removable: true, removeLabel: "Remove Reserve Model" });
        reserveListEl.appendChild(memberRow);
      });
      const includeDetailsLabel = document.createElement("label");
      includeDetailsLabel.className = "field-group fusion-include-details";
      const includeDetailsCheckbox = document.createElement("input");
      includeDetailsCheckbox.type = "checkbox";
      includeDetailsCheckbox.className = "fusion-include-details-input";
      includeDetailsCheckbox.checked = data.include_details_default !== false;
      const includeDetailsText = document.createElement("span");
      includeDetailsText.className = "field-label";
      ctx.bindLocalizedText(includeDetailsText, "editor:toggles.fusionFull");
      includeDetailsLabel.appendChild(includeDetailsCheckbox);
      includeDetailsLabel.appendChild(includeDetailsText);
      cardBody.appendChild(buildFusionSectionHeading("editor:sections.fusion.mainHeading"));
      cardBody.appendChild(mainList);
      cardBody.appendChild(buildFusionSectionHeading("editor:sections.fusion.judgeHeading"));
      cardBody.appendChild(judgeList);
      cardBody.appendChild(buildFusionSectionHeading("editor:sections.fusion.panelHeading"));
      cardBody.appendChild(panelListEl);
      cardBody.appendChild(addPanelButton);
      const reserveHeading = buildFusionSectionHeading("editor:sections.fusion.reserveHeading");
      ctx.appendFieldHint(reserveHeading, "editor:sections.fusion.reserveHint");
      cardBody.appendChild(reserveHeading);
      cardBody.appendChild(reserveListEl);
      cardBody.appendChild(addReserveButton);
      cardBody.appendChild(includeDetailsLabel);
      cardBody.appendChild(buildFusionSectionHeading("editor:sections.fusion.webHeading"));
      cardBody.appendChild(buildFusionWebToolsSection(data.web_tools));
      card.classList.add("collapsed");
      card.appendChild(cardHeader);
      card.appendChild(cardBody);
      return card;
    }
    function normalizeFusionMemberRow(row, settings) {
      const required = settings.required;
      const roleLabel = settings.roleLabel;
      const providerSelect = row.querySelector(".provider-select");
      const modelInput = row.querySelector(".model-input");
      const temperatureInput = row.querySelector(".fusion-temperature-input");
      const maxTokensInput = row.querySelector(".fusion-max-tokens-input");
      const reasoningInput = row.querySelector(".fusion-reasoning-input");
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
      if (temperatureRaw !== "") {
        const temperature = Number(temperatureRaw);
        if (Number.isNaN(temperature)) {
          throw new Error(`${roleLabel} has an invalid temperature.`);
        }
        member.temperature = temperature;
      }
      const maxTokensRaw = maxTokensInput.value.trim();
      if (maxTokensRaw !== "") {
        const maxTokens = parseInt(maxTokensRaw, 10);
        if (Number.isNaN(maxTokens)) {
          throw new Error(`${roleLabel} has invalid max completion tokens.`);
        }
        member.max_completion_tokens = maxTokens;
      }
      const reasoningRaw = reasoningInput.value.trim();
      if (reasoningRaw !== "") {
        member.reasoning = ctx.parseObjectTextarea(reasoningInput.value, `${roleLabel} reasoning`);
      }
      return member;
    }
    function normalizeFusionCardForSave(card) {
      const gatewayModelInput = card.querySelector(".gateway-model-input");
      const gatewayModelName = gatewayModelInput.value.trim();
      if (!gatewayModelName) {
        throw new Error("Each fusion model must have a gateway model name.");
      }
      const mainRow = card.querySelector(".fusion-main-list > .fusion-member-row");
      const main_model = normalizeFusionMemberRow(mainRow, {
        required: true,
        roleLabel: `Fusion '${gatewayModelName}' main model`
      });
      const judgeRow = card.querySelector(".fusion-judge-list > .fusion-member-row");
      const judge_model = normalizeFusionMemberRow(judgeRow, {
        required: false,
        roleLabel: `Fusion '${gatewayModelName}' judge model`
      });
      const panelRows = Array.from(card.querySelectorAll(".fusion-panel-list > .fusion-member-row"));
      const panel = panelRows.map((rowEl) => normalizeFusionMemberRow(rowEl, {
        required: true,
        roleLabel: `Fusion '${gatewayModelName}' panel model`
      })).filter(Boolean);
      if (panel.length === 0) {
        throw new Error(`Fusion model '${gatewayModelName}' must have at least one panel model.`);
      }
      if (panel.length > FUSION_PANEL_MAX) {
        throw new Error(`Fusion model '${gatewayModelName}' can have at most ${FUSION_PANEL_MAX} panel models.`);
      }
      const reserveRows = Array.from(card.querySelectorAll(".fusion-reserve-list > .fusion-member-row"));
      const reserve = reserveRows.map((rowEl) => normalizeFusionMemberRow(rowEl, {
        required: false,
        roleLabel: `Fusion '${gatewayModelName}' reserve model`
      })).filter(Boolean);
      if (reserve.length > FUSION_PANEL_MAX) {
        throw new Error(`A Fusion reserve can have at most ${FUSION_PANEL_MAX} models.`);
      }
      const includeDetailsCheckbox = card.querySelector(".fusion-include-details-input");
      const rule = {
        gateway_model_name: gatewayModelName,
        panel,
        main_model,
        include_details_default: includeDetailsCheckbox ? includeDetailsCheckbox.checked : true
      };
      if (judge_model) {
        rule.judge_model = judge_model;
      }
      rule.reserve = reserve;
      const webToolsEnabled = card.querySelector(".fusion-web-tools-enabled");
      if (webToolsEnabled && webToolsEnabled.checked) {
        const searchModel = card.querySelector(".fusion-web-search-model").value.trim();
        if (!searchModel) {
          throw new Error(`Fusion model '${gatewayModelName}' web tools require a search model.`);
        }
        const webTools = { search_model: searchModel };
        const readModel = card.querySelector(".fusion-web-read-model").value.trim();
        if (readModel) {
          webTools.read_model = readModel;
        }
        const numericFields = [
          [".fusion-web-max-tool-calls", "max_tool_calls", "max tool calls"],
          [".fusion-web-max-iterations", "max_iterations", "max iterations"],
          [".fusion-web-max-results", "max_results", "max results"]
        ];
        numericFields.forEach(([selector, key, label]) => {
          const raw = card.querySelector(selector).value.trim();
          if (raw === "") {
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
      const rules = Array.from(ctx.elements.fusionList.querySelectorAll(".fusion-card")).map(normalizeFusionCardForSave);
      return { rules };
    }
    function getNormalizedFusionContent() {
      return ctx.stableSerialize(getFusionPayloadForSave());
    }
    function renderFusion(rules) {
      ctx.elements.fusionList.textContent = "";
      if (Array.isArray(rules)) {
        rules.forEach((rule) => {
          const card = buildFusionCard(rule);
          ctx.elements.fusionList.appendChild(card);
        });
      }
      ctx.refreshFusionEmptyState();
    }
    async function loadFusionEditor() {
      try {
        const loaded = await ctx.loadConfigDocument(
          "fusion",
          "/v1/config/fusion-rules/structured",
          {
            validate: (payload) => ctx.validateFusionPayload(payload, true),
            apply: async (payload) => {
              ctx.state.availableProviders = payload.providers;
              await renderFusion(payload.rules);
            }
          }
        );
        if (!loaded) {
          ctx.showLocalizedMessage("warning", "A newer local edit was preserved. Reload again to discard it.");
          return false;
        }
        ctx.state.originalFusionContent = getNormalizedFusionContent();
        ctx.updateSaveButtonDisabledState();
        ctx.showLocalizedMessage("success", "Fusion Models loaded successfully.");
        return true;
      } catch (error) {
        console.error("Error fetching Fusion Models:", error);
        ctx.showLocalizedError("Error loading Fusion Models:", error);
        ctx.state.originalFusionContent = null;
        ctx.updateSaveButtonDisabledState();
        return false;
      }
    }
    async function saveFusion() {
      ctx.elements.saveButton.disabled = true;
      ctx.showLocalizedMessage("info", "Saving Fusion Models...");
      let payload;
      try {
        payload = getFusionPayloadForSave();
      } catch (error) {
        ctx.showClientValidationError(error);
        return;
      }
      try {
        const result = await ctx.saveConfigDocument(
          "fusion",
          "/v1/config/fusion-rules/structured",
          payload,
          {
            errorTitle: "Error saving Fusion Models:",
            extractPublishedPayload: (body) => ({ rules: body.rules }),
            validatePublished: (published) => ctx.validateFusionPayload(published, false)
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
          "success",
          ctx.safeSuccessMessage(result.body, "Fusion Models updated successfully.")
        );
      } catch (error) {
        console.error("Error saving Fusion:", error);
        ctx.showLocalizedError("Error saving Fusion Models:", error);
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
      saveFusion
    });
  }

  // src/core.mjs
  function createEditorElements() {
    const messageArea = document.getElementById("messageArea");
    const rawDetailElement = document.getElementById("messageRawDetail");
    const saveButton = document.getElementById("saveButton");
    const conflictState = document.getElementById("editorConflictState");
    const conflictTitle = document.getElementById("editorConflictTitle");
    const conflictMessage = document.getElementById("editorConflictMessage");
    const reloadEditorDocumentButton = document.getElementById("reloadEditorDocumentButton");
    const addRuleButton = document.getElementById("addRuleButton");
    const previewRulesButton = document.getElementById("previewRulesButton");
    const suggestEvalOrderButton = document.getElementById("suggestEvalOrderButton");
    const rulesPreviewArea = document.getElementById("rulesPreviewArea");
    const rulesList = document.getElementById("rulesList");
    const rulesEmptyState = document.getElementById("rulesEmptyState");
    const tabOpenRouterFree = document.querySelector('[data-entity-target="openrouter-free"]');
    const editorContainerRules = document.getElementById("editor-container-rules");
    const editorContainerEmbeddings = document.getElementById("editor-container-embeddings");
    const editorContainerRerank = document.getElementById("editor-container-rerank");
    const editorContainerImages = document.getElementById("editor-container-images");
    const editorContainerAudio = document.getElementById("editor-container-audio");
    const editorContainerWeb = document.getElementById("editor-container-web");
    const editorContainerOpenRouterFree = document.getElementById("editor-container-openrouter-free");
    const editorContainerFallbackEval = document.getElementById("editor-container-fallback-eval");
    const editorContainerProviders = document.getElementById("editor-container-providers");
    const editorContainerModelRules = document.getElementById("editor-container-model-rules");
    const editorContainerFusion = document.getElementById("editor-container-fusion");
    const addFusionButton = document.getElementById("addFusionButton");
    const fusionList = document.getElementById("fusionList");
    const fusionEmptyState = document.getElementById("fusionEmptyState");
    const editorContainerRouter = document.getElementById("editor-container-router");
    const addRouterButton = document.getElementById("addRouterButton");
    const routerList = document.getElementById("routerList");
    const routerEmptyState = document.getElementById("routerEmptyState");
    const addProviderButton = document.getElementById("addProviderButton");
    const providersList = document.getElementById("providersList");
    const providersEmptyState = document.getElementById("providersEmptyState");
    const addEmbeddingButton = document.getElementById("addEmbeddingButton");
    const embeddingsList = document.getElementById("embeddingsList");
    const embeddingsEmptyState = document.getElementById("embeddingsEmptyState");
    const addRerankButton = document.getElementById("addRerankButton");
    const rerankList = document.getElementById("rerankList");
    const rerankEmptyState = document.getElementById("rerankEmptyState");
    const addImageGenerationButton = document.getElementById("addImageGenerationButton");
    const imageGenerationList = document.getElementById("imageGenerationList");
    const imageGenerationEmptyState = document.getElementById("imageGenerationEmptyState");
    const addImageEditButton = document.getElementById("addImageEditButton");
    const imageEditList = document.getElementById("imageEditList");
    const imageEditEmptyState = document.getElementById("imageEditEmptyState");
    const addAudioSpeechButton = document.getElementById("addAudioSpeechButton");
    const audioSpeechList = document.getElementById("audioSpeechList");
    const audioSpeechEmptyState = document.getElementById("audioSpeechEmptyState");
    const addAudioTranscriptionButton = document.getElementById("addAudioTranscriptionButton");
    const audioTranscriptionsList = document.getElementById("audioTranscriptionsList");
    const audioTranscriptionsEmptyState = document.getElementById("audioTranscriptionsEmptyState");
    const addWebSearchButton = document.getElementById("addWebSearchButton");
    const webSearchList = document.getElementById("webSearchList");
    const webSearchEmptyState = document.getElementById("webSearchEmptyState");
    const addWebReadButton = document.getElementById("addWebReadButton");
    const webReadList = document.getElementById("webReadList");
    const webReadEmptyState = document.getElementById("webReadEmptyState");
    const addWebResearchButton = document.getElementById("addWebResearchButton");
    const webResearchList = document.getElementById("webResearchList");
    const webResearchEmptyState = document.getElementById("webResearchEmptyState");
    const addWebDeepResearchButton = document.getElementById("addWebDeepResearchButton");
    const webDeepResearchList = document.getElementById("webDeepResearchList");
    const webDeepResearchEmptyState = document.getElementById("webDeepResearchEmptyState");
    const openRouterFreeStatus = document.getElementById("openRouterFreeStatus");
    const openRouterFreeModels = document.getElementById("openRouterFreeModels");
    const openRouterFreeEmptyState = document.getElementById("openRouterFreeEmptyState");
    const runOpenRouterFreeEvalButton = document.getElementById("runOpenRouterFreeEvalButton");
    const runFallbackEvalButton = document.getElementById("runFallbackEvalButton");
    const fallbackEvalStatus = document.getElementById("fallbackEvalStatus");
    const fallbackEvalModels = document.getElementById("fallbackEvalModels");
    const fallbackEvalEmptyState = document.getElementById("fallbackEvalEmptyState");
    const modelRulesRawInput = document.getElementById("modelRulesRawInput");
    return {
      messageArea,
      rawDetailElement,
      saveButton,
      conflictState,
      conflictTitle,
      conflictMessage,
      reloadEditorDocumentButton,
      addRuleButton,
      previewRulesButton,
      suggestEvalOrderButton,
      rulesPreviewArea,
      rulesList,
      rulesEmptyState,
      tabOpenRouterFree,
      editorContainerRules,
      editorContainerEmbeddings,
      editorContainerRerank,
      editorContainerImages,
      editorContainerAudio,
      editorContainerWeb,
      editorContainerOpenRouterFree,
      editorContainerFallbackEval,
      editorContainerProviders,
      editorContainerModelRules,
      editorContainerFusion,
      addFusionButton,
      fusionList,
      fusionEmptyState,
      editorContainerRouter,
      addRouterButton,
      routerList,
      routerEmptyState,
      addProviderButton,
      providersList,
      providersEmptyState,
      addEmbeddingButton,
      embeddingsList,
      embeddingsEmptyState,
      addRerankButton,
      rerankList,
      rerankEmptyState,
      addImageGenerationButton,
      imageGenerationList,
      imageGenerationEmptyState,
      addImageEditButton,
      imageEditList,
      imageEditEmptyState,
      addAudioSpeechButton,
      audioSpeechList,
      audioSpeechEmptyState,
      addAudioTranscriptionButton,
      audioTranscriptionsList,
      audioTranscriptionsEmptyState,
      addWebSearchButton,
      webSearchList,
      webSearchEmptyState,
      addWebReadButton,
      webReadList,
      webReadEmptyState,
      addWebResearchButton,
      webResearchList,
      webResearchEmptyState,
      addWebDeepResearchButton,
      webDeepResearchList,
      webDeepResearchEmptyState,
      openRouterFreeStatus,
      openRouterFreeModels,
      openRouterFreeEmptyState,
      runOpenRouterFreeEvalButton,
      runFallbackEvalButton,
      fallbackEvalStatus,
      fallbackEvalModels,
      fallbackEvalEmptyState,
      modelRulesRawInput
    };
  }
  function registerCore(ctx) {
    const { gatewayI18n } = ctx;
    function t(key, values = {}) {
      return gatewayI18n.t(key, values);
    }
    function renderLocalizedBinding(binding) {
      if (!binding.element.isConnected && binding.wasConnected) {
        ctx.state.localizedBindings.delete(binding);
        return;
      }
      binding.wasConnected = binding.element.isConnected;
      if (typeof binding.render === "function") {
        binding.element.textContent = binding.render();
        return;
      }
      const values = typeof binding.getValues === "function" ? binding.getValues() : binding.getValues || {};
      const key = typeof binding.key === "function" ? binding.key() : binding.key;
      const value = t(key, values);
      if (binding.attribute) {
        binding.element.setAttribute(binding.attribute, value);
      } else {
        binding.element.textContent = value;
      }
    }
    function bindLocalizedText(element, key, getValues) {
      const binding = { element, key, getValues };
      ctx.state.localizedBindings.add(binding);
      renderLocalizedBinding(binding);
      return element;
    }
    function bindLocalizedAttribute(element, attribute, key, getValues) {
      const binding = { element, attribute, key, getValues };
      ctx.state.localizedBindings.add(binding);
      renderLocalizedBinding(binding);
      return element;
    }
    function bindLocalizedValue(element, render) {
      const binding = { element, render };
      ctx.state.localizedBindings.add(binding);
      renderLocalizedBinding(binding);
      return element;
    }
    function setRawDetail(rawDetail) {
      const detail = boundedSafeText(rawDetail);
      ctx.elements.rawDetailElement.textContent = detail;
      ctx.elements.rawDetailElement.hidden = !detail;
      ctx.elements.rawDetailElement.setAttribute("lang", "und");
      ctx.elements.rawDetailElement.setAttribute("dir", "auto");
    }
    const CONFIG_NAME_KEYS = /* @__PURE__ */ new Map([
      ["Fallback Rules", "editor:tabs.rules"],
      ["Embeddings Routes", "editor:sections.embeddings.title"],
      ["Rerank Routes", "editor:sections.rerank.title"],
      ["Images Routes", "editor:messages.imagesName"],
      ["Audio Routes", "editor:messages.audioName"],
      ["Web Services", "editor:messages.webName"],
      ["Fusion Models", "editor:sections.fusion.title"],
      ["Router Models", "editor:sections.router.title"],
      ["Providers", "editor:sections.providers.title"],
      ["Model Rules", "editor:sections.modelRules.title"]
    ]);
    const EXACT_MESSAGE_KEYS = /* @__PURE__ */ new Map([
      ["A newer local edit was preserved. Reload again to discard it.", "editor:messages.newerEdit"],
      ["No changes detected.", "editor:messages.noChanges"],
      ["The configuration is invalid.", "editor:errors.genericInvalid"],
      ["Cannot save Providers: provider configuration has not loaded successfully.", "editor:errors.providerNotReady"],
      ["Cannot add Provider: provider configuration has not loaded successfully.", "editor:errors.providerNotReady"],
      ["No active editor selected.", "editor:errors.noActiveEditor"],
      ["OpenRouter free model ranking loaded.", "editor:eval.loadedOpenrouter"],
      ["Loading OpenRouter free model ranking...", "editor:eval.loadingOpenrouter"],
      ["Starting OpenRouter free model full eval...", "editor:eval.startingOpenrouter"],
      ["OpenRouter free model full eval started.", "editor:eval.startedOpenrouter"],
      ["Fallback model eval status loaded.", "editor:eval.loadedFallback"],
      ["Loading fallback model eval status...", "editor:eval.loadingFallback"],
      ["Starting fallback model eval...", "editor:eval.startingFallback"],
      ["Fallback model eval started.", "editor:eval.startedFallback"],
      ["Model Rules saved successfully.", "editor:messages.modelRulesSaved"]
    ]);
    function localizedMessageDescriptor(message, suppliedValues = {}) {
      if (typeof message === "string" && message.startsWith("editor:")) {
        return { key: message, values: suppliedValues, rawDetail: "" };
      }
      const exactKey = EXACT_MESSAGE_KEYS.get(message);
      if (exactKey) {
        return { key: exactKey, values: {}, rawDetail: "" };
      }
      for (const [pattern, key] of [
        [/^Loading (.+)\.\.\.$/, "editor:messages.loading"],
        [/^Saving (.+)\.\.\.$/, "editor:messages.saving"],
        [/^(.+) loaded successfully\.$/, "editor:messages.loaded"],
        [/^(.+) updated successfully\.$/, "editor:messages.saved"]
      ]) {
        const match = typeof message === "string" ? message.match(pattern) : null;
        const nameKey = match ? CONFIG_NAME_KEYS.get(match[1]) : null;
        if (nameKey) {
          return {
            key,
            values: () => ({ name: t(nameKey) }),
            rawDetail: ""
          };
        }
      }
      const warningPrefix = "Fallback Rules loaded with warnings. ";
      if (typeof message === "string" && message.startsWith(warningPrefix)) {
        return {
          key: "editor:messages.loadedWithWarnings",
          values: { details: message.slice(warningPrefix.length) },
          rawDetail: ""
        };
      }
      const panelLimit = typeof message === "string" ? message.match(/^A Fusion panel can have at most (\d+) models\.$/) : null;
      if (panelLimit) {
        return {
          key: "editor:errors.panelLimit",
          values: { count: Number(panelLimit[1]) },
          rawDetail: ""
        };
      }
      const reserveLimit = typeof message === "string" ? message.match(/^A Fusion reserve can have at most (\d+) models\.$/) : null;
      if (reserveLimit) {
        return {
          key: "editor:errors.reserveLimit",
          values: { count: Number(reserveLimit[1]) },
          rawDetail: ""
        };
      }
      return {
        key: typeForUnknownMessage(message),
        values: {},
        rawDetail: typeof message === "string" ? message : ""
      };
    }
    function typeForUnknownMessage(message) {
      return typeof message === "string" && message.includes("invalid") ? "editor:errors.genericInvalid" : "editor:errors.genericRequest";
    }
    function resolveDescriptorValues(descriptor) {
      return typeof descriptor.values === "function" ? descriptor.values() : descriptor.values;
    }
    function showLocalizedMessage(type, message, values = {}, rawDetail = "") {
      const descriptor = localizedMessageDescriptor(message, values);
      if (rawDetail) {
        descriptor.rawDetail = rawDetail;
      }
      ctx.state.currentMessage = { type, ...descriptor };
      ctx.elements.messageArea.className = type;
      ctx.elements.messageArea.textContent = t(descriptor.key, resolveDescriptorValues(descriptor));
      setRawDetail(descriptor.rawDetail);
      if (type === "error" || type === "warning") {
        ctx.elements.messageArea.scrollIntoView({ block: "nearest" });
      }
    }
    function rerenderLocale() {
      Array.from(ctx.state.localizedBindings).forEach(renderLocalizedBinding);
      if (ctx.state.currentMessage) {
        ctx.elements.messageArea.textContent = t(
          ctx.state.currentMessage.key,
          resolveDescriptorValues(ctx.state.currentMessage)
        );
      }
      ctx.rerenderProviderCatalogStatuses();
      updateControlsVisibility();
    }
    function boundedSafeText(value) {
      if (typeof value !== "string") {
        return "";
      }
      const normalized = value.replace(/[\u0000-\u001F\u007F]/g, " ").trim();
      if (normalized.length <= ctx.constants.MAX_SAFE_ERROR_LENGTH) {
        return normalized;
      }
      return `${normalized.slice(0, ctx.constants.MAX_SAFE_ERROR_LENGTH - 1)}…`;
    }
    function ruleValidationMessages(detail) {
      return Array.isArray(detail.errors) ? detail.errors.filter((error) => error && typeof error === "object" && (error.type === "rule_validation" || error.type === "preflight") && typeof error.msg === "string").map((error) => error.msg) : [];
    }
    function safeResponseError(response, body) {
      const detail = body && typeof body === "object" && body.detail && typeof body.detail === "object" ? body.detail : null;
      const parts = detail ? [
        typeof detail.message === "string" ? detail.message : "",
        ...ruleValidationMessages(detail)
      ] : [];
      const detailMessage = boundedSafeText(parts.filter(Boolean).join(" "));
      return detailMessage || t("editor:errors.requestFailed", { status: response.status });
    }
    function safeClientError(error) {
      return error instanceof ConfigUiError ? error.message : t("editor:errors.genericRequest");
    }
    function safeSuccessMessage(body, fallback) {
      return fallback;
    }
    class ConfigUiError extends Error {
    }
    class LocalizedUiError extends ConfigUiError {
      constructor(key, values = {}) {
        super(key);
        this.key = key;
        this.values = values;
      }
    }
    const KNOWN_CONFIG_ERROR_KEYS = /* @__PURE__ */ new Map([
      ["The configuration response is not valid JSON.", "editor:errors.invalidJson"],
      ["The configuration response has an invalid shape.", "editor:errors.invalidShape"],
      ["The configuration response omitted a valid revision.", "editor:errors.invalidRevision"],
      ["Reload this configuration before saving.", "editor:errors.reloadBeforeSave"]
    ]);
    function showClientValidationError(error) {
      if (error instanceof LocalizedUiError) {
        showLocalizedMessage("error", error.key, error.values);
        return;
      }
      showLocalizedMessage(
        "error",
        "editor:errors.genericInvalid",
        {},
        boundedSafeText(error && error.message)
      );
    }
    function cloneConfigPayload(payload) {
      return typeof payload === "string" ? payload : JSON.parse(JSON.stringify(payload));
    }
    function requireStrongEtag(response) {
      const etag = response.headers.get("ETag");
      if (typeof etag !== "string" || !ctx.constants.STRONG_ETAG_PATTERN.test(etag)) {
        throw new ConfigUiError("The configuration response omitted a valid revision.");
      }
      return etag;
    }
    function commitDocumentBase(documentName, payload, etag) {
      if (!ctx.state.documentBases.has(documentName)) {
        throw new TypeError(`Unknown configuration document: ${documentName}`);
      }
      if (!ctx.constants.STRONG_ETAG_PATTERN.test(etag)) {
        throw new ConfigUiError("The configuration response omitted a valid revision.");
      }
      ctx.state.documentBases.set(documentName, {
        payload: cloneConfigPayload(payload),
        etag
      });
    }
    function getDocumentBase(documentName) {
      const base = ctx.state.documentBases.get(documentName);
      if (!base) {
        throw new ConfigUiError("Reload this configuration before saving.");
      }
      return {
        payload: cloneConfigPayload(base.payload),
        etag: base.etag
      };
    }
    function getOperationBasePayload() {
      return getDocumentBase("operation").payload;
    }
    function isRecord(value) {
      return value !== null && typeof value === "object" && !Array.isArray(value);
    }
    function requireArrayProperty(payload, propertyName) {
      if (!isRecord(payload) || !Array.isArray(payload[propertyName])) {
        throw new ConfigUiError("The configuration response has an invalid shape.");
      }
    }
    function validateFallbackPayload(payload, includeProviders = true) {
      requireArrayProperty(payload, "rules");
      if (includeProviders) {
        requireArrayProperty(payload, "providers");
      }
      return payload;
    }
    function validateOperationPayload(payload) {
      if (!isRecord(payload)) {
        throw new ConfigUiError("The configuration response has an invalid shape.");
      }
      ["embeddings", "rerank", "images_generations", "images_edits"].forEach((sectionName) => {
        requireArrayProperty(payload, sectionName);
      });
      return ctx.normalizeOperationRulesPayload(payload);
    }
    function validateFusionPayload(payload, includeProviders = false) {
      requireArrayProperty(payload, "rules");
      if (includeProviders) {
        requireArrayProperty(payload, "providers");
      }
      return payload;
    }
    function validateRouterPayload(payload) {
      requireArrayProperty(payload, "rules");
      if (Object.prototype.hasOwnProperty.call(payload, "chat_models")) {
        requireArrayProperty(payload, "chat_models");
      }
      if (Object.prototype.hasOwnProperty.call(payload, "fallback_chains") && !isRecord(payload.fallback_chains)) {
        throw new ConfigUiError("The configuration response has an invalid shape.");
      }
      return payload;
    }
    function validateProvidersPayload(payload) {
      requireArrayProperty(payload, "providers");
      return payload;
    }
    async function readJsonBody(response) {
      try {
        return await response.json();
      } catch (error) {
        throw new ConfigUiError(
          "The configuration response is not valid JSON.",
          { cause: error }
        );
      }
    }
    const CONFLICT_COPY = {
      revision: {
        title: "editor:conflict.title",
        message: "editor:conflict.message",
        action: "editor:conflict.reload"
      },
      outOfSync: {
        title: "editor:conflict.outOfSyncTitle",
        message: "editor:conflict.outOfSyncMessage",
        action: "editor:conflict.resync"
      }
    };
    function applyConflictCopy(element, key) {
      element.setAttribute("data-i18n", key);
      element.textContent = t(key);
    }
    function conflictModeFor(body) {
      const detail = body && typeof body === "object" ? body.detail : null;
      return detail && detail.code === "config_sources_out_of_sync" ? "outOfSync" : "revision";
    }
    function showConflict(documentName, mode = "revision") {
      const resolvedMode = CONFLICT_COPY[mode] ? mode : "revision";
      const copy = CONFLICT_COPY[resolvedMode];
      ctx.elements.conflictState.dataset.document = documentName;
      ctx.elements.conflictState.dataset.mode = resolvedMode;
      applyConflictCopy(ctx.elements.conflictTitle, copy.title);
      applyConflictCopy(ctx.elements.conflictMessage, copy.message);
      applyConflictCopy(ctx.elements.reloadEditorDocumentButton, copy.action);
      ctx.elements.conflictState.hidden = false;
      ctx.elements.conflictState.focus();
    }
    function clearConflict() {
      ctx.elements.conflictState.hidden = true;
      delete ctx.elements.conflictState.dataset.document;
      delete ctx.elements.conflictState.dataset.mode;
    }
    async function resyncConfigSources() {
      try {
        const response = await ctx.apiFetch("/v1/config/resync", { method: "POST" });
        if (!response.ok) {
          const body = await readJsonBody(response).catch(() => ({}));
          showLocalizedMessage(
            "error",
            "editor:conflict.resyncFailed",
            {},
            safeResponseError(response, body)
          );
          return false;
        }
      } catch (error) {
        showLocalizedMessage(
          "error",
          "editor:conflict.resyncFailed",
          {},
          safeClientError(error)
        );
        return false;
      }
      showLocalizedMessage("success", "editor:conflict.resynced");
      return true;
    }
    async function reloadAfterConflict() {
      if (ctx.elements.conflictState.dataset.mode === "outOfSync" && !await resyncConfigSources()) {
        return false;
      }
      return reloadActiveDocument();
    }
    function currentDocumentName() {
      if (["embeddings", "rerank", "images", "audio", "web"].includes(ctx.state.activeEditor)) {
        return "operation";
      }
      return {
        rules: "fallback",
        fusion: "fusion",
        router: "router",
        providers: "providers",
        "model-rules": "model"
      }[ctx.state.activeEditor] || null;
    }
    function isInteractionLocked() {
      return ctx.state.saveInFlight || ctx.state.busyDocuments.size > 0;
    }
    function syncInteractionLock() {
      if (isInteractionLocked()) {
        const controls = document.querySelectorAll(
          "#reloadEditorDocumentButton, .editor-entity-item"
        );
        controls.forEach((control) => {
          if (!ctx.state.lockedControls.has(control)) {
            ctx.state.lockedControls.set(control, control.disabled);
          }
          control.disabled = true;
        });
        document.querySelectorAll(".editor-tab-content.active").forEach((subtree) => {
          if (!ctx.state.lockedSubtrees.has(subtree)) {
            ctx.state.lockedSubtrees.set(subtree, subtree.inert);
          }
          subtree.inert = true;
          subtree.querySelectorAll("[draggable]").forEach((row) => {
            if (!ctx.state.lockedDraggables.has(row)) {
              ctx.state.lockedDraggables.set(row, row.draggable);
            }
            row.draggable = false;
          });
        });
        return;
      }
      ctx.state.lockedControls.forEach((wasDisabled, control) => {
        if (control.isConnected) {
          control.disabled = wasDisabled;
        }
      });
      ctx.state.lockedControls.clear();
      ctx.state.lockedDraggables.forEach((wasDraggable, row) => {
        if (row.isConnected) {
          row.draggable = wasDraggable;
        }
      });
      ctx.state.lockedDraggables.clear();
      ctx.state.lockedSubtrees.forEach((wasInert, subtree) => {
        if (subtree.isConnected) {
          subtree.inert = wasInert;
        }
      });
      ctx.state.lockedSubtrees.clear();
    }
    function setDocumentBusy(documentName, busy) {
      if (busy) {
        ctx.state.busyDocuments.add(documentName);
      } else {
        ctx.state.busyDocuments.delete(documentName);
      }
      updateSaveButtonDisabledState();
      syncInteractionLock();
    }
    async function loadConfigDocument(documentName, url, options) {
      ctx.invalidateProviderCatalogRows();
      const requestId = (ctx.state.loadRequestIds.get(documentName) || 0) + 1;
      const requestMutationVersion = ctx.state.editorMutationVersion;
      ctx.state.loadRequestIds.set(documentName, requestId);
      setDocumentBusy(documentName, true);
      try {
        const response = await ctx.apiFetch(url);
        const payload = options.responseType === "text" ? await response.text() : await readJsonBody(response);
        if (!response.ok) {
          throw new ConfigUiError(
            options.responseType === "text" ? `Request failed (HTTP ${response.status}).` : safeResponseError(response, payload)
          );
        }
        const etag = requireStrongEtag(response);
        const validatedPayload = options.validate(payload);
        if (ctx.state.loadRequestIds.get(documentName) !== requestId || ctx.state.editorMutationVersion !== requestMutationVersion) {
          return false;
        }
        const application = options.apply(validatedPayload);
        syncInteractionLock();
        await application;
        if (ctx.state.loadRequestIds.get(documentName) !== requestId || ctx.state.editorMutationVersion !== requestMutationVersion) {
          return false;
        }
        commitDocumentBase(documentName, validatedPayload, etag);
        clearConflict();
        return true;
      } catch (error) {
        if (ctx.state.loadRequestIds.get(documentName) === requestId) {
          ctx.state.documentBases.set(documentName, null);
        }
        throw error;
      } finally {
        if (ctx.state.loadRequestIds.get(documentName) === requestId) {
          setDocumentBusy(documentName, false);
        }
      }
    }
    async function saveConfigDocument(documentName, url, payload, options = {}) {
      ctx.invalidateProviderCatalogRows();
      let base;
      try {
        base = getDocumentBase(documentName);
      } catch (error) {
        showLocalizedError(options.errorTitle, error);
        return null;
      }
      const submittedMutationVersion = ctx.state.editorMutationVersion;
      const response = await ctx.apiFetch(url, {
        method: "POST",
        headers: {
          "Content-Type": options.contentType || "application/json",
          "If-Match": base.etag
        },
        body: options.body === void 0 ? JSON.stringify(payload) : options.body
      });
      const body = await readJsonBody(response).catch(() => ({}));
      if (response.status === 409) {
        showConflict(documentName, conflictModeFor(body));
        return null;
      }
      if (!response.ok) {
        showLocalizedError(options.errorTitle, safeResponseError(response, body));
        return null;
      }
      let etag;
      let publishedPayload;
      try {
        etag = requireStrongEtag(response);
        publishedPayload = options.extractPublishedPayload ? options.extractPublishedPayload(body, payload) : body;
        publishedPayload = options.validatePublished ? options.validatePublished(publishedPayload) : publishedPayload;
      } catch (error) {
        showLocalizedError(options.errorTitle, error);
        return null;
      }
      commitDocumentBase(documentName, publishedPayload, etag);
      clearConflict();
      return {
        body,
        payload: cloneConfigPayload(publishedPayload),
        submittedMutationVersion
      };
    }
    function showLocalizedError(title, detail) {
      if (detail instanceof LocalizedUiError) {
        showLocalizedMessage("error", detail.key, detail.values);
        return;
      }
      if (detail instanceof ConfigUiError) {
        const key = KNOWN_CONFIG_ERROR_KEYS.get(detail.message);
        if (key) {
          showLocalizedMessage("error", key);
          return;
        }
        detail = safeClientError(detail);
      }
      const match = typeof title === "string" ? title.match(/^Error (loading|saving) (.+):$/) : null;
      const nameKey = match ? CONFIG_NAME_KEYS.get(match[2]) : null;
      if (match && nameKey) {
        const key = match[1] === "loading" ? "editor:errors.loadTitle" : "editor:errors.saveTitle";
        showLocalizedMessage(
          "error",
          key,
          () => ({ name: t(nameKey) }),
          detail
        );
        return;
      }
      showLocalizedMessage(
        "error",
        "editor:errors.genericRequest",
        {},
        [title, detail].filter(Boolean).join(" ")
      );
    }
    function clearElement(element) {
      while (element.firstChild) {
        element.removeChild(element.firstChild);
      }
    }
    function formatDateTime(value) {
      if (!value) {
        return t("editor:messages.notAvailable");
      }
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) {
        return t("editor:messages.notAvailable");
      }
      return ctx.gatewayI18n.formatDate(date, { dateStyle: "medium", timeStyle: "medium" });
    }
    function formatNumber(value) {
      if (typeof value !== "number" || !Number.isFinite(value)) {
        return t("editor:messages.notApplicable");
      }
      return ctx.gatewayI18n.formatNumber(value);
    }
    function updateControlsVisibility() {
      ctx.elements.saveButton.hidden = ctx.state.activeEditor === "openrouter-free" || ctx.state.activeEditor === "fallback-eval";
      const key = {
        rules: "editor:actions.saveFallback",
        embeddings: "editor:actions.saveEmbeddings",
        rerank: "editor:actions.saveRerank",
        images: "editor:actions.saveImages",
        audio: "editor:actions.saveAudio",
        web: "editor:actions.saveWeb",
        fusion: "editor:actions.saveFusion",
        router: "editor:actions.saveRouter",
        "model-rules": "editor:actions.saveModelRules"
      }[ctx.state.activeEditor] || "editor:actions.saveConfiguration";
      ctx.elements.saveButton.textContent = ctx.elements.saveButton.hidden ? "" : gatewayI18n.t(key);
      updateSaveButtonDisabledState();
      updateProvidersControlsState();
    }
    function isActiveEditorLoaded() {
      return {
        rules: ctx.state.originalRulesContent,
        embeddings: ctx.state.originalEmbeddingsContent,
        rerank: ctx.state.originalRerankContent,
        images: ctx.state.originalImagesContent,
        audio: ctx.state.originalAudioContent,
        web: ctx.state.originalWebContent,
        fusion: ctx.state.originalFusionContent,
        router: ctx.state.originalRouterContent,
        providers: ctx.state.originalProvidersContent,
        "model-rules": ctx.state.originalModelRulesContent
      }[ctx.state.activeEditor] !== null;
    }
    function updateSaveButtonDisabledState() {
      const documentName = currentDocumentName();
      if (ctx.state.saveInFlight || ctx.state.busyDocuments.size > 0) {
        ctx.elements.saveButton.disabled = true;
      } else if (!documentName) {
        ctx.elements.saveButton.disabled = false;
      } else if (ctx.state.activeEditor === "providers") {
        ctx.elements.saveButton.disabled = ctx.state.providersLoadState !== "ready" || !ctx.state.documentBases.get(documentName) || !isActiveEditorLoaded();
      } else {
        ctx.elements.saveButton.disabled = !ctx.state.documentBases.get(documentName) || !isActiveEditorLoaded();
      }
      syncInteractionLock();
      updateDirtyIndicator();
    }
    function updateDirtyIndicator() {
      const dirty = isCurrentEditorDirty();
      ctx.elements.saveButton.setAttribute("data-editor-dirty", dirty ? "true" : "false");
    }
    function updateProvidersControlsState() {
      ctx.elements.addProviderButton.disabled = ctx.state.providersLoadState !== "ready";
      updateSaveButtonDisabledState();
    }
    function setProvidersLoadState(state) {
      ctx.state.providersLoadState = state;
      updateProvidersControlsState();
    }
    function refreshRulesEmptyState() {
      ctx.elements.rulesEmptyState.hidden = ctx.elements.rulesList.children.length !== 0;
    }
    function refreshEmbeddingsEmptyState() {
      ctx.elements.embeddingsEmptyState.hidden = ctx.elements.embeddingsList.children.length !== 0;
    }
    function refreshRerankEmptyState() {
      ctx.elements.rerankEmptyState.hidden = ctx.elements.rerankList.children.length !== 0;
    }
    function refreshImageGenerationEmptyState() {
      ctx.elements.imageGenerationEmptyState.hidden = ctx.elements.imageGenerationList.children.length !== 0;
    }
    function refreshImageEditEmptyState() {
      ctx.elements.imageEditEmptyState.hidden = ctx.elements.imageEditList.children.length !== 0;
    }
    function refreshAudioSpeechEmptyState() {
      ctx.elements.audioSpeechEmptyState.hidden = ctx.elements.audioSpeechList.children.length !== 0;
    }
    function refreshAudioTranscriptionsEmptyState() {
      ctx.elements.audioTranscriptionsEmptyState.hidden = ctx.elements.audioTranscriptionsList.children.length !== 0;
    }
    function refreshWebSearchEmptyState() {
      ctx.elements.webSearchEmptyState.hidden = ctx.elements.webSearchList.children.length !== 0;
    }
    function refreshWebReadEmptyState() {
      ctx.elements.webReadEmptyState.hidden = ctx.elements.webReadList.children.length !== 0;
    }
    function refreshWebResearchEmptyState() {
      ctx.elements.webResearchEmptyState.hidden = ctx.elements.webResearchList.children.length !== 0;
    }
    function refreshWebDeepResearchEmptyState() {
      ctx.elements.webDeepResearchEmptyState.hidden = ctx.elements.webDeepResearchList.children.length !== 0;
    }
    function refreshProvidersEmptyState() {
      ctx.elements.providersEmptyState.hidden = ctx.elements.providersList.children.length !== 0;
    }
    function refreshFusionEmptyState() {
      ctx.elements.fusionEmptyState.hidden = ctx.elements.fusionList.children.length !== 0;
    }
    function refreshRouterEmptyState() {
      ctx.elements.routerEmptyState.hidden = ctx.elements.routerList.children.length !== 0;
    }
    const FIELD_LABEL_KEYS = /* @__PURE__ */ new Map([
      ["Provider", "editor:fields.provider"],
      ["Model", "editor:fields.model"],
      ["Gateway Model Name", "editor:fields.gatewayModelName"],
      ["Provider Order", "editor:fields.providerOrder"],
      ["Upstream Key Pool", "editor:fields.upstreamKeyPool"],
      ["Retry Delay", "editor:fields.retryDelay"],
      ["Retry Count", "editor:fields.retryCount"],
      ["Custom Body Params", "editor:fields.customBodyParams"],
      ["Custom Headers", "editor:fields.customHeaders"],
      ["Payload Transforms", "editor:fields.payloadTransforms"],
      ["Target Path", "editor:fields.targetPath"],
      ["Request Format", "editor:fields.requestFormat"],
      ["Response Format", "editor:fields.responseFormat"],
      ["Response Output Format", "editor:fields.responseOutputFormat"],
      ["Request Mapping", "editor:fields.requestMapping"],
      ["Response Mapping", "editor:fields.responseMapping"],
      ["Temperature", "editor:fields.temperature"],
      ["Max Completion Tokens", "editor:fields.maxCompletionTokens"],
      ["Reasoning (JSON)", "editor:fields.reasoningJson"],
      ["Search model (required)", "editor:fields.searchModelRequired"],
      ["Read model (optional)", "editor:fields.readModelOptional"],
      ["Max tool calls per panel model", "editor:fields.maxToolCalls"],
      ["Max iterations per panel model", "editor:fields.maxIterations"],
      ["Max results per search", "editor:fields.maxResults"],
      ["Gateway Target", "editor:fields.gatewayTarget"],
      ["Fallback Gateway", "editor:fields.fallbackGateway"],
      ["Start At Entry", "editor:fields.startAtEntry"],
      ["Target Type", "editor:fields.targetType"],
      ["Selector Model", "editor:fields.selectorModel"],
      ["Voices Target Path", "editor:fields.voicesTargetPath"],
      ["Query Model (optional)", "editor:fields.queryModelOptional"],
      ["Provider Name", "editor:fields.providerName"],
      ["Base URL", "editor:fields.baseUrl"],
      ["API Key", "editor:fields.apiKey"],
      ["API Type", "editor:fields.apiType"],
      ["Proxy (optional)", "editor:fields.proxyOptional"],
      ["Models Metadata (JSON)", "editor:fields.modelsMetadata"],
      ["Available Models (optional)", "editor:fields.availableModels"],
      ["Routing Policy (JSON)", "editor:fields.routingPolicy"],
      ["Upstream Key Pools (JSON)", "editor:fields.upstreamKeyPools"],
      ["Cost per successful request (USD)", "editor:fields.costPerRequest"],
      ["Max Total Attempts (chain budget)", "editor:fields.maxAttempts"],
      ["Search Model", "editor:fields.searchModel"],
      ["Read Model", "editor:fields.readModel"],
      ["Rerank Model", "editor:fields.rerankModel"],
      ["Analysis Model", "editor:fields.analysisModel"],
      ["Fast LLM", "editor:fields.fastModel"],
      ["Smart LLM", "editor:fields.smartModel"],
      ["Strategic LLM", "editor:fields.strategicModel"],
      ["Embedding Model", "editor:fields.embeddingModel"],
      ["Image Generation Model", "editor:fields.imageGenerationModel"],
      ["Image Generation Size", "editor:fields.imageGenerationSize"],
      ["Custom body params", "editor:fields.customBodyParams"],
      ["Custom headers", "editor:fields.customHeaders"],
      ["Payload transforms", "editor:fields.payloadTransforms"],
      ["Request mapping", "editor:fields.requestMapping"],
      ["Response mapping", "editor:fields.responseMapping"],
      ["Vision Support", "editor:fields.supportsVision"],
      ["Tools Support", "editor:fields.supportsTools"],
      ["Context Window (tokens)", "editor:fields.contextWindow"]
    ]);
    const PLACEHOLDER_KEYS = /* @__PURE__ */ new Map([
      ["Choose or enter model", "editor:placeholders.chooseOrEnterModel"],
      ["Select a gateway model", "editor:placeholders.chooseGatewayModel"],
      ["default", "editor:placeholders.defaultValue"],
      ["Retry delay (seconds)", "editor:placeholders.retryDelay"],
      ["Retry count", "editor:placeholders.retryCount"],
      ["unlimited", "editor:placeholders.unlimited"],
      ["Choose API type", "editor:placeholders.chooseApiType"],
      ["Choose a provider", "editor:placeholders.chooseProvider"],
      ["Choose a provider first", "editor:placeholders.chooseProviderFirst"],
      ["Choose a model", "editor:placeholders.chooseModel"],
      ["No models available", "editor:placeholders.noModels"],
      ["Loading models...", "editor:placeholders.loadingModels"],
      ["Loading models…", "editor:placeholders.loadingModels"],
      ["Select fallback entry", "editor:placeholders.selectFallbackEntry"],
      ["e.g. 128000", "editor:placeholders.contextWindow"]
    ]);
    const ACTION_TEXT_KEYS = /* @__PURE__ */ new Map([
      ["Remove Fallback", "editor:actions.removeFallback"],
      ["Disable Special Fallback", "editor:actions.removeFallback"],
      ["Remove Rule", "editor:actions.removeRule"],
      ["Remove Model", "editor:actions.removeModel"],
      ["Remove Fallback Route", "editor:actions.removeRoute"],
      ["Remove Route", "editor:actions.removeRoute"],
      ["Remove Service", "editor:actions.removeRoute"],
      ["Remove Target", "editor:actions.removeTarget"],
      ["Remove Provider", "editor:actions.removeProvider"],
      ["Remove Panel Model", "editor:actions.removePanelModel"],
      ["Remove Reserve Model", "editor:actions.removeReserveModel"],
      ["Add Fallback Model", "editor:actions.addFallbackModel"],
      ["Add Fallback Route", "editor:actions.addFallbackRoute"],
      ["Add Route", "editor:actions.addRoute"],
      ["Add Target", "editor:actions.addTarget"],
      ["Add Panel Model", "editor:actions.addPanelModel"],
      ["Add Reserve Model", "editor:actions.addReserveModel"],
      ["Add Model", "editor:actions.addModel"],
      ["Remove", "editor:actions.remove"]
    ]);
    function bindKnownActionText(element, text) {
      const key = ACTION_TEXT_KEYS.get(text);
      if (key) {
        bindLocalizedText(element, key);
      } else {
        element.textContent = text;
      }
      return element;
    }
    function bindKnownText(element, text) {
      const key = FIELD_LABEL_KEYS.get(text);
      if (key) {
        bindLocalizedText(element, key);
      } else {
        element.textContent = text;
      }
      return element;
    }
    function bindKnownPlaceholder(element, placeholder) {
      const key = PLACEHOLDER_KEYS.get(placeholder);
      if (key) {
        bindLocalizedAttribute(element, "placeholder", key);
      } else {
        element.placeholder = placeholder;
      }
      return element;
    }
    function createFieldGroup(labelText, inputElement, className) {
      const group = document.createElement("label");
      group.className = `field-group ${className || ""}`.trim();
      const label = document.createElement("span");
      label.className = "field-label";
      const labelTextElement = document.createElement("span");
      labelTextElement.className = "field-label-text";
      bindKnownText(labelTextElement, labelText);
      label.appendChild(labelTextElement);
      group.appendChild(label);
      group.appendChild(inputElement);
      return group;
    }
    function createTextInput(className, placeholder) {
      const input = document.createElement("input");
      input.type = "text";
      input.className = className;
      bindKnownPlaceholder(input, placeholder);
      return input;
    }
    function createNumberInput(className, placeholder) {
      const input = document.createElement("input");
      input.type = "number";
      input.min = "0";
      input.step = "1";
      input.className = className;
      bindKnownPlaceholder(input, placeholder);
      return input;
    }
    function createOperationCostCalculatorField(initialData = {}) {
      const rateInput = createNumberInput("cost-calculator-rate-input", "0.1");
      rateInput.step = "any";
      const configuredRate = initialData.cost_calculator?.rate_usd;
      if (configuredRate !== void 0 && configuredRate !== null) {
        rateInput.value = String(configuredRate);
      }
      const field = createFieldGroup(
        "Cost per successful request (USD)",
        rateInput,
        "cost-calculator-field"
      );
      ctx.appendFieldHint(
        field,
        "editor:hints.cost"
      );
      return field;
    }
    function applyOperationCostCalculator(payload, ruleCard) {
      const rateInput = ruleCard.querySelector(".cost-calculator-rate-input");
      const rawRate = rateInput?.value.trim() || "";
      if (!rawRate) {
        return payload;
      }
      const rateUsd = Number(rawRate);
      if (!Number.isFinite(rateUsd) || rateUsd < 0) {
        throw new LocalizedUiError("editor:errors.costRate");
      }
      payload.cost_calculator = {
        unit: "operation",
        rate_usd: rateUsd
      };
      return payload;
    }
    function createTextarea(className, placeholder) {
      const textarea = document.createElement("textarea");
      textarea.className = className;
      bindKnownPlaceholder(textarea, placeholder);
      return textarea;
    }
    function createSelect(className) {
      const select = document.createElement("select");
      select.className = className;
      return select;
    }
    function createTriStateSelect(className) {
      const select = createSelect(className);
      const unknownOption = document.createElement("option");
      unknownOption.value = "";
      bindLocalizedText(unknownOption, "editor:capability.unknown");
      select.appendChild(unknownOption);
      const supportedOption = document.createElement("option");
      supportedOption.value = "true";
      bindLocalizedText(supportedOption, "editor:capability.supported");
      select.appendChild(supportedOption);
      const unsupportedOption = document.createElement("option");
      unsupportedOption.value = "false";
      bindLocalizedText(unsupportedOption, "editor:capability.unsupported");
      select.appendChild(unsupportedOption);
      return select;
    }
    function capabilityAutofillSourceFor(status, gatewayModelName, index, fieldName) {
      const resolutions = status && typeof status === "object" ? status.resolutions : null;
      const entries = resolutions ? resolutions[gatewayModelName] : null;
      if (!Array.isArray(entries)) {
        return null;
      }
      const entry = entries.find((candidate) => candidate && candidate.index === index);
      const source = entry && entry.fields ? entry.fields[fieldName]?.source : null;
      return source === "provider" || source === "openrouter" ? source : null;
    }
    function wrapCapabilityField({ fieldName, control, kind, locked, source }) {
      const wrapper = document.createElement("div");
      wrapper.className = "capability-field-slot";
      control.dataset.capabilityLocked = locked ? "true" : "false";
      wrapper.appendChild(control);
      if (!locked) {
        return wrapper;
      }
      control.hidden = true;
      const badge = document.createElement("span");
      badge.className = "capability-badge";
      badge.dataset.capabilityField = fieldName;
      const valueText = document.createElement("span");
      valueText.className = "capability-badge-value";
      if (kind === "boolean") {
        bindLocalizedText(
          valueText,
          control.value === "true" ? "editor:capability.supported" : "editor:capability.unsupported"
        );
      } else {
        valueText.textContent = control.value;
      }
      badge.appendChild(valueText);
      const autoLabel = document.createElement("span");
      autoLabel.className = "capability-badge-auto";
      bindLocalizedText(autoLabel, "editor:capability.autofilled");
      badge.appendChild(autoLabel);
      if (source === "provider" || source === "openrouter") {
        const sourceLabel = document.createElement("span");
        sourceLabel.className = "capability-badge-source";
        bindLocalizedText(sourceLabel, `editor:capability.source.${source}`);
        badge.appendChild(sourceLabel);
      }
      const editButton = document.createElement("button");
      editButton.type = "button";
      editButton.className = "capability-badge-edit secondary-button";
      bindLocalizedText(editButton, "editor:capability.override");
      editButton.addEventListener("click", () => {
        control.dataset.capabilityLocked = "false";
        control.hidden = false;
        badge.remove();
      });
      badge.appendChild(editButton);
      wrapper.appendChild(badge);
      return wrapper;
    }
    function sortProviderModelIds(modelIds) {
      return [...modelIds].sort((left, right) => {
        const comparison = ctx.gatewayI18n.getCollator({
          numeric: true,
          sensitivity: "base"
        }).compare(left, right);
        if (comparison !== 0) {
          return comparison;
        }
        return left < right ? -1 : left > right ? 1 : 0;
      });
    }
    function setSelectOptions(select, options, placeholder, selectedValue) {
      select.textContent = "";
      const placeholderOption = document.createElement("option");
      placeholderOption.value = "";
      const placeholderKey = PLACEHOLDER_KEYS.get(placeholder);
      if (placeholderKey) {
        bindLocalizedText(placeholderOption, placeholderKey);
      } else {
        placeholderOption.textContent = placeholder;
      }
      select.appendChild(placeholderOption);
      options.forEach((optionValue) => {
        const option = document.createElement("option");
        option.value = optionValue;
        option.textContent = optionValue;
        select.appendChild(option);
      });
      select.value = selectedValue && options.includes(selectedValue) ? selectedValue : "";
    }
    function setModelSelectOptions(select, options, selectedValue, placeholder) {
      const currentValue = typeof selectedValue === "string" ? selectedValue : "";
      select.textContent = "";
      const placeholderOption = document.createElement("option");
      placeholderOption.value = "";
      const placeholderKey = PLACEHOLDER_KEYS.get(placeholder || "Select a gateway model");
      if (placeholderKey) {
        bindLocalizedText(placeholderOption, placeholderKey);
      } else {
        placeholderOption.textContent = placeholder;
      }
      select.appendChild(placeholderOption);
      options.forEach((optionValue) => {
        const option = document.createElement("option");
        option.value = optionValue;
        option.textContent = optionValue;
        select.appendChild(option);
      });
      if (currentValue && !options.includes(currentValue)) {
        const staleOption = document.createElement("option");
        staleOption.value = currentValue;
        bindLocalizedText(
          staleOption,
          "editor:placeholders.notConfigured",
          () => ({ value: currentValue })
        );
        staleOption.dataset.stale = "true";
        select.appendChild(staleOption);
      }
      select.value = currentValue;
    }
    function normalizeObjectTextarea(value) {
      if (!value || Object.keys(value).length === 0) {
        return "";
      }
      return JSON.stringify(value, null, 2);
    }
    function parseObjectTextarea(value, fieldLabel) {
      const trimmedValue = value.trim();
      if (!trimmedValue) {
        return {};
      }
      let parsedValue;
      try {
        parsedValue = JSON.parse(trimmedValue);
      } catch (error) {
        const fieldKey = FIELD_LABEL_KEYS.get(fieldLabel) || "editor:fields.field";
        throw new LocalizedUiError(
          "editor:errors.fieldJson",
          () => ({ field: t(fieldKey) })
        );
      }
      if (!parsedValue || Array.isArray(parsedValue) || typeof parsedValue !== "object") {
        const fieldKey = FIELD_LABEL_KEYS.get(fieldLabel) || "editor:fields.field";
        throw new LocalizedUiError(
          "editor:errors.fieldJson",
          () => ({ field: t(fieldKey) })
        );
      }
      return parsedValue;
    }
    function parseProvidersOrder(value) {
      if (!value.trim()) {
        return void 0;
      }
      const providerNames = value.split(",").map((item) => item.trim()).filter(Boolean);
      if (providerNames.length === 0) {
        return void 0;
      }
      const unknownProviders = providerNames.filter((providerName) => !ctx.state.availableProviders.includes(providerName));
      if (unknownProviders.length > 0) {
        throw new LocalizedUiError(
          "editor:errors.unknownProviders",
          { providers: unknownProviders.join(", ") }
        );
      }
      return providerNames;
    }
    function createRetrySettingsInputs(initialData = {}) {
      const retryDelayInput = createNumberInput("retry-delay-input", "Retry delay (seconds)");
      retryDelayInput.value = initialData.retry_delay ?? "";
      const retryCountInput = createNumberInput("retry-count-input", "Retry count");
      retryCountInput.value = initialData.retry_count ?? "";
      return { retryDelayInput, retryCountInput };
    }
    function applyRetrySettingsToPayload(payload, retryDelayInput, retryCountInput) {
      if (retryDelayInput.value !== "") {
        payload.retry_delay = Number.parseFloat(retryDelayInput.value);
      }
      if (retryCountInput.value !== "") {
        payload.retry_count = Number.parseInt(retryCountInput.value, 10);
      }
    }
    function applyCapabilityFieldsToPayload(payload, visionSelect, toolsSelect, contextWindowInput) {
      const autofilledFields = [];
      if (visionSelect.value !== "") {
        payload.supports_vision = visionSelect.value === "true";
        if (visionSelect.dataset?.capabilityLocked === "true") {
          autofilledFields.push("supports_vision");
        }
      }
      if (toolsSelect.value !== "") {
        payload.supports_tools = toolsSelect.value === "true";
        if (toolsSelect.dataset?.capabilityLocked === "true") {
          autofilledFields.push("supports_tools");
        }
      }
      if (contextWindowInput.value.trim() !== "") {
        payload.context_window = Number.parseInt(contextWindowInput.value, 10);
        if (contextWindowInput.dataset?.capabilityLocked === "true") {
          autofilledFields.push("context_window");
        }
      }
      if (autofilledFields.length > 0) {
        payload.capabilities_autofilled = autofilledFields;
      }
    }
    function setupRowReordering(row) {
      let dragOriginParent = null;
      let dragOriginIndex = -1;
      row.draggable = true;
      if (isInteractionLocked()) {
        ctx.state.lockedDraggables.set(row, true);
        row.draggable = false;
      }
      row.addEventListener("dragstart", (e) => {
        if (isInteractionLocked()) {
          e.preventDefault();
          e.stopPropagation();
          window._draggedRow = null;
          return;
        }
        if (["input", "textarea", "select", "button"].includes(e.target.tagName.toLowerCase())) {
          e.preventDefault();
          return;
        }
        dragOriginParent = row.parentNode;
        dragOriginIndex = Array.from(dragOriginParent.children).indexOf(row);
        row.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        setTimeout(() => {
          if (window._draggedRow === row) {
            row.style.opacity = "0.5";
          }
        }, 0);
        window._draggedRow = row;
      });
      row.addEventListener("dragend", () => {
        const currentParent = row.parentNode;
        const currentIndex = currentParent ? Array.from(currentParent.children).indexOf(row) : -1;
        const orderChanged = dragOriginParent !== null && (currentParent !== dragOriginParent || currentIndex !== dragOriginIndex);
        row.classList.remove("dragging");
        row.style.opacity = "";
        window._draggedRow = null;
        dragOriginParent = null;
        dragOriginIndex = -1;
        if (orderChanged) {
          ctx.state.editorMutationVersion += 1;
          updateDirtyIndicator();
        }
      });
      row.addEventListener("dragover", (e) => {
        if (isInteractionLocked()) {
          e.preventDefault();
          e.stopPropagation();
          return;
        }
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        if (window._draggedRow && window._draggedRow !== row && row.classList.contains("fallback-row")) {
          if (row.parentNode !== window._draggedRow.parentNode) return;
          const bounding = row.getBoundingClientRect();
          const offset = bounding.y + bounding.height / 2;
          if (e.clientY > offset) {
            row.parentNode.insertBefore(window._draggedRow, row.nextSibling);
          } else {
            row.parentNode.insertBefore(window._draggedRow, row);
          }
        }
      });
    }
    function createMoveButtons(row) {
      const moveUpButton = document.createElement("button");
      moveUpButton.type = "button";
      moveUpButton.className = "icon-button move-up-button";
      moveUpButton.textContent = "↑";
      bindLocalizedAttribute(moveUpButton, "title", "editor:actions.moveUp");
      moveUpButton.addEventListener("click", () => {
        if (row.previousElementSibling) {
          row.parentNode.insertBefore(row, row.previousElementSibling);
        }
      });
      const moveDownButton = document.createElement("button");
      moveDownButton.type = "button";
      moveDownButton.className = "icon-button move-down-button";
      moveDownButton.textContent = "↓";
      bindLocalizedAttribute(moveDownButton, "title", "editor:actions.moveDown");
      moveDownButton.addEventListener("click", () => {
        if (row.nextElementSibling) {
          row.parentNode.insertBefore(row.nextElementSibling, row);
        }
      });
      return { moveUpButton, moveDownButton };
    }
    function isCurrentEditorDirty() {
      if (ctx.state.activeEditor === "rules" && ctx.state.originalRulesContent !== null) {
        try {
          return ctx.getRulesSnapshotContent() !== ctx.state.originalRulesContent;
        } catch (error) {
          return true;
        }
      }
      if (ctx.state.activeEditor === "embeddings" && ctx.state.originalEmbeddingsContent !== null) {
        try {
          return ctx.getNormalizedEmbeddingsContent() !== ctx.state.originalEmbeddingsContent;
        } catch (error) {
          return true;
        }
      }
      if (ctx.state.activeEditor === "rerank" && ctx.state.originalRerankContent !== null) {
        try {
          return ctx.getNormalizedRerankContent() !== ctx.state.originalRerankContent;
        } catch (error) {
          return true;
        }
      }
      if (ctx.state.activeEditor === "images" && ctx.state.originalImagesContent !== null) {
        try {
          return ctx.getNormalizedImagesContent() !== ctx.state.originalImagesContent;
        } catch (error) {
          return true;
        }
      }
      if (ctx.state.activeEditor === "audio" && ctx.state.originalAudioContent !== null) {
        try {
          return ctx.getNormalizedAudioContent() !== ctx.state.originalAudioContent;
        } catch (error) {
          return true;
        }
      }
      if (ctx.state.activeEditor === "web" && ctx.state.originalWebContent !== null) {
        try {
          return ctx.getNormalizedWebContent() !== ctx.state.originalWebContent;
        } catch (error) {
          return true;
        }
      }
      if (ctx.state.activeEditor === "providers" && ctx.state.originalProvidersContent !== null) {
        try {
          return ctx.getProvidersSnapshotContent() !== ctx.state.originalProvidersContent;
        } catch (error) {
          return true;
        }
      }
      if (ctx.state.activeEditor === "fusion" && ctx.state.originalFusionContent !== null) {
        try {
          return ctx.getNormalizedFusionContent() !== ctx.state.originalFusionContent;
        } catch (error) {
          return true;
        }
      }
      if (ctx.state.activeEditor === "router" && ctx.state.originalRouterContent !== null) {
        try {
          return ctx.getNormalizedRouterContent() !== ctx.state.originalRouterContent;
        } catch (error) {
          return true;
        }
      }
      if (ctx.state.activeEditor === "model-rules" && ctx.state.originalModelRulesContent !== null) {
        return ctx.elements.modelRulesRawInput.value !== ctx.state.originalModelRulesContent;
      }
      return false;
    }
    function beforeRulesTabActivate(context) {
      if (context.reason !== "repair" && context.previousKey !== context.key && isCurrentEditorDirty()) {
        return confirm(gatewayI18n.t("editor:errors.unsavedConfirm"));
      }
      return true;
    }
    function shouldReloadSelectedRulesTab(tabName) {
      if (tabName === "providers" && ctx.state.providersLoadState === "loading") {
        return false;
      }
      if (tabName === "providers" && ctx.state.providersLoadState === "error") {
        return true;
      }
      return !(ctx.state.originalRulesContent !== null || ctx.state.originalEmbeddingsContent !== null || ctx.state.originalRerankContent !== null || ctx.state.originalImagesContent !== null || ctx.state.originalAudioContent !== null || ctx.state.originalWebContent !== null || ctx.state.originalProvidersContent !== null || ctx.state.originalFusionContent !== null || ctx.state.originalRouterContent !== null || ctx.state.originalModelRulesContent !== null);
    }
    function activateRulesTab(context) {
      const tabName = context.key;
      const previousEditor = context.previousKey || ctx.state.activeEditor;
      if (previousEditor !== tabName) {
        if (previousEditor === "openrouter-free") {
          ctx.stopOpenRouterFreePolling();
        } else if (previousEditor === "fallback-eval") {
          ctx.stopFallbackEvalPolling();
        }
      }
      ctx.state.activeEditor = tabName;
      ctx.state.activeRulesTabContext = context;
      ctx.renderActiveEntity?.();
      updateControlsVisibility();
      ctx.elements.editorContainerOpenRouterFree.classList.remove("active");
      ctx.elements.editorContainerOpenRouterFree.style.display = "none";
      ctx.elements.editorContainerFallbackEval.classList.remove("active");
      ctx.elements.editorContainerFallbackEval.style.display = "none";
      ctx.elements.editorContainerFusion.classList.remove("active");
      ctx.elements.editorContainerFusion.style.display = "none";
      ctx.elements.editorContainerRouter.classList.remove("active");
      ctx.elements.editorContainerRouter.style.display = "none";
      ctx.elements.editorContainerModelRules.classList.remove("active");
      ctx.elements.editorContainerModelRules.style.display = "none";
      if (tabName === "rules") {
        ctx.elements.editorContainerRules.classList.add("active");
        ctx.elements.editorContainerRules.style.display = "flex";
        ctx.elements.editorContainerEmbeddings.classList.remove("active");
        ctx.elements.editorContainerEmbeddings.style.display = "none";
        ctx.elements.editorContainerRerank.classList.remove("active");
        ctx.elements.editorContainerRerank.style.display = "none";
        ctx.elements.editorContainerImages.classList.remove("active");
        ctx.elements.editorContainerImages.style.display = "none";
        ctx.elements.editorContainerAudio.classList.remove("active");
        ctx.elements.editorContainerAudio.style.display = "none";
        ctx.elements.editorContainerWeb.classList.remove("active");
        ctx.elements.editorContainerWeb.style.display = "none";
        ctx.elements.editorContainerFallbackEval.classList.remove("active");
        ctx.elements.editorContainerFallbackEval.style.display = "none";
        ctx.elements.editorContainerProviders.classList.remove("active");
        ctx.elements.editorContainerProviders.style.display = "none";
        return ctx.loadRulesEditor();
      } else if (tabName === "embeddings") {
        ctx.elements.editorContainerRules.classList.remove("active");
        ctx.elements.editorContainerRules.style.display = "none";
        ctx.elements.editorContainerEmbeddings.classList.add("active");
        ctx.elements.editorContainerEmbeddings.style.display = "flex";
        ctx.elements.editorContainerRerank.classList.remove("active");
        ctx.elements.editorContainerRerank.style.display = "none";
        ctx.elements.editorContainerImages.classList.remove("active");
        ctx.elements.editorContainerImages.style.display = "none";
        ctx.elements.editorContainerAudio.classList.remove("active");
        ctx.elements.editorContainerAudio.style.display = "none";
        ctx.elements.editorContainerWeb.classList.remove("active");
        ctx.elements.editorContainerWeb.style.display = "none";
        ctx.elements.editorContainerFallbackEval.classList.remove("active");
        ctx.elements.editorContainerFallbackEval.style.display = "none";
        ctx.elements.editorContainerProviders.classList.remove("active");
        ctx.elements.editorContainerProviders.style.display = "none";
        return ctx.loadEmbeddingsEditor();
      } else if (tabName === "rerank") {
        ctx.elements.editorContainerRules.classList.remove("active");
        ctx.elements.editorContainerRules.style.display = "none";
        ctx.elements.editorContainerEmbeddings.classList.remove("active");
        ctx.elements.editorContainerEmbeddings.style.display = "none";
        ctx.elements.editorContainerRerank.classList.add("active");
        ctx.elements.editorContainerRerank.style.display = "flex";
        ctx.elements.editorContainerImages.classList.remove("active");
        ctx.elements.editorContainerImages.style.display = "none";
        ctx.elements.editorContainerAudio.classList.remove("active");
        ctx.elements.editorContainerAudio.style.display = "none";
        ctx.elements.editorContainerWeb.classList.remove("active");
        ctx.elements.editorContainerWeb.style.display = "none";
        ctx.elements.editorContainerFallbackEval.classList.remove("active");
        ctx.elements.editorContainerFallbackEval.style.display = "none";
        ctx.elements.editorContainerProviders.classList.remove("active");
        ctx.elements.editorContainerProviders.style.display = "none";
        return ctx.loadRerankEditor();
      } else if (tabName === "images") {
        ctx.elements.editorContainerRules.classList.remove("active");
        ctx.elements.editorContainerRules.style.display = "none";
        ctx.elements.editorContainerEmbeddings.classList.remove("active");
        ctx.elements.editorContainerEmbeddings.style.display = "none";
        ctx.elements.editorContainerRerank.classList.remove("active");
        ctx.elements.editorContainerRerank.style.display = "none";
        ctx.elements.editorContainerImages.classList.add("active");
        ctx.elements.editorContainerImages.style.display = "flex";
        ctx.elements.editorContainerAudio.classList.remove("active");
        ctx.elements.editorContainerAudio.style.display = "none";
        ctx.elements.editorContainerWeb.classList.remove("active");
        ctx.elements.editorContainerWeb.style.display = "none";
        ctx.elements.editorContainerFallbackEval.classList.remove("active");
        ctx.elements.editorContainerFallbackEval.style.display = "none";
        ctx.elements.editorContainerProviders.classList.remove("active");
        ctx.elements.editorContainerProviders.style.display = "none";
        return ctx.loadImagesEditor();
      } else if (tabName === "audio") {
        ctx.elements.editorContainerRules.classList.remove("active");
        ctx.elements.editorContainerRules.style.display = "none";
        ctx.elements.editorContainerEmbeddings.classList.remove("active");
        ctx.elements.editorContainerEmbeddings.style.display = "none";
        ctx.elements.editorContainerRerank.classList.remove("active");
        ctx.elements.editorContainerRerank.style.display = "none";
        ctx.elements.editorContainerImages.classList.remove("active");
        ctx.elements.editorContainerImages.style.display = "none";
        ctx.elements.editorContainerAudio.classList.add("active");
        ctx.elements.editorContainerAudio.style.display = "flex";
        ctx.elements.editorContainerWeb.classList.remove("active");
        ctx.elements.editorContainerWeb.style.display = "none";
        ctx.elements.editorContainerFallbackEval.classList.remove("active");
        ctx.elements.editorContainerFallbackEval.style.display = "none";
        ctx.elements.editorContainerProviders.classList.remove("active");
        ctx.elements.editorContainerProviders.style.display = "none";
        return ctx.loadAudioEditor();
      } else if (tabName === "web") {
        ctx.elements.editorContainerRules.classList.remove("active");
        ctx.elements.editorContainerRules.style.display = "none";
        ctx.elements.editorContainerEmbeddings.classList.remove("active");
        ctx.elements.editorContainerEmbeddings.style.display = "none";
        ctx.elements.editorContainerRerank.classList.remove("active");
        ctx.elements.editorContainerRerank.style.display = "none";
        ctx.elements.editorContainerImages.classList.remove("active");
        ctx.elements.editorContainerImages.style.display = "none";
        ctx.elements.editorContainerAudio.classList.remove("active");
        ctx.elements.editorContainerAudio.style.display = "none";
        ctx.elements.editorContainerWeb.classList.add("active");
        ctx.elements.editorContainerWeb.style.display = "flex";
        ctx.elements.editorContainerFallbackEval.classList.remove("active");
        ctx.elements.editorContainerFallbackEval.style.display = "none";
        ctx.elements.editorContainerProviders.classList.remove("active");
        ctx.elements.editorContainerProviders.style.display = "none";
        return ctx.loadWebEditor();
      } else if (tabName === "openrouter-free") {
        ctx.elements.editorContainerRules.classList.remove("active");
        ctx.elements.editorContainerRules.style.display = "none";
        ctx.elements.editorContainerEmbeddings.classList.remove("active");
        ctx.elements.editorContainerEmbeddings.style.display = "none";
        ctx.elements.editorContainerRerank.classList.remove("active");
        ctx.elements.editorContainerRerank.style.display = "none";
        ctx.elements.editorContainerImages.classList.remove("active");
        ctx.elements.editorContainerImages.style.display = "none";
        ctx.elements.editorContainerAudio.classList.remove("active");
        ctx.elements.editorContainerAudio.style.display = "none";
        ctx.elements.editorContainerWeb.classList.remove("active");
        ctx.elements.editorContainerWeb.style.display = "none";
        ctx.elements.editorContainerOpenRouterFree.classList.add("active");
        ctx.elements.editorContainerOpenRouterFree.style.display = "flex";
        ctx.elements.editorContainerFallbackEval.classList.remove("active");
        ctx.elements.editorContainerFallbackEval.style.display = "none";
        ctx.elements.editorContainerProviders.classList.remove("active");
        ctx.elements.editorContainerProviders.style.display = "none";
        return ctx.loadOpenRouterFreeModels(true, context);
      } else if (tabName === "fallback-eval") {
        ctx.elements.editorContainerRules.classList.remove("active");
        ctx.elements.editorContainerRules.style.display = "none";
        ctx.elements.editorContainerEmbeddings.classList.remove("active");
        ctx.elements.editorContainerEmbeddings.style.display = "none";
        ctx.elements.editorContainerRerank.classList.remove("active");
        ctx.elements.editorContainerRerank.style.display = "none";
        ctx.elements.editorContainerImages.classList.remove("active");
        ctx.elements.editorContainerImages.style.display = "none";
        ctx.elements.editorContainerAudio.classList.remove("active");
        ctx.elements.editorContainerAudio.style.display = "none";
        ctx.elements.editorContainerWeb.classList.remove("active");
        ctx.elements.editorContainerWeb.style.display = "none";
        ctx.elements.editorContainerOpenRouterFree.classList.remove("active");
        ctx.elements.editorContainerOpenRouterFree.style.display = "none";
        ctx.elements.editorContainerFallbackEval.classList.add("active");
        ctx.elements.editorContainerFallbackEval.style.display = "flex";
        ctx.elements.editorContainerProviders.classList.remove("active");
        ctx.elements.editorContainerProviders.style.display = "none";
        return ctx.loadFallbackModelEvals(true, context);
      } else if (tabName === "providers") {
        ctx.elements.editorContainerRules.classList.remove("active");
        ctx.elements.editorContainerRules.style.display = "none";
        ctx.elements.editorContainerEmbeddings.classList.remove("active");
        ctx.elements.editorContainerEmbeddings.style.display = "none";
        ctx.elements.editorContainerRerank.classList.remove("active");
        ctx.elements.editorContainerRerank.style.display = "none";
        ctx.elements.editorContainerImages.classList.remove("active");
        ctx.elements.editorContainerImages.style.display = "none";
        ctx.elements.editorContainerAudio.classList.remove("active");
        ctx.elements.editorContainerAudio.style.display = "none";
        ctx.elements.editorContainerWeb.classList.remove("active");
        ctx.elements.editorContainerWeb.style.display = "none";
        ctx.elements.editorContainerFallbackEval.classList.remove("active");
        ctx.elements.editorContainerFallbackEval.style.display = "none";
        ctx.elements.editorContainerProviders.classList.add("active");
        ctx.elements.editorContainerProviders.style.display = "flex";
        return ctx.loadProvidersEditor();
      } else if (tabName === "fusion") {
        ctx.elements.editorContainerRules.classList.remove("active");
        ctx.elements.editorContainerRules.style.display = "none";
        ctx.elements.editorContainerEmbeddings.classList.remove("active");
        ctx.elements.editorContainerEmbeddings.style.display = "none";
        ctx.elements.editorContainerRerank.classList.remove("active");
        ctx.elements.editorContainerRerank.style.display = "none";
        ctx.elements.editorContainerImages.classList.remove("active");
        ctx.elements.editorContainerImages.style.display = "none";
        ctx.elements.editorContainerAudio.classList.remove("active");
        ctx.elements.editorContainerAudio.style.display = "none";
        ctx.elements.editorContainerWeb.classList.remove("active");
        ctx.elements.editorContainerWeb.style.display = "none";
        ctx.elements.editorContainerFallbackEval.classList.remove("active");
        ctx.elements.editorContainerFallbackEval.style.display = "none";
        ctx.elements.editorContainerProviders.classList.remove("active");
        ctx.elements.editorContainerProviders.style.display = "none";
        ctx.elements.editorContainerFusion.classList.add("active");
        ctx.elements.editorContainerFusion.style.display = "flex";
        return ctx.loadFusionEditor();
      } else if (tabName === "router") {
        ctx.elements.editorContainerRules.classList.remove("active");
        ctx.elements.editorContainerRules.style.display = "none";
        ctx.elements.editorContainerEmbeddings.classList.remove("active");
        ctx.elements.editorContainerEmbeddings.style.display = "none";
        ctx.elements.editorContainerRerank.classList.remove("active");
        ctx.elements.editorContainerRerank.style.display = "none";
        ctx.elements.editorContainerImages.classList.remove("active");
        ctx.elements.editorContainerImages.style.display = "none";
        ctx.elements.editorContainerAudio.classList.remove("active");
        ctx.elements.editorContainerAudio.style.display = "none";
        ctx.elements.editorContainerWeb.classList.remove("active");
        ctx.elements.editorContainerWeb.style.display = "none";
        ctx.elements.editorContainerFallbackEval.classList.remove("active");
        ctx.elements.editorContainerFallbackEval.style.display = "none";
        ctx.elements.editorContainerProviders.classList.remove("active");
        ctx.elements.editorContainerProviders.style.display = "none";
        ctx.elements.editorContainerFusion.classList.remove("active");
        ctx.elements.editorContainerFusion.style.display = "none";
        ctx.elements.editorContainerRouter.classList.add("active");
        ctx.elements.editorContainerRouter.style.display = "flex";
        return ctx.loadRouterEditor();
      } else if (tabName === "model-rules") {
        ctx.elements.editorContainerRules.classList.remove("active");
        ctx.elements.editorContainerRules.style.display = "none";
        ctx.elements.editorContainerEmbeddings.classList.remove("active");
        ctx.elements.editorContainerEmbeddings.style.display = "none";
        ctx.elements.editorContainerRerank.classList.remove("active");
        ctx.elements.editorContainerRerank.style.display = "none";
        ctx.elements.editorContainerImages.classList.remove("active");
        ctx.elements.editorContainerImages.style.display = "none";
        ctx.elements.editorContainerAudio.classList.remove("active");
        ctx.elements.editorContainerAudio.style.display = "none";
        ctx.elements.editorContainerWeb.classList.remove("active");
        ctx.elements.editorContainerWeb.style.display = "none";
        ctx.elements.editorContainerFallbackEval.classList.remove("active");
        ctx.elements.editorContainerFallbackEval.style.display = "none";
        ctx.elements.editorContainerProviders.classList.remove("active");
        ctx.elements.editorContainerProviders.style.display = "none";
        ctx.elements.editorContainerFusion.classList.remove("active");
        ctx.elements.editorContainerFusion.style.display = "none";
        ctx.elements.editorContainerRouter.classList.remove("active");
        ctx.elements.editorContainerRouter.style.display = "none";
        ctx.elements.editorContainerModelRules.classList.add("active");
        ctx.elements.editorContainerModelRules.style.display = "flex";
        return ctx.loadModelRulesEditor();
      }
    }
    function reselectRulesTab(context) {
      ctx.state.activeRulesTabContext = context;
      if (ctx.state.evalTabLoadStates.has(context.key) && ctx.state.evalTabLoadStates.get(context.key) !== "ready") {
        return activateRulesTab(context);
      }
      if (context.key === "openrouter-free" && ctx.state.openRouterFreePollingEnabled) {
        clearTimeout(ctx.state.openRouterFreePollTimer);
        ctx.state.openRouterFreePollTimer = window.setTimeout(() => {
          void ctx.loadOpenRouterFreeModels(false, ctx.state.activeRulesTabContext);
        }, 3e3);
      } else if (context.key === "fallback-eval" && ctx.state.fallbackEvalPollingEnabled) {
        clearTimeout(ctx.state.fallbackEvalPollTimer);
        ctx.state.fallbackEvalPollTimer = window.setTimeout(() => {
          void ctx.loadFallbackModelEvals(false, ctx.state.activeRulesTabContext);
        }, 3e3);
      }
      if (!shouldReloadSelectedRulesTab(context.key)) {
        return false;
      }
      return activateRulesTab(context);
    }
    async function reloadActiveDocument() {
      if (ctx.state.activeEditor === "rules") {
        return ctx.loadRulesEditor();
      }
      if (ctx.state.activeEditor === "embeddings") {
        return ctx.loadEmbeddingsEditor();
      }
      if (ctx.state.activeEditor === "rerank") {
        return ctx.loadRerankEditor();
      }
      if (ctx.state.activeEditor === "images") {
        return ctx.loadImagesEditor();
      }
      if (ctx.state.activeEditor === "audio") {
        return ctx.loadAudioEditor();
      }
      if (ctx.state.activeEditor === "web") {
        return ctx.loadWebEditor();
      }
      if (ctx.state.activeEditor === "fusion") {
        return ctx.loadFusionEditor();
      }
      if (ctx.state.activeEditor === "router") {
        return ctx.loadRouterEditor();
      }
      if (ctx.state.activeEditor === "providers") {
        return ctx.loadProvidersEditor();
      }
      if (ctx.state.activeEditor === "model-rules") {
        return ctx.loadModelRulesEditor();
      }
      return false;
    }
    Object.assign(ctx, {
      t,
      renderLocalizedBinding,
      bindLocalizedText,
      bindLocalizedAttribute,
      bindLocalizedValue,
      setRawDetail,
      localizedMessageDescriptor,
      typeForUnknownMessage,
      resolveDescriptorValues,
      showLocalizedMessage,
      rerenderLocale,
      boundedSafeText,
      safeResponseError,
      safeClientError,
      safeSuccessMessage,
      showClientValidationError,
      cloneConfigPayload,
      requireStrongEtag,
      commitDocumentBase,
      getDocumentBase,
      getOperationBasePayload,
      isRecord,
      requireArrayProperty,
      validateFallbackPayload,
      validateOperationPayload,
      validateFusionPayload,
      validateRouterPayload,
      validateProvidersPayload,
      readJsonBody,
      showConflict,
      clearConflict,
      resyncConfigSources,
      reloadAfterConflict,
      currentDocumentName,
      isInteractionLocked,
      syncInteractionLock,
      setDocumentBusy,
      loadConfigDocument,
      saveConfigDocument,
      showLocalizedError,
      clearElement,
      formatDateTime,
      formatNumber,
      updateControlsVisibility,
      isActiveEditorLoaded,
      updateSaveButtonDisabledState,
      updateDirtyIndicator,
      updateProvidersControlsState,
      setProvidersLoadState,
      refreshRulesEmptyState,
      refreshEmbeddingsEmptyState,
      refreshRerankEmptyState,
      refreshImageGenerationEmptyState,
      refreshImageEditEmptyState,
      refreshAudioSpeechEmptyState,
      refreshAudioTranscriptionsEmptyState,
      refreshWebSearchEmptyState,
      refreshWebReadEmptyState,
      refreshWebResearchEmptyState,
      refreshWebDeepResearchEmptyState,
      refreshProvidersEmptyState,
      refreshFusionEmptyState,
      refreshRouterEmptyState,
      bindKnownActionText,
      bindKnownText,
      bindKnownPlaceholder,
      createFieldGroup,
      createTextInput,
      createNumberInput,
      createOperationCostCalculatorField,
      applyOperationCostCalculator,
      createTextarea,
      createSelect,
      createTriStateSelect,
      capabilityAutofillSourceFor,
      wrapCapabilityField,
      sortProviderModelIds,
      setSelectOptions,
      setModelSelectOptions,
      normalizeObjectTextarea,
      parseObjectTextarea,
      parseProvidersOrder,
      createRetrySettingsInputs,
      applyRetrySettingsToPayload,
      applyCapabilityFieldsToPayload,
      setupRowReordering,
      createMoveButtons,
      isCurrentEditorDirty,
      beforeRulesTabActivate,
      shouldReloadSelectedRulesTab,
      activateRulesTab,
      reselectRulesTab,
      reloadActiveDocument,
      ConfigUiError,
      LocalizedUiError
    });
  }
  var ENTITY_DOCUMENT_LABEL_KEYS = {
    rules: "editor:tabs.rules",
    embeddings: "editor:tabs.embeddings",
    rerank: "editor:tabs.rerank",
    images: "editor:tabs.images",
    audio: "editor:tabs.audio",
    web: "editor:tabs.web",
    fusion: "editor:tabs.fusion",
    router: "editor:tabs.router",
    "model-rules": "editor:tabs.modelRules",
    "openrouter-free": "editor:tabs.openrouterFree",
    "fallback-eval": "editor:tabs.fallbackEval",
    providers: "editor:tabs.providers"
  };
  var ENTITY_VALIDATION_LABEL_KEYS = {
    dirty: "editor:footer.unsavedChanges",
    clean: "editor:footer.allSaved"
  };
  function registerEntityPanel(ctx) {
    const entityNavList = document.getElementById("entityNavList");
    const entityItems = Array.from(document.querySelectorAll(".editor-entity-item"));
    const entityGroups = Array.from(document.querySelectorAll(".editor-entity-group"));
    const searchInput = document.getElementById("entitySearchInput");
    const searchEmptyState = document.getElementById("entityListEmptyState");
    const footerDocument = document.getElementById("editorFooterDocument");
    const footerValidation = document.getElementById("editorFooterValidation");
    const footerPreviewButton = document.getElementById("footerPreviewButton");
    if (!entityNavList || entityItems.length === 0) {
      return;
    }
    function t(key, values = {}) {
      return ctx.gatewayI18n.t(key, values);
    }
    function renderValidationSummary() {
      if (!footerValidation || ctx.gatewayI18n.currentLocale === null) return;
      const dirty = ctx.isCurrentEditorDirty();
      const labelKey = ENTITY_VALIDATION_LABEL_KEYS[dirty ? "dirty" : "clean"];
      footerValidation.textContent = t(labelKey);
      footerValidation.classList.toggle("is-dirty", dirty);
    }
    function renderActiveEntity() {
      if (ctx.gatewayI18n.currentLocale === null) return;
      const key = ctx.state.activeEditor || null;
      entityItems.forEach((item) => {
        const isActive = key !== null && item.dataset.entityTarget === key;
        item.classList.toggle("active", isActive);
        item.setAttribute("aria-current", isActive ? "true" : "false");
      });
      const labelKey = key ? ENTITY_DOCUMENT_LABEL_KEYS[key] : null;
      const label = labelKey ? t(labelKey) : "";
      if (footerDocument) {
        footerDocument.textContent = label;
      }
      if (footerPreviewButton) {
        footerPreviewButton.hidden = key !== "rules";
      }
      renderValidationSummary();
    }
    function applySearchFilter() {
      const query = (searchInput?.value || "").trim().toLowerCase();
      let anyGroupVisible = false;
      entityGroups.forEach((group) => {
        let groupHasVisible = false;
        group.querySelectorAll(".editor-entity-item").forEach((item) => {
          if (item.hidden) {
            item.classList.remove("is-filtered");
            return;
          }
          const matches = !query || item.textContent.toLowerCase().includes(query);
          item.classList.toggle("is-filtered", !matches);
          if (matches) groupHasVisible = true;
        });
        group.hidden = !groupHasVisible;
        if (groupHasVisible) anyGroupVisible = true;
      });
      if (searchEmptyState) {
        searchEmptyState.hidden = anyGroupVisible;
      }
    }
    entityItems.forEach((item) => {
      item.addEventListener("click", () => {
        const target = item.dataset.entityTarget;
        const controller = ctx.state.rulesTabsController;
        if (!target || !controller) return;
        void controller.activate(target, { reason: "pointer", focus: false });
      });
    });
    if (searchInput) {
      ["input", "change"].forEach((eventName) => {
        searchInput.addEventListener(eventName, (event) => {
          event.stopPropagation();
          applySearchFilter();
        });
      });
    }
    if (footerPreviewButton) {
      footerPreviewButton.addEventListener("click", () => {
        ctx.elements.previewRulesButton?.click();
      });
    }
    const entityAvailabilityObserver = new MutationObserver(() => {
      renderActiveEntity();
      applySearchFilter();
    });
    entityItems.forEach((item) => {
      entityAvailabilityObserver.observe(item, {
        attributes: true,
        attributeFilter: ["hidden"]
      });
    });
    if (ctx.elements.saveButton) {
      const dirtyObserver = new MutationObserver(renderValidationSummary);
      dirtyObserver.observe(ctx.elements.saveButton, {
        attributes: true,
        attributeFilter: ["data-editor-dirty"]
      });
    }
    ctx.gatewayI18n.subscribe(renderActiveEntity);
    ctx.gatewayI18n.ready.then(renderActiveEntity).catch(() => void 0);
    renderActiveEntity();
    applySearchFilter();
    Object.assign(ctx, { renderActiveEntity });
  }
  function createRulesTabsController(ctx) {
    let activeKey = ctx.state.activeEditor ?? null;
    let sequence = 0;
    let pendingAbort = null;
    let activeAbort = null;
    function getEntityItems() {
      return Array.from(document.querySelectorAll(".editor-entity-item"));
    }
    function beginOperation() {
      pendingAbort?.abort();
      pendingAbort = new AbortController();
      sequence += 1;
      return { abortController: pendingAbort, token: sequence };
    }
    function isPending(abortController, token) {
      return pendingAbort === abortController && sequence === token && !abortController.signal.aborted;
    }
    function isActive(abortController) {
      return activeAbort === abortController && !abortController.signal.aborted;
    }
    function promote(abortController) {
      activeAbort?.abort();
      activeAbort = abortController;
      pendingAbort = null;
    }
    async function activate(key, activateOptions = {}) {
      const reason = activateOptions.reason ?? "programmatic";
      const previousKey = activeKey;
      const { abortController, token } = beginOperation();
      const context = Object.freeze({
        key,
        previousKey,
        reason,
        signal: abortController.signal,
        isCurrent: () => isPending(abortController, token) || isActive(abortController)
      });
      if (key === activeKey) {
        promote(abortController);
        if (!ctx.reselectRulesTab) return true;
        await ctx.reselectRulesTab(context);
        return isActive(abortController);
      }
      if (ctx.beforeRulesTabActivate) {
        const permitted = await ctx.beforeRulesTabActivate(context);
        if (!isPending(abortController, token)) return false;
        if (permitted === false) {
          pendingAbort = null;
          abortController.abort();
          return false;
        }
      }
      if (!isPending(abortController, token)) return false;
      promote(abortController);
      activeKey = key;
      if (ctx.activateRulesTab) await ctx.activateRulesTab(context);
      return isActive(abortController);
    }
    async function repair() {
      const items = getEntityItems();
      const index = items.findIndex((el) => el.dataset.entityTarget === activeKey);
      const activeItem = items[index];
      if (activeItem && !activeItem.hidden) return false;
      const forward = items.slice(index + 1).find((el) => !el.hidden);
      const backward = items.slice(0, Math.max(index, 0)).reverse().find((el) => !el.hidden);
      const replacement = forward || backward;
      if (!replacement) return false;
      return activate(replacement.dataset.entityTarget, { reason: "repair", focus: false });
    }
    return Object.freeze({
      activate,
      repair,
      get activeKey() {
        return activeKey;
      }
    });
  }

  // src/operations.mjs
  function registerOperations(ctx) {
    const { gatewayI18n } = ctx;
    const ConfigUiError = ctx.ConfigUiError;
    function normalizeOperationRulesPayload(payload = {}) {
      const normalized = {
        embeddings: Array.isArray(payload.embeddings) ? payload.embeddings : [],
        rerank: Array.isArray(payload.rerank) ? payload.rerank : [],
        images_generations: Array.isArray(payload.images_generations) ? payload.images_generations : [],
        images_edits: Array.isArray(payload.images_edits) ? payload.images_edits : []
      };
      if (Object.prototype.hasOwnProperty.call(payload, "audio_transcriptions")) {
        normalized.audio_transcriptions = Array.isArray(payload.audio_transcriptions) ? payload.audio_transcriptions : [];
      }
      ["audio_speech", "pdf_conversions"].forEach((sectionName) => {
        if (Object.prototype.hasOwnProperty.call(payload, sectionName)) {
          normalized[sectionName] = Array.isArray(payload[sectionName]) ? payload[sectionName] : [];
        }
      });
      ["web_search", "web_read", "web_research", "web_deep_research"].forEach((sectionName) => {
        if (Object.prototype.hasOwnProperty.call(payload, sectionName)) {
          normalized[sectionName] = Array.isArray(payload[sectionName]) ? payload[sectionName] : [];
        }
      });
      return normalized;
    }
    function applyOperationRulesPayload(payload = {}) {
      const normalized = normalizeOperationRulesPayload(payload);
      ctx.state.embeddingRules = normalized.embeddings;
      ctx.state.rerankRules = normalized.rerank;
      ctx.state.imageGenerationRules = normalized.images_generations;
      ctx.state.imageEditRules = normalized.images_edits;
      ctx.state.audioSpeechRules = normalized.audio_speech || [];
      ctx.state.audioTranscriptionRules = normalized.audio_transcriptions || [];
      ctx.state.pdfConversionRules = normalized.pdf_conversions || [];
      ctx.state.webSearchRules = normalized.web_search || [];
      ctx.state.webReadRules = normalized.web_read || [];
      ctx.state.webResearchRules = normalized.web_research || [];
      ctx.state.webDeepResearchRules = normalized.web_deep_research || [];
      return normalized;
    }
    function buildOperationRoutesPayload(overrides = {}, basePayload = null) {
      const source = basePayload ? normalizeOperationRulesPayload(basePayload) : {
        embeddings: ctx.state.embeddingRules,
        rerank: ctx.state.rerankRules,
        images_generations: ctx.state.imageGenerationRules,
        images_edits: ctx.state.imageEditRules
      };
      if (!basePayload && ctx.state.audioTranscriptionRules.length > 0) {
        source.audio_transcriptions = ctx.state.audioTranscriptionRules;
      }
      if (!basePayload && ctx.state.audioSpeechRules.length > 0) {
        source.audio_speech = ctx.state.audioSpeechRules;
      }
      if (!basePayload && ctx.state.pdfConversionRules.length > 0) {
        source.pdf_conversions = ctx.state.pdfConversionRules;
      }
      const payload = {
        embeddings: overrides.embeddings ?? source.embeddings,
        rerank: overrides.rerank ?? source.rerank,
        images_generations: overrides.images_generations ?? source.images_generations,
        images_edits: overrides.images_edits ?? source.images_edits
      };
      if (Object.prototype.hasOwnProperty.call(overrides, "audio_transcriptions") || Object.prototype.hasOwnProperty.call(source, "audio_transcriptions") || ctx.state.audioTranscriptionRules.length > 0) {
        payload.audio_transcriptions = overrides.audio_transcriptions ?? (source.audio_transcriptions || []);
      }
      ["audio_speech", "pdf_conversions"].forEach((sectionName) => {
        if (Object.prototype.hasOwnProperty.call(overrides, sectionName) || Object.prototype.hasOwnProperty.call(source, sectionName)) {
          payload[sectionName] = overrides[sectionName] ?? (source[sectionName] || []);
        }
      });
      ["web_search", "web_read", "web_research", "web_deep_research"].forEach((sectionName) => {
        if (Object.prototype.hasOwnProperty.call(overrides, sectionName) || Object.prototype.hasOwnProperty.call(source, sectionName)) {
          payload[sectionName] = overrides[sectionName] ?? (source[sectionName] || []);
        }
      });
      return payload;
    }
    Theme.attachToggle("darkModeToggle");
    async function loadOperationRulesPayload(configName, applyPayload) {
      ctx.showLocalizedMessage("info", `Loading ${configName}...`);
      return ctx.loadConfigDocument(
        "operation",
        "/v1/config/model-operations/structured",
        {
          validate: ctx.validateOperationPayload,
          apply: async (payload) => {
            await ctx.ensureAvailableProvidersLoaded();
            applyOperationRulesPayload(payload);
            await applyPayload(payload);
          }
        }
      );
    }
    async function saveOperationPayload(payload, errorTitle, applyPublished) {
      const result = await ctx.saveConfigDocument(
        "operation",
        "/v1/config/model-operations/structured",
        payload,
        {
          errorTitle,
          validatePublished: ctx.validateOperationPayload
        }
      );
      if (!result) {
        return null;
      }
      if (ctx.state.editorMutationVersion === result.submittedMutationVersion) {
        applyOperationRulesPayload(result.payload);
        const application = applyPublished(result.payload);
        ctx.syncInteractionLock();
        await application;
      }
      return result;
    }
    function collectCurrentWebSectionModels(listElement) {
      return Array.from(listElement.querySelectorAll(".rule-card > .rule-card-header .gateway-model-input")).map((input) => input.value.trim()).filter(Boolean);
    }
    function refreshWebCrossDropdowns() {
      ctx.state.gatewayModelCatalog.web_search = collectCurrentWebSectionModels(ctx.elements.webSearchList);
      ctx.state.gatewayModelCatalog.web_read = collectCurrentWebSectionModels(ctx.elements.webReadList);
      const crossSelectors = [
        { selector: ".search-model-input", options: ctx.state.gatewayModelCatalog.web_search },
        { selector: ".read-model-input", options: ctx.state.gatewayModelCatalog.web_read }
      ];
      [ctx.elements.webResearchList, ctx.elements.webDeepResearchList].forEach((list) => {
        crossSelectors.forEach(({ selector, options }) => {
          list.querySelectorAll(selector).forEach((select) => {
            if (select.tagName !== "SELECT") return;
            ctx.setModelSelectOptions(select, options, select.value);
          });
        });
      });
      const chatSelects = [
        ...ctx.elements.webSearchList.querySelectorAll(".query-model-input")
      ];
      chatSelects.forEach((select) => {
        if (select.tagName !== "SELECT") return;
        ctx.setModelSelectOptions(select, ctx.state.gatewayModelCatalog.chat, select.value);
      });
    }
    async function loadGatewayModelCatalog() {
      const response = await ctx.apiFetch("/v1/config/models-rules/structured");
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      const rules = Array.isArray(payload.rules) ? payload.rules : [];
      ctx.state.gatewayModelCatalog.chat = rules.map((rule) => typeof rule.gateway_model_name === "string" ? rule.gateway_model_name.trim() : "").filter(Boolean);
    }
    function applyOperationCatalog(normalizedPayload) {
      ctx.state.gatewayModelCatalog.embeddings = (normalizedPayload.embeddings || []).map((item) => typeof item.gateway_model_name === "string" ? item.gateway_model_name.trim() : "").filter(Boolean);
      ctx.state.gatewayModelCatalog.rerank = (normalizedPayload.rerank || []).map((item) => typeof item.gateway_model_name === "string" ? item.gateway_model_name.trim() : "").filter(Boolean);
      ctx.state.gatewayModelCatalog.images_generations = (normalizedPayload.images_generations || []).map((item) => typeof item.gateway_model_name === "string" ? item.gateway_model_name.trim() : "").filter(Boolean);
      ctx.state.gatewayModelCatalog.web_search = (normalizedPayload.web_search || []).map((item) => typeof item.gateway_model_name === "string" ? item.gateway_model_name.trim() : "").filter(Boolean);
      ctx.state.gatewayModelCatalog.web_read = (normalizedPayload.web_read || []).map((item) => typeof item.gateway_model_name === "string" ? item.gateway_model_name.trim() : "").filter(Boolean);
    }
    function getEmbeddingsPayloadForSave(basePayload = null) {
      const embeddings = Array.from(ctx.elements.embeddingsList.querySelectorAll(".rule-card")).map(normalizeEmbeddingCardForSave);
      return buildOperationRoutesPayload({ embeddings }, basePayload);
    }
    function getNormalizedEmbeddingsContent() {
      return ctx.stableSerialize(getEmbeddingsPayloadForSave());
    }
    function normalizeEmbeddingRouteForSave(routeRow) {
      const providerSelect = routeRow.querySelector(".provider-select");
      const modelInput = routeRow.querySelector(".model-input");
      const customBodyParamsInput = routeRow.querySelector(".custom-body-params-input");
      const customHeadersInput = routeRow.querySelector(".custom-headers-input");
      const targetPathInput = routeRow.querySelector(".target-path-input");
      const retryDelayInput = routeRow.querySelector(".retry-delay-input");
      const retryCountInput = routeRow.querySelector(".retry-count-input");
      const provider = providerSelect.value.trim();
      const model = modelInput.value.trim();
      if (!provider) {
        throw new Error("Each embedding route must have a provider selected.");
      }
      if (!model) {
        throw new Error(`Enter or choose a model for provider '${provider}' before saving.`);
      }
      const routePayload = {
        provider,
        model,
        target_path: targetPathInput.value.trim() || "/embeddings",
        custom_body_params: ctx.parseObjectTextarea(customBodyParamsInput.value, "Custom body params"),
        custom_headers: ctx.parseObjectTextarea(customHeadersInput.value, "Custom headers")
      };
      ctx.applyRetrySettingsToPayload(routePayload, retryDelayInput, retryCountInput);
      return routePayload;
    }
    function normalizeEmbeddingCardForSave(ruleCard) {
      const gatewayModelInput = ruleCard.querySelector(".gateway-model-input");
      const routeRows = Array.from(ruleCard.querySelectorAll(".fallback-list > .fallback-row"));
      const gatewayModelName = gatewayModelInput.value.trim();
      if (!gatewayModelName) {
        throw new Error("Each embedding model rule must have a gateway model name.");
      }
      if (routeRows.length === 0) {
        throw new Error(`Embedding model '${gatewayModelName}' must contain at least one route.`);
      }
      return {
        gateway_model_name: gatewayModelName,
        routes: routeRows.map(normalizeEmbeddingRouteRowForSave)
      };
    }
    function normalizeEmbeddingRouteRowForSave(routeRow) {
      return normalizeEmbeddingRouteForSave(routeRow);
    }
    async function loadEmbeddingsEditor() {
      try {
        const loaded = await loadOperationRulesPayload(
          "Embeddings Routes",
          async (payload) => {
            await renderEmbeddings(payload.embeddings);
          }
        );
        if (!loaded) {
          ctx.showLocalizedMessage("warning", "A newer local edit was preserved. Reload again to discard it.");
          return false;
        }
        ctx.state.originalEmbeddingsContent = getNormalizedEmbeddingsContent();
        ctx.updateSaveButtonDisabledState();
        ctx.showLocalizedMessage("success", "Embeddings Routes loaded successfully.");
        return true;
      } catch (error) {
        console.error("Error fetching Embeddings Routes:", error);
        ctx.showLocalizedError("Error loading Embeddings Routes:", error);
        ctx.state.originalEmbeddingsContent = null;
        ctx.updateSaveButtonDisabledState();
        return false;
      }
    }
    function renderEmbeddings(embeddings) {
      ctx.elements.embeddingsList.textContent = "";
      if (!Array.isArray(embeddings) || embeddings.length === 0) {
        ctx.refreshEmbeddingsEmptyState();
        return;
      }
      embeddings.forEach((embedding) => {
        const embeddingCard = buildEmbeddingCard(embedding);
        ctx.elements.embeddingsList.appendChild(embeddingCard);
      });
      ctx.refreshEmbeddingsEmptyState();
    }
    function buildEmbeddingCard(initialData) {
      const card = document.createElement("section");
      card.className = "rule-card";
      const cardHeader = document.createElement("div");
      cardHeader.className = "rule-card-header";
      const titleWrap = document.createElement("div");
      titleWrap.className = "rule-card-title";
      const gatewayModelInput = ctx.createTextInput("gateway-model-input", "llmgateway/embedding-model");
      gatewayModelInput.value = initialData.gateway_model_name || "";
      titleWrap.appendChild(ctx.createFieldGroup("Gateway Model Name", gatewayModelInput, "gateway-model-field"));
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, "Remove Model");
      removeButton.addEventListener("click", () => {
        card.remove();
        ctx.refreshEmbeddingsEmptyState();
      });
      cardHeader.appendChild(titleWrap);
      cardHeader.appendChild(removeButton);
      const routeList = document.createElement("div");
      routeList.className = "fallback-list";
      const addRouteButton = document.createElement("button");
      addRouteButton.type = "button";
      addRouteButton.className = "secondary-button add-fallback-button";
      ctx.bindKnownActionText(addRouteButton, "Add Fallback Route");
      addRouteButton.addEventListener("click", () => {
        routeList.appendChild(buildEmbeddingRouteRow({}));
      });
      const cardBody = document.createElement("div");
      cardBody.className = "rule-card-body";
      cardBody.appendChild(routeList);
      cardBody.appendChild(addRouteButton);
      const accordionToggle = document.createElement("button");
      accordionToggle.type = "button";
      accordionToggle.className = "accordion-toggle";
      const svgNS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("width", "20");
      svg.setAttribute("height", "20");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("fill", "none");
      svg.setAttribute("stroke", "currentColor");
      svg.setAttribute("stroke-width", "2");
      svg.setAttribute("stroke-linecap", "round");
      svg.setAttribute("stroke-linejoin", "round");
      const polyline = document.createElementNS(svgNS, "polyline");
      polyline.setAttribute("points", "6 9 12 15 18 9");
      svg.appendChild(polyline);
      accordionToggle.appendChild(svg);
      accordionToggle.addEventListener("click", () => {
        ctx.toggleProviderCatalogCard(card);
      });
      const headerLeft = document.createElement("div");
      headerLeft.className = "rule-card-header-left";
      headerLeft.appendChild(accordionToggle);
      headerLeft.appendChild(titleWrap);
      while (cardHeader.firstChild) {
        cardHeader.removeChild(cardHeader.firstChild);
      }
      cardHeader.appendChild(headerLeft);
      cardHeader.appendChild(removeButton);
      card.classList.add("collapsed");
      card.appendChild(cardHeader);
      card.appendChild(cardBody);
      const routes = Array.isArray(initialData.routes) ? initialData.routes : [];
      routes.forEach((route) => {
        const routeRow = buildEmbeddingRouteRow(route);
        routeList.appendChild(routeRow);
      });
      if (routes.length === 0) {
        const routeRow = buildEmbeddingRouteRow({});
        routeList.appendChild(routeRow);
      }
      return card;
    }
    function buildEmbeddingRouteRow(initialData) {
      const row = document.createElement("div");
      row.className = "fallback-row";
      ctx.setupRowReordering(row);
      const fieldsGrid = document.createElement("div");
      fieldsGrid.className = "fallback-row-grid";
      const providerSelect = ctx.createSelect("provider-select");
      ctx.setSelectOptions(providerSelect, ctx.state.availableProviders, "Choose a provider", initialData.provider || "");
      const modelInput = ctx.createTextInput("model-input", "Choose or enter model");
      modelInput.value = initialData.model || "";
      const dataListId = `models-list-${Math.random().toString(36).substr(2, 9)}`;
      modelInput.setAttribute("list", dataListId);
      const dataList = document.createElement("datalist");
      dataList.id = dataListId;
      row.appendChild(dataList);
      const targetPathInput = ctx.createTextInput("target-path-input", "/embeddings");
      targetPathInput.value = initialData.target_path || "/embeddings";
      targetPathInput.readOnly = true;
      const { retryDelayInput, retryCountInput } = ctx.createRetrySettingsInputs(initialData);
      const customBodyParamsInput = ctx.createTextarea("custom-body-params-input", '{"param": "value"}');
      customBodyParamsInput.value = ctx.normalizeObjectTextarea(initialData.custom_body_params);
      const customHeadersInput = ctx.createTextarea("custom-headers-input", '{"X-Header": "value"}');
      customHeadersInput.value = ctx.normalizeObjectTextarea(initialData.custom_headers);
      fieldsGrid.appendChild(ctx.createFieldGroup("Provider", providerSelect, "provider-field"));
      fieldsGrid.appendChild(ctx.createFieldGroup("Model", modelInput, "model-field"));
      fieldsGrid.appendChild(ctx.createFieldGroup("Target Path", targetPathInput));
      const modelStatus = document.createElement("div");
      modelStatus.className = "model-status";
      modelStatus.dataset.state = "idle";
      const advancedDetails = document.createElement("details");
      advancedDetails.className = "advanced-options";
      const advancedSummary = document.createElement("summary");
      ctx.bindLocalizedText(advancedSummary, "editor:actions.advanced");
      advancedDetails.appendChild(advancedSummary);
      const advancedGrid = document.createElement("div");
      advancedGrid.className = "advanced-grid";
      advancedGrid.appendChild(ctx.createFieldGroup("Retry Delay", retryDelayInput));
      advancedGrid.appendChild(ctx.createFieldGroup("Retry Count", retryCountInput));
      advancedGrid.appendChild(ctx.createFieldGroup("Custom Body Params", customBodyParamsInput, "textarea-group"));
      advancedGrid.appendChild(ctx.createFieldGroup("Custom Headers", customHeadersInput, "textarea-group"));
      advancedDetails.appendChild(advancedGrid);
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, "Remove Fallback Route");
      removeButton.addEventListener("click", () => {
        row.remove();
      });
      const rowActions = document.createElement("div");
      rowActions.className = "fallback-row-actions";
      const { moveUpButton, moveDownButton } = ctx.createMoveButtons(row);
      rowActions.appendChild(moveUpButton);
      rowActions.appendChild(moveDownButton);
      rowActions.appendChild(removeButton);
      row.appendChild(fieldsGrid);
      row.appendChild(modelStatus);
      row.appendChild(advancedDetails);
      row.appendChild(rowActions);
      ctx.createLazyProviderCatalogRowController({
        row,
        providerSelect,
        modelControl: modelInput,
        dataList,
        modelStatus
      });
      return row;
    }
    async function loadRerankEditor() {
      try {
        const loaded = await loadOperationRulesPayload(
          "Rerank Routes",
          async (payload) => {
            await renderRerank(payload.rerank);
          }
        );
        if (!loaded) {
          ctx.showLocalizedMessage("warning", "A newer local edit was preserved. Reload again to discard it.");
          return false;
        }
        ctx.state.originalRerankContent = getNormalizedRerankContent();
        ctx.updateSaveButtonDisabledState();
        ctx.showLocalizedMessage("success", "Rerank Routes loaded successfully.");
        return true;
      } catch (error) {
        console.error("Error fetching Rerank Routes:", error);
        ctx.showLocalizedError("Error loading Rerank Routes:", error);
        ctx.state.originalRerankContent = null;
        ctx.updateSaveButtonDisabledState();
        return false;
      }
    }
    function renderRerank(rerank) {
      ctx.elements.rerankList.textContent = "";
      if (!Array.isArray(rerank) || rerank.length === 0) {
        ctx.refreshRerankEmptyState();
        return;
      }
      rerank.forEach((item) => {
        const rerankCard = buildRerankCard(item);
        ctx.elements.rerankList.appendChild(rerankCard);
      });
      ctx.refreshRerankEmptyState();
    }
    function buildRerankCard(initialData) {
      const card = document.createElement("section");
      card.className = "rule-card";
      const cardHeader = document.createElement("div");
      cardHeader.className = "rule-card-header";
      const titleWrap = document.createElement("div");
      titleWrap.className = "rule-card-title";
      const gatewayModelInput = ctx.createTextInput("gateway-model-input", "llmgateway/rerank-model");
      gatewayModelInput.value = initialData.gateway_model_name || "";
      titleWrap.appendChild(ctx.createFieldGroup("Gateway Model Name", gatewayModelInput, "gateway-model-field"));
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, "Remove Model");
      removeButton.addEventListener("click", () => {
        card.remove();
        ctx.refreshRerankEmptyState();
      });
      cardHeader.appendChild(titleWrap);
      cardHeader.appendChild(removeButton);
      const routeList = document.createElement("div");
      routeList.className = "fallback-list";
      const addRouteButton = document.createElement("button");
      addRouteButton.type = "button";
      addRouteButton.className = "secondary-button add-fallback-button";
      ctx.bindKnownActionText(addRouteButton, "Add Fallback Route");
      addRouteButton.addEventListener("click", () => {
        routeList.appendChild(buildRerankRouteRow({}));
      });
      const cardBody = document.createElement("div");
      cardBody.className = "rule-card-body";
      cardBody.appendChild(routeList);
      cardBody.appendChild(addRouteButton);
      const accordionToggle = document.createElement("button");
      accordionToggle.type = "button";
      accordionToggle.className = "accordion-toggle";
      const svgNS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("width", "20");
      svg.setAttribute("height", "20");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("fill", "none");
      svg.setAttribute("stroke", "currentColor");
      svg.setAttribute("stroke-width", "2");
      svg.setAttribute("stroke-linecap", "round");
      svg.setAttribute("stroke-linejoin", "round");
      const polyline = document.createElementNS(svgNS, "polyline");
      polyline.setAttribute("points", "6 9 12 15 18 9");
      svg.appendChild(polyline);
      accordionToggle.appendChild(svg);
      accordionToggle.addEventListener("click", () => {
        ctx.toggleProviderCatalogCard(card);
      });
      const headerLeft = document.createElement("div");
      headerLeft.className = "rule-card-header-left";
      headerLeft.appendChild(accordionToggle);
      headerLeft.appendChild(titleWrap);
      while (cardHeader.firstChild) {
        cardHeader.removeChild(cardHeader.firstChild);
      }
      cardHeader.appendChild(headerLeft);
      cardHeader.appendChild(removeButton);
      card.classList.add("collapsed");
      card.appendChild(cardHeader);
      card.appendChild(cardBody);
      const routes = Array.isArray(initialData.routes) ? initialData.routes : [];
      routes.forEach((route) => {
        const routeRow = buildRerankRouteRow(route);
        routeList.appendChild(routeRow);
      });
      if (routes.length === 0) {
        const routeRow = buildRerankRouteRow({});
        routeList.appendChild(routeRow);
      }
      return card;
    }
    function buildRerankRouteRow(initialData) {
      const row = document.createElement("div");
      row.className = "fallback-row";
      ctx.setupRowReordering(row);
      const fieldsGrid = document.createElement("div");
      fieldsGrid.className = "fallback-row-grid";
      const providerSelect = ctx.createSelect("provider-select");
      ctx.setSelectOptions(providerSelect, ctx.state.availableProviders, "Choose a provider", initialData.provider || "");
      const modelInput = ctx.createTextInput("model-input", "Choose or enter model");
      modelInput.value = initialData.model || "";
      const dataListId = `rerank-models-list-${Math.random().toString(36).substr(2, 9)}`;
      modelInput.setAttribute("list", dataListId);
      const dataList = document.createElement("datalist");
      dataList.id = dataListId;
      row.appendChild(dataList);
      const targetPathInput = ctx.createTextInput("target-path-input", "/score");
      targetPathInput.value = initialData.target_path || "/score";
      const requestFormatSelect = ctx.createSelect("request-format-select");
      ctx.setSelectOptions(requestFormatSelect, ["query_passages", "query_texts"], "Default request format", initialData.request_format || "");
      const responseFormatSelect = ctx.createSelect("response-format-select");
      ctx.setSelectOptions(responseFormatSelect, ["rankings_logit", "scores"], "Default response format", initialData.response_format || "");
      const responseOutputFormatSelect = ctx.createSelect("response-output-format-select");
      ctx.setSelectOptions(
        responseOutputFormatSelect,
        ["jina_results"],
        "Default output format",
        initialData.response_output_format || ""
      );
      const { retryDelayInput, retryCountInput } = ctx.createRetrySettingsInputs(initialData);
      const customBodyParamsInput = ctx.createTextarea("custom-body-params-input", '{"param": "value"}');
      customBodyParamsInput.value = ctx.normalizeObjectTextarea(initialData.custom_body_params);
      const customHeadersInput = ctx.createTextarea("custom-headers-input", '{"X-Header": "value"}');
      customHeadersInput.value = ctx.normalizeObjectTextarea(initialData.custom_headers);
      fieldsGrid.appendChild(ctx.createFieldGroup("Provider", providerSelect, "provider-field"));
      fieldsGrid.appendChild(ctx.createFieldGroup("Model", modelInput, "model-field"));
      fieldsGrid.appendChild(ctx.createFieldGroup("Target Path", targetPathInput));
      const modelStatus = document.createElement("div");
      modelStatus.className = "model-status";
      modelStatus.dataset.state = "idle";
      const advancedDetails = document.createElement("details");
      advancedDetails.className = "advanced-options";
      const advancedSummary = document.createElement("summary");
      ctx.bindLocalizedText(advancedSummary, "editor:actions.advanced");
      advancedDetails.appendChild(advancedSummary);
      const advancedGrid = document.createElement("div");
      advancedGrid.className = "advanced-grid";
      advancedGrid.appendChild(ctx.createFieldGroup("Request Format", requestFormatSelect));
      advancedGrid.appendChild(ctx.createFieldGroup("Response Format", responseFormatSelect));
      advancedGrid.appendChild(ctx.createFieldGroup("Response Output Format", responseOutputFormatSelect));
      advancedGrid.appendChild(ctx.createFieldGroup("Retry Delay", retryDelayInput));
      advancedGrid.appendChild(ctx.createFieldGroup("Retry Count", retryCountInput));
      advancedGrid.appendChild(ctx.createFieldGroup("Custom Body Params", customBodyParamsInput, "textarea-group"));
      advancedGrid.appendChild(ctx.createFieldGroup("Custom Headers", customHeadersInput, "textarea-group"));
      advancedDetails.appendChild(advancedGrid);
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, "Remove Fallback Route");
      removeButton.addEventListener("click", () => {
        row.remove();
      });
      const rowActions = document.createElement("div");
      rowActions.className = "fallback-row-actions";
      const { moveUpButton, moveDownButton } = ctx.createMoveButtons(row);
      rowActions.appendChild(moveUpButton);
      rowActions.appendChild(moveDownButton);
      rowActions.appendChild(removeButton);
      row.appendChild(fieldsGrid);
      row.appendChild(modelStatus);
      row.appendChild(advancedDetails);
      row.appendChild(rowActions);
      ctx.createLazyProviderCatalogRowController({
        row,
        providerSelect,
        modelControl: modelInput,
        dataList,
        modelStatus
      });
      return row;
    }
    function getRerankPayloadForSave(basePayload = null) {
      const rerank = Array.from(ctx.elements.rerankList.querySelectorAll(".rule-card")).map(normalizeRerankCardForSave);
      return buildOperationRoutesPayload({ rerank }, basePayload);
    }
    function getNormalizedRerankContent() {
      return ctx.stableSerialize(getRerankPayloadForSave());
    }
    function normalizeRerankRouteForSave(routeRow) {
      const providerSelect = routeRow.querySelector(".provider-select");
      const modelInput = routeRow.querySelector(".model-input");
      const customBodyParamsInput = routeRow.querySelector(".custom-body-params-input");
      const customHeadersInput = routeRow.querySelector(".custom-headers-input");
      const targetPathInput = routeRow.querySelector(".target-path-input");
      const requestFormatSelect = routeRow.querySelector(".request-format-select");
      const responseFormatSelect = routeRow.querySelector(".response-format-select");
      const responseOutputFormatSelect = routeRow.querySelector(".response-output-format-select");
      const retryDelayInput = routeRow.querySelector(".retry-delay-input");
      const retryCountInput = routeRow.querySelector(".retry-count-input");
      const provider = providerSelect.value.trim();
      const model = modelInput.value.trim();
      const target_path = targetPathInput.value.trim();
      const request_format = requestFormatSelect.value.trim();
      const response_format = responseFormatSelect.value.trim();
      const response_output_format = responseOutputFormatSelect.value.trim();
      if (!provider) {
        throw new Error("Each rerank route must have a provider selected.");
      }
      if (!model) {
        throw new Error(`Enter or choose a model for provider '${provider}' before saving.`);
      }
      if (!target_path) {
        throw new Error("Target path is required.");
      }
      if (!target_path.startsWith("/") && !/^https?:\/\//i.test(target_path)) {
        throw new Error("Target path must start with / or with http:// or https://");
      }
      const routePayload = {
        provider,
        model,
        target_path,
        custom_body_params: ctx.parseObjectTextarea(customBodyParamsInput.value, "Custom body params"),
        custom_headers: ctx.parseObjectTextarea(customHeadersInput.value, "Custom headers")
      };
      if (request_format) {
        routePayload.request_format = request_format;
      }
      if (response_format) {
        routePayload.response_format = response_format;
      }
      if (response_output_format) {
        routePayload.response_output_format = response_output_format;
      }
      ctx.applyRetrySettingsToPayload(routePayload, retryDelayInput, retryCountInput);
      return routePayload;
    }
    function normalizeRerankCardForSave(ruleCard) {
      const gatewayModelInput = ruleCard.querySelector(".gateway-model-input");
      const routeRows = Array.from(ruleCard.querySelectorAll(".fallback-list > .fallback-row"));
      const gatewayModelName = gatewayModelInput.value.trim();
      if (!gatewayModelName) {
        throw new Error("Each rerank model rule must have a gateway model name.");
      }
      if (routeRows.length === 0) {
        throw new Error(`Rerank model '${gatewayModelName}' must contain at least one route.`);
      }
      return {
        gateway_model_name: gatewayModelName,
        routes: routeRows.map(normalizeRerankRouteForSave)
      };
    }
    async function saveRerank() {
      ctx.elements.saveButton.disabled = true;
      ctx.showLocalizedMessage("info", "Saving Rerank Routes...");
      let payload;
      try {
        payload = getRerankPayloadForSave(ctx.getOperationBasePayload());
      } catch (error) {
        ctx.showClientValidationError(error);
        return;
      }
      try {
        const result = await saveOperationPayload(
          payload,
          "Error saving Rerank Routes:",
          () => {
            ctx.state.originalRerankContent = getNormalizedRerankContent();
          }
        );
        if (!result) {
          return;
        }
        ctx.showLocalizedMessage(
          "success",
          ctx.safeSuccessMessage(result.body, "Rerank Routes updated successfully.")
        );
      } catch (error) {
        console.error("Error saving Rerank:", error);
        ctx.showLocalizedError("Error saving Rerank Routes:", error);
      } finally {
        ctx.updateSaveButtonDisabledState();
      }
    }
    async function saveEmbeddings() {
      ctx.elements.saveButton.disabled = true;
      ctx.showLocalizedMessage("info", "Saving Embeddings Routes...");
      let payload;
      try {
        payload = getEmbeddingsPayloadForSave(ctx.getOperationBasePayload());
      } catch (error) {
        ctx.showClientValidationError(error);
        return;
      }
      try {
        const result = await saveOperationPayload(
          payload,
          "Error saving Embeddings Routes:",
          () => {
            ctx.state.originalEmbeddingsContent = getNormalizedEmbeddingsContent();
          }
        );
        if (!result) {
          return;
        }
        ctx.showLocalizedMessage(
          "success",
          ctx.safeSuccessMessage(result.body, "Embeddings Routes updated successfully.")
        );
      } catch (error) {
        console.error("Error saving Embeddings:", error);
        ctx.showLocalizedError("Error saving Embeddings Routes:", error);
      } finally {
        ctx.updateSaveButtonDisabledState();
      }
    }
    function renderImageSection(listElement, refreshEmptyState, items, buildCard) {
      listElement.textContent = "";
      if (!Array.isArray(items) || items.length === 0) {
        refreshEmptyState();
        return;
      }
      items.forEach((item) => {
        const itemCard = buildCard(item);
        listElement.appendChild(itemCard);
      });
      refreshEmptyState();
    }
    function buildImageCard(initialData, options) {
      const card = document.createElement("section");
      card.className = "rule-card";
      const cardHeader = document.createElement("div");
      cardHeader.className = "rule-card-header";
      const titleWrap = document.createElement("div");
      titleWrap.className = "rule-card-title";
      const gatewayModelInput = ctx.createTextInput("gateway-model-input", options.gatewayPlaceholder);
      gatewayModelInput.value = initialData.gateway_model_name || "";
      titleWrap.appendChild(ctx.createFieldGroup("Gateway Model Name", gatewayModelInput, "gateway-model-field"));
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, "Remove Model");
      removeButton.addEventListener("click", () => {
        card.remove();
        options.refreshEmptyState();
      });
      const routeList = document.createElement("div");
      routeList.className = "fallback-list";
      const addRouteButton = document.createElement("button");
      addRouteButton.type = "button";
      addRouteButton.className = "secondary-button add-fallback-button";
      ctx.bindKnownActionText(addRouteButton, "Add Route");
      addRouteButton.addEventListener("click", () => {
        routeList.appendChild(buildImageRouteRow({}, options.defaultTargetPath));
      });
      const cardBody = document.createElement("div");
      cardBody.className = "rule-card-body";
      cardBody.appendChild(ctx.createOperationCostCalculatorField(initialData));
      cardBody.appendChild(routeList);
      cardBody.appendChild(addRouteButton);
      const accordionToggle = document.createElement("button");
      accordionToggle.type = "button";
      accordionToggle.className = "accordion-toggle";
      const svgNS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("width", "20");
      svg.setAttribute("height", "20");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("fill", "none");
      svg.setAttribute("stroke", "currentColor");
      svg.setAttribute("stroke-width", "2");
      svg.setAttribute("stroke-linecap", "round");
      svg.setAttribute("stroke-linejoin", "round");
      const polyline = document.createElementNS(svgNS, "polyline");
      polyline.setAttribute("points", "6 9 12 15 18 9");
      svg.appendChild(polyline);
      accordionToggle.appendChild(svg);
      accordionToggle.addEventListener("click", () => {
        ctx.toggleProviderCatalogCard(card);
      });
      const headerLeft = document.createElement("div");
      headerLeft.className = "rule-card-header-left";
      headerLeft.appendChild(accordionToggle);
      headerLeft.appendChild(titleWrap);
      cardHeader.appendChild(headerLeft);
      cardHeader.appendChild(removeButton);
      card.classList.add("collapsed");
      card.appendChild(cardHeader);
      card.appendChild(cardBody);
      const routes = Array.isArray(initialData.routes) ? initialData.routes : [];
      routes.forEach((route) => {
        const routeRow = buildImageRouteRow(route, options.defaultTargetPath);
        routeList.appendChild(routeRow);
      });
      if (routes.length === 0) {
        const routeRow = buildImageRouteRow({}, options.defaultTargetPath);
        routeList.appendChild(routeRow);
      }
      return card;
    }
    function buildImageRouteRow(initialData, defaultTargetPath) {
      const row = document.createElement("div");
      row.className = "fallback-row";
      ctx.setupRowReordering(row);
      const fieldsGrid = document.createElement("div");
      fieldsGrid.className = "fallback-row-grid";
      const providerSelect = ctx.createSelect("provider-select");
      ctx.setSelectOptions(providerSelect, ctx.state.availableProviders, "Choose a provider", initialData.provider || "");
      const modelInput = ctx.createTextInput("model-input", "Choose or enter model");
      modelInput.value = initialData.model || "";
      const dataListId = `image-models-list-${Math.random().toString(36).substr(2, 9)}`;
      modelInput.setAttribute("list", dataListId);
      const dataList = document.createElement("datalist");
      dataList.id = dataListId;
      row.appendChild(dataList);
      const targetPathInput = ctx.createTextInput("target-path-input", defaultTargetPath);
      targetPathInput.value = initialData.target_path || defaultTargetPath;
      const requestFormatSelect = ctx.createSelect("request-format-select");
      ctx.setSelectOptions(
        requestFormatSelect,
        ctx.constants.IMAGE_REQUEST_FORMAT_OPTIONS,
        "Default request format",
        initialData.request_format || ""
      );
      const responseFormatSelect = ctx.createSelect("response-format-select");
      ctx.setSelectOptions(
        responseFormatSelect,
        ctx.constants.IMAGE_RESPONSE_FORMAT_OPTIONS,
        "Default response format",
        initialData.response_format || ""
      );
      const { retryDelayInput, retryCountInput } = ctx.createRetrySettingsInputs(initialData);
      const customBodyParamsInput = ctx.createTextarea("custom-body-params-input", '{"param": "value"}');
      customBodyParamsInput.value = ctx.normalizeObjectTextarea(initialData.custom_body_params);
      const customHeadersInput = ctx.createTextarea("custom-headers-input", '{"X-Header": "value"}');
      customHeadersInput.value = ctx.normalizeObjectTextarea(initialData.custom_headers);
      const requestMappingInput = ctx.createTextarea("request-mapping-input", '{"fields": {"prompt": "prompt"}}');
      requestMappingInput.value = ctx.normalizeObjectTextarea(initialData.request_mapping);
      const responseMappingInput = ctx.createTextarea("response-mapping-input", '{"artifacts_path": "artifacts"}');
      responseMappingInput.value = ctx.normalizeObjectTextarea(initialData.response_mapping);
      fieldsGrid.appendChild(ctx.createFieldGroup("Provider", providerSelect, "provider-field"));
      fieldsGrid.appendChild(ctx.createFieldGroup("Model", modelInput, "model-field"));
      fieldsGrid.appendChild(ctx.createFieldGroup("Target Path", targetPathInput));
      const modelStatus = document.createElement("div");
      modelStatus.className = "model-status";
      modelStatus.dataset.state = "idle";
      const advancedDetails = document.createElement("details");
      advancedDetails.className = "advanced-options";
      const advancedSummary = document.createElement("summary");
      ctx.bindLocalizedText(advancedSummary, "editor:actions.advanced");
      advancedDetails.appendChild(advancedSummary);
      const advancedGrid = document.createElement("div");
      advancedGrid.className = "advanced-grid";
      advancedGrid.appendChild(ctx.createFieldGroup("Request Format", requestFormatSelect));
      advancedGrid.appendChild(ctx.createFieldGroup("Response Format", responseFormatSelect));
      advancedGrid.appendChild(ctx.createFieldGroup("Retry Delay", retryDelayInput));
      advancedGrid.appendChild(ctx.createFieldGroup("Retry Count", retryCountInput));
      advancedGrid.appendChild(ctx.createFieldGroup("Request Mapping", requestMappingInput, "textarea-group"));
      advancedGrid.appendChild(ctx.createFieldGroup("Response Mapping", responseMappingInput, "textarea-group"));
      advancedGrid.appendChild(ctx.createFieldGroup("Custom Body Params", customBodyParamsInput, "textarea-group"));
      advancedGrid.appendChild(ctx.createFieldGroup("Custom Headers", customHeadersInput, "textarea-group"));
      advancedDetails.appendChild(advancedGrid);
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, "Remove Route");
      removeButton.addEventListener("click", () => {
        row.remove();
      });
      const rowActions = document.createElement("div");
      rowActions.className = "fallback-row-actions";
      const { moveUpButton, moveDownButton } = ctx.createMoveButtons(row);
      rowActions.appendChild(moveUpButton);
      rowActions.appendChild(moveDownButton);
      rowActions.appendChild(removeButton);
      row.appendChild(fieldsGrid);
      row.appendChild(modelStatus);
      row.appendChild(advancedDetails);
      row.appendChild(rowActions);
      ctx.createLazyProviderCatalogRowController({
        row,
        providerSelect,
        modelControl: modelInput,
        dataList,
        modelStatus
      });
      return row;
    }
    function normalizeImageRouteForSave(routeRow, defaultTargetPath, routeLabel) {
      const providerSelect = routeRow.querySelector(".provider-select");
      const modelInput = routeRow.querySelector(".model-input");
      const customBodyParamsInput = routeRow.querySelector(".custom-body-params-input");
      const customHeadersInput = routeRow.querySelector(".custom-headers-input");
      const requestMappingInput = routeRow.querySelector(".request-mapping-input");
      const responseMappingInput = routeRow.querySelector(".response-mapping-input");
      const targetPathInput = routeRow.querySelector(".target-path-input");
      const requestFormatSelect = routeRow.querySelector(".request-format-select");
      const responseFormatSelect = routeRow.querySelector(".response-format-select");
      const retryDelayInput = routeRow.querySelector(".retry-delay-input");
      const retryCountInput = routeRow.querySelector(".retry-count-input");
      const provider = providerSelect.value.trim();
      const model = modelInput.value.trim();
      const target_path = targetPathInput.value.trim() || defaultTargetPath;
      const request_format = requestFormatSelect.value.trim();
      const response_format = responseFormatSelect.value.trim();
      if (!provider) {
        throw new Error(`Each ${routeLabel} route must have a provider selected.`);
      }
      if (!model) {
        throw new Error(`Enter or choose a model for provider '${provider}' before saving.`);
      }
      if (!target_path.startsWith("/") && !/^https?:\/\//i.test(target_path)) {
        throw new Error("Target path must start with / or with http:// or https://");
      }
      const routePayload = {
        provider,
        model,
        target_path,
        custom_body_params: ctx.parseObjectTextarea(customBodyParamsInput.value, "Custom body params"),
        custom_headers: ctx.parseObjectTextarea(customHeadersInput.value, "Custom headers")
      };
      const request_mapping = ctx.parseObjectTextarea(requestMappingInput.value, "Request mapping");
      const response_mapping = ctx.parseObjectTextarea(responseMappingInput.value, "Response mapping");
      if (request_format) {
        routePayload.request_format = request_format;
      }
      if (response_format) {
        routePayload.response_format = response_format;
      }
      if (Object.keys(request_mapping).length > 0) {
        routePayload.request_mapping = request_mapping;
      }
      if (Object.keys(response_mapping).length > 0) {
        routePayload.response_mapping = response_mapping;
      }
      ctx.applyRetrySettingsToPayload(routePayload, retryDelayInput, retryCountInput);
      return routePayload;
    }
    function normalizeImageCardForSave(ruleCard, routeLabel, defaultTargetPath) {
      const gatewayModelInput = ruleCard.querySelector(".gateway-model-input");
      const routeRows = Array.from(ruleCard.querySelectorAll(".fallback-list > .fallback-row"));
      const gatewayModelName = gatewayModelInput.value.trim();
      if (!gatewayModelName) {
        throw new Error(`Each ${routeLabel} model rule must have a gateway model name.`);
      }
      if (routeRows.length === 0) {
        throw new Error(`${routeLabel} model '${gatewayModelName}' must contain at least one route.`);
      }
      return ctx.applyOperationCostCalculator({
        gateway_model_name: gatewayModelName,
        routes: routeRows.map((routeRow) => normalizeImageRouteForSave(routeRow, defaultTargetPath, routeLabel))
      }, ruleCard);
    }
    function getImagesPayloadForSave(basePayload = null) {
      const images_generations = Array.from(ctx.elements.imageGenerationList.querySelectorAll(".rule-card")).map((ruleCard) => normalizeImageCardForSave(ruleCard, "image generation", "/images/generations"));
      const images_edits = Array.from(ctx.elements.imageEditList.querySelectorAll(".rule-card")).map((ruleCard) => normalizeImageCardForSave(ruleCard, "image edit", "/images/edits"));
      return buildOperationRoutesPayload({
        images_generations,
        images_edits
      }, basePayload);
    }
    function getNormalizedImagesContent() {
      return ctx.stableSerialize(getImagesPayloadForSave());
    }
    async function loadImagesEditor() {
      try {
        const loaded = await loadOperationRulesPayload(
          "Images Routes",
          async (payload) => {
            await renderImageSection(
              ctx.elements.imageGenerationList,
              ctx.refreshImageGenerationEmptyState,
              payload.images_generations,
              (item) => buildImageCard(item, {
                gatewayPlaceholder: "llmgateway/image-generation-model",
                defaultTargetPath: "/images/generations",
                refreshEmptyState: ctx.refreshImageGenerationEmptyState
              })
            );
            await renderImageSection(
              ctx.elements.imageEditList,
              ctx.refreshImageEditEmptyState,
              payload.images_edits,
              (item) => buildImageCard(item, {
                gatewayPlaceholder: "llmgateway/image-edit-model",
                defaultTargetPath: "/images/edits",
                refreshEmptyState: ctx.refreshImageEditEmptyState
              })
            );
          }
        );
        if (!loaded) {
          ctx.showLocalizedMessage("warning", "A newer local edit was preserved. Reload again to discard it.");
          return false;
        }
        ctx.state.originalImagesContent = getNormalizedImagesContent();
        ctx.updateSaveButtonDisabledState();
        ctx.showLocalizedMessage("success", "Images Routes loaded successfully.");
        return true;
      } catch (error) {
        console.error("Error fetching Images Routes:", error);
        ctx.showLocalizedError("Error loading Images Routes:", error);
        ctx.state.originalImagesContent = null;
        ctx.updateSaveButtonDisabledState();
        return false;
      }
    }
    async function saveImages() {
      ctx.elements.saveButton.disabled = true;
      ctx.showLocalizedMessage("info", "Saving Images Routes...");
      let payload;
      try {
        payload = getImagesPayloadForSave(ctx.getOperationBasePayload());
      } catch (error) {
        ctx.showClientValidationError(error);
        return;
      }
      try {
        const result = await saveOperationPayload(
          payload,
          "Error saving Images Routes:",
          () => {
            ctx.state.originalImagesContent = getNormalizedImagesContent();
          }
        );
        if (!result) {
          return;
        }
        ctx.showLocalizedMessage(
          "success",
          ctx.safeSuccessMessage(result.body, "Images Routes updated successfully.")
        );
      } catch (error) {
        console.error("Error saving Images:", error);
        ctx.showLocalizedError("Error saving Images Routes:", error);
      } finally {
        ctx.updateSaveButtonDisabledState();
      }
    }
    function getAudioPayloadForSave(basePayload = null) {
      const audio_speech = Array.from(ctx.elements.audioSpeechList.querySelectorAll(".rule-card")).map(
        normalizeAudioSpeechCardForSave
      );
      const audio_transcriptions = Array.from(ctx.elements.audioTranscriptionsList.querySelectorAll(".rule-card")).map(
        normalizeAudioTranscriptionCardForSave
      );
      return buildOperationRoutesPayload({ audio_speech, audio_transcriptions }, basePayload);
    }
    function getNormalizedAudioContent() {
      return ctx.stableSerialize(getAudioPayloadForSave());
    }
    function validateAudioTargetPath(targetPath, fieldLabel) {
      if (!targetPath.startsWith("/") && !/^https?:\/\//i.test(targetPath)) {
        throw new Error(`${fieldLabel} must start with / or with http:// or https://`);
      }
    }
    function normalizeAudioRouteForSave(routeRow, options) {
      const providerSelect = routeRow.querySelector(".provider-select");
      const modelInput = routeRow.querySelector(".model-input");
      const customBodyParamsInput = routeRow.querySelector(".custom-body-params-input");
      const customHeadersInput = routeRow.querySelector(".custom-headers-input");
      const targetPathInput = routeRow.querySelector(".target-path-input");
      const requestFormatSelect = routeRow.querySelector(".request-format-select");
      const voicesTargetPathInput = routeRow.querySelector(".voices-target-path-input");
      const retryDelayInput = routeRow.querySelector(".retry-delay-input");
      const retryCountInput = routeRow.querySelector(".retry-count-input");
      const provider = providerSelect.value.trim();
      const model = modelInput.value.trim();
      const target_path = targetPathInput.value.trim() || options.defaultTargetPath;
      const request_format = requestFormatSelect?.value.trim() || "";
      const voices_target_path = voicesTargetPathInput?.value.trim() || "";
      if (!provider) {
        throw new Error(`Each ${options.routeLabel} route must have a provider selected.`);
      }
      if (!model) {
        throw new Error(`Enter or choose a model for provider '${provider}' before saving.`);
      }
      validateAudioTargetPath(target_path, "Target path");
      const routePayload = {
        provider,
        model,
        target_path,
        custom_body_params: ctx.parseObjectTextarea(customBodyParamsInput.value, "Custom body params"),
        custom_headers: ctx.parseObjectTextarea(customHeadersInput.value, "Custom headers")
      };
      if (request_format) {
        routePayload.request_format = request_format;
      }
      if (voices_target_path) {
        validateAudioTargetPath(voices_target_path, "Voices target path");
        routePayload.voices_target_path = voices_target_path;
      }
      ctx.applyRetrySettingsToPayload(routePayload, retryDelayInput, retryCountInput);
      return routePayload;
    }
    function normalizeAudioSpeechRouteForSave(routeRow) {
      return normalizeAudioRouteForSave(routeRow, {
        routeLabel: "audio speech",
        defaultTargetPath: "/audio/speech"
      });
    }
    function normalizeAudioTranscriptionRouteForSave(routeRow) {
      return normalizeAudioRouteForSave(routeRow, {
        routeLabel: "audio transcription",
        defaultTargetPath: "/audio/transcriptions"
      });
    }
    function normalizeAudioSpeechCardForSave(ruleCard) {
      const gatewayModelInput = ruleCard.querySelector(".gateway-model-input");
      const routeRows = Array.from(ruleCard.querySelectorAll(".fallback-list > .fallback-row"));
      const gatewayModelName = gatewayModelInput.value.trim();
      if (!gatewayModelName) {
        throw new Error("Each audio speech model rule must have a gateway model name.");
      }
      if (routeRows.length === 0) {
        throw new Error(`Audio speech model '${gatewayModelName}' must contain at least one route.`);
      }
      return ctx.applyOperationCostCalculator({
        gateway_model_name: gatewayModelName,
        routes: routeRows.map(normalizeAudioSpeechRouteForSave)
      }, ruleCard);
    }
    function normalizeAudioTranscriptionCardForSave(ruleCard) {
      const gatewayModelInput = ruleCard.querySelector(".gateway-model-input");
      const routeRows = Array.from(ruleCard.querySelectorAll(".fallback-list > .fallback-row"));
      const gatewayModelName = gatewayModelInput.value.trim();
      if (!gatewayModelName) {
        throw new Error("Each audio transcription model rule must have a gateway model name.");
      }
      if (routeRows.length === 0) {
        throw new Error(`Audio transcription model '${gatewayModelName}' must contain at least one route.`);
      }
      return ctx.applyOperationCostCalculator({
        gateway_model_name: gatewayModelName,
        routes: routeRows.map(normalizeAudioTranscriptionRouteForSave)
      }, ruleCard);
    }
    async function loadAudioEditor() {
      try {
        const loaded = await loadOperationRulesPayload(
          "Audio Routes",
          async (payload) => {
            await renderAudioSpeech(payload.audio_speech || []);
            await renderAudioTranscriptions(payload.audio_transcriptions || []);
          }
        );
        if (!loaded) {
          ctx.showLocalizedMessage("warning", "A newer local edit was preserved. Reload again to discard it.");
          return false;
        }
        ctx.state.originalAudioContent = getNormalizedAudioContent();
        ctx.updateSaveButtonDisabledState();
        ctx.showLocalizedMessage("success", "Audio Routes loaded successfully.");
        return true;
      } catch (error) {
        console.error("Error fetching Audio Routes:", error);
        ctx.showLocalizedError("Error loading Audio Routes:", error);
        ctx.state.originalAudioContent = null;
        ctx.updateSaveButtonDisabledState();
        return false;
      }
    }
    function renderAudioSpeech(items) {
      ctx.elements.audioSpeechList.textContent = "";
      if (!Array.isArray(items) || items.length === 0) {
        ctx.refreshAudioSpeechEmptyState();
        return;
      }
      items.forEach((item) => {
        const card = buildAudioSpeechCard(item);
        ctx.elements.audioSpeechList.appendChild(card);
      });
      ctx.refreshAudioSpeechEmptyState();
    }
    function renderAudioTranscriptions(items) {
      ctx.elements.audioTranscriptionsList.textContent = "";
      if (!Array.isArray(items) || items.length === 0) {
        ctx.refreshAudioTranscriptionsEmptyState();
        return;
      }
      items.forEach((item) => {
        const card = buildAudioTranscriptionCard(item);
        ctx.elements.audioTranscriptionsList.appendChild(card);
      });
      ctx.refreshAudioTranscriptionsEmptyState();
    }
    function buildAudioSpeechCard(initialData) {
      return buildAudioCard(initialData, {
        gatewayPlaceholder: "llmgateway/audio-speech-model",
        addRouteButtonText: "Add Route",
        refreshEmptyState: ctx.refreshAudioSpeechEmptyState,
        buildRouteRow: buildAudioSpeechRouteRow
      });
    }
    function buildAudioTranscriptionCard(initialData) {
      return buildAudioCard(initialData, {
        gatewayPlaceholder: "llmgateway/audio-transcription-model",
        addRouteButtonText: "Add Fallback Route",
        refreshEmptyState: ctx.refreshAudioTranscriptionsEmptyState,
        buildRouteRow: buildAudioTranscriptionRouteRow
      });
    }
    function buildAudioCard(initialData, options) {
      const card = document.createElement("section");
      card.className = "rule-card";
      const cardHeader = document.createElement("div");
      cardHeader.className = "rule-card-header";
      const titleWrap = document.createElement("div");
      titleWrap.className = "rule-card-title";
      const gatewayModelInput = ctx.createTextInput("gateway-model-input", options.gatewayPlaceholder);
      gatewayModelInput.value = initialData.gateway_model_name || "";
      titleWrap.appendChild(ctx.createFieldGroup("Gateway Model Name", gatewayModelInput, "gateway-model-field"));
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, "Remove Model");
      removeButton.addEventListener("click", () => {
        card.remove();
        options.refreshEmptyState();
      });
      const routeList = document.createElement("div");
      routeList.className = "fallback-list";
      const addRouteButton = document.createElement("button");
      addRouteButton.type = "button";
      addRouteButton.className = "secondary-button add-fallback-button";
      ctx.bindKnownActionText(addRouteButton, options.addRouteButtonText);
      addRouteButton.addEventListener("click", () => {
        routeList.appendChild(options.buildRouteRow({}));
      });
      const cardBody = document.createElement("div");
      cardBody.className = "rule-card-body";
      cardBody.appendChild(ctx.createOperationCostCalculatorField(initialData));
      cardBody.appendChild(routeList);
      cardBody.appendChild(addRouteButton);
      const accordionToggle = document.createElement("button");
      accordionToggle.type = "button";
      accordionToggle.className = "accordion-toggle";
      const svgNS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("width", "20");
      svg.setAttribute("height", "20");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("fill", "none");
      svg.setAttribute("stroke", "currentColor");
      svg.setAttribute("stroke-width", "2");
      svg.setAttribute("stroke-linecap", "round");
      svg.setAttribute("stroke-linejoin", "round");
      const polyline = document.createElementNS(svgNS, "polyline");
      polyline.setAttribute("points", "6 9 12 15 18 9");
      svg.appendChild(polyline);
      accordionToggle.appendChild(svg);
      accordionToggle.addEventListener("click", () => {
        ctx.toggleProviderCatalogCard(card);
      });
      const headerLeft = document.createElement("div");
      headerLeft.className = "rule-card-header-left";
      headerLeft.appendChild(accordionToggle);
      headerLeft.appendChild(titleWrap);
      cardHeader.appendChild(headerLeft);
      cardHeader.appendChild(removeButton);
      card.classList.add("collapsed");
      card.appendChild(cardHeader);
      card.appendChild(cardBody);
      const routes = Array.isArray(initialData.routes) ? initialData.routes : [];
      routes.forEach((route) => {
        const routeRow = options.buildRouteRow(route);
        routeList.appendChild(routeRow);
      });
      if (routes.length === 0) {
        const routeRow = options.buildRouteRow({});
        routeList.appendChild(routeRow);
      }
      return card;
    }
    function buildAudioSpeechRouteRow(initialData) {
      return buildAudioRouteRow(initialData, {
        defaultTargetPath: "/audio/speech",
        includeRequestFormat: false,
        includeVoicesTargetPath: true,
        dataListPrefix: "audio-speech-models-list",
        removeButtonText: "Remove Route",
        customBodyPlaceholder: '{"voice": "alloy"}'
      });
    }
    function buildAudioTranscriptionRouteRow(initialData) {
      return buildAudioRouteRow(initialData, {
        defaultTargetPath: "/audio/transcriptions",
        includeRequestFormat: true,
        includeVoicesTargetPath: false,
        dataListPrefix: "audio-transcription-models-list",
        removeButtonText: "Remove Fallback Route",
        customBodyPlaceholder: '{"language": "en"}'
      });
    }
    function buildAudioRouteRow(initialData, options) {
      const row = document.createElement("div");
      row.className = "fallback-row";
      ctx.setupRowReordering(row);
      const fieldsGrid = document.createElement("div");
      fieldsGrid.className = "fallback-row-grid";
      const providerSelect = ctx.createSelect("provider-select");
      ctx.setSelectOptions(providerSelect, ctx.state.availableProviders, "Choose a provider", initialData.provider || "");
      const modelInput = ctx.createTextInput("model-input", "Choose or enter model");
      modelInput.value = initialData.model || "";
      const dataListId = `${options.dataListPrefix}-${Math.random().toString(36).substr(2, 9)}`;
      modelInput.setAttribute("list", dataListId);
      const dataList = document.createElement("datalist");
      dataList.id = dataListId;
      row.appendChild(dataList);
      const targetPathInput = ctx.createTextInput("target-path-input", options.defaultTargetPath);
      targetPathInput.value = initialData.target_path || options.defaultTargetPath;
      const { retryDelayInput, retryCountInput } = ctx.createRetrySettingsInputs(initialData);
      const customBodyParamsInput = ctx.createTextarea("custom-body-params-input", options.customBodyPlaceholder);
      customBodyParamsInput.value = ctx.normalizeObjectTextarea(initialData.custom_body_params);
      const customHeadersInput = ctx.createTextarea("custom-headers-input", '{"X-Header": "value"}');
      customHeadersInput.value = ctx.normalizeObjectTextarea(initialData.custom_headers);
      fieldsGrid.appendChild(ctx.createFieldGroup("Provider", providerSelect, "provider-field"));
      fieldsGrid.appendChild(ctx.createFieldGroup("Model", modelInput, "model-field"));
      fieldsGrid.appendChild(ctx.createFieldGroup("Target Path", targetPathInput));
      const modelStatus = document.createElement("div");
      modelStatus.className = "model-status";
      modelStatus.dataset.state = "idle";
      const advancedDetails = document.createElement("details");
      advancedDetails.className = "advanced-options";
      const advancedSummary = document.createElement("summary");
      ctx.bindLocalizedText(advancedSummary, "editor:actions.advanced");
      advancedDetails.appendChild(advancedSummary);
      const advancedGrid = document.createElement("div");
      advancedGrid.className = "advanced-grid";
      if (options.includeRequestFormat) {
        const requestFormatSelect = ctx.createSelect("request-format-select");
        ctx.setSelectOptions(
          requestFormatSelect,
          ctx.constants.AUDIO_REQUEST_FORMAT_OPTIONS,
          "Default request format",
          initialData.request_format || ""
        );
        advancedGrid.appendChild(ctx.createFieldGroup("Request Format", requestFormatSelect));
      }
      if (options.includeVoicesTargetPath) {
        const voicesTargetPathInput = ctx.createTextInput("voices-target-path-input", "/voices");
        voicesTargetPathInput.value = initialData.voices_target_path || "";
        advancedGrid.appendChild(ctx.createFieldGroup("Voices Target Path", voicesTargetPathInput));
      }
      advancedGrid.appendChild(ctx.createFieldGroup("Retry Delay", retryDelayInput));
      advancedGrid.appendChild(ctx.createFieldGroup("Retry Count", retryCountInput));
      advancedGrid.appendChild(ctx.createFieldGroup("Custom Body Params", customBodyParamsInput, "textarea-group"));
      advancedGrid.appendChild(ctx.createFieldGroup("Custom Headers", customHeadersInput, "textarea-group"));
      advancedDetails.appendChild(advancedGrid);
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, options.removeButtonText);
      removeButton.addEventListener("click", () => {
        row.remove();
      });
      const rowActions = document.createElement("div");
      rowActions.className = "fallback-row-actions";
      const { moveUpButton, moveDownButton } = ctx.createMoveButtons(row);
      rowActions.appendChild(moveUpButton);
      rowActions.appendChild(moveDownButton);
      rowActions.appendChild(removeButton);
      row.appendChild(fieldsGrid);
      row.appendChild(modelStatus);
      row.appendChild(advancedDetails);
      row.appendChild(rowActions);
      ctx.createLazyProviderCatalogRowController({
        row,
        providerSelect,
        modelControl: modelInput,
        dataList,
        modelStatus
      });
      return row;
    }
    async function saveAudio() {
      ctx.elements.saveButton.disabled = true;
      ctx.showLocalizedMessage("info", "Saving Audio Routes...");
      let payload;
      try {
        payload = getAudioPayloadForSave(ctx.getOperationBasePayload());
      } catch (error) {
        ctx.showClientValidationError(error);
        return;
      }
      try {
        const result = await saveOperationPayload(
          payload,
          "Error saving Audio Routes:",
          () => {
            ctx.state.originalAudioContent = getNormalizedAudioContent();
          }
        );
        if (!result) {
          return;
        }
        ctx.showLocalizedMessage(
          "success",
          ctx.safeSuccessMessage(result.body, "Audio Routes updated successfully.")
        );
      } catch (error) {
        console.error("Error saving Audio Routes:", error);
        ctx.showLocalizedError("Error saving Audio Routes:", error);
      } finally {
        ctx.updateSaveButtonDisabledState();
      }
    }
    function createAccordionToggle(card) {
      const accordionToggle = document.createElement("button");
      accordionToggle.type = "button";
      accordionToggle.className = "accordion-toggle";
      const svgNS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("width", "20");
      svg.setAttribute("height", "20");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("fill", "none");
      svg.setAttribute("stroke", "currentColor");
      svg.setAttribute("stroke-width", "2");
      svg.setAttribute("stroke-linecap", "round");
      svg.setAttribute("stroke-linejoin", "round");
      const polyline = document.createElementNS(svgNS, "polyline");
      polyline.setAttribute("points", "6 9 12 15 18 9");
      svg.appendChild(polyline);
      accordionToggle.appendChild(svg);
      accordionToggle.addEventListener("click", () => {
        card.classList.toggle("collapsed");
      });
      return accordionToggle;
    }
    function createWebCardShell(initialData, gatewayPlaceholder, removeLabel, refreshEmptyState) {
      const card = document.createElement("section");
      card.className = "rule-card collapsed";
      const cardHeader = document.createElement("div");
      cardHeader.className = "rule-card-header";
      const titleWrap = document.createElement("div");
      titleWrap.className = "rule-card-title";
      const gatewayModelInput = ctx.createTextInput("gateway-model-input", gatewayPlaceholder);
      gatewayModelInput.value = initialData.gateway_model_name || "";
      titleWrap.appendChild(ctx.createFieldGroup("Gateway Model Name", gatewayModelInput, "gateway-model-field"));
      const headerLeft = document.createElement("div");
      headerLeft.className = "rule-card-header-left";
      headerLeft.appendChild(createAccordionToggle(card));
      headerLeft.appendChild(titleWrap);
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, removeLabel);
      removeButton.addEventListener("click", () => {
        card.remove();
        refreshEmptyState();
        refreshWebCrossDropdowns();
      });
      cardHeader.appendChild(headerLeft);
      cardHeader.appendChild(removeButton);
      const cardBody = document.createElement("div");
      cardBody.className = "rule-card-body";
      cardBody.appendChild(ctx.createOperationCostCalculatorField(initialData));
      card.appendChild(cardHeader);
      card.appendChild(cardBody);
      return { card, cardBody, gatewayModelInput };
    }
    function appendFieldHint(fieldGroup, hintKey) {
      if (!hintKey) return;
      const hint = document.createElement("small");
      hint.className = "field-hint";
      ctx.bindLocalizedText(hint, hintKey);
      fieldGroup.appendChild(hint);
    }
    function attachFieldTooltip(fieldGroup, tooltipKey) {
      if (!tooltipKey) return;
      const label = fieldGroup.querySelector(".field-label");
      if (!label) return;
      const wrapper = document.createElement("span");
      wrapper.className = "field-tooltip";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "field-tooltip-button";
      button.textContent = gatewayI18n.t("editor:hints.infoIcon");
      ctx.bindLocalizedAttribute(
        button,
        "aria-label",
        "editor:hints.fieldInfo",
        () => ({
          field: label.querySelector(".field-label-text")?.textContent || ctx.t("editor:fields.field")
        })
      );
      ctx.bindLocalizedAttribute(button, "title", tooltipKey);
      const popover = document.createElement("span");
      popover.className = "field-tooltip-popover";
      popover.setAttribute("role", "tooltip");
      ctx.bindLocalizedText(popover, tooltipKey);
      wrapper.appendChild(button);
      wrapper.appendChild(popover);
      label.appendChild(wrapper);
    }
    function buildWebSearchCard(initialData, options) {
      const { card, cardBody, gatewayModelInput } = createWebCardShell(
        initialData,
        options.gatewayPlaceholder,
        "Remove Service",
        options.refreshEmptyState
      );
      gatewayModelInput.addEventListener("input", refreshWebCrossDropdowns);
      const serviceGrid = document.createElement("div");
      serviceGrid.className = "fallback-row-grid";
      const queryModelSelect = ctx.createSelect("query-model-input");
      ctx.setModelSelectOptions(queryModelSelect, ctx.state.gatewayModelCatalog.chat, initialData.query_model || "");
      const queryField = ctx.createFieldGroup("Query Model (optional)", queryModelSelect, "model-field");
      appendFieldHint(queryField, "editor:hints.webQueryModel");
      serviceGrid.appendChild(queryField);
      cardBody.appendChild(serviceGrid);
      return card;
    }
    function buildWebReadCard(initialData, options) {
      const { card, gatewayModelInput } = createWebCardShell(
        initialData,
        options.gatewayPlaceholder,
        "Remove Service",
        options.refreshEmptyState
      );
      gatewayModelInput.addEventListener("input", refreshWebCrossDropdowns);
      return card;
    }
    function normalizeWebSearchCardForSave(ruleCard) {
      const gatewayModelName = ruleCard.querySelector(".gateway-model-input").value.trim();
      const queryModel = ruleCard.querySelector(".query-model-input")?.value.trim();
      if (!gatewayModelName) {
        throw new Error("Each web search service must have a gateway model name.");
      }
      const payload = { gateway_model_name: gatewayModelName };
      if (queryModel) {
        payload.query_model = queryModel;
      }
      return ctx.applyOperationCostCalculator(payload, ruleCard);
    }
    function normalizeWebReadCardForSave(ruleCard) {
      const gatewayModelName = ruleCard.querySelector(".gateway-model-input").value.trim();
      if (!gatewayModelName) {
        throw new Error("Each web read service must have a gateway model name.");
      }
      return ctx.applyOperationCostCalculator({ gateway_model_name: gatewayModelName }, ruleCard);
    }
    function buildWebReferenceCard(initialData, options) {
      const { card, cardBody } = createWebCardShell(
        initialData,
        options.gatewayPlaceholder,
        "Remove Service",
        options.refreshEmptyState
      );
      const fieldsGrid = document.createElement("div");
      fieldsGrid.className = "fallback-row-grid";
      options.fields.forEach((field) => {
        let control;
        if (field.catalog) {
          control = ctx.createSelect(field.className);
          const catalogOptions = ctx.state.gatewayModelCatalog[field.catalog] || [];
          ctx.setModelSelectOptions(control, catalogOptions, initialData[field.key] || field.defaultValue || "");
        } else {
          control = ctx.createTextInput(field.className, field.placeholder);
          control.value = initialData[field.key] || field.defaultValue || "";
        }
        const group = ctx.createFieldGroup(field.label, control, "model-field");
        appendFieldHint(group, field.hintKey);
        fieldsGrid.appendChild(group);
      });
      cardBody.appendChild(fieldsGrid);
      return card;
    }
    function normalizeWebReferenceCardForSave(ruleCard, options) {
      const gatewayModelName = ruleCard.querySelector(".gateway-model-input").value.trim();
      if (!gatewayModelName) {
        throw new Error(`Each ${options.serviceLabel} must have a gateway model name.`);
      }
      const payload = { gateway_model_name: gatewayModelName };
      options.fields.forEach((field) => {
        const value = ruleCard.querySelector(`.${field.className}`).value.trim();
        if (field.required && !value) {
          throw new Error(`${options.serviceLabel} '${gatewayModelName}' must define ${field.key}.`);
        }
        if (value) {
          payload[field.key] = value;
        }
      });
      return ctx.applyOperationCostCalculator(payload, ruleCard);
    }
    const WEB_SEARCH_CARD_OPTIONS = {
      gatewayPlaceholder: "llmgateway/web-search",
      refreshEmptyState: ctx.refreshWebSearchEmptyState
    };
    const WEB_READ_CARD_OPTIONS = {
      gatewayPlaceholder: "llmgateway/web-read",
      refreshEmptyState: ctx.refreshWebReadEmptyState
    };
    const WEB_RESEARCH_CARD_OPTIONS = {
      gatewayPlaceholder: "llmgateway/web-research",
      serviceLabel: "web research service",
      refreshEmptyState: ctx.refreshWebResearchEmptyState,
      fields: [
        { key: "search_model", label: "Search Model", className: "search-model-input", catalog: "web_search", required: true, hintKey: "editor:hints.webSearchModel" },
        { key: "read_model", label: "Read Model", className: "read-model-input", catalog: "web_read", required: true, hintKey: "editor:hints.webReadModel" },
        { key: "rerank_model", label: "Rerank Model", className: "rerank-model-input", catalog: "rerank", required: true, hintKey: "editor:hints.webRerankModel" },
        { key: "analysis_model", label: "Analysis Model", className: "analysis-model-input", catalog: "chat", required: true, hintKey: "editor:hints.webAnalysisModel" }
      ]
    };
    const WEB_DEEP_RESEARCH_CARD_OPTIONS = {
      gatewayPlaceholder: "llmgateway/web-deep-research",
      serviceLabel: "web deep research service",
      refreshEmptyState: ctx.refreshWebDeepResearchEmptyState,
      fields: [
        { key: "search_model", label: "Search Model", className: "search-model-input", catalog: "web_search", required: true, hintKey: "editor:hints.webSearchModel" },
        { key: "read_model", label: "Read Model", className: "read-model-input", catalog: "web_read", required: true, hintKey: "editor:hints.webReadModel" },
        { key: "fast_model", label: "Fast LLM", className: "fast-model-input", catalog: "chat", required: true, hintKey: "editor:hints.webFastModel" },
        { key: "smart_model", label: "Smart LLM", className: "smart-model-input", catalog: "chat", required: true, hintKey: "editor:hints.webSmartModel" },
        { key: "strategic_model", label: "Strategic LLM", className: "strategic-model-input", catalog: "chat", required: true, hintKey: "editor:hints.webStrategicModel" },
        { key: "embedding_model", label: "Embedding Model", className: "embedding-model-input", catalog: "embeddings", hintKey: "editor:hints.webEmbeddingModel" },
        { key: "image_generation_model", label: "Image Generation Model", className: "image-generation-model-input", catalog: "images_generations", hintKey: "editor:hints.webImageModel" },
        { key: "image_generation_size", label: "Image Generation Size", className: "image-generation-size-input", placeholder: "1024x1024", hintKey: "editor:hints.webImageSize" }
      ]
    };
    function getWebPayloadForSave(basePayload = null) {
      const web_search = Array.from(ctx.elements.webSearchList.querySelectorAll(".rule-card")).map(normalizeWebSearchCardForSave);
      const web_read = Array.from(ctx.elements.webReadList.querySelectorAll(".rule-card")).map(normalizeWebReadCardForSave);
      const web_research = Array.from(ctx.elements.webResearchList.querySelectorAll(".rule-card")).map(
        (card) => normalizeWebReferenceCardForSave(card, WEB_RESEARCH_CARD_OPTIONS)
      );
      const web_deep_research = Array.from(ctx.elements.webDeepResearchList.querySelectorAll(".rule-card")).map(
        (card) => normalizeWebReferenceCardForSave(card, WEB_DEEP_RESEARCH_CARD_OPTIONS)
      );
      return buildOperationRoutesPayload(
        { web_search, web_read, web_research, web_deep_research },
        basePayload
      );
    }
    function getNormalizedWebContent() {
      return ctx.stableSerialize(getWebPayloadForSave());
    }
    async function loadWebEditor() {
      try {
        const loaded = await loadOperationRulesPayload(
          "Web Services",
          async (payload) => {
            await loadGatewayModelCatalog();
            applyOperationCatalog(payload);
            await renderWebSections(payload);
            refreshWebCrossDropdowns();
          }
        );
        if (!loaded) {
          ctx.showLocalizedMessage("warning", "A newer local edit was preserved. Reload again to discard it.");
          return false;
        }
        ctx.state.originalWebContent = getNormalizedWebContent();
        ctx.updateSaveButtonDisabledState();
        ctx.showLocalizedMessage("success", "Web Services loaded successfully.");
        return true;
      } catch (error) {
        console.error("Error fetching Web Services:", error);
        ctx.showLocalizedError("Error loading Web Services:", error);
        ctx.state.originalWebContent = null;
        ctx.updateSaveButtonDisabledState();
        return false;
      }
    }
    function renderWebSections(payload) {
      ctx.elements.webSearchList.textContent = "";
      ctx.elements.webReadList.textContent = "";
      ctx.elements.webResearchList.textContent = "";
      ctx.elements.webDeepResearchList.textContent = "";
      (payload.web_search || []).forEach((item) => {
        const card = buildWebSearchCard(item, WEB_SEARCH_CARD_OPTIONS);
        ctx.elements.webSearchList.appendChild(card);
      });
      (payload.web_read || []).forEach((item) => {
        const card = buildWebReadCard(item, WEB_READ_CARD_OPTIONS);
        ctx.elements.webReadList.appendChild(card);
      });
      (payload.web_research || []).forEach((item) => {
        ctx.elements.webResearchList.appendChild(buildWebReferenceCard(item, WEB_RESEARCH_CARD_OPTIONS));
      });
      (payload.web_deep_research || []).forEach((item) => {
        ctx.elements.webDeepResearchList.appendChild(buildWebReferenceCard(item, WEB_DEEP_RESEARCH_CARD_OPTIONS));
      });
      ctx.refreshWebSearchEmptyState();
      ctx.refreshWebReadEmptyState();
      ctx.refreshWebResearchEmptyState();
      ctx.refreshWebDeepResearchEmptyState();
    }
    async function saveWeb() {
      ctx.elements.saveButton.disabled = true;
      ctx.showLocalizedMessage("info", "Saving Web Services...");
      let payload;
      try {
        payload = getWebPayloadForSave(ctx.getOperationBasePayload());
      } catch (error) {
        ctx.showClientValidationError(error);
        return;
      }
      try {
        const result = await saveOperationPayload(
          payload,
          "Error saving Web Services:",
          (published) => {
            applyOperationCatalog(published);
            refreshWebCrossDropdowns();
            ctx.state.originalWebContent = getNormalizedWebContent();
          }
        );
        if (!result) {
          return;
        }
        ctx.showLocalizedMessage(
          "success",
          ctx.safeSuccessMessage(result.body, "Web Services updated successfully.")
        );
      } catch (error) {
        console.error("Error saving Web Services:", error);
        ctx.showLocalizedError("Error saving Web Services:", error);
      } finally {
        ctx.updateSaveButtonDisabledState();
      }
    }
    async function loadModelRulesEditor() {
      ctx.showLocalizedMessage("info", "Loading Model Rules...");
      try {
        const loaded = await ctx.loadConfigDocument(
          "model",
          "/v1/config/model-rules",
          {
            responseType: "text",
            validate: (content) => {
              if (typeof content !== "string") {
                throw new ConfigUiError("The configuration response has an invalid shape.");
              }
              return content;
            },
            apply: (content) => {
              ctx.elements.modelRulesRawInput.value = content;
            }
          }
        );
        if (!loaded) {
          ctx.showLocalizedMessage("warning", "A newer local edit was preserved. Reload again to discard it.");
          return false;
        }
        ctx.state.originalModelRulesContent = ctx.elements.modelRulesRawInput.value;
        ctx.updateSaveButtonDisabledState();
        ctx.showLocalizedMessage("success", "Model Rules loaded successfully.");
        return true;
      } catch (error) {
        console.error("Error fetching Model Rules:", error);
        ctx.showLocalizedError("Error loading Model Rules:", error);
        ctx.state.originalModelRulesContent = null;
        ctx.updateSaveButtonDisabledState();
        return false;
      }
    }
    async function saveModelRules() {
      ctx.elements.saveButton.disabled = true;
      ctx.showLocalizedMessage("info", "Saving Model Rules...");
      try {
        const payload = ctx.elements.modelRulesRawInput.value;
        const result = await ctx.saveConfigDocument(
          "model",
          "/v1/config/model-rules",
          payload,
          {
            contentType: "text/plain",
            body: payload,
            errorTitle: "Error saving Model Rules:",
            extractPublishedPayload: (_body, submitted) => submitted,
            validatePublished: (content) => {
              if (typeof content !== "string") {
                throw new ConfigUiError("The configuration response has an invalid shape.");
              }
              return content;
            }
          }
        );
        if (!result) {
          return;
        }
        if (ctx.state.editorMutationVersion === result.submittedMutationVersion) {
          ctx.elements.modelRulesRawInput.value = result.payload;
          ctx.state.originalModelRulesContent = result.payload;
        }
        ctx.showLocalizedMessage(
          "success",
          ctx.safeSuccessMessage(result.body, "Model Rules saved successfully.")
        );
      } catch (error) {
        console.error("Error saving Model Rules:", error);
        ctx.showLocalizedError("Error saving Model Rules:", error);
      } finally {
        ctx.updateSaveButtonDisabledState();
      }
    }
    Object.assign(ctx, {
      normalizeOperationRulesPayload,
      applyOperationRulesPayload,
      buildOperationRoutesPayload,
      loadOperationRulesPayload,
      saveOperationPayload,
      collectCurrentWebSectionModels,
      refreshWebCrossDropdowns,
      loadGatewayModelCatalog,
      applyOperationCatalog,
      getEmbeddingsPayloadForSave,
      getNormalizedEmbeddingsContent,
      normalizeEmbeddingRouteForSave,
      normalizeEmbeddingCardForSave,
      normalizeEmbeddingRouteRowForSave,
      loadEmbeddingsEditor,
      renderEmbeddings,
      buildEmbeddingCard,
      buildEmbeddingRouteRow,
      loadRerankEditor,
      renderRerank,
      buildRerankCard,
      buildRerankRouteRow,
      getRerankPayloadForSave,
      getNormalizedRerankContent,
      normalizeRerankRouteForSave,
      normalizeRerankCardForSave,
      saveRerank,
      saveEmbeddings,
      renderImageSection,
      buildImageCard,
      buildImageRouteRow,
      normalizeImageRouteForSave,
      normalizeImageCardForSave,
      getImagesPayloadForSave,
      getNormalizedImagesContent,
      loadImagesEditor,
      saveImages,
      getAudioPayloadForSave,
      getNormalizedAudioContent,
      validateAudioTargetPath,
      normalizeAudioRouteForSave,
      normalizeAudioSpeechRouteForSave,
      normalizeAudioTranscriptionRouteForSave,
      normalizeAudioSpeechCardForSave,
      normalizeAudioTranscriptionCardForSave,
      loadAudioEditor,
      renderAudioSpeech,
      renderAudioTranscriptions,
      buildAudioSpeechCard,
      buildAudioTranscriptionCard,
      buildAudioCard,
      buildAudioSpeechRouteRow,
      buildAudioTranscriptionRouteRow,
      buildAudioRouteRow,
      saveAudio,
      createAccordionToggle,
      createWebCardShell,
      appendFieldHint,
      attachFieldTooltip,
      buildWebSearchCard,
      buildWebReadCard,
      normalizeWebSearchCardForSave,
      normalizeWebReadCardForSave,
      buildWebReferenceCard,
      normalizeWebReferenceCardForSave,
      getWebPayloadForSave,
      getNormalizedWebContent,
      loadWebEditor,
      renderWebSections,
      saveWeb,
      loadModelRulesEditor,
      saveModelRules,
      WEB_SEARCH_CARD_OPTIONS,
      WEB_READ_CARD_OPTIONS,
      WEB_RESEARCH_CARD_OPTIONS,
      WEB_DEEP_RESEARCH_CARD_OPTIONS
    });
  }

  // src/providers.mjs
  function registerProviders(ctx) {
    async function getProviderModels(providerName) {
      if (!providerName) {
        return [];
      }
      const cachedEntry = ctx.state.providerModelsCache.get(providerName);
      if (cachedEntry && Date.now() - cachedEntry.fetchedAt < ctx.constants.MODELS_CACHE_TTL_MS) {
        return cachedEntry.models;
      }
      const existingRequest = ctx.state.providerModelsRequests.get(providerName);
      if (existingRequest) {
        return existingRequest;
      }
      const requestEpoch = ctx.state.providerModelsCacheEpoch;
      let requestPromise;
      requestPromise = ctx.apiFetch(`/v1/config/providers/${encodeURIComponent(providerName)}/models`).then(async (response) => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(payload.detail || `HTTP ${response.status}`);
        }
        const models = Array.isArray(payload.models) ? payload.models.map((item) => item.id).filter((modelId) => typeof modelId === "string") : [];
        const sortedModels = ctx.sortProviderModelIds(models);
        if (ctx.state.providerModelsCacheEpoch === requestEpoch) {
          ctx.state.providerModelsCache.set(providerName, {
            models: sortedModels,
            fetchedAt: Date.now()
          });
        }
        return sortedModels;
      }).finally(() => {
        if (ctx.state.providerModelsRequests.get(providerName) === requestPromise) {
          ctx.state.providerModelsRequests.delete(providerName);
        }
      });
      ctx.state.providerModelsRequests.set(providerName, requestPromise);
      return requestPromise;
    }
    function catalogStatusText(status) {
      const values = {
        count: status.count || 0,
        error: status.error || "",
        model: status.model || "",
        provider: status.provider || ""
      };
      const key = status.state === "idle" && !status.provider ? "noProvider" : status.state;
      return window.gatewayI18n.t(`editor:catalog.${key}`, values);
    }
    function populateProviderModelDataList(dataList, models) {
      dataList.textContent = "";
      models.forEach((modelId) => {
        const option = document.createElement("option");
        option.value = modelId;
        dataList.appendChild(option);
      });
    }
    function createLazyProviderCatalogRowController(options) {
      const {
        row,
        providerSelect,
        modelControl,
        dataList = null,
        modelStatus,
        requireListedModel = false
      } = options;
      const statusText = document.createElement("span");
      const retryButton = document.createElement("button");
      retryButton.type = "button";
      retryButton.className = "model-catalog-retry secondary-button";
      modelStatus.textContent = "";
      modelStatus.setAttribute("aria-live", "polite");
      modelStatus.setAttribute("aria-atomic", "true");
      modelStatus.appendChild(statusText);
      modelStatus.appendChild(retryButton);
      let rowGeneration = 0;
      let availableModels = null;
      let status = {
        state: "idle",
        provider: providerSelect.value.trim(),
        model: modelControl.value.trim()
      };
      function render() {
        modelStatus.dataset.state = status.state;
        statusText.textContent = catalogStatusText(status);
        retryButton.textContent = window.gatewayI18n.t("editor:catalog.retry");
        retryButton.hidden = status.state !== "error";
      }
      function setStatus(state, values = {}) {
        status = {
          state,
          provider: providerSelect.value.trim(),
          model: modelControl.value.trim(),
          ...values
        };
        render();
      }
      function applyModels(models, selectedModel) {
        if (dataList) {
          populateProviderModelDataList(dataList, models);
          return;
        }
        modelControl.disabled = false;
        const optionsWithSavedModel = selectedModel && !models.includes(selectedModel) ? [selectedModel, ...models] : models;
        ctx.setSelectOptions(
          modelControl,
          optionsWithSavedModel,
          models.length > 0 ? "Choose a model" : "No models available",
          selectedModel
        );
      }
      async function load() {
        const provider = providerSelect.value.trim();
        const selectedModel = modelControl.value.trim();
        const loadGeneration = ++rowGeneration;
        const pageGeneration = ctx.state.providerCatalogGeneration;
        if (!provider) {
          row.dataset.modelsLoadError = "false";
          ctx.clearUnavailableFallbackModelMetadata(row);
          setStatus("idle");
          return [];
        }
        const cachedEntry = ctx.state.providerModelsCache.get(provider);
        if (cachedEntry && Date.now() - cachedEntry.fetchedAt < ctx.constants.MODELS_CACHE_TTL_MS && status.provider === provider && ["ready", "empty", "unavailable"].includes(status.state)) {
          return cachedEntry.models;
        }
        row.dataset.modelsLoadError = "false";
        ctx.clearUnavailableFallbackModelMetadata(row);
        setStatus("loading", { provider, model: selectedModel });
        try {
          const models = await getProviderModels(provider);
          if (rowGeneration !== loadGeneration || ctx.state.providerCatalogGeneration !== pageGeneration || !row.isConnected || providerSelect.value.trim() !== provider) {
            return models;
          }
          applyModels(models, selectedModel);
          availableModels = models;
          if (selectedModel && !models.includes(selectedModel)) {
            row.dataset.modelsLoadError = requireListedModel ? "true" : "false";
            row.dataset.unavailableModel = selectedModel;
            row.dataset.unavailableProvider = provider;
            setStatus("unavailable", { provider, model: selectedModel });
          } else {
            setStatus(models.length > 0 ? "ready" : "empty", {
              count: models.length,
              provider
            });
          }
          return models;
        } catch (error) {
          if (rowGeneration !== loadGeneration || ctx.state.providerCatalogGeneration !== pageGeneration || !row.isConnected || providerSelect.value.trim() !== provider) {
            return [];
          }
          row.dataset.modelsLoadError = "false";
          setStatus("error", {
            error: ctx.boundedSafeText(ctx.safeClientError(error)),
            provider,
            model: selectedModel
          });
          return [];
        }
      }
      function resetForProviderChange() {
        rowGeneration += 1;
        availableModels = null;
        row.dataset.modelsLoadError = "false";
        ctx.clearUnavailableFallbackModelMetadata(row);
        modelControl.value = "";
        if (dataList) {
          dataList.textContent = "";
        } else {
          modelControl.disabled = true;
          ctx.setSelectOptions(
            modelControl,
            [],
            providerSelect.value.trim() ? "Loading models..." : "Choose a provider first",
            ""
          );
        }
        void load();
      }
      function invalidate() {
        rowGeneration += 1;
        if (status.state === "loading") {
          setStatus("idle");
        }
      }
      providerSelect.addEventListener("change", resetForProviderChange);
      modelControl.addEventListener("focus", () => {
        void load();
      });
      modelControl.addEventListener("pointerdown", () => {
        void load();
      });
      retryButton.addEventListener("click", () => {
        void load();
      });
      const controller = Object.freeze({
        invalidate,
        isConnected: () => row.isConnected,
        load,
        markSelected(model) {
          if (availableModels && !availableModels.includes(model)) {
            row.dataset.modelsLoadError = requireListedModel ? "true" : "false";
            row.dataset.unavailableModel = model;
            row.dataset.unavailableProvider = providerSelect.value.trim();
            setStatus("unavailable", { model });
            return;
          }
          row.dataset.modelsLoadError = "false";
          ctx.clearUnavailableFallbackModelMetadata(row);
          setStatus("selected", { model });
        },
        render
      });
      ctx.state.providerCatalogControllers.add(controller);
      ctx.state.providerCatalogControllerByRow.set(row, controller);
      render();
      return controller;
    }
    function invalidateProviderCatalogRows() {
      ctx.state.providerCatalogGeneration += 1;
      ctx.state.providerCatalogControllers.forEach((controller) => {
        if (!controller.isConnected()) {
          ctx.state.providerCatalogControllers.delete(controller);
          return;
        }
        controller.invalidate();
      });
    }
    function rerenderProviderCatalogStatuses() {
      ctx.state.providerCatalogControllers.forEach((controller) => {
        if (!controller.isConnected()) {
          ctx.state.providerCatalogControllers.delete(controller);
          return;
        }
        controller.render();
      });
    }
    function loadProviderCatalogsInCard(card) {
      const loads = Array.from(card.querySelectorAll(".fallback-row")).map((row) => ctx.state.providerCatalogControllerByRow.get(row)).filter(Boolean).map((controller) => controller.load());
      return Promise.allSettled(loads);
    }
    function toggleProviderCatalogCard(card) {
      const isCollapsed = card.classList.toggle("collapsed");
      if (!isCollapsed) {
        void loadProviderCatalogsInCard(card);
      }
    }
    function clearRulesCache() {
      ctx.state.providerModelsCacheEpoch += 1;
      ctx.state.providerModelsCache.clear();
      ctx.state.providerModelsRequests.clear();
    }
    function parseProviderModelsMetadata(value) {
      const trimmedValue = value.trim();
      if (!trimmedValue) {
        return void 0;
      }
      try {
        const parsedValue = JSON.parse(trimmedValue);
        return parsedValue === null ? void 0 : parsedValue;
      } catch (error) {
        throw new Error("Provider models metadata must be valid JSON.");
      }
    }
    function normalizeProviderModelsMetadata(value) {
      if (value === void 0 || value === null) {
        return "";
      }
      return JSON.stringify(value, null, 2);
    }
    function parseProviderJsonObject(value, label) {
      const trimmedValue = value.trim();
      if (!trimmedValue) {
        return void 0;
      }
      let parsedValue;
      try {
        parsedValue = JSON.parse(trimmedValue);
      } catch (error) {
        throw new Error(`${label} must be valid JSON.`);
      }
      if (parsedValue === null) {
        return void 0;
      }
      if (typeof parsedValue !== "object" || Array.isArray(parsedValue)) {
        throw new Error(`${label} must be a JSON object.`);
      }
      return parsedValue;
    }
    function normalizeProviderJsonObject(value) {
      if (value === void 0 || value === null) {
        return "";
      }
      return JSON.stringify(value, null, 2);
    }
    function parseAvailableModels(value) {
      const items = String(value || "").split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
      const seen = /* @__PURE__ */ new Set();
      const result = [];
      items.forEach((item) => {
        if (seen.has(item)) {
          return;
        }
        seen.add(item);
        result.push(item);
      });
      return result;
    }
    function normalizeAvailableModels(value) {
      return Array.isArray(value) ? value.join("\n") : "";
    }
    const PROVIDER_FIELD_TOOLTIPS = {
      name: "editor:tooltips.providerName",
      baseUrl: "editor:tooltips.baseUrl",
      apikey: "editor:tooltips.apiKey",
      type: "editor:tooltips.apiType",
      proxy: "editor:tooltips.proxy",
      modelsMetadata: "editor:tooltips.modelsMetadata",
      availableModels: "editor:tooltips.availableModels",
      routing: "editor:tooltips.routing",
      upstreamKeyPools: "editor:tooltips.upstreamKeyPools",
      upstreamLimits: "editor:tooltips.upstreamLimits",
      modelId: "editor:tooltips.modelId",
      rpm: "editor:tooltips.rpm",
      rpd: "editor:tooltips.rpd",
      tpm: "editor:tooltips.tpm",
      tpd: "editor:tooltips.tpd"
    };
    const UPSTREAM_LIMIT_KEYS = ["rpm", "rpd", "tpm", "tpd"];
    function splitProviderModelsMetadata(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        return { upstreamLimits: [], extra: value === void 0 ? void 0 : value };
      }
      const upstreamLimits = [];
      const extra = {};
      Object.entries(value).forEach(([modelId, modelMeta]) => {
        if (modelMeta && typeof modelMeta === "object" && !Array.isArray(modelMeta) && modelMeta.upstream_limits && typeof modelMeta.upstream_limits === "object") {
          const limits = modelMeta.upstream_limits;
          const row = { modelId };
          UPSTREAM_LIMIT_KEYS.forEach((key) => {
            row[key] = limits[key] === void 0 || limits[key] === null ? "" : String(limits[key]);
          });
          upstreamLimits.push(row);
          const rest = { ...modelMeta };
          delete rest.upstream_limits;
          if (Object.keys(rest).length > 0) {
            extra[modelId] = rest;
          }
        } else {
          extra[modelId] = modelMeta;
        }
      });
      return {
        upstreamLimits,
        extra: Object.keys(extra).length > 0 ? extra : void 0
      };
    }
    function buildUpstreamLimitsSection(initialModels) {
      const container = document.createElement("div");
      container.className = "upstream-limits-section";
      const header = document.createElement("div");
      header.className = "upstream-limits-section-header";
      const title = document.createElement("div");
      title.className = "upstream-limits-title field-label";
      ctx.bindLocalizedText(title, "editor:fields.upstreamLimits");
      header.appendChild(title);
      const titleFieldWrapper = { querySelector: () => title };
      ctx.attachFieldTooltip(titleFieldWrapper, PROVIDER_FIELD_TOOLTIPS.upstreamLimits);
      const addButton = document.createElement("button");
      addButton.type = "button";
      addButton.className = "secondary-button upstream-limit-add";
      ctx.bindKnownActionText(addButton, "Add Model");
      header.appendChild(addButton);
      container.appendChild(header);
      const list = document.createElement("div");
      list.className = "upstream-limits-list";
      container.appendChild(list);
      const emptyState = document.createElement("div");
      emptyState.className = "upstream-limits-empty";
      ctx.bindLocalizedText(emptyState, "editor:messages.noUpstreamLimits");
      container.appendChild(emptyState);
      function refreshEmptyState() {
        emptyState.hidden = list.children.length > 0;
      }
      function appendRow(initialRow) {
        const row = document.createElement("div");
        row.className = "upstream-limit-row";
        const modelInput = ctx.createTextInput("upstream-limit-model", "deepseek/deepseek-r1:free");
        modelInput.value = initialRow && initialRow.modelId ? initialRow.modelId : "";
        const modelField = ctx.createFieldGroup("Model", modelInput, "upstream-limit-model-field");
        ctx.attachFieldTooltip(modelField, PROVIDER_FIELD_TOOLTIPS.modelId);
        row.appendChild(modelField);
        UPSTREAM_LIMIT_KEYS.forEach((key) => {
          const input = ctx.createNumberInput(`upstream-limit-${key}`, "");
          input.min = "1";
          input.value = initialRow && initialRow[key] !== void 0 ? initialRow[key] : "";
          const field = ctx.createFieldGroup(key.toUpperCase(), input, `upstream-limit-${key}-field`);
          ctx.attachFieldTooltip(field, PROVIDER_FIELD_TOOLTIPS[key]);
          row.appendChild(field);
        });
        const removeButton = document.createElement("button");
        removeButton.type = "button";
        removeButton.className = "upstream-limit-remove";
        ctx.bindKnownActionText(removeButton, "Remove");
        removeButton.addEventListener("click", () => {
          row.remove();
          refreshEmptyState();
        });
        row.appendChild(removeButton);
        list.appendChild(row);
        refreshEmptyState();
      }
      const splitInitial = splitProviderModelsMetadata(initialModels);
      splitInitial.upstreamLimits.forEach(appendRow);
      refreshEmptyState();
      addButton.addEventListener("click", () => appendRow());
      function getRows() {
        return Array.from(list.querySelectorAll(".upstream-limit-row")).map((row) => {
          const result = {
            modelId: row.querySelector(".upstream-limit-model").value.trim()
          };
          UPSTREAM_LIMIT_KEYS.forEach((key) => {
            result[key] = row.querySelector(`.upstream-limit-${key}`).value.trim();
          });
          return result;
        });
      }
      return { container, getRows };
    }
    function mergeUpstreamLimitsIntoModels(extraMetadata, rows, providerName) {
      const merged = extraMetadata && typeof extraMetadata === "object" && !Array.isArray(extraMetadata) ? { ...extraMetadata } : {};
      const seen = /* @__PURE__ */ new Set();
      rows.forEach((row, index) => {
        const modelId = row.modelId;
        if (!modelId) {
          const hasAnyValue = UPSTREAM_LIMIT_KEYS.some((key) => row[key]);
          if (hasAnyValue) {
            throw new Error(`Provider '${providerName}' upstream limits row #${index + 1} is missing a model id.`);
          }
          return;
        }
        if (seen.has(modelId)) {
          throw new Error(`Provider '${providerName}' has duplicate upstream limits for model '${modelId}'.`);
        }
        seen.add(modelId);
        const limits = {};
        UPSTREAM_LIMIT_KEYS.forEach((key) => {
          const raw = row[key];
          if (raw === "" || raw === void 0) return;
          const parsed = Number(raw);
          if (!Number.isInteger(parsed) || parsed <= 0) {
            throw new Error(`Provider '${providerName}' model '${modelId}' ${key} must be a positive integer.`);
          }
          limits[key] = parsed;
        });
        if (Object.keys(limits).length === 0) return;
        const base = merged[modelId] && typeof merged[modelId] === "object" && !Array.isArray(merged[modelId]) ? { ...merged[modelId] } : {};
        base.upstream_limits = limits;
        merged[modelId] = base;
      });
      return Object.keys(merged).length > 0 ? merged : void 0;
    }
    function normalizeProviderCardForSave(providerCard) {
      const nameInput = providerCard.querySelector(".provider-name-input");
      const baseUrlInput = providerCard.querySelector(".provider-base-url-input");
      const apiKeyInput = providerCard.querySelector(".provider-api-key-input");
      const typeSelect = providerCard.querySelector(".provider-type-select");
      const proxyInput = providerCard.querySelector(".provider-proxy-input");
      const modelsInput = providerCard.querySelector(".provider-models-input");
      const routingInput = providerCard.querySelector(".provider-routing-input");
      const upstreamKeyPoolsInput = providerCard.querySelector(".provider-upstream-key-pools-input");
      const name = nameInput.value.trim();
      const baseUrl = baseUrlInput.value.trim();
      const apikey = apiKeyInput.value.trim();
      const type = typeSelect.value.trim();
      const proxy = proxyInput.value.trim();
      if (!name) {
        throw new Error("Each provider must have a name.");
      }
      if (!baseUrl) {
        throw new Error(`Provider '${name}' must have a base URL.`);
      }
      if (!/^https?:\/\//i.test(baseUrl)) {
        throw new Error(`Provider '${name}' base URL must start with http:// or https://.`);
      }
      if (!["openai", "anthropic"].includes(type)) {
        throw new Error(`Provider '${name}' must use API type openai or anthropic.`);
      }
      const routing = parseProviderJsonObject(routingInput ? routingInput.value : "", "Provider routing");
      const upstreamKeyPools = parseProviderJsonObject(
        upstreamKeyPoolsInput ? upstreamKeyPoolsInput.value : "",
        "Provider upstream key pools"
      );
      if (!apikey && !upstreamKeyPools) {
        throw new Error(`Provider '${name}' must have an API key, environment reference, or upstream key pool.`);
      }
      const providerPayload = {
        name,
        baseUrl,
        type
      };
      if (apikey) {
        providerPayload.apikey = apikey;
      }
      if (proxy) {
        providerPayload.proxy = proxy;
      }
      if (routing) {
        providerPayload.routing = routing;
      }
      if (upstreamKeyPools) {
        providerPayload.upstream_key_pools = upstreamKeyPools;
      }
      const extraModels = parseProviderModelsMetadata(modelsInput.value);
      const upstreamRows = providerCard._getUpstreamLimitsRows ? providerCard._getUpstreamLimitsRows() : [];
      const mergedModels = mergeUpstreamLimitsIntoModels(extraModels, upstreamRows, name);
      if (mergedModels !== void 0) {
        providerPayload.models = mergedModels;
      } else if (extraModels !== void 0) {
        providerPayload.models = extraModels;
      }
      const availableModelsInput = providerCard.querySelector(".provider-available-models-input");
      const availableModels = parseAvailableModels(availableModelsInput ? availableModelsInput.value : "");
      if (availableModels.length > 0) {
        providerPayload.available_models = availableModels;
      }
      return providerPayload;
    }
    function getProvidersPayloadForSave() {
      const providers = Array.from(ctx.elements.providersList.querySelectorAll(".provider-card")).map(normalizeProviderCardForSave);
      const seenProviderNames = /* @__PURE__ */ new Set();
      const duplicateNames = [];
      providers.forEach((provider) => {
        if (seenProviderNames.has(provider.name)) {
          duplicateNames.push(provider.name);
        }
        seenProviderNames.add(provider.name);
      });
      if (duplicateNames.length > 0) {
        throw new Error(`Duplicate provider names: ${duplicateNames.join(", ")}.`);
      }
      return { providers };
    }
    function getProvidersSnapshotContent() {
      return ctx.stableSerialize(getProvidersPayloadForSave());
    }
    function buildProviderCard(initialData = {}) {
      const card = document.createElement("section");
      card.className = "rule-card provider-card collapsed";
      const cardHeader = document.createElement("div");
      cardHeader.className = "rule-card-header";
      const titleWrap = document.createElement("div");
      titleWrap.className = "rule-card-title";
      const providerNameInput = ctx.createTextInput("provider-name-input", "openrouter");
      providerNameInput.value = initialData.name || "";
      const providerNameField = ctx.createFieldGroup("Provider Name", providerNameInput, "gateway-model-field");
      ctx.attachFieldTooltip(providerNameField, PROVIDER_FIELD_TOOLTIPS.name);
      titleWrap.appendChild(providerNameField);
      const headerLeft = document.createElement("div");
      headerLeft.className = "rule-card-header-left";
      headerLeft.appendChild(ctx.createAccordionToggle(card));
      headerLeft.appendChild(titleWrap);
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, "Remove Provider");
      removeButton.addEventListener("click", () => {
        card.remove();
        ctx.refreshProvidersEmptyState();
      });
      cardHeader.appendChild(headerLeft);
      cardHeader.appendChild(removeButton);
      const cardBody = document.createElement("div");
      cardBody.className = "rule-card-body";
      const fieldsGrid = document.createElement("div");
      fieldsGrid.className = "fallback-row-grid provider-fields-grid";
      const baseUrlInput = ctx.createTextInput("provider-base-url-input", "https://api.example.com/v1");
      baseUrlInput.value = initialData.baseUrl || "";
      const apiKeyInput = ctx.createTextInput("provider-api-key-input", "${APIKEY_PROVIDER}");
      apiKeyInput.type = "password";
      apiKeyInput.autocomplete = "off";
      apiKeyInput.value = initialData.apikey || "";
      const apiKeyRevealButton = document.createElement("button");
      apiKeyRevealButton.type = "button";
      apiKeyRevealButton.className = "secondary-button compact-button";
      ctx.bindLocalizedText(
        apiKeyRevealButton,
        () => apiKeyInput.type === "password" ? "editor:actions.show" : "editor:actions.hide"
      );
      apiKeyRevealButton.addEventListener("click", () => {
        const shouldShow = apiKeyInput.type === "password";
        apiKeyInput.type = shouldShow ? "text" : "password";
        ctx.rerenderLocale();
      });
      const apiKeyControl = document.createElement("div");
      apiKeyControl.className = "secret-input-row";
      apiKeyControl.appendChild(apiKeyInput);
      apiKeyControl.appendChild(apiKeyRevealButton);
      const typeSelect = ctx.createSelect("provider-type-select");
      ctx.setSelectOptions(typeSelect, ["openai", "anthropic"], "Choose API type", initialData.type || "openai");
      const proxyInput = ctx.createTextInput("provider-proxy-input", "${PROXY_PROVIDER} or https://proxy:8080");
      proxyInput.value = initialData.proxy || "";
      const baseUrlField = ctx.createFieldGroup("Base URL", baseUrlInput, "provider-base-url-field");
      ctx.attachFieldTooltip(baseUrlField, PROVIDER_FIELD_TOOLTIPS.baseUrl);
      const apiKeyField = ctx.createFieldGroup("API Key", apiKeyControl, "provider-api-key-field");
      ctx.attachFieldTooltip(apiKeyField, PROVIDER_FIELD_TOOLTIPS.apikey);
      const typeField = ctx.createFieldGroup("API Type", typeSelect, "provider-type-field");
      ctx.attachFieldTooltip(typeField, PROVIDER_FIELD_TOOLTIPS.type);
      const proxyField = ctx.createFieldGroup("Proxy (optional)", proxyInput, "provider-proxy-field");
      ctx.attachFieldTooltip(proxyField, PROVIDER_FIELD_TOOLTIPS.proxy);
      fieldsGrid.appendChild(baseUrlField);
      fieldsGrid.appendChild(apiKeyField);
      fieldsGrid.appendChild(typeField);
      fieldsGrid.appendChild(proxyField);
      const advancedDetails = document.createElement("details");
      advancedDetails.className = "advanced-options";
      const advancedSummary = document.createElement("summary");
      ctx.bindLocalizedText(advancedSummary, "editor:actions.advanced");
      advancedDetails.appendChild(advancedSummary);
      const advancedGrid = document.createElement("div");
      advancedGrid.className = "advanced-grid";
      const { container: upstreamLimitsContainer, getRows: getUpstreamLimitsRows } = buildUpstreamLimitsSection(initialData.models);
      advancedGrid.appendChild(upstreamLimitsContainer);
      const splitModels = splitProviderModelsMetadata(initialData.models);
      const modelsInput = ctx.createTextarea("provider-models-input", '{"pricing": {"input": 0.1}}');
      modelsInput.value = normalizeProviderModelsMetadata(splitModels.extra);
      const modelsField = ctx.createFieldGroup("Models Metadata (JSON)", modelsInput, "textarea-group");
      ctx.attachFieldTooltip(modelsField, PROVIDER_FIELD_TOOLTIPS.modelsMetadata);
      ctx.appendFieldHint(modelsField, "editor:hints.providerMetadata");
      advancedGrid.appendChild(modelsField);
      const availableModelsInput = ctx.createTextarea("provider-available-models-input", "deepseek/deepseek-r1:free\nqwen/qwen3-max");
      availableModelsInput.value = normalizeAvailableModels(initialData.available_models);
      const availableModelsField = ctx.createFieldGroup("Available Models (optional)", availableModelsInput, "textarea-group");
      ctx.attachFieldTooltip(availableModelsField, PROVIDER_FIELD_TOOLTIPS.availableModels);
      ctx.appendFieldHint(availableModelsField, "editor:hints.providerAvailableModels");
      advancedGrid.appendChild(availableModelsField);
      const routingInput = ctx.createTextarea(
        "provider-routing-input",
        '{"strategy": "round-robin", "session_affinity": false}'
      );
      routingInput.value = normalizeProviderJsonObject(initialData.routing);
      const routingField = ctx.createFieldGroup("Routing Policy (JSON)", routingInput, "textarea-group");
      ctx.attachFieldTooltip(routingField, PROVIDER_FIELD_TOOLTIPS.routing);
      ctx.appendFieldHint(routingField, "editor:hints.providerRouting");
      advancedGrid.appendChild(routingField);
      const upstreamKeyPoolsInput = ctx.createTextarea(
        "provider-upstream-key-pools-input",
        '{"main": {"strategy": "priority", "keys": [{"id": "primary", "apikey": "${PROVIDER_KEY_1}", "priority": 100}]}}'
      );
      upstreamKeyPoolsInput.value = normalizeProviderJsonObject(initialData.upstream_key_pools);
      const upstreamKeyPoolsField = ctx.createFieldGroup("Upstream Key Pools (JSON)", upstreamKeyPoolsInput, "textarea-group");
      ctx.attachFieldTooltip(upstreamKeyPoolsField, PROVIDER_FIELD_TOOLTIPS.upstreamKeyPools);
      ctx.appendFieldHint(upstreamKeyPoolsField, "editor:hints.providerKeyPools");
      advancedGrid.appendChild(upstreamKeyPoolsField);
      advancedDetails.appendChild(advancedGrid);
      cardBody.appendChild(fieldsGrid);
      cardBody.appendChild(advancedDetails);
      card.appendChild(cardHeader);
      card.appendChild(cardBody);
      card._getUpstreamLimitsRows = getUpstreamLimitsRows;
      return card;
    }
    async function renderProviders(providers) {
      ctx.elements.providersList.textContent = "";
      if (!Array.isArray(providers) || providers.length === 0) {
        ctx.refreshProvidersEmptyState();
        return;
      }
      providers.forEach((provider) => {
        ctx.elements.providersList.appendChild(buildProviderCard(provider));
      });
      ctx.refreshProvidersEmptyState();
    }
    async function loadProvidersEditor() {
      const requestId = ++ctx.state.providersLoadRequestId;
      ctx.state.originalProvidersContent = null;
      ctx.setProvidersLoadState("loading");
      ctx.showLocalizedMessage("info", "Loading Providers...");
      try {
        const loaded = await ctx.loadConfigDocument(
          "providers",
          "/v1/config/providers/structured",
          {
            validate: ctx.validateProvidersPayload,
            apply: async (payload) => {
              if (requestId !== ctx.state.providersLoadRequestId) {
                return;
              }
              await renderProviders(payload.providers);
              ctx.state.availableProviders = payload.providers.map((provider) => typeof provider.name === "string" ? provider.name.trim() : "").filter(Boolean);
            }
          }
        );
        if (!loaded || requestId !== ctx.state.providersLoadRequestId) {
          if (!loaded) {
            ctx.showLocalizedMessage("warning", "A newer local edit was preserved. Reload again to discard it.");
          }
          return false;
        }
        ctx.state.originalProvidersContent = getProvidersSnapshotContent();
        ctx.setProvidersLoadState("ready");
        ctx.showLocalizedMessage("success", "Providers loaded successfully.");
        return true;
      } catch (error) {
        if (requestId !== ctx.state.providersLoadRequestId) {
          return false;
        }
        console.error("Error fetching Providers:", error);
        ctx.showLocalizedError("Error loading Providers:", error);
        ctx.state.originalProvidersContent = null;
        ctx.setProvidersLoadState("error");
        return false;
      }
    }
    async function saveProviders() {
      if (ctx.state.providersLoadState !== "ready" || ctx.state.originalProvidersContent === null) {
        ctx.showLocalizedMessage("error", "Cannot save Providers: provider configuration has not loaded successfully.");
        return;
      }
      let payload;
      try {
        payload = ctx.getProvidersPayloadForSave();
      } catch (error) {
        ctx.showClientValidationError(error);
        return;
      }
      ctx.elements.saveButton.disabled = true;
      ctx.showLocalizedMessage("info", "Saving Providers...");
      try {
        const result = await ctx.saveConfigDocument(
          "providers",
          "/v1/config/providers/structured",
          payload,
          {
            errorTitle: "Error saving Providers:",
            validatePublished: ctx.validateProvidersPayload
          }
        );
        if (!result) {
          return;
        }
        if (ctx.state.editorMutationVersion === result.submittedMutationVersion) {
          const application = ctx.renderProviders(result.payload.providers);
          ctx.syncInteractionLock();
          await application;
          ctx.state.originalProvidersContent = ctx.getProvidersSnapshotContent();
          ctx.clearRulesCache();
          ctx.state.availableProviders = result.payload.providers.map((provider) => typeof provider.name === "string" ? provider.name.trim() : "").filter(Boolean);
        }
        ctx.showLocalizedMessage(
          "success",
          ctx.safeSuccessMessage(result.body, "Providers updated successfully.")
        );
      } catch (error) {
        console.error("Error saving Providers:", error);
        ctx.showLocalizedError("Error saving Providers:", error);
      } finally {
        ctx.updateProvidersControlsState();
      }
    }
    Object.assign(ctx, {
      getProviderModels,
      catalogStatusText,
      populateProviderModelDataList,
      createLazyProviderCatalogRowController,
      invalidateProviderCatalogRows,
      rerenderProviderCatalogStatuses,
      loadProviderCatalogsInCard,
      toggleProviderCatalogCard,
      clearRulesCache,
      parseProviderModelsMetadata,
      normalizeProviderModelsMetadata,
      parseProviderJsonObject,
      normalizeProviderJsonObject,
      parseAvailableModels,
      normalizeAvailableModels,
      splitProviderModelsMetadata,
      buildUpstreamLimitsSection,
      mergeUpstreamLimitsIntoModels,
      normalizeProviderCardForSave,
      getProvidersPayloadForSave,
      getProvidersSnapshotContent,
      buildProviderCard,
      renderProviders,
      loadProvidersEditor,
      saveProviders
    });
  }

  // src/router.mjs
  function registerRouter(ctx) {
    function setRouterFallbackIndexOptions(select, gatewayModel, selectedIndex) {
      const chain = Array.isArray(ctx.state.routerFallbackChains[gatewayModel]) ? ctx.state.routerFallbackChains[gatewayModel] : [];
      const currentValue = selectedIndex !== void 0 && selectedIndex !== null ? String(selectedIndex) : "";
      select.textContent = "";
      const placeholderOption = document.createElement("option");
      placeholderOption.value = "";
      ctx.bindLocalizedText(placeholderOption, "editor:placeholders.selectFallbackEntry");
      select.appendChild(placeholderOption);
      chain.forEach((entry) => {
        const option = document.createElement("option");
        option.value = String(entry.index);
        ctx.bindLocalizedValue(option, () => {
          const unknown = ctx.t("editor:messages.unknown");
          return `${entry.index} · ${entry.provider || unknown}/${entry.model || unknown}`;
        });
        select.appendChild(option);
      });
      if (currentValue && !chain.some((entry) => String(entry.index) === currentValue)) {
        const staleOption = document.createElement("option");
        staleOption.value = currentValue;
        ctx.bindLocalizedText(staleOption, "editor:placeholders.notConfigured", () => ({ value: currentValue }));
        staleOption.dataset.stale = "true";
        select.appendChild(staleOption);
      }
      select.value = currentValue;
    }
    function buildRouterTargetRow(initialData) {
      const data = initialData || {};
      const row = document.createElement("div");
      row.className = "fallback-row router-target-row";
      const fieldsGrid = document.createElement("div");
      fieldsGrid.className = "fallback-row-grid router-target-grid";
      const typeSelect = ctx.createSelect("router-target-type-select");
      ctx.setSelectOptions(typeSelect, ["gateway_model", "fallback_entry"], "Choose target type", data.type || "gateway_model");
      const gatewayTargetSelect = ctx.createSelect("router-gateway-target-select");
      ctx.setModelSelectOptions(gatewayTargetSelect, ctx.state.gatewayModelCatalog.chat, data.model || "");
      const gatewayTargetGroup = ctx.createFieldGroup("Gateway Target", gatewayTargetSelect, "router-gateway-target-field");
      ctx.appendFieldHint(gatewayTargetGroup, "editor:hints.routerGatewayTarget");
      const fallbackGatewaySelect = ctx.createSelect("router-fallback-gateway-select");
      ctx.setModelSelectOptions(fallbackGatewaySelect, ctx.state.gatewayModelCatalog.chat, data.gateway_model || "");
      const fallbackGatewayGroup = ctx.createFieldGroup("Fallback Gateway", fallbackGatewaySelect, "router-fallback-gateway-field");
      ctx.appendFieldHint(fallbackGatewayGroup, "editor:hints.routerFallbackGateway");
      const fallbackIndexSelect = ctx.createSelect("router-fallback-index-select");
      setRouterFallbackIndexOptions(fallbackIndexSelect, data.gateway_model || "", data.index);
      const fallbackIndexGroup = ctx.createFieldGroup("Start At Entry", fallbackIndexSelect, "router-fallback-index-field");
      ctx.appendFieldHint(fallbackIndexGroup, "editor:hints.routerFallbackEntry");
      fieldsGrid.appendChild(ctx.createFieldGroup("Target Type", typeSelect, "router-target-type-field"));
      fieldsGrid.appendChild(gatewayTargetGroup);
      fieldsGrid.appendChild(fallbackGatewayGroup);
      fieldsGrid.appendChild(fallbackIndexGroup);
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, "Remove Target");
      removeButton.addEventListener("click", () => {
        row.remove();
      });
      const rowActions = document.createElement("div");
      rowActions.className = "fallback-row-actions";
      rowActions.appendChild(removeButton);
      const syncVisibility = () => {
        const isFallbackEntry = typeSelect.value === "fallback_entry";
        gatewayTargetGroup.style.display = isFallbackEntry ? "none" : "";
        fallbackGatewayGroup.style.display = isFallbackEntry ? "" : "none";
        fallbackIndexGroup.style.display = isFallbackEntry ? "" : "none";
      };
      typeSelect.addEventListener("change", syncVisibility);
      fallbackGatewaySelect.addEventListener("change", () => {
        setRouterFallbackIndexOptions(fallbackIndexSelect, fallbackGatewaySelect.value, "");
      });
      syncVisibility();
      row.appendChild(fieldsGrid);
      row.appendChild(rowActions);
      return row;
    }
    function buildRouterCard(initialData) {
      const data = initialData || {};
      const card = document.createElement("section");
      card.className = "rule-card router-card collapsed";
      const cardHeader = document.createElement("div");
      cardHeader.className = "rule-card-header";
      const titleWrap = document.createElement("div");
      titleWrap.className = "rule-card-title";
      const gatewayModelInput = ctx.createTextInput("gateway-model-input", "llmgateway/router");
      gatewayModelInput.value = data.gateway_model_name || "";
      titleWrap.appendChild(ctx.createFieldGroup("Gateway Model Name", gatewayModelInput, "gateway-model-field"));
      const selectorSelect = ctx.createSelect("router-selector-model-select");
      ctx.setModelSelectOptions(selectorSelect, ctx.state.gatewayModelCatalog.chat, data.selector_model || "");
      const selectorField = ctx.createFieldGroup("Selector Model", selectorSelect, "router-selector-model-field");
      ctx.appendFieldHint(selectorField, "editor:hints.routerSelector");
      titleWrap.appendChild(selectorField);
      const headerLeft = document.createElement("div");
      headerLeft.className = "rule-card-header-left";
      headerLeft.appendChild(ctx.createAccordionToggle(card));
      headerLeft.appendChild(titleWrap);
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "icon-button danger-button";
      ctx.bindKnownActionText(removeButton, "Remove Model");
      removeButton.addEventListener("click", () => {
        card.remove();
        ctx.refreshRouterEmptyState();
      });
      cardHeader.appendChild(headerLeft);
      cardHeader.appendChild(removeButton);
      const cardBody = document.createElement("div");
      cardBody.className = "rule-card-body";
      const targetsList = document.createElement("div");
      targetsList.className = "fallback-list router-target-list";
      const addTargetButton = document.createElement("button");
      addTargetButton.type = "button";
      addTargetButton.className = "secondary-button add-fallback-button";
      ctx.bindKnownActionText(addTargetButton, "Add Target");
      addTargetButton.addEventListener("click", () => {
        targetsList.appendChild(buildRouterTargetRow({ type: "gateway_model" }));
      });
      const targets = Array.isArray(data.targets) ? data.targets : [];
      targets.forEach((target) => {
        targetsList.appendChild(buildRouterTargetRow(target));
      });
      if (targets.length === 0) {
        targetsList.appendChild(buildRouterTargetRow({ type: "gateway_model" }));
      }
      cardBody.appendChild(ctx.buildFusionSectionHeading("editor:sections.router.targetsHeading"));
      cardBody.appendChild(targetsList);
      cardBody.appendChild(addTargetButton);
      card.appendChild(cardHeader);
      card.appendChild(cardBody);
      return card;
    }
    function normalizeRouterTargetRow(row, gatewayModelName) {
      const type = row.querySelector(".router-target-type-select").value.trim();
      if (type === "gateway_model") {
        const model = row.querySelector(".router-gateway-target-select").value.trim();
        if (!model) {
          throw new Error(`Router model '${gatewayModelName}' has a gateway target without a model.`);
        }
        return { type, model };
      }
      if (type === "fallback_entry") {
        const gatewayModel = row.querySelector(".router-fallback-gateway-select").value.trim();
        const indexRaw = row.querySelector(".router-fallback-index-select").value.trim();
        if (!gatewayModel) {
          throw new Error(`Router model '${gatewayModelName}' has a fallback-entry target without a gateway model.`);
        }
        if (indexRaw === "") {
          throw new Error(`Router model '${gatewayModelName}' has a fallback-entry target without an entry index.`);
        }
        const index = Number.parseInt(indexRaw, 10);
        if (!Number.isFinite(index) || index < 0) {
          throw new Error(`Router model '${gatewayModelName}' has an invalid fallback-entry index.`);
        }
        return { type, gateway_model: gatewayModel, index };
      }
      throw new Error(`Router model '${gatewayModelName}' has unsupported target type '${type}'.`);
    }
    function normalizeRouterCardForSave(card) {
      const gatewayModelName = card.querySelector(".gateway-model-input").value.trim();
      if (!gatewayModelName) {
        throw new Error("Each router model must have a gateway model name.");
      }
      const selectorModel = card.querySelector(".router-selector-model-select").value.trim();
      if (!selectorModel) {
        throw new Error(`Router model '${gatewayModelName}' must have a selector model.`);
      }
      const targetRows = Array.from(card.querySelectorAll(".router-target-list > .router-target-row"));
      const targets = targetRows.map((row) => normalizeRouterTargetRow(row, gatewayModelName));
      if (targets.length === 0) {
        throw new Error(`Router model '${gatewayModelName}' must have at least one target.`);
      }
      return {
        gateway_model_name: gatewayModelName,
        selector_model: selectorModel,
        targets
      };
    }
    function getRouterPayloadForSave() {
      const rules = Array.from(ctx.elements.routerList.querySelectorAll(".router-card")).map(normalizeRouterCardForSave);
      return { rules };
    }
    function getNormalizedRouterContent() {
      return ctx.stableSerialize(getRouterPayloadForSave());
    }
    async function renderRouter(rules) {
      ctx.elements.routerList.textContent = "";
      if (Array.isArray(rules)) {
        rules.forEach((rule) => {
          ctx.elements.routerList.appendChild(buildRouterCard(rule));
        });
      }
      ctx.refreshRouterEmptyState();
    }
    async function loadRouterEditor() {
      try {
        const loaded = await ctx.loadConfigDocument(
          "router",
          "/v1/config/router-rules/structured",
          {
            validate: ctx.validateRouterPayload,
            apply: async (payload) => {
              ctx.state.gatewayModelCatalog.chat = payload.chat_models || [];
              ctx.state.routerFallbackChains = payload.fallback_chains || {};
              await renderRouter(payload.rules);
            }
          }
        );
        if (!loaded) {
          ctx.showLocalizedMessage("warning", "A newer local edit was preserved. Reload again to discard it.");
          return false;
        }
        ctx.state.originalRouterContent = getNormalizedRouterContent();
        ctx.updateSaveButtonDisabledState();
        ctx.showLocalizedMessage("success", "Router Models loaded successfully.");
        return true;
      } catch (error) {
        console.error("Error fetching Router Models:", error);
        ctx.showLocalizedError("Error loading Router Models:", error);
        ctx.state.originalRouterContent = null;
        ctx.updateSaveButtonDisabledState();
        return false;
      }
    }
    async function saveRouter() {
      ctx.elements.saveButton.disabled = true;
      ctx.showLocalizedMessage("info", "Saving Router Models...");
      let payload;
      try {
        payload = getRouterPayloadForSave();
      } catch (error) {
        ctx.showClientValidationError(error);
        return;
      }
      try {
        const result = await ctx.saveConfigDocument(
          "router",
          "/v1/config/router-rules/structured",
          payload,
          {
            errorTitle: "Error saving Router Models:",
            validatePublished: ctx.validateRouterPayload
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
          "success",
          ctx.safeSuccessMessage(result.body, "Router Models updated successfully.")
        );
      } catch (error) {
        console.error("Error saving Router:", error);
        ctx.showLocalizedError("Error saving Router Models:", error);
      } finally {
        ctx.updateSaveButtonDisabledState();
      }
    }
    Object.assign(ctx, {
      setRouterFallbackIndexOptions,
      buildRouterTargetRow,
      buildRouterCard,
      normalizeRouterTargetRow,
      normalizeRouterCardForSave,
      getRouterPayloadForSave,
      getNormalizedRouterContent,
      renderRouter,
      loadRouterEditor,
      saveRouter
    });
  }

  // src/state.mjs
  var EDITOR_CONSTANTS = Object.freeze({
    MODELS_CACHE_TTL_MS: 15 * 60 * 1e3,
    IMAGE_REQUEST_FORMAT_OPTIONS: Object.freeze(["openai_images", "openai_images_multipart", "nvidia_genai_json"]),
    IMAGE_RESPONSE_FORMAT_OPTIONS: Object.freeze(["openai_images", "nvidia_artifacts"]),
    AUDIO_REQUEST_FORMAT_OPTIONS: Object.freeze(["nvidia_riva_grpc"]),
    MAX_SAFE_ERROR_LENGTH: 240,
    STRONG_ETAG_PATTERN: /^"[\x21\x23-\x7E\x80-\xFF]+"$/
  });
  function createEditorState() {
    const documentBases = /* @__PURE__ */ new Map([
      ["fallback", null],
      ["operation", null],
      ["fusion", null],
      ["router", null],
      ["providers", null],
      ["model", null]
    ]);
    return {
      documentBases,
      loadRequestIds: new Map(Array.from(documentBases.keys(), (documentName) => [documentName, 0])),
      busyDocuments: /* @__PURE__ */ new Set(),
      lockedControls: /* @__PURE__ */ new Map(),
      lockedSubtrees: /* @__PURE__ */ new Map(),
      lockedDraggables: /* @__PURE__ */ new Map(),
      evalTabLoadStates: /* @__PURE__ */ new Map([
        ["openrouter-free", "idle"],
        ["fallback-eval", "idle"]
      ]),
      activeEditor: "rules",
      originalRulesContent: null,
      originalEmbeddingsContent: null,
      originalRerankContent: null,
      originalImagesContent: null,
      originalAudioContent: null,
      originalWebContent: null,
      originalProvidersContent: null,
      originalFusionContent: null,
      originalRouterContent: null,
      originalModelRulesContent: null,
      availableProviders: [],
      embeddingRules: [],
      rerankRules: [],
      imageGenerationRules: [],
      imageEditRules: [],
      audioSpeechRules: [],
      audioTranscriptionRules: [],
      pdfConversionRules: [],
      webSearchRules: [],
      webReadRules: [],
      webResearchRules: [],
      webDeepResearchRules: [],
      gatewayModelCatalog: {
        chat: [],
        embeddings: [],
        images_generations: [],
        web_search: [],
        web_read: []
      },
      routerFallbackChains: {},
      providerModelsCache: /* @__PURE__ */ new Map(),
      providerModelsRequests: /* @__PURE__ */ new Map(),
      providerModelsCacheEpoch: 0,
      providerCatalogControllers: /* @__PURE__ */ new Set(),
      providerCatalogControllerByRow: /* @__PURE__ */ new WeakMap(),
      providersLoadState: "loading",
      providersLoadRequestId: 0,
      saveInFlight: false,
      editorMutationVersion: 0,
      fallbackEvalPollTimer: null,
      openRouterFreePollTimer: null,
      fallbackEvalPollingEnabled: false,
      openRouterFreePollingEnabled: false,
      providerCatalogGeneration: 0,
      rulesTabsController: null,
      activeRulesTabContext: null,
      capabilityAutofillStatus: null,
      currentMessage: null,
      localizedBindings: /* @__PURE__ */ new Set()
    };
  }

  // src/index.mjs
  function startEditor(ctx) {
    const {
      WEB_SEARCH_CARD_OPTIONS,
      WEB_READ_CARD_OPTIONS,
      WEB_RESEARCH_CARD_OPTIONS,
      WEB_DEEP_RESEARCH_CARD_OPTIONS
    } = ctx;
    ctx.state.rulesTabsController = createRulesTabsController(ctx);
    ctx.elements.addProviderButton.addEventListener("click", () => {
      if (ctx.state.providersLoadState !== "ready") {
        ctx.showLocalizedMessage("error", "Cannot add Provider: provider configuration has not loaded successfully.");
        return;
      }
      const providerCard = ctx.buildProviderCard({});
      providerCard.classList.remove("collapsed");
      ctx.elements.providersList.appendChild(providerCard);
      ctx.refreshProvidersEmptyState();
    });
    ctx.elements.addFusionButton.addEventListener("click", () => {
      const fusionCard = ctx.buildFusionCard({});
      fusionCard.classList.remove("collapsed");
      ctx.elements.fusionList.appendChild(fusionCard);
      ctx.refreshFusionEmptyState();
    });
    ctx.elements.addRouterButton.addEventListener("click", () => {
      const routerCard = ctx.buildRouterCard({});
      routerCard.classList.remove("collapsed");
      ctx.elements.routerList.appendChild(routerCard);
      ctx.refreshRouterEmptyState();
    });
    ctx.elements.addRuleButton.addEventListener("click", () => {
      const ruleCard = ctx.buildRuleCard({});
      ruleCard.classList.remove("collapsed");
      ctx.elements.rulesList.appendChild(ruleCard);
      ctx.refreshRulesEmptyState();
    });
    ctx.elements.previewRulesButton.addEventListener("click", () => {
      ctx.previewRulesChanges();
    });
    ctx.elements.suggestEvalOrderButton.addEventListener("click", () => {
      void ctx.renderSuggestedFallbackOrder();
    });
    ctx.elements.addEmbeddingButton.addEventListener("click", () => {
      const embeddingCard = ctx.buildEmbeddingCard({});
      embeddingCard.classList.remove("collapsed");
      ctx.elements.embeddingsList.appendChild(embeddingCard);
      ctx.refreshEmbeddingsEmptyState();
    });
    ctx.elements.addRerankButton.addEventListener("click", () => {
      const rerankCard = ctx.buildRerankCard({});
      rerankCard.classList.remove("collapsed");
      ctx.elements.rerankList.appendChild(rerankCard);
      ctx.refreshRerankEmptyState();
    });
    ctx.elements.addImageGenerationButton.addEventListener("click", () => {
      const imageGenerationCard = ctx.buildImageCard({}, {
        gatewayPlaceholder: "llmgateway/image-generation-model",
        defaultTargetPath: "/images/generations",
        refreshEmptyState: ctx.refreshImageGenerationEmptyState
      });
      imageGenerationCard.classList.remove("collapsed");
      ctx.elements.imageGenerationList.appendChild(imageGenerationCard);
      ctx.refreshImageGenerationEmptyState();
    });
    ctx.elements.addImageEditButton.addEventListener("click", () => {
      const imageEditCard = ctx.buildImageCard({}, {
        gatewayPlaceholder: "llmgateway/image-edit-model",
        defaultTargetPath: "/images/edits",
        refreshEmptyState: ctx.refreshImageEditEmptyState
      });
      imageEditCard.classList.remove("collapsed");
      ctx.elements.imageEditList.appendChild(imageEditCard);
      ctx.refreshImageEditEmptyState();
    });
    ctx.elements.addAudioSpeechButton.addEventListener("click", () => {
      const audioCard = ctx.buildAudioSpeechCard({});
      audioCard.classList.remove("collapsed");
      ctx.elements.audioSpeechList.appendChild(audioCard);
      ctx.refreshAudioSpeechEmptyState();
    });
    ctx.elements.addAudioTranscriptionButton.addEventListener("click", () => {
      const audioCard = ctx.buildAudioTranscriptionCard({});
      audioCard.classList.remove("collapsed");
      ctx.elements.audioTranscriptionsList.appendChild(audioCard);
      ctx.refreshAudioTranscriptionsEmptyState();
    });
    ctx.elements.addWebSearchButton.addEventListener("click", () => {
      const searchCard = ctx.buildWebSearchCard({}, WEB_SEARCH_CARD_OPTIONS);
      searchCard.classList.remove("collapsed");
      ctx.elements.webSearchList.appendChild(searchCard);
      ctx.refreshWebSearchEmptyState();
      ctx.refreshWebCrossDropdowns();
    });
    ctx.elements.addWebReadButton.addEventListener("click", () => {
      const readCard = ctx.buildWebReadCard({}, WEB_READ_CARD_OPTIONS);
      readCard.classList.remove("collapsed");
      ctx.elements.webReadList.appendChild(readCard);
      ctx.refreshWebReadEmptyState();
      ctx.refreshWebCrossDropdowns();
    });
    ctx.elements.addWebResearchButton.addEventListener("click", () => {
      const researchCard = ctx.buildWebReferenceCard({}, WEB_RESEARCH_CARD_OPTIONS);
      researchCard.classList.remove("collapsed");
      ctx.elements.webResearchList.appendChild(researchCard);
      ctx.refreshWebResearchEmptyState();
    });
    ctx.elements.addWebDeepResearchButton.addEventListener("click", () => {
      const deepResearchCard = ctx.buildWebReferenceCard({}, WEB_DEEP_RESEARCH_CARD_OPTIONS);
      deepResearchCard.classList.remove("collapsed");
      ctx.elements.webDeepResearchList.appendChild(deepResearchCard);
      ctx.refreshWebDeepResearchEmptyState();
    });
    ctx.elements.runFallbackEvalButton.addEventListener("click", () => {
      void ctx.runFallbackModelEval();
    });
    if (ctx.elements.runOpenRouterFreeEvalButton) {
      ctx.elements.runOpenRouterFreeEvalButton.addEventListener("click", () => {
        void ctx.runOpenRouterFreeEval();
      });
    }
    ctx.elements.reloadEditorDocumentButton.addEventListener("click", () => {
      void ctx.reloadAfterConflict();
    });
    const editorRoot = document.querySelector(".container");
    ["input", "change"].forEach((eventName) => {
      editorRoot.addEventListener(eventName, () => {
        ctx.state.editorMutationVersion += 1;
        ctx.updateDirtyIndicator();
      });
    });
    editorRoot.addEventListener("click", () => {
      queueMicrotask(ctx.updateDirtyIndicator);
    });
    window.addEventListener("beforeunload", (event) => {
      if (!ctx.isCurrentEditorDirty()) {
        return;
      }
      event.preventDefault();
      event.returnValue = "";
    });
    ctx.elements.saveButton.addEventListener("click", async function() {
      if (ctx.state.saveInFlight) {
        return;
      }
      let saveAction = null;
      if (ctx.state.activeEditor === "rules") {
        saveAction = ctx.saveRules;
      } else if (ctx.state.activeEditor === "embeddings") {
        saveAction = ctx.saveEmbeddings;
      } else if (ctx.state.activeEditor === "rerank") {
        saveAction = ctx.saveRerank;
      } else if (ctx.state.activeEditor === "images") {
        saveAction = ctx.saveImages;
      } else if (ctx.state.activeEditor === "audio") {
        saveAction = ctx.saveAudio;
      } else if (ctx.state.activeEditor === "web") {
        saveAction = ctx.saveWeb;
      } else if (ctx.state.activeEditor === "providers") {
        saveAction = ctx.saveProviders;
      } else if (ctx.state.activeEditor === "fusion") {
        saveAction = ctx.saveFusion;
      } else if (ctx.state.activeEditor === "router") {
        saveAction = ctx.saveRouter;
      } else if (ctx.state.activeEditor === "model-rules") {
        saveAction = ctx.saveModelRules;
      }
      if (!saveAction) {
        ctx.showLocalizedMessage("error", "No active editor selected.");
        return;
      }
      ctx.state.saveInFlight = true;
      ctx.updateSaveButtonDisabledState();
      try {
        await saveAction();
      } finally {
        ctx.state.saveInFlight = false;
        ctx.updateSaveButtonDisabledState();
      }
    });
    async function initEditor() {
      await window.gatewayI18n.ready;
      window.gatewayI18n.bind(document);
      ctx.gatewayI18n.subscribe(ctx.rerenderLocale);
      ctx.updateControlsVisibility();
      void ctx.initializeOpenRouterFreeTabAvailability();
      await ctx.state.rulesTabsController.activate(ctx.state.activeEditor, { reason: "initial" });
    }
    initEditor().catch(() => void 0);
  }
  document.addEventListener("DOMContentLoaded", function() {
    const { apiFetch } = window.gatewayAuth;
    const ctx = {
      apiFetch,
      gatewayI18n: window.gatewayI18n,
      elements: createEditorElements(),
      constants: EDITOR_CONSTANTS,
      state: createEditorState()
    };
    registerCore(ctx);
    registerProviders(ctx);
    registerFallback(ctx);
    registerOperations(ctx);
    registerFusion(ctx);
    registerRouter(ctx);
    registerEntityPanel(ctx);
    startEditor(ctx);
  });
})();
