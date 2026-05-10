from __future__ import annotations

import threading

_round_robin_lock = threading.Lock()
_round_robin_indexes: dict[tuple[str, ...], int] = {}


def split_api_keys(raw_api_key: str | None) -> list[str]:
    if not raw_api_key:
        return []
    return [key for key in (part.strip() for part in raw_api_key.split(",")) if key]


def has_api_key(raw_api_key: str | None) -> bool:
    return bool(split_api_keys(raw_api_key))


def select_next_api_key(raw_api_key: str | None) -> str | None:
    keys = split_api_keys(raw_api_key)
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]
    key_pool = tuple(keys)
    with _round_robin_lock:
        index = _round_robin_indexes.get(key_pool, 0)
        _round_robin_indexes[key_pool] = (index + 1) % len(keys)
    return keys[index]
