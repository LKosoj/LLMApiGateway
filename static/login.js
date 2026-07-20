(function initLogin(global) {
    'use strict';

    const i18n = global.gatewayI18n;
    const ui = global.gatewayUi;
    const form = document.getElementById('loginForm');
    const apiKeyInput = document.getElementById('apiKeyInput');
    const nextInput = document.getElementById('nextInput');
    const submitButton = document.getElementById('submitButton');
    const errorSummary = document.getElementById('errorSummary');
    const errorDetail = document.getElementById('errorBox');
    const templateRawDetail = document.getElementById('serverErrorTemplate')
        .content.textContent;
    errorDetail.textContent = templateRawDetail;
    const hasTemplateError = templateRawDetail.trim() !== ''
        && global.getComputedStyle(errorDetail).display !== 'none';
    const loginStatus = ui.createStatus(errorSummary, {
        rawDetailElement: errorDetail,
        renderMessage: (message) => i18n.t(message.key, message.values || {}),
    });

    let ready = false;
    let running = false;

    function showLocaleError() {
        const error = document.querySelector('[data-locale-error]');
        error.textContent = i18n.t('common:localeChangeFailed');
        error.hidden = false;
    }

    function renderSubmitButton() {
        submitButton.disabled = !ready || running;
        submitButton.setAttribute('aria-busy', String(running));
        if (!ready) return;
        submitButton.textContent = i18n.t(
            running ? 'auth:submitting' : 'auth:submit',
        );
    }

    function showError(message, rawDetail = null) {
        errorSummary.hidden = false;
        errorDetail.hidden = rawDetail === null;
        errorDetail.style.display = rawDetail === null ? 'none' : 'block';
        loginStatus.error(message, {rawDetail});
    }

    function showDescriptor(descriptor) {
        showError(
            {key: descriptor.summaryKey, values: descriptor.summaryValues},
            descriptor.rawDetail,
        );
    }

    function clearError() {
        loginStatus.clear();
        errorSummary.hidden = true;
        errorDetail.hidden = true;
        errorDetail.style.display = 'none';
    }

    async function submitLogin() {
        try {
            const response = await global.fetch('/auth/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    api_key: apiKeyInput.value,
                    next: nextInput.value,
                }),
            });
            let payload;
            try {
                payload = await response.json();
            } catch (error) {
                showDescriptor(ui.describeApiError(
                    {detail: error.message},
                    {
                        status: response.status,
                        requestId: response.headers.get('X-Request-ID'),
                    },
                ));
                return;
            }
            if (!response.ok) {
                showDescriptor(ui.describeApiError(payload, {
                    status: response.status,
                    requestId: response.headers.get('X-Request-ID'),
                }));
                return;
            }
            global.location.assign(payload.redirect_to || '/v1/ui/overview');
        } catch (error) {
            showDescriptor(ui.describeApiError({detail: error.message}));
        } finally {
            running = false;
            renderSubmitButton();
        }
    }

    form.addEventListener('submit', (event) => {
        event.preventDefault();
        if (!ready || running) return;
        running = true;
        clearError();
        renderSubmitButton();
        void submitLogin();
    });

    renderSubmitButton();

    async function bootstrap() {
        await i18n.ready;
        i18n.bind(document);

        const localeSelect = document.querySelector('[data-locale-select]');
        const localeController = ui.createLocaleSelector(localeSelect, {
            i18n,
            onError: showLocaleError,
        });
        await localeController.ready;

        if (hasTemplateError) {
            showError({key: 'auth:templateError'}, templateRawDetail);
        } else {
            clearError();
        }

        ready = true;
        renderSubmitButton();
        i18n.subscribe(() => {
            const error = document.querySelector('[data-locale-error]');
            error.textContent = '';
            error.hidden = true;
            renderSubmitButton();
            loginStatus.rerender();
        });
    }

    void bootstrap().catch(() => undefined);
})(window);
