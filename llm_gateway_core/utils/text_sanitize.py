"""Utilities for sanitizing text data before JSON serialization."""

import re

# Matches any Unicode surrogate character (U+D800 to U+DFFF)
_SURROGATE_RE = re.compile(r'[\ud800-\udfff]')


def remove_surrogates(text: str) -> str:
    """Normalize valid surrogate pairs and replace lone surrogates."""
    if text.isascii() or not _SURROGATE_RE.search(text):
        return text

    return text.encode("utf-16-le", "surrogatepass").decode(
        "utf-16-le",
        "replace",
    )


def sanitize_payload(obj: object) -> object:
    """Recursively strip surrogate characters from all strings in *obj*.

    Uses copy-on-write for dicts and lists: if no nested value changes, the
    original object is returned without allocations. Otherwise returns a new
    object with valid surrogate pairs normalized into real Unicode code points
    and lone surrogates replaced by U+FFFD so that downstream ``json.dumps`` /
    UTF-8 encoding never fails with ``surrogates not allowed``.

    Non-string leaves (int, float, bool, None) are returned unchanged.
    """
    if isinstance(obj, str):
        return remove_surrogates(obj)
    if isinstance(obj, dict):
        new_dict: dict | None = None
        for k, v in obj.items():
            new_v = sanitize_payload(v)
            if new_v is not v:
                if new_dict is None:
                    new_dict = dict(obj)
                new_dict[k] = new_v
        return new_dict if new_dict is not None else obj
    if isinstance(obj, list):
        new_list: list | None = None
        for idx, item in enumerate(obj):
            new_item = sanitize_payload(item)
            if new_item is not item:
                if new_list is None:
                    new_list = list(obj)
                new_list[idx] = new_item
        return new_list if new_list is not None else obj
    return obj
