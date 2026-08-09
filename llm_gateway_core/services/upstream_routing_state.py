from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Iterable, Literal, Mapping

from llm_gateway_core.config.loader import (
    resolve_provider_api_key_value,
)
from llm_gateway_core.services.error_classifier import (
    classify_error,
    is_daily_quota_exhausted_error,
    parse_learned_limit_from_error,
)
from llm_gateway_core.utils.api_keys import split_api_keys

KEYLESS_FINGERPRINT = "keyless"
DEFAULT_COOLDOWN_SECONDS = 600.0
PENALTY_DECAY_SECONDS = 300.0
DEFAULT_SESSION_AFFINITY_TTL_SECONDS = 3600.0
KeySelectionStrategy = Literal["round-robin", "fill-first", "priority"]

# Single 429 hits get a short transient cooldown. A second (or later) 429 hit
# for the same (provider, model, key) within RATE_LIMIT_HIT_WINDOW_SECONDS is
# an "exhaustion event"; exhaustion events are tracked over the trailing
# RATE_LIMIT_EXHAUSTION_WINDOW_SECONDS and drive the escalation ladder below.
RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS = 90.0
RATE_LIMIT_ESCALATION_LADDER_SECONDS: tuple[float, float, float, float] = (
    120.0,
    600.0,
    3600.0,
    86400.0,
)
RATE_LIMIT_HIT_WINDOW_SECONDS = 3600.0
RATE_LIMIT_EXHAUSTION_WINDOW_SECONDS = 86400.0
DAILY_QUOTA_MIN_COOLDOWN_SECONDS = 60.0
COOLDOWN_CEILING_SECONDS = 86400.0

# 401/403 is an access problem (revoked key, model not on the plan), not a
# transient upstream hiccup, so it is not classified as a temporary failure.
# It still gets a cooldown: without one the same rejected target stays first
# in the fallback chain and is re-attempted on every single request.
ACCESS_DENIED_STATUS_CODES = frozenset({401, 403})

ObservedLimitAxis = Literal["rpm", "rpd", "tpm", "tpd"]
ObservedLimitSource = Literal["header", "error_body"]


