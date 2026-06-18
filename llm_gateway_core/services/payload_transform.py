from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

RESERVED_PAYLOAD_TRANSFORM_FIELDS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "messages",
        "model",
        "stream",
        "tool_choice",
        "tools",
        "x-api-key",
    }
)


def validate_payload_transform_object(value: Mapping[str, Any] | None, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"'{field_name}' must be a JSON object.")

    normalized_value: dict[str, Any] = {}
    for key, item_value in value.items():
        normalized_key = _validate_transform_field_name(key, field_name)
        normalized_value[normalized_key] = item_value
    return normalized_value


def validate_payload_transform_filters(value: list[str] | None, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"'{field_name}' must be a list of field names.")

    normalized_values: list[str] = []
    seen_values: set[str] = set()
    for item in value:
        normalized_item = _validate_transform_field_name(item, field_name)
        if normalized_item in seen_values:
            raise ValueError(f"'{field_name}' must not contain duplicate fields.")
        seen_values.add(normalized_item)
        normalized_values.append(normalized_item)
    return normalized_values


def apply_payload_transforms(payload: dict[str, Any], transforms: object | None) -> dict[str, Any]:
    transform_config = _transform_config_to_dict(transforms)
    if not transform_config:
        return payload

    defaults = validate_payload_transform_object(transform_config.get("defaults"), "payload_transforms.defaults")
    overrides = validate_payload_transform_object(transform_config.get("overrides"), "payload_transforms.overrides")
    filters = validate_payload_transform_filters(transform_config.get("filters"), "payload_transforms.filters")

    transformed_payload = copy.deepcopy(payload)
    for key, value in defaults.items():
        if key not in transformed_payload:
            transformed_payload[key] = copy.deepcopy(value)
    for key, value in overrides.items():
        transformed_payload[key] = copy.deepcopy(value)
    for key in filters:
        transformed_payload.pop(key, None)
    return transformed_payload


def _transform_config_to_dict(transforms: object | None) -> dict[str, Any]:
    if transforms is None:
        return {}
    if hasattr(transforms, "model_dump"):
        data = transforms.model_dump(exclude_none=True)
        return data if isinstance(data, dict) else {}
    if isinstance(transforms, Mapping):
        return dict(transforms)
    raise ValueError("'payload_transforms' must be a JSON object.")


def _validate_transform_field_name(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"'{field_name}' must contain only string field names.")

    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"'{field_name}' must not contain empty field names.")
    if "." in normalized_value or "[" in normalized_value or "]" in normalized_value:
        raise ValueError(f"'{field_name}' supports only top-level payload fields.")
    if normalized_value.lower() in RESERVED_PAYLOAD_TRANSFORM_FIELDS:
        raise ValueError(
            f"'{field_name}' must not change reserved or auth-sensitive field '{normalized_value}'."
        )
    return normalized_value
