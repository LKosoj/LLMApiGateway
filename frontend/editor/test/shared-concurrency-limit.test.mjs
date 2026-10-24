import assert from 'node:assert/strict';
import test from 'node:test';

import { registerProviders } from '../src/providers.mjs';

const ctx = {};
registerProviders(ctx);

test('shared concurrency limit is saved at provider level with other shared key limits', () => {
    assert.deepEqual(
        ctx.parseSharedUpstreamLimits({ rpm: '10', max_concurrency: '2' }, 'test'),
        { rpm: 10, max_concurrency: 2 },
    );
    assert.deepEqual(ctx.parseSharedUpstreamLimits({ max_concurrency: '' }, 'test'), {});
});

test('shared concurrency limit requires a positive integer', () => {
    for (const maxConcurrency of ['0', '-1', '1.5', 'true', 'Infinity']) {
        assert.throws(() => ctx.parseSharedUpstreamLimits({ max_concurrency: maxConcurrency }, 'test'));
    }
});

test('shared concurrency limit is not added to per-model upstream limits', () => {
    const models = ctx.mergeUpstreamLimitsIntoModels({}, [{
        modelId: 'model-a',
        rpm: '10',
        rpd: '',
        tpm: '',
        tpd: '',
        max_concurrency: '2',
    }], 'test');

    assert.deepEqual(models, { 'model-a': { upstream_limits: { rpm: 10 } } });
});
