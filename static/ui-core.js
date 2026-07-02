(function initGatewayUiCore(global) {
    'use strict';

    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function toFiniteNumber(value, fallback = 0) {
        const numberValue = Number(value);
        return Number.isFinite(numberValue) ? numberValue : fallback;
    }

    function formatInteger(value) {
        return String(Math.trunc(toFiniteNumber(value, 0)));
    }

    function showToast(element, message, options = {}) {
        if (!element) return null;
        const timeoutMs = toFiniteNumber(options.timeoutMs, 3000);
        const token = Symbol('toast');
        element._gatewayToastToken = token;
        element.textContent = message;
        element.classList.toggle('error', Boolean(options.isError));
        element.classList.add('visible');
        if (element._gatewayToastTimer) {
            clearTimeout(element._gatewayToastTimer);
        }
        element._gatewayToastTimer = setTimeout(() => {
            if (element._gatewayToastToken === token) {
                element.classList.remove('visible');
            }
        }, timeoutMs);
        return token;
    }

    global.gatewayUi = {
        escapeHtml,
        toFiniteNumber,
        formatInteger,
        showToast,
    };
})(window);
