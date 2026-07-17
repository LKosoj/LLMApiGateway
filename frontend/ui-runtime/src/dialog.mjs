const FOCUSABLE_SELECTOR = [
  "a[href]",
  "area[href]",
  "button",
  "input",
  "select",
  "summary",
  "textarea",
  "iframe",
  "[contenteditable]:not([contenteditable='false'])",
  "[tabindex]",
].join(",");

const CLOSE_REASONS = new Set(["backdrop", "cancel", "escape", "submit"]);
const activeDialogs = new WeakMap();

function requireElement(value, name) {
  if (!value || typeof value.setAttribute !== "function") {
    throw new TypeError(`${name} must be a DOM element`);
  }
  return value;
}

function resolveTarget(value, context) {
  return typeof value === "function" ? value(context) : value;
}

function firstSummary(details) {
  return Array.from(details.children ?? [])
    .find((child) => child.tagName?.toLowerCase() === "summary") ?? null;
}

function isHiddenByClosedDetails(element) {
  for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
    if (
      ancestor.tagName?.toLowerCase() !== "details"
      || ancestor.hasAttribute("open")
    ) continue;
    if (!firstSummary(ancestor)?.contains(element)) return true;
  }
  return false;
}

function isVisible(element) {
  if (
    element.isConnected === false
    || element.hidden
    || element.inert
    || element.closest?.("[hidden], [inert], [aria-hidden='true']")
  ) return false;
  if (typeof element.checkVisibility === "function") {
    return element.checkVisibility({
      checkOpacity: false,
      checkVisibilityCSS: true,
      contentVisibilityAuto: true,
    });
  }
  if (isHiddenByClosedDetails(element)) return false;
  const getComputedStyle = element.ownerDocument?.defaultView?.getComputedStyle;
  if (typeof getComputedStyle !== "function") return true;
  for (let current = element; current; current = current.parentElement) {
    const style = getComputedStyle(current);
    if (
      style?.display === "none"
      || style?.contentVisibility === "hidden"
      || style?.visibility === "hidden"
      || style?.visibility === "collapse"
    ) return false;
  }
  return true;
}

function focusKind(element) {
  const tagName = element.tagName?.toLowerCase();
  const tabIndexAttribute = element.getAttribute?.("tabindex");
  const hasTabIndex = tabIndexAttribute !== null
    && /^-?\d+$/.test(tabIndexAttribute.trim());
  const tabIndex = hasTabIndex ? Number(tabIndexAttribute) : null;
  const contentEditable = element.getAttribute?.("contenteditable");
  const editable = contentEditable !== null && contentEditable.toLowerCase() !== "false";
  const summary = tagName === "summary"
    && element.parentElement?.tagName?.toLowerCase() === "details"
    && firstSummary(element.parentElement) === element;
  const native = (
    summary
    || ["button", "iframe", "select", "textarea"].includes(tagName)
    || (tagName === "input" && element.getAttribute?.("type")?.toLowerCase() !== "hidden")
    || (["a", "area"].includes(tagName) && element.getAttribute?.("href") !== null)
  );
  return { programmatic: native || editable || hasTabIndex, sequential: tabIndex ?? 0 };
}

function canReceiveFocus(element, sequential = false) {
  if (
    !element
    || typeof element.focus !== "function"
    || element.disabled
    || element.matches?.(":disabled")
    || !isVisible(element)
  ) return false;
  const kind = focusKind(element);
  return kind.programmatic && (!sequential || kind.sequential >= 0);
}

function snapshotAttribute(element, name) {
  return {
    name,
    present: element.hasAttribute(name),
    value: element.getAttribute(name),
  };
}

function restoreAttribute(element, snapshot) {
  if (snapshot.present) element.setAttribute(snapshot.name, snapshot.value ?? "");
  else element.removeAttribute(snapshot.name);
}

