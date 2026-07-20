(function initAccessDenied(global) {
    'use strict';

    const i18n = global.gatewayI18n;

    async function bootstrap() {
        await i18n.ready;
        i18n.bind(document);
    }

    void bootstrap().catch(() => undefined);
})(window);
