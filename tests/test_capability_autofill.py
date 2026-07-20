import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.v1.rules_editor import _trigger_capability_autofill, editor_router
from llm_gateway_core.config.config_store import ConfigFile
from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.middleware.auth import ROLE_MASTER, ROLE_USER
from llm_gateway_core.services.capability_autofill import (
    CapabilityAutofillService,
    _build_model_fallback_configs,
    _serialize_fallback_rules,
)
from llm_gateway_core.services.config_updates import ConfigUpdateError, ConfigUpdateErrorCode
from llm_gateway_core.services.openrouter_free_models import CapabilityMetadata
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


class _FakeLease:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.released = False

    def release(self):
        self.released = True


class _FakeRuntimeManager:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.acquired_leases = []

    def acquire_current(self):
        lease = _FakeLease(self._snapshot)
        self.acquired_leases.append(lease)
        return lease


class _FakeConfigLoader:
    def __init__(self, providers_config, fallback_rules_base):
        self.providers_config = providers_config
        self._fallback_rules_base = fallback_rules_base


class _FakeSnapshot:
    def __init__(self, config_loader, provider_models_service, proxy_http_clients=None):
        self.config_loader = config_loader
        self.provider_models_service = provider_models_service
        self.proxy_http_clients = proxy_http_clients or {}


class _FakeProviderModelsService:
    def __init__(self, entries_by_provider=None, fail_providers=None):
        self.entries_by_provider = entries_by_provider or {}
        self.fail_providers = fail_providers or set()
        self.calls = []

    async def get_model_entries(self, provider_name, provider_config, http_client, auth_headers=None):
        self.calls.append(provider_name)
        if provider_name in self.fail_providers:
            raise ValueError(f"simulated fetch failure: {provider_name}")
        return self.entries_by_provider.get(provider_name, {})


def _provider_details(provider_names):
    return {
        name: ProviderDetails(baseUrl=f"https://{name}.example", apikey="DIRECT-KEY")
        for name in provider_names
    }


def _run_materialize(
    *,
    fallback_rules_base,
    provider_names=("prov-a",),
    provider_entries=None,
    fail_providers=None,
    openrouter_index=None,
    coordinator=None,
):
    config_loader = _FakeConfigLoader(_provider_details(provider_names), fallback_rules_base)
    provider_models_service = _FakeProviderModelsService(provider_entries, fail_providers)
    snapshot = _FakeSnapshot(config_loader, provider_models_service)
    runtime_manager = _FakeRuntimeManager(snapshot)
    coordinator = coordinator if coordinator is not None else AsyncMock()
    openrouter_service = Mock()
    openrouter_service.get_capability_index = AsyncMock(return_value=openrouter_index or {})
    service = CapabilityAutofillService()

    run_async(
        service.materialize(
            runtime_manager=runtime_manager,
            config_update_coordinator=coordinator,
            shared_http_client=Mock(),
            openrouter_free_models_service=openrouter_service,
        )
    )
    return service, runtime_manager, coordinator, provider_models_service


def _written_rules(coordinator):
    """Decode the candidate_bytes passed to coordinator.update(), as a dict keyed
    by gateway_model_name."""
    kwargs = coordinator.update.call_args.kwargs
    payload = json.loads(kwargs["candidate_bytes"].decode("utf-8"))
    return {rule["gateway_model_name"]: rule for rule in payload}


