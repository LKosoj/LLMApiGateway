import assert from "node:assert/strict";
import test from "node:test";

import {
  escapeHtml,
  formatInteger,
  loadProductVersion,
  showToast,
  toFiniteNumber,
} from "../src/ui-core.mjs";

function makeElement() {
  const classes = new Set();
  return {
    classList: {
      add: (...names) => names.forEach((name) => classes.add(name)),
      contains: (name) => classes.has(name),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      toggle(name, force) {
        if (force) classes.add(name);
        else classes.delete(name);
      },
    },
    dataset: {},
    textContent: "",
  };
}

test("legacy formatting helpers preserve the handwritten ui-core contract", () => {
  assert.equal(escapeHtml(`<a title="x">Tom & Jerry's</a>`), "&lt;a title=&quot;x&quot;&gt;Tom &amp; Jerry&#39;s&lt;/a&gt;");
  assert.equal(toFiniteNumber("4.5"), 4.5);
  assert.equal(toFiniteNumber("not-a-number", 7), 7);
  assert.equal(formatInteger(8.9), "8");
});

test("a stale toast timer cannot hide a newer toast", async () => {
  const element = makeElement();
  showToast(element, "first", { timeoutMs: 1 });
  showToast(element, "second", { isError: true, timeoutMs: 30 });

  await new Promise((resolve) => setTimeout(resolve, 10));

  assert.equal(element.textContent, "second");
  assert.equal(element.classList.contains("visible"), true);
  assert.equal(element.classList.contains("error"), true);
  clearTimeout(element._gatewayToastTimer);
});

test("product version caches health and rerenders from locale state", async () => {
  const element = makeElement();
  const calls = [];
  const listeners = [];
  let locale = "en";
  const version = await loadProductVersion({
    document: { querySelectorAll: () => [element] },
    fetch: async (url, options) => {
      calls.push([url, options]);
      return {
        ok: true,
        headers: new Headers({ "X-LLMGateway-Build-Version": "1.2.3" }),
      };
    },
    subscribe: (listener) => listeners.push(listener),
    translate: (key, values) => `${locale}:${key}:${values.version}`,
  });

  assert.equal(version, "1.2.3");
  assert.equal(element.textContent, "en:common:productVersion.ready:1.2.3");
  assert.equal(element.dataset.productVersionState, "ready");
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/health");

  locale = "ru";
  listeners[0]();
  assert.equal(element.textContent, "ru:common:productVersion.ready:1.2.3");
  assert.equal(calls.length, 1);
});


test("unavailable product version rerenders without a second request", async () => {
  const element = makeElement();
  const listeners = [];
  const events = [];
  const consoleErrors = [];
  let fetchCount = 0;
  let locale = "en";
  const originalConsoleError = console.error;
  console.error = (...args) => consoleErrors.push(args);
  try {
    const version = await loadProductVersion({
      document: { querySelectorAll: () => [element] },
      eventTarget: { dispatchEvent: (event) => events.push(event) },
      fetch: async () => {
        fetchCount += 1;
        return { ok: false, status: 503 };
      },
      subscribe: (listener) => listeners.push(listener),
      translate: (key) => `${locale}:${key}`,
    });

    assert.equal(version, null);
    assert.equal(element.textContent, "en:common:productVersion.unavailable");
    assert.equal(element.dataset.productVersionState, "error");
    assert.equal(fetchCount, 1);
    assert.equal(events.length, 1);
    assert.equal(consoleErrors.length, 1);

    locale = "ru";
    listeners[0]();
    assert.equal(element.textContent, "ru:common:productVersion.unavailable");
    assert.equal(fetchCount, 1);
    assert.equal(events.length, 1);
    assert.equal(consoleErrors.length, 1);
  } finally {
    console.error = originalConsoleError;
  }
});
