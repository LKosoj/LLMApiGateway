/**
 * Unified theme manager for LLM Gateway UI.
 *
 * Public API:
 *   Theme.init()                 — read persisted preference, apply, listen for system changes
 *   Theme.get()                  — {mode, effective}
 *   Theme.set(mode)              — mode: 'light' | 'dark' | 'system'
 *   Theme.cycle()                — rotate light -> dark -> system -> light
 *   Theme.attachToggle(id)       — wire up a button and keep its label updated
 *
 * Storage key: 'llmgateway:theme'
 * Legacy keys migrated on first init: 'darkMode' ('enabled'/'disabled'/'true'/'false'/'1'/'0'), 'theme' ('light'/'dark')
 */

(function (global) {
    'use strict';

    var STORAGE_KEY = 'llmgateway:theme';
    var LEGACY_KEYS = ['darkMode', 'theme'];
    var EVENT_NAME = 'llmgateway:theme-changed';
    var CYCLE_ORDER = ['light', 'dark', 'system'];

    // Icons/labels for the toggle button
    var LABELS = { light: '☀', dark: '☾', system: '◎' };
    var ICONS = {
        light: '&#9728;',   // sun unicode ☀
        dark:  '&#9790;',   // crescent moon ☾
        system: '&#9741;'   // monitor-ish ⊥ fallback; use text if icon not suitable
    };

    function _mq() {
        return global.matchMedia && global.matchMedia('(prefers-color-scheme: dark)');
    }

    function _systemIsDark() {
        var mq = _mq();
        return mq ? mq.matches : false;
    }

    function _effective(mode) {
        if (mode === 'dark') return 'dark';
        if (mode === 'light') return 'light';
        return _systemIsDark() ? 'dark' : 'light';
    }

    /** Migrate legacy localStorage keys to the unified key. */
    function _migrate() {
        try {
            // Already migrated
            if (localStorage.getItem(STORAGE_KEY) !== null) {
                // Still remove legacy keys so they don't accumulate
                LEGACY_KEYS.forEach(function (k) { localStorage.removeItem(k); });
                return;
            }

            var darkMode = localStorage.getItem('darkMode');
            var themeLegacy = localStorage.getItem('theme');

            var resolved = null;
            if (darkMode === 'enabled' || darkMode === 'true' || darkMode === '1') {
                resolved = 'dark';
            } else if (darkMode === 'disabled' || darkMode === 'false' || darkMode === '0') {
                resolved = 'light';
            } else if (themeLegacy === 'dark') {
                resolved = 'dark';
            } else if (themeLegacy === 'light') {
                resolved = 'light';
            }

            if (resolved !== null) {
                localStorage.setItem(STORAGE_KEY, resolved);
            }
            LEGACY_KEYS.forEach(function (k) { localStorage.removeItem(k); });
        } catch (e) {
            // localStorage may be unavailable (private mode / security policy)
        }
    }

    function _read() {
        try {
            var v = localStorage.getItem(STORAGE_KEY);
            if (v === 'light' || v === 'dark' || v === 'system') return v;
        } catch (e) {
            // ignore
        }
        return 'system';
    }

    function _write(mode) {
        try {
            localStorage.setItem(STORAGE_KEY, mode);
        } catch (e) {
            // ignore
        }
    }

    function _apply(effectiveTheme) {
        document.body.classList.toggle('dark-mode', effectiveTheme === 'dark');
    }

    function _dispatch(mode, effective) {
        try {
            document.dispatchEvent(
                new CustomEvent(EVENT_NAME, { detail: { mode: mode, effective: effective } })
            );
        } catch (e) {
            // CustomEvent may not be available in very old browsers
        }
    }

    // System-preference listener handle so we can remove it if mode changes away from 'system'
    var _systemListener = null;

    function _detachSystemListener() {
        if (_systemListener) {
            var mq = _mq();
            if (mq && mq.removeEventListener) {
                mq.removeEventListener('change', _systemListener);
            } else if (mq && mq.removeListener) {
                mq.removeListener(_systemListener);
            }
            _systemListener = null;
        }
    }

    function _attachSystemListener(mode) {
        _detachSystemListener();
        if (mode !== 'system') return;
        var mq = _mq();
        if (!mq) return;

        _systemListener = function () {
            var eff = _effective('system');
            _apply(eff);
            _dispatch('system', eff);
            _updateAllToggles();
        };

        if (mq.addEventListener) {
            mq.addEventListener('change', _systemListener);
        } else if (mq.addListener) {
            // Safari < 14 fallback
            mq.addListener(_systemListener);
        }
    }

    // Registry of attached toggle elements
    var _toggles = [];
    var _translator = null;

    function _buttonLabel(mode, translator) {
        var keyMap = {
            light: 'common:theme.switchToDark',
            dark: 'common:theme.switchToSystem',
            system: 'common:theme.switchToLight'
        };
        if (!Object.prototype.hasOwnProperty.call(keyMap, mode)) {
            throw new Error('Theme: invalid mode "' + mode + '"');
        }
        var aria = null;
        if (translator !== null) {
            aria = translator(keyMap[mode]);
            if (typeof aria !== 'string' || aria.length === 0) {
                throw new Error('Theme: translation must be a non-empty string for ' + keyMap[mode]);
            }
        }
        return {
            text: LABELS[mode],
            icon: ICONS[mode],
            aria: aria
        };
    }

    function _updateToggleEl(el, mode, label) {
        if (label.aria !== null) {
            el.setAttribute('aria-label', label.aria);
            el.setAttribute('title', label.aria);
        }
        el.setAttribute('data-theme-mode', mode);
        el.textContent = label.text;
    }

    function _updateAllToggles() {
        var mode = _read();
        var label = _buttonLabel(mode, _translator);
        _toggles.forEach(function (el) {
            _updateToggleEl(el, mode, label);
        });
    }

    var Theme = {
        init: function () {
            _migrate();
            var mode = _read();
            var eff = _effective(mode);
            _apply(eff);
            _attachSystemListener(mode);
        },

        get: function () {
            var mode = _read();
            return { mode: mode, effective: _effective(mode) };
        },

        set: function (mode) {
            if (mode !== 'light' && mode !== 'dark' && mode !== 'system') {
                throw new Error('Theme.set: invalid mode "' + mode + '"');
            }
            _buttonLabel(mode, _translator);
            _write(mode);
            var eff = _effective(mode);
            _apply(eff);
            _attachSystemListener(mode);
            _dispatch(mode, eff);
            _updateAllToggles();
        },

        cycle: function () {
            var current = _read();
            var idx = CYCLE_ORDER.indexOf(current);
            var next = CYCLE_ORDER[(idx + 1) % CYCLE_ORDER.length];
            Theme.set(next);
        },

        setTranslator: function (translator) {
            if (typeof translator !== 'function') {
                throw new TypeError('Theme.setTranslator: translator must be a function');
            }
            var mode = _read();
            var label = _buttonLabel(mode, translator);
            _translator = translator;
            _toggles.forEach(function (el) {
                _updateToggleEl(el, mode, label);
            });
        },

        attachToggle: function (elementId) {
            var el = typeof elementId === 'string'
                ? document.getElementById(elementId)
                : elementId;
            if (!el) return;

            // Avoid duplicate registration
            if (_toggles.indexOf(el) === -1) {
                _toggles.push(el);
            }

            el.addEventListener('click', function () {
                Theme.cycle();
            });

            // Set initial label
            var mode = _read();
            _updateToggleEl(el, mode, _buttonLabel(mode, _translator));
        }
    };

    global.Theme = Theme;

    // Auto-init
    if (document.readyState !== 'loading') {
        Theme.init();
    } else {
        document.addEventListener('DOMContentLoaded', function () { Theme.init(); });
    }
})(window);
