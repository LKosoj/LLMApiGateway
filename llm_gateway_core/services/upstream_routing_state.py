from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Iterable, Literal, Mapping

from llm_gateway_core.config.loader import (
    resolve_provider_api_key_value,
)
from llm_gateway_core.utils.api_keys import split_api_keys

KEYLESS_FINGERPRINT = "keyless"
DEFAULT_COOLDOWN_SECONDS = 600.0
PENALTY_DECAY_SECONDS = 300.0
DEFAULT_SESSION_AFFINITY_TTL_SECONDS = 3600.0
KeySelectionStrategy = Literal["round-robin", "fill-first", "priority"]


def fingerprint_api_key(api_key: str | None) -> str:
    if not api_key:
        return KEYLESS_FINGERPRINT
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class UpstreamQuotaLimits:
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "UpstreamQuotaLimits | None":
        if not isinstance(value, Mapping):
            return None

        def _positive_int(name: str) -> int | None:
            raw_value = value.get(name)
            if raw_value is None or raw_value == "":
                return None
            parsed = int(raw_value)
            if parsed <= 0:
                raise ValueError(f"{name} must be a positive integer")
            return parsed

        return cls(
            rpm=_positive_int("rpm"),
            rpd=_positive_int("rpd"),
            tpm=_positive_int("tpm"),
            tpd=_positive_int("tpd"),
        )


@dataclass(frozen=True)
class SelectedUpstreamKey:
    api_key: str | None
    fingerprint: str
    blocked_reason: str | None = None
    candidate_id: str | None = None

    @property
    def available(self) -> bool:
        return self.blocked_reason is None


@dataclass(frozen=True)
class UpstreamKeyCandidate:
    api_key: str | None
    order: int = 0
    priority: int = 0
    candidate_id: str | None = None

    @property
    def fingerprint(self) -> str:
        return fingerprint_api_key(self.api_key)


@dataclass
class _RuntimeState:
    health_status: str = "unknown"
    last_checked_at: str | None = None
    last_error: str | None = None
    cooldown_until: float | None = None
    penalty_score: float = 0.0
    penalty_updated_at: float = 0.0


@dataclass
class _AffinityBinding:
    fingerprint: str
    expires_at: float


