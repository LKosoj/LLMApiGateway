import assert from 'node:assert/strict';
import test from 'node:test';

import { registerCore } from '../src/core.mjs';

// safeResponseError only reads the parsed body and the response status, so a
// duck-typed response is enough. registerCore touches no DOM while it defines
// its functions — the elements it closes over are only reached when a handler
// actually runs.
function buildCtx() {
    const ctx = {
        gatewayI18n: { t: (key, values = {}) => `${key}:${JSON.stringify(values)}` },
        constants: { MAX_SAFE_ERROR_LENGTH: 500 },
    };
    registerCore(ctx);
    return ctx;
}

function errorBody(...errors) {
    return {
        detail: {
            code: 'config_validation_failed',
            message: 'Configuration validation failed.',
            errors,
        },
    };
}

test('a refused resync shows which source the process could not parse', () => {
    const ctx = buildCtx();

    const detail = ctx.safeResponseError(
        { status: 400 },
        errorBody({
            type: 'source_invalid',
            loc: ['router_rules'],
            msg: 'router_rules on disk did not pass validation in this process.',
        }),
    );

    assert.equal(
        detail,
        'Configuration validation failed. router_rules on disk did not pass '
        + 'validation in this process.',
    );
});

test('entries whose loc path carries the meaning stay behind the message', () => {
    const ctx = buildCtx();

    const detail = ctx.safeResponseError(
        { status: 400 },
        errorBody({ type: 'value_error', loc: ['targets', 0], msg: 'unusable alone' }),
    );

    assert.equal(detail, 'Configuration validation failed.');
});
