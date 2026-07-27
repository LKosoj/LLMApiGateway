from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request

from llm_gateway_core.config.config_store import (
    ConfigDocument,
    ConfigFile,
    ConfigSourceBundle,
)
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.services.config_updates import (
    ConfigRevision,
    ConfigUpdateCoordinator,
    ConfigUpdateError,
    ConfigUpdateErrorCode,
)
from llm_gateway_core.services.runtime_config import RuntimeSnapshot
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTTP_HELPER_SOURCE = (
    PROJECT_ROOT / "llm_gateway_core/api/v1/config_update_http.py"
)
_HELPER_MODULE_NAME = "llm_gateway_core.api.v1._config_update_http_test_leaf"
_HELPER_SPEC = importlib.util.spec_from_file_location(
    _HELPER_MODULE_NAME,
    HTTP_HELPER_SOURCE,
)
assert _HELPER_SPEC is not None and _HELPER_SPEC.loader is not None
config_update_http = importlib.util.module_from_spec(_HELPER_SPEC)
sys.modules[_HELPER_MODULE_NAME] = config_update_http
_HELPER_SPEC.loader.exec_module(config_update_http)
ConfigRequestInvalid = config_update_http.ConfigRequestInvalid
RawConfigBody = config_update_http.RawConfigBody
capture_config_update_runtime = config_update_http.capture_config_update_runtime
config_error_response = config_update_http.config_error_response
config_response_headers = config_update_http.config_response_headers
parse_config_if_match = config_update_http.parse_config_if_match
read_raw_config_body = config_update_http.read_raw_config_body
_MISSING = object()
_DIGEST = "a" * 64


def _request(
    *,
    headers: tuple[tuple[bytes, bytes], ...] = (),
    body: bytes = b"",
    services: object = _MISSING,
    snapshot: object = _MISSING,
) -> Request:
    app = FastAPI()
    if services is not _MISSING:
        app.state.services = services
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/config/model-rules",
        "raw_path": b"/v1/config/model-rules",
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "app": app,
        "state": {},
    }
    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(scope, receive)
    if snapshot is not _MISSING:
        request.state.runtime_snapshot = snapshot
    return request


def _snapshot(
    tmp_path: Path,
    *,
    generation: int = 7,
    missing: ConfigFile | None = None,
    empty: ConfigFile | None = None,
) -> tuple[ConfigSourceBundle, RuntimeSnapshot]:
    documents: dict[ConfigFile, ConfigDocument] = {}
    for config_file in ConfigFile:
        path = tmp_path / f"{config_file.value}.json"
        if config_file is missing:
            documents[config_file] = ConfigDocument.missing(config_file, path)
        else:
            content = (
                b""
                if config_file is empty
                else f'{{"config":"{config_file.value}"}}\r\n'.encode()
            )
            documents[config_file] = ConfigDocument.from_bytes(
                config_file,
                path,
                content,
            )
    bundle = ConfigSourceBundle(documents)
    loader = ConfigLoader.from_source_bundle(bundle)
    return bundle, make_runtime_snapshot(
        generation=generation,
        config_loader=loader,
    )


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (b'"model_rules:missing"', ConfigRevision(ConfigFile.MODEL_RULES, None)),
        (
            f'\t "model_rules:sha256:{_DIGEST}" \t'.encode(),
            ConfigRevision(ConfigFile.MODEL_RULES, _DIGEST),
        ),
    ],
)
def test_if_match_accepts_one_exact_strong_revision(
    raw_value: bytes,
    expected: ConfigRevision,
) -> None:
    request = _request(
        headers=((b"x-unrelated", b"kept"), (b"If-Match", raw_value)),
    )

    assert parse_config_if_match(request, ConfigFile.MODEL_RULES) == expected


def test_if_match_is_optional() -> None:
    assert parse_config_if_match(
        _request(headers=((b"x-unrelated", b"value"),)),
        ConfigFile.MODEL_RULES,
    ) is None


