import assert from "node:assert/strict";
import test from "node:test";

import {
  attachBootstrapState,
  bootstrapGatewayI18n,
} from "../src/bootstrap.mjs";
import { FakeDocument } from "./helpers.mjs";

test("bootstrap failure is visible and observed without replacing rejected ready", async () => {
  const document = new FakeDocument();
  const failure = new Error("synthetic catalog failure");
  const ready = Promise.reject(failure);
  const runtime = { ready };

  await attachBootstrapState(runtime, document);

  await assert.rejects(runtime.ready, (error) => error === failure);
  assert.equal(document.documentElement.getAttribute("data-i18n-state"), "error");
  const alert = document.querySelector("[data-i18n-bootstrap-error]");
  assert(alert);
  assert.equal(alert.getAttribute("role"), "alert");
  assert.equal(
    alert.textContent,
    "Localization failed. / Не удалось загрузить локализацию.",
  );
});

test("bootstrap success exposes a stable ready state", async () => {
  const document = new FakeDocument();
  await attachBootstrapState({ ready: Promise.resolve() }, document);
  assert.equal(document.documentElement.getAttribute("data-i18n-state"), "ready");
  assert.equal(document.querySelector("[data-i18n-bootstrap-error]"), null);
});

test("bootstrap renders a visible failure when runtime creation throws synchronously", async () => {
  const document = new FakeDocument();
  const failure = new DOMException("localStorage is blocked", "SecurityError");
  const runtime = bootstrapGatewayI18n(() => {
    throw failure;
  }, document);

  await assert.rejects(runtime.ready, (error) => error === failure);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(document.documentElement.getAttribute("data-i18n-state"), "error");
  const alert = document.querySelector("[data-i18n-bootstrap-error]");
  assert(alert);
  assert.equal(alert.getAttribute("role"), "alert");
});
