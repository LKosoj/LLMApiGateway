(function initSharedNav(global) {
    'use strict';

    function normalizedPath(value) {
        try {
            return new URL(value, global.location.origin).pathname.replace(/\/$/, '');
        } catch (_err) {
            return '';
        }
    }

    function applyCurrentNav(root = document) {
        const currentPath = normalizedPath(global.location.pathname);
        root.querySelectorAll('.top-nav .nav-button[href]').forEach((link) => {
            const linkPath = normalizedPath(link.getAttribute('href'));
            const isActive = linkPath === currentPath || link.classList.contains('active');
            if (isActive) {
                link.setAttribute('aria-current', 'page');
            } else {
                link.removeAttribute('aria-current');
            }
        });
    }

    global.gatewayNav = {
        applyCurrentNav,
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => applyCurrentNav());
    } else {
        applyCurrentNav();
    }
})(window);