def test_if_match_accepts_inner_list_header_representation() -> None:
    request = _request()
    request.scope["headers"] = [
        [b"if-match", b'"model_rules:missing"'],
    ]

    assert parse_config_if_match(
        request,
        ConfigFile.MODEL_RULES,
    ) == ConfigRevision(ConfigFile.MODEL_RULES, None)


@pytest.mark.parametrize(
    "header",
    [
        None,
        (),
        (b"if-match",),
        (b"if-match", b'"model_rules:missing"', b"extra"),
        ("if-match", b'"model_rules:missing"'),
        (b"if-match", '"model_rules:missing"'),
    ],
)
def test_if_match_rejects_malformed_header_shapes_and_types(
    header: object,
) -> None:
    request = _request()
    request.scope["headers"] = [header]

    with pytest.raises(ConfigRequestInvalid) as raised:
        parse_config_if_match(request, ConfigFile.MODEL_RULES)

    assert str(raised.value) == "The configuration request is invalid."
    assert repr(header) not in str(raised.value)


@pytest.mark.parametrize(
    "raw_value",
    [
        b"*",
        b'W/"model_rules:missing"',
        b'"model_rules:missing", "model_rules:missing"',
        b'"providers:missing"',
        b'"MODEL_RULES:missing"',
        b'"model_rules:sha256:' + b"A" * 64 + b'"',
        b'"model_rules:sha256:' + b"a" * 63 + b'"',
        b'"model_rules:missing\\""',
        b'\n"model_rules:missing"',
        b'"model_rules:missing"\r',
        b"\xff",
    ],
)
def test_if_match_rejects_every_noncanonical_form(raw_value: bytes) -> None:
    with pytest.raises(ConfigRequestInvalid) as raised:
        parse_config_if_match(
            _request(headers=((b"if-match", raw_value),)),
            ConfigFile.MODEL_RULES,
        )

    assert str(raised.value) == "The configuration request is invalid."
    assert "model_rules" not in str(raised.value)


def test_if_match_rejects_identical_duplicate_header_fields() -> None:
    value = b'"model_rules:missing"'
    request = _request(
        headers=((b"if-match", value), (b"IF-MATCH", value)),
    )

    with pytest.raises(ConfigRequestInvalid):
        parse_config_if_match(request, ConfigFile.MODEL_RULES)


@pytest.mark.parametrize(
    ("body", "validation_text"),
    [
        (b"", ""),
        (b"{}", "{}"),
        (b"{\n}\n", "{\n}\n"),
        (b"{\r\n}\r\n", "{\r\n}\r\n"),
        (b"{\r}\r", "{\r}\r"),
        (b"\xef\xbb\xbf{\r\n}\r\n", "{\r\n}\r\n"),
    ],
)
def test_raw_body_preserves_exact_bytes_and_newlines(
    body: bytes,
    validation_text: str,
) -> None:
    result = run_async(read_raw_config_body(_request(body=body)))

    assert result.original_bytes == body
    assert result.validation_text == validation_text


@pytest.mark.parametrize(
    "body",
    [
        b"\xff",
        b"\xef\xbb\xbf\xef\xbb\xbf{}",
        b"{}\xef\xbb\xbf",
        b"\xef\xbb\xbf{}\xef\xbb\xbf",
    ],
)
def test_raw_body_rejects_invalid_utf8_and_repeated_or_embedded_bom(
    body: bytes,
) -> None:
    with pytest.raises(ConfigRequestInvalid) as raised:
        run_async(read_raw_config_body(_request(body=body)))

    assert str(raised.value) == "The configuration request is invalid."
    assert body.hex() not in str(raised.value)


@pytest.mark.parametrize(
    "headers",
    [
        (),
        ((b"content-type", b"application/octet-stream"),),
        ((b"content-type", b"application/json; charset=latin-1"),),
    ],
)
def test_raw_body_does_not_apply_content_type_or_size_policy(
    headers: tuple[tuple[bytes, bytes], ...],
) -> None:
    body = b'{"value":"ok"}\n'

    assert run_async(
        read_raw_config_body(_request(headers=headers, body=body))
    ).original_bytes == body


