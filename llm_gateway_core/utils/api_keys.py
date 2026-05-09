from __future__ import annotations

import random


def split_api_keys(raw_api_key: str | None) -> list[str]:
    if not raw_api_key:
        return []
    return [key for key in (part.strip() for part in raw_api_key.split(",")) if key]


def has_api_key(raw_api_key: str | None) -> bool:
    return bool(split_api_keys(raw_api_key))


def select_random_api_key(raw_api_key: str | None) -> str | None:
    keys = split_api_keys(raw_api_key)
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]
    return random.choice(keys)
