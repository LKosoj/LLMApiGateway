import { createInstance } from "i18next";
import ICU from "i18next-icu";

import { createAtomicBindingPlan } from "./binder.mjs";
import {
  GatewayI18nError,
  STORAGE_KEY,
  resolveInitialLocale,
  resolveRegisteredLocale,
  validateCatalog,
  validateRegistry,
} from "./contracts.mjs";

class RegistryCatalogBackend {
  constructor(registry, fetchFunction, baseUrl) {
    this.type = "backend";
    this.registry = registry;
    this.fetch = fetchFunction;
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.cache = new Map();
    this.locales = new Set(registry.locales.map((locale) => locale.code));
    this.namespaces = new Set(registry.namespaces);
  }

  init() {}

  read(locale, namespace, callback) {
    this.load(locale, namespace).then(
      (catalog) => callback(null, catalog),
      (error) => callback(error, false),
    );
  }

  async load(locale, namespace) {
    if (!this.locales.has(locale) || !this.namespaces.has(namespace)) {
      throw new GatewayI18nError(
        "CATALOG_REQUEST_NOT_REGISTERED",
        `Catalog is not registered: ${locale}/${namespace}`,
      );
    }
    const cacheKey = `${locale}/${namespace}`;
    if (!this.cache.has(cacheKey)) {
      const pending = this.#fetchCatalog(locale, namespace).catch((error) => {
        this.cache.delete(cacheKey);
        throw error;
      });
      this.cache.set(cacheKey, pending);
    }
    return this.cache.get(cacheKey);
  }

  async #fetchCatalog(locale, namespace) {
    let response;
    try {
      response = await this.fetch(
        `${this.baseUrl}/${encodeURIComponent(locale)}/${encodeURIComponent(namespace)}.json`,
        { cache: "no-store", credentials: "same-origin" },
      );
    } catch (error) {
      throw new GatewayI18nError(
        "CATALOG_FETCH_FAILED",
        `Catalog request failed: ${locale}/${namespace}`,
        { cause: error },
      );
    }
    if (!response || response.ok !== true) {
      throw new GatewayI18nError(
        "CATALOG_FETCH_FAILED",
        `Catalog request returned ${response?.status ?? "no response"}: ${locale}/${namespace}`,
      );
    }
    let catalog;
    try {
      catalog = await response.json();
    } catch (error) {
      throw new GatewayI18nError(
        "CATALOG_JSON_INVALID",
        `Catalog response is not valid JSON: ${locale}/${namespace}`,
        { cause: error },
      );
    }
    return validateCatalog(catalog, locale, namespace);
  }
}

function localeDirection(registry, locale) {
  return registry.locales.find((entry) => entry.code === locale).dir;
}

function splitTranslationKey(key) {
  if (typeof key !== "string") {
    throw new GatewayI18nError("TRANSLATION_KEY_INVALID", "Translation key must be a string");
  }
  const parts = key.split(":");
  if (parts.length !== 2 || !parts[0] || !parts[1]) {
    throw new GatewayI18nError(
      "TRANSLATION_KEY_INVALID",
      "Translation key must include an explicit namespace",
    );
  }
  const [namespace, path] = parts;
  if (!/^[a-z][a-z0-9_]*$/.test(namespace) || path.split(".").some((segment) => !segment)) {
    throw new GatewayI18nError("TRANSLATION_KEY_INVALID", "Translation key is malformed");
  }
  return [namespace, path];
}

async function buildI18nextInstance(registry, backend, locale, namespaces) {
  await Promise.all(namespaces.map((namespace) => backend.load(locale, namespace)));
  const instance = createInstance();
  instance.use(backend);
  instance.use(new ICU({
    bindI18n: false,
    bindI18nStore: false,
    memoize: true,
    memoizeFallback: false,
    parseErrorHandler(error, key) {
      throw new GatewayI18nError(
        "TRANSLATION_FORMAT_FAILED",
        `ICU formatting failed: ${key}`,
        { cause: error },
      );
    },
  }));
  await instance.init({
    defaultNS: false,
    fallbackLng: false,
    fallbackNS: false,
    initImmediate: false,
    ignoreJSONStructure: false,
    keySeparator: ".",
    lng: locale,
    load: "currentOnly",
    nonExplicitSupportedLngs: false,
    ns: namespaces,
    nsSeparator: ":",
    returnEmptyString: false,
    returnObjects: false,
    saveMissing: false,
    supportedLngs: registry.locales.map((entry) => entry.code),
  });
  return instance;
}

function stableOptions(options) {
  return JSON.stringify(Object.entries(options).sort(([left], [right]) => left.localeCompare(right)));
}

function readStoredLocale(storage) {
  try {
    return storage.getItem(STORAGE_KEY);
  } catch (error) {
    throw new GatewayI18nError("LOCALE_STORAGE_READ_FAILED", "Cannot read locale preference", {
      cause: error,
    });
  }
}

function writeStoredLocale(storage, locale) {
  try {
    storage.setItem(STORAGE_KEY, locale);
  } catch (error) {
    throw new GatewayI18nError("LOCALE_STORAGE_WRITE_FAILED", "Cannot save locale preference", {
      cause: error,
    });
  }
}

