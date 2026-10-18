import assert from 'node:assert/strict';
import test from 'node:test';

import { registerOperations } from '../src/operations.mjs';

function buildCtx() {
    globalThis.Theme = { attachToggle() {} };
    const ctx = {
        gatewayI18n: {},
        ConfigUiError: Error,
        parseObjectTextarea(value) {
            const trimmedValue = value.trim();
            return trimmedValue ? JSON.parse(trimmedValue) : {};
        },
        applyOperationCostCalculator(payload) {
            return payload;
        },
        applyRetrySettingsToPayload() {},
        state: {
            embeddingRules: [],
            decisionsRules: [],
            rerankRules: [],
            imageGenerationRules: [],
            imageEditRules: [],
            audioTranscriptionRules: [],
            audioSpeechRules: [],
            audioDiarizationRules: [],
            audioSpeakerTranscriptionRules: [],
            pdfConversionRules: [],
            gatewayModelCatalog: { audio_transcriptions: [], audio_diarizations: [] },
            editorMutationVersion: 0,
        },
    };
    registerOperations(ctx);
    return ctx;
}

function fakeNode(queries) {
    return {
        querySelector(selector) {
            return queries[selector] ?? null;
        },
        querySelectorAll(selector) {
            return queries[selector] ?? [];
        },
    };
}

test('payload round trip keeps audio_diarizations and audio_speaker_transcriptions', () => {
    const ctx = buildCtx();
    const audio_diarizations = [{
        gateway_model_name: 'llmgateway/diarizations',
        routes: [{provider: 'neuraldeep', model: 'nemotron-3-diarization', target_path: '/audio/diarize'}],
    }];
    const audio_speaker_transcriptions = [{
        gateway_model_name: 'llmgateway/stt-speakers',
        transcription_model: 'llmgateway/whisper-large-v3',
        diarization_model: 'llmgateway/diarizations',
    }];
    const basePayload = {
        embeddings: [],
        decisions: [],
        rerank: [],
        images_generations: [],
        images_edits: [],
        audio_diarizations,
        audio_speaker_transcriptions,
    };

    const payload = ctx.buildOperationRoutesPayload({embeddings: []}, basePayload);
    assert.deepEqual(payload.audio_diarizations, audio_diarizations);
    assert.deepEqual(payload.audio_speaker_transcriptions, audio_speaker_transcriptions);

    ctx.applyOperationRulesPayload(basePayload);
    assert.deepEqual(ctx.state.audioDiarizationRules, audio_diarizations);
    assert.deepEqual(ctx.state.audioSpeakerTranscriptionRules, audio_speaker_transcriptions);
});

test('speaker transcription card saves only gateway, transcription and diarization models', () => {
    const ctx = buildCtx();
    const card = fakeNode({
        '.gateway-model-input': {value: ' llmgateway/stt-speakers '},
        '.transcription-model-input': {value: 'llmgateway/whisper-large-v3'},
        '.diarization-model-input': {value: 'llmgateway/diarizations'},
    });

    assert.deepEqual(ctx.normalizeAudioSpeakerTranscriptionCardForSave(card), {
        gateway_model_name: 'llmgateway/stt-speakers',
        transcription_model: 'llmgateway/whisper-large-v3',
        diarization_model: 'llmgateway/diarizations',
    });
});

test('audio route payload_transforms is preserved on save', () => {
    const ctx = buildCtx();
    const payload_transforms = {value_maps: {language: {ru: 'Russian'}}};
    const routeRow = fakeNode({
        '.provider-select': {value: 'neuraldeep'},
        '.model-input': {value: 'nemotron-3-diarization'},
        '.target-path-input': {value: ''},
        '.custom-body-params-input': {value: ''},
        '.custom-headers-input': {value: ''},
        '.payload-transforms-input': {value: JSON.stringify(payload_transforms)},
        '.retry-delay-input': {value: ''},
        '.retry-count-input': {value: ''},
    });
    const emptyTransformsRow = fakeNode({
        '.provider-select': {value: 'neuraldeep'},
        '.model-input': {value: 'nemotron-3-diarization'},
        '.target-path-input': {value: ''},
        '.custom-body-params-input': {value: ''},
        '.custom-headers-input': {value: ''},
        '.payload-transforms-input': {value: '  '},
        '.retry-delay-input': {value: ''},
        '.retry-count-input': {value: ''},
    });

    const saved = ctx.normalizeAudioDiarizationRouteForSave(routeRow);
    assert.equal(saved.target_path, '/audio/diarize');
    assert.deepEqual(saved.payload_transforms, payload_transforms);
    assert.equal('payload_transforms' in ctx.normalizeAudioDiarizationRouteForSave(emptyTransformsRow), false);
    assert.deepEqual(ctx.normalizeAudioSpeechRouteForSave(routeRow).payload_transforms, payload_transforms);
});
