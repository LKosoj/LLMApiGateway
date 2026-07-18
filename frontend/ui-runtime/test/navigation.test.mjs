import assert from "node:assert/strict";
import test from "node:test";

import {
  NAV_ITEMS,
  createNavigation,
  measureNavigationOverflow,
  normalizePathname,
  visibleNavigationItems,
} from "../src/navigation.mjs";

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(...values) {
    values.forEach((value) => this.values.add(value));
  }

  contains(value) {
    return this.values.has(value);
  }
}

class FakeElement {
  constructor(document, tagName, namespaceURI = null) {
    this.ownerDocument = document;
    this.tagName = tagName.toUpperCase();
    this.namespaceURI = namespaceURI;
    this.attributes = new Map();
    this.children = [];
    this.classList = new FakeClassList();
    this.dataset = {};
    this.hidden = false;
    this.listeners = new Map();
    this.textContent = "";
    this.innerHTMLWrites = 0;
    this.rect = { left: 0, right: 100 };
    this.scrollIntoViewCalls = [];
  }

  append(...children) {
    this.children.push(...children);
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  replaceChildren(...children) {
    this.children = [...children];
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  toggleAttribute(name, force) {
    if (force) this.attributes.set(name, "");
    else this.attributes.delete(name);
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  removeEventListener(type, listener) {
    if (this.listeners.get(type) === listener) this.listeners.delete(type);
  }

  getBoundingClientRect() {
    return this.rect;
  }

  scrollIntoView(options) {
    this.scrollIntoViewCalls.push(options);
  }

  set innerHTML(_value) {
    this.innerHTMLWrites += 1;
  }
}

class FakeDocument {
  constructor() {
    this.created = [];
  }

  createElement(tagName) {
    const element = new FakeElement(this, tagName);
    this.created.push(element);
    return element;
  }

  createElementNS(namespaceURI, tagName) {
    const element = new FakeElement(this, tagName, namespaceURI);
    this.created.push(element);
    return element;
  }
}

function makeRoot() {
  const document = new FakeDocument();
  return { document, root: new FakeElement(document, "nav") };
}

function renderedLinks(root) {
  return root.children[0]?.children ?? [];
}

const LABELS = Object.freeze({
  "common:navigation.label": "Primary navigation",
  "common:navigation.docs": "Docs",
  "common:navigation.usage": "Usage Statistics",
  "common:navigation.quota": "Quota",
  "common:navigation.rules": "Rules Editor",
  "common:navigation.playground": "Playground",
  "common:navigation.apiKeys": "API Keys",
  "common:navigation.rejections": "Rejections",
  "common:navigation.translator": "Translator Debug",
  "common:navigation.pricing": "Pricing",
  "common:navigation.freeModels": "Free LLM Catalog",
});

function translate(key) {
  assert(Object.hasOwn(LABELS, key), `Unexpected translation key: ${key}`);
  return LABELS[key];
}

test("NAV_ITEMS is an immutable ten-route role contract", () => {
  assert.deepEqual(
    NAV_ITEMS.map(({ id, href, labelKey, roles }) => ({ id, href, labelKey, roles })),
    [
      { id: "docs", href: "/v1/ui/docs", labelKey: "common:navigation.docs", roles: ["master", "user"] },
      { id: "usage", href: "/v1/ui/usage-stats", labelKey: "common:navigation.usage", roles: ["master", "user"] },
      { id: "quota", href: "/v1/ui/quota", labelKey: "common:navigation.quota", roles: ["master", "user"] },
      { id: "rules", href: "/v1/ui/rules-editor", labelKey: "common:navigation.rules", roles: ["master"] },
      { id: "playground", href: "/v1/ui/playground", labelKey: "common:navigation.playground", roles: ["master"] },
      { id: "api-keys", href: "/v1/ui/api-keys", labelKey: "common:navigation.apiKeys", roles: ["master"] },
      { id: "rejections", href: "/v1/ui/rejections", labelKey: "common:navigation.rejections", roles: ["master"] },
      { id: "translator", href: "/v1/ui/translator-debug", labelKey: "common:navigation.translator", roles: ["master"] },
      { id: "pricing", href: "/v1/ui/pricing", labelKey: "common:navigation.pricing", roles: ["master"] },
      { id: "free-models", href: "/v1/ui/free-models", labelKey: "common:navigation.freeModels", roles: ["master", "user"] },
    ],
  );
  assert(Object.isFrozen(NAV_ITEMS));
  NAV_ITEMS.forEach((item) => {
    assert(Object.isFrozen(item));
    assert(Object.isFrozen(item.roles));
  });
});

test("path normalization ignores query and trailing slashes", () => {
  assert.equal(normalizePathname("/v1/ui/docs/"), "/v1/ui/docs");
  assert.equal(normalizePathname("https://gateway.invalid/v1/ui/docs/?lang=ru"), "/v1/ui/docs");
  assert.equal(normalizePathname("/"), "/");
  assert.equal(normalizePathname("not a URL"), "");
});

test("navigation role filtering is fail-closed", () => {
  assert.equal(visibleNavigationItems("master").length, 10);
  assert.deepEqual(
    visibleNavigationItems("user").map((item) => item.id),
    ["docs", "usage", "quota", "free-models"],
  );
  assert.deepEqual(visibleNavigationItems("pending"), []);
  assert.deepEqual(visibleNavigationItems("unknown"), []);
  assert.deepEqual(visibleNavigationItems("admin"), []);
});

test("navigation renders safe SVG nodes and exactly one normalized current route", () => {
  const { document, root } = makeRoot();
  const controller = createNavigation(root, {
    role: "master",
    pathname: "/v1/ui/api-keys/?dialog=create",
    translate,
  });
  const links = renderedLinks(root);

  assert.equal(root.hidden, false);
  assert.equal(links.length, 10);
  assert.equal(links.filter((link) => link.getAttribute("aria-current") === "page").length, 1);
  assert.equal(links[5].getAttribute("aria-current"), "page");
  assert.equal(links[5].classList.contains("active"), true);
  assert.equal(links[5].children[1].textContent, "API Keys");
  assert.deepEqual(links[5].scrollIntoViewCalls, [{ block: "nearest", inline: "nearest" }]);
  assert.equal(document.created.some((element) => element.namespaceURI?.includes("svg")), true);
  assert.equal(document.created.reduce((total, element) => total + element.innerHTMLWrites, 0), 0);
  assert.equal(document.created.some((element) => ["BUTTON", "SELECT"].includes(element.tagName)), false);

  controller.destroy();
});

test("recognized roles require an explicit translator", () => {
  const { root } = makeRoot();
  assert.throws(
    () => createNavigation(root, { role: "user", pathname: "/v1/ui/docs" }),
    /translator is required/,
  );
});

test("navigation remains hidden until a recognized role is supplied", () => {
  const { root } = makeRoot();
  const controller = createNavigation(root, {
    role: "pending",
    pathname: "/v1/ui/docs",
    translate,
  });
  assert.equal(root.hidden, true);
  assert.deepEqual(renderedLinks(root), []);

  controller.update({ role: "user" });
  assert.equal(root.hidden, false);
  assert.equal(renderedLinks(root).length, 4);

  controller.update({ role: "unknown" });
  assert.equal(root.hidden, true);
  assert.deepEqual(renderedLinks(root), []);
  controller.destroy();
});

test("overflow measurement follows logical inline edges in LTR and RTL", () => {
  const scroller = { getBoundingClientRect: () => ({ left: 10, right: 110 }) };
  const first = { getBoundingClientRect: () => ({ left: 0, right: 40 }) };
  const last = { getBoundingClientRect: () => ({ left: 90, right: 130 }) };

  assert.deepEqual(measureNavigationOverflow(scroller, [first, last], "ltr"), {
    start: true,
    end: true,
  });
  assert.deepEqual(measureNavigationOverflow(scroller, [last, first], "rtl"), {
    start: true,
    end: true,
  });

  const visibleFirst = { getBoundingClientRect: () => ({ left: 10, right: 40 }) };
  const visibleLast = { getBoundingClientRect: () => ({ left: 80, right: 110 }) };
  assert.deepEqual(measureNavigationOverflow(scroller, [visibleFirst, visibleLast], "ltr"), {
    start: false,
    end: false,
  });
  assert.deepEqual(measureNavigationOverflow(scroller, [visibleLast, visibleFirst], "rtl"), {
    start: false,
    end: false,
  });
});

test("scroll refresh exposes geometry-based overflow cues", () => {
  const { root } = makeRoot();
  const controller = createNavigation(root, {
    role: "user",
    pathname: "/v1/ui/docs",
    translate,
  });
  const scroller = root.children[0];
  const links = renderedLinks(root);
  scroller.rect = { left: 10, right: 110 };
  links[0].rect = { left: 0, right: 40 };
  links[3].rect = { left: 90, right: 130 };

  controller.refreshOverflow();
  assert.equal(root.attributes.has("data-overflow-start"), true);
  assert.equal(root.attributes.has("data-overflow-end"), true);

  scroller.listeners.get("scroll")();
  assert.equal(root.attributes.has("data-overflow-start"), true);
  controller.destroy();
  assert.equal(scroller.listeners.has("scroll"), false);
});