class CapabilityAutofillResolutionTests(unittest.TestCase):
    def test_provider_field_wins_over_openrouter_when_both_present(self):
        fallback_rules_base = {
            "gw": {
                "fallback_models": [{"provider": "prov-a", "model": "model-a"}],
            }
        }
        provider_entries = {
            "prov-a": {
                "model-a": {
                    "architecture": {"input_modalities": ["text", "image"]},
                    "supported_parameters": ["tools"],
                    "context_length": 32000,
                }
            }
        }
        openrouter_index = {
            "model-a": CapabilityMetadata(
                supports_vision=False, supports_tools=False, context_window=999999
            )
        }

        _, _, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
            openrouter_index=openrouter_index,
        )

        candidate = _written_rules(coordinator)["gw"]["fallback_models"][0]
        self.assertTrue(candidate["supports_vision"])
        self.assertTrue(candidate["supports_tools"])
        self.assertEqual(candidate["context_window"], 32000)
        self.assertEqual(
            sorted(candidate["capabilities_autofilled"]),
            ["context_window", "supports_tools", "supports_vision"],
        )

    def test_openrouter_used_when_provider_has_no_entry_for_model(self):
        fallback_rules_base = {
            "gw": {"fallback_models": [{"provider": "prov-a", "model": "model-a"}]}
        }
        provider_entries = {"prov-a": {}}
        openrouter_index = {
            "model-a": CapabilityMetadata(
                supports_vision=True, supports_tools=True, context_window=16000
            )
        }

        _, _, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
            openrouter_index=openrouter_index,
        )

        candidate = _written_rules(coordinator)["gw"]["fallback_models"][0]
        self.assertTrue(candidate["supports_vision"])
        self.assertTrue(candidate["supports_tools"])
        self.assertEqual(candidate["context_window"], 16000)

    def test_openrouter_backfills_only_the_fields_the_provider_left_unresolved(self):
        fallback_rules_base = {
            "gw": {"fallback_models": [{"provider": "prov-a", "model": "model-a"}]}
        }
        # No "architecture" -> supports_vision parses to None.
        # No context_length / top_provider.context_length -> context_window parses to None.
        # supported_parameters present -> supports_tools parses to True (never None).
        provider_entries = {
            "prov-a": {"model-a": {"supported_parameters": ["tools"]}}
        }
        openrouter_index = {
            "model-a": CapabilityMetadata(
                supports_vision=True, supports_tools=False, context_window=8000
            )
        }

        _, _, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
            openrouter_index=openrouter_index,
        )

        candidate = _written_rules(coordinator)["gw"]["fallback_models"][0]
        # supports_tools resolved from the provider (True), not overridden by OpenRouter's False.
        self.assertTrue(candidate["supports_tools"])
        # supports_vision and context_window backfilled from OpenRouter since the provider
        # had no data for those two specific fields.
        self.assertTrue(candidate["supports_vision"])
        self.assertEqual(candidate["context_window"], 8000)

    def test_openrouter_used_when_provider_entry_has_no_supported_parameters_key(self):
        # The provider's /models entry has no `supported_parameters` key at all (a
        # plain OpenAI-style catalog), so it must not resolve supports_tools to a
        # materialized False -- the field stays unresolved by the provider and
        # falls through to OpenRouter as the second source.
        fallback_rules_base = {
            "gw": {"fallback_models": [{"provider": "prov-a", "model": "model-a"}]}
        }
        provider_entries = {"prov-a": {"model-a": {}}}
        openrouter_index = {
            "model-a": CapabilityMetadata(
                supports_vision=None, supports_tools=True, context_window=None
            )
        }

        service, _, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
            openrouter_index=openrouter_index,
        )

        candidate = _written_rules(coordinator)["gw"]["fallback_models"][0]
        self.assertTrue(candidate["supports_tools"])
        self.assertIn("supports_tools", candidate["capabilities_autofilled"])

        # Per-field provenance: the provider "answered" nothing for this field
        # (no supported_parameters key), so the resolution must be attributed to
        # OpenRouter, not silently to the provider.
        status = run_async(service.get_status())
        resolved_entry = status["resolutions"]["gw"][0]
        self.assertEqual(resolved_entry["fields"]["supports_tools"]["source"], "openrouter")

    def test_openrouter_key_matching_normalizes_prefix_case_and_free_suffix(self):
        fallback_rules_base = {
            "gw": {
                "fallback_models": [
                    {"provider": "prov-a", "model": "OtherVendor/Model-X:free"}
                ]
            }
        }
        provider_entries = {"prov-a": {}}
        openrouter_index = {
            "model-x": CapabilityMetadata(
                supports_vision=True, supports_tools=True, context_window=4096
            )
        }

        _, _, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
            openrouter_index=openrouter_index,
        )

        candidate = _written_rules(coordinator)["gw"]["fallback_models"][0]
        self.assertEqual(candidate["context_window"], 4096)
        self.assertTrue(candidate["supports_tools"])


