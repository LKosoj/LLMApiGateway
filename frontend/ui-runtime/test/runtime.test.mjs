import assert from "node:assert/strict";
import test from "node:test";

import {
  GatewayI18nError,
  STORAGE_KEY,
  analyzeCatalog,
  createGatewayI18n,
  resolveInitialLocale,
  validateCatalog,
  validateRegistry,
} from "../src/index.mjs";
import {
  FakeDocument,
  FakeElement,
  FakeStorage,
  makeFetch,
  makeRegistry,
} from "./helpers.mjs";

const COMMON_CATALOGS = {
  "/static/locales/en/common.json": {
    items: "{count, plural, one {# item} other {# items}}",
    unsafe: "5 < 10 & plain text",
  },
  "/static/locales/ru/common.json": {
    items: "{count, plural, one {# элемент} few {# элемента} many {# элементов} other {# элемента}}",
    unsafe: "5 < 10 & обычный текст",
  },
};

function makeLongPrivateUseLocale(lastSubtagLength) {
  const subtags = Array.from({ length: 27 }, (_value, index) => (
    `${index.toString(36).padStart(2, "0")}abcdef`.slice(0, 8)
  ));
  subtags.push("z".repeat(lastSubtagLength));
  return `en-x-${subtags.join("-")}`;
}

test("registry accepts twenty locales and rejects the twenty-first", () => {
  const twenty = Array.from({ length: 20 }, (_value, index) => ({
    code: index === 0 ? "en" : `q${String.fromCharCode(97 + index)}`,
    nativeLabel: `Locale ${index}`,
    dir: index === 19 ? "rtl" : "ltr",
  }));
  assert.equal(validateRegistry(makeRegistry(twenty)).locales.length, 20);

  assert.throws(
    () => validateRegistry(makeRegistry([...twenty, {
      code: "zz",
      nativeLabel: "Locale 21",
      dir: "ltr",
    }])),
    (error) => error instanceof GatewayI18nError
      && error.code === "REGISTRY_LOCALE_LIMIT",
  );
});

test("initial locale resolution removes stale preference then uses browser base", () => {
  const storage = new FakeStorage({ [STORAGE_KEY]: "stale" });
  const locale = resolveInitialLocale(
    validateRegistry(makeRegistry()),
    storage,
    ["de-DE", "ru-RU"],
  );

  assert.equal(locale, "ru");
  assert.equal(STORAGE_KEY, "llmgateway:locale");
  assert.equal(storage.getItem(STORAGE_KEY), null);
});

test("registry accepts canonical extension locale tags", () => {
  const registry = makeRegistry([
    { code: "en", nativeLabel: "English", dir: "ltr" },
    { code: "en-US-u-nu-latn", nativeLabel: "English with Latin digits", dir: "ltr" },
  ]);
  assert.equal(validateRegistry(registry).locales[1].code, "en-US-u-nu-latn");
});

test("registry delegates an 83-character canonical locale tag to Intl", () => {
  const longLocale = "en-u-ca-gregoryx-co-phonebk-hc-h23-kf-upper-kn-nu-latn-tz-usnyc-x-abcdefgh-ijklmnop";
  assert.equal(longLocale.length, 83);
  const registry = makeRegistry([
    { code: "en", nativeLabel: "English", dir: "ltr" },
    { code: longLocale, nativeLabel: "Long locale", dir: "ltr" },
  ]);

  assert.equal(validateRegistry(registry).locales[1].code, longLocale);
});

