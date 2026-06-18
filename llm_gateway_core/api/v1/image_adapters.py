import base64
import copy
import functools
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException

from ...config.loader import (
    OperationRoute,
    REQUEST_FORMAT_NVIDIA_GENAI_JSON,
    REQUEST_FORMAT_OPENAI_IMAGES,
    REQUEST_FORMAT_OPENAI_IMAGES_MULTIPART,
    RESPONSE_FORMAT_NVIDIA_ARTIFACTS,
    RESPONSE_FORMAT_OPENAI_IMAGES,
)
from ...services.payload_transform import apply_payload_transforms
from ...services.request_handler import FORBIDDEN_CUSTOM_BODY_PARAM_KEYS

DEFAULT_IMAGE_MEDIA_TYPE = "image/png"
SUPPORTED_OPENAI_IMAGE_RESPONSE_FORMATS = frozenset({"", "b64_json"})
PROTECTED_IMAGE_REQUEST_FIELDS = frozenset({"prompt", "images", "mask"})
REQUEST_MAPPING_OMIT_CLIENT_FIELDS = "omit_client_fields"
MISSING = object()
PATH_SEGMENT_RE = re.compile(r"([^[.\]]+)|\[(\d+)\]")
DATA_URL_RE = re.compile(r"^data:([^;,]+)?(?:;[^,]*)?;base64,(.*)$", re.IGNORECASE | re.DOTALL)
SIZE_DIMENSIONS_RE = re.compile(r"^\s*(\d+)\s*x\s*(\d+)\s*$", re.IGNORECASE)


class ImageRouteConfigError(ValueError):
    """Raised when image route configuration is invalid."""


class ImageDownstreamResponseError(ValueError):
    """Raised when downstream image response cannot be normalized."""


@dataclass
class PreparedImageRequest:
    transport: Literal["json", "multipart"]
    json_payload: dict[str, Any] | None = None
    multipart_data: list[tuple[str, object]] | None = None
    multipart_files: list[tuple[str, tuple[str, bytes, str | None]]] | None = None