def test_raw_body_is_frozen_and_repr_safe() -> None:
    secret = "credential-payload-secret"
    result = run_async(read_raw_config_body(_request(body=secret.encode())))

    with pytest.raises(FrozenInstanceError):
        result.validation_text = "changed"  # type: ignore[misc]
    assert repr(result) == "RawConfigBody()"
    assert secret not in repr(result)


def test_capture_uses_only_typed_container_and_request_snapshot(
    tmp_path: Path,
) -> None:
    _, snapshot = _snapshot(tmp_path)
    coordinator = object.__new__(ConfigUpdateCoordinator)
    services = make_app_services(config_update_coordinator=coordinator)
    request = _request(services=services, snapshot=snapshot)
    request.app.state.config_update_coordinator = object()
    request.app.state.runtime_snapshot = object()
    request.state.config_update_coordinator = object()

    captured_services, captured_snapshot = capture_config_update_runtime(request)

    assert captured_services is services
    assert captured_snapshot is snapshot
    assert captured_services.config_update_coordinator is coordinator
    assert captured_services.http_client is services.http_client


def test_capture_fails_closed_without_exact_typed_dependencies(
    tmp_path: Path,
) -> None:
    _, snapshot = _snapshot(tmp_path)
    coordinator = object.__new__(ConfigUpdateCoordinator)
    services = make_app_services(config_update_coordinator=coordinator)
    foreign_coordinator_services = make_app_services(
        config_update_coordinator=object(),
    )
    requests = (
        _request(snapshot=snapshot),
        _request(services=object(), snapshot=snapshot),
        _request(services=services, snapshot=object()),
        _request(services=foreign_coordinator_services, snapshot=snapshot),
    )

    for request in requests:
        request.app.state.config_update_coordinator = coordinator
        request.app.state.runtime_snapshot = snapshot
        with pytest.raises(ConfigUpdateError) as raised:
            capture_config_update_runtime(request)
        assert raised.value.code is ConfigUpdateErrorCode.UPDATE_UNAVAILABLE


def test_response_headers_use_exact_existing_digest_without_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, snapshot = _snapshot(tmp_path)
    document = bundle[ConfigFile.MODEL_RULES]
    assert document.content is not None

    def fail_recapture(_bundle: ConfigSourceBundle) -> ConfigSourceBundle:
        raise AssertionError("unexpected I/O")

    monkeypatch.setattr(
        ConfigSourceBundle,
        "recapture",
        fail_recapture,
    )

    headers = config_response_headers(snapshot, ConfigFile.MODEL_RULES)

    assert headers == {
        "ETag": (
            '"model_rules:sha256:'
            f'{hashlib.sha256(document.content).hexdigest()}"'
        ),
        "X-Config-Generation": "7",
        "Cache-Control": "no-store",
    }


def test_response_headers_distinguish_missing_document_from_empty_bytes(
    tmp_path: Path,
) -> None:
    _, missing_snapshot = _snapshot(
        tmp_path / "missing",
        missing=ConfigFile.MODEL_RULES,
    )
    _, empty_snapshot = _snapshot(
        tmp_path / "empty",
        empty=ConfigFile.MODEL_RULES,
    )

    assert config_response_headers(missing_snapshot, ConfigFile.MODEL_RULES) == {
        "ETag": '"model_rules:missing"',
        "X-Config-Generation": "7",
        "Cache-Control": "no-store",
    }
    assert config_response_headers(empty_snapshot, ConfigFile.MODEL_RULES) == {
        "ETag": (
            '"model_rules:sha256:'
            f'{hashlib.sha256(b"").hexdigest()}"'
        ),
        "X-Config-Generation": "7",
        "Cache-Control": "no-store",
    }


