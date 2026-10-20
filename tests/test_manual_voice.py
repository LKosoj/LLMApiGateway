import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from llm_gateway_core.api.v1 import audio, rules_editor
from llm_gateway_core.config.schemas import OperationRoute


def test_manual_voice_route_validation():
    route = OperationRoute(provider="routerai", model="fish-audio/drama-3", target_path="/audio/speech", voice_input="manual")
    assert route.model_dump(exclude_none=True)["voice_input"] == "manual"
    with pytest.raises(ValidationError):
        OperationRoute(provider="routerai", model="fish-audio/drama-3", target_path="/audio/speech", voice_input="unknown")


def test_manual_voice_catalog_does_not_contact_provider():
    route = OperationRoute(provider="routerai", model="fish-audio/drama-3", target_path="/audio/speech", voice_input="manual")
    dispatcher = Mock()
    dispatcher.lookup_route.return_value = route
    with patch.object(audio, "_get_operation_runtime", return_value=(dispatcher, None, None, {})):
        assert asyncio.run(audio._get_audio_voices_for_model(Mock(), "llmgateway/drama-3")) == []


def test_playground_exposes_manual_voice_models_from_first_route():
    config = SimpleNamespace(
        fallback_rules={},
        operation_rules={"audio_speech": {
            "manual": {"routes": [{"voice_input": "manual"}]},
            "catalog": {"routes": [{}]},
            "first-route-only": {"routes": [{}, {"voice_input": "manual"}]},
        }},
    )
    result = rules_editor._build_playground_models(config)
    assert result["audio_speech_manual_voice"] == ["manual"]
    assert result["audio_speech"] == ["catalog", "first-route-only", "manual"]
