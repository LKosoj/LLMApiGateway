from __future__ import annotations

import io
import hashlib
import json
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import scripts.check_container_image as checker


@dataclass(frozen=True)
class LayerEntry:
    name: str
    kind: str
    content: bytes = b""
    mode: int = 0o644
    uid: int = 0
    gid: int = 0
    linkname: str = ""
    uname: str = ""
    gname: str = ""
    pax_headers: dict[str, str] | None = None


def _directory(
    name: str,
    *,
    mode: int = 0o755,
    uid: int = 0,
    gid: int = 0,
) -> LayerEntry:
    return LayerEntry(name, "directory", mode=mode, uid=uid, gid=gid)


def _file(
    name: str,
    content: bytes = b"payload",
    *,
    mode: int = 0o644,
    uid: int = 0,
    gid: int = 0,
) -> LayerEntry:
    return LayerEntry(name, "file", content, mode, uid, gid)


def _symlink(
    name: str,
    target: str,
    *,
    uid: int = 0,
    gid: int = 0,
) -> LayerEntry:
    return LayerEntry(name, "symlink", uid=uid, gid=gid, linkname=target)


def _hardlink(name: str, target: str) -> LayerEntry:
    return LayerEntry(name, "hardlink", linkname=target)


def _special(name: str) -> LayerEntry:
    return LayerEntry(name, "special")


def _layer_bytes(entries: list[LayerEntry], *, mtime: int = 1) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as layer_tar:
        for entry in entries:
            member = tarfile.TarInfo(entry.name)
            member.mode = entry.mode
            member.uid = entry.uid
            member.gid = entry.gid
            member.mtime = mtime
            member.linkname = entry.linkname
            member.uname = entry.uname
            member.gname = entry.gname
            member.pax_headers = entry.pax_headers or {}
            if entry.kind == "directory":
                member.type = tarfile.DIRTYPE
                layer_tar.addfile(member)
            elif entry.kind == "symlink":
                member.type = tarfile.SYMTYPE
                layer_tar.addfile(member)
            elif entry.kind == "hardlink":
                member.type = tarfile.LNKTYPE
                layer_tar.addfile(member)
            elif entry.kind == "special":
                member.type = tarfile.FIFOTYPE
                layer_tar.addfile(member)
            else:
                member.size = len(entry.content)
                layer_tar.addfile(member, io.BytesIO(entry.content))
    return payload.getvalue()


def _add_bytes(archive: tarfile.TarFile, name: str, content: bytes, *, mtime: int) -> None:
    member = tarfile.TarInfo(name)
    member.mode = 0o644
    member.size = len(content)
    member.mtime = mtime
    archive.addfile(member, io.BytesIO(content))


def _image_archive(
    path: Path,
    layers: list[list[LayerEntry]],
    *,
    config: dict[str, object] | None = None,
    repo_tag: str = "test:latest",
    mtime: int = 1,
) -> None:
    layer_payloads = [
        _layer_bytes(entries, mtime=mtime) for entries in layers
    ]
    layer_names = [f"layer-{index}.tar" for index in range(len(layer_payloads))]
    manifest = [{"Config": "config.json", "Layers": layer_names, "RepoTags": [repo_tag]}]
    image_config = {"config": _valid_runtime_config() if config is None else config}
    with tarfile.open(path, mode="w") as image_tar:
        _add_bytes(
            image_tar,
            "manifest.json",
            json.dumps(manifest, separators=(",", ":")).encode(),
            mtime=mtime,
        )
        _add_bytes(
            image_tar,
            "config.json",
            json.dumps(image_config, separators=(",", ":")).encode(),
            mtime=mtime,
        )
        for layer_name, payload in zip(layer_names, layer_payloads, strict=True):
            _add_bytes(image_tar, layer_name, payload, mtime=mtime)


