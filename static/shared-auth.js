(function initGatewayAuth(global) {
    function buildLoginUrl() {
        const currentPath = `${window.location.pathname}${window.location.search}`;
        return `/auth/login?next=${encodeURIComponent(currentPath)}`;
    }

    async function apiFetch(url, options = {}) {
        const response = await fetch(url, options);
        if (response.status === 401) {
            window.location.assign(buildLoginUrl());
            throw new Error("Authentication required");
        }
        return response;
    }

    async function logout() {
        await fetch("/auth/logout", {method: "POST"});
        window.location.assign(buildLoginUrl());
    }

    let cachedIdentity = null;

    async function fetchIdentity() {
        if (cachedIdentity !== null) return cachedIdentity;
        try {
            const response = await fetch("/auth/me");
            if (!response.ok) {
                cachedIdentity = {role: "unknown", key_id: null, name: null};
                return cachedIdentity;
            }
            cachedIdentity = await response.json();
        } catch (error) {
            cachedIdentity = {role: "unknown", key_id: null, name: null};
        }
        return cachedIdentity;
    }

    function applyRoleVisibility(identity) {
        const isMaster = Boolean(identity && identity.role === "master");
        document.querySelectorAll("[data-master-only]").forEach((el) => {
            el.style.display = isMaster ? "" : "none";
        });
        document.querySelectorAll("[data-user-only]").forEach((el) => {
            el.style.display = !isMaster ? "" : "none";
        });
    }

    async function bootstrapRoleUI() {
        const identity = await fetchIdentity();
        applyRoleVisibility(identity);
        return identity;
    }

    global.gatewayAuth = {
        apiFetch,
        logout,
        fetchIdentity,
        applyRoleVisibility,
        bootstrapRoleUI,
    };

    // Auto-apply role-based visibility as soon as the DOM is ready so that
    // pages don't need individual wiring.
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => {
            bootstrapRoleUI().catch(() => {});
        });
    } else {
        bootstrapRoleUI().catch(() => {});
    }
})(window);
