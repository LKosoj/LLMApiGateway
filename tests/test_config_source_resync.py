"""Digest-only source comparison and the disk resync path.

The coordinator used to compare whole ``ConfigSourceBundle`` objects, whose
equality covers filesystem metadata: a ``chmod`` or an atomic replacement with
identical bytes read as drift and locked every config editor until a restart.
These tests pin the narrower contract (content identity only), the split
between ``REVISION_CONFLICT`` and ``SOURCES_OUT_OF_SYNC``, and ``resync()``
as the only in-process way out of a real drift.
"""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

import pytest

from llm_gateway_core.config.config_store import ConfigFile, ConfigSourceBundle
from llm_gateway_core.services.config_updates import (
    ConfigUpdateError,
    ConfigUpdateErrorCode,
)
from tests._async_compat import run_async
from tests.test_config_update_transaction import (
    FILENAMES,
    _expect_update_error,
    _make_harness,
    _model_rules,
    _revision,
    _write_sources,
)


def _capture(root: Path) -> ConfigSourceBundle:
    return ConfigSourceBundle.capture(root)


def _drift_providers(root: Path) -> None:
    """Rewrite providers.json out of band with different, still-valid bytes."""
    (root / FILENAMES[ConfigFile.PROVIDERS]).write_text(
        json.dumps(
            [
                {
                    "primary": {
                        "baseUrl": "https://drifted.example/v1",
                        "apikey": "DIRECT-KEY",
                    }
                }
            ]
        ),
        encoding="utf-8",
    )


def test_content_digests_ignore_metadata_but_track_bytes(tmp_path: Path) -> None:
    _write_sources(tmp_path)
    bundle = _capture(tmp_path)
    target = tmp_path / FILENAMES[ConfigFile.MODEL_RULES]

    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    after_chmod = bundle.recapture()

    assert after_chmod != bundle
    assert after_chmod.content_digests() == bundle.content_digests()

    target.write_bytes(_model_rules("changed"))
    after_write = bundle.recapture()

    assert after_write.content_digests() != bundle.content_digests()
    assert (
        after_write.content_digests()[ConfigFile.MODEL_RULES]
        != bundle.content_digests()[ConfigFile.MODEL_RULES]
    )
    assert (
        after_write.content_digests()[ConfigFile.PROVIDERS]
        == bundle.content_digests()[ConfigFile.PROVIDERS]
    )


