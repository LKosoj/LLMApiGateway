import assert from "node:assert/strict";
import test from "node:test";

import { createTabs } from "../src/tabs.mjs";

class TestEvent {
  constructor(type, options = {}) {
    Object.assign(this, options);
    this.type = type;
    this.defaultPrevented = false;
  }

  preventDefault() {
    this.defaultPrevented = true;
  }
}

class TestDocument {
  constructor(direction = "ltr") {
    this.activeElement = null;
    this.observers = new Set();
    this.documentElement = { dir: direction, getAttribute: (name) => (
      name === "dir" ? direction : null
    ) };
    const document = this;
    this.defaultView = {
      getComputedStyle: () => ({ direction }),
      MutationObserver: class {
        constructor(callback) {
          this.callback = callback;
          this.observation = null;
          this.queued = false;
          document.observers.add(this);
        }

        observe(target, options) {
          this.observation = { options, target };
        }

        disconnect() {
          this.observation = null;
          document.observers.delete(this);
        }
      },
    };
    this.elements = [];
  }

  register(element) {
    element.ownerDocument = this;
    this.elements.push(element);
    for (const child of element.children) this.register(child);
  }

  getElementById(id) {
    return this.elements.find((element) => element.id === id) ?? null;
  }

  querySelectorAll(selector) {
    if (selector !== "[id]") throw new TypeError(`Unsupported document selector: ${selector}`);
    return this.elements.filter((element) => element.id);
  }

  notifyMutation(target, attributeName) {
    for (const observer of this.observers) {
      const observed = observer.observation;
      if (!observed) continue;
      if (target !== observed.target
          && (!observed.options.subtree || !observed.target.contains(target))) continue;
      if (observed.options.attributeFilter
          && !observed.options.attributeFilter.includes(attributeName)) continue;
      if (observer.queued) continue;
      observer.queued = true;
      queueMicrotask(() => {
        observer.queued = false;
        if (observer.observation) observer.callback();
      });
    }
  }
}

class TestElement {
  constructor(role = null) {
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.ownerDocument = null;
    this._hidden = false;
    this._disabled = false;
    this._inert = false;
    this.scrollCalls = [];
    this.listeners = new Map();
    if (role) this.setAttribute("role", role);
  }

  get id() {
    return this.getAttribute("id") ?? "";
  }

  set id(value) {
    this.setAttribute("id", value);
  }

  get hidden() {
    return this._hidden;
  }

  set hidden(value) {
    this._hidden = Boolean(value);
    this.ownerDocument?.notifyMutation(this, "hidden");
  }

  get disabled() {
    return this._disabled;
  }

  set disabled(value) {
    this._disabled = Boolean(value);
    this.ownerDocument?.notifyMutation(this, "disabled");
  }

  get inert() {
    return this._inert;
  }

  set inert(value) {
    this._inert = Boolean(value);
    this.ownerDocument?.notifyMutation(this, "inert");
  }

  get dataset() {
    const values = {};
    for (const [name, value] of this.attributes) {
      if (!name.startsWith("data-")) continue;
      const key = name.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      values[key] = value;
    }
    return values;
  }

  append(...children) {
    for (const child of children) {
      child.parentElement = this;
      child.ownerDocument = this.ownerDocument;
      this.children.push(child);
    }
  }

  contains(candidate) {
    if (candidate === this) return true;
    return this.children.some((child) => child.contains(candidate));
  }

  closest(selector) {
    let node = this;
    while (node) {
      if (selector === '[role="tablist"]' && node.getAttribute("role") === "tablist") {
        return node;
      }
      node = node.parentElement;
    }
    return null;
  }

  querySelectorAll(selector) {
    const role = selector.match(/^\[role="([^"]+)"\]$/)?.[1];
    const result = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (role && child.getAttribute("role") === role) result.push(child);
        visit(child);
      }
    };
    visit(this);
    return result;
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    this.ownerDocument?.notifyMutation(this, name);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
    this.ownerDocument?.notifyMutation(this, name);
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }

  removeEventListener(type, listener) {
    this.listeners.get(type)?.delete(listener);
  }

  dispatch(type, options = {}, target = this) {
    const event = new TestEvent(type, { ...options, target });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
    return event;
  }

  focus() {
    this.ownerDocument.activeElement = this;
  }

  scrollIntoView(options) {
    this.scrollCalls.push(options);
  }
}

