"""Build unpublished runtime generations with manager-owned resources."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import httpx

from ..config.loader import ConfigLoader
from ..utils.log_redaction import safe_exception_type_name
from ..utils.usage_tracking import build_model_cost_rate_registry
from .accounting import build_operation_cost_calculator_registry
from .fusion_ensemble import FusionEnsembleService
from .http_client_factory import populate_proxy_http_clients
from .provider_models import ProviderModelsService
from .request_handler import OperationDispatcher
from .router_model import RouterModelService
from .runtime_config import (
    RuntimeGenerationManager,
    RuntimeSnapshot,
    RuntimeUnpublishedSlot,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeCandidate:
    """An unpublished snapshot whose proxy clients remain manager-owned."""

    snapshot: RuntimeSnapshot
    _manager: RuntimeGenerationManager = field(repr=False)
    _slot: RuntimeUnpublishedSlot = field(repr=False)

    def __repr__(self) -> str:
        return f"RuntimeCandidate(generation={self.snapshot.generation})"

    async def close_unpublished(self) -> bool:
        """Close resources that have not been transferred to a generation."""
        return await self._slot.close_unpublished()

    def install_initial(self) -> RuntimeSnapshot:
        """Atomically install generation one and transfer slot ownership."""
        self._manager.install_initial_candidate(self.snapshot, slot=self._slot)
        return self.snapshot

    def publish(self, *, expected_generation: int) -> RuntimeSnapshot:
        """Compare-and-publish the candidate and transfer slot ownership."""
        return self._manager.publish_candidate(
            self.snapshot,
            expected_generation=expected_generation,
            slot=self._slot,
        )


async def build_runtime_candidate(
    *,
    manager: RuntimeGenerationManager,
    generation: int,
    config_loader: ConfigLoader,
    shared_http_client: httpx.AsyncClient,
) -> RuntimeCandidate:
    """Build one complete runtime candidate without publishing it."""
    slot = manager.open_unpublished_slot(generation)
    try:
        proxy_http_clients = await populate_proxy_http_clients(
            config_loader.providers_config,
            register_client=slot.register_http_client,
        )
        cost_rate_registry = MappingProxyType(
            dict(build_model_cost_rate_registry(config_loader.providers_config))
        )
        operation_cost_calculator_registry = MappingProxyType(
            dict(
                build_operation_cost_calculator_registry(
                    config_loader.operation_rules
                )
            )
        )
        snapshot = RuntimeSnapshot(
            generation=generation,
            config_loader=config_loader,
            operation_dispatcher=OperationDispatcher(
                config_loader.providers_config,
                config_loader.operation_rules,
                shared_http_client,
                model_rules=(
                    config_loader.model_rules
                    if isinstance(getattr(config_loader, "model_rules", None), Mapping)
                    else {}
                ),
            ),
            fusion_service=FusionEnsembleService(
                config_loader,
                cost_rate_registry=cost_rate_registry,
            ),
            router_model_service=RouterModelService(
                config_loader,
                cost_rate_registry=cost_rate_registry,
            ),
            provider_models_service=ProviderModelsService(),
            proxy_http_clients=proxy_http_clients,
            cost_rate_registry=cost_rate_registry,
            operation_cost_calculator_registry=operation_cost_calculator_registry,
        )
        candidate = RuntimeCandidate(snapshot=snapshot, _manager=manager, _slot=slot)
    except BaseException:
        try:
            await slot.close_unpublished()
        except BaseException as cleanup_exc:
            logger.error(
                "Runtime candidate cleanup failed: resource=%s exception_type=%s.",
                "unpublished_slot",
                safe_exception_type_name(cleanup_exc),
            )
        raise
    return candidate
