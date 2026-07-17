import assert from "node:assert/strict";
import test from "node:test";

import * as runtime from "../src/index.mjs";

test("shared UI primitives have one source public API", () => {
  for (const name of [
    "AUTH_STATES",
    "NAV_ITEMS",
    "applyRoleVisibility",
    "createAuthController",
    "createDialog",
    "describeApiError",
    "createLocaleSelector",
    "createNavigation",
    "createStatus",
    "createTabs",
    "escapeHtml",
    "formatInteger",
    "loadProductVersion",
    "measureNavigationOverflow",
    "normalizePathname",
    "showToast",
    "toFiniteNumber",
    "visibleNavigationItems",
  ]) {
    assert.notEqual(runtime[name], undefined, `${name} must be exported`);
  }
  assert(Object.isFrozen(runtime.AUTH_STATES));
  assert(Object.isFrozen(runtime.NAV_ITEMS));
});