function createFixture({ direction = "ltr", count = 4, nested = false } = {}) {
  const document = new TestDocument(direction);
  const container = new TestElement();
  const tablist = new TestElement("tablist");
  const tabs = Array.from({ length: count }, (_value, index) => {
    const tab = new TestElement("tab");
    tab.setAttribute("data-tab-key", `tab-${index + 1}`);
    return tab;
  });
  const panels = Array.from({ length: count }, () => new TestElement("tabpanel"));
  tablist.append(...tabs);
  container.append(tablist, ...panels);

  let nestedTab = null;
  if (nested) {
    const nestedList = new TestElement("tablist");
    nestedTab = new TestElement("tab");
    nestedList.append(nestedTab);
    tablist.append(nestedList);
  }

  document.register(container);
  return { document, tablist, tabs, panels, nestedTab };
}

function selectedKeys(tabs) {
  return tabs
    .filter((tab) => tab.getAttribute("aria-selected") === "true")
    .map((tab) => tab.dataset.tabKey);
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

test("createTabs builds a stable ARIA graph and one roving tab stop", () => {
  const { tablist, tabs, panels } = createFixture();
  tabs[1].id = "existing-tab";
  panels[1].id = "existing-panel";

  const controller = createTabs(tablist, {
    initialKey: "tab-2",
    panels,
  });

  assert.equal(tablist.id, "gateway-tabs-1");
  assert.equal(controller.activeKey, "tab-2");
  assert.deepEqual(selectedKeys(tabs), ["tab-2"]);
  assert.deepEqual(tabs.map((tab) => tab.getAttribute("tabindex")), ["-1", "0", "-1", "-1"]);
  assert.equal(tabs[1].id, "existing-tab");
  assert.equal(panels[1].id, "existing-panel");
  for (let index = 0; index < tabs.length; index += 1) {
    assert.equal(tabs[index].getAttribute("aria-controls"), panels[index].id);
    assert.equal(panels[index].getAttribute("aria-labelledby"), tabs[index].id);
    assert.equal(panels[index].getAttribute("role"), "tabpanel");
    assert.equal(panels[index].hidden, index !== 1);
  }
  assert.deepEqual(tabs[1].scrollCalls.at(-1), { block: "nearest", inline: "nearest" });
});

test("generated IDs avoid document collisions and colliding existing IDs are rejected", () => {
  const generated = createFixture({ count: 1 });
  generated.tablist.id = "fixed-tabs";
  const tabCollision = new TestElement();
  tabCollision.id = "fixed-tabs-tab-1";
  const panelCollision = new TestElement();
  panelCollision.id = "fixed-tabs-panel-1";
  generated.document.register(tabCollision);
  generated.document.register(panelCollision);

  createTabs(generated.tablist, { panels: generated.panels });
  assert.equal(generated.tabs[0].id, "fixed-tabs-tab-1-2");
  assert.equal(generated.panels[0].id, "fixed-tabs-panel-1-2");

  const existing = createFixture({ count: 1 });
  existing.tablist.id = "another-tabs";
  existing.tabs[0].id = "duplicate-id";
  const duplicate = new TestElement();
  duplicate.id = "duplicate-id";
  duplicate.ownerDocument = existing.document;
  existing.document.elements.push(duplicate);
  assert.throws(
    () => createTabs(existing.tablist, { panels: existing.panels }),
    /already exists in the document/,
  );
});

test("manual keyboard navigation is isolated, modifier-safe and skips unavailable tabs", async () => {
  const { document, tablist, tabs, panels, nestedTab } = createFixture({ nested: true });
  tabs[1].hidden = true;
  tabs[2].disabled = true;
  const controller = createTabs(tablist, { panels });

  tabs[0].focus();
  const right = tablist.dispatch("keydown", { key: "ArrowRight" }, tabs[0]);
  assert.equal(right.defaultPrevented, true);
  assert.equal(document.activeElement, tabs[3]);
  assert.equal(controller.activeKey, "tab-1", "arrows must not activate in manual mode");
  assert.deepEqual(tabs.map((tab) => tab.getAttribute("tabindex")), ["-1", "-1", "-1", "0"]);

  tablist.dispatch("keydown", { key: "Home" }, tabs[3]);
  assert.equal(document.activeElement, tabs[0]);
  tablist.dispatch("keydown", { key: "End" }, tabs[0]);
  assert.equal(document.activeElement, tabs[3]);

  const modified = tablist.dispatch("keydown", { ctrlKey: true, key: "Home" }, tabs[3]);
  assert.equal(modified.defaultPrevented, false);
  assert.equal(document.activeElement, tabs[3]);

  tablist.dispatch("keydown", { key: "Enter" }, tabs[3]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controller.activeKey, "tab-4");

  tablist.dispatch("keydown", { key: "ArrowLeft" }, nestedTab);
  assert.equal(document.activeElement, tabs[3], "nested tabs must not reach the parent controller");
});

test("horizontal arrow direction follows LTR and RTL", () => {
  const ltr = createFixture({ direction: "ltr", count: 3 });
  createTabs(ltr.tablist, { panels: ltr.panels });
  ltr.tabs[0].focus();
  ltr.tablist.dispatch("keydown", { key: "ArrowRight" }, ltr.tabs[0]);
  assert.equal(ltr.document.activeElement, ltr.tabs[1]);

  const rtl = createFixture({ direction: "rtl", count: 3 });
  createTabs(rtl.tablist, { panels: rtl.panels });
  rtl.tabs[0].focus();
  rtl.tablist.dispatch("keydown", { key: "ArrowRight" }, rtl.tabs[0]);
  assert.equal(rtl.document.activeElement, rtl.tabs[2]);
  rtl.tablist.dispatch("keydown", { key: "ArrowLeft" }, rtl.tabs[2]);
  assert.equal(rtl.document.activeElement, rtl.tabs[0]);
});

test("click and Space activate the focused owned tab", async () => {
  const { tablist, tabs, panels } = createFixture({ count: 3 });
  const controller = createTabs(tablist, { panels });

  tablist.dispatch("click", {}, tabs[1]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controller.activeKey, "tab-2");

  tabs[2].focus();
  const space = tablist.dispatch("keydown", { key: " " }, tabs[2]);
  assert.equal(space.defaultPrevented, true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controller.activeKey, "tab-3");
});

test("repair deterministically chooses the next tab, then the previous tab", async () => {
  const { tablist, tabs, panels } = createFixture();
  const controller = createTabs(tablist, { initialKey: "tab-2", panels });

  tabs[1].hidden = true;
  assert.equal(await controller.repair(), true);
  assert.equal(controller.activeKey, "tab-3");

  tabs[2].hidden = true;
  tabs[3].inert = true;
  assert.equal(await controller.repair(), true);
  assert.equal(controller.activeKey, "tab-1");
  assert.deepEqual(selectedKeys(tabs), ["tab-1"]);
});

test("MutationObserver forces hidden-active repair past a page veto", async () => {
  const { tablist, tabs, panels } = createFixture({ count: 3 });
  const beforeReasons = [];
  const activatedReasons = [];
  const controller = createTabs(tablist, {
    initialKey: "tab-2",
    panels,
    beforeActivate(context) {
      beforeReasons.push(context.reason);
      return false;
    },
    onActivate(context) {
      activatedReasons.push(context.reason);
    },
  });

  tabs[1].hidden = true;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controller.activeKey, "tab-3");
  assert.deepEqual(beforeReasons, ["repair"]);
  assert.deepEqual(activatedReasons, ["repair"]);
});