def _deepcopy_if_needed(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


@functools.lru_cache(maxsize=512)
def _iter_path_parts(path: str) -> tuple[str | int, ...]:
    path_parts: list[str | int] = []
    for match in PATH_SEGMENT_RE.finditer(path):
        key_name, index_value = match.groups()
        if key_name is not None:
            path_parts.append(key_name)
        else:
            path_parts.append(int(index_value))
    return tuple(path_parts)


def extract_path_value(source: Any, path: str) -> Any:
    if not isinstance(path, str) or not path.strip():
        return MISSING

    value = source
    for path_part in _iter_path_parts(path.strip()):
        if isinstance(path_part, int):
            if not isinstance(value, list) or path_part >= len(value):
                return MISSING
            value = value[path_part]
            continue

        if not isinstance(value, dict) or path_part not in value:
            return MISSING
        value = value[path_part]

    return value


def _root_field_name(path: str) -> str | None:
    path_parts = _iter_path_parts(path)
    if not path_parts:
        return None
    first_part = path_parts[0]
    return first_part if isinstance(first_part, str) else None


def _merge_route_custom_body_params(base_payload: dict[str, Any], route: OperationRoute) -> dict[str, Any]:
    merged_payload = copy.deepcopy(base_payload)

    for param_name, param_value in route.custom_body_params.items():
        normalized_param_name = param_name.lower()
        if normalized_param_name in FORBIDDEN_CUSTOM_BODY_PARAM_KEYS:
            continue
        if normalized_param_name == "model":
            continue
        if normalized_param_name in PROTECTED_IMAGE_REQUEST_FIELDS:
            continue
        merged_payload[param_name] = _deepcopy_if_needed(param_value)

    return apply_payload_transforms(merged_payload, route.payload_transforms)


def _configured_omitted_client_fields(route: OperationRoute) -> frozenset[str]:
    request_mapping = route.request_mapping or {}
    raw_fields = request_mapping.get(REQUEST_MAPPING_OMIT_CLIENT_FIELDS)
    if raw_fields is None:
        return frozenset()
    if not isinstance(raw_fields, list):
        raise ImageRouteConfigError("request_mapping.omit_client_fields must be a list of field names.")

    omitted_fields: set[str] = set()
    for field_name in raw_fields:
        if not isinstance(field_name, str) or not field_name.strip():
            raise ImageRouteConfigError("request_mapping.omit_client_fields must contain non-empty strings.")
        omitted_fields.add(field_name.strip().lower())
    return frozenset(omitted_fields)


def _copy_without_configured_client_fields(payload: dict[str, Any], route: OperationRoute) -> dict[str, Any]:
    downstream_payload = copy.deepcopy(payload)
    omitted_fields = _configured_omitted_client_fields(route)
    if not omitted_fields:
        return downstream_payload

    for field_name in list(downstream_payload.keys()):
        if field_name.lower() in omitted_fields:
            downstream_payload.pop(field_name)
    return downstream_payload


def build_openai_json_payload(payload: dict[str, Any], route: OperationRoute) -> dict[str, Any]:
    downstream_payload = _copy_without_configured_client_fields(payload, route)
    for key in list(downstream_payload.keys()):
        if key.lower() in FORBIDDEN_CUSTOM_BODY_PARAM_KEYS:
            downstream_payload.pop(key)

    downstream_payload["model"] = route.model
    return _merge_route_custom_body_params(downstream_payload, route)


def build_openai_multipart_payload(payload: dict[str, Any], route: OperationRoute) -> tuple[list[tuple[str, object]], list[tuple[str, tuple[str, bytes, str | None]]]]:
    payload = _copy_without_configured_client_fields(payload, route)
    data_payload: dict[str, object] = {}
    files_payload: list[tuple[str, tuple[str, bytes, str | None]]] = []

    for field_name, field_value in payload.items():
        if field_name == "images":
            for image_value in field_value:
                serialized_image = _serialize_openai_multipart_image(image_value)
                files_payload.append(("image[]", serialized_image))
            continue

        if field_name == "mask":
            files_payload.append(("mask", _serialize_openai_multipart_image(field_value)))
            continue

        if field_name.lower() in FORBIDDEN_CUSTOM_BODY_PARAM_KEYS:
            continue

        data_payload[field_name] = field_value

    data_payload["model"] = route.model
    data_payload = _merge_route_custom_body_params(data_payload, route)
    return list(data_payload.items()), files_payload


def _decode_image_base64(value: str) -> bytes:
    normalized_value = re.sub(r"\s+", "", value)
    try:
        return base64.b64decode(normalized_value, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Image source must be valid base64 data.") from exc


def _decode_image_source_string(value: str, *, default_content_type: str | None) -> tuple[bytes, str | None]:
    stripped_value = value.strip()
    if not stripped_value:
        raise HTTPException(status_code=400, detail="Image source must not be empty.")
    if stripped_value.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="HTTP image URLs cannot be converted to multipart; send base64/data_url image content.",
        )

    data_url_match = DATA_URL_RE.match(stripped_value)
    if data_url_match:
        data_url_content_type, encoded_value = data_url_match.groups()
        return _decode_image_base64(encoded_value), data_url_content_type or default_content_type

    return _decode_image_base64(stripped_value), default_content_type


def _serialize_openai_multipart_image(image_value: Any) -> tuple[str, bytes, str | None]:
    if isinstance(image_value, str):
        content, content_type = _decode_image_source_string(
            image_value,
            default_content_type=DEFAULT_IMAGE_MEDIA_TYPE,
        )
        return "upload.png", content, content_type

    if not isinstance(image_value, dict):
        raise HTTPException(status_code=400, detail="Multipart image payload must be an object or base64 string.")

    filename = image_value.get("filename") or "upload.png"
    if not isinstance(filename, str) or not filename.strip():
        filename = "upload.png"

    content_type = image_value.get("content_type")
    if content_type is not None and not isinstance(content_type, str):
        raise HTTPException(status_code=400, detail="Multipart image payload content_type must be a string.")

    content = image_value.get("bytes")
    if isinstance(content, bytes):
        return filename, content, content_type

    default_content_type = content_type or DEFAULT_IMAGE_MEDIA_TYPE
    for source_key in ("data_url", "b64_json", "base64", "image_url", "url"):
        source_value = image_value.get(source_key)
        if isinstance(source_value, str) and source_value.strip():
            decoded_content, decoded_content_type = _decode_image_source_string(
                source_value,
                default_content_type=default_content_type,
            )
            return filename, decoded_content, decoded_content_type

    raise HTTPException(
        status_code=400,
        detail="Multipart image payload must contain bytes, data_url, b64_json, base64, image_url, or url.",
    )


def _normalize_requested_response_format(request_payload: dict[str, Any]) -> str | None:
    requested_response_format = request_payload.get("response_format")
    if requested_response_format is None:
        return None

    if not isinstance(requested_response_format, str):
        raise HTTPException(status_code=400, detail="Invalid 'response_format' in request body; expected string")

    return requested_response_format.strip()


def _validate_openai_image_response_format(request_payload: dict[str, Any], response_mapping: dict[str, Any]) -> None:
    requested_response_format = _normalize_requested_response_format(request_payload)
    if requested_response_format in (None, *SUPPORTED_OPENAI_IMAGE_RESPONSE_FORMATS):
        return

    url_field = response_mapping.get("url_field")
    if requested_response_format == "url" and isinstance(url_field, str) and url_field.strip():
        return

    raise HTTPException(
        status_code=400,
        detail=(
            "Configured image adapter does not support the requested response_format. "
            "Supported values: b64_json."
        ),
    )


def _validate_request_mapping_config(route: OperationRoute, operation: str) -> dict[str, Any]:
    request_mapping = route.request_mapping or {}
    fields = request_mapping.get("fields", {})
    if not isinstance(fields, dict) or not fields:
        raise ImageRouteConfigError(
            f"Route '{route.model}' with request_format '{route.request_format}' must define request_mapping.fields."
        )

    prompt_is_mapped = False
    images_are_mapped = False
    for target_field, mapping_spec in fields.items():
        source_path = mapping_spec if isinstance(mapping_spec, str) else mapping_spec.get("from")
        if not isinstance(source_path, str):
            continue

        target_is_downstream_field = isinstance(target_field, str) and bool(target_field.strip())
        root_field = _root_field_name(source_path)
        if root_field == "prompt" and target_is_downstream_field:
            prompt_is_mapped = True
        if root_field == "images" and target_is_downstream_field:
            images_are_mapped = True

    if not prompt_is_mapped:
        raise ImageRouteConfigError(
            f"Route '{route.model}' with request_format '{route.request_format}' must map 'prompt'."
        )

    if operation == "images_edits" and not images_are_mapped:
        raise ImageRouteConfigError(
            f"Edit route '{route.model}' with request_format '{route.request_format}' must map at least one image source."
        )

    return request_mapping


def _validate_supported_client_fields(request_payload: dict[str, Any], request_mapping: dict[str, Any], operation: str) -> None:
    allowed_fields = {"model", "prompt", "response_format"}
    if operation == "images_edits":
        allowed_fields.add("images")

    configured_allowed_fields = request_mapping.get("allowed_client_fields", [])
    if isinstance(configured_allowed_fields, list):
        allowed_fields.update(field_name for field_name in configured_allowed_fields if isinstance(field_name, str))

    fields = request_mapping.get("fields", {})
    for mapping_spec in fields.values():
        source_path = mapping_spec if isinstance(mapping_spec, str) else mapping_spec.get("from")
        if not isinstance(source_path, str):
            continue
        root_field = _root_field_name(source_path)
        if root_field:
            allowed_fields.add(root_field)

    unsupported_fields = sorted(field_name for field_name in request_payload.keys() if field_name not in allowed_fields)
    if unsupported_fields:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported image request fields for the configured adapter: "
                f"{', '.join(unsupported_fields)}"
            ),
        )