class UpstreamRoutingState:
    def __init__(self, *, time_func: Any = time.time, monotonic_func: Any = time.monotonic) -> None:
        self._time_func = time_func
        self._monotonic_func = monotonic_func
        self._lock = Lock()
        self._states: dict[tuple[str, str, str], _RuntimeState] = {}
        self._request_windows: dict[tuple[str, str, str], deque[float]] = defaultdict(deque)
        self._token_windows: dict[tuple[str, str, str], deque[tuple[float, int]]] = defaultdict(deque)
        self._round_robin_indexes: dict[tuple[str, str, tuple[str, ...]], int] = {}
        self._session_affinity: dict[tuple[str, str, str, str], _AffinityBinding] = {}

    def select_key(
        self,
        provider: str,
        model: str,
        api_keys: Iterable[str | None],
        *,
        limits: UpstreamQuotaLimits | None = None,
        strategy: KeySelectionStrategy = "round-robin",
        session_id: str | None = None,
        affinity_scope: str | None = None,
        session_affinity_ttl_seconds: float | None = None,
        pool_name: str = "default",
    ) -> SelectedUpstreamKey:
        keys = [key for key in api_keys if key]
        if not keys:
            keys = [None]
        candidates = [UpstreamKeyCandidate(api_key=key, order=index) for index, key in enumerate(keys)]
        return self.select_key_from_candidates(
            provider,
            model,
            candidates,
            limits=limits,
            strategy=strategy,
            session_id=session_id,
            affinity_scope=affinity_scope,
            session_affinity_ttl_seconds=session_affinity_ttl_seconds,
            pool_name=pool_name,
        )

    def select_key_from_candidates(
        self,
        provider: str,
        model: str,
        candidates: Iterable[UpstreamKeyCandidate],
        *,
        limits: UpstreamQuotaLimits | None = None,
        strategy: KeySelectionStrategy = "round-robin",
        session_id: str | None = None,
        affinity_scope: str | None = None,
        session_affinity_ttl_seconds: float | None = None,
        pool_name: str = "default",
    ) -> SelectedUpstreamKey:
        key_candidates = list(candidates)
        if not key_candidates:
            key_candidates = [UpstreamKeyCandidate(api_key=None)]
        if strategy not in {"round-robin", "fill-first", "priority"}:
            raise ValueError("strategy must be one of: round-robin, fill-first, priority")

        now = self._monotonic_func()
        available: list[tuple[float, UpstreamKeyCandidate]] = []
        blocked_reasons: list[str] = []
        with self._lock:
            for candidate in key_candidates:
                fingerprint = candidate.fingerprint
                ref = (provider, model, fingerprint)
                state = self._state_for(ref)
                cooldown_remaining = self._cooldown_remaining(state, now)
                if cooldown_remaining is not None:
                    blocked_reasons.append(f"{fingerprint}: cooldown {cooldown_remaining:.0f}s")
                    continue
                quota_reason = self._quota_block_reason(ref, limits, now)
                if quota_reason:
                    blocked_reasons.append(f"{fingerprint}: {quota_reason}")
                    continue
                available.append((self._decayed_penalty(state, now), candidate))

            if not available:
                reason = "; ".join(blocked_reasons) or "no upstream keys available"
                return SelectedUpstreamKey(None, KEYLESS_FINGERPRINT, reason)

            affinity_key = self._affinity_key(provider, model, pool_name, session_id, affinity_scope)
            if affinity_key is not None:
                binding = self._session_affinity.get(affinity_key)
                if binding is not None and binding.expires_at > now:
                    selected = self._candidate_by_fingerprint(available, binding.fingerprint)
                    if selected is not None:
                        self._session_affinity[affinity_key] = _AffinityBinding(
                            fingerprint=binding.fingerprint,
                            expires_at=now + self._normalized_affinity_ttl(session_affinity_ttl_seconds),
                        )
                        return self._selected_from_candidate(selected)
                elif binding is not None:
                    self._session_affinity.pop(affinity_key, None)

            selected = self._select_available_candidate(provider, model, available, strategy)
            result = self._selected_from_candidate(selected)
            if affinity_key is not None:
                self._session_affinity[affinity_key] = _AffinityBinding(
                    fingerprint=result.fingerprint,
                    expires_at=now + self._normalized_affinity_ttl(session_affinity_ttl_seconds),
                )
            return result

    def record_attempt_start(self, provider: str, model: str, key_fingerprint: str) -> None:
        ref = (provider, model, key_fingerprint)
        now = self._monotonic_func()
        with self._lock:
            self._request_windows[ref].append(now)
            self._trim_numeric_window(self._request_windows[ref], now, 86400.0)
            self._state_for(ref)

    def record_tokens(self, provider: str, model: str, key_fingerprint: str, total_tokens: int | None) -> None:
        if not total_tokens or total_tokens <= 0:
            return
        ref = (provider, model, key_fingerprint)
        now = self._monotonic_func()
        with self._lock:
            self._token_windows[ref].append((now, int(total_tokens)))
            self._trim_token_window(self._token_windows[ref], now, 86400.0)
            self._state_for(ref)

    def record_success(self, provider: str, model: str, key_fingerprint: str) -> None:
        self.mark_health(provider, model, key_fingerprint, "healthy", None)

    def record_failure(
        self,
        provider: str,
        model: str,
        key_fingerprint: str,
        error_detail: object,
        *,
        temporary: bool,
        apply_penalty: bool,
        retry_after: float | None = None,
    ) -> None:
        now = self._monotonic_func()
        status_code = getattr(error_detail, "status_code", None)
        health_status = "invalid" if status_code in {401, 403} else "error"
        with self._lock:
            ref = (provider, model, key_fingerprint)
            state = self._state_for(ref)
            state.health_status = health_status
            state.last_checked_at = self._utc_now_iso()
            state.last_error = str(error_detail)[:500] if error_detail else None
            if temporary:
                cooldown_seconds = max(DEFAULT_COOLDOWN_SECONDS, float(retry_after or 0.0))
                state.cooldown_until = now + cooldown_seconds
            if apply_penalty and temporary:
                state.penalty_score = min(100.0, self._decayed_penalty(state, now) + 25.0)
                state.penalty_updated_at = now

    def mark_health(
        self,
        provider: str,
        model: str,
        key_fingerprint: str,
        health_status: str,
        last_error: str | None,
    ) -> None:
        with self._lock:
            state = self._state_for((provider, model, key_fingerprint))
            state.health_status = health_status
            state.last_checked_at = self._utc_now_iso()
            state.last_error = last_error[:500] if last_error else None
            if health_status == "healthy":
                state.cooldown_until = None
                state.penalty_score = max(0.0, state.penalty_score - 10.0)
                state.penalty_updated_at = self._monotonic_func()

    def order_rules_by_penalty(
        self,
        rules: list[dict[str, Any]],
        providers_config: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        now = self._monotonic_func()

        def _rule_penalty(index_and_rule: tuple[int, dict[str, Any]]) -> tuple[float, int]:
            index, rule = index_and_rule
            provider = rule.get("provider")
            model = rule.get("model")
            if not isinstance(provider, str) or not isinstance(model, str):
                return 0.0, index
            provider_config = providers_config.get(provider)
            fingerprints = self._fingerprints_for_rule(provider_config, rule)
            with self._lock:
                penalties = [
                    self._decayed_penalty(self._state_for((provider, model, fingerprint)), now)
                    for fingerprint in fingerprints
                ]
            return (min(penalties) if penalties else 0.0), index

        return [rule for _index, rule in sorted(enumerate(rules), key=_rule_penalty)]

    def get_status_rows(self) -> list[dict[str, Any]]:
        now = self._monotonic_func()
        with self._lock:
            rows = []
            refs = set(self._states) | set(self._request_windows) | set(self._token_windows)
            for provider, model, key_fingerprint in sorted(refs):
                ref = (provider, model, key_fingerprint)
                state = self._state_for(ref)
                rows.append(
                    {
                        "provider": provider,
                        "model": model,
                        "upstream_key_fingerprint": key_fingerprint,
                        "health_status": state.health_status,
                        "last_checked_at": state.last_checked_at,
                        "last_error": state.last_error,
                        "cooldown_remaining_seconds": self._cooldown_remaining(state, now) or 0.0,
                        "penalty_score": round(self._decayed_penalty(state, now), 2),
                        "requests_last_minute": self._count_since(self._request_windows[ref], now, 60.0),
                        "requests_last_day": self._count_since(self._request_windows[ref], now, 86400.0),
                        "tokens_last_minute": self._tokens_since(self._token_windows[ref], now, 60.0),
                        "tokens_last_day": self._tokens_since(self._token_windows[ref], now, 86400.0),
                    }
                )
            return rows

    def _fingerprints_for_rule(self, provider_config: Any, rule: Mapping[str, Any]) -> list[str]:
        if provider_config is None:
            return [KEYLESS_FINGERPRINT]
        pool_name = rule.get("upstream_key_pool")
        try:
            if isinstance(pool_name, str) and pool_name.strip():
                pool_fingerprints = self._fingerprints_for_provider_key_pool(provider_config, pool_name.strip())
                return pool_fingerprints

            resolved = resolve_provider_api_key_value(getattr(provider_config, "apikey", None))
        except Exception:
            return [KEYLESS_FINGERPRINT]
        keys = split_api_keys(resolved)
        if not keys:
            return [KEYLESS_FINGERPRINT]
        return [fingerprint_api_key(key) for key in keys]

    def _fingerprints_for_provider_key_pool(self, provider_config: Any, pool_name: str) -> list[str]:
        pools = getattr(provider_config, "upstream_key_pools", None)
        if not isinstance(pools, Mapping):
            return []
        pool = pools.get(pool_name)
        if pool is None:
            return []
        fingerprints: list[str] = []
        keys = getattr(pool, "keys", None)
        if not isinstance(keys, list):
            return fingerprints
        for key_spec in keys:
            if getattr(key_spec, "enabled", True) is False:
                continue
            resolved = resolve_provider_api_key_value(getattr(key_spec, "apikey", None))
            for api_key in split_api_keys(resolved):
                fingerprint = fingerprint_api_key(api_key)
                if fingerprint not in fingerprints:
                    fingerprints.append(fingerprint)
        return fingerprints

    def _affinity_key(
        self,
        provider: str,
        model: str,
        pool_name: str,
        session_id: str | None,
        affinity_scope: str | None,
    ) -> tuple[str, str, str, str, str] | None:
        if not session_id:
            return None
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return None
        normalized_scope = affinity_scope.strip() if isinstance(affinity_scope, str) else "default"
        if not normalized_scope:
            normalized_scope = "default"
        return provider, model, pool_name, normalized_scope, normalized_session_id

    def _normalized_affinity_ttl(self, value: float | None) -> float:
        if value is None:
            return DEFAULT_SESSION_AFFINITY_TTL_SECONDS
        return max(1.0, float(value))

    def _candidate_by_fingerprint(
        self,
        available: list[tuple[float, UpstreamKeyCandidate]],
        fingerprint: str,
    ) -> UpstreamKeyCandidate | None:
        for _penalty, candidate in available:
            if candidate.fingerprint == fingerprint:
                return candidate
        return None

    def _selected_from_candidate(self, candidate: UpstreamKeyCandidate) -> SelectedUpstreamKey:
        return SelectedUpstreamKey(
            candidate.api_key,
            candidate.fingerprint,
            candidate_id=candidate.candidate_id,
        )

    def _select_available_candidate(
        self,
        provider: str,
        model: str,
        available: list[tuple[float, UpstreamKeyCandidate]],
        strategy: KeySelectionStrategy,
    ) -> UpstreamKeyCandidate:
        if strategy == "fill-first":
            return min(available, key=lambda item: (item[1].order, item[1].fingerprint))[1]

        if strategy == "priority":
            highest_priority = max(candidate.priority for _penalty, candidate in available)
            available = [
                (penalty, candidate)
                for penalty, candidate in available
                if candidate.priority == highest_priority
            ]

        available.sort(key=lambda item: (item[0], item[1].order, item[1].fingerprint))
        best_penalty = available[0][0]
        best_candidates = [item for item in available if item[0] == best_penalty]
        pool_key = (provider, model, tuple(item[1].fingerprint for item in best_candidates))
        index = self._round_robin_indexes.get(pool_key, 0) % len(best_candidates)
        self._round_robin_indexes[pool_key] = (index + 1) % len(best_candidates)
        return best_candidates[index][1]

    def _state_for(self, ref: tuple[str, str, str]) -> _RuntimeState:
        state = self._states.get(ref)
        if state is None:
            state = _RuntimeState()
            self._states[ref] = state
        return state

    def _quota_block_reason(
        self,
        ref: tuple[str, str, str],
        limits: UpstreamQuotaLimits | None,
        now: float,
    ) -> str | None:
        if limits is None:
            return None
        requests = self._request_windows[ref]
        tokens = self._token_windows[ref]
        self._trim_numeric_window(requests, now, 86400.0)
        self._trim_token_window(tokens, now, 86400.0)
        if limits.rpm is not None and self._count_since(requests, now, 60.0) >= limits.rpm:
            return "rpm quota exhausted"
        if limits.rpd is not None and self._count_since(requests, now, 86400.0) >= limits.rpd:
            return "rpd quota exhausted"
        if limits.tpm is not None and self._tokens_since(tokens, now, 60.0) >= limits.tpm:
            return "tpm quota exhausted"
        if limits.tpd is not None and self._tokens_since(tokens, now, 86400.0) >= limits.tpd:
            return "tpd quota exhausted"
        return None

    def _cooldown_remaining(self, state: _RuntimeState, now: float) -> float | None:
        if state.cooldown_until is None:
            return None
        remaining = state.cooldown_until - now
        if remaining <= 0:
            state.cooldown_until = None
            return None
        return remaining

    def _decayed_penalty(self, state: _RuntimeState, now: float) -> float:
        if state.penalty_score <= 0:
            return 0.0
        elapsed = max(0.0, now - state.penalty_updated_at)
        if elapsed <= 0:
            return state.penalty_score
        remaining = max(0.0, state.penalty_score - (elapsed / PENALTY_DECAY_SECONDS) * 25.0)
        if remaining == 0:
            state.penalty_score = 0.0
        return remaining

    def _trim_numeric_window(self, values: deque[float], now: float, seconds: float) -> None:
        cutoff = now - seconds
        while values and values[0] < cutoff:
            values.popleft()

    def _trim_token_window(self, values: deque[tuple[float, int]], now: float, seconds: float) -> None:
        cutoff = now - seconds
        while values and values[0][0] < cutoff:
            values.popleft()

    def _count_since(self, values: deque[float], now: float, seconds: float) -> int:
        cutoff = now - seconds
        return sum(1 for timestamp in values if timestamp >= cutoff)

    def _tokens_since(self, values: deque[tuple[float, int]], now: float, seconds: float) -> int:
        cutoff = now - seconds
        return sum(tokens for timestamp, tokens in values if timestamp >= cutoff)

    def _utc_now_iso(self) -> str:
        return datetime.fromtimestamp(self._time_func(), tz=timezone.utc).isoformat()


def upstream_limits_for_model(provider_config: Any, model: str) -> UpstreamQuotaLimits | None:
    models_metadata = getattr(provider_config, "models", None)
    if not isinstance(models_metadata, Mapping):
        return None
    model_metadata = models_metadata.get(model)
    if not isinstance(model_metadata, Mapping):
        return None
    return UpstreamQuotaLimits.from_mapping(model_metadata.get("upstream_limits"))
