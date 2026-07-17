import assert from "node:assert/strict";
import test from "node:test";

import { createStatus } from "../src/status.mjs";

class ClassListFake {
  constructor() {
    this.values = new Set();
  }

  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  toggle(value, force) {
    if (force) this.values.add(value);
    else this.values.delete(value);
  }
  contains(value) { return this.values.has(value); }
}

class ElementFake {
  constructor() {
    this.attributes = new Map();
    this.classList = new ClassListFake();
    this.children = [];
    this.operations = [];
    this._textContent = "";
  }

  append(child) { this.children.push(child); }
  contains(candidate) {
    return candidate === this || this.children.some((child) => child.contains(candidate));
  }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  hasAttribute(name) { return this.attributes.has(name); }
  setAttribute(name, value) {
    this.operations.push(["attribute", name, String(value)]);
    this.attributes.set(name, String(value));
  }
  removeAttribute(name) { this.attributes.delete(name); }
  get textContent() { return this._textContent; }
  set textContent(value) {
    this.operations.push(["text", String(value)]);
    this._textContent = String(value);
  }
}

function assertSemanticsPrecedeText(element) {
  const textIndex = element.operations.findIndex((operation) => operation[0] === "text");
  assert.notEqual(textIndex, -1);
  for (const attribute of ["role", "aria-live", "aria-atomic", "aria-busy"]) {
    const attributeIndex = element.operations.findIndex(
      (operation) => operation[0] === "attribute" && operation[1] === attribute,
    );
    assert.notEqual(attributeIndex, -1);
    assert.ok(attributeIndex < textIndex, `${attribute} must be applied before text`);
  }
}

test("status exposes busy, polite, assertive, and error semantics", () => {
  const element = new ElementFake();
  const status = createStatus(element, {
    renderMessage: (message, locale) => `${locale}:${message.key}`,
  });

  status.busy({ key: "loading" }, { localeContext: "en" });
  assert.equal(element.textContent, "en:loading");
  assert.equal(element.getAttribute("role"), "status");
  assert.equal(element.getAttribute("aria-live"), "polite");
  assert.equal(element.getAttribute("aria-busy"), "true");
  assertSemanticsPrecedeText(element);

  element.operations = [];
  status.polite({ key: "ready" }, { localeContext: "en" });
  assert.equal(element.getAttribute("role"), "status");
  assert.equal(element.getAttribute("aria-busy"), "false");
  assertSemanticsPrecedeText(element);

  element.operations = [];
  status.assertive({ key: "attention" }, { localeContext: "en" });
  assert.equal(element.getAttribute("role"), "alert");
  assert.equal(element.getAttribute("aria-live"), "assertive");
  assertSemanticsPrecedeText(element);

  element.operations = [];
  status.error({ key: "failed" }, { localeContext: "en" });
  assert.equal(element.getAttribute("data-status-kind"), "error");
  assert.equal(element.classList.contains("error"), true);
  assertSemanticsPrecedeText(element);
});

test("rerender keeps message data and applies a new locale context", () => {
  const element = new ElementFake();
  const calls = [];
  const message = Object.freeze({ key: "saved", values: { count: 2 } });
  const status = createStatus(element, {
    renderMessage: (data, locale) => {
      calls.push([data, locale]);
      return `${locale}:${data.key}:${data.values.count}`;
    },
  });

  status.polite(message, { localeContext: "en" });
  status.rerender("ru");

  assert.equal(element.textContent, "ru:saved:2");
  assert.equal(status.state.message, message);
  assert.deepEqual(calls, [[message, "en"], [message, "ru"]]);
});

test("raw detail is isolated from the live region and marked as untranslated", () => {
  const element = new ElementFake();
  const rawDetailElement = new ElementFake();
  rawDetailElement.setAttribute("role", "alert");
  rawDetailElement.setAttribute("aria-live", "assertive");
  const status = createStatus(element, { rawDetailElement });

  status.error("Request failed", { rawDetail: "upstream <trace>" });

  assert.equal(element.textContent, "Request failed");
  assert.equal(element.getAttribute("aria-live"), "assertive");
  assert.equal(rawDetailElement.textContent, "upstream <trace>");
  assert.equal(rawDetailElement.getAttribute("role"), null);
  assert.equal(rawDetailElement.getAttribute("aria-live"), null);
  assert.equal(rawDetailElement.getAttribute("lang"), "und");
  assert.equal(rawDetailElement.getAttribute("dir"), "auto");

  const withoutRawSink = createStatus(new ElementFake());
  assert.throws(
    () => withoutRawSink.error("Failed", { rawDetail: "large response" }),
    /rawDetailElement/,
  );
});

test("raw detail sink must be structurally separate from the live region", () => {
  const same = new ElementFake();
  assert.throws(() => createStatus(same, { rawDetailElement: same }), /separate/);

  const statusParent = new ElementFake();
  const nestedRaw = new ElementFake();
  statusParent.append(nestedRaw);
  assert.throws(
    () => createStatus(statusParent, { rawDetailElement: nestedRaw }),
    /separate/,
  );

  const rawParent = new ElementFake();
  const nestedStatus = new ElementFake();
  rawParent.append(nestedStatus);
  assert.throws(
    () => createStatus(nestedStatus, { rawDetailElement: rawParent }),
    /separate/,
  );
});

test("an expired timer cannot clear a newer status", () => {
  const element = new ElementFake();
  const callbacks = [];
  const status = createStatus(element, {
    setTimeout: (callback) => {
      callbacks.push(callback);
      return callbacks.length;
    },
    clearTimeout: () => {},
  });

  status.polite("First", { timeoutMs: 100 });
  status.error("Second");
  callbacks[0]();

  assert.equal(element.textContent, "Second");
  assert.equal(status.state.kind, "error");
});

test("clear and destroy remove controller-owned status state", () => {
  const element = new ElementFake();
  const rawDetailElement = new ElementFake();
  const status = createStatus(element, { rawDetailElement });

  status.busy("Loading", { rawDetail: "request 42" });
  status.clear();
  assert.equal(element.textContent, "");
  assert.equal(element.getAttribute("role"), null);
  assert.equal(rawDetailElement.textContent, "");

  status.destroy();
  assert.equal(status.state, null);
  assert.throws(() => status.polite("Later"), /destroyed/);
});
