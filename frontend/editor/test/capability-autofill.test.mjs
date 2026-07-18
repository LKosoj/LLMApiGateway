import assert from 'node:assert/strict';
import test from 'node:test';

import { registerCore } from '../src/core.mjs';

// F-auto (capability autofill): the round-trip logic in
// applyCapabilityFieldsToPayload and the status-matching logic in
// capabilityAutofillSourceFor are pure enough to exercise without a DOM --
// they only read `.value`/`.dataset` off whatever is handed to them, so a
// duck-typed stand-in for a <select>/<input> control is enough here. The
// actual badge-vs-control DOM branching (wrapCapabilityField) is covered by
// tests/test_rules_editor_capability_autofill_browser.py, which drives a real
// browser.
function buildCtx() {
    const ctx = {};
    registerCore(ctx);
    return ctx;
}

function fakeControl(value, locked) {
    return { value, dataset: locked === undefined ? {} : { capabilityLocked: locked ? 'true' : 'false' } };
}

test('applyCapabilityFieldsToPayload keeps an untouched autofilled field in the list', () => {
    const ctx = buildCtx();
    const payload = {};
    ctx.applyCapabilityFieldsToPayload(
        payload,
        fakeControl('true', true),
        fakeControl('', false),
        fakeControl('128000', true),
    );
    assert.deepEqual(payload, {
        supports_vision: true,
        context_window: 128000,
        capabilities_autofilled: ['supports_vision', 'context_window'],
    });
});

test('applyCapabilityFieldsToPayload excludes a field once its control is unlocked (override)', () => {
    const ctx = buildCtx();
    const payload = {};
    // Same values as the locked case above, but the vision control has been
    // "edited" (dataset.capabilityLocked flipped to 'false' by the badge's
    // Edit button) -- it must drop out of capabilities_autofilled even though
    // its value is unchanged.
    ctx.applyCapabilityFieldsToPayload(
        payload,
        fakeControl('true', false),
        fakeControl('', false),
        fakeControl('128000', true),
    );
    assert.deepEqual(payload, {
        supports_vision: true,
        context_window: 128000,
        capabilities_autofilled: ['context_window'],
    });
});

test('applyCapabilityFieldsToPayload omits capabilities_autofilled entirely when nothing is locked', () => {
    const ctx = buildCtx();
    const payload = {};
    ctx.applyCapabilityFieldsToPayload(
        payload,
        fakeControl('true', false),
        fakeControl('false', false),
        fakeControl('', false),
    );
    assert.deepEqual(payload, { supports_vision: true, supports_tools: false });
    assert.ok(!('capabilities_autofilled' in payload));
});

test('applyCapabilityFieldsToPayload tolerates controls with no dataset (never wrapped)', () => {
    const ctx = buildCtx();
    const payload = {};
    ctx.applyCapabilityFieldsToPayload(
        payload,
        { value: 'true' },
        { value: '' },
        { value: '' },
    );
    assert.deepEqual(payload, { supports_vision: true });
});

test('capabilityAutofillSourceFor matches by gateway model name and candidate index', () => {
    const ctx = buildCtx();
    const status = {
        lastRunAt: '2026-07-18T12:00:00Z',
        lastError: null,
        resolutions: {
            'llmgateway/model-a': [
                {
                    index: 0,
                    provider: 'prov-a',
                    model: 'vendor/model-a',
                    fields: { supports_vision: { source: 'provider' } },
                },
                {
                    index: 'context_overflow_fallback',
                    provider: 'prov-a',
                    model: 'vendor/model-a',
                    fields: { context_window: { source: 'openrouter' } },
                },
            ],
        },
    };

    assert.equal(ctx.capabilityAutofillSourceFor(status, 'llmgateway/model-a', 0, 'supports_vision'), 'provider');
    assert.equal(
        ctx.capabilityAutofillSourceFor(status, 'llmgateway/model-a', 'context_overflow_fallback', 'context_window'),
        'openrouter',
    );
    // Same index, a field this run's resolutions didn't touch.
    assert.equal(ctx.capabilityAutofillSourceFor(status, 'llmgateway/model-a', 0, 'supports_tools'), null);
    // Wrong index / wrong gateway model / missing status: no source, never throws.
    assert.equal(ctx.capabilityAutofillSourceFor(status, 'llmgateway/model-a', 1, 'supports_vision'), null);
    assert.equal(ctx.capabilityAutofillSourceFor(status, 'llmgateway/unknown', 0, 'supports_vision'), null);
    assert.equal(ctx.capabilityAutofillSourceFor(null, 'llmgateway/model-a', 0, 'supports_vision'), null);
});

