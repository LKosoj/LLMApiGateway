import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from tests._async_compat import run_async
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from llm_gateway_core.api.v1 import decisions
from llm_gateway_core.api.v1.models import _add_gateway_operation_models
from llm_gateway_core.api.v1.rules_editor import (
    _build_structured_operation_rules_response,
    _serialize_structured_operation_rules,
)
from llm_gateway_core.config.loader import ConfigLoader, ProviderDetails
from llm_gateway_core.config.schemas import ModelsOperationConfig
from llm_gateway_core.middleware.accounting_admission import (
    ACCOUNTING_REQUEST_CONTEXT_STATE_KEY,
    AccountingRequestContext,
)
from llm_gateway_core.middleware.response_observation import ResponseObservationMiddleware
from llm_gateway_core.services.accounting import (
    AccountingReceipt,
    AccountingReservation,
    ProjectionStatus,
    SourceStatus,
    classify_billing_policy,
)
from llm_gateway_core.services.active_requests import operation_from_path
from llm_gateway_core.services.request_handler import OperationDispatcher
from llm_gateway_core.utils.usage_tracking import ModelCostRates, build_model_cost_rate_registry, calculate_model_cost_usd
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


PAYLOAD = {
    "model": "gateway/decision",
    "state": {"ticket": "Refund please"},
    "questions": {
        "refund": {"type": "noul", "instructions": "Requests a refund?"},
        "team": {"type": "choice", "instructions": {"task": "Choose team"},
                 "criteria": {"billing": "Billing", "tech": "Tech"}},
        "urgency": {"type": "score", "instructions": "How urgent?", "criteria": ["Low", "High"]},
    },
}
RULES = {"decisions": [{"gateway_model_name": "gateway/decision", "routes": [
    {"provider": "aitunnel", "model": "jev-1.13"},
    {"provider": "aitunnel", "model": "other", "target_path": "/decisions"},
]}]}


@pytest.mark.parametrize("state", ["text", {"ticket": "message"}, [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]])
def test_decisions_accepts_text_json_and_image_parts(state):
    payload = deepcopy(PAYLOAD)
    payload["state"] = state
    assert decisions.validate_decisions_request(payload) == "gateway/decision"


@pytest.mark.parametrize("change", [
    {"model": ""}, {"state": None}, {"questions": {}}, {"stream": True},
    {"questions": {"q": []}},
    {"questions": {"q": {"type": "text", "instructions": "x"}}},
    {"questions": {"q": {"type": "noul"}}},
    {"questions": {"q": {"type": "noul", "instructions": "x", "criteria": {"true": "yes"}}}},
    {"questions": {"q": {"type": "choice", "instructions": "x", "criteria": []}}},
    {"questions": {"q": {"type": "score", "instructions": "x", "criteria": {}}}},
])
def test_decisions_rejects_invalid_request(change):
    with pytest.raises(HTTPException) as exc:
        decisions.validate_decisions_request({**PAYLOAD, **change})
    assert exc.value.status_code == 400


def test_decisions_config_defaults_and_structured_roundtrip():
    assert operation_from_path("/v1/decisions") == "decisions"
    config = ModelsOperationConfig.model_validate(RULES)
    assert config.decisions[0].routes[0].target_path == "/decisions"
    serialized = json.loads(_serialize_structured_operation_rules(config))
    assert serialized["decisions"][0]["routes"][0]["model"] == "jev-1.13"
    loader = ConfigLoader()
    loader.operation_rules = loader._build_operation_config(serialized)
    response = _build_structured_operation_rules_response(loader)
    assert response["decisions"] == serialized["decisions"]
    catalog = {}
    _add_gateway_operation_models(catalog, loader.operation_rules)
    assert "decisions" in catalog["gateway/decision"]["capabilities"]
    assert ModelsOperationConfig.model_validate({}).decisions == []
    bad = deepcopy(RULES)
    bad["decisions"][0]["cost_calculator"] = {"unit": "operation", "rate_usd": 1}
    with pytest.raises(ValidationError):
        ModelsOperationConfig.model_validate(bad)


