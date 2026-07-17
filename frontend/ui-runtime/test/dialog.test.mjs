import assert from "node:assert/strict";
import test from "node:test";

import { createDialog } from "../src/dialog.mjs";

class EventTargetFake {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type, listener) {
    this.listeners.get(type)?.delete(listener);
  }

  emit(type, event = {}) {
    event.target ??= this;
    event.preventDefault ??= () => { event.defaultPrevented = true; };
    for (const listener of this.listeners.get(type) ?? []) listener(event);
    return event;
  }
}

class ClassListFake {
  constructor() {
    this.values = new Set();
  }

  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}

class ElementFake extends EventTargetFake {
  constructor(document, options = {}) {
    super();
    this.ownerDocument = document;
    this.attributes = new Map();
    this.children = [];
    this.parentElement = null;
    this.classList = new ClassListFake();
    this.disabled = false;
    this.hidden = false;
    this.inert = false;
    this.isConnected = true;
    this.focusable = options.focusable ?? false;
    this.tagName = (options.tagName ?? (this.focusable ? "button" : "div")).toUpperCase();
    this.disabledByFieldset = false;
    this.rendered = true;
    this.styleDisplay = "block";
    this.styleContentVisibility = "visible";
    this.styleVisibility = "visible";
    this.checkVisibilityResult = options.checkVisibility;
    this.focusNoop = false;
    if (Object.hasOwn(options, "checkVisibility")) {
      this.checkVisibilityCalls = [];
      this.checkVisibility = (visibilityOptions) => {
        this.checkVisibilityCalls.push(visibilityOptions);
        return this.checkVisibilityResult;
      };
    }
  }

  append(...children) {
    for (const child of children) {
      child.parentElement = this;
      this.children.push(child);
    }
  }

  contains(candidate) {
    return candidate === this || this.children.some((child) => child.contains(candidate));
  }

  querySelectorAll() {
    const result = [];
    const visit = (element) => {
      if (
        element.focusable
        || element.tagName === "SUMMARY"
        || element.hasAttribute("contenteditable")
        || element.hasAttribute("tabindex")
      ) result.push(element);
      element.children.forEach(visit);
    };
    this.children.forEach(visit);
    return result;
  }

  closest(selector) {
    let candidate = this;
    while (candidate) {
      if (selector === "[data-dialog-close]" && candidate.hasAttribute("data-dialog-close")) {
        return candidate;
      }
      if (
        selector === "[hidden], [inert], [aria-hidden='true']"
        && (
          candidate.hidden
          || candidate.inert
          || candidate.getAttribute("aria-hidden") === "true"
        )
      ) return candidate;
      if (
        selector === "details:not([open])"
        && candidate.tagName === "DETAILS"
        && !candidate.hasAttribute("open")
      ) return candidate;
      candidate = candidate.parentElement;
    }
    return null;
  }

  focus() {
    const tabIndex = this.getAttribute("tabindex");
    const contentEditable = this.getAttribute("contenteditable");
    const firstSummary = this.parentElement?.tagName === "DETAILS"
      && this.parentElement.children.find((child) => child.tagName === "SUMMARY") === this;
    const native = (
      ["BUTTON", "INPUT", "SELECT", "TEXTAREA", "IFRAME"].includes(this.tagName)
      || firstSummary
    ) && !(this.tagName === "INPUT" && this.getAttribute("type") === "hidden");
    const canFocus = (native || tabIndex !== null || contentEditable === "true")
      && !this.disabled
      && !this.disabledByFieldset
      && !this.hidden
      && !this.inert
      && this.rendered
      && this.styleDisplay !== "none"
      && this.styleContentVisibility !== "hidden"
      && !["hidden", "collapse"].includes(this.styleVisibility);
    if (
      canFocus
      && this.checkVisibilityResult !== false
      && !this.hiddenByClosedDetails()
      && !this.focusNoop
    ) this.ownerDocument.activeElement = this;
  }

  hiddenByClosedDetails() {
    for (let ancestor = this.parentElement; ancestor; ancestor = ancestor.parentElement) {
      if (ancestor.tagName !== "DETAILS" || ancestor.hasAttribute("open")) continue;
      const summary = ancestor.children.find((child) => child.tagName === "SUMMARY");
      if (!summary?.contains(this)) return true;
    }
    return false;
  }

  getClientRects() {
    return this.rendered ? [{}] : [];
  }

  matches(selector) {
    return selector === ":disabled" && (this.disabled || this.disabledByFieldset);
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === "hidden") this.hidden = true;
    if (name === "inert") this.inert = true;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === "hidden") this.hidden = false;
    if (name === "inert") this.inert = false;
  }
}