test("registry accepts a 255-byte locale segment and rejects 256 bytes", () => {
  const boundaryLocale = makeLongPrivateUseLocale(7);
  const overLimitLocale = makeLongPrivateUseLocale(8);
  assert.equal(new TextEncoder().encode(boundaryLocale).byteLength, 255);
  assert.equal(new TextEncoder().encode(overLimitLocale).byteLength, 256);
  assert.equal(Intl.getCanonicalLocales(boundaryLocale)[0], boundaryLocale);
  assert.equal(Intl.getCanonicalLocales(overLimitLocale)[0], overLimitLocale);

  assert.equal(validateRegistry(makeRegistry([
    { code: "en", nativeLabel: "English", dir: "ltr" },
    { code: boundaryLocale, nativeLabel: "Boundary locale", dir: "ltr" },
  ])).locales[1].code, boundaryLocale);
  assert.throws(
    () => validateRegistry(makeRegistry([
      { code: "en", nativeLabel: "English", dir: "ltr" },
      { code: overLimitLocale, nativeLabel: "Over-limit locale", dir: "ltr" },
    ])),
    (error) => error instanceof GatewayI18nError
      && error.code === "REGISTRY_LOCALE_PATH_TOO_LONG",
  );
});

test("catalog validation rejects empty, non-string, key-valued and invalid ICU messages", () => {
  for (const [catalog, code] of [
    [{ empty: "" }, "CATALOG_EMPTY_MESSAGE"],
    [{ nested: { invalid: 42 } }, "CATALOG_NON_STRING_MESSAGE"],
    [{ repeated: "repeated" }, "CATALOG_KEY_AS_VALUE"],
    [{ broken: "{count, plural, one {one}" }, "CATALOG_INVALID_ICU"],
    [{ markup: "Use <strong>bold</strong> text" }, "CATALOG_RICH_TEXT_FORBIDDEN"],
    [{ "": "Empty raw key" }, "CATALOG_KEY_INVALID"],
    [{ "literal.dot": "Literal dot key" }, "CATALOG_KEY_INVALID"],
    [{ "literal:colon": "Literal colon key" }, "CATALOG_KEY_INVALID"],
  ]) {
    assert.throws(
      () => validateCatalog(catalog, "en", "common"),
      (error) => error instanceof GatewayI18nError && error.code === code,
    );
  }
  assert.doesNotThrow(() => validateCatalog(
    { section: { title: "Nested heading" } },
    "en",
    "common",
  ));
});

test("catalog argument parity includes ICU type but ignores locale plural categories", () => {
  const englishPlural = analyzeCatalog(
    { items: "{count, plural, one {# item} other {# items}}" },
    "en",
    "common",
  );
  const russianPlural = analyzeCatalog(
    { items: "{count, plural, one {# item} few {# items} many {# items} other {# items}}" },
    "ru",
    "common",
  );
  const numberArgument = analyzeCatalog(
    { items: "{count, number}" },
    "en",
    "common",
  );
  const selectArgument = analyzeCatalog(
    { items: "{count, select, one {One} other {Other}}" },
    "ru",
    "common",
  );

  assert.deepEqual(
    englishPlural.argumentsByKey.get("items"),
    russianPlural.argumentsByKey.get("items"),
  );
  assert.notDeepEqual(
    numberArgument.argumentsByKey.get("items"),
    selectArgument.argumentsByKey.get("items"),
  );
});

test("runtime loads only active locale namespaces and formats Russian plurals", async () => {
  const calls = [];
  const runtime = createGatewayI18n({
    registry: makeRegistry(),
    fetch: makeFetch(COMMON_CATALOGS, calls),
    document: new FakeDocument(),
    storage: new FakeStorage(),
    navigator: { languages: ["ru-RU"] },
  });
  await runtime.ready;

  assert.deepEqual(calls, ["/static/locales/ru/common.json"]);
  assert.equal(runtime.currentLocale, "ru");
  assert.equal(runtime.t("common:items", { count: 1 }), "1 элемент");
  assert.equal(runtime.t("common:items", { count: 2 }), "2 элемента");
  assert.equal(runtime.t("common:items", { count: 5 }), "5 элементов");
  assert.throws(
    () => runtime.t("common:missing"),
    (error) => error instanceof GatewayI18nError
      && error.code === "TRANSLATION_MISSING",
  );
  assert.throws(
    () => runtime.t("common:items"),
    (error) => error instanceof GatewayI18nError
      && error.code === "TRANSLATION_FORMAT_FAILED",
  );
  for (const invalidKey of [
    "common:items:extra",
    "common:.items",
    "common:items.",
    "common:items..count",
  ]) {
    assert.throws(
      () => runtime.t(invalidKey),
      (error) => error instanceof GatewayI18nError
        && error.code === "TRANSLATION_KEY_INVALID",
    );
  }
  assert(Object.isFrozen(runtime.registry));
  assert(Object.isFrozen(runtime.registry.locales));
});