def _endpoint_case(monkeypatch, *, usage, rates=True, upstream_error=False, unknown=False, input_rate=2.0, upstream_errors=None, operation_rules=None):
    async def run():
        calls = []

        async def upstream(req):
            calls.append(json.loads(req.content))
            status = (upstream_errors or {}).get(calls[-1]["model"], 400 if upstream_error else 200)
            if status != 200:
                return httpx.Response(status, json={"error": "upstream failed"})
            return httpx.Response(200, json={"model": "jev-snapshot", "answers": {"refund": {"type": "noul", "noul": 0.9}}, "usage": usage})

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            loader = ConfigLoader()
            loader.providers_config = {
                route["provider"]: ProviderDetails(baseUrl="https://upstream/v1", apikey="test")
                for route in (operation_rules or RULES)["decisions"][0]["routes"]
            }
            loader.operation_rules = loader._build_operation_config(operation_rules or RULES)
            services = make_app_services(http_client=client)
            services.accounting_service.release.return_value = True
            snapshot = make_runtime_snapshot(
                config_loader=loader, http_client=client,
                cost_rate_registry={
                    (route["provider"], route["model"]): ModelCostRates(input_rate, 0.0)
                    for route in (operation_rules or RULES)["decisions"][0]["routes"]
                } if rates else {},
            )
            app = FastAPI()
            app.state.services = services
            context = AccountingRequestContext(
                method="POST", route_template="/v1/decisions",
                policy=classify_billing_policy("POST", "/v1/decisions"), request_id="decisions-test",
                reservation=AccountingReservation(reservation_id="reservation-test", request_id="decisions-test", api_key_id=7, reserved_usd=1.0),
            )
            request = Request({"type": "http", "method": "POST", "path": "/v1/decisions", "headers": [], "app": app,
                               "state": {"runtime_snapshot": snapshot, "llmgateway_request_id": "decisions-test", ACCOUNTING_REQUEST_CONTEXT_STATE_KEY: context}})
            payload = {**PAYLOAD, "model": "missing"} if unknown else PAYLOAD
            monkeypatch.setattr(decisions, "read_json_request_body", AsyncMock(return_value=payload))
            monkeypatch.setattr(decisions, "enforce_virtual_key_access", lambda req, model: None)
            monkeypatch.setattr(decisions, "resolve_provider_auth_headers", AsyncMock(return_value={"Authorization": "Bearer test"}))

            async def commit(reservation, event):
                return AccountingReceipt(source_status=SourceStatus.ACCEPTED, projection_status=ProjectionStatus.APPLIED,
                                         event_id=event.event_id, billing_fingerprint=event.billing_fingerprint, usage_row_id=1)

            services.accounting_service.commit.side_effect = commit
            try:
                response = await decisions.create_decisions(request)
            except HTTPException as exc:
                return exc.status_code, None, calls, services.accounting_service
            messages = []

            async def respond(scope, receive, send):
                await response(scope, receive, send)

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await ResponseObservationMiddleware(respond)(request.scope, receive, send)
            body = b"".join(message.get("body", b"") for message in messages)
            return messages[0]["status"], json.loads(body), calls, services.accounting_service
    return run_async(run())


@pytest.mark.parametrize("upstream_cost,expected", [(None, 0.002), (0, 0), (0.123, 0.123)])
def test_decisions_records_usage_and_preserves_answers(monkeypatch, upstream_cost, expected):
    usage = {"input_tokens": 1000, "output_tokens": 0, "cost_rub": 0.84}
    if upstream_cost is not None:
        usage["cost"] = upstream_cost
    status, body, calls, service = _endpoint_case(monkeypatch, usage=usage)
    assert status == 200
    assert body["answers"]["refund"]["noul"] == 0.9
    assert body["usage"]["cost_rub"] == 0.84
    assert calls == [{**PAYLOAD, "model": "jev-1.13"}]
    event = service.commit.call_args.args[1]
    assert event.usage.prompt_tokens == 1000
    assert event.usage.cost == pytest.approx(expected)
    assert event.route_template == "/v1/decisions"
    assert event.model == "jev-1.13"
    assert event.provider == "aitunnel"


def test_decisions_does_not_replace_usd_with_rubles_or_default_cost(monkeypatch):
    status, _, calls, service = _endpoint_case(monkeypatch, usage={"input_tokens": 1000, "cost_rub": 0.84}, rates=False)
    assert status == 503
    assert len(calls) == 2
    service.commit.assert_not_called()
    service.release.assert_awaited_once()


def test_decisions_upstream_error_does_not_try_next_route(monkeypatch):
    status, _, calls, service = _endpoint_case(monkeypatch, usage={}, upstream_error=True)
    assert status == 400
    assert len(calls) == 1
    service.release.assert_awaited_once()


def test_decisions_unknown_model_has_no_upstream_call(monkeypatch):
    status, _, calls, service = _endpoint_case(monkeypatch, usage={}, unknown=True)
    assert status == 404
    assert calls == []
    service.commit.assert_not_called()


