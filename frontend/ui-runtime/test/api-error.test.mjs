import assert from "node:assert/strict";
import test from "node:test";

import { describeApiError } from "../src/api-error.mjs";


test("known API error codes map only to fixed common messages", () => {
  const cases = [
    [
      { error: { code: "auth_invalid_api_key", message: "credential rejected" } },
      "common:errors.invalidApiKey",
      "credential rejected",
    ],
    [
      { detail: { code: "auth_invalid_request", message: "body rejected" } },
      "common:errors.invalidRequest",
      "body rejected",
    ],
    [
      { error: { code: "auth_rate_limited", message: "slow down" } },
      "common:errors.rateLimited",
      "slow down",
    ],
    [
      { detail: { code: "auth_unavailable", message: "maintenance" } },
      "common:errors.unavailable",
      "maintenance",
    ],
  ];

  for (const [payload, summaryKey, rawDetail] of cases) {
    const before = structuredClone(payload);
    const descriptor = describeApiError(payload, { status: 503 });

    assert.deepEqual(descriptor, {
      code: payload.error?.code ?? payload.detail?.code,
      summaryKey,
      summaryValues: {},
      rawDetail,
      requestId: null,
    });
    assert.deepEqual(payload, before);
    assert(Object.isFrozen(descriptor));
    assert(Object.isFrozen(descriptor.summaryValues));
  }
});


test("unknown and malicious codes cannot become translation keys", () => {
  const rawDetail = "<img src=x onerror=alert(1)>";
  const descriptor = describeApiError({
    detail: rawDetail,
    error: {
      code: "common:navigation.pricing",
      message: "must not override exact detail",
    },
    request_id: "request-exact-01",
  }, { status: 418 });

  assert.deepEqual(descriptor, {
    code: null,
    summaryKey: "common:errors.requestFailed",
    summaryValues: { status: 418 },
    rawDetail,
    requestId: "request-exact-01",
  });
  assert(!descriptor.summaryKey.includes("navigation.pricing"));
});


test("network failures use a fixed generic message and keep request ID separate", () => {
  const descriptor = describeApiError(
    { error: { code: ["auth_invalid_api_key"], message: "socket closed" } },
    { requestId: "header-request-id", status: 0 },
  );

  assert.deepEqual(descriptor, {
    code: null,
    summaryKey: "common:errors.network",
    summaryValues: {},
    rawDetail: "socket closed",
    requestId: "header-request-id",
  });
});


test("recognized error.code takes precedence over recognized detail.code", () => {
  const descriptor = describeApiError({
    error: { code: "auth_unavailable", message: "outer" },
    detail: { code: "auth_invalid_request", message: "inner" },
  }, { status: 503 });

  assert.equal(descriptor.code, "auth_unavailable");
  assert.equal(descriptor.summaryKey, "common:errors.unavailable");
  assert.equal(descriptor.rawDetail, "inner");
});