test("formatted output may equal the translation path or qualified key", async () => {
  const runtime = createGatewayI18n({
    registry: makeRegistry([{ code: "en", nativeLabel: "English", dir: "ltr" }]),
    fetch: makeFetch({
      "/static/locales/en/common.json": { name: "{value}" },
    }),
    document: new FakeDocument(),
    storage: new FakeStorage(),
    navigator: { languages: ["en"] },
  });
  await runtime.ready;

  assert.equal(runtime.t("common:name", { value: "name" }), "name");
  assert.equal(
    runtime.t("common:name", { value: "common:name" }),
    "common:name",
  );
});

test("loadNamespaces validates a registered namespace before making it bindable", async () => {
  const registry = {
    ...makeRegistry(),
    namespaces: ["common", "auth"],
    pageNamespaces: { runtime: ["common"] },
  };
  const authNode = new FakeElement({ "data-i18n": "auth:title" });
  const document = new FakeDocument([authNode]);
  const runtime = createGatewayI18n({
    registry,
    fetch: makeFetch({
      "/static/locales/en/common.json": COMMON_CATALOGS["/static/locales/en/common.json"],
      "/static/locales/en/auth.json": { title: "Sign in" },
    }),
    document,
    storage: new FakeStorage(),
    navigator: { languages: ["en"] },
  });
  await runtime.ready;

  assert.throws(
    () => runtime.bind(document),
    (error) => error instanceof GatewayI18nError
      && error.code === "TRANSLATION_NAMESPACE_NOT_LOADED",
  );
  await runtime.loadNamespaces("auth");
  runtime.bind(document);
  assert.equal(authNode.textContent, "Sign in");
});

test("namespace loading does not read or roll back inaccessible locale storage", async () => {
  const registry = {
    ...makeRegistry(),
    namespaces: ["common", "auth"],
    pageNamespaces: { runtime: ["common"] },
  };
  const storage = new FakeStorage();
  const authNode = new FakeElement({ "data-i18n": "auth:title" });
  const document = new FakeDocument([authNode]);
  const runtime = createGatewayI18n({
    registry,
    fetch: makeFetch({
      "/static/locales/en/common.json": COMMON_CATALOGS["/static/locales/en/common.json"],
      "/static/locales/en/auth.json": { title: "Sign in" },
    }),
    document,
    storage,
    navigator: { languages: ["en"] },
  });
  await runtime.ready;
  storage.getItem = () => {
    throw new DOMException("storage blocked", "SecurityError");
  };
  storage.setItem = storage.getItem;
  storage.removeItem = storage.getItem;

  await runtime.loadNamespaces("auth");
  runtime.bind(document);

  assert.equal(authNode.textContent, "Sign in");
});

test("language commit exposes a typed locale-storage read failure", async () => {
  const storage = new FakeStorage();
  const runtime = createGatewayI18n({
    registry: makeRegistry(),
    fetch: makeFetch(COMMON_CATALOGS),
    document: new FakeDocument(),
    storage,
    navigator: { languages: ["en"] },
  });
  await runtime.ready;
  storage.getItem = () => {
    throw new DOMException("storage blocked", "SecurityError");
  };

  await assert.rejects(
    runtime.changeLanguage("ru"),
    (error) => error instanceof GatewayI18nError
      && error.code === "LOCALE_STORAGE_READ_FAILED",
  );
  assert.equal(runtime.currentLocale, "en");
});

