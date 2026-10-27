import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.requests import Request

from llm_gateway_core.api.v1 import rules_editor


@pytest.mark.parametrize("endpoint", [rules_editor.get_playground_models, rules_editor.get_web_playground_models])
@pytest.mark.parametrize("allowed_models", [["allowed"], ["missing"], []])
def test_playground_filters_every_model_list(endpoint, allowed_models):
    operations = {
        section: {name: {"routes": [{"voice_input": "manual"}]} for name in ("allowed", "forbidden")}
        for section in (
            "web_search", "web_read", "web_research", "web_deep_research",
            "audio_speech", "audio_transcriptions", "audio_speaker_transcriptions",
            "audio_diarizations", "images_generations", "images_edits", "pdf_conversions",
        )
    }
    config = SimpleNamespace(
        operation_rules=operations,
        fallback_rules={"allowed": {}, "forbidden": {}},
        fusion_rules={"allowed": {}, "forbidden": {}},
        router_rules={"allowed": {}, "forbidden": {}},
        providers_config={"private-provider": {}},
    )
    request = Request({"type": "http", "state": {"api_key_record": SimpleNamespace(allowed_models=allowed_models)}})
    with patch.object(rules_editor, "_capture_control_runtime", return_value=(None, SimpleNamespace(config_loader=config))):
        response = asyncio.run(endpoint(request))
    models = json.loads(response.body)
    expected = sorted(set(allowed_models) & {"allowed", "forbidden"}) if allowed_models else ["allowed", "forbidden"]
    assert models.pop("providers") == []
    assert models
    for section, names in models.items():
        assert names == expected, section
