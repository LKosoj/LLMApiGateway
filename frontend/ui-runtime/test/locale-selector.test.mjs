import assert from "node:assert/strict";
import test from "node:test";

import { createLocaleSelector } from "../src/locale-selector.mjs";

class SelectFake {
  constructor() {
    this.children = [];
    this.dataset = {};
    this.disabled = false;
    this.listeners = new Map();
    this.value = "";
    this.ownerDocument = {
      createDocumentFragment: () => ({ children: [], appendChild(child) { this.children.push(child); } }),
      createElement: () => ({ textContent: "", value: "" }),
    };
  }

  addEventListener(type, listener) { this.listeners.set(type, listener); }
  removeEventListener(type, listener) {
    if (this.listeners.get(type) === listener) this.listeners.delete(type);
  }
  replaceChildren(fragment) { this.children = fragment.children; }
}

function makeRuntime(changeLanguage) {
  return {
    ready: Promise.resolve(),
    currentLocale: "en",
    registry: {
      locales: [
        { code: "en", nativeLabel: "English", dir: "ltr" },
        { code: "ru", nativeLabel: "Русский", dir: "ltr" },
      ],
    },
    async changeLanguage(locale) {
      await changeLanguage?.(locale);
      this.currentLocale = locale;
    },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

test("locale selector renders the complete registry and changes language", async () => {
  const select = new SelectFake();
  const runtime = makeRuntime();
  const controller = createLocaleSelector(select, { i18n: runtime, onError: assert.fail });
  await controller.ready;

  assert.deepEqual(select.children.map(({ value, textContent }) => [value, textContent]), [
    ["en", "English"],
    ["ru", "Русский"],
  ]);
  assert.equal(select.value, "en");

  select.value = "ru";
  select.listeners.get("change")();
  await controller.transition;

  assert.equal(runtime.currentLocale, "ru");
  assert.equal(select.value, "ru");
  assert.equal(select.dataset.localeState, "ready");
  assert.equal(select.disabled, false);
});

test("locale change failures are explicit and preserve the committed locale", async () => {
  const select = new SelectFake();
  const failure = new Error("catalog unavailable");
  const errors = [];
  const runtime = makeRuntime(async () => { throw failure; });
  const controller = createLocaleSelector(select, {
    i18n: runtime,
    onError: (error) => errors.push(error),
  });
  await controller.ready;

  await assert.rejects(controller.changeLanguage("ru"), /catalog unavailable/);
  assert.deepEqual(errors, [failure]);
  assert.equal(select.value, "en");
  assert.equal(select.dataset.localeState, "error");
});

test("event-driven failures remain observable through transition", async () => {
  const select = new SelectFake();
  const runtime = makeRuntime(async () => { throw new Error("catalog unavailable"); });
  const controller = createLocaleSelector(select, { i18n: runtime, onError: () => {} });
  await controller.ready;

  select.value = "ru";
  select.listeners.get("change")();

  await assert.rejects(controller.transition, /catalog unavailable/);
  assert.equal(select.dataset.localeState, "error");
});

test("overlapping changes keep the selector busy until every operation settles", async () => {
  const select = new SelectFake();
  const first = deferred();
  const second = deferred();
  const calls = [];
  const runtime = makeRuntime((locale) => {
    calls.push(locale);
    return calls.length === 1 ? first.promise : second.promise;
  });
  const controller = createLocaleSelector(select, { i18n: runtime, onError: assert.fail });
  await controller.ready;

  const firstChange = controller.changeLanguage("ru");
  const secondChange = controller.changeLanguage("en");
  first.resolve();
  await firstChange;
  assert.equal(select.disabled, true);
  assert.equal(select.dataset.localeState, "changing");

  second.resolve();
  await secondChange;
  assert.equal(select.disabled, false);
  assert.equal(select.dataset.localeState, "ready");
  assert.equal(select.value, "en");
});

test("destroy invalidates an in-flight change and restores owned DOM state", async () => {
  const select = new SelectFake();
  select.dataset.localeState = "initial";
  const pending = deferred();
  const runtime = makeRuntime(() => pending.promise);
  const controller = createLocaleSelector(select, { i18n: runtime, onError: assert.fail });
  await controller.ready;

  const change = controller.changeLanguage("ru");
  controller.destroy();
  assert.equal(select.disabled, false);
  assert.equal(select.dataset.localeState, "initial");

  pending.resolve();
  await change;
  assert.equal(select.value, "en");
  assert.equal(select.dataset.localeState, "initial");
});

test("locale selector requires an explicit failure handler", () => {
  assert.throws(
    () => createLocaleSelector(new SelectFake(), { i18n: makeRuntime() }),
    /error handler is required/,
  );
});
