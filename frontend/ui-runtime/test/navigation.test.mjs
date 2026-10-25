import assert from "node:assert/strict";
import test from "node:test";

import {
  BOTTOM_NAV_ITEMS,
  NAV_ITEMS,
  createBottomNavigation,
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

  remove(...values) {
    values.forEach((value) => this.values.delete(value));
  }

  contains(value) {
    return this.values.has(value);
  }

  toggle(value, force) {
    const shouldHave = force === undefined ? !this.values.has(value) : Boolean(force);
    if (shouldHave) this.values.add(value);
    else this.values.delete(value);
    return shouldHave;
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
    this.focusCalls = 0;
  }

  focus() {
    this.focusCalls += 1;
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
  "common:navigation.live": "Live now",
  "common:navigation.failures": "Model failures",
  "common:navigation.myKey": "My key",
  "common:navigation.groups.monitoring": "Monitoring",
  "common:navigation.groups.settings": "Settings",
  "common:navigation.groups.tools": "Tools",
  "common:navigation.groups.help": "Help",
  "common:navigation.overview": "Overview",
  "common:navigation.activity": "Activity",
  "common:navigation.docs": "Docs",
  "common:navigation.usage": "Analytics (legacy)",
  "common:navigation.quota": "Quota",
  "common:navigation.rules": "Rules Editor",
  "common:navigation.playground": "Playground",
  "common:navigation.apiKeys": "API Keys",
  "common:navigation.rejections": "Rejections",
  "common:navigation.translator": "Translator Check",
  "common:navigation.pricing": "Pricing",
  "common:navigation.freeModels": "Free LLM Catalog",
  "common:navigation.bottomLabel": "Quick sections",
  "common:navigation.short.live": "Live",
  "common:navigation.short.failures": "Failures",
  "common:navigation.short.myKey": "My key",
  "common:navigation.short.activity": "Activity",
  "common:navigation.short.docs": "Docs",
  "common:navigation.short.menu": "Menu",
});

function translate(key) {
  assert(Object.hasOwn(LABELS, key), `Unexpected translation key: ${key}`);
  return LABELS[key];
}

