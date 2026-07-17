(function initSharedNav(global) {
    'use strict';

    const ui = global.gatewayUi;
    const i18n = global.gatewayI18n;
    const auth = global.gatewayAuth;
    if (!ui || !i18n || !auth) {
        throw new Error('UI runtime and shared auth must load before shared-nav.js');
    }

    let navigation = null;
    let localeSelector = null;
    let unsubscribeLocale = null;
    let unbindDocument = null;

    function localeErrorElement() {
        return document.querySelector('[data-locale-error]');
    }

    function hideLocaleError() {
        const element = localeErrorElement();
        if (element) {
            element.hidden = true;
            element.textContent = '';
        }
    }

    function showLocaleError() {
        const element = localeErrorElement();
        if (!element) return;
        element.textContent = i18n.t('common:localeChangeFailed');
        element.hidden = false;
    }

    function translate(key) {
        return i18n.t(key);
    }

    function applyCurrentNav() {
        if (!navigation) return;
        navigation.update({
            direction: i18n.dir,
            pathname: global.location.pathname,
            translate,
        });
    }

    function applyCurrentThemeLabels() {
        if (!global.Theme || typeof global.Theme.setTranslator !== 'function') {
            throw new Error('Theme.setTranslator must load before shared navigation');
        }
        global.Theme.setTranslator(translate);
    }

    async function bootstrap() {
        const [identity] = await Promise.all([
            auth.fetchIdentity(),
            i18n.ready.then(applyCurrentThemeLabels),
        ]);
        unbindDocument = i18n.bind(document);

        const navRoot = document.querySelector('[data-gateway-nav]');
        if (navRoot) {
            navigation = ui.createNavigation(navRoot, {
                direction: i18n.dir,
                pathname: global.location.pathname,
                role: identity.role,
                translate,
            });
        }

        const select = document.querySelector('[data-locale-select]');
        if (select) {
            localeSelector = ui.createLocaleSelector(select, {
                i18n,
                onError: showLocaleError,
            });
            await localeSelector.ready;
        }

        unsubscribeLocale = i18n.subscribe(() => {
            hideLocaleError();
            applyCurrentThemeLabels();
            applyCurrentNav();
        });
    }

    function destroy() {
        unsubscribeLocale?.();
        unbindDocument?.();
        localeSelector?.destroy();
        navigation?.destroy();
    }

    global.gatewayNav = Object.freeze({
        applyCurrentNav,
        bootstrap,
        destroy,
        get ready() {
            return ready;
        },
    });

    const ready = document.readyState === 'loading'
        ? new Promise((resolve) => {
            document.addEventListener('DOMContentLoaded', resolve, {once: true});
        }).then(bootstrap)
        : bootstrap();
    void ready.catch(() => undefined);
})(window);
