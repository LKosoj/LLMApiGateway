"""Exact staged i18n inventory validation for first-party static pages."""

from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any, NamedTuple


INVENTORY_PATH = Path("scripts/i18n_inventory.json")
INVENTORY_FIELDS = {
    "schemaVersion",
    "requireAllComplete",
    "pages",
    "exemptions",
}
PAGE_FIELDS = {"status", "html", "scripts"}
EXEMPTION_FIELDS = {
    "page",
    "file",
    "kind",
    "line",
    "column",
    "text",
    "classification",
}
EXEMPTIBLE_KINDS = {
    "html-text",
    "html-attribute",
    "html-code",
    "html-pre",
    "js-call-sink",
    "js-sink",
}
TECHNICAL_CLASSIFICATIONS = {"code", "pre", "protocol", "sample"}
TRANSLATABLE_ATTRIBUTES = {
    "alt",
    "aria-description",
    "aria-label",
    "placeholder",
    "title",
}
ATTRIBUTE_BINDINGS = {
    "aria-description": "data-i18n-aria-description",
    "aria-label": "data-i18n-aria-label",
    "placeholder": "data-i18n-placeholder",
    "title": "data-i18n-title",
}
BINDING_ATTRIBUTES = {"data-i18n", *ATTRIBUTE_BINDINGS.values()}
BINDING_PATTERN = re.compile(r"^([a-z][a-z0-9_]*):([^.:\s]+(?:\.[^.:\s]+)*)$")
IGNORED_HTML_TAGS = {"script", "style", "template"}
VOID_HTML_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
RAW_ATTRIBUTE_PATTERN = re.compile(
    r"[^\s=/>]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?"
)
ASCII_LOWER_TABLE = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)


class InventoryFinding(NamedTuple):
    page: str
    file: str
    kind: str
    line: int
    column: int
    text: str


class _HtmlFrame(NamedTuple):
    tag: str
    text_bound: bool
    line: int
    column: int


def _read_json(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"invalid JSON file {path}: {error}")
        return None


def _normalized_literal(value: str) -> str:
    return " ".join(value.split())


def _is_visible_literal(value: str) -> bool:
    return bool(value) and any(character.isalnum() for character in value)


def _flatten_catalog(value: object, prefix: str = "") -> set[str]:
    if not isinstance(value, dict):
        return set()
    keys: set[str] = set()
    for name, child in value.items():
        path = f"{prefix}.{name}" if prefix else str(name)
        if isinstance(child, str):
            keys.add(path)
        elif isinstance(child, dict):
            keys.update(_flatten_catalog(child, path))
    return keys


def _position_from_offset(
    raw: str,
    offset: int,
    start_line: int,
    start_column: int,
) -> tuple[int, int]:
    prefix = raw[:offset]
    newline_count = prefix.count("\n")
    if newline_count == 0:
        return start_line, start_column + offset
    return start_line + newline_count, offset - prefix.rfind("\n")


def _attribute_positions(
    raw: str,
    attrs: list[tuple[str, str | None]],
    start_line: int,
    start_column: int,
) -> list[tuple[int, int]]:
    matches = list(RAW_ATTRIBUTE_PATTERN.finditer(raw[1:]))[1:]
    positions: list[tuple[int, int]] = []
    for index, _attribute in enumerate(attrs):
        if index >= len(matches):
            positions.append((start_line, start_column))
            continue
        match = matches[index]
        token = match.group(0)
        equals = token.find("=")
        value_offset = 0
        if equals >= 0:
            value_offset = equals + 1
            while value_offset < len(token) and token[value_offset].isspace():
                value_offset += 1
            if value_offset < len(token) and token[value_offset] in "'\"":
                value_offset += 1
        positions.append(
            _position_from_offset(
                raw,
                match.start() + 1 + value_offset,
                start_line,
                start_column,
            )
        )
    return positions


