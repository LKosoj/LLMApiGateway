from __future__ import annotations

import hashlib
import logging
import os
import socket
from pathlib import Path

import pytest

import llm_gateway_core.config.config_sources as config_store
from llm_gateway_core.config.config_store import (
    ConfigDocument,
    ConfigFile,
    ConfigSourceBundle,
    ConfigSourceError,
)


FILENAMES = {
    ConfigFile.PROVIDERS: "providers.json",
    ConfigFile.FALLBACK_RULES: "models_fallback_rules.json",
    ConfigFile.MODEL_RULES: "models_model_rules.json",
    ConfigFile.OPERATION_RULES: "models_operation_rules.json",
    ConfigFile.FUSION_RULES: "models_fusion_rules.json",
    ConfigFile.ROUTER_RULES: "models_router_rules.json",
}
MANDATORY_FILES = {ConfigFile.PROVIDERS, ConfigFile.FALLBACK_RULES}


def _write_sources(root: Path, *, include_optional: bool = True) -> dict[ConfigFile, bytes]:
    contents: dict[ConfigFile, bytes] = {}
    for index, (config_file, filename) in enumerate(FILENAMES.items()):
        if not include_optional and config_file not in MANDATORY_FILES:
            continue
        content = f"source-{index}\n".encode()
        (root / filename).write_bytes(content)
        contents[config_file] = content
    return contents


def test_capture_reads_all_six_sources_with_exact_metadata(tmp_path: Path) -> None:
    contents = _write_sources(tmp_path)

    bundle = ConfigSourceBundle.capture(tmp_path)

    assert set(bundle.documents) == set(ConfigFile)
    for config_file, content in contents.items():
        document = bundle[config_file]
        source_stat = (tmp_path / FILENAMES[config_file]).stat()
        assert document.exists is True
        assert document.path == tmp_path / FILENAMES[config_file]
        assert document.content == content
        assert document.digest == hashlib.sha256(content).hexdigest()
        assert document.metadata is not None
        assert document.metadata.mode == source_stat.st_mode
        assert document.metadata.uid == source_stat.st_uid
        assert document.metadata.gid == source_stat.st_gid
        assert document.metadata.device == source_stat.st_dev
        assert document.metadata.inode == source_stat.st_ino
        assert document.metadata.size == source_stat.st_size
        assert document.metadata.mtime_ns == source_stat.st_mtime_ns
        assert document.metadata.ctime_ns == source_stat.st_ctime_ns
        assert document.metadata.link_count == 1


def test_capture_represents_missing_optional_sources_separately(tmp_path: Path) -> None:
    _write_sources(tmp_path, include_optional=False)

    bundle = ConfigSourceBundle.capture(tmp_path)

    for config_file in set(ConfigFile) - MANDATORY_FILES:
        document = bundle[config_file]
        assert document.exists is False
        assert document.content is None
        assert document.digest is None
        assert document.metadata is None


@pytest.mark.parametrize("config_file", sorted(MANDATORY_FILES, key=lambda item: item.value))
def test_capture_rejects_missing_mandatory_source(
    tmp_path: Path,
    config_file: ConfigFile,
) -> None:
    _write_sources(tmp_path, include_optional=False)
    (tmp_path / FILENAMES[config_file]).unlink()

    with pytest.raises(ConfigSourceError, match="required config source is missing") as error:
        ConfigSourceBundle.capture(tmp_path)

    assert error.value.config_file is config_file


def test_capture_keeps_zero_byte_optional_source_distinct_from_missing(tmp_path: Path) -> None:
    _write_sources(tmp_path, include_optional=False)
    (tmp_path / FILENAMES[ConfigFile.MODEL_RULES]).write_bytes(b"")

    bundle = ConfigSourceBundle.capture(tmp_path)

    document = bundle[ConfigFile.MODEL_RULES]
    assert document.exists is True
    assert document.content == b""
    assert document.digest == hashlib.sha256(b"").hexdigest()
    assert document.metadata is not None
    assert document.metadata.size == 0


def test_capture_normalizes_relative_and_absolute_overrides_lexically(tmp_path: Path) -> None:
    _write_sources(tmp_path, include_optional=False)
    nested = tmp_path / "nested"
    nested.mkdir()
    operation_path = tmp_path / FILENAMES[ConfigFile.OPERATION_RULES]
    operation_path.write_bytes(b"operation")

    relative_bundle = ConfigSourceBundle.capture(
        tmp_path,
        overrides={ConfigFile.OPERATION_RULES: "nested/../models_operation_rules.json"},
    )
    absolute_bundle = ConfigSourceBundle.capture(
        tmp_path,
        overrides={ConfigFile.OPERATION_RULES: operation_path},
    )

    assert relative_bundle[ConfigFile.OPERATION_RULES].path == operation_path
    assert absolute_bundle[ConfigFile.OPERATION_RULES].path == operation_path
    assert relative_bundle[ConfigFile.OPERATION_RULES].content == b"operation"
    assert absolute_bundle[ConfigFile.OPERATION_RULES].content == b"operation"


