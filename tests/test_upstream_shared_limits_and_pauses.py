from concurrent.futures import ThreadPoolExecutor

import pytest

from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.services.upstream_routing_state import (
    UpstreamQuotaLimits,
    UpstreamRoutingState,
    fingerprint_api_key,
)


@pytest.mark.parametrize("axis", ["rpm", "rpd", "tpm", "tpd"])
def test_shared_key_limits_combine_models_but_isolate_keys_and_providers(axis):
    now = [1000.0]
    state = UpstreamRoutingState(monotonic_func=lambda: now[0])
    limits = UpstreamQuotaLimits(**{axis: 2})
    fingerprint = fingerprint_api_key("key-a")
    for model in ("model-a", "model-b"):
        assert state.record_attempt_start("provider", model, fingerprint, key_limits=limits) is None
        state.record_tokens("provider", model, fingerprint, 1)

    assert not state.select_key("provider", "model-c", ["key-a"], key_limits=limits).available
    assert state.select_key("provider", "model-c", ["key-b"], key_limits=limits).available
    assert state.select_key("other-provider", "model-c", ["key-a"], key_limits=limits).available
    assert state.select_key("provider", "model-c", ["key-a"]).available

    now[0] += 86401 if axis.endswith("d") else 61
    assert state.select_key("provider", "model-c", ["key-a"], key_limits=limits).available


def test_model_limits_and_shared_limits_both_apply():
    state = UpstreamRoutingState()
    fingerprint = fingerprint_api_key("key")
    state.record_attempt_start("provider", "model-a", fingerprint)
    assert not state.select_key(
        "provider", "model-a", ["key"], limits=UpstreamQuotaLimits(rpm=1),
        key_limits=UpstreamQuotaLimits(rpm=10),
    ).available
    assert state.select_key(
        "provider", "model-b", ["key"], limits=UpstreamQuotaLimits(rpm=1),
        key_limits=UpstreamQuotaLimits(rpm=10),
    ).available
    assert not state.select_key(
        "provider", "model-b", ["key"], key_limits=UpstreamQuotaLimits(rpm=1),
    ).available


def test_shared_limit_admission_is_atomic_across_models():
    state = UpstreamRoutingState()
    limits = UpstreamQuotaLimits(rpm=1)
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(
            lambda index: state.record_attempt_start("provider", f"model-{index}", "fp", key_limits=limits),
            range(32),
        ))
    assert outcomes.count(None) == 1
    assert outcomes.count("shared key rpm quota exhausted") == 31


def test_manual_pause_is_scoped_expires_and_survives_health_success():
    now = [1000.0]
    state = UpstreamRoutingState(monotonic_func=lambda: now[0])
    fingerprint = fingerprint_api_key("key-a")
    state.pause_key("provider", fingerprint, model="model-a", duration_seconds=60, reason="maintenance")
    state.mark_health("provider", "model-a", fingerprint, "healthy", None)
    state.record_success("provider", "model-a", fingerprint)
    assert not state.select_key("provider", "model-a", ["key-a"]).available
    assert state.select_key("provider", "model-b", ["key-a"]).available
    assert state.select_key("provider", "model-a", ["key-b"]).available
    assert state.select_key("other", "model-a", ["key-a"]).available
    assert state.record_attempt_start("provider", "model-a", fingerprint) == "upstream key is manually paused"
    assert state.get_manual_pauses()[0]["reason"] == "maintenance"
    now[0] += 60
    assert state.get_manual_pauses() == []
    assert state.select_key("provider", "model-a", ["key-a"]).available


def test_resuming_manual_pause_does_not_clear_automatic_cooldown():
    state = UpstreamRoutingState()
    fingerprint = fingerprint_api_key("key")
    state.record_failure("provider", "model", fingerprint, "failure", temporary=True, apply_penalty=False)
    state.pause_key("provider", fingerprint, model=None, duration_seconds=60, reason="maintenance")
    assert not state.select_key("provider", "other-model", ["key"]).available
    state.resume_key("provider", fingerprint, model=None)
    assert state.select_key("provider", "other-model", ["key"]).available
    assert not state.select_key("provider", "model", ["key"]).available


def test_whole_key_and_model_pauses_are_independent():
    state = UpstreamRoutingState()
    fingerprint = fingerprint_api_key("key")
    for model in (None, "model"):
        state.pause_key("provider", fingerprint, model=model, duration_seconds=60, reason="maintenance")
    state.resume_key("provider", fingerprint, model="model")
    assert not state.select_key("provider", "model", ["key"]).available
    state.resume_key("provider", fingerprint, model=None)
    assert state.select_key("provider", "model", ["key"]).available


@pytest.mark.parametrize(
    "limits",
    [
        {"rpm": 0},
        {"rpd": -1},
        {"tpm": True},
        {"tpd": 1.5},
        {"max_concurrency": 0},
        {"max_concurrency": -1},
        {"max_concurrency": True},
        {"max_concurrency": 1.5},
        {"max_concurrency": "1"},
        {"rpmm": 10},
    ],
)
def test_provider_shared_limits_reject_invalid_configuration(limits):
    with pytest.raises(ValueError):
        ProviderDetails(baseUrl="https://provider.example", apikey="key", upstream_limits=limits)


def test_provider_shared_limits_round_trip():
    provider = ProviderDetails(
        baseUrl="https://provider.example",
        apikey="key",
        upstream_limits={"rpm": 10, "tpm": 1000, "max_concurrency": 2},
    )
    assert provider.model_dump(exclude_none=True)["upstream_limits"] == {
        "rpm": 10,
        "tpm": 1000,
        "max_concurrency": 2,
    }