def _apply_mapping_transform(value: Any, mapping_spec: dict[str, Any]) -> Any:
    transform_name = mapping_spec.get("transform", "copy")
    if transform_name == "copy":
        return _deepcopy_if_needed(value)

    if transform_name == "map":
        value_mapping = mapping_spec.get("mapping")
        if not isinstance(value_mapping, dict):
            raise ImageRouteConfigError("request_mapping transform 'map' requires an object in 'mapping'.")

        lookup_key = str(value)
        if lookup_key not in value_mapping:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported mapped value '{value}' for field '{mapping_spec.get('from')}'.",
            )
        return _deepcopy_if_needed(value_mapping[lookup_key])

    if transform_name == "to_data_url":
        default_content_type = mapping_spec.get("content_type")
        if default_content_type is not None and not isinstance(default_content_type, str):
            raise ImageRouteConfigError("request_mapping transform 'to_data_url' requires string 'content_type'.")
        return image_source_to_data_url(value, default_content_type=default_content_type)

    if transform_name == "to_data_url_list":
        default_content_type = mapping_spec.get("content_type")
        if default_content_type is not None and not isinstance(default_content_type, str):
            raise ImageRouteConfigError("request_mapping transform 'to_data_url_list' requires string 'content_type'.")
        if not isinstance(value, list) or not value:
            raise HTTPException(
                status_code=400,
                detail=f"Field '{mapping_spec.get('from')}' must contain a non-empty image list.",
            )
        return [
            image_source_to_data_url(item, default_content_type=default_content_type)
            for item in value
        ]

    if transform_name == "first_image_to_reference":
        image_value = _extract_single_image_source(value, mapping_spec)
        try:
            reference_value = image_source_to_reference_string(image_value)
        except HTTPException as exc:
            raise _override_validation_error(exc, mapping_spec) from exc
        return _validate_reference_string(reference_value, mapping_spec)

    if transform_name == "size_width":
        return _extract_dimension_from_size(value, component="width", field_name=mapping_spec.get("from"))

    if transform_name == "size_height":
        return _extract_dimension_from_size(value, component="height", field_name=mapping_spec.get("from"))

    raise ImageRouteConfigError(f"Unsupported image request mapping transform '{transform_name}'.")


