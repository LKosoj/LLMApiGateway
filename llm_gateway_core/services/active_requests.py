import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

from fastapi import Request

if TYPE_CHECKING:
    from .runtime_config import AppServices


_PATH_OPERATION_SUFFIXES = (
    ("/chat/completions", "chat"),
    ("/responses", "chat"),
    ("/messages", "messages"),
    ("/embeddings", "embeddings"),
    ("/rerank", "rerank"),
    ("/decisions", "decisions"),
    ("/images/generations", "images_generation"),
    ("/images/edits", "images_edit"),
    ("/images", "images_generation"),
    ("/audio/speech", "audio_speech"),
    ("/audio/transcriptions", "audio_transcription"),
    ("/audio/diarize", "audio_diarization"),
    ("/pdf/convert", "pdf_conversion"),
    ("/pdf/jobs", "pdf_conversion"),
    ("/web/search", "web_search"),
    ("/web/read", "web_read"),
    ("/web/research", "web_research"),
    ("/web/deep-research", "web_deep_research"),
    ("/tavily/search", "web_search"),
    ("/tavily/extract", "web_read"),
    ("/jina/extract", "web_read"),
    ("/jina/search", "web_search"),
)


def _parse_utc_iso_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("since must be a valid ISO 8601 datetime string")
    cleaned = value.strip()
    if not cleaned:
        return None
    normalized = f"{cleaned[:-1]}+00:00" if cleaned.endswith(("Z", "z")) else cleaned
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("since must be a valid ISO 8601 datetime string") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def operation_from_path(path: str) -> str | None:
    normalized_path = path.rstrip("/")
    for prefix, operation in (("/v1/jina/extract/", "web_read"), ("/v1/jina/search/", "web_search")):
        if normalized_path.startswith(prefix):
            return operation
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

    def list_records(
        self,
        *,
        api_key_id: int | None = None,
        provider: str | None = None,
        model: str | None = None,
        operation: str | None = None,
        request_id: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        provider_needle = provider.strip().lower() if isinstance(provider, str) else ""
        model_needle = model.strip().lower() if isinstance(model, str) else ""
        operation_needle = operation.strip().lower() if isinstance(operation, str) else ""
        request_id_needle = request_id.strip().lower() if isinstance(request_id, str) else ""
        since_dt = _parse_utc_iso_datetime(since)

        now = time.monotonic()
        with self._lock:
            records = []
            for active_id, record in self._records.items():
                if api_key_id is not None and record.get("api_key_id") != api_key_id:
                    continue
                if provider_needle and provider_needle not in str(record.get("provider") or "").lower():
                    continue
                if model_needle:
                    model_val = str(record.get("model") or "").lower()
                    gw_model_val = str(record.get("gateway_model") or "").lower()
                    if model_needle not in model_val and model_needle not in gw_model_val:
                        continue
                if operation_needle and operation_needle not in str(record.get("operation") or "").lower():
                    continue
                if request_id_needle and request_id_needle not in str(record.get("request_id") or "").lower():
                    continue
                if since_dt is not None:
                    ts_raw = record.get("timestamp")
                    try:
                        record_dt = _parse_utc_iso_datetime(str(ts_raw)) if ts_raw else None
                    except ValueError:
                        record_dt = None
                    if record_dt is None or record_dt < since_dt:
                        continue
                public_record = {
                    key: value
                    for key, value in record.items()
                    if not key.startswith("_")
                }
                public_record["id"] = f"active:{active_id}"
                public_record["duration_ms"] = int(
                    (now - float(record["_started_monotonic"])) * 1000
                )
                records.append(public_record)

        records.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
        return records


def get_active_requests_registry(app: Any) -> ActiveRequestsRegistry:
    services = cast("AppServices", app.state.services)
    return services.active_requests_registry


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
    get_active_requests_registry(request.app).update(
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
