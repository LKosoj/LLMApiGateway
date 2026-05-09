"""Utilities for sanitizing text data before JSON serialization."""

import re

# Matches any Unicode surrogate character (U+D800 to U+DFFF)
_SURROGATE_RE = re.compile(r'[\ud800-\udfff]')


def remove_surrogates(text: str) -> str:
    """Normalize valid surrogate pairs and replace lone surrogates."""
    if not _SURROGATE_RE.search(text):
        return text

    return text.encode("utf-16-le", "surrogatepass").decode(
        "utf-16-le",
        "replace",
    )


def sanitize_payload(obj: object) -> object:
    """Recursively strip surrogate characters from all strings in *obj*.

    Returns a new object (dict / list / str) with valid surrogate pairs
    normalized into real Unicode code points and lone surrogates replaced by
    U+FFFD so that downstream ``json.dumps`` / UTF-8 encoding never fails
    with ``surrogates not allowed``.

    Non-string leaves (int, float, bool, None) are returned unchanged.
    """
    if isinstance(obj, str):
        return remove_surrogates(obj)
    if isinstance(obj, dict):
        return {k: sanitize_payload(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_payload(item) for item in obj]
    return obj
