const STATUS_SEMANTICS = Object.freeze({
  assertive: Object.freeze({ busy: false, live: "assertive", role: "alert" }),
  busy: Object.freeze({ busy: true, live: "polite", role: "status" }),
  error: Object.freeze({ busy: false, live: "assertive", role: "alert" }),
  polite: Object.freeze({ busy: false, live: "polite", role: "status" }),
});

function requireElement(value, name) {
  if (!value || typeof value.setAttribute !== "function") {
    throw new TypeError(`${name} must be a DOM element`);
  }
  return value;
}

function normalizeTimeout(value) {
  if (value === undefined || value === null) return null;
  const timeout = Number(value);
  if (!Number.isFinite(timeout) || timeout < 0) {
    throw new TypeError("timeoutMs must be a non-negative finite number");
  }
  return timeout;
}

export function createStatus(element, options = {}) {
  requireElement(element, "status element");
  const rawDetailElement = options.rawDetailElement ?? null;
  if (rawDetailElement !== null) {
    requireElement(rawDetailElement, "rawDetailElement");
    if (
      rawDetailElement === element
      || element.contains?.(rawDetailElement)
      || rawDetailElement.contains?.(element)
    ) {
      throw new TypeError("rawDetailElement must be structurally separate from status element");
    }
  }
  if (options.renderMessage !== undefined && typeof options.renderMessage !== "function") {
    throw new TypeError("renderMessage must be a function");
  }
  const renderMessage = options.renderMessage ?? ((message) => String(message ?? ""));
  const schedule = options.setTimeout ?? globalThis.setTimeout;
  const cancelScheduled = options.clearTimeout ?? globalThis.clearTimeout;
  if (typeof schedule !== "function" || typeof cancelScheduled !== "function") {
    throw new TypeError("Timer functions are required");
  }
  const defaultTimeoutMs = normalizeTimeout(options.defaultTimeoutMs);

  let destroyed = false;
  let currentState = null;
  let activeToken = Symbol("initial-status");
  let timer = null;

  function requireUsable() {
    if (destroyed) throw new Error("Status controller has been destroyed");
  }

  function cancelTimer() {
    if (timer !== null) cancelScheduled(timer);
    timer = null;
    activeToken = Symbol("status");
    return activeToken;
  }

  function renderRawDetail(rawDetail) {
    if (rawDetailElement === null) return;
    rawDetailElement.removeAttribute("role");
    rawDetailElement.removeAttribute("aria-live");
    rawDetailElement.removeAttribute("aria-atomic");
    rawDetailElement.textContent = rawDetail === null ? "" : String(rawDetail);
    if (rawDetail === null) {
      rawDetailElement.removeAttribute("lang");
      rawDetailElement.removeAttribute("dir");
    } else {
      rawDetailElement.setAttribute("lang", "und");
      rawDetailElement.setAttribute("dir", "auto");
    }
  }

  function publish(kind, message, publishOptions = {}) {
    requireUsable();
    if (!(kind in STATUS_SEMANTICS)) throw new TypeError(`Unsupported status kind: ${kind}`);
    const hasRawDetail = Object.hasOwn(publishOptions, "rawDetail");
    if (hasRawDetail && rawDetailElement === null) {
      throw new TypeError("rawDetail requires a separate rawDetailElement");
    }
    const timeoutMs = normalizeTimeout(
      Object.hasOwn(publishOptions, "timeoutMs")
        ? publishOptions.timeoutMs
        : defaultTimeoutMs,
    );
    const nextState = Object.freeze({
      kind,
      localeContext: publishOptions.localeContext,
      message,
      rawDetail: hasRawDetail ? publishOptions.rawDetail : null,
    });
    const rendered = renderMessage(nextState.message, nextState.localeContext);
    const token = cancelTimer();
    currentState = nextState;
    const semantics = STATUS_SEMANTICS[kind];
    element.setAttribute("role", semantics.role);
    element.setAttribute("aria-live", semantics.live);
    element.setAttribute("aria-atomic", "true");
    element.setAttribute("aria-busy", String(semantics.busy));
    element.setAttribute("data-status-kind", kind);
    element.classList.add("visible");
    element.classList.toggle("busy", semantics.busy);
    element.classList.toggle("error", kind === "error");
    element.textContent = String(rendered ?? "");
    renderRawDetail(nextState.rawDetail);
    if (timeoutMs !== null && timeoutMs > 0) {
      timer = schedule(() => {
        if (!destroyed && activeToken === token) clear();
      }, timeoutMs);
    }
    return token;
  }

  function clear() {
    requireUsable();
    cancelTimer();
    currentState = null;
    element.textContent = "";
    element.removeAttribute("role");
    element.removeAttribute("aria-live");
    element.removeAttribute("aria-atomic");
    element.removeAttribute("aria-busy");
    element.removeAttribute("data-status-kind");
    element.classList.remove("visible");
    element.classList.remove("busy");
    element.classList.remove("error");
    renderRawDetail(null);
    return true;
  }

  function rerender(localeContext) {
    requireUsable();
    if (currentState === null) return false;
    const nextState = Object.freeze({ ...currentState, localeContext });
    const rendered = renderMessage(nextState.message, nextState.localeContext);
    currentState = nextState;
    element.textContent = String(rendered ?? "");
    return true;
  }

  function destroy() {
    if (destroyed) return false;
    clear();
    destroyed = true;
    return true;
  }

  return Object.freeze({
    assertive: (message, publishOptions) => publish("assertive", message, publishOptions),
    busy: (message, publishOptions) => publish("busy", message, publishOptions),
    clear,
    destroy,
    error: (message, publishOptions) => publish("error", message, publishOptions),
    polite: (message, publishOptions) => publish("polite", message, publishOptions),
    rerender,
    get state() {
      return currentState;
    },
  });
}