def _valid_entries() -> list[LayerEntry]:
    source_entries = [
        _directory(path) if kind == "directory" else _file(path)
        for path, kind in sorted(checker._load_exact_source_allowlist().items())
    ]
    return [
        _directory("app"),
        *source_entries,
        _directory("app/examples"),
        _file("app/examples/free-tier-providers.md", b"example"),
        _file("app/main.py", b"print('gateway')\n"),
        _file("app/entrypoint.sh", b"#!/bin/sh\n", mode=0o555),
        _file("app/healthcheck.py", b"print('healthy')\n", mode=0o555),
        # Mounts replace these paths at runtime, so only these four paths may be
        # owned by the runtime identity or group-writable in the image.
        _directory("app/config", mode=0o770, uid=10001, gid=10001),
        _directory("app/db", mode=0o770, uid=10001, gid=10001),
        _directory("app/logs", mode=0o770, uid=10001, gid=10001),
        _directory("app/outputs", mode=0o770, uid=10001, gid=10001),
        _directory("app/outputs/images", mode=0o770, uid=10001, gid=10001),
        _directory("opt"),
        _directory("opt/venv"),
        _directory("opt/venv/bin"),
        _symlink("opt/venv/bin/python", "/usr/local/bin/python"),
        _directory("opt/cloakbrowser"),
        _directory("opt/cloakbrowser/chromium-146.0.7680.177.3"),
        _file(checker.BROWSER_BINARY_PATH, b"browser", mode=0o555),
        _directory("usr"),
        _directory("usr/local"),
        _directory("usr/local/bin"),
        _file("usr/local/bin/python", b"system-python", mode=0o555),
    ]


def _valid_runtime_config() -> dict[str, object]:
    return {
        "User": checker.EXPECTED_RUNTIME_USER,
        "WorkingDir": checker.EXPECTED_WORKING_DIR,
        "Entrypoint": list(checker.EXPECTED_ENTRYPOINT),
        "Cmd": list(checker.EXPECTED_COMMAND),
        "Healthcheck": dict(checker.EXPECTED_HEALTHCHECK),
        "Labels": {checker.OCI_VERSION_LABEL: "1.10.0"},
        "Env": [
            "PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
            *(f"{key}={value}" for key, value in checker.ESSENTIAL_ENV.items()),
        ],
    }


def _replace_entry(
    entries: list[LayerEntry],
    path: str,
    **changes: object,
) -> list[LayerEntry]:
    return [replace(entry, **changes) if entry.name == path else entry for entry in entries]


def test_manifest_is_stable_and_budgets_are_exact(tmp_path: Path) -> None:
    first_archive = tmp_path / "first.tar"
    second_archive = tmp_path / "second.tar"
    entries = _valid_entries()
    _image_archive(first_archive, [entries], mtime=1)
    midpoint = len(entries) // 2
    _image_archive(
        second_archive,
        [list(reversed(entries[:midpoint])), list(reversed(entries[midpoint:]))],
        mtime=2,
    )

    first = checker.inspect_image_archive(first_archive)
    second = checker.inspect_image_archive(second_archive)

    assert first == second
    assert checker.MAX_IMAGE_SIZE_BYTES == 2_415_919_104
    assert checker.MAX_APP_PAYLOAD_BYTES == 16_777_216
    assert first["runtime_config"]["user"] == "10001:10001"
    assert (
        first["runtime_config"]["environment"]["GATEWAY_OUTPUTS_DIR"]
        == "/app/outputs"
    )
    assert first["schema_version"] == 2
    assert "image_size_bytes" not in json.dumps(first)
    assert [entry["path"] for entry in first["app_entries"]] == sorted(
        entry["path"] for entry in first["app_entries"]
    )


def test_canonical_layer_root_member_is_accepted(tmp_path: Path) -> None:
    archive = tmp_path / "root-member.tar"
    _image_archive(archive, [[_directory("."), *_valid_entries()]])

    manifest = checker.inspect_image_archive(archive)

    assert manifest["schema_version"] == 2