def test_capture_rejects_normalized_path_collision_before_reading(tmp_path: Path) -> None:
    _write_sources(tmp_path, include_optional=False)

    with pytest.raises(ConfigSourceError, match="config path collision"):
        ConfigSourceBundle.capture(
            tmp_path,
            overrides={ConfigFile.MODEL_RULES: "./providers.json"},
        )


def test_capture_canonicalizes_double_slash_root_before_collision_check(tmp_path: Path) -> None:
    _write_sources(tmp_path, include_optional=False)
    providers_path = tmp_path / FILENAMES[ConfigFile.PROVIDERS]
    double_slash_path = f"//{str(providers_path).lstrip('/')}"

    with pytest.raises(ConfigSourceError, match="config path collision"):
        ConfigSourceBundle.capture(
            tmp_path,
            overrides={ConfigFile.MODEL_RULES: double_slash_path},
        )


def test_bundle_construction_uses_same_root_canonicalization(tmp_path: Path) -> None:
    _write_sources(tmp_path, include_optional=False)
    captured = ConfigSourceBundle.capture(tmp_path)
    documents = dict(captured.documents)
    providers_path = documents[ConfigFile.PROVIDERS].path
    documents[ConfigFile.MODEL_RULES] = ConfigDocument.from_bytes(
        ConfigFile.MODEL_RULES,
        Path(f"//{str(providers_path).lstrip('/')}"),
        b"candidate",
    )

    with pytest.raises(ValueError, match="path collision"):
        ConfigSourceBundle(documents)


@pytest.mark.parametrize("config_file", sorted(MANDATORY_FILES, key=lambda item: item.value))
def test_bundle_construction_rejects_missing_mandatory_document(
    tmp_path: Path,
    config_file: ConfigFile,
) -> None:
    _write_sources(tmp_path, include_optional=False)
    captured = ConfigSourceBundle.capture(tmp_path)
    documents = dict(captured.documents)
    documents[config_file] = ConfigDocument.missing(
        config_file,
        documents[config_file].path,
    )

    with pytest.raises(ValueError, match="mandatory config source is missing"):
        ConfigSourceBundle(documents)


def test_capture_warns_on_hard_linked_source(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_sources(tmp_path, include_optional=False)
    # Hard-link to a file outside the tracked config-file set so this only
    # exercises the single-file nlink warning, not the unrelated (and still
    # fatal) cross-config-file inode-collision guard in capture(): linking
    # two tracked files (e.g. providers.json and models_model_rules.json)
    # together would trip that guard instead of the one under test here.
    external = tmp_path / "external-hardlink-source.json"
    external_bytes = b'{"linked":true}\n'
    external.write_bytes(external_bytes)
    os.link(external, tmp_path / FILENAMES[ConfigFile.MODEL_RULES])

    with caplog.at_level(logging.WARNING):
        bundle = ConfigSourceBundle.capture(tmp_path)

    document = bundle[ConfigFile.MODEL_RULES]
    assert document.content == external_bytes
    assert document.metadata is not None
    assert document.metadata.link_count == 2
    assert "hard-linked" in caplog.text


def test_capture_rejects_final_symlink(tmp_path: Path) -> None:
    _write_sources(tmp_path, include_optional=False)
    target = tmp_path / "target.json"
    target.write_bytes(b"target")
    (tmp_path / FILENAMES[ConfigFile.MODEL_RULES]).symlink_to(target)

    with pytest.raises(ConfigSourceError, match="path is unsafe"):
        ConfigSourceBundle.capture(tmp_path)


def test_capture_follows_parent_symlink(tmp_path: Path) -> None:
    _write_sources(tmp_path, include_optional=False)
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    rules_bytes = b"rules\n"
    (real_parent / "rules.json").write_bytes(rules_bytes)
    (tmp_path / "linked").symlink_to(real_parent, target_is_directory=True)

    bundle = ConfigSourceBundle.capture(
        tmp_path,
        overrides={ConfigFile.MODEL_RULES: "linked/rules.json"},
    )

    assert bundle[ConfigFile.MODEL_RULES].content == rules_bytes


@pytest.mark.parametrize("source_kind", ["directory", "fifo", "socket"])
def test_capture_rejects_special_files_without_reading(
    tmp_path: Path,
    source_kind: str,
) -> None:
    _write_sources(tmp_path, include_optional=False)
    source_path = tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
    open_socket: socket.socket | None = None
    if source_kind == "directory":
        source_path.mkdir()
    elif source_kind == "fifo":
        os.mkfifo(source_path)
    else:
        open_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        open_socket.bind(str(source_path))

    try:
        with pytest.raises(ConfigSourceError):
            ConfigSourceBundle.capture(tmp_path)
    finally:
        if open_socket is not None:
            open_socket.close()


def test_capture_isolated_from_later_file_mutation(tmp_path: Path) -> None:
    contents = _write_sources(tmp_path, include_optional=False)
    bundle = ConfigSourceBundle.capture(tmp_path)

    (tmp_path / FILENAMES[ConfigFile.PROVIDERS]).write_bytes(b"replacement")

    assert bundle[ConfigFile.PROVIDERS].content == contents[ConfigFile.PROVIDERS]


def test_capture_rejects_mid_read_mutation_without_leaking_file_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_sources(tmp_path, include_optional=False)
    providers_path = tmp_path / FILENAMES[ConfigFile.PROVIDERS]
    source_stat = providers_path.stat()
    original_read_all = config_store._read_all
    open_fds_before = set(os.listdir("/proc/self/fd"))

    def read_then_mutate(source_fd: int) -> bytes:
        content = original_read_all(source_fd)
        replacement = b"mutate-0\n"
        assert len(replacement) == len(content)
        providers_path.write_bytes(replacement)
        os.utime(
            providers_path,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 1_000_000_000),
        )
        return content

    monkeypatch.setattr(config_store, "_read_all", read_then_mutate)

    with pytest.raises(ConfigSourceError, match="config source changed during capture"):
        ConfigSourceBundle.capture(tmp_path)

    assert set(os.listdir("/proc/self/fd")) == open_fds_before