test("pending forced repair is aborted if its original active tab recovers", async () => {
  const { tablist, tabs, panels } = createFixture({ count: 3 });
  const slow = deferred();
  let repairContext;
  const controller = createTabs(tablist, {
    initialKey: "tab-2",
    panels,
    async beforeActivate(context) {
      if (context.reason !== "repair") return true;
      repairContext = context;
      await slow.promise;
      return true;
    },
  });

  tabs[1].hidden = true;
  const pendingRepair = controller.repair();
  await Promise.resolve();
  assert.equal(repairContext.reason, "repair");
  tabs[1].hidden = false;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(repairContext.signal.aborted, true);
  assert.equal(repairContext.isCurrent(), false);

  slow.resolve();
  assert.equal(await pendingRepair, false);
  assert.equal(controller.activeKey, "tab-2");
  assert.deepEqual(selectedKeys(tabs), ["tab-2"]);
});

test("MutationObserver restores roving focus when a manually focused tab becomes unavailable", async () => {
  const { document, tablist, tabs, panels } = createFixture({ count: 3 });
  const controller = createTabs(tablist, { panels });
  tabs[0].focus();
  tablist.dispatch("keydown", { key: "ArrowRight" }, tabs[0]);
  assert.equal(document.activeElement, tabs[1]);
  assert.equal(controller.activeKey, "tab-1");

  tabs[1].hidden = true;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controller.activeKey, "tab-1");
  assert.equal(document.activeElement, tabs[0]);
  assert.deepEqual(tabs.map((tab) => tab.getAttribute("tabindex")), ["0", "-1", "-1"]);
});

test("async veto and stale beforeActivate cannot commit an obsolete tab", async () => {
  const { tablist, tabs, panels } = createFixture();
  const slow = deferred();
  const calls = [];
  const controller = createTabs(tablist, {
    panels,
    async beforeActivate(context) {
      calls.push(context);
      if (context.key === "tab-2") await slow.promise;
      return context.key !== "tab-4";
    },
  });

  const obsolete = controller.activate("tab-2");
  const current = controller.activate("tab-3");
  assert.equal(await current, true);
  slow.resolve(true);
  assert.equal(await obsolete, false);
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(calls[0].isCurrent(), false);
  assert.equal(controller.activeKey, "tab-3");
  assert.deepEqual(selectedKeys(tabs), ["tab-3"]);

  assert.equal(await controller.activate("tab-4"), false);
  assert.equal(controller.activeKey, "tab-3");
});

