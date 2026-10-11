import html
import json
import re
from pathlib import Path

from llm_gateway_core.api.v1.decisions import validate_decisions_request


ROOT = Path(__file__).resolve().parents[1]


def _decisions_section() -> str:
    document = (ROOT / "static/gateway-docs.html").read_text()
    return re.search(r'<section id="decisions".*?</section>', document, re.S).group(0)


def _curl_payloads() -> list[dict]:
    section = _decisions_section().replace('<span data-docs-api-base></span>', 'http://example.test/v1')
    section = re.sub(r'<[^>]+>', '', section)
    section = html.unescape(section)
    return [json.loads(raw) for raw in re.findall(r"-d '([^']+)'", section, re.S)]


def test_decisions_docs_curl_examples_match_request_contract():
    payloads = _curl_payloads()

    assert len(payloads) == 3
    assert {next(iter(payload["questions"].values()))["type"] for payload in payloads} == {
        "noul",
        "choice",
        "score",
    }
    for payload in payloads:
        assert validate_decisions_request(payload) == "llmgateway/jev-1.13"


def test_decisions_docs_locales_and_catalog_label_match():
    en = json.loads((ROOT / "static/locales/en/docs.json").read_text())
    ru = json.loads((ROOT / "static/locales/ru/docs.json").read_text())

    def keys(value):
        if isinstance(value, dict):
            return {name: keys(item) for name, item in value.items()}
        if isinstance(value, list):
            return [keys(item) for item in value]
        return type(value).__name__

    assert keys(en["sections"]["decisions"]) == keys(ru["sections"]["decisions"])
    assert en["catalog"]["groups"]["decisions"]["title"] == "Decisions"
    assert ru["catalog"]["groups"]["decisions"]["title"] == "Decisions"


def test_decisions_docs_response_uses_provider_answer_fields():
    section = _decisions_section()
    blocks = re.findall(r'<pre[^>]*><code>(.*?)</code></pre>', section, re.S)
    response = json.loads(html.unescape(blocks[-1]))
    answers = response["answers"]

    assert 0 <= answers["refund_allowed"]["noul"] <= 1
    assert answers["risk_level"]["choice"] in answers["risk_level"]["probabilities"]
    assert "score" in answers["service_score"]
    assert "legend" in answers["service_score"]
    assert all("value" not in answer for answer in answers.values())