function restoreStoredLocale(storage, locale) {
  try {
    if (locale === null) storage.removeItem(STORAGE_KEY);
    else storage.setItem(STORAGE_KEY, locale);
  } catch (error) {
    throw new GatewayI18nError(
      "LOCALE_STORAGE_ROLLBACK_FAILED",
      "Cannot restore locale preference",
      { cause: error },
    );
  }
}

export class GatewayI18nRuntime {
  constructor(options) {
    this._registry = validateRegistry(options.registry);
    this._document = options.document;
    this._storage = options.storage;
    this._navigator = options.navigator;
    this._page = options.page
      ?? this._document?.documentElement?.dataset?.i18nPage
      ?? "runtime";
    const pageNamespaces = this._registry.pageNamespaces[this._page];
    if (!pageNamespaces) {
      throw new GatewayI18nError("PAGE_NOT_REGISTERED", `Page is not registered: ${this._page}`);
    }
    if (typeof options.fetch !== "function") {
      throw new GatewayI18nError("FETCH_UNAVAILABLE", "A fetch implementation is required");
    }
    if (!this._document?.documentElement) {
      throw new GatewayI18nError("DOCUMENT_UNAVAILABLE", "A browser document is required");
    }
    if (!this._storage) {
      throw new GatewayI18nError("LOCALE_STORAGE_UNAVAILABLE", "Locale storage is required");
    }
    this._backend = new RegistryCatalogBackend(
      this._registry,
      options.fetch,
      options.baseUrl ?? "/static/locales",
    );
    this._bindings = new Map();
    this._listeners = new Set();
    this._formatters = new Map();
    this._instance = null;
    this._locale = null;
    this._activeNamespaces = [...pageNamespaces];
    this.ready = this.#initialize();
    this._transitionTail = Promise.resolve();
  }

  get registry() {
    return this._registry;
  }

  get currentLocale() {
    return this._locale;
  }

  get dir() {
    return this._locale === null ? null : localeDirection(this._registry, this._locale);
  }

