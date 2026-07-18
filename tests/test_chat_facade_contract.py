import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI

from llm_gateway_core.api.v1 import chat, chat_dialects, chat_dispatch, chat_streaming
from tests._async_compat import run_async


def test_chat_facade_preserves_seven_openapi_operations() -> None:
    app = FastAPI()
    app.include_router(chat.router, prefix="/chat", tags=["Chat Completions V1"])
    app.include_router(
        chat.router,
        prefix="/v1/chat",
        tags=["Chat Completions V1 (Double Prefix Compat)"],
    )
    app.include_router(chat.responses_router, tags=["OpenAI Responses V1"])
    app.include_router(chat.anthropic_router, tags=["Anthropic Messages V1"])
    app.include_router(
        chat.anthropic_router,
        prefix="/v1",
        tags=["Anthropic Messages V1 (Double Prefix Compat)"],
    )

    operations = {
        (path, method): (
            operation["operationId"],
            operation["summary"],
            operation["tags"],
        )
        for path, path_item in app.openapi()["paths"].items()
        for method, operation in path_item.items()
    }
    assert operations == {
        ("/chat/completions", "post"): (
            "chat_completions_chat_completions_post",
            "Chat Completions",
            ["Chat Completions V1"],
        ),
        ("/v1/chat/completions", "post"): (
            "chat_completions_v1_chat_completions_post",
            "Chat Completions",
            ["Chat Completions V1 (Double Prefix Compat)"],
        ),
        ("/responses", "post"): (
            "openai_responses_responses_post",
            "Openai Responses",
            ["OpenAI Responses V1"],
        ),
        ("/messages", "post"): (
            "anthropic_messages_messages_post",
            "Anthropic Messages",
            ["Anthropic Messages V1"],
        ),
        ("/messages/count_tokens", "post"): (
            "anthropic_messages_count_tokens_messages_count_tokens_post",
            "Anthropic Messages Count Tokens",
            ["Anthropic Messages V1"],
        ),
        ("/v1/messages", "post"): (
            "anthropic_messages_v1_messages_post",
            "Anthropic Messages",
            ["Anthropic Messages V1 (Double Prefix Compat)"],
        ),
        ("/v1/messages/count_tokens", "post"): (
            "anthropic_messages_count_tokens_v1_messages_count_tokens_post",
            "Anthropic Messages Count Tokens",
            ["Anthropic Messages V1 (Double Prefix Compat)"],
        ),
    }


def test_chat_facade_reexports_moved_contract_symbols_by_identity() -> None:
    assert chat._anthropic_request_to_openai_payload is chat_dialects._anthropic_request_to_openai_payload
    assert chat._openai_response_to_responses is chat_dialects._openai_response_to_responses
    assert chat._openai_stream_to_anthropic is chat_streaming._openai_stream_to_anthropic
    assert chat._openai_stream_to_responses is chat_streaming._openai_stream_to_responses
    assert chat._compute_retry_sleep_seconds is chat_dispatch._compute_retry_sleep_seconds
    assert chat._require_model_rotation_db is chat_dispatch._require_model_rotation_db
    assert chat.chat_completions.__module__ == chat.__name__
    assert chat.openai_responses.__module__ == chat.__name__
    assert chat.anthropic_messages.__module__ == chat.__name__
    assert chat.anthropic_messages_count_tokens.__module__ == chat.__name__


def test_attempt_wrapper_forwards_current_facade_patch_seams(monkeypatch) -> None:
    impl = AsyncMock(return_value=(None, "expected", 4))
    make_request = AsyncMock()
    asyncio_hook = object()
    logging_hook = object()
    classify_hook = object()
    provider_auth_hook = object()
    monkeypatch.setattr(chat, "_attempt_model_fallback_rule_impl", impl)
    monkeypatch.setattr(chat, "make_llm_request", make_request)
    monkeypatch.setattr(chat, "asyncio", asyncio_hook)
    monkeypatch.setattr(chat, "logging", logging_hook)
    monkeypatch.setattr(chat, "classify_error", classify_hook)
    monkeypatch.setattr(chat, "resolve_provider_auth_material", provider_auth_hook)

    result = run_async(
        chat._attempt_model_fallback_rule(
            object(),
            object(),
            {},
            "gateway-model",
            {},
            {},
            False,
            proxy_http_clients={},
            upstream_routing_state=object(),
        )
    )

    assert result == (None, "expected", 4)
    kwargs = impl.await_args.kwargs
    assert kwargs["make_llm_request_hook"] is make_request
    assert kwargs["asyncio_hook"] is asyncio_hook
    assert kwargs["logging_hook"] is logging_hook
    assert kwargs["classify_error_hook"] is classify_hook
    assert kwargs["resolve_provider_auth_material_hook"] is provider_auth_hook


def test_dispatch_wrapper_forwards_current_fallback_and_logging_hooks(monkeypatch) -> None:
    impl = AsyncMock(return_value={"ok": True})
    fallback_hook = AsyncMock()
    logging_hook = object()
    monkeypatch.setattr(chat, "_dispatch_chat_request_impl", impl)
    monkeypatch.setattr(chat, "_attempt_model_fallback_rule", fallback_hook)
    monkeypatch.setattr(chat, "logging", logging_hook)

    result = run_async(chat._dispatch_chat_request(object(), {"model": "gateway-model"}))

    assert result == {"ok": True}
    kwargs = impl.await_args.kwargs
    assert kwargs["attempt_model_fallback_rule_hook"] is fallback_hook
    assert kwargs["logging_hook"] is logging_hook


def _relative_chat_imports(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module is not None
        and node.module.startswith("chat")
    }


def test_chat_owner_modules_form_a_one_way_dependency_graph() -> None:
    owner_modules = {
        "chat_dialects": set(),
        "chat_streaming": {"chat_dialects"},
        "chat_dispatch": {"chat_dialects", "chat_streaming"},
    }
    api_dir = Path(chat.__file__).parent

    for module_name, allowed_dependencies in owner_modules.items():
        source = (api_dir / f"{module_name}.py").read_text(encoding="utf-8")
        imported_modules = _relative_chat_imports(source)
        assert imported_modules <= allowed_dependencies | {
            "chat_accounting",
            "chat_model_behavior",
            "chat_sanitizers",
        }
        assert "chat" not in imported_modules


def test_chat_dependency_scan_detects_a_reverse_facade_import() -> None:
    assert _relative_chat_imports("from .chat import router\n") == {"chat"}


def test_stream_event_formatters_preserve_exact_sse_bytes() -> None:
    assert chat._format_anthropic_sse_event("message_stop", {"type": "message_stop"}) == (
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    assert chat._format_responses_sse_event({"type": "response.completed", "id": "r-1"}) == (
        b'data: {"id":"r-1","type":"response.completed"}\n\n'
    )
    assert inspect.signature(chat._attempt_model_fallback_rule).parameters["proxy_http_clients"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
