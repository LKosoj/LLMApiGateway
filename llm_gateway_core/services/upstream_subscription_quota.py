"""Upstream subscription quota fetcher.

Fetches subscription/quota information from upstream providers
(GitHub Copilot, Gemini CLI, Antigravity) and returns typed snapshots.
Uses a TTL cache to avoid hammering upstream APIs.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubscriptionQuotaCategory:
    used: int
    total: int
    remaining: int | None
    unlimited: bool


@dataclass(frozen=True)
class SubscriptionQuotaSnapshot:
    provider: str
    kind: str
    plan: str | None
    reset_date: str | None
    categories: dict[str, SubscriptionQuotaCategory]
    fetched_at: float
    error: str | None


def _error_snapshot(
    provider: str,
    kind: str,
    error: str,
) -> SubscriptionQuotaSnapshot:
    return SubscriptionQuotaSnapshot(
        provider=provider,
        kind=kind,
        plan=None,
        reset_date=None,
        categories={},
        fetched_at=time.time(),
        error=error,
    )


def _parse_copilot_paid(
    provider: str,
    data: dict[str, Any],
) -> SubscriptionQuotaSnapshot | None:
    quota_snapshots = data.get("quota_snapshots")
    if not isinstance(quota_snapshots, dict):
        return None

    categories: dict[str, SubscriptionQuotaCategory] = {}
    for cat_name, cat_data in quota_snapshots.items():
        if not isinstance(cat_data, dict):
            continue
        entitlement = cat_data.get("entitlement")
        remaining = cat_data.get("remaining")
        unlimited = bool(cat_data.get("unlimited", False))
        if unlimited:
            used = 0
            total = 0
            remaining_val: int | None = None
        elif isinstance(entitlement, int) and isinstance(remaining, int):
            used = max(0, entitlement - remaining)
            total = entitlement
            remaining_val = remaining
        else:
            continue
        categories[cat_name] = SubscriptionQuotaCategory(
            used=used,
            total=total,
            remaining=remaining_val,
            unlimited=unlimited,
        )

    if not categories:
        return None

    return SubscriptionQuotaSnapshot(
        provider=provider,
        kind="github_copilot",
        plan=data.get("copilot_plan"),
        reset_date=data.get("quota_reset_date"),
        categories=categories,
        fetched_at=time.time(),
        error=None,
    )


def _parse_copilot_free(
    provider: str,
    data: dict[str, Any],
) -> SubscriptionQuotaSnapshot | None:
    monthly_quotas = data.get("monthly_quotas")
    limited_user_quotas = data.get("limited_user_quotas")
    if not isinstance(monthly_quotas, dict) or not isinstance(limited_user_quotas, dict):
        return None

    categories: dict[str, SubscriptionQuotaCategory] = {}
    for cat_name in monthly_quotas:
        total = monthly_quotas.get(cat_name)
        used = limited_user_quotas.get(cat_name)
        if not isinstance(total, int) or not isinstance(used, int):
            continue
        remaining = max(0, total - used)
        categories[cat_name] = SubscriptionQuotaCategory(
            used=used,
            total=total,
            remaining=remaining,
            unlimited=False,
        )

    if not categories:
        return None

    return SubscriptionQuotaSnapshot(
        provider=provider,
        kind="github_copilot",
        plan="free",
        reset_date=data.get("limited_user_reset_date"),
        categories=categories,
        fetched_at=time.time(),
        error=None,
    )


class UpstreamSubscriptionQuotaService:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        ttl_seconds: float = 60.0,
    ) -> None:
        self._client = http_client
        self._ttl = ttl_seconds
        # key -> (expires_at, snapshot)
        self._cache: dict[tuple[str, str], tuple[float, SubscriptionQuotaSnapshot]] = {}

    def _get_cached(
        self, provider: str, kind: str
    ) -> SubscriptionQuotaSnapshot | None:
        key = (provider, kind)
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, snapshot = entry
        if time.monotonic() >= expires_at:
            return None
        return snapshot

    def _set_cached(
        self,
        provider: str,
        kind: str,
        snapshot: SubscriptionQuotaSnapshot,
        *,
        is_error: bool,
    ) -> None:
        key = (provider, kind)
        if is_error:
            ttl = min(self._ttl / 2, 30.0)
        else:
            ttl = self._ttl
        self._cache[key] = (time.monotonic() + ttl, snapshot)

    async def fetch_github_copilot(
        self, *, copilot_token: str, provider: str = "github_copilot"
    ) -> SubscriptionQuotaSnapshot:
        cached = self._get_cached(provider, "github_copilot")
        if cached is not None:
            return cached

        try:
            resp = await self._client.get(
                "https://api.github.com/copilot_internal/user",
                headers={
                    "Authorization": f"Bearer {copilot_token}",
                    "Accept": "application/json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "GitHubCopilotChat/0.26.7",
                },
            )
        except Exception as exc:
            logger.exception("GitHub Copilot quota request failed for provider %s", provider)
            snapshot = _error_snapshot(provider, "github_copilot", f"Request failed: {exc}")
            self._set_cached(provider, "github_copilot", snapshot, is_error=True)
            return snapshot

        if resp.status_code != 200:
            logger.warning(
                "GitHub Copilot quota returned HTTP %s for provider %s", resp.status_code, provider
            )
            snapshot = _error_snapshot(
                provider, "github_copilot", f"HTTP {resp.status_code}"
            )
            self._set_cached(provider, "github_copilot", snapshot, is_error=True)
            return snapshot

        try:
            data = resp.json()
        except Exception:
            logger.exception("GitHub Copilot quota returned invalid JSON for provider %s", provider)
            snapshot = _error_snapshot(provider, "github_copilot", "Invalid JSON response")
            self._set_cached(provider, "github_copilot", snapshot, is_error=True)
            return snapshot

        # Try paid format first
        snapshot = _parse_copilot_paid(provider, data)
        if snapshot is None:
            # Try free format
            snapshot = _parse_copilot_free(provider, data)
        if snapshot is None:
            snapshot = _error_snapshot(provider, "github_copilot", "Unknown response format")
            self._set_cached(provider, "github_copilot", snapshot, is_error=True)
            return snapshot

        self._set_cached(provider, "github_copilot", snapshot, is_error=False)
        return snapshot

    async def fetch_gemini_cli(
        self, *, access_token: str, provider: str = "gemini_cli"
    ) -> SubscriptionQuotaSnapshot:
        cached = self._get_cached(provider, "gemini_cli")
        if cached is not None:
            return cached

        try:
            resp = await self._client.get(
                "https://cloudresourcemanager.googleapis.com/v1/projects?filter=lifecycleState:ACTIVE",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except Exception as exc:
            logger.exception("Gemini CLI quota request failed for provider %s", provider)
            snapshot = _error_snapshot(provider, "gemini_cli", f"Request failed: {exc}")
            self._set_cached(provider, "gemini_cli", snapshot, is_error=True)
            return snapshot

        if resp.status_code != 200:
            logger.warning(
                "Gemini CLI quota returned HTTP %s for provider %s", resp.status_code, provider
            )
            snapshot = _error_snapshot(provider, "gemini_cli", f"HTTP {resp.status_code}")
            self._set_cached(provider, "gemini_cli", snapshot, is_error=True)
            return snapshot

        snapshot = SubscriptionQuotaSnapshot(
            provider=provider,
            kind="gemini_cli",
            plan="google_cloud",
            reset_date=None,
            categories={},
            fetched_at=time.time(),
            error=None,
        )
        self._set_cached(provider, "gemini_cli", snapshot, is_error=False)
        return snapshot

    async def fetch_antigravity(
        self, *, access_token: str, provider: str = "antigravity"
    ) -> SubscriptionQuotaSnapshot:
        cached = self._get_cached(provider, "antigravity")
        if cached is not None:
            return cached

        snapshot = SubscriptionQuotaSnapshot(
            provider=provider,
            kind="antigravity",
            plan=None,
            reset_date=None,
            categories={},
            fetched_at=time.time(),
            error=None,
        )
        self._set_cached(provider, "antigravity", snapshot, is_error=False)
        return snapshot

    async def fetch_all(
        self, *, providers_config: dict[str, Any]
    ) -> list[SubscriptionQuotaSnapshot]:
        snapshots: list[SubscriptionQuotaSnapshot] = []

        for provider_name, details in providers_config.items():
            quota_cfg = getattr(details, "subscription_quota", None)
            if quota_cfg is None:
                continue

            kind = quota_cfg.kind
            token_env = quota_cfg.token_env
            token = os.getenv(token_env, "")

            if not token:
                snapshots.append(
                    _error_snapshot(
                        provider_name,
                        kind,
                        f"Token env var {token_env} is not set",
                    )
                )
                continue

            if kind == "github_copilot":
                snapshot = await self.fetch_github_copilot(
                    copilot_token=token, provider=provider_name
                )
            elif kind == "gemini_cli":
                snapshot = await self.fetch_gemini_cli(
                    access_token=token, provider=provider_name
                )
            elif kind == "antigravity":
                snapshot = await self.fetch_antigravity(
                    access_token=token, provider=provider_name
                )
            else:
                snapshot = _error_snapshot(
                    provider_name, kind, f"Unknown kind: {kind}"
                )

            snapshots.append(snapshot)

        return snapshots


def snapshot_to_dict(snapshot: SubscriptionQuotaSnapshot) -> dict[str, Any]:
    d = asdict(snapshot)
    # categories values are dataclass instances — asdict handles them recursively
    return d
