export function createLocaleSelector(select, options = {}) {
  const i18n = options.i18n;
  if (!select?.ownerDocument || typeof select.addEventListener !== "function") {
    throw new TypeError("locale select element is required");
  }
  if (!i18n?.ready || typeof i18n.changeLanguage !== "function") {
    throw new TypeError("i18n runtime is required");
  }
  if (typeof options.onError !== "function") {
    throw new TypeError("locale error handler is required");
  }

  let destroyed = false;
  let transition = Promise.resolve();
  let generation = 0;
  let pendingCount = 0;
  let latestOutcome = null;
  const initialDisabled = select.disabled;
  const initialLocaleState = Object.hasOwn(select.dataset, "localeState")
    ? select.dataset.localeState
    : null;

  function renderOptions() {
    const fragment = select.ownerDocument.createDocumentFragment();
    for (const locale of i18n.registry.locales) {
      const option = select.ownerDocument.createElement("option");
      option.value = locale.code;
      option.textContent = locale.nativeLabel;
      fragment.appendChild(option);
    }
    select.replaceChildren(fragment);
    select.value = i18n.currentLocale;
  }

  async function changeLanguage(locale) {
    if (destroyed) {
      throw new Error("locale selector is destroyed");
    }
    const operation = ++generation;
    pendingCount += 1;
    select.disabled = true;
    select.dataset.localeState = "changing";
    try {
      await i18n.changeLanguage(locale);
      if (!destroyed && operation === generation) {
        select.value = i18n.currentLocale;
        latestOutcome = { operation, state: "ready" };
      }
    } catch (error) {
      if (!destroyed && operation === generation) {
        select.value = i18n.currentLocale;
        latestOutcome = { operation, state: "error" };
        options.onError(error);
      }
      throw error;
    } finally {
      pendingCount -= 1;
      if (!destroyed && pendingCount === 0) {
        select.disabled = initialDisabled;
        if (latestOutcome?.operation === generation) {
          select.dataset.localeState = latestOutcome.state;
        }
      }
    }
  }

  function handleChange() {
    transition = changeLanguage(select.value);
    void transition.catch(() => undefined);
  }

  const ready = Promise.resolve(i18n.ready).then(() => {
    if (destroyed) {
      return;
    }
    renderOptions();
    select.dataset.localeState = "ready";
    select.addEventListener("change", handleChange);
  });

  return Object.freeze({
    ready,
    changeLanguage,
    get transition() {
      return transition;
    },
    destroy() {
      if (destroyed) {
        return;
      }
      destroyed = true;
      generation += 1;
      select.removeEventListener("change", handleChange);
      select.disabled = initialDisabled;
      if (initialLocaleState === null) {
        delete select.dataset.localeState;
      } else {
        select.dataset.localeState = initialLocaleState;
      }
    },
  });
}