@dataclass(frozen=True)
class ObservedLimitEntry:
    """One upstream-reported quota reading for a single (provider, model, key) axis."""

    limit: int | None
    remaining: int | None
    reset_at_monotonic: float | None
    source: ObservedLimitSource
    observed_at: str


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
    """In-memory upstream health/cooldown/quota tracker.

    All state lives in process memory (dicts/deques guarded by one lock) and
    is lost on restart; there is no persistence layer behind it.
    """

    def __init__(self, *, time_func: Any = time.time, monotonic_func: Any = time.monotonic) -> None:
        self._time_func = time_func
        self._monotonic_func = monotonic_func
        self._lock = Lock()
        self._states: dict[tuple[str, str, str], _RuntimeState] = {}
        self._request_windows: dict[tuple[str, str, str], deque[float]] = defaultdict(deque)
        self._token_windows: dict[tuple[str, str, str], deque[tuple[float, int]]] = defaultdict(deque)
        self._round_robin_indexes: dict[tuple[str, str, tuple[str, ...]], int] = {}
        self._session_affinity: dict[tuple[str, str, str, str], _AffinityBinding] = {}
        # All 429 hits in the trailing hour, used to detect exhaustion events.
        self._rate_limit_hit_windows: dict[tuple[str, str, str], deque[float]] = defaultdict(deque)
        # Exhaustion events (>=2nd 429 hit within an hour) in the trailing 24h,
        # used to pick the escalation-ladder rung.
        self._exhaustion_windows: dict[tuple[str, str, str], deque[float]] = defaultdict(deque)
        self._observed_limits: dict[tuple[str, str, str], dict[ObservedLimitAxis, ObservedLimitEntry]] = {}
        # (provider, model) pairs that answered a forced ``tool_choice`` with a
        # rejection. Deliberately not keyed by upstream key: refusing to be told
        # it must call a tool is a property of the model/endpoint, not of the
        # credential used to reach it.
        self._forced_tool_choice_unsupported: set[tuple[str, str]] = set()

    def record_forced_tool_choice_unsupported(self, provider: str, model: str) -> None:
        """Remember that this model rejects a forced ``tool_choice``."""
        with self._lock:
            self._forced_tool_choice_unsupported.add((provider, model))

    def forced_tool_choice_unsupported(self, provider: str, model: str) -> bool:
        with self._lock:
            return (provider, model) in self._forced_tool_choice_unsupported

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

    def cooldown_remaining_seconds(
        self, provider: str, model: str, key_fingerprint: str
    ) -> float | None:
        """Return the remaining cooldown for ``(provider, model, key_fingerprint)``.

        Read-only: unlike most lookups here, this never creates a
        ``_RuntimeState`` entry for a ref that hasn't recorded anything yet,
        so probing candidates for a status report cannot inflate ``_states``.
        Returns ``None`` when there is no recorded state or no active cooldown.
        """
        ref = (provider, model, key_fingerprint)
        now = self._monotonic_func()
        with self._lock:
            state = self._states.get(ref)
            if state is None:
                return None
            return self._cooldown_remaining(state, now)

    def has_zero_remaining_quota_block(
        self, provider: str, model: str, key_fingerprint: str
    ) -> bool:
        """Return whether ``(provider, model, key_fingerprint)`` is currently
        blocked by an observed ``remaining == 0`` quota reading.

        Read-only, like :meth:`cooldown_remaining_seconds`: never creates
        state for a ref that hasn't recorded an observed limit yet.
        """
        now = self._monotonic_func()
        with self._lock:
            return self._observed_zero_remaining_block_reason(
                (provider, model, key_fingerprint), now
            ) is not None

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
        retry_after_floor_seconds: float | None = None,
    ) -> bool:
        """Record an upstream failure and, if temporary, set a cooldown.

        ``retry_after`` is the legacy override: for temporary failures that
        are neither a 429 nor a daily-quota exhaustion it still replaces the
        default cooldown outright, unchanged from prior behavior.

        ``retry_after_floor_seconds`` only applies to the new 429/daily-quota
        branches below: it is a *floor* on the escalation-ladder cooldown
        (never shortens it) and an *override* of the daily-quota
        until-next-UTC-midnight calculation.

        A plain (non-429) temporary failure -- 5xx, timeout, connection error
        -- still sets a cooldown here so *future* requests skip this key, but
        it is not a rate-limit block: this method returns ``True`` only for a
        429/rate-limit failure (daily-quota exhaustion is a subset of that),
        signaling that the caller should switch upstream keys immediately for
        the *current* request. It returns ``False`` for any other outcome,
        including ``temporary=False``.

        A 401/403 arrives with ``temporary=False`` -- access denial is not a
        transient upstream fault -- yet it also gets the default cooldown, so
        a target the upstream refuses to serve stops being re-attempted on
        every request until the cooldown expires.
        """
        now = self._monotonic_func()
        status_code = getattr(error_detail, "status_code", None)
        access_denied = status_code in ACCESS_DENIED_STATUS_CODES
        health_status = "invalid" if access_denied else "error"
        rate_limited = False
        with self._lock:
            ref = (provider, model, key_fingerprint)
            state = self._state_for(ref)
            state.health_status = health_status
            state.last_checked_at = self._utc_now_iso()
            state.last_error = str(error_detail)[:500] if error_detail else None
            if temporary:
                error_type = classify_error(error_detail)
                rate_limited = error_type == "http_429"
                daily_quota = rate_limited and is_daily_quota_exhausted_error(error_detail)
                if daily_quota:
                    cooldown_seconds = min(
                        self._daily_quota_cooldown_seconds(retry_after_floor_seconds),
                        COOLDOWN_CEILING_SECONDS,
                    )
                elif rate_limited:
                    cooldown_seconds = min(
                        self._rate_limit_ladder_cooldown_seconds(ref, now, retry_after_floor_seconds),
                        COOLDOWN_CEILING_SECONDS,
                    )
                else:
                    cooldown_seconds = DEFAULT_COOLDOWN_SECONDS
                    if retry_after is not None:
                        try:
                            retry_after_seconds = float(retry_after)
                        except (TypeError, ValueError):
                            retry_after_seconds = 0.0
                        if retry_after_seconds > 0:
                            cooldown_seconds = retry_after_seconds
                state.cooldown_until = now + cooldown_seconds
                if rate_limited:
                    self._maybe_learn_limit_from_error(ref, error_detail)
            elif access_denied:
                state.cooldown_until = now + DEFAULT_COOLDOWN_SECONDS
            if apply_penalty and temporary:
                state.penalty_score = min(100.0, self._decayed_penalty(state, now) + 25.0)
                state.penalty_updated_at = now
        return rate_limited

    def _rate_limit_ladder_cooldown_seconds(
        self,
        ref: tuple[str, str, str],
        now: float,
        retry_after_floor_seconds: float | None,
    ) -> float:
        """Single 429 hit -> transient cooldown; repeat hits climb the ladder.

        A hit is an "exhaustion event" once it is the 2nd-or-later 429 for
        this (provider, model, key) within the trailing hour. Exhaustion
        events are tracked over a trailing 24h window and pick the ladder
        rung by their count. ``retry_after_floor_seconds`` is a floor, not an
        override, on whichever cooldown results.
        """
        hits = self._rate_limit_hit_windows[ref]
        hits.append(now)
        self._trim_numeric_window(hits, now, RATE_LIMIT_HIT_WINDOW_SECONDS)

        if len(hits) >= 2:
            exhaustion_events = self._exhaustion_windows[ref]
            exhaustion_events.append(now)
            self._trim_numeric_window(exhaustion_events, now, RATE_LIMIT_EXHAUSTION_WINDOW_SECONDS)
            ladder_index = min(
                len(exhaustion_events) - 1,
                len(RATE_LIMIT_ESCALATION_LADDER_SECONDS) - 1,
            )
            base_cooldown = RATE_LIMIT_ESCALATION_LADDER_SECONDS[ladder_index]
        else:
            base_cooldown = RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS

        floor = retry_after_floor_seconds if retry_after_floor_seconds and retry_after_floor_seconds > 0 else 0.0
        return max(base_cooldown, floor)

    def _daily_quota_cooldown_seconds(self, retry_after_floor_seconds: float | None) -> float:
        if retry_after_floor_seconds is not None and retry_after_floor_seconds > 0:
            return retry_after_floor_seconds
        return self._seconds_until_next_utc_midnight()

    def _seconds_until_next_utc_midnight(self) -> float:
        now_wall = datetime.fromtimestamp(self._time_func(), tz=timezone.utc)
        next_midnight = (now_wall + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        seconds = (next_midnight - now_wall).total_seconds()
        return max(DAILY_QUOTA_MIN_COOLDOWN_SECONDS, seconds)

    def _maybe_learn_limit_from_error(self, ref: tuple[str, str, str], error_detail: object) -> None:
        learned = parse_learned_limit_from_error(error_detail)
        if learned is None:
            return
        axis, limit_value = learned
        self._record_observed_limit_locked(
            ref,
            axis,
            limit=limit_value,
            remaining=None,
            reset_at_monotonic=None,
            source="error_body",
        )

    def record_observed_limit(
        self,
        provider: str,
        model: str,
        key_fingerprint: str,
        axis: ObservedLimitAxis,
        *,
        limit: int | None,
        remaining: int | None,
        reset_at_monotonic: float | None,
        source: ObservedLimitSource,
    ) -> None:
        """Record an upstream-observed quota reading for one axis.

        ``header`` readings are precise and always replace whatever was
        stored for that axis (including a prior header reading, which may
        raise the limit). ``error_body`` readings never replace a ``header``
        reading, and only replace a prior ``error_body`` reading when no
        limit was stored yet or the new limit is strictly lower (more
        conservative).
        """
        with self._lock:
            self._record_observed_limit_locked(
                (provider, model, key_fingerprint),
                axis,
                limit=limit,
                remaining=remaining,
                reset_at_monotonic=reset_at_monotonic,
                source=source,
            )

    def _record_observed_limit_locked(
        self,
        ref: tuple[str, str, str],
        axis: ObservedLimitAxis,
        *,
        limit: int | None,
        remaining: int | None,
        reset_at_monotonic: float | None,
        source: ObservedLimitSource,
    ) -> None:
        axis_map = self._observed_limits.setdefault(ref, {})
        existing = axis_map.get(axis)
        if source == "error_body" and existing is not None:
            if existing.source == "header":
                return
            if existing.limit is not None and (limit is None or limit >= existing.limit):
                return
        axis_map[axis] = ObservedLimitEntry(
            limit=limit,
            remaining=remaining,
            reset_at_monotonic=reset_at_monotonic,
            source=source,
            observed_at=self._utc_now_iso(),
        )

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
            refs = (
                set(self._states)
                | set(self._request_windows)
                | set(self._token_windows)
                | set(self._rate_limit_hit_windows)
                | set(self._exhaustion_windows)
                | set(self._observed_limits)
            )
            for provider, model, key_fingerprint in sorted(refs):
                ref = (provider, model, key_fingerprint)
                state = self._state_for(ref)
                exhaustion_events = self._exhaustion_windows.get(ref)
                if exhaustion_events:
                    self._trim_numeric_window(exhaustion_events, now, RATE_LIMIT_EXHAUSTION_WINDOW_SECONDS)
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
                        "cooldown_escalation_level": len(exhaustion_events) if exhaustion_events else 0,
                        "observed_limits": self._observed_limits_snapshot(ref, now),
                        # Read directly: the lock is already held here and is
                        # not reentrant, so the public getter would deadlock.
                        "forced_tool_choice_unsupported": (provider, model)
                        in self._forced_tool_choice_unsupported,
                    }
                )
            return rows

    def _observed_limits_snapshot(self, ref: tuple[str, str, str], now: float) -> dict[str, dict[str, Any]]:
        axis_map = self._observed_limits.get(ref)
        if not axis_map:
            return {}
        snapshot: dict[str, dict[str, Any]] = {}
        for axis, entry in axis_map.items():
            reset_in_seconds = None
            if entry.reset_at_monotonic is not None:
                reset_in_seconds = entry.reset_at_monotonic - now
            snapshot[axis] = {
                "limit": entry.limit,
                "remaining": entry.remaining,
                "reset_in_seconds": reset_in_seconds,
                "source": entry.source,
                "observed_at": entry.observed_at,
            }
        return snapshot

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

    def _effective_limit(
        self,
        ref: tuple[str, str, str],
        axis: ObservedLimitAxis,
        configured: int | None,
    ) -> int | None:
        """Tighter of the configured limit and this key's observed limit for ``axis``."""
        observed_entry = self._observed_limits.get(ref, {}).get(axis)
        observed_limit = observed_entry.limit if observed_entry is not None else None
        if configured is None:
            return observed_limit
        if observed_limit is None:
            return configured
        return min(configured, observed_limit)

    def _observed_zero_remaining_block_reason(
        self, ref: tuple[str, str, str], now: float
    ) -> str | None:
        """Block a key whose latest observed reading has zero remaining quota.

        Only blocks while the reported reset time is known and still in the
        future; an unknown or already-past reset does not block, since the
        provider may have already replenished the quota.
        """
        axis_map = self._observed_limits.get(ref)
        if not axis_map:
            return None
        for axis, entry in axis_map.items():
            if entry.remaining != 0:
                continue
            if entry.reset_at_monotonic is None or entry.reset_at_monotonic <= now:
                continue
            return f"quota {axis} exhausted ({entry.source})"
        return None

    def _quota_block_reason(
        self,
        ref: tuple[str, str, str],
        limits: UpstreamQuotaLimits | None,
        now: float,
    ) -> str | None:
        zero_remaining_reason = self._observed_zero_remaining_block_reason(ref, now)
        if zero_remaining_reason is not None:
            return zero_remaining_reason

        effective_rpm = self._effective_limit(ref, "rpm", limits.rpm if limits else None)
        effective_rpd = self._effective_limit(ref, "rpd", limits.rpd if limits else None)
        effective_tpm = self._effective_limit(ref, "tpm", limits.tpm if limits else None)
        effective_tpd = self._effective_limit(ref, "tpd", limits.tpd if limits else None)
        if (
            effective_rpm is None
            and effective_rpd is None
            and effective_tpm is None
            and effective_tpd is None
        ):
            return None

        requests = self._request_windows[ref]
        tokens = self._token_windows[ref]
        self._trim_numeric_window(requests, now, 86400.0)
        self._trim_token_window(tokens, now, 86400.0)
        if effective_rpm is not None and self._count_since(requests, now, 60.0) >= effective_rpm:
            return "rpm quota exhausted"
        if effective_rpd is not None and self._count_since(requests, now, 86400.0) >= effective_rpd:
            return "rpd quota exhausted"
        if effective_tpm is not None and self._tokens_since(tokens, now, 60.0) >= effective_tpm:
            return "tpm quota exhausted"
        if effective_tpd is not None and self._tokens_since(tokens, now, 86400.0) >= effective_tpd:
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
