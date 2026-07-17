import { IntlMessageFormat } from "intl-messageformat";

const MAX_LOCALES = 20;
const MAX_LOCALE_PATH_BYTES = 255;
const NAMESPACE_PATTERN = /^[a-z][a-z0-9_]*$/;
const PAGE_PATTERN = /^[a-z][a-z0-9-]*$/;
const MESSAGE_ARGUMENT_TYPES = new Set([1, 2, 3, 4, 5, 6]);
const MESSAGE_ARGUMENT_TYPE_NAMES = Object.freeze({
  1: "argument",
  2: "number",
  3: "date",
  4: "time",
  5: "select",
});
const RICH_TEXT_PATTERN = /<(?:!--|!doctype\b|\/?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?\/?>)/i;

export const STORAGE_KEY = "llmgateway:locale";

export class GatewayI18nError extends Error {
  constructor(code, message, options = {}) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = "GatewayI18nError";
    Object.defineProperty(this, "code", {
      configurable: false,
      enumerable: true,
      value: code,
      writable: false,
    });
  }
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

export function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) {
      deepFreeze(child);
    }
    Object.freeze(value);
  }
  return value;
}

export function canonicalizeLocale(code, errorCode = "LOCALE_INVALID") {
  if (typeof code !== "string" || !code) {
    throw new GatewayI18nError(errorCode, "Locale code must be a non-empty string");
  }
  try {
    const canonical = Intl.getCanonicalLocales(code);
    if (canonical.length !== 1) {
      throw new RangeError("Expected one locale");
    }
    return canonical[0];
  } catch (error) {
    throw new GatewayI18nError(errorCode, `Invalid locale code: ${code}`, {
      cause: error,
    });
  }
}

function requireExactKeys(value, expected, code, subject) {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    throw new GatewayI18nError(code, `${subject} has an invalid schema`);
  }
}