def test_metadata_only_change_no_longer_blocks_an_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            (tmp_path / FILENAMES[ConfigFile.PROVIDERS]).chmod(
                stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
            )

            result = await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("published"),
            )

            assert result.snapshot.generation == 2
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_neighbour_drift_reports_out_of_sync_and_names_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            _drift_providers(tmp_path)

            with caplog.at_level(
                logging.WARNING,
                logger="llm_gateway_core.services.config_updates",
            ):
                error = await _expect_update_error(
                    harness.coordinator.update(
                        base_snapshot=harness.initial_snapshot,
                        config_file=ConfigFile.MODEL_RULES,
                        candidate_bytes=_model_rules("candidate"),
                    ),
                    ConfigUpdateErrorCode.SOURCES_OUT_OF_SYNC,
                )

            assert error.errors is not None
            assert [entry["loc"] for entry in error.errors] == [
                [ConfigFile.PROVIDERS.value]
            ]
            assert ConfigFile.PROVIDERS.value in caplog.text
            assert harness.manager.current_generation == 1
            assert (
                tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
            ).read_bytes() != _model_rules("candidate")
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_target_drift_reports_out_of_sync_because_a_reload_cannot_clear_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-band drift of the edited file is a resync case, not a reload one.

    Reading the document back only ever returns the loaded snapshot, so its
    revision never moves and a second attempt fails identically. Reporting
    this as a revision conflict sends the caller into that loop; reporting it
    as out-of-sync points at the resync that actually resolves it.
    """

    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            (tmp_path / FILENAMES[ConfigFile.MODEL_RULES]).write_bytes(
                _model_rules("external")
            )

            for _attempt in range(2):
                error = await _expect_update_error(
                    harness.coordinator.update(
                        base_snapshot=harness.initial_snapshot,
                        config_file=ConfigFile.MODEL_RULES,
                        candidate_bytes=_model_rules("candidate"),
                        expected_revision=_revision(
                            harness.initial_snapshot, ConfigFile.MODEL_RULES
                        ),
                    ),
                    ConfigUpdateErrorCode.SOURCES_OUT_OF_SYNC,
                )
                assert error.errors is not None
                assert [entry["loc"] for entry in error.errors] == [
                    [ConfigFile.MODEL_RULES.value]
                ]

            resynced = await harness.coordinator.resync(
                base_snapshot=harness.initial_snapshot
            )
            await harness.wait_for_retirement()
            published = await harness.coordinator.update(
                base_snapshot=resynced.snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("candidate"),
                expected_revision=_revision(
                    resynced.snapshot, ConfigFile.MODEL_RULES
                ),
            )

            assert published.snapshot.generation == 3
            assert (
                tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
            ).read_bytes() == _model_rules("candidate")
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_check_sources_rejects_before_any_candidate_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            harness.coordinator.check_sources(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.PROVIDERS,
            )

            _drift_providers(tmp_path)

            with pytest.raises(ConfigUpdateError) as raised:
                harness.coordinator.check_sources(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                )

            assert raised.value.code is ConfigUpdateErrorCode.SOURCES_OUT_OF_SYNC
            assert harness.built == []
            assert harness.coordinator.status_snapshot.active_updates == 0
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_resync_adopts_disk_and_unblocks_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            _drift_providers(tmp_path)
            drifted_bytes = (
                tmp_path / FILENAMES[ConfigFile.PROVIDERS]
            ).read_bytes()

            result = await harness.coordinator.resync(
                base_snapshot=harness.initial_snapshot
            )

            assert result.snapshot.generation == 2
            assert harness.manager.current_generation == 2
            assert (
                tmp_path / FILENAMES[ConfigFile.PROVIDERS]
            ).read_bytes() == drifted_bytes
            bundle = result.snapshot.config_loader.source_bundle
            assert isinstance(bundle, ConfigSourceBundle)
            assert bundle.content_digests() == _capture(tmp_path).content_digests()
            assert (
                result.snapshot.config_loader.providers_config[
                    "primary"
                ].baseUrl
                == "https://drifted.example/v1"
            )

            await harness.wait_for_retirement()
            follow_up = await harness.coordinator.update(
                base_snapshot=result.snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("published"),
            )

            assert follow_up.snapshot.generation == 3
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_resync_without_drift_is_a_no_op(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            result = await harness.coordinator.resync(
                base_snapshot=harness.initial_snapshot
            )

            assert result.snapshot is harness.initial_snapshot
            assert result.cleanup_pending is False
            assert harness.built == []
            assert harness.manager.current_generation == 1
            assert harness.coordinator.status_snapshot.active_updates == 0
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_resync_ignores_metadata_only_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            (tmp_path / FILENAMES[ConfigFile.PROVIDERS]).chmod(
                stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
            )

            result = await harness.coordinator.resync(
                base_snapshot=harness.initial_snapshot
            )

            assert result.snapshot is harness.initial_snapshot
            assert harness.built == []
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_resync_refuses_an_invalid_disk_state_and_keeps_the_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            (tmp_path / FILENAMES[ConfigFile.FALLBACK_RULES]).write_text(
                json.dumps(
                    [
                        {
                            "gateway_model_name": "gateway/chat",
                            "fallback_models": [
                                {"provider": "missing", "model": "upstream-chat"}
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            await _expect_update_error(
                harness.coordinator.resync(
                    base_snapshot=harness.initial_snapshot
                ),
                ConfigUpdateErrorCode.VALIDATION_FAILED,
            )

            assert harness.manager.current_generation == 1
            assert harness.built == []
            assert harness.coordinator.status_snapshot.active_updates == 0
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_resync_names_the_source_whose_shape_this_process_cannot_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused disk state names its file without quoting the file.

    Reproduces the deadlock an operator hits when a schema change reaches disk
    before the service restarts: the extra field is valid for the newer build
    and ``extra_forbidden`` for the running one, so saving is blocked as out of
    sync and the resync that would clear it refuses the disk. The frozen
    generic message leaves nothing to act on, and the Pydantic report quotes
    raw input, so the file name is the one safe thing to hand back.
    """

    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            (tmp_path / FILENAMES[ConfigFile.ROUTER_RULES]).write_text(
                json.dumps(
                    [
                        {
                            "gateway_model_name": "gateway/router",
                            "selector_model": "gateway/chat",
                            "targets": [
                                {"type": "gateway_model", "model": "gateway/chat"}
                            ],
                            "field_only_a_newer_build_knows": "SECRET-LOOKING-VALUE",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            error = await _expect_update_error(
                harness.coordinator.resync(
                    base_snapshot=harness.initial_snapshot
                ),
                ConfigUpdateErrorCode.VALIDATION_FAILED,
            )

            assert error.errors is not None
            assert len(error.errors) == 1
            reported = error.errors[0]
            assert reported["type"] == "source_invalid"
            assert reported["loc"] == [ConfigFile.ROUTER_RULES.value]
            assert ConfigFile.ROUTER_RULES.value in reported["msg"]
            assert "SECRET-LOOKING-VALUE" not in reported["msg"]
            assert "field_only_a_newer_build_knows" not in reported["msg"]

            assert harness.manager.current_generation == 1
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_resync_keeps_the_cross_validation_message_when_there_is_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming the file must not displace a message that names the rule.

    A cross-validation failure already says which rule pointed at what, which
    is strictly more actionable than the file name, so it stays the reported
    error rather than being replaced by the coarser one.
    """

    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            (tmp_path / FILENAMES[ConfigFile.ROUTER_RULES]).write_text(
                json.dumps(
                    [
                        {
                            "gateway_model_name": "gateway/router",
                            "selector_model": "gateway/not-configured",
                            "targets": [
                                {"type": "gateway_model", "model": "gateway/chat"}
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            error = await _expect_update_error(
                harness.coordinator.resync(
                    base_snapshot=harness.initial_snapshot
                ),
                ConfigUpdateErrorCode.VALIDATION_FAILED,
            )

            assert error.errors is not None
            assert [entry["type"] for entry in error.errors] == ["rule_validation"]
            assert "gateway/not-configured" in error.errors[0]["msg"]
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_resync_rejects_a_stale_base_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            published = await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("published"),
            )
            assert published.snapshot.generation == 2

            await _expect_update_error(
                harness.coordinator.resync(
                    base_snapshot=harness.initial_snapshot
                ),
                ConfigUpdateErrorCode.GENERATION_STALE,
            )

            assert harness.manager.current_generation == 2
        finally:
            await harness.shutdown()

    run_async(scenario())