test("language commit exposes typed locale-storage write and rollback failures", async () => {
  const writeStorage = new FakeStorage();
  const writeRuntime = createGatewayI18n({
    registry: makeRegistry(),
    fetch: makeFetch(COMMON_CATALOGS),
    document: new FakeDocument(),
    storage: writeStorage,
    navigator: { languages: ["en"] },
  });
  await writeRuntime.ready;
  writeStorage.setItem = () => {
    throw new DOMException("storage blocked", "SecurityError");
  };

  await assert.rejects(
    writeRuntime.changeLanguage("ru"),
    (error) => error instanceof GatewayI18nError
      && error.code === "LOCALE_STORAGE_WRITE_FAILED",
  );

  const rollbackStorage = new FakeStorage({ [STORAGE_KEY]: "en" });
  const rollbackDocument = new FakeDocument();
  const rollbackRuntime = createGatewayI18n({
    registry: makeRegistry(),
    fetch: makeFetch(COMMON_CATALOGS),
    document: rollbackDocument,
    storage: rollbackStorage,
    navigator: { languages: ["en"] },
  });
  await rollbackRuntime.ready;
  let storageWrites = 0;
  const setItem = rollbackStorage.setItem.bind(rollbackStorage);
  rollbackStorage.setItem = (key, value) => {
    storageWrites += 1;
    if (storageWrites === 2) {
      throw new DOMException("rollback blocked", "SecurityError");
    }
    setItem(key, value);
  };
  const setAttribute = rollbackDocument.documentElement.setAttribute.bind(
    rollbackDocument.documentElement,
  );
  rollbackDocument.documentElement.setAttribute = (name, value) => {
    if (name === "lang" && value === "ru") {
      throw new Error("synthetic DOM failure");
    }
    setAttribute(name, value);
  };

  await assert.rejects(
    rollbackRuntime.changeLanguage("ru"),
    (error) => error instanceof GatewayI18nError
      && error.code === "LOCALE_STORAGE_ROLLBACK_FAILED",
  );
  assert.equal(rollbackRuntime.currentLocale, "en");
});

test("stale bind unsubscribe cannot remove a newer registration", async () => {
  const node = new FakeElement({ "data-i18n": "common:items" });
  const document = new FakeDocument([node]);
  const runtime = createGatewayI18n({
    registry: makeRegistry(),
    fetch: makeFetch(COMMON_CATALOGS),
    document,
    storage: new FakeStorage(),
    navigator: { languages: ["en"] },
  });
  await runtime.ready;
  const staleUnsubscribe = runtime.bind(document, { count: 1 });
  const currentUnsubscribe = runtime.bind(document, { count: 2 });

  staleUnsubscribe();
  await runtime.changeLanguage("ru");
  assert.equal(node.textContent, "2 элемента");

  currentUnsubscribe();
  await runtime.changeLanguage("en");
  assert.equal(node.textContent, "2 элемента");
});

test("failed language change preserves locale, preference, document and bound DOM", async () => {
  const node = new FakeElement({ "data-i18n": "common:items" });
  const document = new FakeDocument([node]);
  const storage = new FakeStorage({ [STORAGE_KEY]: "ru" });
  const runtime = createGatewayI18n({
    registry: makeRegistry(),
    fetch: makeFetch({
      "/static/locales/ru/common.json": COMMON_CATALOGS["/static/locales/ru/common.json"],
    }),
    document,
    storage,
    navigator: { languages: ["ru"] },
  });
  await runtime.ready;
  runtime.bind(document, { count: 2 });
  const before = node.textContent;

  await assert.rejects(
    runtime.changeLanguage("en"),
    (error) => error instanceof GatewayI18nError
      && error.code === "CATALOG_FETCH_FAILED",
  );
  assert.equal(runtime.currentLocale, "ru");
  assert.equal(storage.getItem(STORAGE_KEY), "ru");
  assert.equal(document.documentElement.getAttribute("lang"), "ru");
  assert.equal(document.documentElement.getAttribute("dir"), "ltr");
  assert.equal(node.textContent, before);
});