export function validateRegistry(input) {
  if (!isPlainObject(input)) {
    throw new GatewayI18nError("REGISTRY_INVALID", "Locale registry must be an object");
  }
  requireExactKeys(
    input,
    ["schemaVersion", "defaultLocale", "locales", "namespaces", "pageNamespaces"],
    "REGISTRY_INVALID",
    "Locale registry",
  );
  if (input.schemaVersion !== 1) {
    throw new GatewayI18nError("REGISTRY_SCHEMA_VERSION", "Unsupported locale registry schema");
  }
  if (!Array.isArray(input.locales) || input.locales.length === 0) {
    throw new GatewayI18nError("REGISTRY_LOCALES_INVALID", "Locale registry has no locales");
  }
  if (input.locales.length > MAX_LOCALES) {
    throw new GatewayI18nError(
      "REGISTRY_LOCALE_LIMIT",
      `Locale registry exceeds the ${MAX_LOCALES}-locale limit`,
    );
  }

  const localeCodes = new Set();
  const locales = input.locales.map((locale) => {
    if (!isPlainObject(locale)) {
      throw new GatewayI18nError("REGISTRY_LOCALE_INVALID", "Locale entry must be an object");
    }
    requireExactKeys(
      locale,
      ["code", "nativeLabel", "dir"],
      "REGISTRY_LOCALE_INVALID",
      "Locale entry",
    );
    const canonical = canonicalizeLocale(locale.code, "REGISTRY_LOCALE_INVALID");
    if (canonical !== locale.code) {
      throw new GatewayI18nError(
        "REGISTRY_LOCALE_NOT_CANONICAL",
        `Locale code must be canonical BCP 47: ${locale.code}`,
      );
    }
    if (new TextEncoder().encode(canonical).byteLength > MAX_LOCALE_PATH_BYTES) {
      throw new GatewayI18nError(
        "REGISTRY_LOCALE_PATH_TOO_LONG",
        `Locale code exceeds ${MAX_LOCALE_PATH_BYTES} UTF-8 bytes`,
      );
    }
    if (localeCodes.has(canonical)) {
      throw new GatewayI18nError("REGISTRY_LOCALE_DUPLICATE", `Duplicate locale: ${canonical}`);
    }
    if (typeof locale.nativeLabel !== "string" || !locale.nativeLabel.trim()) {
      throw new GatewayI18nError("REGISTRY_LOCALE_INVALID", "Locale nativeLabel is required");
    }
    if (locale.dir !== "ltr" && locale.dir !== "rtl") {
      throw new GatewayI18nError("REGISTRY_LOCALE_INVALID", "Locale dir must be ltr or rtl");
    }
    localeCodes.add(canonical);
    return {
      code: canonical,
      nativeLabel: locale.nativeLabel,
      dir: locale.dir,
    };
  });

  if (!localeCodes.has(input.defaultLocale)) {
    throw new GatewayI18nError(
      "REGISTRY_DEFAULT_INVALID",
      "Default locale must reference a registered locale",
    );
  }
  if (!Array.isArray(input.namespaces) || input.namespaces.length === 0) {
    throw new GatewayI18nError("REGISTRY_NAMESPACES_INVALID", "Namespace registry is empty");
  }
  const namespaces = [];
  const namespaceSet = new Set();
  for (const namespace of input.namespaces) {
    if (typeof namespace !== "string" || !NAMESPACE_PATTERN.test(namespace)) {
      throw new GatewayI18nError("REGISTRY_NAMESPACE_INVALID", "Invalid namespace name");
    }
    if (namespaceSet.has(namespace)) {
      throw new GatewayI18nError("REGISTRY_NAMESPACE_DUPLICATE", `Duplicate namespace: ${namespace}`);
    }
    namespaceSet.add(namespace);
    namespaces.push(namespace);
  }
  if (!isPlainObject(input.pageNamespaces) || Object.keys(input.pageNamespaces).length === 0) {
    throw new GatewayI18nError("REGISTRY_PAGES_INVALID", "Page namespace mapping is empty");
  }
  const pageNamespaces = {};
  for (const [page, mappedNamespaces] of Object.entries(input.pageNamespaces)) {
    if (!PAGE_PATTERN.test(page) || !Array.isArray(mappedNamespaces) || mappedNamespaces.length === 0) {
      throw new GatewayI18nError("REGISTRY_PAGE_INVALID", `Invalid page mapping: ${page}`);
    }
    const unique = new Set(mappedNamespaces);
    if (unique.size !== mappedNamespaces.length || mappedNamespaces.some((name) => !namespaceSet.has(name))) {
      throw new GatewayI18nError("REGISTRY_PAGE_INVALID", `Invalid namespaces for page: ${page}`);
    }
    pageNamespaces[page] = [...mappedNamespaces];
  }

  return deepFreeze({
    schemaVersion: 1,
    defaultLocale: input.defaultLocale,
    locales,
    namespaces,
    pageNamespaces,
  });
}

function flattenMessages(value, prefix = "", result = new Map()) {
  if (!isPlainObject(value)) {
    throw new GatewayI18nError("CATALOG_INVALID", "Catalog must be a JSON object");
  }
  for (const [key, child] of Object.entries(value)) {
    if (!key || key.includes(".") || key.includes(":")) {
      throw new GatewayI18nError(
        "CATALOG_KEY_INVALID",
        `Catalog key segment is invalid: ${prefix ? `${prefix}.` : ""}${key}`,
      );
    }
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof child === "string") {
      result.set(path, child);
    } else if (isPlainObject(child)) {
      flattenMessages(child, path, result);
    } else {
      throw new GatewayI18nError(
        "CATALOG_NON_STRING_MESSAGE",
        `Catalog message must be a string: ${path}`,
      );
    }
  }
  return result;
}