test("NAV_ITEMS is an immutable grouped fifteen-route role contract", () => {
  assert.deepEqual(
    NAV_ITEMS.map(({ id, href, labelKey, group, roles }) => ({ id, href, labelKey, group, roles })),
    [
      { id: "live", href: "/v1/ui/live", labelKey: "common:navigation.live", group: "monitoring", roles: ["master"] },
      { id: "failures", href: "/v1/ui/failures", labelKey: "common:navigation.failures", group: "monitoring", roles: ["master"] },
      { id: "my-key", href: "/v1/ui/my-key", labelKey: "common:navigation.myKey", group: "monitoring", roles: ["user"] },
      { id: "overview", href: "/v1/ui/overview", labelKey: "common:navigation.overview", group: "monitoring", roles: ["master", "user"] },
      { id: "activity", href: "/v1/ui/activity", labelKey: "common:navigation.activity", group: "monitoring", roles: ["master", "user"] },
      { id: "usage", href: "/v1/ui/usage-stats", labelKey: "common:navigation.usage", group: "monitoring", roles: ["master"] },
      { id: "quota", href: "/v1/ui/quota", labelKey: "common:navigation.quota", group: "monitoring", roles: ["master", "user"] },
      { id: "rejections", href: "/v1/ui/rejections", labelKey: "common:navigation.rejections", group: "monitoring", roles: ["master"] },
      { id: "rules", href: "/v1/ui/rules-editor", labelKey: "common:navigation.rules", group: "settings", roles: ["master"] },
      { id: "api-keys", href: "/v1/ui/api-keys", labelKey: "common:navigation.apiKeys", group: "settings", roles: ["master"] },
      { id: "pricing", href: "/v1/ui/pricing", labelKey: "common:navigation.pricing", group: "settings", roles: ["master"] },
      { id: "playground", href: "/v1/ui/playground", labelKey: "common:navigation.playground", group: "tools", roles: ["master"] },
      { id: "translator", href: "/v1/ui/translator-debug", labelKey: "common:navigation.translator", group: "tools", roles: ["master"] },
      { id: "free-models", href: "/v1/ui/free-models", labelKey: "common:navigation.freeModels", group: "tools", roles: ["master", "user"] },
      { id: "docs", href: "/v1/ui/docs", labelKey: "common:navigation.docs", group: "help", roles: ["master", "user"] },
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
  assert.equal(visibleNavigationItems("master").length, 14);
  assert.deepEqual(
    visibleNavigationItems("user").map((item) => item.id),
    ["my-key", "overview", "activity", "quota", "free-models", "docs"],
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
  assert.equal(links.length, 14);
  assert.equal(links.every((link) => link.tagName === "A"), true);
  assert.equal(links.filter((link) => link.getAttribute("aria-current") === "page").length, 1);
  assert.equal(links[8].getAttribute("aria-current"), "page");
  assert.equal(links[8].classList.contains("active"), true);
  assert.equal(links[8].children[1].textContent, "API Keys");
  assert.deepEqual(links[8].scrollIntoViewCalls, [{ block: "nearest", inline: "nearest" }]);
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
  assert.equal(renderedLinks(root).length, 6);

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
  links[links.length - 1].rect = { left: 90, right: 130 };

  controller.refreshOverflow();
  assert.equal(root.attributes.has("data-overflow-start"), true);
  assert.equal(root.attributes.has("data-overflow-end"), true);

  scroller.listeners.get("scroll")();
  assert.equal(root.attributes.has("data-overflow-start"), true);
  controller.destroy();
  assert.equal(scroller.listeners.has("scroll"), false);
});

test('layout "list" renders links directly under root without a scroller wrapper', () => {
  const { root } = makeRoot();
  const controller = createNavigation(root, {
    role: "user",
    pathname: "/v1/ui/quota",
    layout: "list",
    translate,
  });

  assert.equal(root.classList.contains("gateway-nav-list"), true);
  const links = root.children.filter((child) => child.tagName === "A");
  assert.deepEqual(
    links.map((child) => child.getAttribute("data-nav-id")),
    ["my-key", "overview", "activity", "quota", "free-models", "docs"],
  );
  assert.equal(links[3].classList.contains("active"), true);
  assert.equal(links[3].getAttribute("aria-current"), "page");
  assert.equal(root.attributes.has("data-overflow-start"), false);
  assert.equal(root.attributes.has("data-overflow-end"), false);

  controller.destroy();
});

test('layout "list" renders one translated group label before each group of links', () => {
  const { root } = makeRoot();
  const controller = createNavigation(root, {
    role: "user",
    pathname: "/v1/ui/my-key",
    layout: "list",
    translate,
  });

  assert.deepEqual(
    root.children.map((child) => child.getAttribute("data-nav-group") ?? child.getAttribute("data-nav-id")),
    ["monitoring", "my-key", "overview", "activity", "quota", "tools", "free-models", "help", "docs"],
  );
  const groupLabels = root.children.filter((child) => child.tagName === "DIV");
  assert.deepEqual(groupLabels.map((label) => label.textContent), ["Monitoring", "Tools", "Help"]);
  assert.equal(groupLabels.every((label) => label.classList.contains("gateway-nav-group")), true);

  controller.destroy();
});

test('layout "scroller" is the unmodified default (no gateway-nav-list class)', () => {
  const { root } = makeRoot();
  const controller = createNavigation(root, {
    role: "user",
    pathname: "/v1/ui/docs",
    translate,
  });

  assert.equal(root.classList.contains("gateway-nav-list"), false);
  assert.equal(root.children[0].classList.contains("gateway-nav-scroller"), true);

  controller.destroy();
});

test('layout "list" supports ArrowUp/ArrowDown roving focus with wraparound', () => {
  const { root } = makeRoot();
  const controller = createNavigation(root, {
    role: "master",
    pathname: "/v1/ui/docs",
    layout: "list",
    translate,
  });
  const links = root.children.filter((child) => child.tagName === "A");
  const keydown = root.listeners.get("keydown");
  assert.equal(typeof keydown, "function");

  function press(key, target) {
    let prevented = false;
    keydown({ key, target, preventDefault: () => { prevented = true; } });
    return prevented;
  }

  assert.equal(press("ArrowDown", links[0]), true);
  assert.equal(links[1].focusCalls, 1);

  assert.equal(press("ArrowDown", links[links.length - 1]), true);
  assert.equal(links[0].focusCalls, 1);

  assert.equal(press("ArrowUp", links[0]), true);
  assert.equal(links[links.length - 1].focusCalls, 1);

  // Unrelated keys and unknown targets are ignored.
  assert.equal(press("Tab", links[0]), false);
  assert.equal(press("ArrowDown", { not: "a link" }), false);

  controller.destroy();
});

test('layout "list" supports Home/End roving focus', () => {
  const { root } = makeRoot();
  const controller = createNavigation(root, {
    role: "master",
    pathname: "/v1/ui/docs",
    layout: "list",
    translate,
  });
  const links = root.children.filter((child) => child.tagName === "A");
  const keydown = root.listeners.get("keydown");

  keydown({ key: "End", target: links[2], preventDefault: () => {} });
  assert.equal(links[links.length - 1].focusCalls, 1);

  keydown({ key: "Home", target: links[2], preventDefault: () => {} });
  assert.equal(links[0].focusCalls, 1);

  controller.destroy();
  assert.equal(root.listeners.has("keydown"), false);
});

test("bottom navigation shows three role sections plus a menu button", () => {
  const { root } = makeRoot();
  let menuOpened = 0;
  const controller = createBottomNavigation(root, {
    role: "master",
    pathname: "/v1/ui/failures/",
    translate,
    onMenu: () => {
      menuOpened += 1;
    },
  });

  assert.equal(root.hidden, false);
  assert.equal(root.getAttribute("aria-label"), "Quick sections");
  const entries = root.children;
  assert.deepEqual(entries.map((entry) => entry.tagName), ["A", "A", "A", "BUTTON"]);
  assert.deepEqual(entries.slice(0, 3).map((link) => link.getAttribute("data-nav-id")), ["live", "failures", "activity"]);
  assert.deepEqual(entries.map((entry) => entry.children[1].textContent), ["Live", "Failures", "Activity", "Menu"]);
  assert.equal(entries.every((entry) => entry.classList.contains("gateway-bottom-nav-link")), true);
  assert.equal(entries.some((entry) => entry.classList.contains("nav-button")), false);
  assert.deepEqual(entries.map((entry) => entry.getAttribute("aria-current")), [null, "page", null, null]);

  entries[3].listeners.get("click")();
  assert.equal(menuOpened, 1);

  controller.update({ role: "user", pathname: "/v1/ui/my-key" });
  assert.deepEqual(root.children.slice(0, 3).map((link) => link.getAttribute("href")), [
    "/v1/ui/my-key",
    "/v1/ui/activity",
    "/v1/ui/docs",
  ]);
  assert.equal(root.children[0].getAttribute("aria-current"), "page");
});

test("bottom navigation items exist in NAV_ITEMS for the same role", () => {
  for (const [role, ids] of Object.entries(BOTTOM_NAV_ITEMS)) {
    const visible = visibleNavigationItems(role).map((item) => item.id);
    ids.forEach((id) => assert.equal(visible.includes(id), true, `${role}: ${id}`));
  }
});

test("bottom navigation stays hidden for an unknown role", () => {
  const { root } = makeRoot();
  createBottomNavigation(root, { role: "pending", translate });
  assert.equal(root.hidden, true);
  assert.deepEqual(root.children, []);
});
