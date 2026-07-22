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

    let currentIdentity = null;
    let identityContainer = null;
    let identityRoleElement = null;
    let identityNameElement = null;
    let identityLogoutButton = null;
    let identityLogoutLabel = null;

    let sidebarFooter = null;
    let sidebarOverlay = null;
    let sidebarPanel = null;
    let sidebarDialog = null;
    let sidebarCollapseButton = null;
    let navToggleButton = null;
    let navCloseButton = null;
    let desktopMediaQuery = null;
    let sidebarCollapsed = false;

    const DESKTOP_MEDIA_QUERY = '(min-width: 1024px)';
    const SIDEBAR_STORAGE_KEY = 'llmgateway:sidebar';

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

    function applyCurrentSidebarLabels() {
        if (navToggleButton) navToggleButton.setAttribute('aria-label', i18n.t('common:navigation.toggle'));
        if (navCloseButton) navCloseButton.setAttribute('aria-label', i18n.t('common:navigation.close'));
        if (sidebarOverlay) sidebarOverlay.setAttribute('aria-label', i18n.t('common:navigation.overlay'));
        applyCurrentSidebarMode();
    }

    function applyCurrentSidebarMode() {
        if (!sidebarPanel) return;
        const collapsed = sidebarCollapsed && desktopMediaQuery?.matches === true;
        sidebarPanel.toggleAttribute('data-collapsed', collapsed);
        document.body.toggleAttribute('data-gateway-sidebar-collapsed', collapsed);
        if (sidebarCollapseButton) {
            const label = collapsed
                ? i18n.t('common:navigation.expandSidebar')
                : i18n.t('common:navigation.collapseSidebar');
            sidebarCollapseButton.setAttribute('aria-expanded', String(!collapsed));
            sidebarCollapseButton.setAttribute('aria-label', label);
            sidebarCollapseButton.setAttribute('title', label);
        }
        sidebarPanel.querySelectorAll('.nav-button').forEach((link) => {
            const label = link.querySelector('.nav-button-label')?.textContent;
            if (collapsed && label) {
                link.setAttribute('title', label);
            } else {
                link.removeAttribute('title');
            }
        });
    }

    function setSidebarCollapsed(collapsed) {
        sidebarCollapsed = collapsed;
        global.localStorage.setItem(
            SIDEBAR_STORAGE_KEY,
            collapsed ? 'collapsed' : 'expanded',
        );
        applyCurrentSidebarMode();
    }

    function handleDesktopMediaChange(event) {
        if (event.matches && sidebarDialog?.state === 'open') {
            sidebarDialog.close('cancel');
        }
        applyCurrentSidebarMode();
    }

    // Builds the desktop sidebar / mobile drawer shell around the existing
    // `[data-gateway-nav]` mount and moves it there, so every page gets the
    // shell without touching its HTML. The overlay is mounted as a direct
    // body child (not inside `.top-nav`) because `.top-nav-content` uses
    // `backdrop-filter` on several pages, which would otherwise confine a
    // `position: fixed` drawer to the top bar instead of the full viewport.
    function buildSidebarShell(navRoot) {
        const topNavContent = navRoot.closest('.top-nav-content');
        if (!topNavContent) {
            throw new Error('.top-nav-content must exist before shared navigation');
        }
        const settings = topNavContent.querySelector('.gateway-nav-actions');
        if (!settings) {
            throw new Error('.gateway-nav-actions must exist before shared navigation');
        }
        const localeLabel = settings.querySelector('.gateway-locale-label');
        const localeSelect = settings.querySelector('.gateway-locale-select');
        if (!localeLabel || !localeSelect) {
            throw new Error('locale controls must exist before shared navigation');
        }

        sidebarOverlay = document.createElement('div');
        sidebarOverlay.className = 'gateway-nav-overlay';
        sidebarOverlay.setAttribute('data-gateway-nav-overlay', '');
        sidebarOverlay.hidden = true;

        sidebarPanel = document.createElement('aside');
        sidebarPanel.className = 'gateway-sidebar';
        sidebarPanel.id = 'gateway-sidebar';
        sidebarPanel.setAttribute('data-gateway-sidebar', '');

        navCloseButton = document.createElement('button');
        navCloseButton.type = 'button';
        navCloseButton.className = 'gateway-nav-close';
        navCloseButton.setAttribute('data-gateway-nav-close', '');
        navCloseButton.setAttribute('data-dialog-close', 'cancel');
        navCloseButton.textContent = '×';

        sidebarCollapseButton = document.createElement('button');
        sidebarCollapseButton.type = 'button';
        sidebarCollapseButton.className = 'gateway-sidebar-collapse';
        sidebarCollapseButton.setAttribute('data-gateway-sidebar-collapse', '');
        sidebarCollapseButton.setAttribute('aria-controls', 'gateway-sidebar');
        const collapseIcon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        collapseIcon.classList.add('gateway-sidebar-collapse-icon');
        collapseIcon.setAttribute('viewBox', '0 0 24 24');
        collapseIcon.setAttribute('aria-hidden', 'true');
        collapseIcon.setAttribute('focusable', 'false');
        const collapseFrame = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        collapseFrame.setAttribute('d', 'M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z');
        const collapseDivider = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        collapseDivider.setAttribute('d', 'M9 3v18');
        const collapseArrow = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        collapseArrow.setAttribute('d', 'm15 9-3 3 3 3');
        collapseIcon.append(collapseFrame, collapseDivider, collapseArrow);
        sidebarCollapseButton.appendChild(collapseIcon);
        sidebarCollapseButton.addEventListener('click', () => {
            setSidebarCollapsed(!sidebarCollapsed);
        });

        sidebarFooter = document.createElement('div');
        sidebarFooter.className = 'gateway-sidebar-footer';
        sidebarFooter.setAttribute('data-gateway-sidebar-footer', '');

        settings.classList.add('gateway-sidebar-settings');
        settings.setAttribute('data-gateway-sidebar-settings', '');
        const languageIcon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        languageIcon.classList.add('gateway-locale-icon');
        languageIcon.setAttribute('viewBox', '0 0 24 24');
        languageIcon.setAttribute('aria-hidden', 'true');
        languageIcon.setAttribute('focusable', 'false');
        const globe = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        globe.setAttribute('cx', '12');
        globe.setAttribute('cy', '12');
        globe.setAttribute('r', '9');
        const latitude = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        latitude.setAttribute('d', 'M3 12h18');
        const longitude = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        longitude.setAttribute('d', 'M12 3c3 3.3 3 14.7 0 18M12 3c-3 3.3-3 14.7 0 18');
        languageIcon.append(globe, latitude, longitude);
        const localeControl = document.createElement('div');
        localeControl.className = 'gateway-locale-control';
        localeControl.append(languageIcon, localeLabel, localeSelect);
        settings.insertBefore(localeControl, settings.firstChild);
        sidebarFooter.appendChild(settings);

        sidebarPanel.append(navCloseButton, sidebarCollapseButton, navRoot, sidebarFooter);
        sidebarOverlay.appendChild(sidebarPanel);
        document.body.insertBefore(sidebarOverlay, document.body.firstChild);

        navToggleButton = document.createElement('button');
        navToggleButton.type = 'button';
        navToggleButton.className = 'gateway-nav-toggle';
        navToggleButton.setAttribute('data-gateway-nav-toggle', '');
        navToggleButton.setAttribute('aria-controls', 'gateway-sidebar');
        navToggleButton.setAttribute('aria-expanded', 'false');
        const toggleIcon = document.createElement('span');
        toggleIcon.className = 'gateway-nav-toggle-bars';
        toggleIcon.setAttribute('aria-hidden', 'true');
        for (let index = 0; index < 3; index += 1) {
            const bar = document.createElement('span');
            bar.className = 'gateway-nav-toggle-bar';
            toggleIcon.appendChild(bar);
        }
        navToggleButton.appendChild(toggleIcon);
        topNavContent.insertBefore(navToggleButton, topNavContent.firstChild);

        sidebarDialog = ui.createDialog({
            overlay: sidebarOverlay,
            dialog: sidebarPanel,
            label: translate('common:navigation.label'),
            inertRoots: Array.from(document.body.children).filter((element) => element !== sidebarOverlay),
            onClose: () => {
                navToggleButton.setAttribute('aria-expanded', 'false');
            },
        });

        navToggleButton.addEventListener('click', () => {
            if (sidebarDialog.state === 'open') {
                sidebarDialog.close('cancel');
            } else {
                sidebarDialog.open();
                navToggleButton.setAttribute('aria-expanded', 'true');
            }
        });

        navRoot.addEventListener('click', (event) => {
            if (sidebarDialog.state === 'open' && event.target.closest('.nav-button')) {
                sidebarDialog.close('cancel');
            }
        });

        if (typeof global.matchMedia === 'function') {
            desktopMediaQuery = global.matchMedia(DESKTOP_MEDIA_QUERY);
            desktopMediaQuery.addEventListener('change', handleDesktopMediaChange);
        }
        sidebarCollapsed = global.localStorage.getItem(SIDEBAR_STORAGE_KEY) === 'collapsed';
        applyCurrentSidebarMode();
    }

    function ensureIdentityElements() {
        if (identityContainer) return identityContainer;
        if (!sidebarFooter) {
            throw new Error('the sidebar footer must exist before shared navigation');
        }

        identityContainer = document.createElement('div');
        identityContainer.className = 'gateway-identity';
        identityContainer.setAttribute('data-gateway-identity', '');
        identityContainer.hidden = true;

        identityRoleElement = document.createElement('span');
        identityRoleElement.className = 'gateway-identity-role';
        identityRoleElement.setAttribute('data-gateway-identity-role', '');

        identityNameElement = document.createElement('span');
        identityNameElement.className = 'gateway-identity-name';
        identityNameElement.setAttribute('data-gateway-identity-name', '');
        identityNameElement.hidden = true;

        identityLogoutButton = document.createElement('button');
        identityLogoutButton.type = 'button';
        identityLogoutButton.className = 'gateway-identity-logout';
        identityLogoutButton.setAttribute('data-gateway-identity-logout', '');
        identityLogoutButton.addEventListener('click', () => {
            void auth.logout();
        });

        const identityLogoutIcon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        identityLogoutIcon.classList.add('gateway-identity-logout-icon');
        identityLogoutIcon.setAttribute('viewBox', '0 0 24 24');
        identityLogoutIcon.setAttribute('aria-hidden', 'true');
        identityLogoutIcon.setAttribute('focusable', 'false');
        const logoutDoor = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        logoutDoor.setAttribute('d', 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4');
        const logoutArrow = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        logoutArrow.setAttribute('d', 'm16 17 5-5-5-5M21 12H9');
        identityLogoutIcon.append(logoutDoor, logoutArrow);
        identityLogoutLabel = document.createElement('span');
        identityLogoutLabel.className = 'gateway-identity-logout-label';
        identityLogoutButton.append(identityLogoutIcon, identityLogoutLabel);

        identityContainer.append(identityRoleElement, identityNameElement, identityLogoutButton);
        sidebarFooter.appendChild(identityContainer);
        return identityContainer;
    }

    function applyCurrentIdentity() {
        if (currentIdentity === null) return;
        const container = ensureIdentityElements();

        if (currentIdentity.role !== 'master' && currentIdentity.role !== 'user') {
            container.hidden = true;
            return;
        }

        container.hidden = false;
        identityRoleElement.textContent = i18n.t(`common:identity.role.${currentIdentity.role}`);
        if (currentIdentity.name) {
            identityNameElement.textContent = i18n.t('common:identity.name', {name: currentIdentity.name});
            identityNameElement.hidden = false;
        } else {
            identityNameElement.hidden = true;
        }
        const logoutLabel = i18n.t('common:identity.logout');
        identityLogoutLabel.textContent = logoutLabel;
        identityLogoutButton.setAttribute('aria-label', logoutLabel);
    }

    async function bootstrap() {
        const [identity] = await Promise.all([
            auth.fetchIdentity(),
            i18n.ready.then(applyCurrentThemeLabels),
        ]);
        currentIdentity = identity;
        unbindDocument = i18n.bind(document);

        const navRoot = document.querySelector('[data-gateway-nav]');
        if (navRoot) {
            buildSidebarShell(navRoot);
            navigation = ui.createNavigation(navRoot, {
                direction: i18n.dir,
                pathname: global.location.pathname,
                role: identity.role,
                layout: 'list',
                translate,
            });
            applyCurrentSidebarLabels();
        }

        const select = document.querySelector('[data-locale-select]');
        if (select) {
            localeSelector = ui.createLocaleSelector(select, {
                i18n,
                onError: showLocaleError,
            });
            await localeSelector.ready;
        }

        applyCurrentIdentity();

        unsubscribeLocale = i18n.subscribe(() => {
            hideLocaleError();
            applyCurrentThemeLabels();
            applyCurrentNav();
            applyCurrentIdentity();
            applyCurrentSidebarLabels();
        });
    }

    function destroy() {
        unsubscribeLocale?.();
        unbindDocument?.();
        localeSelector?.destroy();
        navigation?.destroy();
        sidebarDialog?.destroy();
        desktopMediaQuery?.removeEventListener('change', handleDesktopMediaChange);
        document.body.removeAttribute('data-gateway-sidebar-collapsed');
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
