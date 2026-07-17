import registry from "../../../static/locales/registry.json" with { type: "json" };

import {
  AUTH_STATES,
  GatewayI18nError,
  NAV_ITEMS,
  STORAGE_KEY,
  applyRoleVisibility,
  createAuthController,
  createDialog,
  createGatewayI18n,
  createLocaleSelector,
  createNavigation,
  createStatus,
  createTabs,
  describeApiError,
  escapeHtml,
  formatInteger,
  loadProductVersion,
  measureNavigationOverflow,
  normalizePathname,
  showToast,
  toFiniteNumber,
  visibleNavigationItems,
} from "./index.mjs";
import { bootstrapGatewayI18n } from "./bootstrap.mjs";

export { GatewayI18nError, STORAGE_KEY, createGatewayI18n };

export const gatewayUi = Object.freeze({
  AUTH_STATES,
  NAV_ITEMS,
  applyRoleVisibility,
  createAuthController,
  createDialog,
  createLocaleSelector,
  createNavigation,
  createStatus,
  createTabs,
  describeApiError,
  escapeHtml,
  formatInteger,
  loadProductVersion,
  measureNavigationOverflow,
  normalizePathname,
  showToast,
  toFiniteNumber,
  visibleNavigationItems,
});

export const gatewayI18n = typeof document === "undefined"
  ? null
  : bootstrapGatewayI18n(() => createGatewayI18n({ registry }), document);

if (gatewayI18n !== null) {
  for (const [name, value] of [["gatewayI18n", gatewayI18n], ["gatewayUi", gatewayUi]]) {
    Object.defineProperty(globalThis, name, {
      configurable: false,
      enumerable: true,
      value,
      writable: false,
    });
  }
  void gatewayI18n.ready.then(
    () => loadProductVersion({
      subscribe: (listener) => gatewayI18n.subscribe(listener),
      translate: (key, values) => gatewayI18n.t(key, values),
    }),
    () => undefined,
  ).catch((error) => {
    console.error("Failed to initialize the localized product version.", error);
  });
}
