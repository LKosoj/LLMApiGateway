(function initGatewayAuth(global) {
    'use strict';

    const ui = global.gatewayUi;
    if (!ui || typeof ui.createAuthController !== 'function') {
        throw new Error('Gateway UI runtime must load before shared-auth.js');
    }

    function buildLoginUrl() {
        const currentPath = `${global.location.pathname}${global.location.search}`;
        return `/auth/login?next=${encodeURIComponent(currentPath)}`;
    }

    async function apiFetch(url, options = {}) {
        const response = await global.fetch(url, options);
        if (response.status === 401) {
            global.location.assign(buildLoginUrl());
            throw new Error('Authentication required');
        }
        return response;
    }

    async function logout() {
        try {
            const response = await global.fetch('/auth/logout', {method: 'POST'});
            if (!response.ok) {
                console.error(`Logout failed with HTTP ${response.status}`);
                return false;
            }
            global.location.assign(buildLoginUrl());
            return true;
        } catch (error) {
            console.error('Logout request failed', error);
            return false;
        }
    }

    const controller = ui.createAuthController({fetch: global.fetch.bind(global)});

    function fetchIdentity() {
        return controller.fetchIdentity();
    }

    function applyRoleVisibility(identity = controller.identity) {
        const state = typeof identity === 'string'
            ? identity
            : identity?.role ?? controller.state;
        ui.applyRoleVisibility(document, state);
    }

    async function bootstrapRoleUI() {
        const identity = await fetchIdentity();
        applyRoleVisibility(identity);
        return identity;
    }

    function subscribe(listener) {
        return controller.subscribe(listener);
    }

    applyRoleVisibility(ui.AUTH_STATES.pending);
    controller.subscribe((state) => applyRoleVisibility(state));

    global.gatewayAuth = Object.freeze({
        apiFetch,
        logout,
        fetchIdentity,
        applyRoleVisibility,
        bootstrapRoleUI,
        subscribe,
        get state() {
            return controller.state;
        },
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            void bootstrapRoleUI();
        }, {once: true});
    } else {
        void bootstrapRoleUI();
    }
})(window);
