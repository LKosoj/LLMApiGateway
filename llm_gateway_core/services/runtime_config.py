"""Immutable runtime generations and lease-based resource retirement."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from types import MappingProxyType

import httpx

from ..config.loader import ConfigLoader
from ..db.api_keys_db import ApiKeysDB
from ..db.fallback_events_db import FallbackEventsDB
from ..db.model_rotation_db import ModelRotationDB
from ..db.rejections_db import RejectionsDB
from ..db.tokens_usage_db import TokensUsageDB
from ..db.write_batcher import WriteBatcher
from ..utils.usage_tracking import ModelCostRates
from ..utils.log_redaction import sanitize_exception_type_name
from .access_control import UsdBudgetLedger
from .accounting import OperationCostCalculator
from .accounting_service import AccountingService
from .active_requests import ActiveRequestsRegistry
from .capability_autofill import CapabilityAutofillService
from .config_updates import ConfigUpdateCoordinator
from .deep_research_process import DeepResearchProcessRunner
from .fallback_model_evals import FallbackModelEvalService
from .free_llm_catalog import FreeLlmCatalogService
from .fusion_ensemble import FusionEnsembleService
from .health import HealthService
from .http_client_factory import close_http_clients
from .image_retention import ImageRetentionService
from .image_storage import GeneratedImageStorage
from .ip_blocklist import IpBlockGuard
from .openrouter_free_models import OpenRouterFreeModelsService
from .provider_models import ProviderModelsService
from .rate_limiter import RateLimiter
from .request_handler import OperationDispatcher
from .router_model import RouterModelService
from .stream_observation import StreamObservationCapacity
from .task_supervisor import TaskSupervisor
from .upstream_routing_state import UpstreamRoutingState
from .upstream_subscription_quota import UpstreamSubscriptionQuotaService
from .upload_admission import UploadAdmission


RUNTIME_CLEANUP_MAX_ATTEMPTS = 3
RUNTIME_GENERATION_HISTORY_LIMIT = 64
RUNTIME_CLEANUP_DIAGNOSTIC_LIMIT = 128
# A retiring generation only keeps its own proxy HTTP clients alive until the
# last request holding its lease finishes, so a few may overlap. The cap exists
# to bound that overlap, not to serialize configuration writes behind long
# requests: publishing waits for nobody below it.
RUNTIME_MAX_RETIRING_GENERATIONS = 4

_CONFIG_GRAPH_ATTRIBUTES = (
    "providers_config",
    "fallback_rules",
    "_fallback_rules_base",
    "operation_rules",
    "fusion_rules",
    "model_rules",
    "router_rules",
)
_IMMUTABLE_SCALAR_TYPES = (str, bytes, int, float, complex, bool, type(None), Enum)


class RuntimeManagerStateError(RuntimeError):
    """The requested operation is invalid for the manager state."""


class RuntimeGenerationConflictError(RuntimeError):
    """A candidate was built from a stale or aliased generation."""


class RuntimeManagerStatus(str, Enum):
    NEW = "new"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    STOP_FAILED = "stop_failed"


class RuntimeGenerationStatus(str, Enum):
    ACTIVE = "active"
    RETIRED = "retired"
    CLOSING = "closing"
    CLOSED = "closed"
    CLOSE_FAILED = "close_failed"


@dataclass(frozen=True, slots=True)
class RuntimeCleanupFailure:
    """Credential-safe diagnostic emitted while retiring one generation."""

    generation: int
    resource: str
    exception_type: str


@dataclass(frozen=True, slots=True)
class AppServices:
    """Process-lifetime dependencies shared by every runtime generation."""

    runtime_manager: RuntimeGenerationManager
    config_update_coordinator: ConfigUpdateCoordinator
    http_client: httpx.AsyncClient
    tokens_usage_db: TokensUsageDB
    fallback_events_db: FallbackEventsDB
    rejections_db: RejectionsDB
    api_keys_db: ApiKeysDB
    model_rotation_db: ModelRotationDB
    write_batcher: WriteBatcher
    accounting_service: AccountingService
    health_service: HealthService
    image_storage: GeneratedImageStorage
    image_retention_service: ImageRetentionService
    usd_budget_ledger: UsdBudgetLedger
    active_requests_registry: ActiveRequestsRegistry
    rate_limiter: RateLimiter
    ip_block_guard: IpBlockGuard | None
    upstream_routing_state: UpstreamRoutingState
    upstream_subscription_quota_service: UpstreamSubscriptionQuotaService
    openrouter_free_models_service: OpenRouterFreeModelsService
    fallback_model_eval_service: FallbackModelEvalService
    capability_autofill_service: CapabilityAutofillService
    free_llm_catalog_service: FreeLlmCatalogService
    deep_research_process_runner: DeepResearchProcessRunner
    upload_admission: UploadAdmission
    upload_admission_timeout_seconds: float
    stream_observation_capacity: StreamObservationCapacity
    json_response_capacity: StreamObservationCapacity
    stream_event_max_bytes: int
    json_response_max_bytes: int
    task_supervisor: TaskSupervisor

    def __post_init__(self) -> None:
        if (
            isinstance(self.upload_admission_timeout_seconds, bool)
            or not isinstance(self.upload_admission_timeout_seconds, (int, float))
            or not math.isfinite(float(self.upload_admission_timeout_seconds))
            or self.upload_admission_timeout_seconds <= 0
        ):
            raise ValueError(
                "upload_admission_timeout_seconds must be a positive finite number"
            )
        if (
            isinstance(self.stream_event_max_bytes, bool)
            or not isinstance(self.stream_event_max_bytes, int)
            or self.stream_event_max_bytes <= 0
        ):
            raise ValueError("stream_event_max_bytes must be a positive integer")
        if (
            self.stream_event_max_bytes
            > self.stream_observation_capacity.snapshot.max_bytes
        ):
            raise ValueError(
                "stream_event_max_bytes must not exceed stream observation byte capacity"
            )
        if (
            isinstance(self.json_response_max_bytes, bool)
            or not isinstance(self.json_response_max_bytes, int)
            or self.json_response_max_bytes <= 0
        ):
            raise ValueError("json_response_max_bytes must be a positive integer")
        if self.json_response_max_bytes > self.json_response_capacity.snapshot.max_bytes:
            raise ValueError(
                "json_response_max_bytes must not exceed JSON response byte capacity"
            )


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """One self-consistent generation of replaceable runtime dependencies."""

    generation: int
    config_loader: ConfigLoader
    operation_dispatcher: OperationDispatcher
    fusion_service: FusionEnsembleService
    router_model_service: RouterModelService
    provider_models_service: ProviderModelsService
    proxy_http_clients: Mapping[str, httpx.AsyncClient]
    cost_rate_registry: Mapping[tuple[str, str], ModelCostRates]
    operation_cost_calculator_registry: Mapping[
        tuple[str, str], OperationCostCalculator
    ] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.generation <= 0:
            raise ValueError("Runtime snapshot generation must be a positive integer.")
        object.__setattr__(
            self,
            "proxy_http_clients",
            MappingProxyType(dict(self.proxy_http_clients)),
        )
        object.__setattr__(
            self,
            "cost_rate_registry",
            MappingProxyType(dict(self.cost_rate_registry)),
        )
        object.__setattr__(
            self,
            "operation_cost_calculator_registry",
            MappingProxyType(dict(self.operation_cost_calculator_registry)),
        )


@dataclass(slots=True)
class _GenerationRecord:
    generation: int
    snapshot: RuntimeSnapshot | None
    status: RuntimeGenerationStatus = RuntimeGenerationStatus.ACTIVE
    active_leases: int = 0
    cleanup_task: asyncio.Task[None] | None = None
    cleanup_clients: dict[str, httpx.AsyncClient] = field(default_factory=dict)
    zero_leases: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.zero_leases.set()


@dataclass(slots=True)
class _FailedGeneration:
    generation: int
    clients: dict[str, httpx.AsyncClient]


@dataclass(slots=True)
class _UnpublishedSlotRecord:
    slot_id: int
    token: object
    generation: int
    clients: dict[str, httpx.AsyncClient] = field(default_factory=dict)
    accepting: bool = True
    close_failed: bool = False
    close_task: asyncio.Task[bool] | None = None


class RuntimeLease:
    """Idempotent ownership token for one runtime snapshot."""

    __slots__ = ("_manager", "_released", "generation", "snapshot")

    def __init__(
        self,
        manager: RuntimeGenerationManager,
        snapshot: RuntimeSnapshot,
    ) -> None:
        self._manager = manager
        self._released = False
        self.generation = snapshot.generation
        self.snapshot = snapshot

    @property
    def released(self) -> bool:
        return self._released

    def retain(self) -> RuntimeLease:
        """Create another lease while this lease keeps a running generation alive."""
        if self._released:
            raise RuntimeManagerStateError("A released runtime lease cannot be retained.")
        return self._manager._retain_from_lease(self.generation)

    def release(self) -> None:
        if self._released:
            return
        self._manager._release(self.generation)
        self._released = True


class RuntimeUnpublishedSlot:
    """Manager-owned handle for HTTP clients not yet published in a snapshot."""

    __slots__ = ("_manager", "_slot_id", "_token", "generation")

    def __init__(
        self,
        manager: RuntimeGenerationManager,
        slot_id: int,
        token: object,
        generation: int,
    ) -> None:
        self._manager = manager
        self._slot_id = slot_id
        self._token = token
        self.generation = generation

    @property
    def slot_id(self) -> int:
        return self._slot_id

    def register_http_client(self, name: str, client: httpx.AsyncClient) -> None:
        self._manager._register_unpublished_http_client(self, name, client)

    async def close_unpublished(self) -> bool:
        return await self._manager._close_unpublished_slot(self)


class RuntimeGenerationManager:
    """Own and atomically replace runtime snapshots on one event loop."""

    def __init__(self) -> None:
        self._status = RuntimeManagerStatus.NEW
        self._loop: asyncio.AbstractEventLoop | None = None
        self._current_generation: int | None = None
        self._generations: dict[int, _GenerationRecord] = {}
        self._generation_history: OrderedDict[int, RuntimeGenerationStatus] = OrderedDict()
        self._failed_generations: dict[int, _FailedGeneration] = {}
        self._unpublished_slots: dict[int, _UnpublishedSlotRecord] = {}
        self._next_unpublished_slot_id = 1
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._cleanup_failures: deque[RuntimeCleanupFailure] = deque(
            maxlen=RUNTIME_CLEANUP_DIAGNOSTIC_LIMIT
        )
        self._recovery_lock = asyncio.Lock()
        self._shutdown_task: asyncio.Task[None] | None = None

    @property
    def status(self) -> RuntimeManagerStatus:
        return self._status

    @property
    def current_generation(self) -> int | None:
        return self._current_generation

    @property
    def cleanup_failures(self) -> tuple[RuntimeCleanupFailure, ...]:
        return tuple(self._cleanup_failures)

    @property
    def cleanup_task_count(self) -> int:
        return len(self._cleanup_tasks)

    @property
    def failed_generations(self) -> tuple[int, ...]:
        return tuple(self._failed_generations)

    @property
    def pending_unpublished_generations(self) -> tuple[int, ...]:
        return tuple(record.generation for record in self._unpublished_slots.values())

    @property
    def failed_unpublished_generations(self) -> tuple[int, ...]:
        """Generations whose manager-owned unpublished cleanup is unresolved."""
        return tuple(
            record.generation
            for record in self._unpublished_slots.values()
            if record.close_failed
        )

    @property
    def unresolved_cleanup_generations(self) -> tuple[int, ...]:
        """Both halves of unresolved runtime retirement cleanup.

        ``retry_failed_cleanup()`` drains ``failed_unpublished_generations``
        (unpublished candidate slots that never finished closing) and
        ``failed_generations`` (already-retired generations that never
        finished closing) together. Any caller deciding whether the manager
        can accept more work, whether health should report red, or whether
        recovery needs to run must treat either half as equally blocking --
        checking only one half lets the other silently wedge admission.
        """
        return self.failed_unpublished_generations + self.failed_generations

    @property
    def active_leases(self) -> Mapping[int, int]:
        return MappingProxyType(
            {
                generation: record.active_leases
                for generation, record in self._generations.items()
            }
        )

    @property
    def generation_statuses(self) -> Mapping[int, RuntimeGenerationStatus]:
        return MappingProxyType(
            {
                **self._generation_history,
                **{
                    generation: RuntimeGenerationStatus.CLOSE_FAILED
                    for generation in self._failed_generations
                },
                **{
                    generation: record.status
                    for generation, record in self._generations.items()
                },
            }
        )

    @property
    def retired_generations(self) -> tuple[int, ...]:
        return tuple(
            generation
            for generation, status in self.generation_statuses.items()
            if status is not RuntimeGenerationStatus.ACTIVE
        )

    def _install_initial_for_testing(self, snapshot: RuntimeSnapshot) -> None:
        self._bind_loop()
        if self._status is not RuntimeManagerStatus.NEW:
            raise RuntimeManagerStateError("Initial runtime generation is already installed.")
        if snapshot.generation != 1:
            raise RuntimeGenerationConflictError("Initial runtime generation must be 1.")
        self._generations[1] = _GenerationRecord(generation=1, snapshot=snapshot)
        self._current_generation = 1
        self._status = RuntimeManagerStatus.RUNNING

    def open_unpublished_slot(self, generation: int) -> RuntimeUnpublishedSlot:
        self._bind_loop()
        if self._status not in {
            RuntimeManagerStatus.NEW,
            RuntimeManagerStatus.RUNNING,
        }:
            raise RuntimeManagerStateError(
                f"Runtime generation manager is {self._status.value}."
            )
        if generation <= 0:
            raise ValueError("Unpublished runtime generation must be a positive integer.")
        if any(record.close_failed for record in self._unpublished_slots.values()):
            raise RuntimeManagerStateError(
                "Unresolved unpublished runtime cleanup must be recovered before "
                "opening another slot."
            )

        slot_id = self._next_unpublished_slot_id
        self._next_unpublished_slot_id += 1
        token = object()
        slot = RuntimeUnpublishedSlot(self, slot_id, token, generation)
        self._unpublished_slots[slot_id] = _UnpublishedSlotRecord(
            slot_id=slot_id,
            token=token,
            generation=generation,
        )
        return slot

    def install_initial_candidate(
        self,
        snapshot: RuntimeSnapshot,
        *,
        slot: RuntimeUnpublishedSlot,
    ) -> None:
        record = self._validate_unpublished_candidate(snapshot, slot=slot)
        if self._status is not RuntimeManagerStatus.NEW:
            raise RuntimeManagerStateError("Initial runtime generation is already installed.")
        if snapshot.generation != 1:
            raise RuntimeGenerationConflictError("Initial runtime generation must be 1.")
        if any(
            other.close_failed
            for other in self._unpublished_slots.values()
            if other is not record
        ):
            raise RuntimeManagerStateError(
                "Unresolved unpublished runtime cleanup must be recovered before "
                "installing the initial candidate."
            )

        generation_record = _GenerationRecord(generation=1, snapshot=snapshot)
        self._generations[1] = generation_record
        self._current_generation = 1
        self._status = RuntimeManagerStatus.RUNNING
        del self._unpublished_slots[record.slot_id]

    def acquire_current(self) -> RuntimeLease:
        self._require_running()
        assert self._current_generation is not None
        return self._acquire(self._current_generation)

    def retain(self, generation: int) -> RuntimeLease:
        """Retain an active generation when no originating lease is available."""
        self._require_running()
        record = self._generations.get(generation)
        if record is None or record.status is not RuntimeGenerationStatus.ACTIVE:
            raise RuntimeManagerStateError(
                f"Runtime generation {generation} is not the active generation."
            )
        return self._acquire(generation)

    def validate_publish(
        self,
        candidate: RuntimeSnapshot,
        *,
        expected_generation: int,
    ) -> None:
        """Validate a compare-and-publish attempt without changing generations."""
        self._validate_publish(candidate, expected_generation=expected_generation)

    def _publish_for_testing(
        self,
        candidate: RuntimeSnapshot,
        *,
        expected_generation: int,
    ) -> RuntimeSnapshot:
        self._validate_publish(candidate, expected_generation=expected_generation)

        current_generation = self._current_generation
        assert current_generation is not None
        current = self._generations[current_generation]
        self._generations[candidate.generation] = _GenerationRecord(
            generation=candidate.generation,
            snapshot=candidate,
        )
        self._current_generation = candidate.generation
        current.status = RuntimeGenerationStatus.RETIRED
        if current.active_leases == 0:
            self._schedule_cleanup(current)
        return candidate

    def publish_candidate(
        self,
        candidate: RuntimeSnapshot,
        *,
        expected_generation: int,
        slot: RuntimeUnpublishedSlot,
    ) -> RuntimeSnapshot:
        self._validate_publish(candidate, expected_generation=expected_generation)
        slot_record = self._validate_unpublished_candidate(candidate, slot=slot)

        current_generation = self._current_generation
        assert current_generation is not None
        current = self._generations[current_generation]
        self._generations[candidate.generation] = _GenerationRecord(
            generation=candidate.generation,
            snapshot=candidate,
        )
        self._current_generation = candidate.generation
        current.status = RuntimeGenerationStatus.RETIRED
        del self._unpublished_slots[slot_record.slot_id]
        if current.active_leases == 0:
            self._schedule_cleanup(current)
        return candidate

    def _validate_publish(
        self,
        candidate: RuntimeSnapshot,
        *,
        expected_generation: int,
    ) -> None:
        self._require_running()
        current_generation = self._current_generation
        if current_generation != expected_generation:
            raise RuntimeGenerationConflictError(
                "Runtime generation changed while the candidate was being built."
            )
        if candidate.generation != expected_generation + 1:
            raise RuntimeGenerationConflictError(
                "Runtime candidate generation must immediately follow the active generation."
            )
        if candidate.generation in self._generations:
            raise RuntimeGenerationConflictError(
                f"Runtime generation {candidate.generation} is already managed."
            )
        if self._failed_generations:
            raise RuntimeManagerStateError(
                "Unresolved runtime cleanup must be recovered before publishing."
            )
        if any(record.close_failed for record in self._unpublished_slots.values()):
            raise RuntimeManagerStateError(
                "Unresolved unpublished runtime cleanup must be recovered before publishing."
            )
        retiring = sum(
            1
            for generation, record in self._generations.items()
            if generation != current_generation
            and record.status
            in {RuntimeGenerationStatus.RETIRED, RuntimeGenerationStatus.CLOSING}
        )
        if retiring >= RUNTIME_MAX_RETIRING_GENERATIONS:
            raise RuntimeManagerStateError(
                "Too many runtime generations are still retiring."
            )

        assert current_generation is not None
        self._reject_shared_generation_resources(candidate)

    async def retry_failed_cleanup(self) -> bool:
        """Retry unresolved client cleanup and report whether every client closed."""
        self._bind_loop()
        if self._status not in {
            RuntimeManagerStatus.RUNNING,
            RuntimeManagerStatus.STOP_FAILED,
        }:
            raise RuntimeManagerStateError(
                f"Runtime generation manager is {self._status.value}."
            )
        unpublished_recovered = await self._retry_failed_unpublished_slots()
        recovered = await self._retry_unresolved_clients()
        if (
            unpublished_recovered
            and recovered
            and self._status is RuntimeManagerStatus.STOP_FAILED
            and not self._generations
            and not self._cleanup_tasks
            and not self._unpublished_slots
        ):
            self._status = RuntimeManagerStatus.STOPPED
        return unpublished_recovered and recovered

    async def shutdown(self) -> None:
        self._bind_loop()
        if self._status is RuntimeManagerStatus.STOPPED:
            return

        if self._shutdown_task is None or self._shutdown_task.done():
            if self._status is RuntimeManagerStatus.RUNNING:
                assert self._current_generation is not None
                current = self._generations[self._current_generation]
                current.status = RuntimeGenerationStatus.RETIRED
            self._status = RuntimeManagerStatus.STOPPING
            self._shutdown_task = asyncio.create_task(
                self._shutdown_impl(),
                name="runtime-generation-shutdown",
            )
        await asyncio.shield(self._shutdown_task)

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeManagerStateError(
                "Runtime generation operations require a running event loop."
            ) from exc
        if self._loop is None:
            self._loop = running_loop
        elif self._loop is not running_loop:
            raise RuntimeManagerStateError(
                "RuntimeGenerationManager cannot be used from another event loop."
            )
        return running_loop

    def _require_running(self) -> None:
        self._bind_loop()
        if self._status is not RuntimeManagerStatus.RUNNING:
            raise RuntimeManagerStateError(
                f"Runtime generation manager is {self._status.value}."
            )

    def _acquire(self, generation: int) -> RuntimeLease:
        record = self._generations[generation]
        snapshot = record.snapshot
        if snapshot is None:
            raise RuntimeManagerStateError(
                f"Runtime generation {generation} no longer owns a snapshot."
            )
        if record.active_leases == 0:
            record.zero_leases.clear()
        record.active_leases += 1
        return RuntimeLease(self, snapshot)

    def _retain_from_lease(self, generation: int) -> RuntimeLease:
        self._require_running()
        record = self._generations.get(generation)
        if (
            record is None
            or record.active_leases <= 0
            or record.status
            not in {RuntimeGenerationStatus.ACTIVE, RuntimeGenerationStatus.RETIRED}
        ):
            raise RuntimeManagerStateError(
                f"Runtime generation {generation} cannot be retained from this lease."
            )
        return self._acquire(generation)

    def _release(self, generation: int) -> None:
        self._bind_loop()
        record = self._generations.get(generation)
        if record is None or record.active_leases <= 0:
            raise RuntimeManagerStateError(
                f"Runtime generation {generation} has no active lease to release."
            )
        record.active_leases -= 1
        if record.active_leases != 0:
            return
        record.zero_leases.set()
        if record.status is RuntimeGenerationStatus.RETIRED:
            if self._status is RuntimeManagerStatus.STOPPING:
                return
            self._schedule_cleanup(record)

    def _get_unpublished_slot(
        self,
        slot: RuntimeUnpublishedSlot,
    ) -> _UnpublishedSlotRecord | None:
        if slot._manager is not self:  # noqa: SLF001
            raise RuntimeManagerStateError(
                "Unpublished runtime slot belongs to another manager."
            )
        record = self._unpublished_slots.get(slot.slot_id)
        if record is not None and record.token is not slot._token:  # noqa: SLF001
            raise RuntimeManagerStateError("Unpublished runtime slot identity is invalid.")
        return record

    def _validate_unpublished_candidate(
        self,
        candidate: RuntimeSnapshot,
        *,
        slot: RuntimeUnpublishedSlot,
    ) -> _UnpublishedSlotRecord:
        self._bind_loop()
        record = self._get_unpublished_slot(slot)
        if record is None or not record.accepting:
            raise RuntimeManagerStateError("Unpublished runtime slot is no longer active.")
        if slot.generation != record.generation:
            raise RuntimeManagerStateError("Unpublished runtime slot identity is invalid.")
        if record.generation != candidate.generation:
            raise RuntimeGenerationConflictError(
                "Unpublished slot generation does not match the runtime candidate."
            )
        if record.clients.keys() != candidate.proxy_http_clients.keys() or any(
            candidate.proxy_http_clients[name] is not client
            for name, client in record.clients.items()
        ):
            raise RuntimeGenerationConflictError(
                "Runtime candidate proxy HTTP clients do not match the unpublished slot."
            )
        return record

    def _register_unpublished_http_client(
        self,
        slot: RuntimeUnpublishedSlot,
        name: str,
        client: httpx.AsyncClient,
    ) -> None:
        self._bind_loop()
        record = self._get_unpublished_slot(slot)
        if record is None or not record.accepting:
            raise RuntimeManagerStateError("Unpublished runtime slot is no longer active.")
        if self._status not in {
            RuntimeManagerStatus.NEW,
            RuntimeManagerStatus.RUNNING,
        }:
            raise RuntimeManagerStateError(
                f"Runtime generation manager is {self._status.value}."
            )
        if name in record.clients:
            raise RuntimeGenerationConflictError(
                f"HTTP client '{name}' is already registered in this unpublished slot."
            )
        if self._owns_http_client(client):
            raise RuntimeGenerationConflictError(
                "HTTP client is already owned by the runtime generation manager."
            )
        record.clients[name] = client

    def _owns_http_client(self, client: httpx.AsyncClient) -> bool:
        unpublished_clients = (
            owned_client
            for record in self._unpublished_slots.values()
            for owned_client in record.clients.values()
        )
        generation_clients = (
            owned_client
            for record in self._generations.values()
            for owned_client in (
                record.snapshot.proxy_http_clients.values()
                if record.snapshot is not None
                else record.cleanup_clients.values()
            )
        )
        failed_clients = (
            owned_client
            for failed in self._failed_generations.values()
            for owned_client in failed.clients.values()
        )
        return any(
            owned_client is client
            for owned_client in (*unpublished_clients, *generation_clients, *failed_clients)
        )

    async def _close_unpublished_slot(self, slot: RuntimeUnpublishedSlot) -> bool:
        self._bind_loop()
        record = self._get_unpublished_slot(slot)
        if record is None:
            return True
        return await self._close_unpublished_record(record)

    async def _close_unpublished_record(self, record: _UnpublishedSlotRecord) -> bool:
        record.accepting = False
        if not record.clients:
            self._unpublished_slots.pop(record.slot_id, None)
            return True
        if record.close_task is not None and record.close_task.done():
            record.close_task = None
        if record.close_task is None:
            cleanup_coroutine = self._cleanup_unpublished_record(record)
            try:
                record.close_task = asyncio.create_task(
                    cleanup_coroutine,
                    name=f"runtime-unpublished-cleanup-{record.slot_id}",
                )
            except BaseException as exc:
                cleanup_coroutine.close()
                record.close_failed = True
                self._record_cleanup_failure(
                    record.generation,
                    "unpublished_cleanup",
                    type(exc).__name__,
                )
                return False
        close_task = record.close_task
        result = await asyncio.shield(close_task)
        if record.close_task is close_task and close_task.done():
            record.close_task = None
        return result

    async def _cleanup_unpublished_record(self, record: _UnpublishedSlotRecord) -> bool:
        try:
            await self._close_clients_with_retries(record.generation, record.clients)
        except BaseException as exc:
            self._record_cleanup_failure(
                record.generation,
                "unpublished_cleanup",
                type(exc).__name__,
            )
        record.close_failed = bool(record.clients)
        if record.close_failed:
            return False
        self._unpublished_slots.pop(record.slot_id, None)
        return True

    async def _retry_failed_unpublished_slots(self) -> bool:
        records = tuple(
            record for record in self._unpublished_slots.values() if record.close_failed
        )
        if records:
            await asyncio.gather(
                *(self._close_unpublished_record(record) for record in records)
            )
        return not any(record.close_failed for record in self._unpublished_slots.values())

    async def _close_all_unpublished_slots(self) -> bool:
        records = tuple(self._unpublished_slots.values())
        if records:
            await asyncio.gather(
                *(self._close_unpublished_record(record) for record in records)
            )
        return not self._unpublished_slots

    def _reject_shared_generation_resources(self, candidate: RuntimeSnapshot) -> None:
        """Reject a candidate that reuses anything a live generation still owns.

        Every generation still present in ``_generations`` counts, not just the
        active one: a retiring generation keeps serving the requests that hold
        its lease, and its clients are closed the moment the last lease drops.
        A candidate that shared one of them would lose it under load.
        """
        candidate_mutable_ids: set[int] = set()
        for attribute in _CONFIG_GRAPH_ATTRIBUTES:
            candidate_mutable_ids.update(
                _mutable_graph_ids(_config_graph_value(candidate.config_loader, attribute))
            )
        candidate_client_ids = {
            id(client) for client in candidate.proxy_http_clients.values()
        }

        for record in self._generations.values():
            snapshot = record.snapshot
            if snapshot is None:
                live_clients: Iterable[httpx.AsyncClient] = record.cleanup_clients.values()
            else:
                generation_owned_resources = (
                    (snapshot.config_loader, candidate.config_loader),
                    (snapshot.operation_dispatcher, candidate.operation_dispatcher),
                    (snapshot.fusion_service, candidate.fusion_service),
                    (snapshot.router_model_service, candidate.router_model_service),
                    (snapshot.provider_models_service, candidate.provider_models_service),
                )
                if any(old is new for old, new in generation_owned_resources):
                    raise RuntimeGenerationConflictError(
                        "Runtime candidate shares a generation-owned service with a live "
                        "generation."
                    )

                live_mutable_ids: set[int] = set()
                for attribute in _CONFIG_GRAPH_ATTRIBUTES:
                    live_mutable_ids.update(
                        _mutable_graph_ids(
                            _config_graph_value(snapshot.config_loader, attribute)
                        )
                    )
                if live_mutable_ids & candidate_mutable_ids:
                    raise RuntimeGenerationConflictError(
                        "Runtime candidate shares mutable configuration with a live generation."
                    )
                live_clients = snapshot.proxy_http_clients.values()

            if any(id(client) in candidate_client_ids for client in live_clients):
                raise RuntimeGenerationConflictError(
                    "Runtime candidate shares a proxy HTTP client with a live generation."
                )

    def _schedule_cleanup(self, record: _GenerationRecord) -> None:
        if record.cleanup_task is not None:
            return
        if record.active_leases != 0:
            raise RuntimeManagerStateError(
                f"Cannot close runtime generation {record.generation} with active leases."
            )
        snapshot = record.snapshot
        if snapshot is None:
            self._finish_cleanup(
                record,
                RuntimeGenerationStatus.CLOSE_FAILED
                if record.cleanup_clients
                else RuntimeGenerationStatus.CLOSED,
                record.cleanup_clients,
            )
            return

        record.cleanup_clients = dict(snapshot.proxy_http_clients)
        record.snapshot = None
        record.status = RuntimeGenerationStatus.CLOSING
        cleanup_coroutine = self._cleanup_generation(record)
        try:
            task = asyncio.create_task(
                cleanup_coroutine,
                name=f"runtime-generation-cleanup-{record.generation}",
            )
        except BaseException as exc:
            cleanup_coroutine.close()
            self._record_cleanup_failure(
                record.generation,
                "generation_cleanup",
                type(exc).__name__,
            )
            self._finish_cleanup(
                record,
                RuntimeGenerationStatus.CLOSE_FAILED
                if record.cleanup_clients
                else RuntimeGenerationStatus.CLOSED,
                record.cleanup_clients,
            )
            return
        record.cleanup_task = task
        self._cleanup_tasks.add(task)
        task.add_done_callback(
            lambda completed, cleanup_record=record: self._cleanup_task_done(
                cleanup_record,
                completed,
            )
        )

    async def _cleanup_generation(self, record: _GenerationRecord) -> None:
        await self._close_clients_with_retries(record.generation, record.cleanup_clients)
        status = (
            RuntimeGenerationStatus.CLOSE_FAILED
            if record.cleanup_clients
            else RuntimeGenerationStatus.CLOSED
        )
        self._finish_cleanup(record, status, record.cleanup_clients)

    async def _close_clients_with_retries(
        self,
        generation: int,
        pending_clients: dict[str, httpx.AsyncClient],
    ) -> None:
        for _attempt in range(RUNTIME_CLEANUP_MAX_ATTEMPTS):
            failed_clients: dict[str, httpx.AsyncClient] = {}
            for client_name, client in tuple(pending_clients.items()):
                try:
                    failures = await close_http_clients({client_name: client})
                except BaseException as exc:
                    self._record_cleanup_failure(
                        generation,
                        client_name,
                        type(exc).__name__,
                    )
                    failed_clients[client_name] = client
                else:
                    if failures:
                        failure = failures[0]
                        self._record_cleanup_failure(
                            generation,
                            failure.client_name,
                            failure.exception_type,
                        )
                        failed_clients[client_name] = client
            pending_clients.clear()
            pending_clients.update(failed_clients)
            if not pending_clients:
                return

    def _cleanup_task_done(
        self,
        record: _GenerationRecord,
        task: asyncio.Task[None],
    ) -> None:
        self._cleanup_tasks.discard(task)
        if record.cleanup_task is not task:
            return
        record.cleanup_task = None
        if record.status is not RuntimeGenerationStatus.CLOSING:
            return
        exception_type = (
            "CancelledError"
            if task.cancelled()
            else type(task.exception()).__name__
            if task.exception() is not None
            else "IncompleteCleanup"
        )
        self._record_cleanup_failure(
            record.generation,
            "generation_cleanup",
            exception_type,
        )
        self._finish_cleanup(
            record,
            RuntimeGenerationStatus.CLOSE_FAILED
            if record.cleanup_clients
            else RuntimeGenerationStatus.CLOSED,
            record.cleanup_clients,
        )

    def _record_cleanup_failure(
        self,
        generation: int,
        resource: str,
        exception_type: str,
    ) -> None:
        failure = RuntimeCleanupFailure(
            generation=generation,
            resource=resource,
            exception_type=sanitize_exception_type_name(exception_type),
        )
        if failure not in self._cleanup_failures:
            self._cleanup_failures.append(failure)

    def _finish_cleanup(
        self,
        record: _GenerationRecord,
        status: RuntimeGenerationStatus,
        failed_clients: Mapping[str, httpx.AsyncClient],
    ) -> None:
        record.status = status
        if record.cleanup_task is not None:
            self._cleanup_tasks.discard(record.cleanup_task)
        record.cleanup_task = None
        self._generations.pop(record.generation, None)
        if failed_clients:
            self._failed_generations[record.generation] = _FailedGeneration(
                generation=record.generation,
                clients=dict(failed_clients),
            )
        else:
            self._failed_generations.pop(record.generation, None)
        record.cleanup_clients.clear()
        self._remember_terminal_status(record.generation, status)

    def _remember_terminal_status(
        self,
        generation: int,
        status: RuntimeGenerationStatus,
    ) -> None:
        self._generation_history[generation] = status
        self._generation_history.move_to_end(generation)
        while len(self._generation_history) > RUNTIME_GENERATION_HISTORY_LIMIT:
            self._generation_history.popitem(last=False)

    async def _retry_unresolved_clients(self) -> bool:
        async with self._recovery_lock:
            for generation, failed_generation in tuple(self._failed_generations.items()):
                await self._close_clients_with_retries(
                    generation,
                    failed_generation.clients,
                )
                if failed_generation.clients:
                    self._remember_terminal_status(
                        generation,
                        RuntimeGenerationStatus.CLOSE_FAILED,
                    )
                else:
                    self._failed_generations.pop(generation, None)
                    self._remember_terminal_status(
                        generation,
                        RuntimeGenerationStatus.CLOSED,
                    )
        return not self._failed_generations

    async def _drain_cleanup_tasks(self) -> None:
        while self._cleanup_tasks:
            cleanup_tasks = tuple(self._cleanup_tasks)
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            self._cleanup_tasks.difference_update(cleanup_tasks)
            await asyncio.sleep(0)

    async def _shutdown_impl(self) -> None:
        try:
            await self._close_all_unpublished_slots()
            records = tuple(self._generations.values())
            await asyncio.gather(*(record.zero_leases.wait() for record in records))
            for record in records:
                if record.status is RuntimeGenerationStatus.RETIRED:
                    self._schedule_cleanup(record)
            await self._drain_cleanup_tasks()
            unpublished_recovered = await self._retry_failed_unpublished_slots()
            recovered = await self._retry_unresolved_clients()
        except BaseException as exc:
            self._record_cleanup_failure(
                self._current_generation or 0,
                "runtime_shutdown",
                type(exc).__name__,
            )
            self._status = RuntimeManagerStatus.STOP_FAILED
            return

        if (
            unpublished_recovered
            and recovered
            and not self._unpublished_slots
            and not self._generations
            and not self._cleanup_tasks
        ):
            self._status = RuntimeManagerStatus.STOPPED
        else:
            self._record_cleanup_failure(
                self._current_generation or 0,
                "runtime_shutdown",
                "UnresolvedHttpClients",
            )
            self._status = RuntimeManagerStatus.STOP_FAILED


def _config_graph_value(config_loader: object, attribute: str) -> object:
    try:
        instance_values = vars(config_loader)
    except TypeError:
        instance_values = {}
    if attribute in instance_values:
        return instance_values[attribute]
    return getattr(config_loader, attribute, None)


def _mutable_graph_ids(root: object) -> set[int]:
    mutable_ids: set[int] = set()
    visited: set[int] = set()

    def visit(value: object) -> None:
        if isinstance(value, _IMMUTABLE_SCALAR_TYPES):
            return
        value_id = id(value)
        if value_id in visited:
            return
        visited.add(value_id)

        if isinstance(value, Mapping):
            if not isinstance(value, MappingProxyType):
                mutable_ids.add(value_id)
            for key, item in value.items():
                visit(key)
                visit(item)
            return
        if isinstance(value, (list, set)):
            mutable_ids.add(value_id)
            for item in value:
                visit(item)
            return
        if isinstance(value, bytearray):
            mutable_ids.add(value_id)
            return
        if isinstance(value, (tuple, frozenset)):
            for item in value:
                visit(item)
            return
        if is_dataclass(value) and not isinstance(value, type):
            params = getattr(type(value), "__dataclass_params__", None)
            if params is None or not params.frozen:
                mutable_ids.add(value_id)
            for data_field in fields(value):
                visit(getattr(value, data_field.name))
            return

        model_fields = getattr(type(value), "model_fields", None)
        if model_fields is None:
            model_fields = getattr(type(value), "__fields__", None)
        if isinstance(model_fields, Mapping):
            model_config = getattr(type(value), "model_config", {})
            frozen = isinstance(model_config, Mapping) and bool(model_config.get("frozen"))
            legacy_config = getattr(type(value), "Config", None)
            if legacy_config is not None and getattr(legacy_config, "allow_mutation", True) is False:
                frozen = True
            if not frozen:
                mutable_ids.add(value_id)
            for field_name in model_fields:
                visit(getattr(value, field_name, None))
            return

        try:
            attributes = vars(value)
        except TypeError:
            return
        mutable_ids.add(value_id)
        for item in attributes.values():
            visit(item)

    visit(root)
    return mutable_ids