def test_every_allowlisted_source_path_is_required_in_the_final_image(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "missing-allowlisted-source.tar"
    entries = [
        entry
        for entry in _valid_entries()
        if entry.name != "app/static/login.html"
    ]
    _image_archive(archive, [entries])

    with pytest.raises(checker.CheckFailure, match="exact Docker source path"):
        checker.inspect_image_archive(archive)


def test_every_allowlisted_source_path_keeps_its_declared_kind(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "wrong-allowlisted-source-kind.tar"
    entries = _replace_entry(
        _valid_entries(),
        "app/static/login.html",
        kind="directory",
        content=b"",
    )
    _image_archive(archive, [entries])

    with pytest.raises(checker.CheckFailure, match="exact Docker source path"):
        checker.inspect_image_archive(archive)


def test_checker_loads_the_exact_source_allowlist() -> None:
    allowlist = checker._load_exact_source_allowlist()

    assert allowlist["app/llm_gateway_core/config/environment.py"] == "file"
    assert "app/llm_gateway_core/config/placeholder_secrets.py" in allowlist
    assert "app/static/login.html" in allowlist
    for runtime_path in (
        "llm_gateway_core/api/v1/chat_dialects.py",
        "llm_gateway_core/api/v1/chat_dispatch.py",
        "llm_gateway_core/api/v1/chat_streaming.py",
        "llm_gateway_core/api/v1/web_adapters.py",
        "llm_gateway_core/api/v1/web_extraction.py",
        "llm_gateway_core/api/v1/web_research_orchestration.py",
        "llm_gateway_core/api/v1/web_safe_fetch.py",
        "llm_gateway_core/config/loading.py",
        "llm_gateway_core/config/schemas.py",
        "llm_gateway_core/config/validation.py",
        "static/locales/en/free-tier-providers.md",
        "static/locales/ru/free-tier-providers.md",
        "static/login.js",
        "static/shared-nav.css",
    ):
        assert allowlist[f"app/{runtime_path}"] == "file"
    for module in (
        "_image_storage_cli_archive.py",
        "_image_storage_cli_copy.py",
        "_image_storage_cli_inventory.py",
    ):
        exact_path = f"app/llm_gateway_core/services/{module}"
        assert allowlist[exact_path] == "file"
        assert not checker._is_allowed_app_path(
            f"{exact_path}/nested/rogue.py",
            allowlist,
        )
    assert "app/llm_gateway_core/services/_image_storage_cli_rogue.py" not in allowlist
    assert "app/llm_gateway_core/rogue.py" not in allowlist
    assert "app/static/rogue.html" not in allowlist


@pytest.mark.parametrize(
    "dockerignore_text",
    (
        "!llm_gateway_core/**\n!static/\n",
        "!llm_gateway_core/\nllm_gateway_core/*\n"
        "!static/\nstatic/*\n!static/missing-runtime-file.html\n",
        "!llm_gateway_core/\n!llm_gateway_core/__init__.py\n"
        "llm_gateway_core/*\n!static/\nstatic/*\n",
        "!llm_gateway_core/\nllm_gateway_core/*\n"
        "!llm_gateway_core/__init__.py\n!static/\nstatic/*\n",
    ),
)
def test_checker_fails_closed_on_a_non_exact_source_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dockerignore_text: str,
) -> None:
    dockerignore = tmp_path / ".dockerignore"
    dockerignore.write_text(dockerignore_text, encoding="utf-8")
    monkeypatch.setattr(checker, "DOCKERIGNORE_PATH", dockerignore)

    with pytest.raises(checker.CheckFailure, match="Docker source allowlist"):
        checker._load_exact_source_allowlist()


def test_checker_fails_closed_when_the_source_allowlist_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checker, "DOCKERIGNORE_PATH", tmp_path / "missing")

    with pytest.raises(checker.CheckFailure, match="source allowlist is unavailable"):
        checker._load_exact_source_allowlist()


@pytest.mark.parametrize(
    "path",
    (
        "app/llm_gateway_core/.arbitrary-hidden",
        "app/static/.hidden/asset.js",
    ),
)
def test_checker_rejects_every_hidden_source_path_component(path: str) -> None:
    assert not checker._is_allowed_app_path(path, {path: "file"})


def test_wrong_runtime_user_fails(tmp_path: Path) -> None:
    archive = tmp_path / "wrong-user.tar"
    config = _valid_runtime_config()
    config["User"] = "999:999"
    _image_archive(archive, [_valid_entries()], config=config)

    with pytest.raises(checker.CheckFailure, match="runtime image user contract is invalid"):
        checker.inspect_image_archive(archive)


def test_forbidden_path_fails_even_when_a_later_layer_deletes_it(tmp_path: Path) -> None:
    archive = tmp_path / "forbidden-history.tar"
    first_layer = [*_valid_entries(), _file("app/tests/secret.py", b"secret")]
    second_layer = [_file("app/.wh.tests", b"")]
    _image_archive(archive, [first_layer, second_layer])

    with pytest.raises(checker.CheckFailure, match="forbidden application path"):
        checker.inspect_image_archive(archive)


