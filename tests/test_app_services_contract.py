from __future__ import annotations

import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields
from typing import Any, get_type_hints
from unittest.mock import Mock

import httpx

from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.db.fallback_events_db import FallbackEventsDB
from llm_gateway_core.db.model_rotation_db import ModelRotationDB
from llm_gateway_core.db.rejections_db import RejectionsDB
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.db.write_batcher import WriteBatcher
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.accounting import OperationCostCalculator
from llm_gateway_core.services.accounting_service import AccountingService
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from llm_gateway_core.services.config_updates import ConfigUpdateCoordinator
from llm_gateway_core.services.deep_research_process import DeepResearchProcessRunner
from llm_gateway_core.services.fallback_model_evals import FallbackModelEvalService
from llm_gateway_core.services.fusion_ensemble import FusionEnsembleService
from llm_gateway_core.services.health import HealthService
from llm_gateway_core.services.image_retention import ImageRetentionService
from llm_gateway_core.services.image_storage import GeneratedImageStorage
from llm_gateway_core.services.ip_blocklist import IpBlockGuard
from llm_gateway_core.services.openrouter_free_models import OpenRouterFreeModelsService
from llm_gateway_core.services.provider_models import ProviderModelsService
from llm_gateway_core.services.rate_limiter import RateLimiter
from llm_gateway_core.services.request_handler import OperationDispatcher
from llm_gateway_core.services.router_model import RouterModelService
from llm_gateway_core.services.runtime_config import (
    AppServices,
    RuntimeGenerationManager,
    RuntimeSnapshot,
)
from llm_gateway_core.services.stream_observation import StreamObservationCapacity
from llm_gateway_core.services.task_supervisor import TaskSupervisor
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from llm_gateway_core.services.upstream_subscription_quota import (
    UpstreamSubscriptionQuotaService,
)
from llm_gateway_core.services.upload_admission import UploadAdmission
from llm_gateway_core.utils.usage_tracking import ModelCostRates


APP_SERVICE_FIELDS = (
    "runtime_manager",
    "config_update_coordinator",
    "http_client",
    "tokens_usage_db",
    "fallback_events_db",
    "rejections_db",
    "api_keys_db",
    "model_rotation_db",
    "write_batcher",
    "accounting_service",
    "health_service",
    "image_storage",
    "image_retention_service",
    "usd_budget_ledger",
    "active_requests_registry",
    "rate_limiter",
    "ip_block_guard",
    "upstream_routing_state",
    "upstream_subscription_quota_service",
    "openrouter_free_models_service",
    "fallback_model_eval_service",
    "deep_research_process_runner",
    "upload_admission",
    "upload_admission_timeout_seconds",
    "stream_observation_capacity",
    "json_response_capacity",
    "stream_event_max_bytes",
    "json_response_max_bytes",
    "task_supervisor",
)


def _services() -> AppServices:
    dependencies = {
        field_name: Mock(name=field_name)
        for field_name in APP_SERVICE_FIELDS
    }
    dependencies["ip_block_guard"] = None
    dependencies["stream_observation_capacity"] = StreamObservationCapacity()
    dependencies["json_response_capacity"] = StreamObservationCapacity()
    dependencies["upload_admission"] = UploadAdmission(max_bytes=1024)
    dependencies["upload_admission_timeout_seconds"] = 5.0
    dependencies["stream_event_max_bytes"] = 1_048_576
    dependencies["json_response_max_bytes"] = 8_388_608
    return AppServices(**dependencies)


