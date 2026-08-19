import assert from 'node:assert/strict';
import test from 'node:test';

import { registerRouter } from '../src/router.mjs';

// Router selector hints: the save-path normalisers only read `.value` off the
// controls they find, so duck-typed stand-ins are enough here. Rendering the
// controls themselves needs a real DOM and is covered by the browser pass.
function buildCtx() {
    const ctx = {};
    registerRouter(ctx);
    return ctx;
}

function fakeRow(values) {
    return {
        querySelector(selector) {
            if (!(selector in values)) {
                throw new Error(`unexpected selector ${selector}`);
            }
            return { value: values[selector] };
        },
    };
}

function fakeCard(values, rows) {
    return {
        querySelector(selector) {
            if (!(selector in values)) {
                throw new Error(`unexpected selector ${selector}`);
            }
            return { value: values[selector] };
        },
        querySelectorAll() {
            return rows;
        },
    };
}

const GATEWAY_ROW = {
    '.router-target-type-select': 'gateway_model',
    '.router-gateway-target-select': 'gateway/high',
    '.router-target-description-input': 'Code and tool calls',
    '.router-target-cost-hint-select': 'premium',
};

test('gateway target keeps the description and cost hint an operator filled in', () => {
    const ctx = buildCtx();
    const target = ctx.normalizeRouterTargetRow(fakeRow(GATEWAY_ROW), 'gateway/router');
    assert.deepEqual(target, {
        type: 'gateway_model',
        model: 'gateway/high',
        description: 'Code and tool calls',
        cost_hint: 'premium',
    });
});

test('fallback-entry target carries the same hints', () => {
    const ctx = buildCtx();
    const target = ctx.normalizeRouterTargetRow(
        fakeRow({
            '.router-target-type-select': 'fallback_entry',
            '.router-fallback-gateway-select': 'gateway/high',
            '.router-fallback-index-select': '1',
            '.router-target-description-input': 'Second entry only',
            '.router-target-cost-hint-select': 'cheap',
        }),
        'gateway/router',
    );
    assert.deepEqual(target, {
        type: 'fallback_entry',
        gateway_model: 'gateway/high',
        index: 1,
        description: 'Second entry only',
        cost_hint: 'cheap',
    });
});

test('empty hints are dropped instead of saved as blanks', () => {
    const ctx = buildCtx();
    const target = ctx.normalizeRouterTargetRow(
        fakeRow({
            ...GATEWAY_ROW,
            '.router-target-description-input': '   ',
            '.router-target-cost-hint-select': '',
        }),
        'gateway/router',
    );
    assert.deepEqual(target, { type: 'gateway_model', model: 'gateway/high' });
});

test('routing policy is saved on the card, and omitted when left empty', () => {
    const ctx = buildCtx();
    const withPolicy = ctx.normalizeRouterCardForSave(
        fakeCard(
            {
                '.gateway-model-input': 'gateway/router',
                '.router-selector-model-select': 'gateway/selector',
                '.router-routing-policy-input': '  Prefer the cheapest candidate.  ',
            },
            [fakeRow(GATEWAY_ROW)],
        ),
    );
    assert.equal(withPolicy.routing_policy, 'Prefer the cheapest candidate.');

    const withoutPolicy = ctx.normalizeRouterCardForSave(
        fakeCard(
            {
                '.gateway-model-input': 'gateway/router',
                '.router-selector-model-select': 'gateway/selector',
                '.router-routing-policy-input': '',
            },
            [fakeRow(GATEWAY_ROW)],
        ),
    );
    assert.ok(!('routing_policy' in withoutPolicy));
});
