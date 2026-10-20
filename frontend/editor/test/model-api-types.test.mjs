import assert from 'node:assert/strict';
import test from 'node:test';
import { registerProviders } from '../src/providers.mjs';

const ctx = {};
registerProviders(ctx);

test('API exceptions round trip without listing other models or losing rates and limits', () => {
    const original = {
        special: { api_type: 'responses', input_rate: 2, output_rate: 10, upstream_limits: { rpm: 5 } },
        ordinary: { input_rate: 1, output_rate: 3 },
    };
    const split = ctx.splitProviderModelsMetadata(original);
    assert.deepEqual(split.apiTypes, [{ modelId: 'special', apiType: 'responses' }]);
    const merged = ctx.mergeUpstreamLimitsIntoModels(
        ctx.mergeModelApiTypesIntoModels(split.extra, split.apiTypes, 'test', 'openai'),
        split.upstreamLimits, 'test',
    );
    assert.deepEqual(merged, original);
    const removed = ctx.mergeUpstreamLimitsIntoModels(
        ctx.mergeModelApiTypesIntoModels(split.extra, [], 'test', 'openai'),
        split.upstreamLimits, 'test',
    );
    assert.deepEqual(removed.special, { input_rate: 2, output_rate: 10, upstream_limits: { rpm: 5 } });
    assert.equal(original.special.api_type, 'responses');
});

test('an API-only model disappears when its exception is removed', () => {
    const split = ctx.splitProviderModelsMetadata({ special: { api_type: 'responses' } });
    assert.equal(ctx.mergeModelApiTypesIntoModels(split.extra, [], 'test', 'openai'), undefined);
});

test('invalid API exception rows fail explicitly', () => {
    const row = { modelId: 'special', apiType: 'responses' };
    for (const rows of [[{ ...row, modelId: '' }], [{ ...row, apiType: '' }], [{ ...row, apiType: 'invalid' }], [row, row]]) {
        assert.throws(() => ctx.mergeModelApiTypesIntoModels({}, rows, 'test', 'openai'));
    }
    assert.throws(() => ctx.mergeModelApiTypesIntoModels({}, [row], 'test', 'anthropic'));
    assert.throws(() => ctx.mergeModelApiTypesIntoModels({}, [{ ...row, apiType: 'chat_completions' }], 'test', 'anthropic'));
});
