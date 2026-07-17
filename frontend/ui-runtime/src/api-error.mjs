const SUMMARY_KEYS = Object.freeze({
  auth_invalid_api_key: "common:errors.invalidApiKey",
  auth_invalid_request: "common:errors.invalidRequest",
  auth_rate_limited: "common:errors.rateLimited",
  auth_unavailable: "common:errors.unavailable",
});

function asRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value
    : null;
}

function knownError(payload) {
  const error = asRecord(payload?.error);
  const detail = asRecord(payload?.detail);
  for (const code of [error?.code, detail?.code]) {
    if (typeof code === "string" && Object.hasOwn(SUMMARY_KEYS, code)) {
      return Object.freeze({ code, summaryKey: SUMMARY_KEYS[code] });
    }
  }
  return null;
}

function rawDetail(payload) {
  if (typeof payload?.detail === "string") return payload.detail;
  const detail = asRecord(payload?.detail);
  if (typeof detail?.message === "string") return detail.message;
  const error = asRecord(payload?.error);
  if (typeof error?.message === "string") return error.message;
  return null;
}

function requestId(payload, explicitRequestId) {
  const error = asRecord(payload?.error);
  const detail = asRecord(payload?.detail);
  for (const value of [
    explicitRequestId,
    payload?.request_id,
    error?.request_id,
    detail?.request_id,
  ]) {
    if (typeof value === "string" && value.length > 0) return value;
  }
  return null;
}

export function describeApiError(payload, options = {}) {
  const body = asRecord(payload);
  const known = knownError(body);
  let summaryKey = known?.summaryKey ?? null;
  let summaryValues = {};

  if (summaryKey === null) {
    if (Number.isInteger(options.status) && options.status > 0) {
      summaryKey = "common:errors.requestFailed";
      summaryValues = { status: options.status };
    } else {
      summaryKey = "common:errors.network";
    }
  }

  return Object.freeze({
    code: known?.code ?? null,
    summaryKey,
    summaryValues: Object.freeze(summaryValues),
    rawDetail: rawDetail(body),
    requestId: requestId(body, options.requestId),
  });
}
