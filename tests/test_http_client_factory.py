import asyncio
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI

import main
from llm_gateway_core.services import http_client_factory
from llm_gateway_core.services.http_client_factory import (
    HttpClientCloseFailure,
    ProxyClientConstructionError,
    ProxyConfigurationError,
    close_http_clients,
    create_proxy_http_clients,
    create_shared_http_client,
    populate_proxy_http_clients,
)
from tests._async_compat import run_async


def _provider(proxy: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(proxy=proxy)


class HttpClientFactoryTests(unittest.TestCase):
    def test_shared_and_proxy_clients_use_the_same_canonical_options(self):
        shared_client = Mock()
        proxy_client = Mock()

        with patch.object(
            http_client_factory.httpx,
            "AsyncClient",
            side_effect=[shared_client, proxy_client],
        ) as constructor:
            self.assertIs(create_shared_http_client(), shared_client)
            clients = run_async(
                create_proxy_http_clients({"proxied": _provider("https://proxy.example:8443")})
            )

        self.assertEqual(clients, {"proxied": proxy_client})
        shared_kwargs = constructor.call_args_list[0].kwargs
        proxy_kwargs = constructor.call_args_list[1].kwargs
        for kwargs in (shared_kwargs, proxy_kwargs):
            self.assertTrue(kwargs["http2"])
            self.assertEqual(
                kwargs["timeout"].connect,
                http_client_factory.HTTP_CLIENT_CONNECT_TIMEOUT_SECONDS,
            )
            self.assertEqual(
                kwargs["timeout"].read,
                http_client_factory.HTTP_CLIENT_READ_TIMEOUT_SECONDS,
            )
            self.assertEqual(
                kwargs["limits"].max_connections,
                http_client_factory.HTTP_CLIENT_MAX_CONNECTIONS,
            )
            self.assertEqual(
                kwargs["limits"].max_keepalive_connections,
                http_client_factory.HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS,
            )
        self.assertNotIn("proxy", shared_kwargs)
        self.assertEqual(proxy_kwargs["proxy"].url.host, "proxy.example")

    def test_literal_env_socks5_and_no_proxy_are_supported(self):
        clients = [Mock(), Mock(), Mock()]
        for client in clients:
            client.aclose = AsyncMock()

        with (
            patch.dict(os.environ, {"TEST_PROVIDER_PROXY": "http://env-proxy.example:8080"}),
            patch.object(http_client_factory.httpx, "AsyncClient", side_effect=clients) as constructor,
        ):
            result = run_async(
                create_proxy_http_clients(
                    {
                        "literal": _provider("https://literal-proxy.example"),
                        "environment": _provider("${TEST_PROVIDER_PROXY}"),
                        "socks": _provider("socks5://socks-proxy.example:1080"),
                        "direct": _provider(),
                    }
                )
            )

        self.assertEqual(set(result), {"literal", "environment", "socks"})
        self.assertEqual(constructor.call_count, 3)
        self.assertEqual(
            [call.kwargs["proxy"].url.scheme for call in constructor.call_args_list],
            ["https", "http", "socks5"],
        )
        run_async(close_http_clients(result))

    def test_all_proxies_are_validated_before_any_client_is_constructed(self):
        with patch.object(http_client_factory.httpx, "AsyncClient") as constructor:
            with self.assertRaisesRegex(ProxyConfigurationError, "provider 'broken'") as raised:
                run_async(
                    create_proxy_http_clients(
                        {
                            "valid": _provider("https://user:password@proxy.example"),
                            "broken": _provider("http://"),
                        }
                    )
                )

        constructor.assert_not_called()
        self.assertNotIn("password", str(raised.exception))
        self.assertNotIn("http://", str(raised.exception))

    def test_unknown_scheme_and_empty_host_are_rejected_without_credentials(self):
        for proxy in ("ftp://user:top-secret@proxy.example", "http://user:top-secret@"):
            with self.subTest(proxy=proxy):
                with self.assertRaises(ProxyConfigurationError) as raised:
                    run_async(create_proxy_http_clients({"unsafe": _provider(proxy)}))
                self.assertEqual(
                    str(raised.exception),
                    "Invalid proxy configuration for provider 'unsafe'.",
                )
                self.assertNotIn("top-secret", str(raised.exception))

    def test_partial_constructor_failure_closes_every_constructed_client(self):
        first_client = Mock()
        first_client.aclose = AsyncMock()

        with patch.object(
            http_client_factory.httpx,
            "AsyncClient",
            side_effect=[first_client, RuntimeError("constructor failed")],
        ):
            with self.assertRaisesRegex(ProxyClientConstructionError, "provider 'second'"):
                run_async(
                    create_proxy_http_clients(
                        {
                            "first": _provider("http://first.example"),
                            "second": _provider("http://second.example"),
                        }
                    )
                )

        first_client.aclose.assert_awaited_once()

    def test_populate_registers_each_client_before_constructing_next(self):
        first_client = Mock()
        second_client = Mock()
        events: list[tuple[str, str]] = []

        def construct_client(**kwargs):
            provider_name = kwargs["proxy"].url.host.split(".")[0]
            events.append(("construct", provider_name))
            return {
                "first": first_client,
                "second": second_client,
            }[provider_name]

        registered: dict[str, httpx.AsyncClient] = {}

        def register_client(provider_name, client):
            events.append(("register", provider_name))
            registered[provider_name] = client

        with patch.object(
            http_client_factory.httpx,
            "AsyncClient",
            side_effect=construct_client,
        ):
            result = run_async(
                populate_proxy_http_clients(
                    {
                        "first": _provider("http://first.example"),
                        "second": _provider("http://second.example"),
                    },
                    register_client=register_client,
                )
            )

        self.assertEqual(
            events,
            [
                ("construct", "first"),
                ("register", "first"),
                ("construct", "second"),
                ("register", "second"),
            ],
        )
        self.assertEqual(result, {"first": first_client, "second": second_client})
        self.assertEqual(registered, result)

    def test_populate_constructor_failure_leaves_registered_clients_owned(self):
        first_client = Mock()
        first_client.aclose = AsyncMock()
        registered: dict[str, httpx.AsyncClient] = {}

        with patch.object(
            http_client_factory.httpx,
            "AsyncClient",
            side_effect=[first_client, RuntimeError("constructor failed")],
        ):
            with self.assertRaisesRegex(ProxyClientConstructionError, "provider 'second'"):
                run_async(
                    populate_proxy_http_clients(
                        {
                            "first": _provider("http://first.example"),
                            "second": _provider("http://second.example"),
                        },
                        register_client=registered.__setitem__,
                    )
                )

        self.assertEqual(registered, {"first": first_client})
        first_client.aclose.assert_not_awaited()

    def test_populate_propagates_terminal_constructor_error_unchanged(self):
        terminal = KeyboardInterrupt("stop")
        first_client = Mock()
        first_client.aclose = AsyncMock()
        registered: dict[str, httpx.AsyncClient] = {}

        with patch.object(
            http_client_factory.httpx,
            "AsyncClient",
            side_effect=[first_client, terminal],
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                run_async(
                    populate_proxy_http_clients(
                        {
                            "first": _provider("http://first.example"),
                            "second": _provider("http://second.example"),
                        },
                        register_client=registered.__setitem__,
                    )
                )

        self.assertIs(raised.exception, terminal)
        self.assertEqual(registered, {"first": first_client})
        first_client.aclose.assert_not_awaited()

    def test_base_exception_is_cleaned_up_and_propagated_unchanged(self):
        first_client = Mock()
        first_client.aclose = AsyncMock()

        with patch.object(
            http_client_factory.httpx,
            "AsyncClient",
            side_effect=[first_client, KeyboardInterrupt("stop")],
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "stop"):
                run_async(
                    create_proxy_http_clients(
                        {
                            "first": _provider("http://first.example"),
                            "second": _provider("http://second.example"),
                        }
                    )
                )

        first_client.aclose.assert_awaited_once()

    def test_close_continues_after_an_individual_client_failure(self):
        failing_client = Mock()
        failing_client.aclose = AsyncMock(side_effect=RuntimeError("close failed"))
        healthy_client = Mock()
        healthy_client.aclose = AsyncMock()

        with self.assertLogs(http_client_factory.logger, level="ERROR") as captured:
            failures = run_async(
                close_http_clients({"failing": failing_client, "healthy": healthy_client})
            )

        failing_client.aclose.assert_awaited_once()
        healthy_client.aclose.assert_awaited_once()
        self.assertEqual(
            failures,
            (HttpClientCloseFailure("failing", "RuntimeError"),),
        )
        diagnostics = "\n".join(captured.output)
        self.assertIn("RuntimeError", diagnostics)
        self.assertNotIn("close failed", diagnostics)

    def test_close_sanitizes_dynamic_exception_class_name(self):
        unsafe_type = type("Unsafe\nproxy-secret", (RuntimeError,), {})
        failing_client = Mock()
        failing_client.aclose = AsyncMock(side_effect=unsafe_type())

        with self.assertLogs(http_client_factory.logger, level="ERROR") as captured:
            failures = run_async(close_http_clients({"failing": failing_client}))

        self.assertEqual(
            failures,
            (HttpClientCloseFailure("failing", "BaseException"),),
        )
        diagnostics = "\n".join(captured.output)
        self.assertIn("BaseException", diagnostics)
        self.assertNotIn("proxy-secret", diagnostics)

    def test_close_keyboard_interrupt_still_closes_remaining_clients(self):
        terminal = KeyboardInterrupt("stop")
        first_client = Mock()
        first_client.aclose = AsyncMock(side_effect=terminal)
        second_client = Mock()
        second_client.aclose = AsyncMock()

        with self.assertRaises(KeyboardInterrupt) as raised:
            run_async(close_http_clients({"first": first_client, "second": second_client}))

        self.assertIs(raised.exception, terminal)
        second_client.aclose.assert_awaited_once()

    def test_close_system_exit_still_closes_remaining_clients(self):
        terminal = SystemExit(7)
        first_client = Mock()
        first_client.aclose = AsyncMock(side_effect=terminal)
        second_client = Mock()
        second_client.aclose = AsyncMock()

        with self.assertRaises(SystemExit) as raised:
            run_async(close_http_clients({"first": first_client, "second": second_client}))

        self.assertIs(raised.exception, terminal)
        second_client.aclose.assert_awaited_once()

    def test_close_cancellation_still_closes_remaining_clients(self):
        terminal = asyncio.CancelledError("cancelled")
        first_client = Mock()
        first_client.aclose = AsyncMock(side_effect=terminal)
        second_client = Mock()
        second_client.aclose = AsyncMock()

        with self.assertRaises(asyncio.CancelledError) as raised:
            run_async(close_http_clients({"first": first_client, "second": second_client}))

        self.assertIs(raised.exception, terminal)
        second_client.aclose.assert_awaited_once()

    def test_rules_editor_depends_on_factory_not_main_or_direct_async_client(self):
        source = (main.PROJECT_ROOT / "llm_gateway_core/api/v1/rules_editor.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("from main import", source)
        self.assertNotIn("httpx.AsyncClient(", source)


class LifespanHttpCleanupTests(unittest.TestCase):
    def test_late_startup_failure_closes_shared_and_proxy_clients(self):
        cleanup_order: list[str] = []
        config_loader = Mock()
        config_loader.providers_config = {}
        config_loader.fallback_rules = {}
        config_loader.model_rules = {}
        config_loader.operation_rules = {}
        config_loader.fusion_rules = {}
        config_loader.router_rules = {}
        config_loader.load_complete.return_value = config_loader

        shared_client = Mock(spec=httpx.AsyncClient)
        shared_client.aclose = AsyncMock(side_effect=lambda: cleanup_order.append("shared"))
        proxy_client = Mock(spec=httpx.AsyncClient)
        proxy_client.aclose = AsyncMock(side_effect=lambda: cleanup_order.append("proxy"))
        write_batcher = Mock()
        write_batcher.start = AsyncMock()
        write_batcher.stop = AsyncMock()
        accounting_service = Mock()
        accounting_service.start = AsyncMock()
        accounting_service.stop = AsyncMock()
        openrouter_service = Mock()
        openrouter_service.start_runtime = AsyncMock()
        def fail_openrouter_stop() -> None:
            cleanup_order.append("openrouter")
            raise RuntimeError("stop failed")

        openrouter_service.stop = AsyncMock(side_effect=fail_openrouter_stop)
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()

        async def populate_proxy_clients(_providers, *, register_client):
            register_client("proxied", proxy_client)
            return {"proxied": proxy_client}

        async def scenario() -> None:
            app = FastAPI()
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(main.settings, "gateway_api_key", "test-gateway-key")
                )
                stack.enter_context(patch("main.preload_templates"))
                stack.enter_context(patch("main.ConfigLoader", return_value=config_loader))
                stack.enter_context(
                    patch.object(main.AtomicConfigFileTransaction, "recover_pending")
                )
                stack.enter_context(patch("main.TokensUsageDB"))
                stack.enter_context(patch("main.FallbackEventsDB"))
                stack.enter_context(patch("main.RejectionsDB"))
                stack.enter_context(patch("main.ApiKeysDB"))
                stack.enter_context(patch("main.ModelRotationDB"))
                stack.enter_context(patch("main.WriteBatcher", return_value=write_batcher))
                stack.enter_context(
                    patch("main.AccountingService", return_value=accounting_service)
                )
                stack.enter_context(
                    patch("main.create_shared_http_client", return_value=shared_client)
                )
                stack.enter_context(
                    patch(
                        "llm_gateway_core.services.runtime_candidate.populate_proxy_http_clients",
                        AsyncMock(side_effect=populate_proxy_clients),
                    )
                )
                stack.enter_context(
                    patch("llm_gateway_core.services.runtime_candidate.OperationDispatcher")
                )
                stack.enter_context(
                    patch("llm_gateway_core.services.runtime_candidate.FusionEnsembleService")
                )
                stack.enter_context(
                    patch("llm_gateway_core.services.runtime_candidate.RouterModelService")
                )
                stack.enter_context(
                    patch("llm_gateway_core.services.runtime_candidate.ProviderModelsService")
                )
                stack.enter_context(
                    patch(
                        "main.OpenRouterFreeModelsService",
                        return_value=openrouter_service,
                    )
                )
                stack.enter_context(patch("main.FallbackModelEvalService"))
                stack.enter_context(
                    patch(
                        "main.ConfigUpdateCoordinator",
                        return_value=config_update_coordinator,
                    )
                )
                stack.enter_context(
                    patch(
                        "main.run_startup_model_verification",
                        AsyncMock(side_effect=ValueError("late startup failure")),
                    )
                )
                with self.assertRaisesRegex(ValueError, "late startup failure"):
                    async with main.lifespan(app):
                        self.fail("lifespan unexpectedly reached yield")

        run_async(scenario())
        shared_client.aclose.assert_awaited_once()
        proxy_client.aclose.assert_awaited_once()
        openrouter_service.stop.assert_awaited_once()
        self.assertEqual(cleanup_order, ["openrouter", "proxy", "shared"])


if __name__ == "__main__":
    unittest.main()