class CapabilityAutofillOwnershipTests(unittest.TestCase):
    def test_manual_value_without_marker_is_never_touched(self):
        fallback_rules_base = {
            "gw": {
                "fallback_models": [
                    {"provider": "prov-a", "model": "model-a", "supports_vision": False}
                ]
            }
        }
        provider_entries = {
            # context_length is unrelated to supports_vision but gives this run a real
            # field to resolve (context_window), so materialize() has a diff to write
            # and the manual-value protection below is actually exercised end to end.
            "prov-a": {
                "model-a": {
                    "architecture": {"input_modalities": ["image"]},
                    "context_length": 32000,
                }
            }
        }

        _, _, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
        )

        # supports_vision was manually set and carries no autofill marker, so it must
        # stay untouched even though the provider now disagrees with it.
        candidate = _written_rules(coordinator)["gw"]["fallback_models"][0]
        self.assertFalse(candidate["supports_vision"])
        self.assertNotIn("supports_vision", candidate.get("capabilities_autofilled", []))

    def test_marked_field_is_re_resolved_each_run(self):
        fallback_rules_base = {
            "gw": {
                "fallback_models": [
                    {
                        "provider": "prov-a",
                        "model": "model-a",
                        "supports_vision": False,
                        "capabilities_autofilled": ["supports_vision"],
                    }
                ]
            }
        }
        provider_entries = {
            "prov-a": {"model-a": {"architecture": {"input_modalities": ["image"]}}}
        }

        _, _, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
        )

        candidate = _written_rules(coordinator)["gw"]["fallback_models"][0]
        self.assertTrue(candidate["supports_vision"])
        self.assertIn("supports_vision", candidate["capabilities_autofilled"])

    def test_empty_field_resolves_and_lands_in_marker(self):
        fallback_rules_base = {
            "gw": {"fallback_models": [{"provider": "prov-a", "model": "model-a"}]}
        }
        provider_entries = {
            "prov-a": {"model-a": {"supported_parameters": ["tools"]}}
        }

        _, _, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
        )

        candidate = _written_rules(coordinator)["gw"]["fallback_models"][0]
        self.assertTrue(candidate["supports_tools"])
        self.assertIn("supports_tools", candidate["capabilities_autofilled"])

    def test_empty_field_that_fails_to_resolve_does_not_land_in_marker(self):
        fallback_rules_base = {
            "gw": {"fallback_models": [{"provider": "prov-a", "model": "model-a"}]}
        }
        # Provider has an entry that resolves supports_tools (forcing a real write), but
        # carries no context data at all, and there is no OpenRouter match either, so
        # context_window cannot resolve this run.
        provider_entries = {
            "prov-a": {"model-a": {"supported_parameters": ["tools"]}}
        }

        _, _, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
        )

        candidate = _written_rules(coordinator)["gw"]["fallback_models"][0]
        self.assertTrue(candidate["supports_tools"])
        self.assertIn("supports_tools", candidate["capabilities_autofilled"])
        self.assertIsNone(candidate.get("context_window"))
        self.assertNotIn("context_window", candidate["capabilities_autofilled"])

    def test_anti_regression_keeps_previous_value_when_resolution_fails_this_run(self):
        fallback_rules_base = {
            "gw": {
                "fallback_models": [
                    {
                        "provider": "prov-a",
                        "model": "model-a",
                        "context_window": 50000,
                        "capabilities_autofilled": ["context_window"],
                    }
                ]
            }
        }
        # This run: the provider entry no longer carries any context signal, and there is
        # no OpenRouter match either, so re-resolution fails.
        provider_entries = {"prov-a": {"model-a": {}}}
        # Unrelated to context_window: gives this run a real field to resolve
        # (supports_vision) so materialize() has a diff to write and the
        # anti-regression behavior below is actually exercised end to end.
        openrouter_index = {
            "model-a": CapabilityMetadata(
                supports_vision=True, supports_tools=None, context_window=None
            )
        }

        _, _, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
            openrouter_index=openrouter_index,
        )

        candidate = _written_rules(coordinator)["gw"]["fallback_models"][0]
        self.assertEqual(candidate["context_window"], 50000)
        self.assertIn("context_window", candidate["capabilities_autofilled"])
        # supports_tools was never set and the provider entry is empty (no
        # `supported_parameters` key) -- it must stay unresolved (absent), never
        # materialize to a false "not supported".
        self.assertIsNone(candidate.get("supports_tools"))
        self.assertNotIn("supports_tools", candidate.get("capabilities_autofilled", []))


