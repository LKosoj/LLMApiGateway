/* Shared, presentational request-detail drawer used by Activity.
 * This module intentionally holds no literal UI text: every label, heading and
 * button caption is supplied by the caller (already translated via gatewayI18n),
 * so this file has nothing for the i18n literal-inventory scanner to flag. */
(function () {
    'use strict';

    function copyTextViaExecCommand(text) {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'fixed';
        textarea.style.top = '-1000px';
        textarea.style.left = '-1000px';
        document.body.appendChild(textarea);
        textarea.select();
        textarea.setSelectionRange(0, textarea.value.length);
        let ok = false;
        try {
            ok = document.execCommand('copy');
        } catch (error) {
            ok = false;
        }
        document.body.removeChild(textarea);
        if (!ok) throw new Error('execCommand copy failed');
    }

    function copyText(text) {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
            return navigator.clipboard.writeText(text).catch(() => copyTextViaExecCommand(text));
        }
        return Promise.resolve().then(() => copyTextViaExecCommand(text));
    }

    function createCopyButton(getText, copyLabel, copiedLabel) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'req-drawer-copy-btn';
        button.textContent = copyLabel;
        button.addEventListener('click', (event) => {
            event.stopPropagation();
            copyText(String(getText())).then(() => {
                button.textContent = copiedLabel;
                button.disabled = true;
                setTimeout(() => {
                    button.textContent = copyLabel;
                    button.disabled = false;
                }, 1200);
            }).catch(() => undefined);
        });
        return button;
    }

    function createField(labelText, valueNode) {
        const row = document.createElement('div');
        row.className = 'req-drawer-field';
        const label = document.createElement('span');
        label.className = 'req-drawer-field-label';
        label.textContent = labelText;
        const value = document.createElement('div');
        value.className = 'req-drawer-field-value';
        if (valueNode instanceof Node) value.appendChild(valueNode);
        else value.textContent = String(valueNode);
        row.appendChild(label);
        row.appendChild(value);
        return row;
    }

    function createCopyableValue(text, copyLabel, copiedLabel) {
        const wrap = document.createElement('div');
        wrap.className = 'req-drawer-copyable';
        const code = document.createElement('code');
        code.textContent = text;
        wrap.appendChild(code);
        wrap.appendChild(createCopyButton(() => text, copyLabel, copiedLabel));
        return wrap;
    }

    function createSection(headingText) {
        const section = document.createElement('div');
        section.className = 'req-drawer-section';
        if (headingText) {
            const heading = document.createElement('h3');
            heading.className = 'req-drawer-section-heading';
            heading.textContent = headingText;
            section.appendChild(heading);
        }
        return section;
    }

    function createStatusPill(text, tone) {
        const pill = document.createElement('span');
        pill.className = `req-drawer-status-pill req-drawer-status-${tone}`;
        pill.textContent = text;
        return pill;
    }

    function createNote(text) {
        const note = document.createElement('p');
        note.className = 'req-drawer-note';
        note.textContent = text;
        return note;
    }

    // Lazily fetches and caches the master-only virtual-key list once, so callers
    // can resolve an api_key_id to its human-readable name without re-fetching it
    // for every drawer open. `apiFetch` is injected so this stays free of any
    // page-specific auth wiring.
    function createKeyNameResolver(apiFetch) {
        let cache = null;
        let pending = null;

        function load() {
            if (cache) return Promise.resolve(cache);
            if (pending) return pending;
            pending = apiFetch('/v1/admin/api-keys')
                .then((response) => {
                    if (!response.ok) throw new Error('failed to load api keys');
                    return response.json();
                })
                .then((data) => {
                    const map = new Map();
                    const records = Array.isArray(data) ? data : (data && data.keys) || [];
                    records.forEach((record) => {
                        if (record && record.id != null) {
                            map.set(Number(record.id), record.name || null);
                        }
                    });
                    cache = map;
                    pending = null;
                    return map;
                })
                .catch(() => {
                    pending = null;
                    return new Map();
                });
            return pending;
        }

        return {
            resolve(id) {
                if (id == null) return Promise.resolve(null);
                return load().then((map) => map.get(Number(id)) ?? null);
            },
        };
    }

    function create(config) {
        const mount = config.mount;

        const overlay = document.createElement('div');
        overlay.className = 'req-drawer-overlay';
        overlay.hidden = true;

        const panel = document.createElement('div');
        panel.className = 'req-drawer-panel';

        const head = document.createElement('div');
        head.className = 'req-drawer-head';

        const titleEl = document.createElement('h2');
        titleEl.id = config.titleId;
        titleEl.className = 'req-drawer-title';
        titleEl.textContent = config.heading;

        const closeBtn = document.createElement('button');
        closeBtn.type = 'button';
        closeBtn.className = 'req-drawer-close';
        closeBtn.setAttribute('aria-label', config.closeLabel);
        closeBtn.dataset.dialogClose = 'cancel';
        closeBtn.textContent = '×';

        head.appendChild(titleEl);
        head.appendChild(closeBtn);

        const body = document.createElement('div');
        body.className = 'req-drawer-body';

        panel.appendChild(head);
        panel.appendChild(body);
        overlay.appendChild(panel);
        mount.appendChild(overlay);

        const dialog = window.gatewayUi.createDialog({
            overlay,
            dialog: panel,
            inertRoots: config.inertRoots || [],
            labelledBy: config.titleId,
            initialFocus: () => closeBtn,
            openClass: 'visible',
            restoreFocus: config.restoreFocus,
            restoreFocusFallback: config.restoreFocusFallback,
        });

        return {
            open(contentNode) {
                body.replaceChildren(contentNode);
                if (window.gatewayAuth) {
                    window.gatewayUi.applyRoleVisibility(body, window.gatewayAuth.state);
                }
                dialog.open();
            },
            close(reason) {
                dialog.close(reason);
            },
            setChrome(heading, closeLabel) {
                titleEl.textContent = heading;
                closeBtn.setAttribute('aria-label', closeLabel);
            },
            get state() {
                return dialog.state;
            },
        };
    }

    window.RequestDrawer = {
        create,
        createField,
        createCopyableValue,
        createCopyButton,
        createSection,
        createStatusPill,
        createNote,
        createKeyNameResolver,
        copyText,
    };
})();
