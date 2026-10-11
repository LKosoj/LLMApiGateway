import assert from 'node:assert/strict';
import test from 'node:test';

import { registerOperations } from '../src/operations.mjs';

function buildCtx() {
    globalThis.Theme = { attachToggle() {} };
    const ctx = {
        gatewayI18n: {},
        ConfigUiError: Error,
        state: {
            embeddingRules: [],
            decisionsRules: [],
            rerankRules: [],
            imageGenerationRules: [],
            imageEditRules: [],
            audioTranscriptionRules: [],
            audioSpeechRules: [],
            pdfConversionRules: [],
            editorMutationVersion: 0,
        },
    };
    registerOperations(ctx);
    return ctx;
}

test('registerOperations exposes the Decisions empty-state callback', () => {
    const ctx = buildCtx();

    assert.equal(typeof ctx.refreshDecisionsEmptyState, 'function');
});

test('saving another operation section preserves loaded Decisions routes', () => {
    const ctx = buildCtx();
    const decisions = [{
        gateway_model_name: 'gateway/decision',
        routes: [{provider: 'aitunnel', model: 'jev-1.13', target_path: '/decisions'}],
    }];

    const payload = ctx.buildOperationRoutesPayload(
        {embeddings: []},
        {embeddings: [], decisions, rerank: [], images_generations: [], images_edits: []},
    );

    assert.deepEqual(payload.decisions, decisions);
});
