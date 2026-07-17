import { GatewayI18nError } from "./contracts.mjs";

const TRANSLATED_ATTRIBUTES = Object.freeze({
  "data-i18n-title": "title",
  "data-i18n-placeholder": "placeholder",
  "data-i18n-aria-label": "aria-label",
  "data-i18n-aria-description": "aria-description",
});
const SELECTOR = [
  "[data-i18n]",
  ...Object.keys(TRANSLATED_ATTRIBUTES).map((attribute) => `[${attribute}]`),
  "[data-i18n-raw-detail]",
].join(",");

function requireTranslation(value, key) {
  if (typeof value !== "string" || !value) {
    throw new GatewayI18nError(
      "BIND_TRANSLATION_INVALID",
      `Binding translation must be a non-empty string: ${key}`,
    );
  }
  return value;
}

export function createAtomicBindingPlan(root, translate) {
  if (!root || typeof root.querySelectorAll !== "function") {
    throw new GatewayI18nError("BIND_ROOT_INVALID", "Binding root must support querySelectorAll");
  }
  if (typeof translate !== "function") {
    throw new GatewayI18nError("BIND_TRANSLATOR_INVALID", "Binding translator must be a function");
  }

  const elements = [];
  const seen = new Set();
  const include = (element) => {
    if (!seen.has(element)) {
      seen.add(element);
      elements.push(element);
    }
  };
  if (typeof root.matches === "function" && root.matches(SELECTOR)) {
    include(root);
  }
  for (const element of root.querySelectorAll(SELECTOR)) {
    include(element);
  }

  const changes = [];
  for (const element of elements) {
    const textKey = element.getAttribute("data-i18n");
    if (textKey !== null && element.hasAttribute("data-i18n-raw-detail")) {
      throw new GatewayI18nError(
        "BIND_RAW_TEXT_CONFLICT",
        "Raw detail cannot also be a translated text target",
      );
    }
    if (textKey !== null) {
      if (element.children?.length > 0) {
        throw new GatewayI18nError(
          "BIND_TEXT_TARGET_HAS_CHILDREN",
          `Text binding target has Element children: ${textKey}`,
        );
      }
      changes.push({
        element,
        kind: "text",
        previous: element.textContent,
        value: requireTranslation(translate(textKey), textKey),
      });
    }
    for (const [source, target] of Object.entries(TRANSLATED_ATTRIBUTES)) {
      const key = element.getAttribute(source);
      if (key !== null) {
        changes.push({
          element,
          kind: "attribute",
          name: target,
          previous: element.getAttribute(target),
          value: requireTranslation(translate(key), key),
        });
      }
    }
    if (element.hasAttribute("data-i18n-raw-detail")) {
      for (const [name, value] of [["lang", "und"], ["dir", "auto"]]) {
        changes.push({
          element,
          kind: "attribute",
          name,
          previous: element.getAttribute(name),
          value,
        });
      }
    }
  }

  let applied = false;
  function restore(change) {
    if (change.kind === "text") {
      change.element.textContent = change.previous;
    } else if (change.previous === null) {
      change.element.removeAttribute(change.name);
    } else {
      change.element.setAttribute(change.name, change.previous);
    }
  }
  return Object.freeze({
    apply() {
      const completed = [];
      try {
        for (const change of changes) {
          if (change.kind === "text") {
            change.element.textContent = change.value;
          } else {
            change.element.setAttribute(change.name, change.value);
          }
          completed.push(change);
        }
        applied = true;
      } catch (error) {
        for (const change of completed.reverse()) {
          restore(change);
        }
        throw new GatewayI18nError("BIND_APPLY_FAILED", "Cannot apply translations atomically", {
          cause: error,
        });
      }
    },
    rollback() {
      if (!applied) {
        return;
      }
      for (const change of [...changes].reverse()) {
        restore(change);
      }
      applied = false;
    },
  });
}
