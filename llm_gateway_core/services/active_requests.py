import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import Request


ACTIVE_REQUESTS_STATE_KEY = "active_requests_registry"

_PATH_OPERATION_SUFFIXES = (
    ("/chat/completions", "chat"),
    ("/responses", "chat"),
    ("/messages", "messages"),
    ("/embeddings", "embeddings"),
    ("/rerank", "rerank"),
    ("/images/generations", "images_generation"),
    ("/images/edits", "images_edit"),
    ("/images", "images_generation"),
    ("/audio/speech", "audio_speech"),
    ("/audio/transcriptions", "audio_transcription"),
    ("/pdf/convert", "pdf_conversion"),
    ("/pdf/jobs", "pdf_conversion"),
    ("/web/search", "web_search"),
    ("/web/read", "web_read"),
    ("/web/research", "web_research"),
    ("/web/deep-research", "web_deep_research"),
    ("/tavily/search", "web_search"),
    ("/tavily/extract", "web_read"),
)


def operation_from_path(path: str) -> str | None:
    normalized_path = path.rstrip("/")
    for suffix, operation in _PATH_OPERATION_SUFFIXES:
        if normalized_path.endswith(suffix):
            return operation
    return None


class ActiveRequestsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}

    def start(
        self,
        *,
        request_id: str,
        path: str,
        api_key_id: int | None,
        gateway_model: str | None = None,
        operation: str | None = None,
        x_title: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._records[request_id] = {
                "request_id": request_id,
                "timestamp": now.isoformat(),
                "duration_ms": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cost": 0.0,
                "gateway_model": gateway_model,
                "operation": operation or operation_from_path(path),
                "model": None,
                "provider": None,
                "is_estimated": False,
                "usage_source": None,
                "cost_saved": 0.0,
                "api_key_id": api_key_id,
                "upstream_key_fingerprint": None,
                "x_title": x_title,
                "status": "running",
                "_started_monotonic": time.monotonic(),
            }

    def update(
        self,
        request_id: str | None,
        *,
        gateway_model: str | None = None,
        operation: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        upstream_key_fingerprint: str | None = None,
        prompt_tokens: int | None = None,
        total_tokens: int | None = None,
        is_estimated: bool | None = None,
    ) -> None:
        if not request_id:
            return
        with self._lock:
            record = self._records.get(request_id)
            if record is None:
                return
            updates = {
                "gateway_model": gateway_model,
                "operation": operation,
                "provider": provider,
                "model": model,
                "upstream_key_fingerprint": upstream_key_fingerprint,
                "prompt_tokens": prompt_tokens,
                "total_tokens": total_tokens,
                "is_estimated": is_estimated,
            }
            for key, value in updates.items():
                if value is not None:
                    record[key] = value

    def finish(self, request_id: str | None) -> None:
        if not request_id:
            return
        with self._lock:
            self._records.pop(request_id, None)

    def list_records(self, *, api_key_id: int | None = None) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            records = []
            for request_id, record in self._records.items():
                if api_key_id is not None and record.get("api_key_id") != api_key_id:
                    continue
                public_record = {
                    key: value
                    for key, value in record.items()
                    if not key.startswith("_")
                }
                public_record["id"] = f"active:{request_id}"
                public_record["duration_ms"] = int(
                    (now - float(record["_started_monotonic"])) * 1000
                )
                records.append(public_record)

        records.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
        return records


def get_active_requests_registry(app: Any) -> ActiveRequestsRegistry:
    registry = getattr(app.state, ACTIVE_REQUESTS_STATE_KEY, None)
    if not isinstance(registry, ActiveRequestsRegistry):
        registry = ActiveRequestsRegistry()
        setattr(app.state, ACTIVE_REQUESTS_STATE_KEY, registry)
    return registry


def active_request_id(request: Request) -> str | None:
    request_id = getattr(request.state, "llmgateway_active_request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id

    request_id = getattr(request.state, "llmgateway_request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id
    return None


def update_active_request(
    request: Request,
    *,
    gateway_model: str | None = None,
    operation: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    upstream_key_fingerprint: str | None = None,
    prompt_tokens: int | None = None,
    total_tokens: int | None = None,
    is_estimated: bool | None = None,
) -> None:
    request_id = active_request_id(request)
    if not request_id:
        return
    try:
        app = request.app
    except KeyError:
        return
    get_active_requests_registry(app).update(
        request_id,
        gateway_model=gateway_model,
        operation=operation,
        provider=provider,
        model=model,
        upstream_key_fingerprint=upstream_key_fingerprint,
        prompt_tokens=prompt_tokens,
        total_tokens=total_tokens,
        is_estimated=is_estimated,
    )


def update_active_request_from_state(request: Request) -> None:
    update_active_request(
        request,
        gateway_model=getattr(request.state, "llmgateway_gateway_model", None),
        operation=getattr(request.state, "llmgateway_operation", None),
        provider=getattr(request.state, "llmgateway_provider", None),
        model=getattr(request.state, "llmgateway_provider_model", None),
        upstream_key_fingerprint=getattr(
            request.state, "llmgateway_upstream_key_fingerprint", None
        ),
    )
