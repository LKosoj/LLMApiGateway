from pathlib import Path

from llm_gateway_core.services.accounting import classify_billing_policy


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_accounting_policy_covers_audio_and_pdf_routes_exactly() -> None:
    for method, route_template in (
        ("POST", "/v1/audio/speech"),
        ("POST", "/v1/pdf/convert"),
        ("POST", "/v1/pdf/jobs"),
        ("GET", "/v1/pdf/jobs/{job_id}"),
    ):
        assert classify_billing_policy(method, route_template) is not None

    assert classify_billing_policy("GET", "/v1/pdf/jobs") is None
    assert classify_billing_policy("POST", "/shadow/v1/pdf/convert") is None


def test_legacy_accounting_mutation_symbols_are_removed() -> None:
    sources = {
        "auth": PROJECT_ROOT / "llm_gateway_core/middleware/auth.py",
        "chat": PROJECT_ROOT / "llm_gateway_core/middleware/chat_logging.py",
        "operation": PROJECT_ROOT / "llm_gateway_core/api/v1/operation_proxy.py",
    }
    forbidden = {
        "auth": (
            "USD_BUDGET_RESERVATION_SUFFIXES",
            "usd_budget_reserved",
            "_reserve_usd_budget_if_needed",
        ),
        "chat": (
            "def record_tokens_usage(",
            "_usd_budget_reserved",
            "_key_tpm_limit",
        ),
        "operation": (
            "def record_operation_usage(",
            "_commit_usd_budget_reservation",
        ),
    }

    for owner, source_path in sources.items():
        source = source_path.read_text(encoding="utf-8")
        assert all(symbol not in source for symbol in forbidden[owner])
