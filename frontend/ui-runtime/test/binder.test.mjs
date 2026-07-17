import assert from "node:assert/strict";
import test from "node:test";

import { GatewayI18nError, createAtomicBindingPlan } from "../src/index.mjs";
import { FakeDocument, FakeElement } from "./helpers.mjs";

test("binder precomputes every translation before mutating any node", () => {
  const first = new FakeElement({ "data-i18n": "common:valid" });
  first.textContent = "before";
  const second = new FakeElement({ "data-i18n": "common:missing" });
  second.textContent = "also before";
  const document = new FakeDocument([first, second]);

  assert.throws(
    () => createAtomicBindingPlan(document, (key) => {
      if (key.endsWith("missing")) {
        throw new GatewayI18nError("TRANSLATION_MISSING", "missing");
      }
      return "after";
    }),
    GatewayI18nError,
  );
  assert.equal(first.textContent, "before");
  assert.equal(second.textContent, "also before");
});

test("binder writes only textContent and allowlisted accessibility attributes", () => {
  const element = new FakeElement({
    "data-i18n": "common:text",
    "data-i18n-title": "common:title",
    "data-i18n-placeholder": "common:placeholder",
    "data-i18n-aria-label": "common:label",
    "data-i18n-aria-description": "common:description",
  });
  const raw = new FakeElement({ "data-i18n-raw-detail": "" });
  raw.textContent = "server <detail>";
  const plan = createAtomicBindingPlan(
    new FakeDocument([element, raw]),
    (key) => `<${key}>`,
  );

  plan.apply();

  assert.equal(element.textContent, "<common:text>");
  assert.equal(element.getAttribute("title"), "<common:title>");
  assert.equal(element.getAttribute("placeholder"), "<common:placeholder>");
  assert.equal(element.getAttribute("aria-label"), "<common:label>");
  assert.equal(
    element.getAttribute("aria-description"),
    "<common:description>",
  );
  assert.equal(element.innerHTMLWrites, 0);
  assert.equal(raw.textContent, "server <detail>");
  assert.equal(raw.getAttribute("lang"), "und");
  assert.equal(raw.getAttribute("dir"), "auto");
});

test("binder includes a matching Element root and each descendant once", () => {
  const child = new FakeElement({ "data-i18n-title": "common:title" });
  const root = new FakeElement({ "data-i18n-aria-label": "common:label" }, [child]);
  const calls = [];
  const plan = createAtomicBindingPlan(root, (key) => {
    calls.push(key);
    return `translated ${key}`;
  });

  plan.apply();

  assert.equal(root.getAttribute("aria-label"), "translated common:label");
  assert.equal(child.getAttribute("title"), "translated common:title");
  assert.deepEqual(calls, ["common:label", "common:title"]);
});

test("text binding rejects an Element with children without destroying identity", () => {
  const icon = new FakeElement();
  const root = new FakeElement({ "data-i18n": "common:text" }, [icon]);

  assert.throws(
    () => createAtomicBindingPlan(root, () => "translated"),
    (error) => error instanceof GatewayI18nError
      && error.code === "BIND_TEXT_TARGET_HAS_CHILDREN",
  );
  assert.equal(root.children[0], icon);
});

test("raw detail and translated text conflict before any DOM mutation", () => {
  const ordinary = new FakeElement({ "data-i18n": "common:ordinary" });
  ordinary.textContent = "ordinary before";
  const conflict = new FakeElement({
    "data-i18n": "common:raw",
    "data-i18n-raw-detail": "",
  });
  conflict.textContent = "server detail";

  assert.throws(
    () => createAtomicBindingPlan(
      new FakeDocument([ordinary, conflict]),
      () => "translated",
    ),
    (error) => error instanceof GatewayI18nError
      && error.code === "BIND_RAW_TEXT_CONFLICT",
  );
  assert.equal(ordinary.textContent, "ordinary before");
  assert.equal(conflict.textContent, "server detail");
  assert.equal(conflict.getAttribute("lang"), null);
  assert.equal(conflict.getAttribute("dir"), null);
});