  async #initialize() {
    const locale = resolveInitialLocale(
      this._registry,
      this._storage,
      this._navigator?.languages ?? [],
    );
    const instance = await buildI18nextInstance(
      this._registry,
      this._backend,
      locale,
      this._activeNamespaces,
    );
    this._document.documentElement.setAttribute("lang", locale);
    this._document.documentElement.setAttribute("dir", localeDirection(this._registry, locale));
    this._instance = instance;
    this._locale = locale;
    return this;
  }

  #requireReady() {
    if (this._instance === null || this._locale === null) {
      throw new GatewayI18nError("RUNTIME_NOT_READY", "The i18n runtime is not ready");
    }
  }

  #enqueueTransition(operation) {
    const transition = this._transitionTail.then(async () => {
      await this.ready;
      return operation();
    });
    this._transitionTail = transition.then(
      () => undefined,
      () => undefined,
    );
    return transition;
  }

  #translateWith(
    instance,
    locale,
    key,
    values = {},
    loadedNamespaces = this._activeNamespaces,
  ) {
    const [namespace, path] = splitTranslationKey(key);
    if (!loadedNamespaces.includes(namespace)) {
      throw new GatewayI18nError(
        "TRANSLATION_NAMESPACE_NOT_LOADED",
        `Translation namespace is not loaded: ${namespace}`,
      );
    }
    const resource = instance.getResource(locale, namespace, path);
    if (typeof resource !== "string" || !resource.trim() || resource === key || resource === path) {
      throw new GatewayI18nError("TRANSLATION_MISSING", `Translation is missing: ${key}`);
    }
    let translated;
    try {
      translated = instance.t(path, { ...values, lng: locale, ns: namespace });
    } catch (error) {
      if (error instanceof GatewayI18nError) {
        throw error;
      }
      throw new GatewayI18nError("TRANSLATION_FORMAT_FAILED", `Translation failed: ${key}`, {
        cause: error,
      });
    }
    if (typeof translated !== "string" || translated.length === 0) {
      throw new GatewayI18nError("TRANSLATION_MISSING", `Translation is missing: ${key}`);
    }
    return translated;
  }

  t(key, values = {}) {
    this.#requireReady();
    return this.#translateWith(this._instance, this._locale, key, values);
  }

  bind(root = this._document, values = {}) {
    this.#requireReady();
    const plan = createAtomicBindingPlan(
      root,
      (key) => this.#translateWith(this._instance, this._locale, key, values),
    );
    plan.apply();
    const token = Symbol("gateway-i18n-binding");
    this._bindings.set(root, { token, values });
    return () => {
      if (this._bindings.get(root)?.token === token) {
        this._bindings.delete(root);
      }
    };
  }

  loadNamespaces(namespaces) {
    return this.#enqueueTransition(async () => {
      const requested = typeof namespaces === "string" ? [namespaces] : namespaces;
      if (!Array.isArray(requested) || requested.length === 0) {
        throw new GatewayI18nError("NAMESPACE_LIST_INVALID", "Namespaces must be a non-empty list");
      }
      const registered = new Set(this._registry.namespaces);
      if (requested.some((namespace) => !registered.has(namespace))) {
        throw new GatewayI18nError("NAMESPACE_NOT_REGISTERED", "Requested namespace is not registered");
      }
      const targetNamespaces = [...new Set([...this._activeNamespaces, ...requested])];
      if (targetNamespaces.length === this._activeNamespaces.length) {
        return;
      }
      const locale = this._locale;
      const candidate = await buildI18nextInstance(
        this._registry,
        this._backend,
        locale,
        targetNamespaces,
      );
      await this.#commit(candidate, locale, targetNamespaces, false);
    });
  }

  changeLanguage(requestedLocale) {
    return this.#enqueueTransition(async () => {
      const locale = resolveRegisteredLocale(this._registry, requestedLocale);
      if (locale === null) {
        throw new GatewayI18nError("LOCALE_NOT_REGISTERED", `Locale is not registered: ${requestedLocale}`);
      }
      if (locale === this._locale) {
        return;
      }
      const targetNamespaces = [...this._activeNamespaces];
      const candidate = await buildI18nextInstance(
        this._registry,
        this._backend,
        locale,
        targetNamespaces,
      );
      await this.#commit(candidate, locale, targetNamespaces, true);
    });
  }

  async #commit(candidate, locale, namespaces, persistLocale) {
    const previousLocale = this._locale;
    const previousInstance = this._instance;
    const previousNamespaces = this._activeNamespaces;
    const html = this._document.documentElement;
    const previousLang = html.getAttribute("lang");
    const previousDir = html.getAttribute("dir");
    const previousStored = persistLocale ? readStoredLocale(this._storage) : null;
    const plans = [];
    for (const [root, binding] of this._bindings) {
      plans.push(createAtomicBindingPlan(
        root,
        (key) => this.#translateWith(candidate, locale, key, binding.values, namespaces),
      ));
    }

    const applied = [];
    let storageChanged = false;
    try {
      if (persistLocale) {
        writeStoredLocale(this._storage, locale);
        storageChanged = true;
      }
      html.setAttribute("lang", locale);
      html.setAttribute("dir", localeDirection(this._registry, locale));
      for (const plan of plans) {
        plan.apply();
        applied.push(plan);
      }
      this._instance = candidate;
      this._locale = locale;
      this._activeNamespaces = [...namespaces];
      this._formatters.clear();
    } catch (error) {
      for (const plan of applied.reverse()) {
        plan.rollback();
      }
      if (previousLang === null) html.removeAttribute("lang");
      else html.setAttribute("lang", previousLang);
      if (previousDir === null) html.removeAttribute("dir");
      else html.setAttribute("dir", previousDir);
      let storageRollbackError = null;
      if (storageChanged) {
        try {
          restoreStoredLocale(this._storage, previousStored);
        } catch (rollbackError) {
          storageRollbackError = rollbackError;
        }
      }
      this._instance = previousInstance;
      this._locale = previousLocale;
      this._activeNamespaces = previousNamespaces;
      if (storageRollbackError !== null) {
        throw storageRollbackError;
      }
      if (error instanceof GatewayI18nError) {
        throw error;
      }
      throw new GatewayI18nError("LANGUAGE_COMMIT_FAILED", "Language change failed", {
        cause: error,
      });
    }
    if (locale !== previousLocale) {
      for (const listener of this._listeners) {
        try {
          listener(Object.freeze({ locale, dir: this.dir }));
        } catch (_error) {
          // A consumer listener must not corrupt an already committed locale transaction.
        }
      }
    }
  }

  subscribe(listener) {
    if (typeof listener !== "function") {
      throw new GatewayI18nError("LISTENER_INVALID", "Locale listener must be a function");
    }
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  }

  #formatter(kind, constructor, options) {
    this.#requireReady();
    const cacheKey = `${kind}:${this._locale}:${stableOptions(options)}`;
    if (!this._formatters.has(cacheKey)) {
      this._formatters.set(cacheKey, new constructor(this._locale, options));
    }
    return this._formatters.get(cacheKey);
  }

  formatNumber(value, options = {}) {
    return this.#formatter("number", Intl.NumberFormat, options).format(value);
  }

  formatCurrency(value, currency, options = {}) {
    return this.#formatter("currency", Intl.NumberFormat, {
      ...options,
      currency,
      style: "currency",
    }).format(value);
  }

  formatDate(value, options = {}) {
    return this.#formatter("date", Intl.DateTimeFormat, options).format(value);
  }

  formatRelativeTime(value, unit, options = {}) {
    return this.#formatter("relative", Intl.RelativeTimeFormat, options).format(value, unit);
  }

  getCollator(options = {}) {
    return this.#formatter("collator", Intl.Collator, options);
  }
}

export function createGatewayI18n(options = {}) {
  return new GatewayI18nRuntime({
    ...options,
    document: options.document ?? globalThis.document,
    fetch: options.fetch ?? globalThis.fetch?.bind(globalThis),
    navigator: options.navigator ?? globalThis.navigator,
    registry: options.registry,
    storage: options.storage ?? globalThis.localStorage,
  });
}