def apply_request_mapping(request_payload: dict[str, Any], route: OperationRoute, operation: str) -> dict[str, Any]:
    request_mapping = _validate_request_mapping_config(route, operation)
    mapped_payload: dict[str, Any] = {}

    constants = request_mapping.get("constants", {})
    if constants is None:
        constants = {}
    if not isinstance(constants, dict):
        raise ImageRouteConfigError("request_mapping.constants must be a JSON object.")
    mapped_payload.update(_deepcopy_if_needed(constants))

    fields = request_mapping["fields"]
    for target_field, mapping_spec in fields.items():
        if isinstance(mapping_spec, str):
            mapping_spec = {"from": mapping_spec}
        if not isinstance(mapping_spec, dict):
            raise ImageRouteConfigError(f"request_mapping.fields['{target_field}'] must be a string or object.")

        source_path = mapping_spec.get("from")
        if not isinstance(source_path, str) or not source_path.strip():
            raise ImageRouteConfigError(f"request_mapping.fields['{target_field}'] must define a non-empty 'from'.")

        source_value = extract_path_value(request_payload, source_path)
        if source_value is MISSING:
            continue
        if not str(target_field).strip():
            continue

        mapped_payload[target_field] = _apply_mapping_transform(source_value, mapping_spec)

    return _merge_route_custom_body_params(mapped_payload, route)


def build_downstream_image_request(
    request_payload: dict[str, Any],
    route: OperationRoute,
    operation: str,
    *,
    source_transport: Literal["json", "multipart"],
) -> PreparedImageRequest:
    effective_request_format = route.request_format or REQUEST_FORMAT_OPENAI_IMAGES

    if effective_request_format == REQUEST_FORMAT_OPENAI_IMAGES:
        if operation == "images_edits" and source_transport == "multipart":
            multipart_data, multipart_files = build_openai_multipart_payload(request_payload, route)
            return PreparedImageRequest(
                transport="multipart",
                multipart_data=multipart_data,
                multipart_files=multipart_files,
            )
        return PreparedImageRequest(
            transport="json",
            json_payload=build_openai_json_payload(request_payload, route),
        )

    if effective_request_format == REQUEST_FORMAT_OPENAI_IMAGES_MULTIPART:
        if operation != "images_edits":
            raise ImageRouteConfigError(
                f"request_format '{REQUEST_FORMAT_OPENAI_IMAGES_MULTIPART}' is supported only for image edit routes."
            )
        multipart_data, multipart_files = build_openai_multipart_payload(request_payload, route)
        return PreparedImageRequest(
            transport="multipart",
            multipart_data=multipart_data,
            multipart_files=multipart_files,
        )

    if effective_request_format != REQUEST_FORMAT_NVIDIA_GENAI_JSON:
        raise ImageRouteConfigError(f"Unsupported image request_format '{effective_request_format}'.")

    normalized_request_payload = _copy_without_configured_client_fields(request_payload, route)
    _validate_openai_image_response_format(normalized_request_payload, route.response_mapping or {})
    request_mapping = _validate_request_mapping_config(route, operation)
    _validate_supported_client_fields(normalized_request_payload, request_mapping, operation)
    return PreparedImageRequest(
        transport="json",
        json_payload=apply_request_mapping(normalized_request_payload, route, operation),
    )


