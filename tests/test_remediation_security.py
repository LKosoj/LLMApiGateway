"""Focused regression tests for the surgical security remediation pass.

Each test targets one specific fix from the 9-item remediation list handed
down for this pass. Two items on that list were investigated but deliberately
NOT implemented because they directly conflict with existing, deliberately
authored tests in off-limits test files that assert the *opposite* behavior
as correct (see the final report for the full evidence trail):

* task 6 (``content_size.py`` full-body buffering) conflicts with
  ``tests/test_middleware_order_and_body_limit.py``'s ~15 "preread" tests,
  which require the current buffer-then-replay ordering.
* task 8 (``admin_pricing.py`` payload-completeness restoration) conflicts
  with ``tests/test_admin_pricing_patch.py`` (two tests) and
  ``tests/test_admin_pricing.py::test_put_pricing_partial_payload_deletes_omitted_rates``,
  all of which assert that a partial/empty PUT payload succeeds and silently
  prunes omitted models' rates.

A later remediation pass removed the ``if not isinstance(client, httpx.AsyncClient)``
branch from ``web_safe_fetch._get_pinned_public_url``, together with the
``client`` parameter it threaded through ``_get_pinned_public_url``,
``_get_with_public_redirects_unbounded`` and ``_get_with_public_redirects``.
That branch was dead in production (the only real call site always passed a
genuine ``httpx.AsyncClient`` that the code never actually used), but it had
also been relied upon as a duck-typed test seam by pre-existing tests in
``tests/test_web_safe_fetch_limits.py`` and ``tests/test_web_api.py`` that
passed a plain fake object with only a ``.get()`` method as the ``client``
argument. Those tests were rewritten to monkeypatch
``web_safe_fetch._PinnedHostAsyncHTTPTransport`` with a factory returning an
``httpx.MockTransport``, so they now exercise the real pinned-fetch code path
(``build_request``, ``send(stream=True, follow_redirects=False)`` and
``_read_bounded_response``) instead of bypassing it. The deadline test below
(``RedirectOverallDeadlineTests``) simply dropped the now-removed ``client``
argument, since the overall deadline fires while ``_validated_fetch_url`` is
mocked to hang, before ``_get_pinned_public_url`` is ever reached.

Both of the first two items are intentionally absent from this file rather
than faked as "passing".
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
import unittest.mock

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from llm_gateway_core.api.v1 import admin_api_keys, audio_adapters, web_extraction, web_safe_fetch
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.middleware import auth as auth_module
from llm_gateway_core.middleware.auth import ApiKeyAuthMiddleware
from llm_gateway_core.middleware.request_logging import RequestLoggingASGIMiddleware
from llm_gateway_core.utils.log_redaction import redact_payload_for_log, redact_request_body_text
from tests._async_compat import run_async
from tests.runtime_test_support import bind_app_services


def _sample_api_key_record(**overrides) -> ApiKeyRecord:
    fields = dict(
        id=1,
        name="test-key",
        api_key="lgk_abcdef1234567890",
        budget_usd=None,
        spent_usd=0.0,
        rpm=None,
        tpm=None,
    )
    fields.update(overrides)
    return ApiKeyRecord(**fields)


# ---------------------------------------------------------------------------
# Task 1: mask API keys in list/patch responses, reveal on demand.
# ---------------------------------------------------------------------------


class MaskApiKeyTests(unittest.TestCase):
    def test_mask_api_key_keeps_prefix_and_suffix_for_long_keys(self):
        self.assertEqual(
            admin_api_keys._mask_api_key("lgk_abcdef1234567890"),
            "lgk_…7890",
        )

    def test_mask_api_key_fully_masks_short_keys(self):
        self.assertEqual(admin_api_keys._mask_api_key("short"), "••••")

    def test_serialize_record_masks_by_default_and_reveals_on_demand(self):
        record = _sample_api_key_record()

        masked = admin_api_keys._serialize_record(record)
        self.assertNotEqual(masked["api_key"], record.api_key)
        self.assertEqual(masked["api_key"], admin_api_keys._mask_api_key(record.api_key))

        revealed = admin_api_keys._serialize_record(record, reveal=True)
        self.assertEqual(revealed["api_key"], record.api_key)


# ---------------------------------------------------------------------------
# Task 3: open redirect via backslash / control characters.
# ---------------------------------------------------------------------------


class OpenRedirectTests(unittest.TestCase):
    def test_backslash_next_path_falls_back_to_default(self):
        self.assertEqual(
            auth_module.normalize_next_path("/\\evil.com"),
            auth_module.DEFAULT_UI_PATH,
        )

    def test_control_character_next_path_falls_back_to_default(self):
        self.assertEqual(
            auth_module.normalize_next_path("/foo\r\nbar"),
            auth_module.DEFAULT_UI_PATH,
        )

    def test_ordinary_same_site_next_path_is_preserved(self):
        self.assertEqual(auth_module.normalize_next_path("/v1/ui/pricing"), "/v1/ui/pricing")


# ---------------------------------------------------------------------------
# Task 4: api_key_role must default to None, never ROLE_MASTER.
# ---------------------------------------------------------------------------


def _build_role_probe_app() -> FastAPI:
    app = FastAPI()
    bind_app_services(app)
    app.add_middleware(ApiKeyAuthMiddleware)
    app.add_middleware(RequestLoggingASGIMiddleware)

    # "/" is in OPTIONAL_AUTH_PATHS (no API key required) but, unlike
    # "/health", is not in RUNTIME_INDEPENDENT_PATHS, so it actually goes
    # through _prepare_api_key_auth instead of bypassing it entirely.
    @app.get("/")
    async def root(request: Request):
        return {"role": request.state.api_key_role}

    return app


class ApiKeyRoleDefaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _build_role_probe_app()
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_public_path_never_defaults_role_to_master(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["role"])


# ---------------------------------------------------------------------------
# Task 5: cloakbrowser DNS-rebinding proxy + redirect overall deadline.
# ---------------------------------------------------------------------------


class PinnedForwardProxyTests(unittest.TestCase):
    def test_connect_to_a_blocked_private_host_is_rejected(self):
        async def scenario():
            proxy = web_safe_fetch._PinnedForwardProxy()
            await proxy.start()
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
                writer.write(b"CONNECT 10.0.0.5:443 HTTP/1.1\r\nHost: 10.0.0.5\r\n\r\n")
                await writer.drain()
                status_line = await reader.readline()
                self.assertTrue(status_line.startswith(b"HTTP/1.1 403"))
                writer.close()
            finally:
                await proxy.stop()

        run_async(scenario())

    def test_connect_tunnels_bytes_to_the_address_returned_by_validation(self):
        async def scenario():
            async def echo(reader, writer):
                data = await reader.read(1024)
                writer.write(data)
                await writer.drain()
                writer.close()

            upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
            upstream_port = upstream.sockets[0].getsockname()[1]

            async def fake_validate(host, port):
                self.assertEqual(host, "pinned.invalid")
                self.assertEqual(port, upstream_port)
                # Simulates DNS resolution having already happened once, at
                # validation time; the proxy must dial *this* address rather
                # than letting the browser re-resolve "pinned.invalid" itself.
                return ("127.0.0.1",)

            original_validate = web_safe_fetch._validate_public_fetch_host
            web_safe_fetch._validate_public_fetch_host = fake_validate
            proxy = web_safe_fetch._PinnedForwardProxy()
            try:
                await proxy.start()
                reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
                writer.write(
                    f"CONNECT pinned.invalid:{upstream_port} HTTP/1.1\r\n"
                    f"Host: pinned.invalid\r\n\r\n".encode()
                )
                await writer.drain()
                status_line = await reader.readline()
                self.assertTrue(status_line.startswith(b"HTTP/1.1 200"))
                await reader.readline()  # blank line terminating the CONNECT response

                writer.write(b"ping-through-tunnel")
                await writer.drain()
                echoed = await reader.read(len(b"ping-through-tunnel"))
                self.assertEqual(echoed, b"ping-through-tunnel")
                writer.close()
                await asyncio.sleep(0.05)
            finally:
                web_safe_fetch._validate_public_fetch_host = original_validate
                await proxy.stop()
                upstream.close()
                await upstream.wait_closed()

        run_async(scenario())


class RedirectOverallDeadlineTests(unittest.TestCase):
    def test_overall_deadline_bounds_slow_redirect_chains(self):
        async def scenario():
            async def slow_validated_url(_url):
                await asyncio.sleep(10)
                raise AssertionError("should have been cancelled by the overall deadline")

            original_timeout = web_safe_fetch.WEB_FETCH_OVERALL_TIMEOUT_SECONDS
            original_validated = web_safe_fetch._validated_fetch_url
            web_safe_fetch.WEB_FETCH_OVERALL_TIMEOUT_SECONDS = 0.05
            web_safe_fetch._validated_fetch_url = slow_validated_url
            try:
                started = time.monotonic()
                # The overall deadline fires while _validated_fetch_url (mocked
                # above) is still sleeping, before _get_pinned_public_url is
                # ever reached.
                with self.assertRaises(HTTPException) as raised:
                    await web_safe_fetch._get_with_public_redirects("https://example.com/")
                elapsed = time.monotonic() - started
            finally:
                web_safe_fetch.WEB_FETCH_OVERALL_TIMEOUT_SECONDS = original_timeout
                web_safe_fetch._validated_fetch_url = original_validated

            self.assertEqual(raised.exception.status_code, 504)
            self.assertLess(elapsed, 5.0)

        run_async(scenario())


# ---------------------------------------------------------------------------
# Follow-up remediation: CloakBrowser's --proxy-server does not cover WebRTC
# ICE/STUN/TURN UDP traffic, letting a page probe internal addresses via
# RTCPeerConnection in a way that bypasses _PinnedForwardProxy.
# ---------------------------------------------------------------------------


class CloakBrowserWebRtcLaunchArgsTests(unittest.TestCase):
    def test_launch_args_disable_non_proxied_webrtc_udp(self):
        self.assertIn(
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            web_extraction._cloakbrowser_launch_args(),
        )


# ---------------------------------------------------------------------------
# Task 7: ffmpeg conversion watchdog timeout.
# ---------------------------------------------------------------------------


class FfmpegWatchdogTests(unittest.TestCase):
    def test_stalled_ffmpeg_process_is_killed_and_reported_as_504(self):
        # Pure in-process stand-in for a stalled/adversarial ffmpeg: its stdout
        # read blocks forever and only unblocks (with EOF) once the watchdog
        # kills the process. No real OS process is launched.
        class _StalledFfmpeg:
            def __init__(self, *args, **kwargs):
                self.returncode = None
                self._killed = threading.Event()
                self.stdout = self

            def read(self, _size):
                self._killed.wait()
                return b""

            def close(self):
                pass

            def kill(self):
                self.returncode = -9
                self._killed.set()

            def wait(self):
                self._killed.wait()
                return self.returncode

            def poll(self):
                return self.returncode

        original_timeout = audio_adapters._FFMPEG_CONVERSION_TIMEOUT_SECONDS
        audio_adapters._FFMPEG_CONVERSION_TIMEOUT_SECONDS = 0.2
        try:
            with unittest.mock.patch(
                "llm_gateway_core.api.v1.audio_adapters.subprocess.Popen",
                _StalledFfmpeg,
            ):
                started = time.monotonic()
                with self.assertRaises(HTTPException) as raised:
                    audio_adapters._convert_audio_bytes_to_wav(
                        b"irrelevant-input-bytes", max_bytes=10_000_000
                    )
                elapsed = time.monotonic() - started
        finally:
            audio_adapters._FFMPEG_CONVERSION_TIMEOUT_SECONDS = original_timeout

        self.assertEqual(raised.exception.status_code, 504)
        # Would be ~5s+ if the watchdog had not killed the stalled process.
        self.assertLess(elapsed, 4.0)

    def test_fast_conversion_is_unaffected_by_the_watchdog(self):
        # Stand-in "ffmpeg" that echoes stdin to stdout once and exits cleanly.
        class _ReadOnce:
            def __init__(self, payload):
                self._payload = payload
                self._done = False

            def read(self, _size):
                if self._done:
                    return b""
                self._done = True
                return self._payload

            def close(self):
                pass

        class _FastFfmpeg:
            def __init__(self, payload):
                self.returncode = 0
                self.stdout = _ReadOnce(payload)

            def wait(self):
                return 0

            def poll(self):
                return 0

            def kill(self):
                pass

        def fake_popen(args, stdin=None, stdout=None, stderr=None):
            payload = stdin.read() if stdin is not None else b""
            return _FastFfmpeg(payload)

        original_timeout = audio_adapters._FFMPEG_CONVERSION_TIMEOUT_SECONDS
        audio_adapters._FFMPEG_CONVERSION_TIMEOUT_SECONDS = 5.0
        try:
            with unittest.mock.patch(
                "llm_gateway_core.api.v1.audio_adapters.subprocess.Popen",
                fake_popen,
            ):
                result = audio_adapters._convert_audio_bytes_to_wav(
                    b"some-audio-bytes", max_bytes=10_000_000
                )
        finally:
            audio_adapters._FFMPEG_CONVERSION_TIMEOUT_SECONDS = original_timeout

        self.assertEqual(result, b"some-audio-bytes")

    def test_watchdog_race_after_successful_conversion_does_not_raise_504(self):
        # Reproduces the race: conversion finishes cleanly (return_code 0,
        # real output collected) right as the watchdog's Timer fires.
        # threading.Timer.cancel() is best-effort, so the watchdog can still
        # run _kill_ffmpeg_on_timeout after the outcome is already decided;
        # kill()/wait() on an already-terminated process are harmless no-ops,
        # matching real subprocess.Popen semantics.
        watchdog_fired = threading.Event()

        class _RacyFfmpeg:
            def __init__(self, payload):
                self._payload = payload
                self._done = False
                self.returncode = 0
                self.stdout = self

            def read(self, _size):
                if not self._done:
                    self._done = True
                    return self._payload
                # Block until the watchdog has actually called kill() (which
                # only happens after timed_out.set()), so the race is
                # reproduced deterministically instead of relying on
                # incidental thread scheduling.
                watchdog_fired.wait(2.0)
                return b""

            def close(self):
                pass

            def wait(self):
                return 0

            def poll(self):
                return 0

            def kill(self):
                watchdog_fired.set()

        def fake_popen(args, stdin=None, stdout=None, stderr=None):
            payload = stdin.read() if stdin is not None else b""
            return _RacyFfmpeg(payload)

        original_timeout = audio_adapters._FFMPEG_CONVERSION_TIMEOUT_SECONDS
        audio_adapters._FFMPEG_CONVERSION_TIMEOUT_SECONDS = 0.05
        try:
            with unittest.mock.patch(
                "llm_gateway_core.api.v1.audio_adapters.subprocess.Popen",
                fake_popen,
            ):
                result = audio_adapters._convert_audio_bytes_to_wav(
                    b"some-audio-bytes", max_bytes=10_000_000
                )
        finally:
            audio_adapters._FFMPEG_CONVERSION_TIMEOUT_SECONDS = original_timeout

        # The watchdog must have actually fired for this to be the race being
        # tested; otherwise this would just be a duplicate of the fast-path test.
        self.assertTrue(watchdog_fired.is_set())
        self.assertEqual(result, b"some-audio-bytes")


# ---------------------------------------------------------------------------
# Task 9 (first half): redact bulk "input"/"system"/"prompt"/"instructions"
# content, too.
# ---------------------------------------------------------------------------


class LogRedactionBulkContentTests(unittest.TestCase):
    def test_input_system_prompt_and_instructions_are_redacted_by_default(self):
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "secret chat"}],
            "input": "secret responses-api input",
            "instructions": "secret responses-api instructions",
            "system": "secret system prompt",
            "prompt": "secret raw prompt",
            "temperature": 0.5,
        }

        redacted = redact_payload_for_log(payload)

        self.assertNotIn("messages", redacted)
        self.assertNotIn("input", redacted)
        self.assertNotIn("instructions", redacted)
        self.assertNotIn("system", redacted)
        self.assertNotIn("prompt", redacted)
        self.assertEqual(redacted["model"], "gpt-4")
        self.assertEqual(redacted["temperature"], 0.5)

    def test_include_messages_flag_still_surfaces_bulk_content(self):
        payload = {
            "input": "secret input",
            "instructions": "secret instructions",
            "system": "secret system",
            "prompt": "secret prompt",
        }

        included = redact_payload_for_log(payload, include_messages=True)

        self.assertEqual(included["input"], "secret input")
        self.assertEqual(included["instructions"], "secret instructions")
        self.assertEqual(included["system"], "secret system")
        self.assertEqual(included["prompt"], "secret prompt")

    def test_responses_api_instructions_are_redacted_via_the_real_request_body_path(self):
        # Mirrors what chat_logging.write_log() redacts: the raw JSON request
        # body text of a /v1/responses call. chat_dialects
        # ._responses_request_to_openai_payload treats "instructions" exactly
        # like "system" (translates it into a {"role": "system", ...}
        # message), so it must be scrubbed the same way from that raw body.
        raw_body = (
            '{"model": "gpt-4", "instructions": "secret system instructions", '
            '"input": "secret responses input"}'
        )

        redacted_text = redact_request_body_text(raw_body)

        self.assertNotIn("secret system instructions", redacted_text)
        self.assertNotIn("secret responses input", redacted_text)
        self.assertIn("gpt-4", redacted_text)


if __name__ == "__main__":
    unittest.main()