class DocumentFake extends EventTargetFake {
  constructor() {
    super();
    this.activeElement = null;
    this.body = new ElementFake(this, { tagName: "body" });
    this.defaultView = {
      getComputedStyle: (element) => ({
        contentVisibility: element.styleContentVisibility,
        display: element.styleDisplay,
        visibility: element.styleVisibility,
      }),
    };
  }
}

function makeDialog(options = {}) {
  const document = new DocumentFake();
  const overlay = new ElementFake(document);
  overlay.classList.add("visible");
  const dialog = new ElementFake(document);
  const first = new ElementFake(document, { focusable: true });
  const last = new ElementFake(document, { focusable: true });
  dialog.append(first, last);
  overlay.append(dialog);
  const background = new ElementFake(document);
  const controller = createDialog({
    document,
    overlay,
    dialog,
    inertRoots: [background],
    labelledBy: "dialog-title",
    openClass: "visible",
    ...options,
  });
  return { background, controller, dialog, document, first, last, overlay };
}

test("dialog establishes semantics, inerts explicit roots, and forbids nested opens", () => {
  const first = makeDialog();
  const secondOverlay = new ElementFake(first.document);
  const secondDialog = new ElementFake(first.document);
  secondOverlay.append(secondDialog);
  const second = createDialog({
    document: first.document,
    overlay: secondOverlay,
    dialog: secondDialog,
    label: "Second dialog",
  });

  assert.equal(first.overlay.hidden, true);
  assert.equal(first.overlay.hasAttribute("hidden"), true);
  assert.equal(first.overlay.classList.contains("visible"), false);

  first.controller.open();

  assert.equal(first.dialog.getAttribute("role"), "dialog");
  assert.equal(first.dialog.getAttribute("aria-modal"), "true");
  assert.equal(first.dialog.getAttribute("aria-labelledby"), "dialog-title");
  assert.equal(first.background.inert, true);
  assert.equal(first.overlay.hidden, false);
  assert.equal(first.overlay.classList.contains("visible"), true);
  assert.equal(first.document.activeElement, first.first);
  assert.throws(() => second.open(), /already open/);

  first.controller.close("cancel");
  assert.equal(first.background.inert, false);
  assert.equal(first.document.activeElement, first.document.body);
  assert.equal(first.document.body.getAttribute("tabindex"), null);
  assert.equal(second.open(), true);
  second.close("cancel");
});

test("dialog traps Tab and focus, then restores a dynamically resolved trigger", () => {
  const reasons = [];
  const trigger = { current: null };
  const fixture = makeDialog({
    onClose: (reason) => reasons.push(reason),
    restoreFocus: () => trigger.current,
    restoreFocusFallback: () => fallbackTrigger,
  });
  const originalTrigger = new ElementFake(fixture.document, { focusable: true });
  const fallbackTrigger = new ElementFake(fixture.document, { focusable: true });
  fixture.document.activeElement = originalTrigger;
  fixture.controller.open();

  fixture.last.focus();
  const tabEvent = fixture.document.emit("keydown", { key: "Tab", shiftKey: false });
  assert.equal(tabEvent.defaultPrevented, true);
  assert.equal(fixture.document.activeElement, fixture.first);

  const outside = new ElementFake(fixture.document, { focusable: true });
  outside.focus();
  fixture.document.emit("focusin", { target: outside });
  assert.equal(fixture.document.activeElement, fixture.first);

  trigger.current = new ElementFake(fixture.document, { focusable: true });
  trigger.current.focusNoop = true;
  const escapeEvent = fixture.document.emit("keydown", { key: "Escape" });
  assert.equal(escapeEvent.defaultPrevented, true);
  assert.equal(fixture.document.activeElement, fallbackTrigger);
  assert.deepEqual(reasons, ["escape"]);
});