@pytest.mark.parametrize(
    "forbidden_path",
    (
        "app/llm_gateway_core/tests/helper.py",
        "app/llm_gateway_core/test_helper.py",
    ),
)
def test_application_tests_are_forbidden_in_every_layer(
    tmp_path: Path,
    forbidden_path: str,
) -> None:
    archive = tmp_path / "application-tests.tar"
    _image_archive(archive, [[*_valid_entries(), _file(forbidden_path)]])

    with pytest.raises(checker.CheckFailure, match="forbidden application path"):
        checker.inspect_image_archive(archive)


@pytest.mark.parametrize(
    "forbidden_path",
    (
        "app/llm_gateway_core/diagnostics/canary.txt",
        "app/static/cache/canary.js",
        "app/static/.hidden/canary.js",
        "app/static/node_modules/canary.js",
        "app/llm_gateway_core/module_test.py",
        "app/static/runtime.log",
        "app/static/runtime.prof",
        "app/static/runtime.trace",
        "app/static/runtime.db",
        "app/static/runtime.sqlite",
        "app/static/runtime.sqlite3",
        "app/llm_gateway_core/native.pyd",
        "app/static/.env.local",
        "app/static/.netrc",
        "app/static/.npmrc",
        "app/static/.pypirc",
        "app/static/credentials.json",
        "app/static/secrets.json",
        "app/static/private.key",
        "app/static/private.pem",
        "app/static/private.p12",
        "app/static/private.pfx",
        "app/static/.coverage",
        "app/static/runtime.db/payload.js",
        "app/static/runtime.log/payload.js",
        "app/static/private.pem/payload.js",
        "app/static/secrets.json/payload.js",
        "app/static/test_probe.py/payload.js",
        "app/static/module.pyc/payload.js",
        "app/static/dev-diagnostic.txt",
        "app/llm_gateway_core/debug_dump.txt",
        "app/llm_gateway_core/id_rsa",
        "app/llm_gateway_core/service-account.json",
        "app/static/playwright-report/index.html",
        "app/llm_gateway_core/.arbitrary-hidden",
        "app/static/.arbitrary-hidden",
        "app/llm_gateway_core/rogue.py",
        "app/static/rogue.html",
    ),
)
def test_nested_non_runtime_source_canaries_are_forbidden(
    tmp_path: Path,
    forbidden_path: str,
) -> None:
    archive = tmp_path / "nested-source-canary.tar"
    _image_archive(archive, [[*_valid_entries(), _file(forbidden_path)]])

    with pytest.raises(checker.CheckFailure, match="forbidden application path"):
        checker.inspect_image_archive(archive)


@pytest.mark.parametrize(
    "allowed_path",
    (
        "app/llm_gateway_core/config/placeholder_secrets.py",
        "app/llm_gateway_core/db/tokens_usage_db.py",
        "app/llm_gateway_core/utils/html_cache.py",
        "app/static/locales/en/common.json",
    ),
)
def test_legitimate_current_source_paths_remain_allowed(
    tmp_path: Path,
    allowed_path: str,
) -> None:
    archive = tmp_path / "legitimate-source.tar"
    entries = [
        *_valid_entries(),
        _file(allowed_path),
    ]
    _image_archive(archive, [entries])

    manifest = checker.inspect_image_archive(archive)

    assert any(
        entry["path"] == f"/{allowed_path}"
        for entry in manifest["app_entries"]
    )


def test_whiteouts_apply_before_same_layer_entries_regardless_of_tar_order(
    tmp_path: Path,
) -> None:
    first_archive = tmp_path / "ordinary-first.tar"
    second_archive = tmp_path / "whiteout-first.tar"
    base = [
        *_valid_entries(),
        _file("app/llm_gateway_core/build_info.py", b"lower"),
    ]
    replacement = _file("app/llm_gateway_core/build_info.py", b"upper")
    whiteout = _file("app/llm_gateway_core/.wh.build_info.py", b"")
    _image_archive(first_archive, [base, [replacement, whiteout]])
    _image_archive(second_archive, [base, [whiteout, replacement]])

    first = checker.inspect_image_archive(first_archive)
    second = checker.inspect_image_archive(second_archive)

    assert first == second
    replaced = next(
        entry
        for entry in first["app_entries"]
        if entry["path"] == "/app/llm_gateway_core/build_info.py"
    )
    assert replaced["sha256"] == hashlib.sha256(b"upper").hexdigest()


