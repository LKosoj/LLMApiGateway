"""Free-tier LLM catalog (freellmapi.co) fetch service.

Periodically pulls the public JSON catalog from freellmapi.co, keeps only
models whose lower-bound monthly token budget clears a configurable floor,
and exposes the filtered result to the `/v1/ui/free-models` page. By design
this is much simpler than `openrouter_free_models.py`: no lease/generation
machinery, just a lock-protected immutable snapshot and `get_status()`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from ..utils.log_redaction import safe_exception_type_name

logger = logging.getLogger(__name__)

FREE_LLM_CATALOG_SOURCE_URL = "https://api.freellmapi.co/v1/latest"
FREE_LLM_CATALOG_FETCH_TIMEOUT_SECONDS = 30.0
REFRESH_INTERVAL_SECONDS = 24 * 60 * 60

# Matches the lower bound of observed "monthlyTokenBudget" strings such as
# "~6M", "~50-100M" or "~2M (60-100/hr)". Strings without a leading "~<digits>M"
# (e.g. "free · 40 RPM", "promo (trial)", "~? (anon)") intentionally do not match.
_BUDGET_FLOOR_PATTERN = re.compile(r"^~(\d+)(?:-\d+)?M\b")


def parse_monthly_token_budget_floor(raw: object) -> int | None:
    """Extract the lower-bound monthly token budget from a source string.

    Returns ``None`` for any string that does not encode a token count
    (rate-limit-only or promotional budgets) or for non-string input.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    match = _BUDGET_FLOOR_PATTERN.match(text)
    if match is None:
        return None
    return int(match.group(1)) * 1_000_000


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclass(frozen=True, slots=True)
class Limits:
    rpm: int | None
    rpd: int | None
    tpm: int | None
    tpd: int | None

    def to_dict(self) -> dict[str, int | None]:
        return {"rpm": self.rpm, "rpd": self.rpd, "tpm": self.tpm, "tpd": self.tpd}