test("DOM commit failure rolls back storage, html attributes and earlier text writes", async () => {
  const node = new FakeElement({
    "data-i18n": "common:unsafe",
    "data-i18n-title": "common:items",
  });
  const document = new FakeDocument([node]);
  const storage = new FakeStorage({ [STORAGE_KEY]: "ru" });
  const runtime = createGatewayI18n({
    registry: makeRegistry(),
    fetch: makeFetch(COMMON_CATALOGS),
    document,
    storage,
    navigator: { languages: ["ru"] },
  });
  await runtime.ready;
  runtime.bind(document, { count: 2 });
  const beforeText = node.textContent;
  const beforeTitle = node.getAttribute("title");
  const setAttribute = node.setAttribute.bind(node);
  let rejectTitleOnce = true;
  node.setAttribute = (name, value) => {
    if (name === "title" && rejectTitleOnce) {
      rejectTitleOnce = false;
      throw new Error("synthetic DOM failure");
    }
    setAttribute(name, value);
  };

  await assert.rejects(
    runtime.changeLanguage("en"),
    (error) => error instanceof GatewayI18nError
      && error.code === "BIND_APPLY_FAILED",
  );
  assert.equal(runtime.currentLocale, "ru");
  assert.equal(storage.getItem(STORAGE_KEY), "ru");
  assert.equal(document.documentElement.getAttribute("lang"), "ru");
  assert.equal(node.textContent, beforeText);
  assert.equal(node.getAttribute("title"), beforeTitle);

  await runtime.changeLanguage("en");
  assert.equal(runtime.currentLocale, "en");
});

test("successful language change reuses nodes and clears locale formatter cache", async () => {
  const node = new FakeElement({
    "data-i18n": "common:unsafe",
    "data-i18n-title": "common:items",
  });
  node.value = "unsaved";
  node.scrollTop = 31;
  const document = new FakeDocument([node]);
  document.activeElement = node;
  const storage = new FakeStorage({ [STORAGE_KEY]: "ru" });
  const runtime = createGatewayI18n({
    registry: makeRegistry(),
    fetch: makeFetch(COMMON_CATALOGS),
    document,
    storage,
    navigator: { languages: ["ru"] },
  });
  await runtime.ready;
  runtime.bind(document, { count: 2 });
  const russianCollator = runtime.getCollator({ sensitivity: "base" });
  assert.equal(runtime.getCollator({ sensitivity: "base" }), russianCollator);

  await runtime.changeLanguage("en");

  assert.equal(runtime.currentLocale, "en");
  assert.equal(runtime.dir, "ltr");
  assert.equal(storage.getItem(STORAGE_KEY), "en");
  assert.equal(document.documentElement.getAttribute("lang"), "en");
  assert.equal(node.textContent, "5 < 10 & plain text");
  assert.equal(node.getAttribute("title"), "2 items");
  assert.equal(node.innerHTMLWrites, 0);
  assert.equal(node.value, "unsaved");
  assert.equal(node.scrollTop, 31);
  assert.equal(document.activeElement, node);
  assert.notEqual(runtime.getCollator({ sensitivity: "base" }), russianCollator);
  assert.notEqual(runtime.formatNumber(1234.5), "");
  assert.match(runtime.formatCurrency(12.5, "USD"), /12|13/);
  assert.notEqual(runtime.formatDate(Date.UTC(2026, 0, 2)), "");
  assert.notEqual(runtime.formatRelativeTime(-1, "day"), "");
});