class CapabilityAutofillWritePathTests(unittest.TestCase):
    def test_no_changes_skips_coordinator_update(self):
        fallback_rules_base = {
            "gw": {
                "fallback_models": [
                    {
                        "provider": "prov-a",
                        "model": "model-a",
                        "supports_vision": True,
                        "supports_tools": True,
                        "context_window": 1000,
                    }
                ]
            }
        }

        _, _, coordinator, _ = _run_materialize(fallback_rules_base=fallback_rules_base)

        coordinator.update.assert_not_awaited()

    def test_materialize_writes_with_fallback_rules_file_and_comments_backup(self):
        fallback_rules_base = {
            "gw": {"fallback_models": [{"provider": "prov-a", "model": "model-a"}]}
        }
        provider_entries = {
            "prov-a": {
                "model-a": {
                    "architecture": {"input_modalities": ["image"]},
                    "supported_parameters": ["tools"],
                    "context_length": 128000,
                }
            }
        }

        service, runtime_manager, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
        )

        coordinator.update.assert_awaited_once()
        kwargs = coordinator.update.call_args.kwargs
        self.assertEqual(kwargs["config_file"], ConfigFile.FALLBACK_RULES)
        self.assertTrue(kwargs["comments_backup"])
        self.assertIsNone(kwargs["expected_revision"])
        self.assertIs(kwargs["base_snapshot"], runtime_manager.acquired_leases[0].snapshot)
        self.assertTrue(runtime_manager.acquired_leases[0].released)

    def test_context_overflow_fallback_candidate_resolves_like_fallback_models(self):
        fallback_rules_base = {
            "gw": {
                "fallback_models": [],
                "context_overflow_fallback": {"provider": "prov-a", "model": "model-a"},
            }
        }
        provider_entries = {
            "prov-a": {
                "model-a": {
                    "architecture": {"input_modalities": ["image"]},
                    "supported_parameters": ["tools"],
                    "context_length": 200000,
                }
            }
        }

        service, _, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
        )

        candidate = _written_rules(coordinator)["gw"]["context_overflow_fallback"]
        self.assertTrue(candidate["supports_vision"])
        self.assertTrue(candidate["supports_tools"])
        self.assertEqual(candidate["context_window"], 200000)

        status = run_async(service.get_status())
        entries = status["resolutions"]["gw"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["index"], "context_overflow_fallback")

    def test_provider_fetch_failure_isolates_that_providers_candidates(self):
        fallback_rules_base = {
            "gw-a": {"fallback_models": [{"provider": "prov-a", "model": "model-a"}]},
            "gw-b": {"fallback_models": [{"provider": "prov-b", "model": "model-b"}]},
        }
        provider_entries = {
            "prov-b": {"model-b": {"supported_parameters": ["tools"]}},
        }

        _, _, coordinator, provider_models_service = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_names=("prov-a", "prov-b"),
            provider_entries=provider_entries,
            fail_providers={"prov-a"},
        )

        self.assertEqual(set(provider_models_service.calls), {"prov-a", "prov-b"})
        rules = _written_rules(coordinator)
        candidate_a = rules["gw-a"]["fallback_models"][0]
        candidate_b = rules["gw-b"]["fallback_models"][0]
        self.assertIsNone(candidate_a.get("supports_tools"))
        self.assertNotIn("capabilities_autofilled", candidate_a)
        self.assertTrue(candidate_b["supports_tools"])
        self.assertIn("supports_tools", candidate_b["capabilities_autofilled"])

    def test_config_update_error_revision_conflict_is_caught_and_logged(self):
        fallback_rules_base = {
            "gw": {"fallback_models": [{"provider": "prov-a", "model": "model-a"}]}
        }
        provider_entries = {
            "prov-a": {"model-a": {"supported_parameters": ["tools"]}}
        }
        coordinator = AsyncMock()
        coordinator.update.side_effect = ConfigUpdateError(ConfigUpdateErrorCode.REVISION_CONFLICT)

        service, runtime_manager, coordinator, _ = _run_materialize(
            fallback_rules_base=fallback_rules_base,
            provider_entries=provider_entries,
            coordinator=coordinator,
        )

        # materialize() must never raise: run_async() above already proves that.
        status = run_async(service.get_status())
        # A caught revision conflict must surface as a non-empty, human-readable
        # status so the next successful run's freshness isn't confused with "no
        # problem happened" -- not silently leave lastError as None.
        self.assertTrue(status["lastError"])
        self.assertIsInstance(status["lastError"], str)
        self.assertIsNotNone(status["lastRunAt"])
        self.assertTrue(runtime_manager.acquired_leases[0].released)

    def test_successful_run_after_config_update_error_resets_last_error(self):
        fallback_rules_base = {
            "gw": {"fallback_models": [{"provider": "prov-a", "model": "model-a"}]}
        }
        provider_entries = {
            "prov-a": {"model-a": {"supported_parameters": ["tools"]}}
        }
        config_loader = _FakeConfigLoader(_provider_details(("prov-a",)), fallback_rules_base)
        provider_models_service = _FakeProviderModelsService(provider_entries)
        snapshot = _FakeSnapshot(config_loader, provider_models_service)
        runtime_manager = _FakeRuntimeManager(snapshot)
        openrouter_service = Mock()
        openrouter_service.get_capability_index = AsyncMock(return_value={})
        service = CapabilityAutofillService()

        failing_coordinator = AsyncMock()
        failing_coordinator.update.side_effect = ConfigUpdateError(
            ConfigUpdateErrorCode.REVISION_CONFLICT
        )
        run_async(
            service.materialize(
                runtime_manager=runtime_manager,
                config_update_coordinator=failing_coordinator,
                shared_http_client=Mock(),
                openrouter_free_models_service=openrouter_service,
            )
        )
        self.assertTrue(run_async(service.get_status())["lastError"])

        run_async(
            service.materialize(
                runtime_manager=runtime_manager,
                config_update_coordinator=AsyncMock(),
                shared_http_client=Mock(),
                openrouter_free_models_service=openrouter_service,
            )
        )

        status = run_async(service.get_status())
        self.assertIsNone(status["lastError"])


