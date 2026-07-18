"""Focused source/catalog contracts for the strict R5.1 i18n runtime."""

from __future__ import annotations

import errno
import importlib.util
import json
from pathlib import Path
import shutil
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "static" / "locales" / "registry.json"
CHECKER = ROOT / "scripts" / "check_i18n.py"
RUNTIME_SOURCE = ROOT / "frontend" / "ui-runtime" / "src" / "runtime.mjs"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_i18n", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _long_private_use_locale(last_subtag_length: int) -> str:
    subtags = [f"{index:02x}abcdef"[:8] for index in range(27)]
    return f"en-x-{'-'.join([*subtags, 'z' * last_subtag_length])}"


def test_r5_1_registry_has_equal_ru_en_catalog_topology() -> None:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))

    assert registry["schemaVersion"] == 1
    assert registry["defaultLocale"] in {"en", "ru"}
    assert {locale["code"] for locale in registry["locales"]} >= {"en", "ru"}
    assert len(registry["locales"]) <= 20
    assert set(registry["namespaces"]) == {
        "common",
        "auth",
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
    for locale in registry["locales"]:
        for namespace in registry["namespaces"]:
            assert (ROOT / "static" / "locales" / locale["code"] / f"{namespace}.json").is_file()
        free_tier_doc = ROOT / "static" / "locales" / locale["code"] / "free-tier-providers.md"
        assert free_tier_doc.is_file()
        assert free_tier_doc.read_text(encoding="utf-8").strip()


def test_r5_1_checker_rejects_missing_empty_and_invalid_free_tier_docs(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    locale_root = tmp_path / "static" / "locales"
    shutil.copytree(ROOT / "static" / "locales", locale_root)
    english_doc = locale_root / "en" / "free-tier-providers.md"
    russian_doc = locale_root / "ru" / "free-tier-providers.md"
    english_doc.write_text("# Free tier\n", encoding="utf-8")
    russian_doc.write_text("# Бесплатные модели\n", encoding="utf-8")

    english_doc.unlink()
    assert "missing free-tier document: en" in checker.validate_structure(tmp_path)

    english_doc.write_text("   \n", encoding="utf-8")
    assert "free-tier document must be non-empty: en" in checker.validate_structure(tmp_path)

    english_doc.write_bytes(b"\xff")
    assert "invalid UTF-8 free-tier document: en" in checker.validate_structure(tmp_path)


def test_r5_1_checker_rejects_unknown_default_locale(tmp_path: Path) -> None:
    checker = _load_checker()
    locale_root = tmp_path / "static" / "locales"
    locale_root.mkdir(parents=True)
    (locale_root / "registry.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "defaultLocale": "de",
                "locales": [
                    {"code": "en", "nativeLabel": "English", "dir": "ltr"},
                    {"code": "ru", "nativeLabel": "Русский", "dir": "ltr"},
                ],
                "namespaces": ["common"],
                "pageNamespaces": {"runtime": ["common"]},
            }
        ),
        encoding="utf-8",
    )

    errors = checker.validate_structure(tmp_path)

    assert "defaultLocale must reference a registered locale" in errors


def test_r5_1_runtime_pins_strict_i18next_options() -> None:
    source = RUNTIME_SOURCE.read_text(encoding="utf-8")

    for contract in (
        "defaultNS: false",
        "fallbackLng: false",
        "fallbackNS: false",
        'load: "currentOnly"',
        "nonExplicitSupportedLngs: false",
        "returnEmptyString: false",
        "returnObjects: false",
        "saveMissing: false",
        "ignoreJSONStructure: false",
    ):
        assert contract in source


def test_r5_1_python_locale_check_is_path_safe_and_accepts_extensions() -> None:
    checker = _load_checker()

    assert checker.is_safe_locale_segment("en-US-u-nu-latn")
    assert checker.is_safe_locale_segment("zh-Hant-TW")
    long_locale = (
        "en-u-ca-gregoryx-co-phonebk-hc-h23-kf-upper-kn-nu-latn-"
        "tz-usnyc-x-abcdefgh-ijklmnop"
    )
    assert len(long_locale) == 83
    assert checker.is_safe_locale_segment(long_locale)
    for unsafe in ("../en", "en/../../ru", r"en\ru", ".", "..", "en:ru", ""):
        assert not checker.is_safe_locale_segment(unsafe)


def test_r5_1_python_locale_check_enforces_255_utf8_byte_boundary() -> None:
    checker = _load_checker()
    boundary_locale = _long_private_use_locale(7)
    over_limit_locale = _long_private_use_locale(8)

    assert len(boundary_locale.encode("utf-8")) == 255
    assert len(over_limit_locale.encode("utf-8")) == 256
    assert checker.is_safe_locale_segment(boundary_locale)
    assert not checker.is_safe_locale_segment(over_limit_locale)


def test_r5_1_checker_rejects_overlong_locale_before_catalog_path_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checker = _load_checker()
    over_limit_locale = _long_private_use_locale(8)
    locale_root = tmp_path / "static" / "locales"
    locale_root.mkdir(parents=True)
    (locale_root / "registry.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "defaultLocale": "en",
                "locales": [
                    {"code": "en", "nativeLabel": "English", "dir": "ltr"},
                    {"code": "ru", "nativeLabel": "Русский", "dir": "ltr"},
                    {
                        "code": over_limit_locale,
                        "nativeLabel": "Over limit",
                        "dir": "ltr",
                    },
                ],
                "namespaces": sorted(checker.EXPECTED_NAMESPACES),
                "pageNamespaces": {"runtime": ["common"]},
            }
        ),
        encoding="utf-8",
    )
    inspected_paths: list[Path] = []
    original_is_file = Path.is_file

    def is_file(path: Path) -> bool:
        inspected_paths.append(path)
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", is_file)

    errors = checker.validate_structure(tmp_path)

    assert "locale entry 2 is not a path-safe locale segment" in errors
    assert all(over_limit_locale not in path.parts for path in inspected_paths)


def test_r5_1_checker_reports_catalog_enametoolong_without_crashing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checker = _load_checker()
    locale_root = tmp_path / "static" / "locales"
    locale_root.mkdir(parents=True)
    (locale_root / "registry.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "defaultLocale": "en",
                "locales": [
                    {"code": "en", "nativeLabel": "English", "dir": "ltr"},
                    {"code": "ru", "nativeLabel": "Русский", "dir": "ltr"},
                ],
                "namespaces": sorted(checker.EXPECTED_NAMESPACES),
                "pageNamespaces": {"runtime": ["common"]},
            }
        ),
        encoding="utf-8",
    )
    original_is_file = Path.is_file

    def is_file(path: Path) -> bool:
        if path.name == "common.json" and path.parent.name == "en":
            raise OSError(errno.ENAMETOOLONG, "File name too long")
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", is_file)

    errors = checker.validate_structure(tmp_path)

    assert "cannot inspect catalog path: en/common" in errors
    assert not any("File name too long" in error for error in errors)