test("availability is checked again after an asynchronous veto hook", async () => {
  const { tablist, tabs, panels } = createFixture({ count: 3 });
  const slow = deferred();
  let context;
  const controller = createTabs(tablist, {
    panels,
    async beforeActivate(candidate) {
      context = candidate;
      await slow.promise;
      return true;
    },
  });

  const pending = controller.activate("tab-2");
  tabs[1].hidden = true;
  slow.resolve();
  assert.equal(await pending, false);
  assert.equal(context.isCurrent(), false);
  assert.equal(controller.activeKey, "tab-1");
  assert.deepEqual(selectedKeys(tabs), ["tab-1"]);
});

test("a vetoed transition does not abort the current tab load", async () => {
  const { tablist, panels } = createFixture({ count: 3 });
  const slow = deferred();
  let activeContext;
  const controller = createTabs(tablist, {
    panels,
    beforeActivate: ({ key }) => key !== "tab-3",
    async onActivate(context) {
      if (context.key !== "tab-2") return;
      activeContext = context;
      await slow.promise;
    },
  });

  const activeLoad = controller.activate("tab-2");
  await Promise.resolve();
  assert.equal(controller.activeKey, "tab-2");
  assert.equal(await controller.activate("tab-3"), false);
  assert.equal(activeContext.signal.aborted, false);
  assert.equal(controller.activeKey, "tab-2");
  slow.resolve();
  assert.equal(await activeLoad, true);
});

test("a pointer veto restores focus and roving tabindex to the active tab", async () => {
  const { document, tablist, tabs, panels } = createFixture({ count: 3 });
  const controller = createTabs(tablist, {
    panels,
    beforeActivate: () => false,
  });

  tabs[1].focus();
  tablist.dispatch("click", {}, tabs[1]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controller.activeKey, "tab-1");
  assert.equal(document.activeElement, tabs[0]);
  assert.deepEqual(tabs.map((tab) => tab.getAttribute("tabindex")), ["0", "-1", "-1"]);
});

test("a throwing pending beforeActivate aborts its cooperative guard", async () => {
  const { tablist, panels } = createFixture({ count: 2 });
  const failure = new Error("beforeActivate failed");
  let context;
  const controller = createTabs(tablist, {
    panels,
    beforeActivate(candidate) {
      context = candidate;
      throw failure;
    },
  });

  await assert.rejects(controller.activate("tab-2"), (error) => error === failure);
  assert.equal(context.signal.aborted, true);
  assert.equal(context.isCurrent(), false);
  assert.equal(controller.activeKey, "tab-1");
});

test("stale onActivate completion cannot change core state and reselect has its own hook", async () => {
  const { tablist, panels } = createFixture({ count: 3 });
  const slow = deferred();
  const activations = [];
  const guardedWrites = [];
  const reselections = [];
  const controller = createTabs(tablist, {
    panels,
    async onActivate(context) {
      activations.push(context);
      if (context.key === "tab-2") await slow.promise;
      if (context.isCurrent()) guardedWrites.push(context.key);
    },
    onReselect(context) {
      reselections.push(context.key);
    },
  });

  const obsolete = controller.activate("tab-2");
  assert.equal(controller.activeKey, "tab-2");
  const current = controller.activate("tab-3");
  assert.equal(await current, true);
  slow.resolve();
  assert.equal(await obsolete, false);
  assert.equal(activations[0].signal.aborted, true);
  assert.equal(activations[0].isCurrent(), false);
  assert.deepEqual(guardedWrites, ["tab-3"]);
  assert.equal(controller.activeKey, "tab-3");

  assert.equal(await controller.activate("tab-3"), true);
  assert.deepEqual(reselections, ["tab-3"]);
});

test("destroy removes behavior and aborts pending work", async () => {
  const { tablist, tabs, panels } = createFixture({ count: 3 });
  const slow = deferred();
  let pendingContext;
  const controller = createTabs(tablist, {
    panels,
    async beforeActivate(context) {
      pendingContext = context;
      await slow.promise;
      return true;
    },
  });

  const pending = controller.activate("tab-2");
  controller.destroy();
  assert.equal(pendingContext.signal.aborted, true);
  slow.resolve();
  assert.equal(await pending, false);
  assert.equal(await controller.activate("tab-3"), false);

  tabs[0].focus();
  const event = tablist.dispatch("keydown", { key: "End" }, tabs[0]);
  assert.equal(event.defaultPrevented, false);
  assert.equal(tabs[0].ownerDocument.activeElement, tabs[0]);
});