function collectArguments(nodes, result = new Map()) {
  for (const node of nodes) {
    if (MESSAGE_ARGUMENT_TYPES.has(node.type)) {
      const type = node.type === 6
        ? (node.pluralType === "ordinal" ? "selectordinal" : "plural")
        : MESSAGE_ARGUMENT_TYPE_NAMES[node.type];
      result.set(`${node.value}\u0000${type}`, [node.value, type]);
    }
    if (node.options) {
      for (const option of Object.values(node.options)) {
        collectArguments(option.value, result);
      }
    }
    if (node.children) {
      collectArguments(node.children, result);
    }
  }
  return result;
}

export function analyzeCatalog(catalog, locale, namespace) {
  const messages = flattenMessages(catalog);
  if (messages.size === 0) {
    throw new GatewayI18nError("CATALOG_EMPTY", `Catalog is empty: ${locale}/${namespace}`);
  }
  const argumentsByKey = new Map();
  for (const [key, message] of messages) {
    if (!message.trim()) {
      throw new GatewayI18nError(
        "CATALOG_EMPTY_MESSAGE",
        `Catalog message is empty: ${locale}/${namespace}:${key}`,
      );
    }
    const leaf = key.slice(key.lastIndexOf(".") + 1);
    if (message === key || message === leaf) {
      throw new GatewayI18nError(
        "CATALOG_KEY_AS_VALUE",
        `Catalog message repeats its key: ${locale}/${namespace}:${key}`,
      );
    }
    if (RICH_TEXT_PATTERN.test(message)) {
      throw new GatewayI18nError(
        "CATALOG_RICH_TEXT_FORBIDDEN",
        `Catalog message contains rich text: ${locale}/${namespace}:${key}`,
      );
    }
    try {
      const formatter = new IntlMessageFormat(message, locale, undefined, {
        ignoreTag: true,
      });
      argumentsByKey.set(
        key,
        [...collectArguments(formatter.getAst()).values()].sort(([leftName, leftType], [rightName, rightType]) => (
          leftName.localeCompare(rightName) || leftType.localeCompare(rightType)
        )),
      );
    } catch (error) {
      throw new GatewayI18nError(
        "CATALOG_INVALID_ICU",
        `Catalog contains invalid ICU: ${locale}/${namespace}:${key}`,
        { cause: error },
      );
    }
  }
  return { argumentsByKey, keys: [...messages.keys()].sort() };
}

export function validateCatalog(catalog, locale, namespace) {
  analyzeCatalog(catalog, locale, namespace);
  return deepFreeze(structuredClone(catalog));
}

export function resolveRegisteredLocale(registry, requested) {
  let canonical;
  try {
    canonical = canonicalizeLocale(requested);
  } catch (_error) {
    return null;
  }
  const registered = new Set(registry.locales.map((locale) => locale.code));
  if (registered.has(canonical)) {
    return canonical;
  }
  const base = canonical.split("-")[0];
  return registered.has(base) ? base : null;
}

export function resolveInitialLocale(registry, storage, browserLanguages) {
  let saved;
  try {
    saved = storage.getItem(STORAGE_KEY);
  } catch (error) {
    throw new GatewayI18nError("LOCALE_STORAGE_READ_FAILED", "Cannot read locale preference", {
      cause: error,
    });
  }
  const registered = new Set(registry.locales.map((locale) => locale.code));
  if (saved !== null) {
    if (registered.has(saved)) {
      return saved;
    }
    try {
      storage.removeItem(STORAGE_KEY);
    } catch (error) {
      throw new GatewayI18nError(
        "LOCALE_STORAGE_WRITE_FAILED",
        "Cannot remove stale locale preference",
        { cause: error },
      );
    }
  }

  const canonicalLanguages = [];
  for (const language of browserLanguages ?? []) {
    try {
      canonicalLanguages.push(canonicalizeLocale(language));
    } catch (_error) {
      // Browsers may expose legacy or malformed extension tags; ignore unknown input.
    }
  }
  for (const language of canonicalLanguages) {
    if (registered.has(language)) {
      return language;
    }
  }
  for (const language of canonicalLanguages) {
    const base = language.split("-")[0];
    if (registered.has(base)) {
      return base;
    }
  }
  return registry.defaultLocale;
}
