import assert from "node:assert/strict";
import test from "node:test";

import {
  AUTH_STATES,
  applyRoleVisibility,
  createAuthController,
} from "../src/auth.mjs";

test("auth controller deduplicates identity requests and publishes a master state", async () => {
  let resolveResponse;
  let calls = 0;
  const fetchIdentity = () => {
    calls += 1;
    return new Promise((resolve) => {
      resolveResponse = resolve;
    });
  };
  const controller = createAuthController({ fetch: fetchIdentity });
  const states = [];
  controller.subscribe((state) => states.push(state));

  const first = controller.fetchIdentity();
  const second = controller.fetchIdentity();

  assert.equal(first, second);
  assert.equal(calls, 1);
  assert.equal(controller.state, AUTH_STATES.pending);

  resolveResponse({
    ok: true,
    json: async () => ({ role: "master", key_id: "key-1", name: "Root" }),
  });
  const identity = await first;

  assert.deepEqual(identity, { role: "master", key_id: "key-1", name: "Root" });
  assert(Object.isFrozen(identity));
  assert.equal(controller.state, AUTH_STATES.master);
  assert.equal(controller.identity, identity);
  assert.deepEqual(states, [AUTH_STATES.master]);
  assert.equal(await controller.fetchIdentity(), identity);
  assert.equal(calls, 1);
});

test("auth controller maps failed and malformed identity responses to unknown", async (t) => {
  const cases = [
    ["non-ok response", async () => ({ ok: false, status: 503 })],
    ["network failure", async () => { throw new Error("offline"); }],
    ["malformed role", async () => ({ ok: true, json: async () => ({ role: "admin" }) })],
    ["invalid JSON", async () => ({ ok: true, json: async () => { throw new Error("bad json"); } })],
  ];

  for (const [name, fetchIdentity] of cases) {
    await t.test(name, async () => {
      const controller = createAuthController({ fetch: fetchIdentity });
      const identity = await controller.fetchIdentity();
      assert.deepEqual(identity, { role: "unknown", key_id: null, name: null });
      assert.equal(controller.state, AUTH_STATES.unknown);
    });
  }
});

test("auth controller accepts the virtual-user role and supports unsubscribe", async () => {
  const states = [];
  const controller = createAuthController({
    fetch: async () => ({
      ok: true,
      json: async () => ({ role: "user", key_id: 42, name: null }),
    }),
  });
  const unsubscribe = controller.subscribe((state) => states.push(state));
  unsubscribe();

  assert.deepEqual(await controller.fetchIdentity(), {
    role: "user",
    key_id: 42,
    name: null,
  });
  assert.equal(controller.state, AUTH_STATES.user);
  assert.deepEqual(states, []);
});

test("role visibility is fail-closed and uses the hidden property", () => {
  const masterOnly = { hidden: false };
  const userOnly = { hidden: false };
  const both = { hidden: false };
  const root = {
    querySelectorAll(selector) {
      if (selector === "[data-master-only]") return [masterOnly, both];
      if (selector === "[data-user-only]") return [userOnly, both];
      throw new Error(`Unexpected selector: ${selector}`);
    },
  };

  applyRoleVisibility(root, AUTH_STATES.pending);
  assert.equal(masterOnly.hidden, true);
  assert.equal(userOnly.hidden, true);
  assert.equal(both.hidden, true);

  applyRoleVisibility(root, AUTH_STATES.master);
  assert.equal(masterOnly.hidden, false);
  assert.equal(userOnly.hidden, true);
  assert.equal(both.hidden, true);

  applyRoleVisibility(root, AUTH_STATES.user);
  assert.equal(masterOnly.hidden, true);
  assert.equal(userOnly.hidden, false);
  assert.equal(both.hidden, true);

  applyRoleVisibility(root, AUTH_STATES.unknown);
  assert.equal(masterOnly.hidden, true);
  assert.equal(userOnly.hidden, true);
  assert.equal(both.hidden, true);
});

test("auth controller rejects an invalid setup without performing a request", () => {
  assert.throws(
    () => createAuthController({ fetch: null }),
    /fetch implementation is required/,
  );
});
