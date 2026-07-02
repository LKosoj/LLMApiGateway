from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

NON_ARTICLE_IMAGE_PATTERNS = (
    "/img/avatars/",
    "avatar",
    "gravatar",
    "profile-picture",
    "favicon",
)
SMALL_IMAGE_DIMENSIONS_REGEX = re.compile(
    r"w_(?:16|24|32|36|40|48|64),h_(?:16|24|32|36|40|48|64)"
)
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
ARTICLE_IMAGE_SELECTORS = (
    "article img",
    "main img",
    ".body.markup img",
    ".available-content img",
    ".captioned-image-container img",
)


def markdown_to_plain_text(value: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", value)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]{0,3}>[ \t]?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*[-*+][ \t]+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*\d+\.[ \t]+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~]+", "", text)
    lines = [line.strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def clean_read_url(url: str) -> str:
    return url.strip().strip("()").strip('"').strip("'").strip()


def clean_image_description(value: Any) -> str:
    return " ".join(str(value or "").split())[:300]


def is_non_article_image(url: str, description: str = "") -> bool:
    lower_url = (url or "").lower()
    lower_description = (description or "").lower()
    if not lower_url:
        return True
    if any(pattern in lower_url for pattern in NON_ARTICLE_IMAGE_PATTERNS):
        return True
    if "avatar" in lower_description:
        return True
    if SMALL_IMAGE_DIMENSIONS_REGEX.search(lower_url):
        return True
    return False


def normalize_image_url(value: Any, base_url: str = "") -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().strip('"').strip("'")
    if not candidate:
        return None
    if candidate.startswith(("data:", "blob:", "javascript:")):
        return None
    resolved = urljoin(base_url, candidate)
    parsed = urlsplit(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return resolved


def add_image_item(
    images: list[dict[str, str]],
    seen: set[str],
    value: Any,
    *,
    base_url: str = "",
    description: Any = "",
    filter_non_article: bool = True,
) -> None:
    image_url = normalize_image_url(value, base_url)
    if image_url is None or image_url in seen:
        return
    clean_description = clean_image_description(description)
    if filter_non_article and is_non_article_image(image_url, clean_description):
        return
    seen.add(image_url)
    images.append({"url": image_url, "description": clean_description})


def best_srcset_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    best_url: str | None = None
    best_width = -1
    for candidate in value.split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        width = 0
        if len(parts) > 1 and parts[1].endswith("w"):
            try:
                width = int(parts[1][:-1])
            except ValueError:
                width = 0
        if best_url is None or width > best_width:
            best_url = parts[0]
            best_width = width
    return best_url


def extract_jsonld_images(raw_html: Any, base_url: str) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_candidate(candidate: Any, description: Any = "") -> None:
        if isinstance(candidate, str):
            add_image_item(images, seen, candidate, base_url=base_url, description=description)
        elif isinstance(candidate, dict):
            add_candidate(
                candidate.get("url")
                or candidate.get("contentUrl")
                or candidate.get("thumbnailUrl"),
                candidate.get("caption") or candidate.get("name") or description,
            )
        elif isinstance(candidate, list):
            for item in candidate:
                add_candidate(item, description)

    try:
        from bs4 import BeautifulSoup

        html_text = raw_html.decode("utf-8", errors="ignore") if isinstance(raw_html, bytes) else str(raw_html or "")
        if not html_text.strip():
            return images

        soup = BeautifulSoup(html_text, "lxml")
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            payload = (script.string or script.text or "").strip()
            if not payload:
                continue
            try:
                data = json.loads(payload)
            except Exception:
                continue

            stack: list[Any] = [data]
            while stack:
                current = stack.pop()
                if isinstance(current, dict):
                    current_type = current.get("@type")
                    types = (
                        [current_type]
                        if isinstance(current_type, str)
                        else current_type
                        if isinstance(current_type, list)
                        else []
                    )
                    normalized_types = {str(item).lower() for item in types}
                    if normalized_types & {"article", "newsarticle", "blogposting"}:
                        description = current.get("headline") or current.get("name") or ""
                        add_candidate(current.get("image"), description)
                        add_candidate(current.get("thumbnailUrl"), description)
                        add_candidate(current.get("primaryImageOfPage"), description)
                    stack.extend(current.values())
                elif isinstance(current, list):
                    stack.extend(current)
    except Exception as exc:
        logger.debug("Failed to extract images from JSON-LD: %s", exc)
    return images


def extract_article_images_from_html(raw_html: Any, base_url: str) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_candidate(value: Any, description: Any = "") -> None:
        add_image_item(images, seen, value, base_url=base_url, description=description)

    def add_img_tag(img_tag: Any) -> None:
        description = img_tag.get("alt") or img_tag.get("title") or ""
        add_candidate(
            img_tag.get("src")
            or img_tag.get("data-src")
            or img_tag.get("data-original")
            or img_tag.get("data-lazy-src")
            or best_srcset_url(img_tag.get("srcset") or img_tag.get("data-srcset")),
            description,
        )

    try:
        from bs4 import BeautifulSoup

        html_text = raw_html.decode("utf-8", errors="ignore") if isinstance(raw_html, bytes) else str(raw_html or "")
        if not html_text.strip():
            return images

        soup = BeautifulSoup(html_text, "lxml")
        for selector in ARTICLE_IMAGE_SELECTORS:
            for img_tag in soup.select(selector):
                add_img_tag(img_tag)

        if not images:
            for img_tag in soup.find_all("img"):
                add_img_tag(img_tag)

        for meta_name in ("og:image", "og:image:url", "twitter:image", "twitter:image:src"):
            for meta in soup.find_all("meta", attrs={"property": meta_name}):
                add_candidate(meta.get("content"))
            for meta in soup.find_all("meta", attrs={"name": meta_name}):
                add_candidate(meta.get("content"))
        for link in soup.find_all("link", rel=lambda value: value and "image_src" in value):
            add_candidate(link.get("href"))
    except Exception as exc:
        logger.debug("Failed to extract article images from HTML: %s", exc)

    for item in extract_jsonld_images(raw_html, base_url):
        add_image_item(images, seen, item.get("url"), base_url=base_url, description=item.get("description"))
    return images


def extract_images_from_markdown(content: Any, base_url: str = "") -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    seen: set[str] = set()
    text = str(content or "")
    for description, image_url in MARKDOWN_IMAGE_RE.findall(text):
        add_image_item(
            images,
            seen,
            image_url,
            base_url=base_url,
            description=description,
            filter_non_article=False,
        )
    return images


def merge_image_items(*groups: Any, base_url: str = "") -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_group(group: Any) -> None:
        if group is None:
            return
        if isinstance(group, str):
            add_image_item(images, seen, group, base_url=base_url, filter_non_article=False)
            return
        if isinstance(group, dict):
            add_image_item(
                images,
                seen,
                group.get("url") or group.get("src") or group.get("image_url") or group.get("contentUrl"),
                base_url=base_url,
                description=group.get("description") or group.get("alt") or group.get("caption") or group.get("title"),
                filter_non_article=False,
            )
            return
        if isinstance(group, list):
            for item in group:
                add_group(item)

    for group in groups:
        add_group(group)
    return images


def content_with_images(content: Any, images: Any, base_url: str = "") -> tuple[str, list[dict[str, str]]]:
    content_text = str(content or "").strip()
    merged_images = merge_image_items(
        images,
        extract_images_from_markdown(content_text, base_url),
        base_url=base_url,
    )
    return content_text, merged_images


def append_images_to_markdown(content: str, images: list[dict[str, str]]) -> str:
    text = content or ""
    additions: list[str] = []
    for image in images:
        url = str(image.get("url") or "").strip()
        if not url or url in text:
            continue
        description = str(image.get("description") or "").strip()
        additions.append(f"![{description}]({url})")
    if not additions:
        return text
    block = "\n\n".join(additions)
    prefix = text.rstrip()
    return f"{prefix}\n\n{block}" if prefix else block