def test_with_candidate_replaces_one_document_without_disk_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_sources(tmp_path, include_optional=False)
    bundle = ConfigSourceBundle.capture(tmp_path)
    candidate = bytearray(b"candidate")
    original_fallback = bundle[ConfigFile.FALLBACK_RULES]
    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: pytest.fail("disk access"))

    updated = bundle.with_candidate(ConfigFile.MODEL_RULES, candidate)
    candidate[:] = b"mutated!!"

    assert updated is not bundle
    assert updated[ConfigFile.MODEL_RULES].exists is True
    assert updated[ConfigFile.MODEL_RULES].content == b"candidate"
    assert updated[ConfigFile.MODEL_RULES].metadata is None
    assert updated[ConfigFile.FALLBACK_RULES] is original_fallback
    assert bundle[ConfigFile.MODEL_RULES].exists is False


def test_documents_mapping_and_override_input_are_defensively_copied(tmp_path: Path) -> None:
    _write_sources(tmp_path, include_optional=False)
    overrides = {ConfigFile.MODEL_RULES: "models_model_rules.json"}
    bundle = ConfigSourceBundle.capture(tmp_path, overrides=overrides)
    overrides[ConfigFile.MODEL_RULES] = "changed.json"
    source_documents = dict(bundle.documents)
    copied_bundle = ConfigSourceBundle(source_documents)
    source_documents[ConfigFile.MODEL_RULES] = bundle[ConfigFile.PROVIDERS]

    with pytest.raises(TypeError):
        bundle.documents[ConfigFile.MODEL_RULES] = bundle[ConfigFile.PROVIDERS]  # type: ignore[index]

    assert bundle[ConfigFile.MODEL_RULES].path == tmp_path / "models_model_rules.json"
    assert copied_bundle[ConfigFile.MODEL_RULES] is bundle[ConfigFile.MODEL_RULES]


def test_repr_hides_source_paths_and_payloads(tmp_path: Path) -> None:
    secret_path = tmp_path / "secret-directory"
    secret_path.mkdir()
    _write_sources(secret_path, include_optional=False)
    secret = b"PRIVATE-API-KEY"
    (secret_path / FILENAMES[ConfigFile.MODEL_RULES]).write_bytes(secret)

    bundle = ConfigSourceBundle.capture(secret_path)

    document_repr = repr(bundle[ConfigFile.MODEL_RULES])
    bundle_repr = repr(bundle)
    assert str(secret_path) not in document_repr
    assert str(secret_path) not in bundle_repr
    assert secret.decode() not in document_repr
    assert secret.decode() not in bundle_repr
    assert "model_rules" in document_repr
    assert "model_rules=present" in bundle_repr
