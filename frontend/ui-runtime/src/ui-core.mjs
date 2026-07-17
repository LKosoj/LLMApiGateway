export function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function toFiniteNumber(value, fallback = 0) {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : fallback;
}

export function formatInteger(value) {
  return String(Math.trunc(toFiniteNumber(value, 0)));
}

export function showToast(element, message, options = {}) {
  if (!element) {
    return null;
  }
  const timeoutMs = toFiniteNumber(options.timeoutMs, 3000);
  const token = Symbol("toast");
  element._gatewayToastToken = token;
  element.textContent = message;
  element.classList.toggle("error", Boolean(options.isError));
  element.classList.add("visible");
  if (element._gatewayToastTimer) {
    clearTimeout(element._gatewayToastTimer);
  }
  element._gatewayToastTimer = setTimeout(() => {
    if (element._gatewayToastToken === token) {
      element.classList.remove("visible");
    }
  }, timeoutMs);
  return token;
}

export async function loadProductVersion(options = {}) {
  const document = options.document ?? globalThis.document;
  const fetchFunction = options.fetch ?? globalThis.fetch;
  const eventTarget = options.eventTarget ?? globalThis;
  const elements = Array.from(document.querySelectorAll("[data-product-version]"));
  if (elements.length === 0) {
    return null;
  }
  if (typeof options.translate !== "function") {
    throw new TypeError("loadProductVersion requires a translate function");
  }
  if (options.subscribe !== undefined && typeof options.subscribe !== "function") {
    throw new TypeError("loadProductVersion subscribe must be a function");
  }

  let version = null;
  let loadError = null;
  try {
    const response = await fetchFunction("/health", {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!response.ok) {
      throw new Error(`Health endpoint returned HTTP ${response.status}`);
    }
    version = response.headers.get("X-LLMGateway-Build-Version")?.trim() ?? null;
    if (!version) {
      throw new Error("Health endpoint omitted the product version header");
    }
  } catch (error) {
    version = null;
    loadError = error;
  }

  const render = () => {
    const key = version === null
      ? "common:productVersion.unavailable"
      : "common:productVersion.ready";
    const values = version === null ? {} : { version };
    const label = options.translate(key, values);
    for (const element of elements) {
      element.textContent = label;
      element.dataset.productVersionState = version === null ? "error" : "ready";
    }
  };

  render();
  options.subscribe?.(render);

  if (loadError !== null) {
    eventTarget.dispatchEvent(new CustomEvent("gateway:product-version-error", { detail: loadError }));
    console.error("Failed to load the LLM Gateway product version.", loadError);
  }
  return version;
}
