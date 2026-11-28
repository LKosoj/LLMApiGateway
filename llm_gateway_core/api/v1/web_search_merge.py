"""Merge and rank free-engine web search results (port of dsh-free-search).

Ports ``dedupeNearDuplicates`` from ``lib/dedupe.js`` (MinHash over title+snippet),
``MULTI_PART_SUFFIXES`` / ``domainOf`` from ``lib/multi_policy.js`` and the
domain-interleaving shape of ``rankMergedResults`` from ``lib/search_quality.js``.
URL dedupe keys follow ``_search_result_url_key`` from ``web_adapters.py`` with
tracking parameters stripped first. No I/O; stdlib plus ``web_relevance`` only.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .web_relevance import _HOST_REQUIRED_SCHEMES, _text, relevance_score, strip_tracking_params

# JS seeds are (i + 1) * 0x9e3779b1 truncated to 32 bits by the bitwise XOR in hash32.
_SEEDS = tuple(((i + 1) * 0x9E3779B1) & 0xFFFFFFFF for i in range(16))
_MASK32 = 0xFFFFFFFF
_URL_IN_TEXT_RE = re.compile(r"https?://\S+")
_IPV4_RE = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}")

_NEAR_DUP_THRESHOLD = 0.8
_SEEN_IN_BOOST = 0.15
_MAX_SEEN_IN_BOOST = 0.45

# Common two-label public suffixes (co.uk / com.cn / ...). Not a full PSL: only the
# ones public search actually hits; anything else falls back to the last two labels.
_MULTI_PART_SUFFIXES = frozenset(
    {
        "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
        "com.hk", "org.hk", "net.hk", "edu.hk", "gov.hk",
        "com.tw", "org.tw", "net.tw", "edu.tw", "gov.tw",
        "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
        "co.kr", "or.kr", "ne.kr", "go.kr", "re.kr",
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk", "sch.uk",
        "com.au", "net.au", "org.au", "edu.au", "gov.au",
        "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz",
        "co.za", "org.za", "net.za", "gov.za",
        "co.in", "net.in", "org.in", "gov.in", "ac.in",
        "com.sg", "com.my", "com.ph", "com.pk", "com.vn", "co.th", "co.id",
        "com.br", "com.mx", "com.ar", "com.tr", "com.ua", "com.pl", "com.ru",
        "co.il", "com.sa", "com.eg", "com.ng", "co.ke",
    }
)


def _hash32(units: tuple[int, ...], seed: int) -> int:
    # FNV-like hash; masking each step matches JS Math.imul + `>>> 0` low 32 bits.
    value = (2166136261 ^ seed) & _MASK32
    for unit in units:
        value ^= unit
        value = (value * 16777619) & _MASK32
    return value


def _normalize_text(text: str) -> str:
    lowered = _URL_IN_TEXT_RE.sub(" ", text.lower())
    # JS \s\p{P}\p{S}: whitespace, punctuation and symbols (Unicode categories P*/S*).
    # U+FEFF counts as \s in JS but not in Python isspace(); strip it explicitly.
    return "".join(
        ch
        for ch in lowered
        if ch != "\ufeff" and not ch.isspace() and unicodedata.category(ch)[0] not in ("P", "S")
    )


def _utf16_units(text: str) -> list[int]:
    # JS strings are UTF-16: charCodeAt/length/slice operate on code units, not points.
    raw = text.encode("utf-16-le", errors="surrogatepass")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def _shingles(text: str) -> list[tuple[int, ...]]:
    units = _utf16_units(_normalize_text(text))
    if not units:
        return []
    size = 3 if len(units) >= 6 else max(1, len(units))
    seen: dict[tuple[int, ...], None] = {}
    for i in range(len(units) - size + 1):
        seen[tuple(units[i : i + size])] = None
    return list(seen)


def _minhash(text: str) -> tuple[int, ...]:
    parts = _shingles(text)
    if not parts:
        return (0,) * len(_SEEDS)
    return tuple(min(_hash32(part, seed) for part in parts) for seed in _SEEDS)


def _signature_similarity(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def dedupe_near_duplicates(
    sources: list[dict[str, Any]], threshold: float = _NEAR_DUP_THRESHOLD
) -> list[dict[str, Any]]:
    """Keep the first source of each near-duplicate group; order is preserved.

    Exact (raw) URL duplicates are removed too. Threshold 0.8 catches syndicated
    copies while leaving different articles on the same topic separate. Sources
    with empty title+snippet get all-zero signatures and collapse into one, as
    in the JS reference.
    """
    kept: list[dict[str, Any]] = []
    signatures: list[tuple[int, ...]] = []
    exact_urls: set[str] = set()
    for source in sources:
        url = _text(source.get("url"))
        if url and url in exact_urls:
            continue
        signature = _minhash(f"{_text(source.get('title'))} {_text(source.get('snippet'))}")
        if any(_signature_similarity(existing, signature) >= threshold for existing in signatures):
            continue
        if url:
            exact_urls.add(url)
        kept.append(source)
        signatures.append(signature)
    return kept


def _normalized_url_key(url: str) -> str:
    """Dedupe key: strip trackers, then `_search_result_url_key` semantics.

    http→https, lowercase host, drop a trailing slash (root stays "/"), drop
    ports 80/443 and the fragment, keep the query. Never raises: a malformed
    port falls back to the fragment-stripped raw string.
    """
    raw = strip_tracking_params(str(url or "").strip())
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return raw.split("#", 1)[0].rstrip("/")
    if not host:
        return raw.split("#", 1)[0].rstrip("/")
    scheme = (parsed.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    netloc = host
    if port and port not in {80, 443}:
        netloc = f"{host}:{port}"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _registrable_domain(url: str) -> str:
    """Simplified eTLD+1 for same-site grouping (port of multi_policy.js domainOf).

    IP / IPv6 / localhost / single-label hosts are returned lowercase as is;
    unparseable URLs fall back to the raw string lowercased. IPv6 loses the JS
    brackets (Python's hostname strips them); grouping is unaffected.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or (parsed.scheme in _HOST_REQUIRED_SCHEMES and not parsed.netloc):
            return raw.lower()
        _ = parsed.port  # validates the port the way `new URL()` does
        host = (parsed.hostname or "").lower()
        if parsed.scheme in _HOST_REQUIRED_SCHEMES and not host:
            return raw.lower()
    except ValueError:
        return raw.lower()
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    if "." not in host or ":" in host or _IPV4_RE.fullmatch(host):
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last_two = ".".join(parts[-2:])
    if last_two in _MULTI_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return last_two


def merge_free_results(
    per_engine: dict[str, list[dict[str, Any]]], *, query: str, max_results: int
) -> list[dict[str, Any]]:
    """Dedupe, score and domain-interleave results from several free engines.

    Engine order in ``per_engine`` is the tie-break priority (first occurrence
    wins). Returns ``[{"url", "title", "snippet"}]`` with the original URL of
    the first occurrence, at most ``max_results`` items.
    """
    if max_results <= 0:
        return []
    records: dict[str, dict[str, Any]] = {}
    engines_by_key: dict[str, set[str]] = {}
    for engine, results in per_engine.items():
        for item in results:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            key = _normalized_url_key(url)
            if not key:
                continue
            engines_by_key.setdefault(key, set()).add(engine)
            if key in records:
                continue
            records[key] = {
                "url": url,
                "title": _text(item.get("title")),
                "snippet": _text(item.get("snippet")),
                "order": len(records),
            }
    unique: list[dict[str, Any]] = []
    for key, record in records.items():
        record["seen_in"] = len(engines_by_key[key])
        unique.append(record)
    kept = dedupe_near_duplicates(unique)
    for record in kept:
        boost = min(_MAX_SEEN_IN_BOOST, _SEEN_IN_BOOST * (record["seen_in"] - 1))
        record["score"] = relevance_score(query, record) + boost
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in kept:
        groups.setdefault(_registrable_domain(record["url"]), []).append(record)
    for group in groups.values():
        group.sort(key=lambda record: (-record["score"], record["order"]))
    ordered = sorted(groups.values(), key=lambda group: (-group[0]["score"], group[0]["order"]))
    merged: list[dict[str, Any]] = []
    longest = max((len(group) for group in ordered), default=0)
    for round_index in range(longest):
        for group in ordered:
            if round_index < len(group):
                merged.append(group[round_index])
        if len(merged) >= max_results:
            break
    return [
        {"url": record["url"], "title": record["title"], "snippet": record["snippet"]}
        for record in merged[:max_results]
    ]