def test_decisions_uses_upstream_usd_without_registry(monkeypatch):
    status, _, _, service = _endpoint_case(
        monkeypatch, usage={"input_tokens": 1000, "cost": 0.123, "cost_rub": 0.84}, rates=False,
    )
    assert status == 200
    assert service.commit.call_args.args[1].usage.cost == 0.123


def test_configured_jev_route_registry_and_usage_record(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    providers = {}
    for item in json.loads((root / "providers.json").read_text()):
        providers.update(item)
    rates = build_model_cost_rate_registry(providers)[("aitunnel", "jev-1.13")]
    assert rates.input_rate == 0.042
    assert rates.output_rate == 0
    assert calculate_model_cost_usd(prompt_tokens=1000000, completion_tokens=0, rates=rates) == 0.042
    config = ModelsOperationConfig.model_validate(json.loads((root / "models_operation_rules.json").read_text()))
    model = next(item for item in config.decisions if item.gateway_model_name == "llmgateway/decision")
    assert [route.model for route in model.routes] == ["jev-1.13-free", "jev-1.13", "pplx-decider-v1.1-27b"]
    assert (model.routes[0].provider, model.routes[0].model, model.routes[0].target_path) == (
        "opencode-zen", "jev-1.13-free", "https://opencode.ai/inference/systemone/v1/systemone",
    )
    zen_rates = build_model_cost_rate_registry(providers)[("opencode-zen", "jev-1.13-free")]
    assert zen_rates == rates
    dispatcher = OperationDispatcher({}, {}, None)
    assert dispatcher.build_target_url(model.routes[0], ProviderDetails(**providers["opencode-zen"])) == (
        "https://opencode.ai/inference/systemone/v1/systemone"
    )
    status, _, _, service = _endpoint_case(
        monkeypatch, usage={"input_tokens": 1000, "cost_rub": 0.0084}, input_rate=rates.input_rate,
    )
    assert status == 200
    assert service.commit.call_args.args[1].usage.cost == pytest.approx(0.000042)


def test_decisions_fallback_chain_records_only_successful_model(monkeypatch):
    rules = deepcopy(RULES)
    rules["decisions"][0]["routes"] = [
        {"provider": "aitunnel", "model": model, "target_path": "/decisions"}
        for model in ["jev-1.13", "pplx-decider-v1-27b"]
    ]
    status, _, calls, service = _endpoint_case(
        monkeypatch, usage={"input_tokens": 1000, "cost": 0.000042},
        upstream_errors={"jev-1.13": 503}, operation_rules=rules,
    )
    assert status == 200
    assert [call["model"] for call in calls] == ["jev-1.13", "pplx-decider-v1-27b"]
    service.commit.assert_awaited_once()
    event = service.commit.call_args.args[1]
    assert event.model == "pplx-decider-v1-27b"
    assert event.usage.cost == 0.000042


def test_decisions_exhausted_fallbacks_return_error_and_release(monkeypatch):
    status, _, calls, service = _endpoint_case(
        monkeypatch, usage={}, upstream_errors={"jev-1.13": 503, "other": 503},
    )
    assert status == 503
    assert [call["model"] for call in calls] == ["jev-1.13", "other"]
    service.commit.assert_not_called()
    service.release.assert_awaited_once()


@pytest.mark.parametrize("upstream_cost,expected", [(None, 0.000042), (0, 0), (0.123, 0.123)])
def test_zen_jev_usage_uses_configured_rates_and_preserves_upstream_cost(monkeypatch, upstream_cost, expected):
    root = Path(__file__).resolve().parents[1]
    rule = json.loads((root / "models_operation_rules.json").read_text())["decisions"][0]
    rule["gateway_model_name"] = PAYLOAD["model"]
    providers = {}
    for item in json.loads((root / "providers.json").read_text()):
        providers.update(item)
    rates = build_model_cost_rate_registry(providers)[("opencode-zen", "jev-1.13-free")]
    usage = {"input_tokens": 1000}
    if upstream_cost is not None:
        usage["cost"] = upstream_cost
    status, _, calls, service = _endpoint_case(
        monkeypatch, usage=usage, input_rate=rates.input_rate, operation_rules={"decisions": [rule]},
    )
    assert status == 200
    assert [call["model"] for call in calls] == ["jev-1.13-free"]
    event = service.commit.call_args.args[1]
    assert event.provider == "opencode-zen"
    assert event.model == "jev-1.13-free"
    assert event.usage.cost == pytest.approx(expected)