def test_whiteout_regular_file_payload_is_scanned_and_secret_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "PRIVATE-R3-3-SENTINEL"
    archive = tmp_path / "sentinel-history.tar"
    output = tmp_path / "manifest.json"
    first_layer = [
        *_valid_entries(),
        _file("app/llm_gateway_core/version.py", b"temporary"),
    ]
    second_layer = [
        _file(
            "app/llm_gateway_core/.wh.version.py",
            f"prefix-{secret}-suffix".encode(),
        )
    ]
    _image_archive(archive, [first_layer, second_layer])
    monkeypatch.setattr(checker, "_save_image", lambda _image, target: shutil.copyfile(archive, target))
    monkeypatch.setattr(checker, "_inspect_image_size", lambda _image: 1)

    assert checker.main(["test:image", "--sentinel", secret, "--manifest-out", str(output)]) == 1
    captured = capsys.readouterr()
    assert "forbidden sentinel bytes detected" in captured.err
    assert secret not in captured.err
    assert not output.exists()


@pytest.mark.parametrize("source", ("manifest", "config"))
def test_raw_manifest_and_config_bytes_are_scanned_for_sentinels(
    tmp_path: Path,
    source: str,
) -> None:
    secret = "PRIVATE-RAW-METADATA-SENTINEL"
    archive = tmp_path / "raw-metadata.tar"
    config = _valid_runtime_config()
    repo_tag = "test:latest"
    if source == "manifest":
        repo_tag = secret
    else:
        config["Labels"] = {checker.OCI_VERSION_LABEL: secret}
    _image_archive(archive, [_valid_entries()], config=config, repo_tag=repo_tag)

    with pytest.raises(checker.CheckFailure, match="forbidden sentinel bytes") as caught:
        checker.inspect_image_archive(archive, sentinels=(secret.encode(),))
    assert secret not in str(caught.value)


@pytest.mark.parametrize("field", ("name", "linkname", "uname", "gname", "pax"))
def test_all_tarinfo_text_metadata_is_scanned_for_sentinels(
    tmp_path: Path,
    field: str,
) -> None:
    secret = "PRIVATE-TARINFO-SENTINEL"
    archive = tmp_path / "tarinfo-metadata.tar"
    entry = _file("usr/share/metadata-probe", b"clean")
    if field == "name":
        entry = replace(entry, name=f"usr/share/{secret}")
    elif field == "linkname":
        entry = _symlink("usr/share/metadata-probe", f"/usr/share/{secret}")
    elif field == "uname":
        entry = replace(entry, uname=secret)
    elif field == "gname":
        entry = replace(entry, gname=secret)
    else:
        entry = replace(entry, pax_headers={"comment": secret})
    _image_archive(archive, [[*_valid_entries(), entry]])

    with pytest.raises(checker.CheckFailure, match="forbidden sentinel bytes") as caught:
        checker.inspect_image_archive(archive, sentinels=(secret.encode(),))
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    ("path", "changes", "message"),
    [
        ("app/main.py", {"uid": 10001}, "not root-owned"),
        ("app/main.py", {"mode": 0o664}, "writable by the runtime user"),
        ("opt/venv/bin/python", {"gid": 10001}, "not root-owned"),
        (checker.BROWSER_BINARY_PATH, {"mode": 0o575}, "writable by the runtime user"),
    ],
)
def test_protected_paths_must_be_root_owned_and_not_runtime_writable(
    tmp_path: Path,
    path: str,
    changes: dict[str, int],
    message: str,
) -> None:
    archive = tmp_path / "permissions.tar"
    _image_archive(archive, [_replace_entry(_valid_entries(), path, **changes)])

    with pytest.raises(checker.CheckFailure, match=message):
        checker.inspect_image_archive(archive)


@pytest.mark.parametrize(
    ("mountpoint", "changes"),
    [
        ("app/config", {"uid": 0}),
        ("app/db", {"gid": 0}),
        ("app/logs", {"mode": 0o750}),
        ("app/outputs", {"mode": 0o775}),
        ("app/outputs/images", {"uid": 0}),
        ("app/outputs/images", {"gid": 0}),
        ("app/outputs/images", {"mode": 0o750}),
    ],
)
def test_mountpoints_have_exact_runtime_identity_and_mode(
    tmp_path: Path,
    mountpoint: str,
    changes: dict[str, int],
) -> None:
    archive = tmp_path / "mountpoint.tar"
    _image_archive(archive, [_replace_entry(_valid_entries(), mountpoint, **changes)])

    with pytest.raises(checker.CheckFailure, match="writable mountpoint contract is invalid"):
        checker.inspect_image_archive(archive)


