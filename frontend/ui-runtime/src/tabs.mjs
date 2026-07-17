let generatedTablistId = 0;
const FORCE_REPAIR = Symbol("forceRepair");
const REPAIR_REVISION = Symbol("repairRevision");
const REPAIR_SOURCE = Symbol("repairSource");

function requireFunction(value, name) {
  if (value !== undefined && typeof value !== "function") {
    throw new TypeError(`${name} must be a function`);
  }
}

function ownTabs(root) {
  return Array.from(root.querySelectorAll('[role="tab"]'))
    .filter((tab) => tab.closest('[role="tablist"]') === root);
}

function hasUnavailableState(element) {
  return element.hidden === true
    || element.disabled === true
    || element.inert === true
    || element.hasAttribute("hidden")
    || element.hasAttribute("disabled")
    || element.hasAttribute("inert")
    || element.getAttribute("aria-hidden") === "true"
    || element.getAttribute("aria-disabled") === "true";
}

function isAvailable(root, tab) {
  if (tab.getAttribute("role") !== "tab") return false;
  let node = tab;
  while (node) {
    if (hasUnavailableState(node)) return false;
    if (node === root) return true;
    node = node.parentElement;
  }
  return false;
}

function resolveDirection(root, option) {
  const configured = typeof option === "function" ? option() : option;
  if (configured === "ltr" || configured === "rtl") return configured;
  const computed = root.ownerDocument?.defaultView?.getComputedStyle?.(root)?.direction;
  if (computed === "rtl") return "rtl";
  return root.ownerDocument?.documentElement?.getAttribute?.("dir") === "rtl" ? "rtl" : "ltr";
}

function documentIdOwners(document, id) {
  if (document?.querySelectorAll) {
    return Array.from(document.querySelectorAll("[id]")).filter((element) => element.id === id);
  }
  const owner = document?.getElementById?.(id);
  return owner ? [owner] : [];
}

function requireUniqueExistingId(element, subject) {
  const owners = documentIdOwners(element.ownerDocument, element.id);
  if (owners.length !== 1 || owners[0] !== element) {
    throw new TypeError(`${subject} ID already exists in the document: ${element.id}`);
  }
}

function generatedRootId(root) {
  const document = root.ownerDocument;
  if (root.id) {
    requireUniqueExistingId(root, "Tablist");
    return root.id;
  }
  let candidate;
  do {
    generatedTablistId += 1;
    candidate = `gateway-tabs-${generatedTablistId}`;
  } while (documentIdOwners(document, candidate).length > 0);
  root.id = candidate;
  return candidate;
}

function ensureElementId(element, desiredId, subject) {
  const document = element.ownerDocument;
  if (element.id) {
    requireUniqueExistingId(element, subject);
    return element.id;
  }

  let candidate = desiredId;
  let suffix = 2;
  while (documentIdOwners(document, candidate).length > 0) {
    candidate = `${desiredId}-${suffix}`;
    suffix += 1;
  }
  element.id = candidate;
  return candidate;
}

function resolveEventTab(root, items, target) {
  let node = target;
  while (node && node !== root) {
    const item = items.find((candidate) => candidate.tab === node);
    if (item) return item;
    node = node.parentElement;
  }
  return null;
}

function closestAvailable(items, root, startIndex) {
  for (let index = startIndex + 1; index < items.length; index += 1) {
    if (isAvailable(root, items[index].tab)) return items[index];
  }
  for (let index = startIndex - 1; index >= 0; index -= 1) {
    if (isAvailable(root, items[index].tab)) return items[index];
  }
  return null;
}

function availableItems(items, root) {
  return items.filter((item) => isAvailable(root, item.tab));
}

function focusItem(items, item) {
  for (const candidate of items) {
    candidate.tab.setAttribute("tabindex", candidate === item ? "0" : "-1");
  }
}

function scrollItem(item) {
  item.tab.scrollIntoView?.({ block: "nearest", inline: "nearest" });
}

function operationContext(item, previous, controller, signal, sequence, reason, isCurrent) {
  return Object.freeze({
    controller,
    isCurrent,
    key: item.key,
    panel: item.panel,
    previousKey: previous?.key ?? null,
    previousPanel: previous?.panel ?? null,
    previousTab: previous?.tab ?? null,
    reason,
    sequence,
    signal,
    tab: item.tab,
  });
}

/**
 * Build one accessible tabs controller around a tablist.
 *
 * Panels may be supplied in tab order or resolved from existing aria-controls.
 * Page-specific behavior belongs in beforeActivate/onActivate/onReselect hooks.
 * Async hooks must check signal or isCurrent() before applying their own DOM writes;
 * the controller can only prevent stale commits to tab/panel state it owns.
 */
