from pathlib import Path

import pytest
from playwright.sync_api import Page, expect


pytestmark = pytest.mark.browser

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "static" / "vendor" / "ui-runtime.bundle.js"


def _load_runtime(page: Page, body: str) -> None:
    page.route(
        "http://gateway.test/**",
        lambda route: route.fulfill(status=200, content_type="text/html", body=""),
    )
    page.goto("http://gateway.test/")
    page.set_content(
        f'<html lang="en" dir="ltr" data-i18n-page="runtime"><body>{body}</body></html>'
    )
    page.evaluate(
        """
        () => {
            window.fetch = async () => ({
                ok: true,
                status: 200,
                json: async () => ({readyMessage: "Ready"}),
            });
        }
        """
    )
    page.add_script_tag(path=str(BUNDLE))
    page.wait_for_function("window.gatewayUi && window.gatewayI18n")
    page.evaluate("() => window.gatewayI18n.ready")


def test_tabs_primitive_has_real_browser_keyboard_rtl_and_repair_contract(page: Page) -> None:
    _load_runtime(
        page,
        """
        <div id="tabs" role="tablist">
          <button role="tab" data-tab-key="one">One</button>
          <button role="tab" data-tab-key="two">Two</button>
          <button role="tab" data-tab-key="three">Three</button>
        </div>
        <section>Panel one</section><section>Panel two</section><section>Panel three</section>
        """,
    )
    page.evaluate(
        """
        () => {
            const root = document.querySelector("#tabs");
            window.r52Tabs = gatewayUi.createTabs(root, {
                initialKey: "one",
                panels: [...document.querySelectorAll("section")],
            });
        }
        """
    )

    tabs = page.locator('[role="tab"]')
    panels = page.locator('[role="tabpanel"]')
    expect(tabs.nth(0)).to_have_attribute("aria-selected", "true")
    expect(tabs.nth(0)).to_have_attribute("tabindex", "0")
    expect(panels.nth(0)).to_have_attribute("aria-labelledby", tabs.nth(0).get_attribute("id"))

    tabs.nth(0).focus()
    page.keyboard.press("ArrowRight")
    expect(tabs.nth(1)).to_be_focused()
    expect(tabs.nth(0)).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Enter")
    expect(tabs.nth(1)).to_have_attribute("aria-selected", "true")

    page.evaluate("document.documentElement.dir = 'rtl'")
    page.keyboard.press("ArrowRight")
    expect(tabs.nth(0)).to_be_focused()

    page.evaluate(
        """
        () => {
            const active = document.querySelector('[role="tab"][aria-selected="true"]');
            active.hidden = true;
        }
        """
    )
    page.wait_for_function("window.r52Tabs.activeKey !== 'two'")
    assert page.locator('[role="tab"][aria-selected="true"]:visible').count() == 1


def test_tabs_primitive_rejects_stale_async_activation_in_a_real_dom(page: Page) -> None:
    _load_runtime(
        page,
        """
        <div id="tabs" role="tablist">
          <button role="tab" data-tab-key="one">One</button>
          <button role="tab" data-tab-key="slow">Slow</button>
          <button role="tab" data-tab-key="current">Current</button>
        </div>
        <section>One</section><section>Slow</section><section>Current</section>
        """,
    )
    result = page.evaluate(
        """
        async () => {
            let releaseSlow;
            const slow = new Promise((resolve) => { releaseSlow = resolve; });
            const writes = [];
            const controller = gatewayUi.createTabs(document.querySelector("#tabs"), {
                panels: [...document.querySelectorAll("section")],
                async beforeActivate(context) {
                    if (context.key === "slow") await slow;
                    return true;
                },
                async onActivate(context) {
                    await Promise.resolve();
                    if (context.isCurrent()) writes.push(context.key);
                },
            });
            const obsolete = controller.activate("slow");
            const current = controller.activate("current");
            await current;
            releaseSlow();
            return {
                obsolete: await obsolete,
                activeKey: controller.activeKey,
                writes,
            };
        }
        """
    )

    assert result == {"obsolete": False, "activeKey": "current", "writes": ["current"]}


def test_dialog_primitive_traps_and_restores_focus_in_chromium(page: Page) -> None:
    _load_runtime(
        page,
        """
        <main id="page-content"><button id="open-dialog">Open</button></main>
        <div id="overlay">
          <div id="dialog" aria-labelledby="dialog-title">
            <h2 id="dialog-title">Edit key</h2>
            <input id="dialog-input">
            <button id="dialog-cancel" data-dialog-close="cancel">Cancel</button>
          </div>
        </div>
        """,
    )
    page.evaluate(
        """
        () => {
            const trigger = document.querySelector("#open-dialog");
            window.r52Dialog = gatewayUi.createDialog({
                overlay: document.querySelector("#overlay"),
                dialog: document.querySelector("#dialog"),
                inertRoots: [document.querySelector("#page-content")],
                restoreFocus: () => document.querySelector("#open-dialog"),
            });
            trigger.addEventListener("click", () => window.r52Dialog.open());
        }
        """
    )

    expect(page.locator("#overlay")).to_be_hidden()
    page.locator("#open-dialog").click()
    expect(page.locator("#dialog")).to_have_attribute("role", "dialog")
    expect(page.locator("#dialog")).to_have_attribute("aria-modal", "true")
    expect(page.locator("#page-content")).to_have_attribute("inert", "")
    expect(page.locator("#dialog-input")).to_be_focused()

    page.locator("#dialog-cancel").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#dialog-input")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator("#overlay")).to_be_hidden()
    expect(page.locator("#open-dialog")).to_be_focused()
    expect(page.locator("#page-content")).not_to_have_attribute("inert", "")


def test_status_primitive_keeps_raw_detail_out_of_the_live_region(page: Page) -> None:
    _load_runtime(page, '<div id="status"></div><pre id="raw"></pre>')
    result = page.evaluate(
        """
        () => {
            const callbacks = [];
            const status = gatewayUi.createStatus(document.querySelector("#status"), {
                rawDetailElement: document.querySelector("#raw"),
                setTimeout: (callback) => { callbacks.push(callback); return callbacks.length; },
                clearTimeout: () => {},
            });
            status.polite("Saved", {timeoutMs: 100});
            status.error("Failed", {rawDetail: "upstream trace"});
            callbacks[0]();
            return {
                status: document.querySelector("#status").outerHTML,
                raw: document.querySelector("#raw").outerHTML,
                state: status.state.kind,
            };
        }
        """
    )

    assert result["state"] == "error"
    assert 'role="alert"' in result["status"]
    assert 'aria-live="assertive"' in result["status"]
    assert "Failed" in result["status"]
    assert 'lang="und"' in result["raw"]
    assert 'dir="auto"' in result["raw"]
    assert "aria-live" not in result["raw"]
    assert "upstream trace" in result["raw"]