def test_outputs_images_runtime_directory_is_required(tmp_path: Path) -> None:
    archive = tmp_path / "missing-output-images.tar"
    entries = [
        entry
        for entry in _valid_entries()
        if entry.name != "app/outputs/images"
    ]
    _image_archive(archive, [entries])

    with pytest.raises(checker.CheckFailure, match="required image directory"):
        checker.inspect_image_archive(archive)


@pytest.mark.parametrize(
    "special_path",
    (
        "app/main.py",
        "opt/venv/runtime.pipe",
        "opt/cloakbrowser/runtime.pipe",
    ),
)
def test_special_files_are_forbidden_in_every_protected_root(
    tmp_path: Path,
    special_path: str,
) -> None:
    archive = tmp_path / "special.tar"
    _image_archive(archive, [[*_valid_entries(), _special(special_path)]])

    with pytest.raises(checker.CheckFailure, match="protected image contains a special file"):
        checker.inspect_image_archive(archive)


@pytest.mark.parametrize(
    "path",
    ("app/entrypoint.sh", "app/healthcheck.py", checker.BROWSER_BINARY_PATH),
)
def test_required_runtime_files_must_be_executable(tmp_path: Path, path: str) -> None:
    archive = tmp_path / "executable.tar"
    _image_archive(archive, [_replace_entry(_valid_entries(), path, mode=0o444)])

    with pytest.raises(checker.CheckFailure, match="required image executable is not executable"):
        checker.inspect_image_archive(archive)


@pytest.mark.parametrize(
    ("missing_path", "message"),
    [
        (checker.BROWSER_BINARY_PATH, "bundled browser executable is missing"),
        (checker.VENV_PYTHON_PATH, "virtual-environment Python is missing"),
    ],
)
def test_required_browser_and_venv_python_must_be_present(
    tmp_path: Path,
    missing_path: str,
    message: str,
) -> None:
    archive = tmp_path / "missing-runtime.tar"
    entries = [entry for entry in _valid_entries() if entry.name != missing_path]
    _image_archive(archive, [entries])

    with pytest.raises(checker.CheckFailure, match=message):
        checker.inspect_image_archive(archive)


@pytest.mark.parametrize(
    "unsafe_entry",
    [
        _symlink("opt/venv/bin/escape", "../../../app/config/secret"),
        _hardlink("opt/venv/bin/escape", "app/logs/secret"),
    ],
)
def test_protected_links_cannot_resolve_into_writable_mounts(
    tmp_path: Path,
    unsafe_entry: LayerEntry,
) -> None:
    archive = tmp_path / "unsafe-link.tar"
    _image_archive(archive, [[*_valid_entries(), unsafe_entry]])

    with pytest.raises(checker.CheckFailure, match="link resolves into a writable mount"):
        checker.inspect_image_archive(archive)


def test_indirect_link_resolution_cannot_reach_a_writable_mount(tmp_path: Path) -> None:
    archive = tmp_path / "indirect-link.tar"
    entries = _replace_entry(_valid_entries(), checker.VENV_PYTHON_PATH, linkname="bridge")
    entries.append(_symlink("opt/venv/bin/bridge", "../../../app/db/python"))
    _image_archive(archive, [entries])

    with pytest.raises(checker.CheckFailure, match="link resolves into a writable mount"):
        checker.inspect_image_archive(archive)


@pytest.mark.parametrize(
    "target",
    (
        "/tmp/secret",
        "/dev/null",
        "/proc/self/status",
        "/run/secret",
        "/sys/kernel",
        "//app/config/secret",
    ),
)
def test_protected_links_reject_dynamic_and_multi_slash_mount_targets(
    tmp_path: Path,
    target: str,
) -> None:
    archive = tmp_path / "dynamic-link.tar"
    _image_archive(
        archive,
        [[*_valid_entries(), _symlink("opt/venv/bin/escape", target)]],
    )

    with pytest.raises(checker.CheckFailure, match="writable mount or dynamic runtime root"):
        checker.inspect_image_archive(archive)