export function createTabs(root, options = {}) {
  if (!root?.querySelectorAll || !root?.addEventListener) {
    throw new TypeError("createTabs root must be a tablist element");
  }
  requireFunction(options.getKey, "getKey");
  requireFunction(options.getPanel, "getPanel");
  requireFunction(options.beforeActivate, "beforeActivate");
  requireFunction(options.onActivate, "onActivate");
  requireFunction(options.onReselect, "onReselect");
  if (options.activation !== undefined
      && options.activation !== "manual"
      && options.activation !== "automatic") {
    throw new TypeError('activation must be "manual" or "automatic"');
  }

  root.setAttribute("role", "tablist");
  const rootId = generatedRootId(root);
  const tabs = ownTabs(root);
  if (tabs.length === 0) throw new TypeError("createTabs requires at least one owned tab");

  const suppliedPanels = options.panels === undefined ? null : Array.from(options.panels);
  if (suppliedPanels && suppliedPanels.length !== tabs.length) {
    throw new TypeError("tabs and panels must have the same length");
  }

  const keys = new Set();
  const panels = new Set();
  const items = tabs.map((tab, index) => {
    const rawKey = options.getKey?.(tab, index)
      ?? tab.dataset?.tabKey
      ?? tab.dataset?.tab
      ?? String(index);
    const key = String(rawKey);
    if (!key || keys.has(key)) throw new TypeError(`Tab key must be unique: ${key}`);
    keys.add(key);

    const controlledId = tab.getAttribute("aria-controls");
    const panel = options.getPanel?.(tab, key, index)
      ?? suppliedPanels?.[index]
      ?? (controlledId ? root.ownerDocument?.getElementById?.(controlledId) : null);
    if (!panel || panels.has(panel)) {
      throw new TypeError(`Tab panel must exist and be unique: ${key}`);
    }
    panels.add(panel);

    ensureElementId(tab, `${rootId}-tab-${index + 1}`, "Tab");
    ensureElementId(panel, `${rootId}-panel-${index + 1}`, "Tab panel");
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", panel.id);
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tab.id);
    return Object.freeze({ index, key, panel, tab });
  });

  const graphIds = items.flatMap((item) => [item.tab.id, item.panel.id]);
  if (new Set(graphIds).size !== graphIds.length) {
    throw new TypeError("Tab and panel IDs must be unique");
  }

  let destroyed = false;
  let active = null;
  let roving = null;
  let sequence = 0;
  let activeOperation = null;
  let pendingOperation = null;
  let pendingRepairOperation = null;
  let repairRevision = 0;
  let observer = null;

  function itemFor(target) {
    if (typeof target === "string") return items.find((item) => item.key === target) ?? null;
    return items.find((item) => item.tab === target) ?? null;
  }

  function beginOperation() {
    if (pendingOperation) {
      if (pendingRepairOperation === pendingOperation) pendingRepairOperation = null;
      pendingOperation.abort();
    }
    pendingOperation = new AbortController();
    sequence += 1;
    return { abortController: pendingOperation, token: sequence };
  }

  function isPending(abortController, token) {
    return !destroyed
      && pendingOperation === abortController
      && sequence === token
      && !abortController.signal.aborted;
  }

  function isActive(abortController) {
    return !destroyed
      && activeOperation === abortController
      && !abortController.signal.aborted;
  }

  function promoteOperation(abortController) {
    activeOperation?.abort();
    activeOperation = abortController;
    if (pendingRepairOperation === abortController) pendingRepairOperation = null;
    pendingOperation = null;
  }

  function cancelPending(abortController) {
    if (pendingOperation === abortController) pendingOperation = null;
    if (pendingRepairOperation === abortController) pendingRepairOperation = null;
    abortController.abort();
  }

  function setRoving(item, focus) {
    focusItem(items, item);
    roving = item;
    if (!focus) return;
    item.tab.focus();
  }

  function commit(item, focus) {
    for (const candidate of items) {
      const selected = candidate === item;
      candidate.tab.setAttribute("aria-selected", selected ? "true" : "false");
      candidate.tab.setAttribute("tabindex", selected ? "0" : "-1");
      candidate.panel.hidden = !selected;
    }
    active = item;
    roving = item;
    if (focus) item.tab.focus();
    scrollItem(item);
  }

  async function activate(target, activateOptions = {}) {
    if (destroyed) return false;
    const item = itemFor(target);
    if (!item || !isAvailable(root, item.tab)) return false;

    const previous = active;
    const { abortController, token } = beginOperation();
    const forcedRepair = activateOptions[FORCE_REPAIR] === true;
    if (forcedRepair) pendingRepairOperation = abortController;
    const context = operationContext(
      item,
      previous,
      controller,
      abortController.signal,
      token,
      activateOptions.reason ?? "programmatic",
      () => isPending(abortController, token) || isActive(abortController),
    );

    if (item === previous) {
      promoteOperation(abortController);
      if (activateOptions.focus) setRoving(item, true);
      scrollItem(item);
      if (!options.onReselect) return true;
      try {
        await options.onReselect(context);
      } catch (error) {
        if (!isActive(abortController)) return false;
        throw error;
      }
      return isActive(abortController);
    }

    try {
      if (options.beforeActivate) {
        const permitted = await options.beforeActivate(context);
        if (!isPending(abortController, token)) return false;
        if (permitted === false && !forcedRepair) {
          const restorePointerFocus = context.reason === "pointer"
            && active
            && isAvailable(root, active.tab);
          cancelPending(abortController);
          if (restorePointerFocus) setRoving(active, true);
          return false;
        }
      }
      if (!isPending(abortController, token)) return false;
      if (forcedRepair && (
        activateOptions[REPAIR_REVISION] !== repairRevision
        || activateOptions[REPAIR_SOURCE] !== active
        || isAvailable(root, activateOptions[REPAIR_SOURCE].tab)
      )) {
        cancelPending(abortController);
        return false;
      }
      if (!isAvailable(root, item.tab)) {
        cancelPending(abortController);
        return false;
      }
      promoteOperation(abortController);
      commit(item, activateOptions.focus === true);
      if (options.onActivate) await options.onActivate(context);
    } catch (error) {
      if (isPending(abortController, token)) {
        cancelPending(abortController);
        throw error;
      }
      if (!isActive(abortController)) return false;
      throw error;
    }
    return isActive(abortController);
  }

  async function repair() {
    if (destroyed) return false;
    repairRevision += 1;
    if (pendingRepairOperation) cancelPending(pendingRepairOperation);
    if (active && isAvailable(root, active.tab)) {
      if (roving && isAvailable(root, roving.tab)) return false;
      const restoreFocus = roving?.tab.ownerDocument?.activeElement === roving.tab;
      setRoving(active, restoreFocus);
      return true;
    }
    const startIndex = active?.index ?? -1;
    const replacement = active
      ? closestAvailable(items, root, startIndex)
      : availableItems(items, root)[0] ?? null;
    if (!replacement) return false;
    const source = active;
    return activate(replacement.key, {
      [FORCE_REPAIR]: true,
      [REPAIR_REVISION]: repairRevision,
      [REPAIR_SOURCE]: source,
      focus: false,
      reason: "repair",
    });
  }

  function moveFocus(current, offset) {
    const available = availableItems(items, root);
    if (available.length === 0) return null;
    const currentIndex = available.indexOf(current);
    const nextIndex = currentIndex === -1
      ? 0
      : (currentIndex + offset + available.length) % available.length;
    return available[nextIndex];
  }

  function reportEventActivation(promise) {
    promise.catch((error) => {
      queueMicrotask(() => { throw error; });
    });
  }

  function onKeydown(event) {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const current = resolveEventTab(root, items, event.target);
    if (!current || !isAvailable(root, current.tab)) return;

    const orientation = root.getAttribute("aria-orientation") ?? "horizontal";
    const direction = resolveDirection(root, options.direction);
    let next = null;
    if (event.key === "Home") next = availableItems(items, root)[0] ?? null;
    else if (event.key === "End") next = availableItems(items, root).at(-1) ?? null;
    else if (orientation === "vertical" && event.key === "ArrowUp") next = moveFocus(current, -1);
    else if (orientation === "vertical" && event.key === "ArrowDown") next = moveFocus(current, 1);
    else if (orientation !== "vertical" && event.key === "ArrowLeft") {
      next = moveFocus(current, direction === "rtl" ? 1 : -1);
    } else if (orientation !== "vertical" && event.key === "ArrowRight") {
      next = moveFocus(current, direction === "rtl" ? -1 : 1);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      reportEventActivation(activate(current.key, { focus: true, reason: "keyboard" }));
      return;
    } else {
      return;
    }

    if (!next) return;
    event.preventDefault();
    setRoving(next, true);
    if (options.activation === "automatic") {
      reportEventActivation(activate(next.key, { focus: true, reason: "keyboard" }));
    }
  }

  function onClick(event) {
    const item = resolveEventTab(root, items, event.target);
    if (!item || !isAvailable(root, item.tab)) return;
    reportEventActivation(activate(item.key, { focus: true, reason: "pointer" }));
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    sequence += 1;
    pendingOperation?.abort();
    activeOperation?.abort();
    observer?.disconnect();
    root.removeEventListener("click", onClick);
    root.removeEventListener("keydown", onKeydown);
  }

  const controller = Object.freeze({
    activate,
    destroy,
    repair,
    get activeKey() {
      return active?.key ?? null;
    },
  });

  const requestedInitial = options.initialKey === undefined
    ? items.find((item) => item.tab.getAttribute("aria-selected") === "true") ?? items[0]
    : itemFor(String(options.initialKey));
  if (!requestedInitial) throw new TypeError(`Unknown initial tab key: ${options.initialKey}`);
  const initial = isAvailable(root, requestedInitial.tab)
    ? requestedInitial
    : closestAvailable(items, root, requestedInitial.index);
  if (!initial) throw new TypeError("createTabs requires at least one available tab");
  commit(initial, false);

  root.addEventListener("click", onClick);
  root.addEventListener("keydown", onKeydown);
  const MutationObserver = root.ownerDocument?.defaultView?.MutationObserver;
  if (MutationObserver) {
    observer = new MutationObserver(() => {
      reportEventActivation(repair());
    });
    observer.observe(root, {
      attributeFilter: ["aria-disabled", "aria-hidden", "disabled", "hidden", "inert", "role"],
      attributes: true,
      subtree: true,
    });
  }

  return controller;
}