class SerializeFallbackRulesDriftGuardTests(unittest.TestCase):
    def test_matches_rules_editor_serialization_byte_for_byte(self):
        from llm_gateway_core.api.v1.rules_editor import _serialize_structured_rules

        rules = _build_model_fallback_configs(
            {
                "gw": {
                    "fallback_models": [
                        {
                            "provider": "prov-a",
                            "model": "model-a",
                            "supports_tools": True,
                            "context_window": 32000,
                            "capabilities_autofilled": ["supports_tools", "context_window"],
                        }
                    ],
                    "rotate_models": True,
                }
            }
        )

        self.assertEqual(
            _serialize_fallback_rules(rules),
            _serialize_structured_rules(rules).encode("utf-8"),
        )


class TriggerCapabilityAutofillHookTests(unittest.TestCase):
    def test_trigger_schedules_materialize_via_task_supervisor(self):
        services = make_app_services()
        with patch.object(services.task_supervisor, "create_task") as create_task_mock:
            create_task_mock.return_value = Mock()
            _trigger_capability_autofill(services)

        self.assertEqual(create_task_mock.call_count, 1)
        args, kwargs = create_task_mock.call_args
        self.assertEqual(kwargs["name"], "capability-autofill-editor-save")
        coroutine = args[0]
        self.assertTrue(asyncio.iscoroutine(coroutine))
        self.assertEqual(coroutine.cr_code.co_name, "materialize")
        coroutine.close()


class CapabilityAutofillStatusApiTests(unittest.TestCase):
    def _app_with_role(self, role: str) -> FastAPI:
        app = FastAPI()
        services = make_app_services()
        runtime_snapshot = make_runtime_snapshot(http_client=services.http_client)
        app.state.services = services

        @app.middleware("http")
        async def set_role(request, call_next):
            request.state.api_key_role = role
            request.state.runtime_snapshot = runtime_snapshot
            return await call_next(request)

        app.include_router(editor_router, prefix="/v1")
        return app

    def test_status_endpoint_rejects_non_master_role(self):
        app = self._app_with_role(ROLE_USER)

        response = TestClient(app).get("/v1/capability-autofill")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "This endpoint is reserved for the master API key"},
        )

    def test_status_endpoint_returns_service_payload_for_master(self):
        app = self._app_with_role(ROLE_MASTER)

        response = TestClient(app).get("/v1/capability-autofill")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"lastRunAt": None, "lastError": None, "resolutions": {}},
        )


if __name__ == "__main__":
    unittest.main()
