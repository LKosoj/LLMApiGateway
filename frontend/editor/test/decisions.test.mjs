import assert from 'node:assert/strict';
import test from 'node:test';

import { registerOperations } from '../src/operations.mjs';

function buildCtx() {
    globalThis.Theme = { attachToggle() {} };
    const ctx = {
        gatewayI18n: {},
        ConfigUiError: Error,
        parseObjectTextarea: value => (value ? {value} : {}),
        state: {
            embeddingRules: [],
            decisionsRules: [],
            rerankRules: [],
            imageGenerationRules: [],
            videoGenerationRules: [],
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

test('saving another operation section preserves loaded video routes and order', () => {
    const ctx = buildCtx();
    const videos = [{
        gateway_model_name: 'llmgateway/wan3.0',
        routes: [
            {provider: 'bettertoken', model: 'wan-3.0', target_path: '/videos', request_format: 'bettertoken_videos'},
            {provider: 'routerai', model: 'alibaba/wan-3.0', target_path: '/videos', request_format: 'routerai_videos'},
        ],
    }];

    const payload = ctx.buildOperationRoutesPayload(
        {embeddings: []},
        {embeddings: [], decisions: [], rerank: [], images_generations: [], images_edits: [], videos_generations: videos},
    );

    assert.deepEqual(payload.videos_generations, videos);
});

test('video route serialization omits unsupported body and response fields', () => {
    const ctx = buildCtx();
    const controls = {
        '.provider-select': {value: 'bettertoken'},
        '.model-input': {value: 'wan-3.0'},
        '.target-path-input': {value: '/videos'},
        '.request-format-select': {value: 'bettertoken_videos'},
        '.response-format-select': null,
        '.custom-body-params-input': null,
        '.custom-headers-input': {value: 'trace'},
        '.request-mapping-input': null,
        '.response-mapping-input': null,
        '.retry-delay-input': null,
        '.retry-count-input': null,
    };
    const payload = ctx.normalizeImageRouteForSave(
        {querySelector: selector => controls[selector]},
        '/videos',
        'video generation',
        {video: true},
    );
    assert.deepEqual(payload, {
        provider: 'bettertoken',
        model: 'wan-3.0',
        target_path: '/videos',
        custom_headers: {value: 'trace'},
        request_format: 'bettertoken_videos',
    });
});