class AppServicesContractTests(unittest.TestCase):
    def test_container_has_exact_process_lifetime_boundary(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(AppServices)),
            APP_SERVICE_FIELDS,
        )
        forbidden_generation_fields = {
            "config_loader",
            "operation_dispatcher",
            "fusion_service",
            "router_model_service",
            "provider_models_service",
            "proxy_http_clients",
            "cost_rate_registry",
            "operation_cost_calculator_registry",
        }
        self.assertTrue(forbidden_generation_fields.isdisjoint(APP_SERVICE_FIELDS))

    def test_container_is_frozen_and_uses_slots(self) -> None:
        services = _services()

        self.assertFalse(hasattr(services, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            services.http_client = Mock()  # type: ignore[misc]

    def test_app_services_type_hints_resolve_to_exact_concrete_types(self) -> None:
        services = _services()

        self.assertIsNone(services.ip_block_guard)
        resolved = get_type_hints(AppServices)
        self.assertEqual(
            resolved,
            {
                "runtime_manager": RuntimeGenerationManager,
                "config_update_coordinator": ConfigUpdateCoordinator,
                "http_client": httpx.AsyncClient,
                "tokens_usage_db": TokensUsageDB,
                "fallback_events_db": FallbackEventsDB,
                "rejections_db": RejectionsDB,
                "api_keys_db": ApiKeysDB,
                "model_rotation_db": ModelRotationDB,
                "write_batcher": WriteBatcher,
                "accounting_service": AccountingService,
                "health_service": HealthService,
                "image_storage": GeneratedImageStorage,
                "image_retention_service": ImageRetentionService,
                "usd_budget_ledger": UsdBudgetLedger,
                "active_requests_registry": ActiveRequestsRegistry,
                "rate_limiter": RateLimiter,
                "ip_block_guard": IpBlockGuard | None,
                "upstream_routing_state": UpstreamRoutingState,
                "upstream_subscription_quota_service": UpstreamSubscriptionQuotaService,
                "openrouter_free_models_service": OpenRouterFreeModelsService,
                "fallback_model_eval_service": FallbackModelEvalService,
                "deep_research_process_runner": DeepResearchProcessRunner,
                "upload_admission": UploadAdmission,
                "upload_admission_timeout_seconds": float,
                "stream_observation_capacity": StreamObservationCapacity,
                "json_response_capacity": StreamObservationCapacity,
                "stream_event_max_bytes": int,
                "json_response_max_bytes": int,
                "task_supervisor": TaskSupervisor,
            },
        )
        self.assertNotIn(Any, resolved.values())

    def test_upload_admission_timeout_must_be_positive_and_finite(self) -> None:
        dependencies = {
            field_name: Mock(name=field_name)
            for field_name in APP_SERVICE_FIELDS
        }
        dependencies["ip_block_guard"] = None

        for invalid in (True, 0, -1, float("inf"), float("nan"), "5"):
            with self.subTest(invalid=invalid):
                dependencies["upload_admission_timeout_seconds"] = invalid
                with self.assertRaises(ValueError):
                    AppServices(**dependencies)

    def test_stream_event_limit_must_be_positive_integer_within_capacity(self) -> None:
        dependencies = {
            field_name: Mock(name=field_name)
            for field_name in APP_SERVICE_FIELDS
        }
        dependencies["ip_block_guard"] = None
        dependencies["stream_observation_capacity"] = StreamObservationCapacity(
            max_bytes=16
        )

        for invalid in (True, 0, 17, 1.5, "16"):
            with self.subTest(invalid=invalid):
                dependencies["stream_event_max_bytes"] = invalid
                with self.assertRaises(ValueError):
                    AppServices(**dependencies)

    def test_json_response_limit_must_be_positive_integer_within_capacity(self) -> None:
        dependencies = {
            field_name: Mock(name=field_name)
            for field_name in APP_SERVICE_FIELDS
        }
        dependencies["ip_block_guard"] = None
        dependencies["stream_observation_capacity"] = StreamObservationCapacity(
            max_bytes=16
        )
        dependencies["stream_event_max_bytes"] = 16
        dependencies["json_response_capacity"] = StreamObservationCapacity(
            max_bytes=32
        )

        for invalid in (True, 0, 33, 1.5, "32"):
            with self.subTest(invalid=invalid):
                dependencies["json_response_max_bytes"] = invalid
                with self.assertRaises(ValueError):
                    AppServices(**dependencies)

    def test_runtime_snapshot_type_hints_are_runtime_resolvable(self) -> None:
        resolved = get_type_hints(RuntimeSnapshot)

        self.assertEqual(
            resolved,
            {
                "generation": int,
                "config_loader": ConfigLoader,
                "operation_dispatcher": OperationDispatcher,
                "fusion_service": FusionEnsembleService,
                "router_model_service": RouterModelService,
                "provider_models_service": ProviderModelsService,
                "proxy_http_clients": Mapping[str, httpx.AsyncClient],
                "cost_rate_registry": Mapping[tuple[str, str], ModelCostRates],
                "operation_cost_calculator_registry": Mapping[
                    tuple[str, str], OperationCostCalculator
                ],
            },
        )
        self.assertNotIn(Any, resolved.values())


if __name__ == "__main__":
    unittest.main()
