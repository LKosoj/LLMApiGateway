export class FakeElement {
  constructor(attributes = {}, children = []) {
    this.attributes = new Map(Object.entries(attributes));
    this._textContent = "";
    this.children = [...children];
    this.value = "";
    this.scrollTop = 0;
    this.scrollLeft = 0;
    this.innerHTMLWrites = 0;
  }

  get textContent() {
    return this._textContent;
  }

  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }

  get dataset() {
    const result = {};
    for (const [name, value] of this.attributes) {
      if (name.startsWith("data-")) {
        const key = name
          .slice(5)
          .replace(/-([a-z])/g, (_match, character) => character.toUpperCase());
        result[key] = value;
      }
    }
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
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  matches(selector) {
    const attributeNames = [...selector.matchAll(/\[([^\]]+)\]/g)].map(
      (match) => match[1],
    );
    return attributeNames.some((attribute) => this.hasAttribute(attribute));
  }

  querySelectorAll(selector) {
    const result = [];
    const visit = (element) => {
      if (element.matches(selector)) {
        result.push(element);
      }
      for (const child of element.children) {
        visit(child);
      }
    };
    for (const child of this.children) {
      visit(child);
    }
    return result;
  }

  prepend(element) {
    this.children.unshift(element);
  }

  appendChild(element) {
    this.children.push(element);
    return element;
  }

  set innerHTML(_value) {
    this.innerHTMLWrites += 1;
  }
}

export class FakeDocument {
  constructor(elements = [], page = "runtime") {
    this.documentElement = new FakeElement({ "data-i18n-page": page });
    this.elements = elements;
    this.body = new FakeElement();
    this.activeElement = null;
  }

  querySelectorAll(selector) {
    return this.elements.filter((element) => element.matches(selector));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0]
      ?? this.body.querySelectorAll(selector)[0]
      ?? null;
  }

  createElement() {
    return new FakeElement();
  }
}

export class FakeStorage {
  constructor(initial = {}) {
    this.values = new Map(Object.entries(initial));
  }

  getItem(key) {
    return this.values.has(key) ? this.values.get(key) : null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

export function makeFetch(catalogs, calls = []) {
  return async (url) => {
    const path = new URL(url, "https://gateway.invalid").pathname;
    calls.push(path);
    if (!(path in catalogs)) {
      return {
        ok: false,
        status: 404,
        json: async () => ({ error: "not found" }),
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => structuredClone(catalogs[path]),
    };
  };
}

export function makeRegistry(locales = [
  { code: "en", nativeLabel: "English", dir: "ltr" },
  { code: "ru", nativeLabel: "Русский", dir: "ltr" },
]) {
  return {
    schemaVersion: 1,
    defaultLocale: "en",
    locales,
    namespaces: ["common"],
    pageNamespaces: { runtime: ["common"] },
  };
}