test("synthetic RTL locale updates lang and direction without changing page identity", async () => {
  const registry = makeRegistry([
    { code: "en", nativeLabel: "English", dir: "ltr" },
    { code: "ar", nativeLabel: "العربية", dir: "rtl" },
  ]);
  const document = new FakeDocument();
  const runtime = createGatewayI18n({
    registry,
    fetch: makeFetch({
      "/static/locales/ar/common.json": { items: "{count} عناصر", unsafe: "آمن" },
    }),
    document,
    storage: new FakeStorage(),
    navigator: { languages: ["ar-EG"] },
  });
  await runtime.ready;

  assert.equal(runtime.currentLocale, "ar");
  assert.equal(runtime.dir, "rtl");
  assert.equal(document.documentElement.getAttribute("lang"), "ar");
  assert.equal(document.documentElement.getAttribute("dir"), "rtl");
});

test("concurrent language changes commit in invocation order", async () => {
  let releaseRussian;
  const russianReady = new Promise((resolve) => {
    releaseRussian = resolve;
  });
  const fetch = async (url) => {
    const path = new URL(url, "https://gateway.invalid").pathname;
    if (path === "/static/locales/ru/common.json") {
      await russianReady;
    }
    const catalog = COMMON_CATALOGS[path];
    return {
      ok: catalog !== undefined,
      status: catalog === undefined ? 404 : 200,
      json: async () => structuredClone(catalog),
    };
  };
  const storage = new FakeStorage();
  const runtime = createGatewayI18n({
    registry: makeRegistry(),
    fetch,
    document: new FakeDocument(),
    storage,
    navigator: { languages: ["en"] },
  });
  await runtime.ready;

  const russian = runtime.changeLanguage("ru");
  const english = runtime.changeLanguage("en");
  releaseRussian();
  await Promise.all([russian, english]);

  assert.equal(runtime.currentLocale, "en");
  assert.equal(storage.getItem(STORAGE_KEY), "en");
});

test("namespace load and language change serialize without losing namespaces", async () => {
  const registry = {
    ...makeRegistry(),
    namespaces: ["common", "auth"],
    pageNamespaces: { runtime: ["common"] },
  };
  let releaseAuth;
  const authReady = new Promise((resolve) => {
    releaseAuth = resolve;
  });
  const catalogs = {
    ...COMMON_CATALOGS,
    "/static/locales/en/auth.json": { title: "Sign in" },
    "/static/locales/ru/auth.json": { title: "Вход" },
  };
  const fetch = async (url) => {
    const path = new URL(url, "https://gateway.invalid").pathname;
    if (path === "/static/locales/en/auth.json") {
      await authReady;
    }
    return { ok: true, status: 200, json: async () => structuredClone(catalogs[path]) };
  };
  const authNode = new FakeElement({ "data-i18n": "auth:title" });
  const document = new FakeDocument([authNode]);
  const runtime = createGatewayI18n({
    registry,
    fetch,
    document,
    storage: new FakeStorage(),
    navigator: { languages: ["en"] },
  });
  await runtime.ready;

  const namespaceLoad = runtime.loadNamespaces("auth");
  const languageChange = runtime.changeLanguage("ru");
  releaseAuth();
  await Promise.all([namespaceLoad, languageChange]);
  runtime.bind(document);

  assert.equal(runtime.currentLocale, "ru");
  assert.equal(authNode.textContent, "Вход");
});

test("a failed queued transition does not poison the next transition", async () => {
  let englishAttempts = 0;
  const fetch = async (url) => {
    const path = new URL(url, "https://gateway.invalid").pathname;
    if (path === "/static/locales/en/common.json" && englishAttempts++ === 0) {
      return { ok: false, status: 503, json: async () => ({}) };
    }
    const catalog = COMMON_CATALOGS[path];
    return { ok: true, status: 200, json: async () => structuredClone(catalog) };
  };
  const runtime = createGatewayI18n({
    registry: makeRegistry(),
    fetch,
    document: new FakeDocument(),
    storage: new FakeStorage({ [STORAGE_KEY]: "ru" }),
    navigator: { languages: ["ru"] },
  });
  await runtime.ready;

  const first = runtime.changeLanguage("en");
  const second = runtime.changeLanguage("en");
  await assert.rejects(first, GatewayI18nError);
  await second;
  assert.equal(runtime.currentLocale, "en");
});