export function createDialog(options) {
  if (!options || typeof options !== "object") {
    throw new TypeError("Dialog options are required");
  }
  const document = options.document ?? globalThis.document;
  if (!document || typeof document.addEventListener !== "function") {
    throw new TypeError("A browser document is required");
  }
  const overlay = requireElement(options.overlay, "overlay");
  const dialog = requireElement(options.dialog ?? overlay, "dialog");
  if (overlay !== dialog && !overlay.contains(dialog)) {
    throw new TypeError("dialog must be contained by overlay");
  }
  const inertRoots = [...new Set(options.inertRoots ?? [])];
  for (const root of inertRoots) {
    requireElement(root, "inert root");
    if (root === overlay || root.contains?.(overlay)) {
      throw new TypeError("An inert root cannot contain the dialog overlay");
    }
  }

  const labelledBy = options.labelledBy ?? dialog.getAttribute("aria-labelledby");
  const label = options.label ?? dialog.getAttribute("aria-label");
  if (!labelledBy && !label) {
    throw new TypeError("Dialog requires labelledBy, label, or an existing accessible name");
  }
  if (options.onClose !== undefined && typeof options.onClose !== "function") {
    throw new TypeError("onClose must be a function");
  }

  const semanticSnapshot = ["role", "aria-modal", "aria-labelledby", "aria-label", "tabindex"]
    .map((name) => snapshotAttribute(dialog, name));
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  if (labelledBy) dialog.setAttribute("aria-labelledby", labelledBy);
  else dialog.setAttribute("aria-label", label);
  if (!dialog.hasAttribute("tabindex")) dialog.setAttribute("tabindex", "-1");
  if (options.openClass) overlay.classList.remove(options.openClass);
  overlay.hidden = true;
  overlay.setAttribute("hidden", "");

  let destroyed = false;
  let open = false;
  let opener = null;
  let inertSnapshots = [];

  function focusableElements() {
    return Array.from(dialog.querySelectorAll(FOCUSABLE_SELECTOR))
      .filter((element) => canReceiveFocus(element, true));
  }

  function focusInside() {
    const requested = resolveTarget(options.initialFocus, { dialog, overlay });
    const target = requested && dialog.contains(requested) && canReceiveFocus(requested)
      ? requested
      : (focusableElements()[0] ?? dialog);
    target.focus();
  }

  function restoreFocus(reason) {
    const context = Object.freeze({ dialog, opener, overlay, reason });
    const requested = resolveTarget(options.restoreFocus, context);
    const fallback = resolveTarget(options.restoreFocusFallback, context);
    for (const candidate of [requested, fallback, opener]) {
      if (!candidate || overlay.contains(candidate) || !canReceiveFocus(candidate)) continue;
      try {
        candidate.focus({ preventScroll: true });
      } catch {
        continue;
      }
      if (document.activeElement === candidate) return;
    }

    const body = document.body;
    if (
      !body
      || typeof body.focus !== "function"
      || typeof body.setAttribute !== "function"
      || overlay.contains(body)
    ) {
      throw new Error("Dialog cannot restore focus outside its overlay");
    }
    const tabIndexSnapshot = snapshotAttribute(body, "tabindex");
    try {
      body.setAttribute("tabindex", "-1");
      body.focus({ preventScroll: true });
      if (document.activeElement !== body) {
        throw new Error("Dialog could not move focus outside its overlay");
      }
    } finally {
      restoreAttribute(body, tabIndexSnapshot);
    }
  }

  function setBackgroundInert() {
    inertSnapshots = inertRoots.map((root) => ({
      root,
      hadAttribute: root.hasAttribute("inert"),
      propertyValue: Boolean(root.inert),
    }));
    for (const { root } of inertSnapshots) {
      root.inert = true;
      root.setAttribute("inert", "");
    }
  }

  function restoreBackground() {
    for (const snapshot of inertSnapshots) {
      snapshot.root.inert = snapshot.propertyValue;
      if (snapshot.hadAttribute) snapshot.root.setAttribute("inert", "");
      else snapshot.root.removeAttribute("inert");
      snapshot.root.inert = snapshot.propertyValue;
    }
    inertSnapshots = [];
  }

  function handleKeydown(event) {
    if (!open) return;
    if (event.key === "Escape") {
      event.preventDefault();
      close("escape");
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = focusableElements();
    if (focusable.length === 0) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function handleFocusIn(event) {
    if (open && !dialog.contains(event.target)) focusInside();
  }

  function handleOverlayClick(event) {
    if (!open) return;
    if (event.target === overlay && options.closeOnBackdrop !== false) {
      close("backdrop");
      return;
    }
    const control = event.target?.closest?.("[data-dialog-close]");
    if (!control || !dialog.contains(control)) return;
    const reason = control.getAttribute("data-dialog-close") || "cancel";
    if (CLOSE_REASONS.has(reason)) close(reason);
  }

  function handleCancel(event) {
    if (!open) return;
    event.preventDefault();
    close("cancel");
  }

  function handleSubmit(event) {
    if (open && options.closeOnSubmit === true && !event.defaultPrevented) close("submit");
  }

  function addOpenListeners() {
    document.addEventListener("keydown", handleKeydown, true);
    document.addEventListener("focusin", handleFocusIn, true);
    overlay.addEventListener("click", handleOverlayClick);
    dialog.addEventListener("cancel", handleCancel);
    dialog.addEventListener("submit", handleSubmit);
  }

  function removeOpenListeners() {
    document.removeEventListener("keydown", handleKeydown, true);
    document.removeEventListener("focusin", handleFocusIn, true);
    overlay.removeEventListener("click", handleOverlayClick);
    dialog.removeEventListener("cancel", handleCancel);
    dialog.removeEventListener("submit", handleSubmit);
  }

  function requireUsable() {
    if (destroyed) throw new Error("Dialog controller has been destroyed");
  }

  function show() {
    requireUsable();
    if (open) return false;
    if (activeDialogs.has(document)) {
      throw new Error("Another dialog is already open in this document");
    }
    opener = document.activeElement;
    activeDialogs.set(document, controller);
    open = true;
    setBackgroundInert();
    overlay.hidden = false;
    overlay.removeAttribute("hidden");
    if (options.openClass) overlay.classList.add(options.openClass);
    addOpenListeners();
    focusInside();
    return true;
  }

  function finishClose(reason, notify) {
    requireUsable();
    if (!open) return false;
    open = false;
    activeDialogs.delete(document);
    removeOpenListeners();
    restoreBackground();
    if (options.openClass) overlay.classList.remove(options.openClass);
    overlay.hidden = true;
    overlay.setAttribute("hidden", "");
    restoreFocus(reason);
    opener = null;
    if (notify) options.onClose?.(reason);
    return true;
  }

  function close(reason = "cancel") {
    if (!CLOSE_REASONS.has(reason)) {
      throw new TypeError(`Unsupported dialog close reason: ${reason}`);
    }
    return finishClose(reason, true);
  }

  function destroy() {
    if (destroyed) return false;
    if (open) finishClose("destroy", false);
    removeOpenListeners();
    semanticSnapshot.forEach((snapshot) => restoreAttribute(dialog, snapshot));
    destroyed = true;
    return true;
  }

  const controller = Object.freeze({
    open: show,
    close,
    destroy,
    get state() {
      if (destroyed) return "destroyed";
      return open ? "open" : "closed";
    },
  });
  return controller;
}
