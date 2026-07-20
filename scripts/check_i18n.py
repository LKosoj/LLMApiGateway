#!/usr/bin/env python3
"""Validate the R5.1 locale registry and strict ICU catalog contract."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from i18n_inventory import validate_inventory  # noqa: E402


EXPECTED_NAMESPACES = {
    "common",
    "auth",
    "overview",
    "activity",
    "docs",
    "editor",
    "playground",
    "usage",
    "topology",
    "api_keys",
    "pricing",
    "quota",
    "rejections",
    "translator",
    "free_models",
}
LOCALE_PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
MAX_LOCALE_PATH_BYTES = 255


def is_safe_locale_segment(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(LOCALE_PATH_SEGMENT_PATTERN.fullmatch(value))
        and len(value.encode("utf-8")) <= MAX_LOCALE_PATH_BYTES
    )


def _read_json(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"invalid JSON file {path}: {error}")
        return None


def validate_structure(root: Path) -> list[str]:
    errors: list[str] = []
    locale_root = root / "static" / "locales"
    registry_path = locale_root / "registry.json"
    registry = _read_json(registry_path, errors)
    if not isinstance(registry, dict):
        return errors or ["locale registry must be a JSON object"]

    expected_fields = {
        "schemaVersion",
        "defaultLocale",
        "locales",
        "namespaces",
        "pageNamespaces",
    }
    if set(registry) != expected_fields:
        errors.append("locale registry fields do not match schema version 1")
    if registry.get("schemaVersion") != 1:
        errors.append("schemaVersion must equal 1")

    locales = registry.get("locales")
    if not isinstance(locales, list) or not locales:
        errors.append("locales must be a non-empty list")
        locales = []
    elif len(locales) > 20:
        errors.append("locale registry must contain at most 20 locales")

    locale_codes: list[str] = []
    for index, locale in enumerate(locales):
        if not isinstance(locale, dict) or set(locale) != {"code", "nativeLabel", "dir"}:
            errors.append(f"locale entry {index} has an invalid schema")
            continue
        code = locale.get("code")
        if not is_safe_locale_segment(code):
            errors.append(f"locale entry {index} is not a path-safe locale segment")
            continue
        if code in locale_codes:
            errors.append(f"duplicate locale code: {code}")
        locale_codes.append(code)
        if not isinstance(locale.get("nativeLabel"), str) or not locale["nativeLabel"].strip():
            errors.append(f"locale {code} has no nativeLabel")
        if locale.get("dir") not in {"ltr", "rtl"}:
            errors.append(f"locale {code} dir must be ltr or rtl")

    if set(locale_codes) < {"en", "ru"}:
        errors.append("registry must contain both en and ru locales")
    if registry.get("defaultLocale") not in locale_codes:
        errors.append("defaultLocale must reference a registered locale")

    namespaces = registry.get("namespaces")
    if not isinstance(namespaces, list) or set(namespaces) != EXPECTED_NAMESPACES:
        errors.append("namespace registry does not match the R5.1 namespace contract")
        namespaces = []
    elif len(namespaces) != len(set(namespaces)):
        errors.append("namespace registry contains duplicates")

    page_namespaces = registry.get("pageNamespaces")
    if not isinstance(page_namespaces, dict) or not page_namespaces:
        errors.append("pageNamespaces must be a non-empty object")
    else:
        namespace_set = set(namespaces)
        for page, mapped in page_namespaces.items():
            if (
                not isinstance(page, str)
                or not isinstance(mapped, list)
                or not mapped
                or len(mapped) != len(set(mapped))
                or not set(mapped) <= namespace_set
            ):
                errors.append(f"invalid pageNamespaces entry: {page}")

    for code in locale_codes:
        free_tier_path = locale_root / code / "free-tier-providers.md"
        try:
            free_tier_is_file = free_tier_path.is_file()
        except OSError:
            errors.append(f"cannot inspect free-tier document: {code}")
            free_tier_is_file = False
        if not free_tier_is_file:
            errors.append(f"missing free-tier document: {code}")
        else:
            try:
                free_tier_content = free_tier_path.read_text(encoding="utf-8")
            except UnicodeError:
                errors.append(f"invalid UTF-8 free-tier document: {code}")
            except OSError:
                errors.append(f"cannot read free-tier document: {code}")
            else:
                if not free_tier_content.strip():
                    errors.append(f"free-tier document must be non-empty: {code}")

        for namespace in namespaces:
            catalog_path = locale_root / code / f"{namespace}.json"
            try:
                is_file = catalog_path.is_file()
            except OSError:
                errors.append(f"cannot inspect catalog path: {code}/{namespace}")
                continue
            if not is_file:
                errors.append(f"missing catalog: {code}/{namespace}")
                continue
            catalog = _read_json(catalog_path, errors)
            if not isinstance(catalog, dict) or not catalog:
                errors.append(f"catalog must be a non-empty object: {code}/{namespace}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--page", action="append", dest="pages")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    errors = validate_structure(root)
    errors.extend(
        validate_inventory(root, set(args.pages) if args.pages is not None else None)
    )
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    checker_root = root / "frontend" / "ui-runtime"
    checker = checker_root / "check-catalogs.mjs"
    try:
        completed = subprocess.run(
            ["node", str(checker), str(root)],
            cwd=checker_root,
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError as error:
        print(f"cannot run ICU catalog checker: {error}", file=sys.stderr)
        return 2
    if completed.returncode != 0:
        print(completed.stderr.strip() or completed.stdout.strip(), file=sys.stderr)
        return 1
    print(completed.stdout.strip())
    print("i18n registry and catalogs are valid")
    print("i18n literal inventory is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
