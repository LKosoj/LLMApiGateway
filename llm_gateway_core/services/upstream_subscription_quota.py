"""Upstream subscription quota fetcher.

Fetches subscription/quota information from upstream providers
(GitHub Copilot, Gemini CLI, Antigravity) and returns typed snapshots.
Uses a TTL cache to avoid hammering upstream APIs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
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


@dataclass(frozen=True, slots=True)
class _QuotaTarget:
    provider: str
    kind: str
    token_env: str
    token: str = field(repr=False)
    http_client: httpx.AsyncClient = field(repr=False, compare=False)
    semantic_digest: str
    credential_digest: str

    @property
    def cache_key(self) -> tuple[str, str]:
        return self.semantic_digest, self.credential_digest


def _semantic_digest(provider: str, kind: str, token_env: str) -> str:
    payload = json.dumps(
        [provider, kind, token_env],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _credential_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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

    def _target(
        self,
        *,
        provider: str,
        kind: str,
        token_env: str,
        token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> _QuotaTarget:
        return _QuotaTarget(
            provider=str(provider),
            kind=str(kind),
            token_env=str(token_env),
            token=str(token),
            http_client=self._client if http_client is None else http_client,
            semantic_digest=_semantic_digest(str(provider), str(kind), str(token_env)),
            credential_digest=_credential_digest(str(token)),
        )

    def _materialize_targets(
        self,
        providers_config: dict[str, Any],
    ) -> tuple[_QuotaTarget, ...]:
        http_client = self._client
        targets: list[_QuotaTarget] = []
        for provider_name, details in providers_config.items():
            quota_cfg = getattr(details, "subscription_quota", None)
            if quota_cfg is None:
                continue
            kind = str(quota_cfg.kind)
            token_env = str(quota_cfg.token_env)
            targets.append(
                self._target(
                    provider=str(provider_name),
                    kind=kind,
                    token_env=token_env,
                    token=os.getenv(token_env, ""),
                    http_client=http_client,
                )
            )
        return tuple(targets)

    def _prune_expired(self, now: float) -> None:
        expired = [
            key for key, (expires_at, _snapshot) in self._cache.items() if expires_at <= now
        ]
        for key in expired:
            self._cache.pop(key, None)

    def _get_cached(self, target: _QuotaTarget) -> SubscriptionQuotaSnapshot | None:
        now = time.monotonic()
        self._prune_expired(now)
        entry = self._cache.get(target.cache_key)
        if entry is None:
            return None
        _expires_at, snapshot = entry
        return snapshot

    def _set_cached(
        self,
        target: _QuotaTarget,
        snapshot: SubscriptionQuotaSnapshot,
        *,
        is_error: bool,
    ) -> None:
        if is_error:
            ttl = min(self._ttl / 2, 30.0)
        else:
            ttl = self._ttl
        now = time.monotonic()
        self._prune_expired(now)
        self._cache[target.cache_key] = (now + ttl, snapshot)

    async def fetch_github_copilot(
        self, *, copilot_token: str, provider: str = "github_copilot"
    ) -> SubscriptionQuotaSnapshot:
        target = self._target(
            provider=provider,
            kind="github_copilot",
            token_env="",
            token=copilot_token,
        )
        return await self._fetch_github_copilot(target)

    async def _fetch_github_copilot(
        self,
        target: _QuotaTarget,
    ) -> SubscriptionQuotaSnapshot:
        cached = self._get_cached(target)
        if cached is not None:
            return cached

        try:
            resp = await target.http_client.get(
                "https://api.github.com/copilot_internal/user",
                headers={
                    "Authorization": f"Bearer {target.token}",
                    "Accept": "application/json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "GitHubCopilotChat/0.26.7",
                },
            )
        except Exception as exc:
            logger.error(
                "GitHub Copilot quota request failed for provider %s (%s)",
                target.provider,
                type(exc).__name__,
            )
            snapshot = _error_snapshot(target.provider, target.kind, "Request failed.")
            self._set_cached(target, snapshot, is_error=True)
            return snapshot

        if resp.status_code != 200:
            logger.warning(
                "GitHub Copilot quota returned HTTP %s for provider %s",
                resp.status_code,
                target.provider,
            )
            snapshot = _error_snapshot(
                target.provider, target.kind, f"HTTP {resp.status_code}"
            )
            self._set_cached(target, snapshot, is_error=True)
            return snapshot

        try:
            data = resp.json()
        except Exception as exc:
            logger.error(
                "GitHub Copilot quota returned invalid JSON for provider %s (%s)",
                target.provider,
                type(exc).__name__,
            )
            snapshot = _error_snapshot(target.provider, target.kind, "Invalid JSON response")
            self._set_cached(target, snapshot, is_error=True)
            return snapshot

        # Try paid format first
        snapshot = _parse_copilot_paid(target.provider, data)
        if snapshot is None:
            # Try free format
            snapshot = _parse_copilot_free(target.provider, data)
        if snapshot is None:
            snapshot = _error_snapshot(
                target.provider, target.kind, "Unknown response format"
            )
            self._set_cached(target, snapshot, is_error=True)
            return snapshot

        self._set_cached(target, snapshot, is_error=False)
        return snapshot

    async def fetch_gemini_cli(
        self, *, access_token: str, provider: str = "gemini_cli"
    ) -> SubscriptionQuotaSnapshot:
        target = self._target(
            provider=provider,
            kind="gemini_cli",
            token_env="",
            token=access_token,
        )
        return await self._fetch_gemini_cli(target)

    async def _fetch_gemini_cli(
        self,
        target: _QuotaTarget,
    ) -> SubscriptionQuotaSnapshot:
        cached = self._get_cached(target)
        if cached is not None:
            return cached

        try:
            resp = await target.http_client.get(
                "https://cloudresourcemanager.googleapis.com/v1/projects?filter=lifecycleState:ACTIVE",
                headers={"Authorization": f"Bearer {target.token}"},
            )
        except Exception as exc:
            logger.error(
                "Gemini CLI quota request failed for provider %s (%s)",
                target.provider,
                type(exc).__name__,
            )
            snapshot = _error_snapshot(target.provider, target.kind, "Request failed.")
            self._set_cached(target, snapshot, is_error=True)
            return snapshot

        if resp.status_code != 200:
            logger.warning(
                "Gemini CLI quota returned HTTP %s for provider %s",
                resp.status_code,
                target.provider,
            )
            snapshot = _error_snapshot(
                target.provider, target.kind, f"HTTP {resp.status_code}"
            )
            self._set_cached(target, snapshot, is_error=True)
            return snapshot

        snapshot = SubscriptionQuotaSnapshot(
            provider=target.provider,
            kind=target.kind,
            plan="google_cloud",
            reset_date=None,
            categories={},
            fetched_at=time.time(),
            error=None,
        )
        self._set_cached(target, snapshot, is_error=False)
        return snapshot

    async def fetch_antigravity(
        self, *, access_token: str, provider: str = "antigravity"
    ) -> SubscriptionQuotaSnapshot:
        target = self._target(
            provider=provider,
            kind="antigravity",
            token_env="",
            token=access_token,
        )
        return await self._fetch_antigravity(target)

    async def _fetch_antigravity(
        self,
        target: _QuotaTarget,
    ) -> SubscriptionQuotaSnapshot:
        cached = self._get_cached(target)
        if cached is not None:
            return cached

        snapshot = SubscriptionQuotaSnapshot(
            provider=target.provider,
            kind=target.kind,
            plan=None,
            reset_date=None,
            categories={},
            fetched_at=time.time(),
            error=None,
        )
        self._set_cached(target, snapshot, is_error=False)
        return snapshot

    async def fetch_all(
        self, *, providers_config: dict[str, Any]
    ) -> list[SubscriptionQuotaSnapshot]:
        snapshots: list[SubscriptionQuotaSnapshot] = []
        targets = self._materialize_targets(providers_config)

        for target in targets:
            if not target.token:
                snapshots.append(
                    _error_snapshot(
                        target.provider,
                        target.kind,
                        f"Token env var {target.token_env} is not set",
                    )
                )
                continue

            if target.kind == "github_copilot":
                snapshot = await self._fetch_github_copilot(target)
            elif target.kind == "gemini_cli":
                snapshot = await self._fetch_gemini_cli(target)
            elif target.kind == "antigravity":
                snapshot = await self._fetch_antigravity(target)
            else:
                snapshot = _error_snapshot(
                    target.provider, target.kind, f"Unknown kind: {target.kind}"
                )

            snapshots.append(snapshot)

        return snapshots


def snapshot_to_dict(snapshot: SubscriptionQuotaSnapshot) -> dict[str, Any]:
    d = asdict(snapshot)
    # categories values are dataclass instances — asdict handles them recursively
    return d