@pytest.mark.parametrize("invalid_digest", [None, "A" * 64, "a" * 63])
def test_response_headers_fail_closed_for_invalid_existing_digest(
    tmp_path: Path,
    invalid_digest: str | None,
) -> None:
    bundle, snapshot = _snapshot(tmp_path)
    document = bundle[ConfigFile.MODEL_RULES]
    object.__setattr__(document, "digest", invalid_digest)

    with pytest.raises(ConfigUpdateError) as raised:
        config_response_headers(snapshot, ConfigFile.MODEL_RULES)

    assert raised.value.code is ConfigUpdateErrorCode.UPDATE_UNAVAILABLE


_ERROR_CASES = {
    ConfigUpdateErrorCode.VALIDATION_FAILED: (
        400,
        "Configuration validation failed.",
    ),
    ConfigUpdateErrorCode.GENERATION_STALE: (
        409,
        "The configuration generation is stale.",
    ),
    ConfigUpdateErrorCode.REVISION_CONFLICT: (
        409,
        "The configuration revision changed.",
    ),
    ConfigUpdateErrorCode.SOURCES_OUT_OF_SYNC: (
        409,
        "The loaded configuration no longer matches the files on disk.",
    ),
    ConfigUpdateErrorCode.GENERATION_BUSY: (
        409,
        "The previous runtime generation is still retiring.",
    ),
    ConfigUpdateErrorCode.COMMIT_FAILED: (
        500,
        "The configuration update could not be committed.",
    ),
    ConfigUpdateErrorCode.UPDATE_UNAVAILABLE: (
        503,
        "Configuration updates are unavailable.",
    ),
    ConfigUpdateErrorCode.UPDATE_BROKEN: (
        503,
        "Configuration update integrity is not proven.",
    ),
}


def test_error_mapping_covers_every_service_error_code() -> None:
    assert set(_ERROR_CASES) == set(ConfigUpdateErrorCode)
    assert set(config_update_http._ERROR_HTTP_STATUS) == set(ConfigUpdateErrorCode)
    assert set(config_update_http._ERROR_MESSAGES) == set(ConfigUpdateErrorCode)

    for code, (status_code, message) in _ERROR_CASES.items():
        response = config_error_response(ConfigUpdateError(code))
        expected = {"detail": {"code": code.value, "message": message}}
        assert response.status_code == status_code
        assert json.loads(response.body) == expected
        assert response.body == json.dumps(
            expected,
            separators=(",", ":"),
        ).encode()


def test_request_invalid_mapping_has_exact_frozen_body() -> None:
    response = config_error_response(ConfigRequestInvalid())

    assert response.status_code == 400
    assert response.body == (
        b'{"detail":{"code":"config_request_invalid",'
        b'"message":"The configuration request is invalid."}}'
    )


def test_error_mapping_ignores_secret_subclass_text_and_rejects_unknown_type() -> None:
    secret = "credential-path-and-payload-secret"

    class SecretConfigUpdateError(ConfigUpdateError):
        def __str__(self) -> str:
            return secret

    response = config_error_response(
        SecretConfigUpdateError(ConfigUpdateErrorCode.COMMIT_FAILED)
    )

    assert secret.encode() not in response.body
    assert json.loads(response.body) == {
        "detail": {
            "code": "config_commit_failed",
            "message": "The configuration update could not be committed.",
        }
    }
    with pytest.raises(TypeError):
        config_error_response(ValueError(secret))  # type: ignore[arg-type]


def test_http_helper_has_no_router_or_writer_specific_imports() -> None:
    source = HTTP_HELPER_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(HTTP_HELPER_SOURCE))
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    route_decorators = {
        decorator.func.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr in {"get", "post", "put", "patch", "delete"}
    }

    assert "APIRouter" not in source
    assert route_decorators == set()
    assert not hasattr(config_update_http, "router")
    assert not any(
        module.endswith(("rules_editor", "admin_pricing"))
        for module in imported_modules
    )
    assert config_update_http.__all__ == (
        "ConfigRequestInvalid",
        "RawConfigBody",
        "capture_config_update_runtime",
        "config_error_response",
        "config_response_headers",
        "parse_config_if_match",
        "read_raw_config_body",
    )