test("dialog skips controls that cannot actually receive focus", () => {
  const requested = { current: null };
  const fixture = makeDialog({ initialFocus: () => requested.current });
  const plainDiv = new ElementFake(fixture.document);
  const fieldsetDisabled = new ElementFake(fixture.document, { focusable: true });
  fieldsetDisabled.disabledByFieldset = true;
  const contentEditableFalse = new ElementFake(fixture.document);
  contentEditableFalse.setAttribute("contenteditable", "false");
  const hiddenInput = new ElementFake(fixture.document, { focusable: true, tagName: "input" });
  hiddenInput.setAttribute("type", "hidden");
  const negativeTabIndex = new ElementFake(fixture.document);
  negativeTabIndex.setAttribute("tabindex", "-1");
  const displayNone = new ElementFake(fixture.document, { focusable: true });
  displayNone.styleDisplay = "none";
  const visibilityHidden = new ElementFake(fixture.document, { focusable: true });
  visibilityHidden.styleVisibility = "hidden";
  const contentHidden = new ElementFake(fixture.document, { focusable: true });
  contentHidden.styleContentVisibility = "hidden";
  const checkedHidden = new ElementFake(fixture.document, {
    checkVisibility: false,
    focusable: true,
  });
  const closedDetails = new ElementFake(fixture.document, { tagName: "details" });
  const closedDetailsControl = new ElementFake(fixture.document, { focusable: true });
  closedDetails.append(closedDetailsControl);
  fixture.dialog.children.unshift(
    plainDiv,
    fieldsetDisabled,
    contentEditableFalse,
    hiddenInput,
    negativeTabIndex,
    displayNone,
    visibilityHidden,
    contentHidden,
    checkedHidden,
    closedDetails,
  );
  for (const child of fixture.dialog.children) child.parentElement = fixture.dialog;
  requested.current = plainDiv;

  plainDiv.focus();
  fieldsetDisabled.focus();
  contentEditableFalse.focus();
  hiddenInput.focus();
  displayNone.focus();
  visibilityHidden.focus();
  contentHidden.focus();
  checkedHidden.focus();
  closedDetailsControl.focus();
  assert.equal(fixture.document.activeElement, null);

  fixture.controller.open();
  assert.equal(fixture.document.activeElement, fixture.first);
  assert.deepEqual(checkedHidden.checkVisibilityCalls, [{
    checkOpacity: false,
    checkVisibilityCSS: true,
    contentVisibilityAuto: true,
  }]);
  fixture.controller.close("cancel");
});

test("closed details exposes only its first summary subtree to focus", () => {
  const requested = { current: null };
  const fixture = makeDialog({ initialFocus: () => requested.current });
  const details = new ElementFake(fixture.document, { tagName: "details" });
  const summary = new ElementFake(fixture.document, { tagName: "summary" });
  const summaryControl = new ElementFake(fixture.document, { focusable: true });
  const hiddenControl = new ElementFake(fixture.document, { focusable: true });
  summary.append(summaryControl);
  details.append(summary, hiddenControl);
  fixture.dialog.children.unshift(details);
  details.parentElement = fixture.dialog;

  requested.current = summary;
  fixture.controller.open();
  assert.equal(fixture.document.activeElement, summary);
  fixture.controller.close("cancel");

  requested.current = summaryControl;
  fixture.controller.open();
  assert.equal(fixture.document.activeElement, summaryControl);
  fixture.controller.close("cancel");

  summary.setAttribute("tabindex", "-1");
  summaryControl.disabled = true;
  requested.current = hiddenControl;
  hiddenControl.focus();
  assert.notEqual(fixture.document.activeElement, hiddenControl);
  fixture.controller.open();
  assert.equal(fixture.document.activeElement, fixture.first);
  fixture.controller.close("cancel");
});

test("dialog reports backdrop, cancel, and opt-in submit close reasons", () => {
  const reasons = [];
  const backdrop = makeDialog({ onClose: (reason) => reasons.push(reason) });
  backdrop.controller.open();
  backdrop.overlay.emit("click", { target: backdrop.overlay });

  const cancelled = makeDialog({ onClose: (reason) => reasons.push(reason) });
  cancelled.controller.open();
  const cancelControl = new ElementFake(cancelled.document, { focusable: true });
  cancelControl.setAttribute("data-dialog-close", "cancel");
  cancelled.dialog.append(cancelControl);
  cancelled.overlay.emit("click", { target: cancelControl });

  const submitted = makeDialog({
    closeOnSubmit: true,
    onClose: (reason) => reasons.push(reason),
  });
  submitted.controller.open();
  submitted.dialog.emit("submit");

  assert.deepEqual(reasons, ["backdrop", "cancel", "submit"]);
});

test("destroy releases inert state and removes every open listener", () => {
  const fixture = makeDialog();
  fixture.controller.open();
  fixture.controller.destroy();

  assert.equal(fixture.controller.state, "destroyed");
  assert.equal(fixture.background.inert, false);
  assert.equal(fixture.document.listeners.get("keydown")?.size ?? 0, 0);
  assert.equal(fixture.document.listeners.get("focusin")?.size ?? 0, 0);
  assert.equal(fixture.overlay.listeners.get("click")?.size ?? 0, 0);
  assert.equal(fixture.dialog.listeners.get("submit")?.size ?? 0, 0);
  assert.throws(() => fixture.controller.open(), /destroyed/);
});