def test_protected_link_follows_unprotected_ancestor_symlink_into_mount(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "ancestor-link.tar"
    entries = [
        *_valid_entries(),
        _symlink("usr/share/bridge", "/app/config"),
        _symlink("opt/venv/bin/escape", "/usr/share/bridge/secret"),
    ]
    _image_archive(archive, [entries])

    with pytest.raises(checker.CheckFailure, match="writable mount or dynamic runtime root"):
        checker.inspect_image_archive(archive)


def test_dotdot_is_processed_after_ancestor_symlink_expansion(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "ancestor-dotdot-link.tar"
    entries = [
        *_valid_entries(),
        _symlink("usr/share/bridge", "/app/config/nested"),
        _symlink(
            "opt/venv/bin/escape",
            "/usr/share/bridge/../payload",
        ),
    ]
    _image_archive(archive, [entries])

    with pytest.raises(checker.CheckFailure, match="writable mount or dynamic runtime root"):
        checker.inspect_image_archive(archive)


def test_protected_link_cycle_fails_closed(tmp_path: Path) -> None:
    archive = tmp_path / "link-cycle.tar"
    entries = [
        *_valid_entries(),
        _symlink("opt/venv/bin/first", "second"),
        _symlink("opt/venv/bin/second", "first"),
    ]
    _image_archive(archive, [entries])

    with pytest.raises(checker.CheckFailure, match="protected image contains a link cycle"):
        checker.inspect_image_archive(archive)


def test_venv_python_symlink_to_read_only_system_python_is_allowed(tmp_path: Path) -> None:
    archive = tmp_path / "system-python-link.tar"
    _image_archive(archive, [_valid_entries()])

    manifest = checker.inspect_image_archive(archive)

    assert manifest["protected_roots"]["/opt/venv"]["entry_count"] >= 3


def test_venv_python_resolves_a_valid_read_only_usr_local_symlink_chain(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "system-python-chain.tar"
    entries = [
        entry for entry in _valid_entries() if entry.name != "usr/local/bin/python"
    ]
    entries.extend(
        (
            _symlink("usr/local/bin/python", "python-real"),
            _file("usr/local/bin/python-real", b"system-python", mode=0o555),
        )
    )
    _image_archive(archive, [entries])

    manifest = checker.inspect_image_archive(archive)

    assert manifest["runtime_config"]["environment"]["PATH"].startswith(
        "/opt/venv/bin:"
    )


def test_venv_python_cannot_use_safe_decoy_after_writable_symlink_dotdot(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "venv-python-dotdot-bypass.tar"
    entries = _replace_entry(
        _valid_entries(),
        checker.VENV_PYTHON_PATH,
        linkname="/usr/share/bridge/../python",
    )
    entries.extend(
        (
            _symlink("usr/share/bridge", "/app/config/nested"),
            _file("usr/share/python", b"safe-decoy", mode=0o555),
        )
    )
    _image_archive(archive, [entries])

    with pytest.raises(checker.CheckFailure, match="writable mount or dynamic runtime root"):
        checker.inspect_image_archive(archive)


@pytest.mark.parametrize(
    "python_entry",
    (
        _symlink(
            checker.VENV_PYTHON_PATH,
            "/usr/local/bin/../bin/python",
        ),
        _symlink(
            checker.VENV_PYTHON_PATH,
            "../../../usr/local/./bin/python",
        ),
        _hardlink(
            checker.VENV_PYTHON_PATH,
            "usr/local/bin/../bin/python",
        ),
    ),
)
def test_safe_dotdot_targets_resolve_to_read_only_system_python(
    tmp_path: Path,
    python_entry: LayerEntry,
) -> None:
    archive = tmp_path / "safe-dotdot-python.tar"
    entries = [
        entry
        for entry in _valid_entries()
        if entry.name != checker.VENV_PYTHON_PATH
    ]
    entries.append(python_entry)
    _image_archive(archive, [entries])

    manifest = checker.inspect_image_archive(archive)

    assert manifest["runtime_config"]["user"] == checker.EXPECTED_RUNTIME_USER


@pytest.mark.parametrize("target_mode", (None, 0o444))
def test_venv_python_requires_a_present_executable_read_only_target(
    tmp_path: Path,
    target_mode: int | None,
) -> None:
    archive = tmp_path / "invalid-system-python.tar"
    entries = _valid_entries()
    if target_mode is None:
        entries = [entry for entry in entries if entry.name != "usr/local/bin/python"]
    else:
        entries = _replace_entry(entries, "usr/local/bin/python", mode=target_mode)
    _image_archive(archive, [entries])

    with pytest.raises(checker.CheckFailure, match="virtual-environment Python is not executable"):
        checker.inspect_image_archive(archive)


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("WorkingDir", "/tmp/PRIVATE-VALUE", "working-directory contract"),
        ("Entrypoint", ["/tmp/PRIVATE-VALUE"], "entrypoint contract"),
        ("Cmd", ["sh", "PRIVATE-VALUE"], "command contract"),
        ("Healthcheck", {"Test": ["NONE"]}, "healthcheck contract"),
        ("Labels", {checker.OCI_VERSION_LABEL: ""}, "OCI version label"),
    ],
)
def test_runtime_config_fields_are_exact_and_errors_do_not_echo_values(
    tmp_path: Path,
    field: str,
    bad_value: object,
    message: str,
) -> None:
    archive = tmp_path / "runtime-config.tar"
    config = _valid_runtime_config()
    config[field] = bad_value
    _image_archive(archive, [_valid_entries()], config=config)

    with pytest.raises(checker.CheckFailure, match=message) as caught:
        checker.inspect_image_archive(archive)
    assert "PRIVATE-VALUE" not in str(caught.value)


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("APP_DIR", "/tmp/private"),
        ("PATH", "/usr/bin:/bin"),
        ("CLOAKBROWSER_BINARY_PATH", "/tmp/private/chrome"),
        ("CLOAKBROWSER_CACHE_DIR", "/tmp/private/cache"),
        ("CLOAKBROWSER_AUTO_UPDATE", "true"),
        ("GATEWAY_OUTPUTS_DIR", "/tmp/private/outputs"),
        ("PYTHONDONTWRITEBYTECODE", "0"),
    ],
)
def test_essential_runtime_environment_is_exact(
    tmp_path: Path,
    key: str,
    bad_value: str,
) -> None:
    archive = tmp_path / "runtime-environment.tar"
    config = _valid_runtime_config()
    config["Env"] = [
        f"{item.partition('=')[0]}={bad_value}"
        if item.partition("=")[0] == key
        else item
        for item in config["Env"]
    ]
    _image_archive(archive, [_valid_entries()], config=config)

    with pytest.raises(checker.CheckFailure, match="environment contract is invalid"):
        checker.inspect_image_archive(archive)


