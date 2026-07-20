import { registerFallback } from './fallback.mjs';
import { registerFusion } from './fusion.mjs';
import { createEditorElements, registerCore, registerEntityPanel, createRulesTabsController } from './core.mjs';
import { registerOperations } from './operations.mjs';
import { registerProviders } from './providers.mjs';
import { registerRouter } from './router.mjs';
import { createEditorState, EDITOR_CONSTANTS } from './state.mjs';

function startEditor(ctx) {
    const {
        WEB_SEARCH_CARD_OPTIONS,
        WEB_READ_CARD_OPTIONS,
        WEB_RESEARCH_CARD_OPTIONS,
        WEB_DEEP_RESEARCH_CARD_OPTIONS,
    } = ctx;

    ctx.state.rulesTabsController = createRulesTabsController(ctx);
    ctx.elements.addProviderButton.addEventListener('click', () => {
        if (ctx.state.providersLoadState !== 'ready') {
            ctx.showLocalizedMessage('error', 'Cannot add Provider: provider configuration has not loaded successfully.');
            return;
        }
        const providerCard = ctx.buildProviderCard({});
        providerCard.classList.remove('collapsed');
        ctx.elements.providersList.appendChild(providerCard);
        ctx.refreshProvidersEmptyState();
    });
    ctx.elements.addFusionButton.addEventListener('click', () => {
        const fusionCard = ctx.buildFusionCard({});
        fusionCard.classList.remove('collapsed');
        ctx.elements.fusionList.appendChild(fusionCard);
        ctx.refreshFusionEmptyState();
    });
    ctx.elements.addRouterButton.addEventListener('click', () => {
        const routerCard = ctx.buildRouterCard({});
        routerCard.classList.remove('collapsed');
        ctx.elements.routerList.appendChild(routerCard);
        ctx.refreshRouterEmptyState();
    });
    ctx.elements.addRuleButton.addEventListener('click', () => {
        const ruleCard = ctx.buildRuleCard({});
        ruleCard.classList.remove('collapsed');
        ctx.elements.rulesList.appendChild(ruleCard);
        ctx.refreshRulesEmptyState();
    });
    ctx.elements.previewRulesButton.addEventListener('click', () => {
        ctx.previewRulesChanges();
    });
    ctx.elements.suggestEvalOrderButton.addEventListener('click', () => {
        void ctx.renderSuggestedFallbackOrder();
    });
    ctx.elements.addEmbeddingButton.addEventListener('click', () => {
        const embeddingCard = ctx.buildEmbeddingCard({});
        embeddingCard.classList.remove('collapsed');
        ctx.elements.embeddingsList.appendChild(embeddingCard);
        ctx.refreshEmbeddingsEmptyState();
    });
    ctx.elements.addRerankButton.addEventListener('click', () => {
        const rerankCard = ctx.buildRerankCard({});
        rerankCard.classList.remove('collapsed');
        ctx.elements.rerankList.appendChild(rerankCard);
        ctx.refreshRerankEmptyState();
    });
    ctx.elements.addImageGenerationButton.addEventListener('click', () => {
        const imageGenerationCard = ctx.buildImageCard({}, {
            gatewayPlaceholder: 'llmgateway/image-generation-model',
            defaultTargetPath: '/images/generations',
            refreshEmptyState: ctx.refreshImageGenerationEmptyState,
        });
        imageGenerationCard.classList.remove('collapsed');
        ctx.elements.imageGenerationList.appendChild(imageGenerationCard);
        ctx.refreshImageGenerationEmptyState();
    });
    ctx.elements.addImageEditButton.addEventListener('click', () => {
        const imageEditCard = ctx.buildImageCard({}, {
            gatewayPlaceholder: 'llmgateway/image-edit-model',
            defaultTargetPath: '/images/edits',
            refreshEmptyState: ctx.refreshImageEditEmptyState,
        });
        imageEditCard.classList.remove('collapsed');
        ctx.elements.imageEditList.appendChild(imageEditCard);
        ctx.refreshImageEditEmptyState();
    });
    ctx.elements.addAudioSpeechButton.addEventListener('click', () => {
        const audioCard = ctx.buildAudioSpeechCard({});
        audioCard.classList.remove('collapsed');
        ctx.elements.audioSpeechList.appendChild(audioCard);
        ctx.refreshAudioSpeechEmptyState();
    });
    ctx.elements.addAudioTranscriptionButton.addEventListener('click', () => {
        const audioCard = ctx.buildAudioTranscriptionCard({});
        audioCard.classList.remove('collapsed');
        ctx.elements.audioTranscriptionsList.appendChild(audioCard);
        ctx.refreshAudioTranscriptionsEmptyState();
    });
    ctx.elements.addWebSearchButton.addEventListener('click', () => {
        const searchCard = ctx.buildWebSearchCard({}, WEB_SEARCH_CARD_OPTIONS);
        searchCard.classList.remove('collapsed');
        ctx.elements.webSearchList.appendChild(searchCard);
        ctx.refreshWebSearchEmptyState();
        ctx.refreshWebCrossDropdowns();
    });
    ctx.elements.addWebReadButton.addEventListener('click', () => {
        const readCard = ctx.buildWebReadCard({}, WEB_READ_CARD_OPTIONS);
        readCard.classList.remove('collapsed');
        ctx.elements.webReadList.appendChild(readCard);
        ctx.refreshWebReadEmptyState();
        ctx.refreshWebCrossDropdowns();
    });
    ctx.elements.addWebResearchButton.addEventListener('click', () => {
        const researchCard = ctx.buildWebReferenceCard({}, WEB_RESEARCH_CARD_OPTIONS);
        researchCard.classList.remove('collapsed');
        ctx.elements.webResearchList.appendChild(researchCard);
        ctx.refreshWebResearchEmptyState();
    });
    ctx.elements.addWebDeepResearchButton.addEventListener('click', () => {
        const deepResearchCard = ctx.buildWebReferenceCard({}, WEB_DEEP_RESEARCH_CARD_OPTIONS);
        deepResearchCard.classList.remove('collapsed');
        ctx.elements.webDeepResearchList.appendChild(deepResearchCard);
        ctx.refreshWebDeepResearchEmptyState();
    });
    ctx.elements.runFallbackEvalButton.addEventListener('click', () => {
        void ctx.runFallbackModelEval();
    });

    if (ctx.elements.runOpenRouterFreeEvalButton) {
        ctx.elements.runOpenRouterFreeEvalButton.addEventListener('click', () => {
            void ctx.runOpenRouterFreeEval();
        });
    }

    ctx.elements.reloadEditorDocumentButton.addEventListener('click', () => {
        void ctx.reloadActiveDocument();
    });

    const editorRoot = document.querySelector('.container');
    ['input', 'change'].forEach(eventName => {
        editorRoot.addEventListener(eventName, () => {
            ctx.state.editorMutationVersion += 1;
            ctx.updateDirtyIndicator();
        });
    });
    editorRoot.addEventListener('click', () => {
        queueMicrotask(ctx.updateDirtyIndicator);
    });

    window.addEventListener('beforeunload', (event) => {
        if (!ctx.isCurrentEditorDirty()) {
            return;
        }
        event.preventDefault();
        event.returnValue = '';
    });

    ctx.elements.saveButton.addEventListener('click', async function () {
        if (ctx.state.saveInFlight) {
            return;
        }
        let saveAction = null;
        if (ctx.state.activeEditor === 'rules') {
            saveAction = ctx.saveRules;
        } else if (ctx.state.activeEditor === 'embeddings') {
            saveAction = ctx.saveEmbeddings;
        } else if (ctx.state.activeEditor === 'rerank') {
            saveAction = ctx.saveRerank;
        } else if (ctx.state.activeEditor === 'images') {
            saveAction = ctx.saveImages;
        } else if (ctx.state.activeEditor === 'audio') {
            saveAction = ctx.saveAudio;
        } else if (ctx.state.activeEditor === 'web') {
            saveAction = ctx.saveWeb;
        } else if (ctx.state.activeEditor === 'providers') {
            saveAction = ctx.saveProviders;
        } else if (ctx.state.activeEditor === 'fusion') {
            saveAction = ctx.saveFusion;
        } else if (ctx.state.activeEditor === 'router') {
            saveAction = ctx.saveRouter;
        } else if (ctx.state.activeEditor === 'model-rules') {
            saveAction = ctx.saveModelRules;
        }

        if (!saveAction) {
            ctx.showLocalizedMessage('error', 'No active editor selected.');
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
        await ctx.state.rulesTabsController.activate(ctx.state.activeEditor, { reason: 'initial' });
    }

    initEditor().catch(() => undefined);
}

document.addEventListener('DOMContentLoaded', function () {
    const { apiFetch } = window.gatewayAuth;
    const ctx = {
        apiFetch,
        gatewayI18n: window.gatewayI18n,
        elements: createEditorElements(),
        constants: EDITOR_CONSTANTS,
        state: createEditorState(),
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
