export const AUTH_STATES = Object.freeze({
  pending: "pending",
  master: "master",
  user: "user",
  unknown: "unknown",
});

const UNKNOWN_IDENTITY = Object.freeze({ role: "unknown", key_id: null, name: null });

function normalizeIdentity(payload) {
  if (payload === null || typeof payload !== "object") return UNKNOWN_IDENTITY;
  if (payload.role !== AUTH_STATES.master && payload.role !== AUTH_STATES.user) {
    return UNKNOWN_IDENTITY;
  }
  const keyId = typeof payload.key_id === "string" || typeof payload.key_id === "number"
    ? payload.key_id
    : null;
  const name = typeof payload.name === "string" ? payload.name : null;
  return Object.freeze({ role: payload.role, key_id: keyId, name });
}

export function applyRoleVisibility(root, state) {
  if (!root || typeof root.querySelectorAll !== "function") {
    throw new TypeError("A queryable role-visibility root is required");
  }
  const masterOnly = new Set(root.querySelectorAll("[data-master-only]"));
  const userOnly = new Set(root.querySelectorAll("[data-user-only]"));
  const gated = new Set([...masterOnly, ...userOnly]);

  for (const element of gated) {
    const masterGateAllows = !masterOnly.has(element) || state === AUTH_STATES.master;
    const userGateAllows = !userOnly.has(element) || state === AUTH_STATES.user;
    element.hidden = !(masterGateAllows && userGateAllows);
  }
}

export function createAuthController(options = {}) {
  if (typeof options.fetch !== "function") {
    throw new TypeError("A fetch implementation is required");
  }

  let state = AUTH_STATES.pending;
  let identity = null;
  let identityPromise = null;
  const listeners = new Set();

  function publish(nextIdentity) {
    identity = nextIdentity;
    state = nextIdentity.role;
    queueMicrotask(() => {
      for (const listener of [...listeners]) listener(state, identity);
    });
    return identity;
  }

  async function requestIdentity() {
    try {
      const response = await options.fetch("/auth/me", { credentials: "same-origin" });
      if (!response || response.ok !== true || typeof response.json !== "function") {
        return UNKNOWN_IDENTITY;
      }
      return normalizeIdentity(await response.json());
    } catch (_error) {
      return UNKNOWN_IDENTITY;
    }
  }

  function fetchIdentity() {
    if (identityPromise === null) {
      identityPromise = requestIdentity().then(publish);
    }
    return identityPromise;
  }

  function subscribe(listener) {
    if (typeof listener !== "function") {
      throw new TypeError("An auth-state listener must be a function");
    }
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  return Object.freeze({
    get identity() {
      return identity;
    },
    get state() {
      return state;
    },
    fetchIdentity,
    subscribe,
  });
}