@dataclass(frozen=True, slots=True)
class Quirk:
    slug: str
    title: str
    body: str
    severity: str

    def to_dict(self) -> dict[str, str]:
        return {
            "slug": self.slug,
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class Model:
    platform_id: str
    model_id: str
    display_name: str
    intelligence_rank: int | None
    speed_rank: int | None
    size_label: str | None
    limits: Limits
    monthly_token_budget: str
    monthly_token_budget_floor: int
    context_window: int | None
    supports_vision: bool
    supports_tools: bool
    quirks: tuple[Quirk, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "modelId": self.model_id,
            "displayName": self.display_name,
            "intelligenceRank": self.intelligence_rank,
            "speedRank": self.speed_rank,
            "sizeLabel": self.size_label,
            "limits": self.limits.to_dict(),
            "monthlyTokenBudget": self.monthly_token_budget,
            "monthlyTokenBudgetFloor": self.monthly_token_budget_floor,
            "contextWindow": self.context_window,
            "supportsVision": self.supports_vision,
            "supportsTools": self.supports_tools,
            "quirks": [quirk.to_dict() for quirk in self.quirks],
        }


@dataclass(frozen=True, slots=True)
class Provider:
    id: str
    name: str
    models: tuple[Model, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "models": [model.to_dict() for model in self.models],
        }


@dataclass(frozen=True, slots=True)
class Snapshot:
    fetched_at: str
    source_version: str | None
    source_generated_at: str | None
    min_monthly_tokens: int
    providers: tuple[Provider, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "updatedAt": self.fetched_at,
            "sourceVersion": self.source_version,
            "sourceGeneratedAt": self.source_generated_at,
            "minMonthlyTokens": self.min_monthly_tokens,
            "providers": [provider.to_dict() for provider in self.providers],
        }


def _parse_quirk(raw: object) -> Quirk | None:
    if not isinstance(raw, dict):
        return None
    return Quirk(
        slug=str(raw.get("slug", "")),
        title=str(raw.get("title", "")),
        body=str(raw.get("body", "")),
        severity=str(raw.get("severity", "")),
    )


def _parse_model(raw: object, *, min_monthly_tokens: int) -> Model | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("enabled") is not True:
        return None

    platform_id = raw.get("platform")
    model_id = raw.get("modelId")
    if not isinstance(platform_id, str) or not platform_id:
        return None
    if not isinstance(model_id, str) or not model_id:
        return None

    monthly_token_budget = raw.get("monthlyTokenBudget")
    floor = parse_monthly_token_budget_floor(monthly_token_budget)
    if floor is None or floor < min_monthly_tokens:
        return None

    display_name = raw.get("displayName")
    if not isinstance(display_name, str) or not display_name:
        display_name = model_id

    raw_limits = raw.get("limits")
    raw_limits = raw_limits if isinstance(raw_limits, dict) else {}
    limits = Limits(
        rpm=_int_or_none(raw_limits.get("rpm")),
        rpd=_int_or_none(raw_limits.get("rpd")),
        tpm=_int_or_none(raw_limits.get("tpm")),
        tpd=_int_or_none(raw_limits.get("tpd")),
    )

    raw_quirks = raw.get("quirks")
    raw_quirks = raw_quirks if isinstance(raw_quirks, list) else []
    quirks = tuple(
        quirk for quirk in (_parse_quirk(entry) for entry in raw_quirks) if quirk is not None
    )

    return Model(
        platform_id=platform_id,
        model_id=model_id,
        display_name=display_name,
        intelligence_rank=_int_or_none(raw.get("intelligenceRank")),
        speed_rank=_int_or_none(raw.get("speedRank")),
        size_label=raw.get("sizeLabel") if isinstance(raw.get("sizeLabel"), str) else None,
        limits=limits,
        monthly_token_budget=monthly_token_budget,
        monthly_token_budget_floor=floor,
        context_window=_int_or_none(raw.get("contextWindow")),
        supports_vision=bool(raw.get("supportsVision", False)),
        supports_tools=bool(raw.get("supportsTools", False)),
        quirks=quirks,
    )


def build_snapshot(
    payload: object,
    *,
    min_monthly_tokens: int,
    now_iso: str,
) -> Snapshot:
    """Build an immutable, filtered, deterministically-sorted catalog snapshot.

    Raises ``ValueError`` on a payload that is not a JSON object or that is
    missing the ``platforms``/``models`` arrays -- callers must treat that as
    an honest fetch failure rather than silently falling back to an empty
    catalog.
    """
    if not isinstance(payload, dict):
        raise ValueError("Free LLM catalog payload must be a JSON object.")
    raw_platforms = payload.get("platforms")
    raw_models = payload.get("models")
    if not isinstance(raw_platforms, list) or not isinstance(raw_models, list):
        raise ValueError(
            "Free LLM catalog payload must contain 'platforms' and 'models' arrays."
        )

    platform_names: dict[str, str] = {}
    for raw_platform in raw_platforms:
        if not isinstance(raw_platform, dict):
            continue
        platform_id = raw_platform.get("id")
        platform_name = raw_platform.get("name")
        if (
            isinstance(platform_id, str)
            and platform_id
            and isinstance(platform_name, str)
            and platform_name
        ):
            platform_names[platform_id] = platform_name

    models_by_platform: dict[str, list[Model]] = {}
    for raw_model in raw_models:
        model = _parse_model(raw_model, min_monthly_tokens=min_monthly_tokens)
        if model is None:
            continue
        models_by_platform.setdefault(model.platform_id, []).append(model)

    providers: list[Provider] = []
    for platform_id, models in models_by_platform.items():
        sorted_models = sorted(
            models,
            key=lambda model: (-model.monthly_token_budget_floor, model.display_name),
        )
        providers.append(
            Provider(
                id=platform_id,
                name=platform_names.get(platform_id, platform_id),
                models=tuple(sorted_models),
            )
        )

    providers.sort(
        key=lambda provider: (
            -max(model.monthly_token_budget_floor for model in provider.models),
            provider.name,
        )
    )

    source_version = payload.get("version")
    source_generated_at = payload.get("generatedAt")
    return Snapshot(
        fetched_at=now_iso,
        source_version=source_version if isinstance(source_version, str) else None,
        source_generated_at=(
            source_generated_at if isinstance(source_generated_at, str) else None
        ),
        min_monthly_tokens=min_monthly_tokens,
        providers=tuple(providers),
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class FreeLlmCatalogService:
    """Owns the last-fetched free-tier catalog snapshot and its fetch status."""

    def __init__(self, *, min_monthly_tokens: int = 30_000_000) -> None:
        self.min_monthly_tokens = min_monthly_tokens
        self._lock = asyncio.Lock()
        self._snapshot: Snapshot | None = None
        self._last_error: str | None = None

    async def refresh_once(self, http_client: httpx.AsyncClient) -> None:
        """Fetch and replace the snapshot; leave it untouched on any failure.

        Never raises: HTTP failures (including timeouts/connect errors),
        malformed JSON, and a malformed catalog shape (``ValueError`` from
        ``build_snapshot``) are all recorded in ``last_error`` and the
        previous snapshot -- if any -- is kept as-is.
        """
        # The fetch happens outside the lock so a slow/timing-out request
        # (up to 30s) never blocks concurrent get_status() readers; the lock
        # only guards the state swap.
        try:
            response = await http_client.get(
                FREE_LLM_CATALOG_SOURCE_URL,
                timeout=FREE_LLM_CATALOG_FETCH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
            snapshot = build_snapshot(
                payload,
                min_monthly_tokens=self.min_monthly_tokens,
                now_iso=_utc_now_iso(),
            )
        except Exception as exc:
            logger.warning(
                "Free LLM catalog refresh failed: error_type=%s.",
                safe_exception_type_name(exc),
            )
            async with self._lock:
                self._last_error = safe_exception_type_name(exc)
            return
        async with self._lock:
            self._snapshot = snapshot
            self._last_error = None

    async def get_status(self) -> dict[str, Any]:
        async with self._lock:
            snapshot = self._snapshot
            last_error = self._last_error
        if snapshot is None:
            return {
                "updatedAt": None,
                "sourceVersion": None,
                "sourceGeneratedAt": None,
                "lastError": last_error,
                "minMonthlyTokens": self.min_monthly_tokens,
                "providers": [],
            }
        payload = snapshot.to_dict()
        payload["lastError"] = last_error
        return payload