def image_source_to_data_url(value: Any, *, default_content_type: str | None = None) -> str:
    media_type = default_content_type or DEFAULT_IMAGE_MEDIA_TYPE

    if isinstance(value, str):
        stripped_value = value.strip()
        if not stripped_value:
            raise HTTPException(status_code=400, detail="Image source must not be empty.")
        if stripped_value.startswith("data:"):
            return stripped_value
        if stripped_value.startswith(("http://", "https://")):
            return stripped_value
        return f"data:{media_type};base64,{stripped_value}"

    if isinstance(value, dict):
        data_url = value.get("data_url")
        if isinstance(data_url, str) and data_url.strip():
            return data_url.strip()

        image_url = value.get("image_url") or value.get("url")
        if isinstance(image_url, str) and image_url.strip():
            return image_url.strip()

        base64_value = value.get("b64_json")
        if isinstance(base64_value, str) and base64_value.strip():
            content_type = value.get("content_type")
            if not isinstance(content_type, str) or not content_type.strip():
                content_type = media_type
            return f"data:{content_type};base64,{base64_value.strip()}"

        raw_bytes = value.get("bytes")
        if isinstance(raw_bytes, bytes):
            content_type = value.get("content_type")
            if not isinstance(content_type, str) or not content_type.strip():
                content_type = media_type
            encoded_bytes = base64.b64encode(raw_bytes).decode("ascii")
            return f"data:{content_type};base64,{encoded_bytes}"

    raise HTTPException(status_code=400, detail="Unsupported image source format in request body.")


def image_source_to_reference_string(value: Any) -> str:
    if isinstance(value, str):
        stripped_value = value.strip()
        if not stripped_value:
            raise HTTPException(status_code=400, detail="Image reference must not be empty.")
        return stripped_value

    if isinstance(value, dict):
        for key_name in ("data_url", "image_url", "url", "reference"):
            reference_value = value.get(key_name)
            if isinstance(reference_value, str) and reference_value.strip():
                return reference_value.strip()

    raise HTTPException(
        status_code=400,
        detail="Configured image adapter requires an image reference string and does not accept uploaded image bytes.",
    )


def _extract_single_image_source(value: Any, mapping_spec: dict[str, Any]) -> Any:
    if not isinstance(value, list) or not value:
        raise HTTPException(
            status_code=400,
            detail=f"Field '{mapping_spec.get('from')}' must contain a non-empty image list.",
        )

    allow_multiple = mapping_spec.get("allow_multiple", False)
    if not isinstance(allow_multiple, bool):
        raise ImageRouteConfigError("request_mapping transform 'first_image_to_reference' requires boolean 'allow_multiple'.")

    if len(value) > 1 and not allow_multiple:
        raise HTTPException(
            status_code=400,
            detail=f"Field '{mapping_spec.get('from')}' must contain exactly one image for this route.",
        )

    return value[0]


def _validate_reference_string(reference_value: str, mapping_spec: dict[str, Any]) -> str:
    pattern = mapping_spec.get("pattern")
    if pattern is None:
        return reference_value
    if not isinstance(pattern, str) or not pattern.strip():
        raise ImageRouteConfigError("request_mapping transform 'first_image_to_reference' requires non-empty string 'pattern'.")

    if re.fullmatch(pattern, reference_value):
        return reference_value

    validation_error = mapping_spec.get("validation_error")
    if validation_error is not None and not isinstance(validation_error, str):
        raise ImageRouteConfigError("request_mapping.fields validation_error must be a string when provided.")

    raise HTTPException(
        status_code=400,
        detail=validation_error or f"Field '{mapping_spec.get('from')}' does not match the configured reference format.",
    )


