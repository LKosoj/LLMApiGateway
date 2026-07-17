const FAILURE_MESSAGE = "Localization failed. / Не удалось загрузить локализацию.";

function setState(document, state) {
  document.documentElement.setAttribute("data-i18n-state", state);
}

function showFailure(document) {
  setState(document, "error");
  let alert = document.querySelector("[data-i18n-bootstrap-error]");
  if (alert === null) {
    alert = document.createElement("div");
    alert.setAttribute("data-i18n-bootstrap-error", "");
    alert.setAttribute("role", "alert");
    alert.setAttribute("aria-live", "assertive");
    const target = document.body ?? document.documentElement;
    if (typeof target.prepend === "function") {
      target.prepend(alert);
    } else {
      target.appendChild(alert);
    }
  }
  alert.textContent = FAILURE_MESSAGE;
}

export function attachBootstrapState(runtime, document) {
  try {
    setState(document, "loading");
  } catch (_error) {
    // The ready observer below still prevents an unhandled rejection.
  }
  return runtime.ready.then(
    () => {
      setState(document, "ready");
    },
    () => {
      showFailure(document);
    },
  ).catch(() => undefined);
}

export function bootstrapGatewayI18n(createRuntime, document) {
  let runtime;
  try {
    runtime = createRuntime();
  } catch (error) {
    runtime = Object.freeze({ ready: Promise.reject(error) });
  }
  attachBootstrapState(runtime, document);
  return runtime;
}