class _StrictHtmlScanner(HTMLParser):
    def __init__(
        self,
        page: str,
        source_path: str,
        page_namespaces: set[str],
        catalog_keys: dict[str, set[str]],
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.page = page
        self.source_path = source_path
        self.page_namespaces = page_namespaces
        self.catalog_keys = catalog_keys
        self.findings: list[InventoryFinding] = []
        self.errors: list[str] = []
        self._stack: list[_HtmlFrame] = []

    def _location(self) -> tuple[int, int]:
        line, column = self.getpos()
        return line, column + 1

    def _error(self, message: str, line: int, column: int) -> None:
        self.errors.append(f"{self.source_path}:{line}:{column}: {message}")

    def _record(self, kind: str, value: str, line: int, column: int) -> None:
        literal = _normalized_literal(value)
        if _is_visible_literal(literal):
            self.findings.append(
                InventoryFinding(
                    self.page,
                    self.source_path,
                    kind,
                    line,
                    column,
                    literal,
                )
            )

    def _validate_binding(
        self,
        value: str | None,
        line: int,
        column: int,
    ) -> None:
        match = BINDING_PATTERN.fullmatch(value or "")
        if match is None:
            self._error(f"invalid i18n binding: {value or ''}", line, column)
            return
        namespace, key = match.groups()
        if namespace not in self.page_namespaces:
            self._error(
                f"namespace is not registered for {self.page}: {namespace}",
                line,
                column,
            )
            return
        if key not in self.catalog_keys.get(namespace, set()):
            self._error(f"catalog key does not exist: {namespace}:{key}", line, column)

    def _start(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        push: bool,
    ) -> None:
        normalized_tag = tag.lower()
        line, column = self._location()
        if self._stack and self._stack[-1].text_bound:
            self._error("bound element must not contain child elements", line, column)
        raw = self.get_starttag_text() or f"<{tag}>"
        positions = _attribute_positions(raw, attrs, line, column)
        attributes: dict[str, str | None] = {}
        counts: dict[str, int] = {}
        for (name, value), (attr_line, attr_column) in zip(attrs, positions, strict=True):
            normalized_name = name.lower()
            counts[normalized_name] = counts.get(normalized_name, 0) + 1
            if counts[normalized_name] > 1 and normalized_name in BINDING_ATTRIBUTES:
                self._error(
                    f"duplicate HTML attribute: {normalized_name}",
                    attr_line,
                    attr_column,
                )
            attributes[normalized_name] = value
            if normalized_name in BINDING_ATTRIBUTES:
                self._validate_binding(value, attr_line, attr_column)

        translatable = set(TRANSLATABLE_ATTRIBUTES)
        input_type = (attributes.get("type") or "").translate(ASCII_LOWER_TABLE)
        if normalized_tag == "input" and input_type in {
            "button",
            "reset",
            "submit",
        }:
            translatable.add("value")
        for attribute in sorted(translatable):
            value = attributes.get(attribute)
            binding = ATTRIBUTE_BINDINGS.get(attribute)
            if value is not None and (binding is None or binding not in attributes):
                index = next(
                    (
                        position
                        for position, (name, _value) in enumerate(attrs)
                        if name.lower() == attribute
                    ),
                    None,
                )
                attr_line, attr_column = positions[index] if index is not None else (line, column)
                self._record("html-attribute", value, attr_line, attr_column)
        if push:
            self._stack.append(
                _HtmlFrame(normalized_tag, "data-i18n" in attributes, line, column)
            )

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._start(tag, attrs, tag.lower() not in VOID_HTML_TAGS)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._start(tag, attrs, False)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        line, column = self._location()
        if not self._stack:
            self._error(f"unexpected closing tag: {normalized_tag}", line, column)
            return
        if self._stack[-1].tag == normalized_tag:
            self._stack.pop()
            return
        self._error(f"misnested closing tag: {normalized_tag}", line, column)
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == normalized_tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if not self._stack:
            line, column = self._location()
            self._record("html-text", data, line, column)
            return
        tags = [frame.tag for frame in self._stack]
        if any(tag in IGNORED_HTML_TAGS for tag in tags) or self._stack[-1].text_bound:
            return
        kind = "html-text"
        if "pre" in tags:
            kind = "html-pre"
        elif "code" in tags:
            kind = "html-code"
        line, column = self._location()
        self._record(kind, data, line, column)

    def finish(self) -> None:
        for frame in reversed(self._stack):
            self._error(f"unclosed HTML tag: {frame.tag}", frame.line, frame.column)


def _is_inventory_source(value: object, suffix: str) -> bool:
    if not isinstance(value, str):
        return False
    path = PurePosixPath(value)
    return (
        value == path.as_posix()
        and not path.is_absolute()
        and path.parts[:1] == ("static",)
        and len(path.parts) == 2
        and ".." not in path.parts
        and path.suffix == suffix
    )


def _registered_catalog_keys(
    root: Path,
    registry: dict[str, object],
    errors: list[str],
) -> dict[str, set[str]]:
    namespaces = registry.get("namespaces")
    locales = registry.get("locales")
    if not isinstance(namespaces, list) or not isinstance(locales, list):
        return {}
    result: dict[str, set[str]] = {}
    for namespace in namespaces:
        if not isinstance(namespace, str):
            continue
        common_keys: set[str] | None = None
        for locale in locales:
            if not isinstance(locale, dict) or not isinstance(locale.get("code"), str):
                continue
            catalog = _read_json(
                root / "static" / "locales" / locale["code"] / f"{namespace}.json",
                errors,
            )
            keys = _flatten_catalog(catalog)
            common_keys = keys if common_keys is None else common_keys & keys
        result[namespace] = common_keys or set()
    return result


def _scan_javascript_sources(
    root: Path,
    sources: list[dict[str, str]],
    errors: list[str],
) -> list[InventoryFinding]:
    scanner_root = root / "frontend" / "ui-runtime"
    scanner = scanner_root / "scan-i18n-inventory.mjs"
    try:
        completed = subprocess.run(
            ["node", str(scanner)],
            cwd=scanner_root,
            input=json.dumps({"sources": sources}),
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as error:
        errors.append(f"cannot run JavaScript inventory scanner: {error}")
        return []
    if completed.returncode != 0:
        errors.append(
            "JavaScript inventory scanner failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        errors.append(f"JavaScript inventory scanner returned invalid JSON: {error}")
        return []
    scanner_errors = payload.get("errors") if isinstance(payload, dict) else None
    raw_findings = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(scanner_errors, list) or not isinstance(raw_findings, list):
        errors.append("JavaScript inventory scanner returned an invalid result")
        return []
    errors.extend(str(error) for error in scanner_errors)
    findings: list[InventoryFinding] = []
    for raw in raw_findings:
        try:
            findings.append(
                InventoryFinding(
                    page=raw["page"],
                    file=raw["file"],
                    kind=raw["kind"],
                    line=raw["line"],
                    column=raw["column"],
                    text=raw["text"],
                )
            )
        except (KeyError, TypeError):
            errors.append("JavaScript inventory scanner returned an invalid finding")
            return []
    return findings


def validate_inventory(
    root: Path,
    selected_pages: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    manifest = _read_json(root / INVENTORY_PATH, errors)
    if not isinstance(manifest, dict):
        return errors or ["i18n inventory manifest must be a JSON object"]
    if set(manifest) != INVENTORY_FIELDS or manifest.get("schemaVersion") != 2:
        errors.append("i18n inventory manifest fields do not match schema version 2")
    require_all_complete = manifest.get("requireAllComplete")
    if not isinstance(require_all_complete, bool):
        errors.append("requireAllComplete must be a boolean")
        require_all_complete = False

    registry = _read_json(root / "static/locales/registry.json", errors)
    if not isinstance(registry, dict):
        return errors
    page_namespaces = registry.get("pageNamespaces")
    registry_pages = set(page_namespaces) if isinstance(page_namespaces, dict) else set()
    catalog_keys = _registered_catalog_keys(root, registry, errors)
    pages = manifest.get("pages")
    if not isinstance(pages, dict) or not pages:
        errors.append("inventory pages must be a non-empty object")
        return errors
    if set(pages) != registry_pages:
        errors.append("inventory pages must exactly match registry pageNamespaces")

    page_statuses: dict[str, str] = {}
    source_owners: dict[str, str] = {}
    source_kinds: dict[str, str] = {}
    for page, entry in pages.items():
        if not isinstance(page, str) or not isinstance(entry, dict) or set(entry) != PAGE_FIELDS:
            errors.append(f"invalid inventory page entry: {page}")
            continue
        status = entry.get("status")
        if status not in {"pending", "complete"}:
            errors.append(f"inventory page {page} status must be pending or complete")
            continue
        page_statuses[page] = status
        html_sources = entry.get("html")
        script_sources = entry.get("scripts")
        if not isinstance(html_sources, list) or not isinstance(script_sources, list):
            errors.append(f"inventory page {page} sources must be lists")
            continue
        if not html_sources and not script_sources:
            errors.append(f"inventory page {page} must declare at least one source")
        for source_list, suffix, kind in (
            (html_sources, ".html", "html"),
            (script_sources, ".js", "javascript"),
        ):
            if len(source_list) != len(set(map(str, source_list))):
                errors.append(f"inventory page {page} contains duplicate sources")
            for source_path in source_list:
                if not _is_inventory_source(source_path, suffix):
                    errors.append(f"invalid inventory source for {page}: {source_path}")
                    continue
                if source_path in source_owners:
                    errors.append(f"inventory source declared more than once: {source_path}")
                    continue
                source_owners[source_path] = page
                source_kinds[source_path] = kind
                if not (root / source_path).is_file():
                    errors.append(f"missing inventory source: {source_path}")

    actual_sources = {
        path.relative_to(root).as_posix()
        for suffix in ("*.html", "*.js")
        for path in (root / "static").glob(suffix)
        if path.is_file()
    }
    for source_path in sorted(actual_sources - set(source_owners)):
        errors.append(f"undeclared first-party source: {source_path}")
    if selected_pages is not None:
        for page in sorted(selected_pages - set(pages)):
            errors.append(f"unknown inventory page: {page}")
    pending_pages = sorted(
        page for page, status in page_statuses.items() if status == "pending"
    )
    if require_all_complete and pending_pages:
        errors.append(
            "inventory requires every declared page to be complete: "
            + ", ".join(pending_pages)
        )

    findings: list[InventoryFinding] = []
    javascript_sources: list[dict[str, str]] = []
    for source_path, page in source_owners.items():
        path = root / source_path
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            errors.append(f"cannot read inventory source {source_path}: {error}")
            continue
        if source_kinds[source_path] == "javascript":
            javascript_sources.append({"page": page, "file": source_path, "source": source})
            continue
        mapped_namespaces = page_namespaces.get(page, []) if isinstance(page_namespaces, dict) else []
        scanner = _StrictHtmlScanner(
            page,
            source_path,
            set(mapped_namespaces) if isinstance(mapped_namespaces, list) else set(),
            catalog_keys,
        )
        scanner.feed(source)
        scanner.close()
        scanner.finish()
        findings.extend(scanner.findings)
        errors.extend(scanner.errors)
    if javascript_sources:
        findings.extend(_scan_javascript_sources(root, javascript_sources, errors))

    finding_keys = set(findings)
    exempted_keys: set[InventoryFinding] = set()
    exemptions = manifest.get("exemptions")
    if not isinstance(exemptions, list):
        errors.append("inventory exemptions must be a list")
        exemptions = []
    for index, exemption in enumerate(exemptions):
        if not isinstance(exemption, dict) or set(exemption) != EXEMPTION_FIELDS:
            errors.append(f"inventory exemption {index} has an invalid schema")
            continue
        page = exemption.get("page")
        source_path = exemption.get("file")
        kind = exemption.get("kind")
        line = exemption.get("line")
        column = exemption.get("column")
        text = exemption.get("text")
        classification = exemption.get("classification")
        if (
            not isinstance(page, str)
            or not isinstance(source_path, str)
            or kind not in EXEMPTIBLE_KINDS
            or not isinstance(line, int)
            or isinstance(line, bool)
            or line < 1
            or not isinstance(column, int)
            or isinstance(column, bool)
            or column < 1
            or not isinstance(text, str)
            or not text
            or classification not in TECHNICAL_CLASSIFICATIONS
            or source_owners.get(source_path) != page
        ):
            errors.append(f"inventory exemption {index} is not exact")
            continue
        finding = InventoryFinding(page, source_path, kind, line, column, text)
        if finding in exempted_keys:
            errors.append(f"duplicate inventory exemption {index}")
            continue
        exempted_keys.add(finding)
        if finding not in finding_keys:
            errors.append(
                f"unused inventory exemption: {source_path}:{line}:{column} {kind} {text}"
            )

    completed_pages = {
        page for page, status in page_statuses.items() if status == "complete"
    }
    strict_pages = completed_pages | (selected_pages or set())
    for item in findings:
        if item.page not in strict_pages or item in exempted_keys:
            continue
        errors.append(
            f"[{item.page}] {item.file}:{item.line}:{item.column} {item.kind}: {item.text}"
        )
    return errors