// The two tests below exercise wrapCapabilityField itself (badge creation and
// its "Edit"/"Изменить" button's real click handler), unlike the
// applyCapabilityFieldsToPayload tests above which hand-set dataset.capabilityLocked
// and never touch the click handler. There is no jsdom dependency in this
// package, so this is a small purpose-built DOM stub -- just enough of
// document.createElement()'s surface (dataset/className/hidden/textContent,
// appendChild/remove, addEventListener) for wrapCapabilityField to run
// unmodified. Fuller DOM/CSS rendering is covered by
// tests/test_rules_editor_capability_autofill_browser.py, which drives a real
// browser.
const CAPABILITY_I18N_STRINGS = {
    'editor:capability.supported': 'Да',
    'editor:capability.unsupported': 'Нет',
    'editor:capability.autofilled': 'Заполнено автоматически',
    'editor:capability.override': 'Изменить',
    'editor:capability.source.provider': 'из каталога провайдера',
    'editor:capability.source.openrouter': 'из каталога OpenRouter',
};

function createFakeElement(tag) {
    return {
        tagName: tag,
        dataset: {},
        className: '',
        hidden: false,
        textContent: '',
        value: '',
        type: '',
        children: [],
        parentNode: null,
        isConnected: true,
        _listeners: {},
        appendChild(child) {
            this.children.push(child);
            child.parentNode = this;
            return child;
        },
        remove() {
            if (!this.parentNode) {
                return;
            }
            const siblings = this.parentNode.children;
            const index = siblings.indexOf(this);
            if (index !== -1) {
                siblings.splice(index, 1);
            }
            this.parentNode = null;
        },
        addEventListener(type, handler) {
            (this._listeners[type] ||= []).push(handler);
        },
        // Not a real CSS selector engine -- just a recursive lookup by exact
        // class token, which is all these tests need.
        querySelector(className) {
            for (const child of this.children) {
                if (child.className && child.className.split(' ').includes(className)) {
                    return child;
                }
                const nested = child.querySelector(className);
                if (nested) {
                    return nested;
                }
            }
            return null;
        },
    };
}

function fireClick(element) {
    for (const handler of element._listeners.click || []) {
        handler();
    }
}

function buildDomCtx() {
    const ctx = {
        gatewayI18n: { t: (key) => CAPABILITY_I18N_STRINGS[key] ?? key },
        state: { localizedBindings: new Set() },
    };
    registerCore(ctx);
    return ctx;
}

function withFakeDocument(run) {
    const previousDocument = globalThis.document;
    globalThis.document = { createElement: (tag) => createFakeElement(tag) };
    try {
        return run();
    } finally {
        globalThis.document = previousDocument;
    }
}

test('wrapCapabilityField Edit button click unlocks the control and drops the field from capabilities_autofilled on save', () => {
    withFakeDocument(() => {
        const ctx = buildDomCtx();
        const toolsControl = document.createElement('select');
        toolsControl.value = 'true';

        const wrapper = ctx.wrapCapabilityField({
            fieldName: 'supports_tools',
            control: toolsControl,
            kind: 'boolean',
            locked: true,
            source: 'provider',
        });

        assert.equal(toolsControl.dataset.capabilityLocked, 'true');
        assert.equal(toolsControl.hidden, true);

        const editButton = wrapper.querySelector('capability-badge-edit');
        assert.ok(editButton, 'expected the badge to render an Edit button');
        assert.equal(editButton.textContent, 'Изменить');

        // Simulate the user clicking the badge's real "Edit"/"Изменить" button --
        // not hand-setting dataset.capabilityLocked as the tests above do.
        fireClick(editButton);

        assert.equal(toolsControl.dataset.capabilityLocked, 'false', 'control must become editable after Edit is clicked');
        assert.equal(toolsControl.hidden, false);

        const payload = {};
        ctx.applyCapabilityFieldsToPayload(
            payload,
            fakeControl('', false),
            toolsControl,
            fakeControl('', false),
        );

        assert.equal(payload.supports_tools, true);
        assert.ok(
            !('capabilities_autofilled' in payload),
            'field must drop out of capabilities_autofilled once its control was edited via the real click handler',
        );
    });
});

test('wrapCapabilityField renders an autofilled badge without a source qualifier when the status has no source info', () => {
    withFakeDocument(() => {
        const ctx = buildDomCtx();
        const contextControl = document.createElement('input');
        contextControl.value = '32000';

        const wrapper = ctx.wrapCapabilityField({
            fieldName: 'context_window',
            control: contextControl,
            kind: 'number',
            locked: true,
            source: null, // capability-autofill status endpoint had no resolution entry for this field
        });

        assert.equal(contextControl.hidden, true, 'control stays hidden behind the badge');

        const badge = wrapper.querySelector('capability-badge');
        assert.ok(badge, 'expected an autofilled badge to render even without source info');

        const autoLabel = badge.querySelector('capability-badge-auto');
        assert.equal(autoLabel.textContent, 'Заполнено автоматически');

        const sourceLabel = badge.querySelector('capability-badge-source');
        assert.equal(sourceLabel, null, 'badge must omit the source qualifier when no source is known');
    });
});