def _override_validation_error(exc: HTTPException, mapping_spec: dict[str, Any]) -> HTTPException:
    validation_error = mapping_spec.get("validation_error")
    if isinstance(validation_error, str) and validation_error.strip():
        return HTTPException(status_code=exc.status_code, detail=validation_error)
    return exc


def _extract_dimension_from_size(value: Any, *, component: Literal["width", "height"], field_name: Any) -> int:
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400,
            detail=f"Field '{field_name}' must be a string in WIDTHxHEIGHT format.",
        )

    match = SIZE_DIMENSIONS_RE.match(value)
    if match is None:
        raise HTTPException(
            status_code=400,
            detail=f"Field '{field_name}' must use WIDTHxHEIGHT format, for example 1024x1024.",
        )

    width_value, height_value = match.groups()
    return int(width_value if component == "width" else height_value)


def normalize_downstream_image_response(
    downstream_json: dict[str, Any],
    route: OperationRoute,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    effective_response_format = route.response_format or RESPONSE_FORMAT_OPENAI_IMAGES
    if effective_response_format == RESPONSE_FORMAT_OPENAI_IMAGES:
        return downstream_json

    if effective_response_format != RESPONSE_FORMAT_NVIDIA_ARTIFACTS:
        raise ImageRouteConfigError(f"Unsupported image response_format '{effective_response_format}'.")

    _validate_openai_image_response_format(request_payload, route.response_mapping or {})
    if not isinstance(downstream_json, dict):
        raise ImageDownstreamResponseError("Downstream image response must be a JSON object.")

    response_mapping = route.response_mapping or {}
    items_path = response_mapping.get("items_path") or response_mapping.get("artifacts_path") or "artifacts"
    if not isinstance(items_path, str) or not items_path.strip():
        raise ImageRouteConfigError("response_mapping.items_path must be a non-empty string.")

    items = extract_path_value(downstream_json, items_path)
    if items is MISSING:
        raise ImageDownstreamResponseError(f"Downstream image response is missing '{items_path}'.")
    if not isinstance(items, list):
        raise ImageDownstreamResponseError(f"Downstream image response field '{items_path}' must be a list.")

    base64_field = response_mapping.get("base64_field", "base64")
    url_field = response_mapping.get("url_field")
    revised_prompt_field = response_mapping.get("revised_prompt_field")
    if not isinstance(base64_field, str) or not base64_field.strip():
        raise ImageRouteConfigError("response_mapping.base64_field must be a non-empty string.")
    if url_field is not None and not isinstance(url_field, str):
        raise ImageRouteConfigError("response_mapping.url_field must be a string when provided.")
    if revised_prompt_field is not None and not isinstance(revised_prompt_field, str):
        raise ImageRouteConfigError("response_mapping.revised_prompt_field must be a string when provided.")

    normalized_images = []
    for item in items:
        if not isinstance(item, dict):
            raise ImageDownstreamResponseError("Each downstream image item must be a JSON object.")

        normalized_item: dict[str, Any] = {}
        base64_value = item.get(base64_field)
        if isinstance(base64_value, str) and base64_value:
            normalized_item["b64_json"] = base64_value
        elif isinstance(url_field, str):
            url_value = item.get(url_field)
            if isinstance(url_value, str) and url_value:
                normalized_item["url"] = url_value
            else:
                raise ImageDownstreamResponseError(
                    f"Downstream image item must contain either '{base64_field}' or '{url_field}'."
                )
        else:
            raise ImageDownstreamResponseError(f"Downstream image item must contain '{base64_field}'.")

        if isinstance(revised_prompt_field, str):
            revised_prompt = item.get(revised_prompt_field)
            if isinstance(revised_prompt, str) and revised_prompt:
                normalized_item["revised_prompt"] = revised_prompt

        normalized_images.append(normalized_item)

    normalized_response = {
        "created": int(time.time()),
        "data": normalized_images,
    }
    usage = downstream_json.get("usage")
    if isinstance(usage, dict):
        normalized_response["usage"] = usage
    return normalized_response