def test_image_and_application_payload_budgets_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "budgets.tar"
    _image_archive(archive, [_valid_entries()])

    monkeypatch.setattr(checker, "MAX_APP_PAYLOAD_BYTES", 1)
    with pytest.raises(checker.CheckFailure, match="application payload exceeds"):
        checker.inspect_image_archive(archive)


def test_cli_image_budget_uses_docker_inspect_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0, b"12345\n", b"")

    monkeypatch.setattr(checker.subprocess, "run", run)

    assert checker._inspect_image_size("test:image") == 12345
    assert captured == [
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Size}}",
        "test:image",
    ]


def test_cli_rejects_oversized_image_before_docker_save(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        checker,
        "_inspect_image_size",
        lambda _image: checker.MAX_IMAGE_SIZE_BYTES + 1,
    )
    monkeypatch.setattr(
        checker,
        "_save_image",
        lambda *_args: pytest.fail("docker save must not run for an oversized image"),
    )

    assert checker.main(["private-image-name"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "container image check failed: image exceeds the configured size budget\n"
    assert "private-image-name" not in captured.err


def test_cli_writes_byte_stable_normalized_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "image.tar"
    first_output = tmp_path / "first.json"
    second_output = tmp_path / "second.json"
    _image_archive(archive, [_valid_entries()])
    monkeypatch.setattr(checker, "_save_image", lambda _image, target: shutil.copyfile(archive, target))
    sizes = iter((123, 456))
    monkeypatch.setattr(checker, "_inspect_image_size", lambda _image: next(sizes))

    assert checker.main(["clean:image", "--manifest-out", str(first_output)]) == 0
    assert checker.main(["dirty:image", "--manifest-out", str(second_output)]) == 0
    capsys.readouterr()

    assert first_output.read_bytes() == second_output.read_bytes()
    manifest = json.loads(first_output.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert "image_size" not in first_output.read_text(encoding="utf-8")
