"""Pure relevance helpers for web search results (port of dsh-free-search).

Ports ``queryOverlapTokens`` / ``looksRelevant`` from ``lib/engines/shared.js`` and
``stripTrackingParams`` from ``lib/url_norm.js``. No I/O, stdlib only.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Known tracking parameters only (exact, case-insensitive). Resource-identifying
# keys such as from/scene/sessionid must never be stripped: they may point to a
# different page and stripping them would merge distinct results.
TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "gclsrc",
        "dclid",
        "yclid",
        "fbclid",
        "igshid",
        "ttclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "_gl",
        "spm",
        "spm_id_from",
        "vd_source",
        "share_medium",
        "share_source",
        "share_plat",
    }
)
_TRACKING_PREFIXES = ("utm_",)
# Schemes for which JS `new URL()` requires a host; anything else with a scheme parses.
_HOST_REQUIRED_SCHEMES = frozenset({"http", "https", "ftp", "ws", "wss"})

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_DIGITS_ONLY_RE = re.compile(r"[0-9]+")


def _is_tracking_param(key: str) -> bool:
    lower = key.lower()
    return lower in TRACKING_PARAMS or lower.startswith(_TRACKING_PREFIXES)


def strip_tracking_params(url: str) -> str:
    """Remove known tracking parameters, keep everything else; never raise.

    Invalid URLs and URLs without tracking parameters are returned verbatim.
    Malformed URLs that WHATWG ``new URL()`` would normalize (collapsed slashes
    like ``https:/host`` or ``https:///host``, etc.) are returned as is here,
    without stripping trackers. Serialization is consistent within the gateway,
    but byte-for-byte parity with JS ``URL.toString()`` (percent-encoding of
    ``~``/``*``, host casing, trailing slash) is not guaranteed.
    """
    if not url:
        return url
    try:
        parsed = urlsplit(url)
        if not parsed.scheme:
            return url
        if parsed.scheme in _HOST_REQUIRED_SCHEMES and not parsed.netloc:
            return url
        # Accessing .port validates it the way `new URL()` does (raises on garbage).
        _ = parsed.port
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        kept = [(key, value) for key, value in pairs if not _is_tracking_param(key)]
        if len(kept) == len(pairs):
            return url
        return urlunsplit(parsed._replace(query=urlencode(kept)))
    except Exception:
        return url


def query_overlap_tokens(query: str) -> list[str]:
    """CJK bigrams plus lowercase ASCII words of length >= 2, deduped in order."""
    tokens: dict[str, None] = {}
    for run in _CJK_RUN_RE.findall(query):
        # Single characters (年/月/日) appear on almost any page; keep bigrams only.
        if len(run) == 2:
            tokens[run] = None
        for i in range(len(run) - 1):
            tokens[run[i : i + 2]] = None
    for word in _ASCII_SPLIT_RE.split(query.lower()):
        if len(word) >= 2:
            tokens[word] = None
    return list(tokens)


def _text(value: Any) -> str:
    # JS `value ?? ""`: only null/undefined become "", other values stringify.
    return "" if value is None else str(value)


def _usable_tokens(query: str) -> tuple[list[str], list[str]]:
    """Return (usable, title_tokens) exactly as looksRelevant derives them."""
    tokens = query_overlap_tokens(query)
    strong = [token for token in tokens if not _DIGITS_ONLY_RE.fullmatch(token)]
    usable = strong if strong else tokens
    # Short ASCII tokens like gt/max are usually model suffixes, not the subject:
    # a strong title hit needs a CJK bigram or an ASCII word of length 4+.
    title_strong = [token for token in strong if _CJK_CHAR_RE.search(token) or len(token) >= 4]
    title_tokens = title_strong if title_strong else usable
    return usable, title_tokens


def looks_relevant(query: str, sources: list[dict[str, Any]]) -> bool:
    """Whether a result batch shares enough vocabulary with the query.

    A batch that fails this gate is treated as empty so the caller can fall back
    to the next engine. Empty-token queries pass; an empty source list fails.
    """
    usable, title_tokens = _usable_tokens(query)
    # Pure-symbol/single-character queries cannot be judged reliably; let them through.
    if not usable:
        return True
    matched: set[str] = set()
    title_matched: set[str] = set()
    relevant = 0
    for source in sources:
        title = _text(source.get("title")).lower()
        hay = f"{title} {_text(source.get('snippet'))} {_text(source.get('url'))}".lower()
        hits = [token for token in usable if token.lower() in hay]
        if hits:
            relevant += 1
            matched.update(hits)
            for token in title_tokens:
                if token.lower() in title:
                    title_matched.add(token)
    enough_sources = relevant >= max(1, (len(sources) + 2) // 3)
    enough_tokens = len(matched) >= min(2, len(usable)) or len(matched) / len(usable) >= 0.5
    return enough_sources and enough_tokens and len(title_matched) > 0


def relevance_score(query: str, source: dict[str, Any]) -> float:
    """Ranking score in 0..1: (usable hits in title+snippet+url + title hits) / (2 * usable)."""
    usable, title_tokens = _usable_tokens(query)
    if not usable:
        return 0.0
    title = _text(source.get("title")).lower()
    hay = f"{title} {_text(source.get('snippet'))} {_text(source.get('url'))}".lower()
    matched_in_hay = sum(1 for token in usable if token.lower() in hay)
    matched_in_title = sum(1 for token in title_tokens if token.lower() in title)
    return (matched_in_hay + matched_in_title) / (2 * len(usable))
