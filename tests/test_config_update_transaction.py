from __future__ import annotations

import asyncio
import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MethodType
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from llm_gateway_core.config.config_store import (
    AtomicConfigFileTransaction,
    AtomicConfigTransactionError,
    AtomicConfigTransactionState,
    CommentsBackupLifecycle,
    ConfigFile,
    ConfigSourceBundle,
)
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.services.config_updates import (
    ConfigRevision,
    ConfigUpdateCoordinator,
    ConfigUpdateError,
    ConfigUpdateErrorCode,
    ConfigUpdateState,
)
from llm_gateway_core.services.runtime_candidate import RuntimeCandidate
from llm_gateway_core.services.runtime_config import (
    RuntimeGenerationManager,
    RuntimeManagerStateError,
    RuntimeManagerStatus,
    RuntimeSnapshot,
    _FailedGeneration,  # noqa: SLF001
)
from tests._async_compat import run_async
from tests.runtime_test_support import (
    install_test_runtime_snapshot,
    make_runtime_snapshot,
)


VALID_PAYLOADS = {
    ConfigFile.PROVIDERS: [
        {
            "primary": {
                "baseUrl": "https://primary.example/v1",
                "apikey": "DIRECT-KEY",
            }
        }
    ],
    ConfigFile.FALLBACK_RULES: [
        {
            "gateway_model_name": "gateway/chat",
            "fallback_models": [
                {"provider": "primary", "model": "upstream-chat"}
            ],
        }
    ],
    ConfigFile.MODEL_RULES: {},
    ConfigFile.OPERATION_RULES: {},
    ConfigFile.FUSION_RULES: [],
    ConfigFile.ROUTER_RULES: [],
}
FILENAMES = {
    ConfigFile.PROVIDERS: "providers.json",
    ConfigFile.FALLBACK_RULES: "models_fallback_rules.json",
    ConfigFile.MODEL_RULES: "models_model_rules.json",
    ConfigFile.OPERATION_RULES: "models_operation_rules.json",
    ConfigFile.FUSION_RULES: "models_fusion_rules.json",
    ConfigFile.ROUTER_RULES: "models_router_rules.json",
}


def _write_sources(root: Path) -> None:
    for config_file, payload in VALID_PAYLOADS.items():
        (root / FILENAMES[config_file]).write_text(
            json.dumps(payload),
            encoding="utf-8",
        )


def _model_rules(alias: str) -> bytes:
    return json.dumps(
        {"aliases": {alias: "gateway/chat"}},
        separators=(",", ":"),
    ).encode()


def _commented_model_rules(alias: str) -> bytes:
    return (
        b'{\n  // preserve this user comment\n  "aliases": {"'
        + alias.encode()
        + b'": "gateway/chat"}\n}\n'
    )


def _owned_artifacts(root: Path) -> tuple[Path, ...]:
    return tuple(root.glob(".llmgateway-config-txn-*"))


def _journal_artifacts(root: Path) -> tuple[Path, ...]:
    return tuple(root.glob(".llmgateway-config-txn-*.journal.*"))


def _comments_backups(root: Path) -> tuple[Path, ...]:
    return tuple(
        root.glob(f"{FILENAMES[ConfigFile.MODEL_RULES]}.bak.*")
    )


@dataclass(slots=True)
class _CandidateProxy:
    candidate: RuntimeCandidate
    publish_error: BaseException | None = None
    post_publish_error: BaseException | None = None
    close_error: BaseException | None = None
    publish_calls: int = 0
    close_calls: int = 0

    @property
    def snapshot(self) -> RuntimeSnapshot:
        return self.candidate.snapshot

    def publish(self, *, expected_generation: int) -> RuntimeSnapshot:
        self.publish_calls += 1
        if self.publish_error is not None:
            raise self.publish_error
        snapshot = self.candidate.publish(expected_generation=expected_generation)
        if self.post_publish_error is not None:
            raise self.post_publish_error
        return snapshot

    async def close_unpublished(self) -> bool:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        return await self.candidate.close_unpublished()


@dataclass(slots=True)
class _Harness:
    root: Path
    manager: RuntimeGenerationManager
    shared_client: httpx.AsyncClient
    initial_snapshot: RuntimeSnapshot
    coordinator: ConfigUpdateCoordinator
    built: list[_CandidateProxy] = field(default_factory=list)
    next_publish_error: BaseException | None = None
    next_post_publish_error: BaseException | None = None
    next_close_error: BaseException | None = None
    next_cleanup_client: object | None = None

    async def build_candidate(
        self,
        *,
        base_snapshot: RuntimeSnapshot,
        candidate_loader: ConfigLoader,
    ) -> _CandidateProxy:
        generation = base_snapshot.generation + 1
        slot = self.manager.open_unpublished_slot(generation)
        proxy_http_clients: dict[str, object] = {}
        if self.next_cleanup_client is not None:
            slot.register_http_client("cleanup", self.next_cleanup_client)  # type: ignore[arg-type]
            proxy_http_clients["cleanup"] = self.next_cleanup_client
            self.next_cleanup_client = None
        candidate = RuntimeCandidate(
            snapshot=make_runtime_snapshot(
                generation=generation,
                config_loader=candidate_loader,
                http_client=self.shared_client,
                proxy_http_clients=proxy_http_clients,
            ),
            _manager=self.manager,
            _slot=slot,
        )
        proxy = _CandidateProxy(
            candidate,
            publish_error=self.next_publish_error,
            post_publish_error=self.next_post_publish_error,
            close_error=self.next_close_error,
        )
        self.next_publish_error = None
        self.next_post_publish_error = None
        self.next_close_error = None
        self.built.append(proxy)
        return proxy

    async def wait_for_retirement(self) -> None:
        for _ in range(20):
            if self.manager.cleanup_task_count == 0:
                return
            await asyncio.sleep(0)
        raise AssertionError("runtime retirement did not finish")

    async def shutdown(self) -> None:
        await self.coordinator.close()
        await self.manager.shutdown()
        await self.shared_client.aclose()


async def _make_harness(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_rules_bytes: bytes | None = None,
    initial_proxy_http_clients: dict[str, object] | None = None,
) -> _Harness:
    _write_sources(root)
    if model_rules_bytes is not None:
        (root / FILENAMES[ConfigFile.MODEL_RULES]).write_bytes(model_rules_bytes)
    monkeypatch.setattr(
        "llm_gateway_core.config.loader.settings.fallback_provider",
        "primary",
    )
    loader = ConfigLoader.from_source_bundle(
        ConfigSourceBundle.capture(root)
    ).load_complete()
    manager = RuntimeGenerationManager()
    shared_client = httpx.AsyncClient()
    initial_snapshot = make_runtime_snapshot(
        generation=1,
        config_loader=loader,
        http_client=shared_client,
        proxy_http_clients=initial_proxy_http_clients or {},
    )
    install_test_runtime_snapshot(manager, initial_snapshot)
    coordinator = ConfigUpdateCoordinator(
        runtime_manager=manager,
        shared_http_client=shared_client,
        initial_snapshot=initial_snapshot,
    )
    harness = _Harness(
        root=root,
        manager=manager,
        shared_client=shared_client,
        initial_snapshot=initial_snapshot,
        coordinator=coordinator,
    )
    monkeypatch.setattr(coordinator, "_build_candidate", harness.build_candidate)
    return harness


def _revision(snapshot: RuntimeSnapshot, config_file: ConfigFile) -> ConfigRevision:
    bundle = snapshot.config_loader.source_bundle
    assert isinstance(bundle, ConfigSourceBundle)
    document = bundle[config_file]
    return ConfigRevision(
        config_file=config_file,
        digest=document.digest if document.exists else None,
    )


async def _expect_update_error(
    awaitable: Any,
    code: ConfigUpdateErrorCode,
) -> ConfigUpdateError:
    with pytest.raises(ConfigUpdateError) as raised:
        await awaitable
    assert raised.value.code is code
    return raised.value


def test_constructor_cleans_orphans_for_exact_six_source_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        _write_sources(tmp_path)
        monkeypatch.setattr(
            "llm_gateway_core.config.loader.settings.fallback_provider",
            "primary",
        )
        bundle = ConfigSourceBundle.capture(tmp_path)
        loader = ConfigLoader.from_source_bundle(bundle).load_complete()
        manager = RuntimeGenerationManager()
        shared_client = httpx.AsyncClient()
        snapshot = make_runtime_snapshot(
            generation=1,
            config_loader=loader,
            http_client=shared_client,
        )
        install_test_runtime_snapshot(manager, snapshot)
        cleanup_orphans = Mock(return_value=0)
        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "cleanup_orphans",
            cleanup_orphans,
        )

        coordinator = ConfigUpdateCoordinator(
            runtime_manager=manager,
            shared_http_client=shared_client,
            initial_snapshot=snapshot,
        )

        assert [call.args[0] for call in cleanup_orphans.call_args_list] == [
            bundle[config_file] for config_file in ConfigFile
        ]
        await coordinator.close()
        await manager.shutdown()
        await shared_client.aclose()

    run_async(scenario())


def test_constructor_orphan_cleanup_failure_is_fail_closed_and_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        _write_sources(tmp_path)
        monkeypatch.setattr(
            "llm_gateway_core.config.loader.settings.fallback_provider",
            "primary",
        )
        loader = ConfigLoader.from_source_bundle(
            ConfigSourceBundle.capture(tmp_path)
        ).load_complete()
        manager = RuntimeGenerationManager()
        shared_client = httpx.AsyncClient()
        snapshot = make_runtime_snapshot(
            generation=1,
            config_loader=loader,
            http_client=shared_client,
        )
        install_test_runtime_snapshot(manager, snapshot)
        cleanup_orphans = Mock(
            side_effect=OSError("orphan cleanup credential secret")
        )
        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "cleanup_orphans",
            cleanup_orphans,
        )

        with pytest.raises(ConfigUpdateError) as raised:
            ConfigUpdateCoordinator(
                runtime_manager=manager,
                shared_http_client=shared_client,
                initial_snapshot=snapshot,
            )

        assert raised.value.code is ConfigUpdateErrorCode.UPDATE_UNAVAILABLE
        assert "credential secret" not in str(raised.value)
        assert cleanup_orphans.call_count == 1
        await manager.shutdown()
        await shared_client.aclose()

    run_async(scenario())


def test_check_base_is_read_only_and_repeats_generation_and_revision_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        config_file = ConfigFile.MODEL_RULES
        source = tmp_path / FILENAMES[config_file]
        original = source.read_bytes()
        real_recapture = ConfigSourceBundle.recapture
        recapture = Mock(side_effect=AssertionError("check_base performed I/O"))
        monkeypatch.setattr(ConfigSourceBundle, "recapture", recapture)

        harness.coordinator.check_base(
            base_snapshot=harness.initial_snapshot,
            config_file=config_file,
            expected_revision=_revision(harness.initial_snapshot, config_file),
        )

        assert harness.coordinator.status_snapshot.active_updates == 0
        assert harness.built == []
        assert source.read_bytes() == original
        recapture.assert_not_called()

        with pytest.raises(ConfigUpdateError) as revision_error:
            harness.coordinator.check_base(
                base_snapshot=harness.initial_snapshot,
                config_file=config_file,
                expected_revision=ConfigRevision(config_file, "0" * 64),
            )
        assert revision_error.value.code is ConfigUpdateErrorCode.REVISION_CONFLICT

        monkeypatch.setattr(ConfigSourceBundle, "recapture", real_recapture)
        result = await harness.coordinator.update(
            base_snapshot=harness.initial_snapshot,
            config_file=config_file,
            candidate_bytes=_model_rules("published"),
        )
        built_count = len(harness.built)
        with pytest.raises(ConfigUpdateError) as stale_error:
            harness.coordinator.check_base(
                base_snapshot=harness.initial_snapshot,
                config_file=config_file,
            )
        assert stale_error.value.code is ConfigUpdateErrorCode.GENERATION_STALE
        assert len(harness.built) == built_count
        assert result.snapshot.generation == 2
        await harness.shutdown()

    run_async(scenario())


def test_check_base_state_precedence_types_and_source_bundle_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        coordinator = harness.coordinator
        coordinator._state = ConfigUpdateState.BROKEN  # noqa: SLF001
        with pytest.raises(ConfigUpdateError) as broken:
            coordinator.check_base(
                base_snapshot=object(),  # type: ignore[arg-type]
                config_file="bad",  # type: ignore[arg-type]
            )
        assert broken.value.code is ConfigUpdateErrorCode.UPDATE_BROKEN

        coordinator._state = ConfigUpdateState.STOPPING  # noqa: SLF001
        with pytest.raises(ConfigUpdateError) as unavailable:
            coordinator.check_base(
                base_snapshot=object(),  # type: ignore[arg-type]
                config_file="bad",  # type: ignore[arg-type]
            )
        assert unavailable.value.code is ConfigUpdateErrorCode.UPDATE_UNAVAILABLE

        coordinator._state = ConfigUpdateState.RUNNING  # noqa: SLF001
        with pytest.raises(TypeError, match="base_snapshot"):
            coordinator.check_base(
                base_snapshot=object(),  # type: ignore[arg-type]
                config_file=ConfigFile.MODEL_RULES,
            )
        with pytest.raises(TypeError, match="config_file"):
            coordinator.check_base(
                base_snapshot=harness.initial_snapshot,
                config_file="bad",  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="identity mismatch"):
            coordinator.check_base(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                expected_revision=ConfigRevision(ConfigFile.PROVIDERS, None),
            )

        loader = harness.initial_snapshot.config_loader
        source_bundle = loader._source_bundle  # noqa: SLF001
        loader._source_bundle = None  # noqa: SLF001
        try:
            with pytest.raises(ConfigUpdateError) as missing_bundle:
                coordinator.check_base(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                )
            assert (
                missing_bundle.value.code
                is ConfigUpdateErrorCode.UPDATE_UNAVAILABLE
            )
        finally:
            loader._source_bundle = source_bundle  # noqa: SLF001
        await harness.shutdown()

    run_async(scenario())


def test_happy_publish_persists_metadata_and_repeats_same_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        config_file = ConfigFile.MODEL_RULES
        first_bytes = _model_rules("alias-one")
        second_bytes = _model_rules("alias-two")
        try:
            first = await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=config_file,
                candidate_bytes=first_bytes,
                expected_revision=_revision(
                    harness.initial_snapshot,
                    config_file,
                ),
            )

            assert first.snapshot.generation == 2
            assert harness.manager.current_generation == 2
            assert not first.cleanup_pending
            assert (tmp_path / FILENAMES[config_file]).read_bytes() == first_bytes
            first_bundle = first.snapshot.config_loader.source_bundle
            assert isinstance(first_bundle, ConfigSourceBundle)
            assert first_bundle[config_file].metadata is not None
            assert first_bundle == first_bundle.recapture()

            await _expect_update_error(
                harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=config_file,
                    candidate_bytes=second_bytes,
                ),
                ConfigUpdateErrorCode.GENERATION_STALE,
            )
            await _expect_update_error(
                harness.coordinator.update(
                    base_snapshot=first.snapshot,
                    config_file=config_file,
                    candidate_bytes=second_bytes,
                    expected_revision=ConfigRevision(
                        config_file,
                        "0" * 64,
                    ),
                ),
                ConfigUpdateErrorCode.REVISION_CONFLICT,
            )

            await harness.wait_for_retirement()
            second = await harness.coordinator.update(
                base_snapshot=first.snapshot,
                config_file=config_file,
                candidate_bytes=second_bytes,
                expected_revision=_revision(first.snapshot, config_file),
            )

            assert second.snapshot.generation == 3
            assert harness.manager.current_generation == 3
            assert (tmp_path / FILENAMES[config_file]).read_bytes() == second_bytes
            second_bundle = second.snapshot.config_loader.source_bundle
            assert isinstance(second_bundle, ConfigSourceBundle)
            assert second_bundle[config_file].metadata is not None
            assert second_bundle == second_bundle.recapture()
            assert len(harness.built) == 2
        finally:
            await harness.shutdown()

    run_async(scenario())


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", object()])
def test_comments_backup_requires_exact_bool_before_candidate_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        original = (tmp_path / FILENAMES[ConfigFile.MODEL_RULES]).read_bytes()
        try:
            with pytest.raises(TypeError, match="comments_backup"):
                await harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                    candidate_bytes=_model_rules("candidate"),
                    comments_backup=invalid,  # type: ignore[arg-type]
                )
            assert harness.built == []
            assert harness.coordinator.status_snapshot.active_updates == 0
            assert (
                tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
            ).read_bytes() == original
            assert _comments_backups(tmp_path) == ()
            assert _owned_artifacts(tmp_path) == ()
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_comments_backup_publishes_exact_commented_source_and_safe_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        original = _commented_model_rules("original")
        harness = await _make_harness(
            tmp_path,
            monkeypatch,
            model_rules_bytes=original,
        )
        try:
            result = await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("candidate"),
                comments_backup=True,
            )

            assert result.comments_backup is not None
            backup = tmp_path / result.comments_backup
            assert backup.parent == tmp_path
            assert backup.read_bytes() == original
            assert stat.S_IMODE(backup.stat().st_mode) == 0o600
            assert result.cleanup_pending is False
            assert result.snapshot.generation == 2
            assert result.comments_backup not in repr(result)
            assert str(tmp_path) not in repr(result)
            assert harness.coordinator.status_snapshot.accepting
        finally:
            await harness.shutdown()

    run_async(scenario())


@pytest.mark.parametrize(
    ("source", "enabled"),
    [
        (_model_rules("plain"), True),
        (_commented_model_rules("commented"), False),
    ],
)
def test_comments_backup_is_absent_without_comments_or_without_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: bytes,
    enabled: bool,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(
            tmp_path,
            monkeypatch,
            model_rules_bytes=source,
        )
        try:
            result = await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("candidate"),
                comments_backup=enabled,
            )
            assert result.comments_backup is None
            assert _comments_backups(tmp_path) == ()
            assert not result.cleanup_pending
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_comments_backup_drift_is_rejected_before_backup_or_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(
            tmp_path,
            monkeypatch,
            model_rules_bytes=_commented_model_rules("original"),
        )
        target = tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
        external = _commented_model_rules("external")
        target.write_bytes(external)
        begin = Mock(side_effect=AssertionError("transaction began for stale base"))
        monkeypatch.setattr(AtomicConfigFileTransaction, "begin", begin)
        try:
            await _expect_update_error(
                harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                    candidate_bytes=_model_rules("candidate"),
                    comments_backup=True,
                ),
                ConfigUpdateErrorCode.REVISION_CONFLICT,
            )
            begin.assert_not_called()
            assert target.read_bytes() == external
            assert _comments_backups(tmp_path) == ()
            assert _owned_artifacts(tmp_path) == ()
            assert harness.built[0].close_calls == 1
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_concurrent_stale_loser_creates_no_second_comments_backup_or_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(
            tmp_path,
            monkeypatch,
            model_rules_bytes=_commented_model_rules("original"),
        )
        entered = 0
        both_entered = asyncio.Event()
        release = asyncio.Event()
        real_backup_begin = CommentsBackupLifecycle.begin
        real_transaction_begin = AtomicConfigFileTransaction.begin

        async def preflight(_snapshot: RuntimeSnapshot) -> None:
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()

        backup_begin = Mock(side_effect=real_backup_begin)
        transaction_begin = Mock(side_effect=real_transaction_begin)

        monkeypatch.setattr(
            CommentsBackupLifecycle,
            "begin",
            backup_begin,
        )
        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "begin",
            transaction_begin,
        )
        tasks = [
            asyncio.create_task(
                harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                    candidate_bytes=_model_rules(alias),
                    comments_backup=True,
                    preflight=preflight,
                )
            )
            for alias in ("first", "second")
        ]
        await both_entered.wait()
        release.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        results = [item for item in outcomes if not isinstance(item, BaseException)]
        stale = [
            item
            for item in outcomes
            if isinstance(item, ConfigUpdateError)
            and item.code is ConfigUpdateErrorCode.GENERATION_STALE
        ]

        assert len(results) == len(stale) == 1
        assert results[0].comments_backup is not None
        assert backup_begin.call_count == 1
        assert transaction_begin.call_count == 1
        assert len(_comments_backups(tmp_path)) == 1
        assert _journal_artifacts(tmp_path) == ()
        assert sum(candidate.close_calls for candidate in harness.built) == 1
        await harness.shutdown()

    run_async(scenario())


def test_out_of_band_drift_is_rejected_and_external_bytes_survive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        config_file = ConfigFile.MODEL_RULES
        external_bytes = _model_rules("external")
        target = tmp_path / FILENAMES[config_file]
        expected_revision = _revision(harness.initial_snapshot, config_file)
        target.write_bytes(external_bytes)
        try:
            await _expect_update_error(
                harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=config_file,
                    candidate_bytes=_model_rules("candidate"),
                    expected_revision=expected_revision,
                ),
                ConfigUpdateErrorCode.REVISION_CONFLICT,
            )

            assert target.read_bytes() == external_bytes
            assert harness.manager.current_generation == 1
            assert harness.manager.pending_unpublished_generations == ()
            assert harness.built[0].close_calls == 1
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_publish_failure_rolls_back_exact_file_and_closes_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        config_file = ConfigFile.MODEL_RULES
        target = tmp_path / FILENAMES[config_file]
        old_bytes = target.read_bytes()
        old_stat = target.stat()
        transactions: list[AtomicConfigFileTransaction] = []
        real_begin = AtomicConfigFileTransaction.begin

        def capture_begin(*args: object, **kwargs: object) -> AtomicConfigFileTransaction:
            transaction = real_begin(*args, **kwargs)
            transactions.append(transaction)
            return transaction

        monkeypatch.setattr(AtomicConfigFileTransaction, "begin", capture_begin)
        harness.next_publish_error = RuntimeError("publish failed")
        try:
            await _expect_update_error(
                harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=config_file,
                    candidate_bytes=_model_rules("candidate"),
                    expected_revision=_revision(
                        harness.initial_snapshot,
                        config_file,
                    ),
                ),
                ConfigUpdateErrorCode.COMMIT_FAILED,
            )

            new_stat = target.stat()
            assert target.read_bytes() == old_bytes
            assert stat.S_IMODE(new_stat.st_mode) == stat.S_IMODE(old_stat.st_mode)
            assert (new_stat.st_uid, new_stat.st_gid) == (
                old_stat.st_uid,
                old_stat.st_gid,
            )
            assert transactions[0].state is AtomicConfigTransactionState.ROLLED_BACK
            assert harness.manager.current_generation == 1
            assert harness.manager.pending_unpublished_generations == ()
            assert harness.built[0].close_calls == 1
            assert harness.coordinator.status_snapshot.accepting
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_cancellation_waiting_commit_does_not_begin_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CancellingLock:
        def __init__(self, primary: asyncio.CancelledError) -> None:
            self.entered = asyncio.Event()
            self.resume = asyncio.Event()
            self.primary = primary

        async def acquire(self) -> None:
            self.entered.set()
            await self.resume.wait()
            raise self.primary

        def release(self) -> None:
            raise AssertionError("an unacquired lock must not be released")

    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        primary = asyncio.CancelledError("first cancellation")
        lock = _CancellingLock(primary)
        monkeypatch.setattr(harness.coordinator, "_commit_lock", lock)
        begin = Mock(side_effect=AssertionError("transaction began before lock"))
        abort = Mock(side_effect=AssertionError("an absent transaction was aborted"))
        monkeypatch.setattr(AtomicConfigFileTransaction, "begin", begin)
        monkeypatch.setattr(AtomicConfigFileTransaction, "abort", abort)
        task = asyncio.create_task(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("candidate"),
            )
        )
        await lock.entered.wait()
        lock.resume.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task

        assert raised.value is primary
        begin.assert_not_called()
        abort.assert_not_called()
        assert harness.built[0].close_calls == 1
        assert harness.manager.pending_unpublished_generations == ()
        assert _owned_artifacts(tmp_path) == ()
        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.RUNNING
        assert status.accepting
        assert status.pending_cleanup == 0

        monkeypatch.setattr(harness.coordinator, "_commit_lock", asyncio.Lock())
        await harness.shutdown()

    run_async(scenario())


def test_close_waits_for_admitted_update_to_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        entered = asyncio.Event()
        resume = asyncio.Event()

        async def preflight(_snapshot: RuntimeSnapshot) -> None:
            entered.set()
            await resume.wait()

        update_task = asyncio.create_task(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("candidate"),
                preflight=preflight,
            )
        )
        await entered.wait()
        close_task = asyncio.create_task(harness.coordinator.close())
        await asyncio.sleep(0)
        assert not close_task.done()
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.STOPPING
        assert harness.coordinator.status_snapshot.active_updates == 1

        resume.set()
        result = await update_task
        await close_task

        assert result.snapshot.generation == 2
        assert harness.manager.current_generation == 2
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.STOPPED
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_finalize_pending_is_observable_and_close_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        real_finalize = AtomicConfigFileTransaction.finalize
        calls = 0

        def fail_once(transaction: AtomicConfigFileTransaction) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("finalize failed")
            real_finalize(transaction)

        monkeypatch.setattr(AtomicConfigFileTransaction, "finalize", fail_once)
        result = await harness.coordinator.update(
            base_snapshot=harness.initial_snapshot,
            config_file=ConfigFile.MODEL_RULES,
            candidate_bytes=_model_rules("candidate"),
        )

        assert result.cleanup_pending
        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.RUNNING
        assert not status.accepting
        assert status.pending_cleanup == 1
        assert status.last_failure is not None
        assert status.last_failure.phase == "finalize"

        await harness.coordinator.close()
        assert calls == 2
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.STOPPED
        assert harness.coordinator.status_snapshot.pending_cleanup == 0
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_finalize_base_exception_keeps_published_disk_and_runtime_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        primary = KeyboardInterrupt("finalize interrupted")
        real_finalize = AtomicConfigFileTransaction.finalize
        calls = 0
        transactions: list[AtomicConfigFileTransaction] = []
        real_begin = AtomicConfigFileTransaction.begin

        def capture_begin(*args: object, **kwargs: object) -> AtomicConfigFileTransaction:
            transaction = real_begin(*args, **kwargs)
            transactions.append(transaction)
            return transaction

        def fail_once(transaction: AtomicConfigFileTransaction) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise primary
            real_finalize(transaction)

        monkeypatch.setattr(AtomicConfigFileTransaction, "begin", capture_begin)
        monkeypatch.setattr(AtomicConfigFileTransaction, "finalize", fail_once)
        candidate_bytes = _model_rules("candidate")
        with pytest.raises(KeyboardInterrupt) as raised:
            await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=candidate_bytes,
            )

        assert raised.value is primary
        assert harness.manager.current_generation == 2
        assert (tmp_path / FILENAMES[ConfigFile.MODEL_RULES]).read_bytes() == candidate_bytes
        assert transactions[0].state is AtomicConfigTransactionState.COMMITTED
        assert harness.coordinator.status_snapshot.pending_cleanup == 1

        await harness.coordinator.close()
        assert transactions[0].state is AtomicConfigTransactionState.FINALIZED
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_close_cleanup_base_exception_identity_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CloseInterrupted(BaseException):
        pass

    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        primary = _CloseInterrupted("close interrupted")
        real_finalize = AtomicConfigFileTransaction.finalize
        calls = 0

        def sequenced_finalize(transaction: AtomicConfigFileTransaction) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("defer finalize")
            if calls == 2:
                raise primary
            real_finalize(transaction)

        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "finalize",
            sequenced_finalize,
        )
        result = await harness.coordinator.update(
            base_snapshot=harness.initial_snapshot,
            config_file=ConfigFile.MODEL_RULES,
            candidate_bytes=_model_rules("candidate"),
        )
        assert result.cleanup_pending

        with pytest.raises(_CloseInterrupted) as raised:
            await harness.coordinator.close()
        assert raised.value is primary
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.BROKEN
        assert harness.coordinator.status_snapshot.pending_cleanup == 1

        await harness.coordinator.close()
        assert calls == 3
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.STOPPED
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_candidate_cleanup_base_exception_identity_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        primary = SystemExit("candidate cleanup interrupted")
        harness.next_close_error = primary

        async def reject(_snapshot: RuntimeSnapshot) -> None:
            raise RuntimeError("preflight rejection")

        with pytest.raises(SystemExit) as raised:
            await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("candidate"),
                preflight=reject,
            )

        assert raised.value is primary
        assert harness.built[0].close_calls == 1
        assert harness.manager.pending_unpublished_generations == (2,)
        assert await harness.built[0].candidate.close_unpublished()
        assert harness.manager.pending_unpublished_generations == ()
        await harness.shutdown()

    run_async(scenario())


@pytest.mark.parametrize(
    "terminal_state",
    [
        AtomicConfigTransactionState.ABORTED,
        AtomicConfigTransactionState.ROLLED_BACK,
    ],
)
def test_terminal_cleanup_state_does_not_enqueue_invalid_retry_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_state: AtomicConfigTransactionState,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        transaction = type(
            "TerminalTransaction",
            (),
            {"state": terminal_state},
        )()
        harness.coordinator._mark_broken(  # noqa: SLF001
            phase="transaction_recovery",
            exception=RuntimeError("cleanup terminal"),
            transaction=transaction,
        )

        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.BROKEN
        assert status.pending_cleanup == 0
        await harness.shutdown()

    run_async(scenario())


def test_fatal_transaction_cleanup_replaces_ordinary_primary_for_abort_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FatalCleanup(BaseException):
        pass

    async def abort_recovery() -> None:
        harness = await _make_harness(tmp_path / "abort", monkeypatch)
        fatal = _FatalCleanup("fatal abort")
        real_abort = AtomicConfigFileTransaction.abort

        def fail_commit(_transaction: AtomicConfigFileTransaction) -> None:
            raise RuntimeError("ordinary commit failure")

        def fatal_abort(transaction: AtomicConfigFileTransaction) -> None:
            real_abort(transaction)
            raise fatal

        monkeypatch.setattr(AtomicConfigFileTransaction, "commit", fail_commit)
        monkeypatch.setattr(AtomicConfigFileTransaction, "abort", fatal_abort)
        with pytest.raises(_FatalCleanup) as raised:
            await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("abort"),
            )
        assert raised.value is fatal
        assert harness.built[0].close_calls == 1
        await harness.shutdown()

    async def inner_recovery() -> None:
        harness = await _make_harness(tmp_path / "inner", monkeypatch)
        fatal = _FatalCleanup("fatal rollback")
        real_rollback = AtomicConfigFileTransaction.rollback

        def fatal_rollback(transaction: AtomicConfigFileTransaction) -> None:
            real_rollback(transaction)
            raise fatal

        monkeypatch.setattr(AtomicConfigFileTransaction, "rollback", fatal_rollback)
        harness.next_publish_error = RuntimeError("ordinary publish failure")
        with pytest.raises(_FatalCleanup) as raised:
            await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("inner"),
            )
        assert raised.value is fatal
        assert harness.built[0].close_calls == 1
        await harness.shutdown()

    (tmp_path / "abort").mkdir()
    run_async(abort_recovery())
    monkeypatch.undo()
    (tmp_path / "inner").mkdir()
    run_async(inner_recovery())


def test_failed_unpublished_cleanup_blocks_status_until_manager_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        client = Mock()
        client.aclose = AsyncMock(side_effect=RuntimeError("cleanup failed"))
        harness.next_cleanup_client = client

        async def reject(_snapshot: RuntimeSnapshot) -> None:
            raise RuntimeError("preflight rejected")

        await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("candidate"),
                preflight=reject,
            ),
            ConfigUpdateErrorCode.VALIDATION_FAILED,
        )

        assert harness.manager.failed_unpublished_generations == (2,)
        status = harness.coordinator.status_snapshot
        assert not status.accepting
        assert status.pending_cleanup == 1
        await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("blocked"),
            ),
            ConfigUpdateErrorCode.UPDATE_UNAVAILABLE,
        )

        client.aclose.side_effect = None
        await harness.coordinator.close()
        assert harness.manager.failed_unpublished_generations == ()
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.STOPPED
        assert harness.coordinator.status_snapshot.pending_cleanup == 0
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_retired_generation_cleanup_failure_blocks_status_until_manager_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        persistent = Mock()
        persistent.aclose = AsyncMock(
            side_effect=[
                RuntimeError("secret one"),
                RuntimeError("secret two"),
                RuntimeError("secret three"),
                None,
            ]
        )
        harness = await _make_harness(
            tmp_path,
            monkeypatch,
            initial_proxy_http_clients={"cleanup": persistent},
        )

        first_result = await harness.coordinator.update(
            base_snapshot=harness.initial_snapshot,
            config_file=ConfigFile.MODEL_RULES,
            candidate_bytes=_model_rules("candidate"),
        )
        await harness.wait_for_retirement()

        assert harness.manager.failed_generations == (1,)
        status = harness.coordinator.status_snapshot
        assert not status.accepting
        assert status.pending_cleanup == 1

        built_before = len(harness.built)
        await _expect_update_error(
            harness.coordinator.update(
                # Use the fresh (published) snapshot so this call reaches
                # admission with a non-stale generation, exercising the
                # unresolved_cleanup_generations gate rather than the
                # unrelated generation-staleness check.
                base_snapshot=first_result.snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("blocked"),
            ),
            ConfigUpdateErrorCode.UPDATE_UNAVAILABLE,
        )
        assert len(harness.built) == built_before  # candidate must not be built

        persistent.aclose.side_effect = None
        await harness.coordinator.close()
        assert harness.manager.failed_generations == ()
        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.STOPPED
        assert status.pending_cleanup == 0
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_admitted_commit_precondition_rejects_on_failed_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        # Synthetic state: in practice `clients` is never empty when a
        # generation lands in `_failed_generations` (see `_finish_cleanup`),
        # but `failed_generations` only inspects the dict's keys, so an
        # empty `clients` mapping is sufficient to exercise this precondition.
        harness.manager._failed_generations[999] = _FailedGeneration(  # noqa: SLF001
            generation=999,
            clients={},
        )

        with pytest.raises(ConfigUpdateError) as raised:
            harness.coordinator._raise_if_admitted_update_cannot_commit()  # noqa: SLF001

        assert raised.value.code is ConfigUpdateErrorCode.UPDATE_UNAVAILABLE
        await harness.shutdown()

    run_async(scenario())


@pytest.mark.parametrize(
    ("config_file", "candidate_bytes"),
    [
        (
            ConfigFile.PROVIDERS,
            json.dumps(VALID_PAYLOADS[ConfigFile.PROVIDERS], separators=(",", ":")).encode(),
        ),
        (
            ConfigFile.FALLBACK_RULES,
            json.dumps(
                VALID_PAYLOADS[ConfigFile.FALLBACK_RULES],
                separators=(",", ":"),
            ).encode(),
        ),
        (ConfigFile.MODEL_RULES, _model_rules("all-files")),
        (ConfigFile.OPERATION_RULES, b"{\n}"),
        (ConfigFile.FUSION_RULES, b"[\n]"),
        (ConfigFile.ROUTER_RULES, b"[\n]"),
    ],
)
def test_each_config_file_identity_commits_complete_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_file: ConfigFile,
    candidate_bytes: bytes,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            result = await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=config_file,
                candidate_bytes=candidate_bytes,
                expected_revision=_revision(harness.initial_snapshot, config_file),
            )

            assert result.snapshot.generation == 2
            assert harness.manager.current_generation == 2
            assert (tmp_path / FILENAMES[config_file]).read_bytes() == candidate_bytes
            bundle = result.snapshot.config_loader.source_bundle
            assert isinstance(bundle, ConfigSourceBundle)
            assert bundle[config_file].content == candidate_bytes
            assert bundle[config_file].metadata is not None
            assert bundle == bundle.recapture()
            assert _owned_artifacts(tmp_path) == ()
        finally:
            await harness.shutdown()

    run_async(scenario())


@pytest.mark.parametrize("same_file", [True, False], ids=["same-file", "different-files"])
def test_concurrent_updates_from_same_generation_publish_exactly_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    same_file: bool,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        real_begin = AtomicConfigFileTransaction.begin
        real_prepare = AtomicConfigFileTransaction.prepare
        transactions: list[AtomicConfigFileTransaction] = []
        max_journals = 0

        def observed_begin(
            _cls: type[AtomicConfigFileTransaction],
            *args: object,
            **kwargs: object,
        ) -> AtomicConfigFileTransaction:
            assert harness.coordinator._commit_lock.locked()  # noqa: SLF001
            transaction = real_begin(*args, **kwargs)  # type: ignore[arg-type]
            transactions.append(transaction)
            return transaction

        def observed_prepare(transaction: AtomicConfigFileTransaction) -> None:
            nonlocal max_journals
            assert harness.coordinator._commit_lock.locked()  # noqa: SLF001
            real_prepare(transaction)
            max_journals = max(max_journals, len(_journal_artifacts(tmp_path)))

        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "begin",
            classmethod(observed_begin),
        )
        monkeypatch.setattr(AtomicConfigFileTransaction, "prepare", observed_prepare)
        first_file = ConfigFile.MODEL_RULES
        second_file = first_file if same_file else ConfigFile.OPERATION_RULES
        updates = (
            (first_file, _model_rules("concurrent-first")),
            (
                second_file,
                _model_rules("concurrent-second") if same_file else b"{\n}",
            ),
        )
        original_bytes = {
            config_file: (tmp_path / FILENAMES[config_file]).read_bytes()
            for config_file in {first_file, second_file}
        }
        entered = 0
        both_entered = asyncio.Event()
        release = asyncio.Event()

        async def preflight(_snapshot: RuntimeSnapshot) -> None:
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()

        tasks = [
            asyncio.create_task(
                harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=config_file,
                    candidate_bytes=candidate_bytes,
                    expected_revision=_revision(
                        harness.initial_snapshot,
                        config_file,
                    ),
                    preflight=preflight,
                )
            )
            for config_file, candidate_bytes in updates
        ]
        await both_entered.wait()
        release.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        winners = [
            index
            for index, outcome in enumerate(outcomes)
            if not isinstance(outcome, BaseException)
        ]
        losers = [
            index
            for index, outcome in enumerate(outcomes)
            if isinstance(outcome, ConfigUpdateError)
            and outcome.code is ConfigUpdateErrorCode.GENERATION_STALE
        ]
        assert len(winners) == 1
        assert len(losers) == 1
        winner_index = winners[0]
        loser_index = losers[0]
        winner_result = outcomes[winner_index]
        assert not isinstance(winner_result, BaseException)
        assert winner_result.snapshot.generation == 2
        assert harness.manager.current_generation == 2

        winner_file, winner_bytes = updates[winner_index]
        loser_file, _loser_bytes = updates[loser_index]
        assert (tmp_path / FILENAMES[winner_file]).read_bytes() == winner_bytes
        if loser_file is not winner_file:
            assert (tmp_path / FILENAMES[loser_file]).read_bytes() == original_bytes[loser_file]
        winner_candidate = next(
            candidate
            for candidate in harness.built
            if candidate.snapshot is winner_result.snapshot
        )
        loser_candidate = next(
            candidate for candidate in harness.built if candidate is not winner_candidate
        )
        assert winner_candidate.publish_calls == 1
        assert winner_candidate.close_calls == 0
        assert loser_candidate.publish_calls == 0
        assert loser_candidate.close_calls == 1
        assert len(transactions) == 1
        assert max_journals == 1
        assert harness.manager.pending_unpublished_generations == ()
        assert _owned_artifacts(tmp_path) == ()
        await harness.shutdown()

    run_async(scenario())


def test_concurrent_same_file_terminal_failure_never_exposes_multiple_journals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CommitInterrupted(BaseException):
        pass

    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        primary = _CommitInterrupted("commit interrupted")
        real_prepare = AtomicConfigFileTransaction.prepare
        real_commit = AtomicConfigFileTransaction.commit
        journal_counts: list[int] = []
        commit_calls = 0
        entered = 0
        both_entered = asyncio.Event()
        release = asyncio.Event()

        async def preflight(_snapshot: RuntimeSnapshot) -> None:
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await release.wait()

        def observed_prepare(transaction: AtomicConfigFileTransaction) -> None:
            assert harness.coordinator._commit_lock.locked()  # noqa: SLF001
            real_prepare(transaction)
            journal_counts.append(len(_journal_artifacts(tmp_path)))

        def interrupt_first_commit(transaction: AtomicConfigFileTransaction) -> None:
            nonlocal commit_calls
            assert harness.coordinator._commit_lock.locked()  # noqa: SLF001
            commit_calls += 1
            journal_counts.append(len(_journal_artifacts(tmp_path)))
            if commit_calls == 1:
                raise primary
            real_commit(transaction)

        monkeypatch.setattr(AtomicConfigFileTransaction, "prepare", observed_prepare)
        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "commit",
            interrupt_first_commit,
        )
        tasks = [
            asyncio.create_task(
                harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                    candidate_bytes=_model_rules(alias),
                    expected_revision=_revision(
                        harness.initial_snapshot,
                        ConfigFile.MODEL_RULES,
                    ),
                    preflight=preflight,
                )
            )
            for alias in ("interrupted", "winner")
        ]
        await both_entered.wait()
        release.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        assert sum(outcome is primary for outcome in outcomes) == 1
        results = [
            outcome for outcome in outcomes if not isinstance(outcome, BaseException)
        ]
        assert len(results) == 1
        assert results[0].snapshot.generation == 2
        assert commit_calls == 2
        assert journal_counts
        assert min(journal_counts) == 1
        assert max(journal_counts) == 1
        assert sum(candidate.close_calls for candidate in harness.built) == 1
        assert sum(candidate.publish_calls for candidate in harness.built) == 1
        assert harness.manager.current_generation == 2
        assert harness.manager.pending_unpublished_generations == ()
        assert _owned_artifacts(tmp_path) == ()
        await harness.shutdown()

    run_async(scenario())


def test_invalid_candidate_load_leaves_disk_and_runtime_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        target = tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
        original = target.read_bytes()
        try:
            await _expect_update_error(
                harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                    candidate_bytes=b"not-json",
                ),
                ConfigUpdateErrorCode.VALIDATION_FAILED,
            )
            assert target.read_bytes() == original
            assert harness.manager.current_generation == 1
            assert harness.coordinator.status_snapshot.active_updates == 0
            assert harness.manager.pending_unpublished_generations == ()
            assert harness.built == []
            assert _owned_artifacts(tmp_path) == ()
        finally:
            await harness.shutdown()

    run_async(scenario())


@pytest.mark.parametrize(
    ("candidate_bytes", "expected_errors"),
    [
        (
            json.dumps(
                [
                    {
                        "gateway_model_name": "gateway/chat",
                        "fallback_models": [
                            {"provider": "ghost", "model": "upstream-chat"}
                        ],
                    }
                ]
            ).encode(),
            (
                {
                    "type": "rule_validation",
                    "loc": [],
                    "msg": (
                        "Invalid provider 'ghost' used in fallback rule for "
                        "'gateway/chat'. Provider not found in configuration."
                    ),
                },
            ),
        ),
        (
            json.dumps(
                [
                    {
                        "gateway_model_name": "gateway/chat",
                        "fallback_models": [
                            {"provider": "primary", "model": {"k": "sk-MUST-NOT-LEAK"}}
                        ],
                    }
                ]
            ).encode(),
            None,
        ),
    ],
    ids=["rule-validation", "quotes-submitted-bytes"],
)
def test_rejected_candidate_reports_only_the_rule_validation_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_bytes: bytes,
    expected_errors: tuple[dict[str, object], ...] | None,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            error = await _expect_update_error(
                harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.FALLBACK_RULES,
                    candidate_bytes=candidate_bytes,
                ),
                ConfigUpdateErrorCode.VALIDATION_FAILED,
            )
            assert error.errors == expected_errors
            assert "MUST-NOT-LEAK" not in repr(error.errors)
        finally:
            await harness.shutdown()

    run_async(scenario())


def test_cancelled_candidate_load_preserves_exact_identity_and_no_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        target = tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
        original = target.read_bytes()
        primary = asyncio.CancelledError("load cancelled")

        def cancel_load(_loader: ConfigLoader) -> ConfigLoader:
            raise primary

        monkeypatch.setattr(ConfigLoader, "load_complete", cancel_load)
        with pytest.raises(asyncio.CancelledError) as raised:
            await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("candidate"),
            )

        assert raised.value is primary
        assert target.read_bytes() == original
        assert harness.manager.current_generation == 1
        assert harness.coordinator.status_snapshot.active_updates == 0
        assert harness.manager.pending_unpublished_generations == ()
        assert harness.built == []
        assert _owned_artifacts(tmp_path) == ()
        await harness.shutdown()

    run_async(scenario())


@pytest.mark.parametrize("fatal", [False, True], ids=["ordinary", "cancelled"])
def test_candidate_build_failure_preserves_boundary_and_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fatal: bool,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        target = tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
        original = target.read_bytes()
        primary: BaseException = (
            asyncio.CancelledError("build cancelled")
            if fatal
            else RuntimeError("build failed")
        )

        async def fail_build(**_kwargs: object) -> RuntimeCandidate:
            raise primary

        monkeypatch.setattr(
            "llm_gateway_core.services.runtime_candidate.build_runtime_candidate",
            fail_build,
        )
        monkeypatch.setattr(
            harness.coordinator,
            "_build_candidate",
            MethodType(ConfigUpdateCoordinator._build_candidate, harness.coordinator),
        )

        if fatal:
            with pytest.raises(asyncio.CancelledError) as raised:
                await harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                    candidate_bytes=_model_rules("candidate"),
                )
            assert raised.value is primary
        else:
            await _expect_update_error(
                harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                    candidate_bytes=_model_rules("candidate"),
                ),
                ConfigUpdateErrorCode.COMMIT_FAILED,
            )

        assert target.read_bytes() == original
        assert harness.manager.current_generation == 1
        assert harness.coordinator.status_snapshot.active_updates == 0
        assert harness.manager.pending_unpublished_generations == ()
        assert _owned_artifacts(tmp_path) == ()
        await harness.shutdown()

    run_async(scenario())


def test_preflight_exception_message_surfaces_as_validation_error_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        message = (
            "Gateway model 'llmgateway/text': "
            "fallback model 'foo' is not available from provider 'bar'."
        )

        async def reject(_snapshot: RuntimeSnapshot) -> None:
            raise ValueError(message)

        error = await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("candidate"),
                preflight=reject,
            ),
            ConfigUpdateErrorCode.VALIDATION_FAILED,
        )

        assert error.errors == (
            {"type": "preflight", "loc": [], "msg": message},
        )
        await harness.shutdown()

    run_async(scenario())


@pytest.mark.parametrize("fatal", [False, True], ids=["ordinary", "cancelled"])
def test_preflight_failure_closes_candidate_and_preserves_primary_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fatal: bool,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        target = tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
        original = target.read_bytes()
        primary: BaseException = (
            asyncio.CancelledError("preflight cancelled")
            if fatal
            else RuntimeError("preflight failed")
        )

        async def fail_preflight(_snapshot: RuntimeSnapshot) -> None:
            raise primary

        if fatal:
            with pytest.raises(asyncio.CancelledError) as raised:
                await harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                    candidate_bytes=_model_rules("candidate"),
                    preflight=fail_preflight,
                )
            assert raised.value is primary
        else:
            await _expect_update_error(
                harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                    candidate_bytes=_model_rules("candidate"),
                    preflight=fail_preflight,
                ),
                ConfigUpdateErrorCode.VALIDATION_FAILED,
            )

        assert target.read_bytes() == original
        assert harness.manager.current_generation == 1
        assert harness.coordinator.status_snapshot.active_updates == 0
        assert harness.manager.pending_unpublished_generations == ()
        assert harness.built[0].close_calls == 1
        assert _owned_artifacts(tmp_path) == ()
        await harness.shutdown()

    run_async(scenario())


def test_manager_busy_and_stopping_have_stable_error_mappings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)

        async def busy_build(**_kwargs: object) -> RuntimeCandidate:
            raise RuntimeManagerStateError("busy")

        monkeypatch.setattr(
            "llm_gateway_core.services.runtime_candidate.build_runtime_candidate",
            busy_build,
        )
        monkeypatch.setattr(
            harness.coordinator,
            "_build_candidate",
            MethodType(ConfigUpdateCoordinator._build_candidate, harness.coordinator),
        )
        await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("busy"),
            ),
            ConfigUpdateErrorCode.GENERATION_BUSY,
        )

        harness.manager._status = RuntimeManagerStatus.STOPPING  # noqa: SLF001
        await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("stopping"),
            ),
            ConfigUpdateErrorCode.UPDATE_UNAVAILABLE,
        )
        harness.manager._status = RuntimeManagerStatus.RUNNING  # noqa: SLF001
        assert harness.coordinator.status_snapshot.active_updates == 0
        assert harness.manager.pending_unpublished_generations == ()
        assert _owned_artifacts(tmp_path) == ()
        await harness.shutdown()

    run_async(scenario())


def test_ordinary_rollback_failure_is_sticky_and_close_retries_exact_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        config_file = ConfigFile.MODEL_RULES
        target = tmp_path / FILENAMES[config_file]
        original = target.read_bytes()
        original_stat = target.stat()
        real_rollback = AtomicConfigFileTransaction.rollback

        def fail_rollback(transaction: AtomicConfigFileTransaction) -> None:
            raise AtomicConfigTransactionError(
                transaction.config_file,
                "injected rollback failure",
            )

        monkeypatch.setattr(AtomicConfigFileTransaction, "rollback", fail_rollback)
        candidate_bytes = _model_rules("candidate")
        harness.next_publish_error = RuntimeError("publish failed")
        await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=config_file,
                candidate_bytes=candidate_bytes,
            ),
            ConfigUpdateErrorCode.UPDATE_BROKEN,
        )

        assert target.read_bytes() == candidate_bytes
        assert harness.manager.current_generation == 1
        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.BROKEN
        assert not status.accepting
        assert status.pending_cleanup == 1
        await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=config_file,
                candidate_bytes=_model_rules("blocked"),
            ),
            ConfigUpdateErrorCode.UPDATE_BROKEN,
        )

        monkeypatch.setattr(AtomicConfigFileTransaction, "rollback", real_rollback)
        await harness.coordinator.close()
        restored_stat = target.stat()
        assert target.read_bytes() == original
        assert stat.S_IMODE(restored_stat.st_mode) == stat.S_IMODE(original_stat.st_mode)
        assert (restored_stat.st_uid, restored_stat.st_gid) == (
            original_stat.st_uid,
            original_stat.st_gid,
        )
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.STOPPED
        assert harness.coordinator.status_snapshot.pending_cleanup == 0
        assert _owned_artifacts(tmp_path) == ()
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_recovery_required_marks_broken_and_rejects_next_update_without_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        real_begin = AtomicConfigFileTransaction.begin
        transactions: list[AtomicConfigFileTransaction] = []

        def capture_begin(
            *args: object,
            **kwargs: object,
        ) -> AtomicConfigFileTransaction:
            transaction = real_begin(*args, **kwargs)  # type: ignore[arg-type]
            transactions.append(transaction)
            return transaction

        def require_startup_recovery(
            transaction: AtomicConfigFileTransaction,
        ) -> None:
            transaction._state = (  # noqa: SLF001
                AtomicConfigTransactionState.RECOVERY_REQUIRED
            )
            raise AtomicConfigTransactionError(
                transaction.config_file,
                "injected recovery requirement",
            )

        begin = Mock(side_effect=capture_begin)
        abort = Mock(side_effect=AssertionError("unsafe abort attempted"))
        rollback = Mock(side_effect=AssertionError("unsafe rollback attempted"))
        finalize = Mock(side_effect=AssertionError("unsafe finalize attempted"))
        monkeypatch.setattr(AtomicConfigFileTransaction, "begin", begin)
        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "commit",
            require_startup_recovery,
        )
        monkeypatch.setattr(AtomicConfigFileTransaction, "abort", abort)
        monkeypatch.setattr(AtomicConfigFileTransaction, "rollback", rollback)
        monkeypatch.setattr(AtomicConfigFileTransaction, "finalize", finalize)

        await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("recovery-required"),
            ),
            ConfigUpdateErrorCode.UPDATE_BROKEN,
        )

        assert len(transactions) == 1
        assert transactions[0].state is AtomicConfigTransactionState.RECOVERY_REQUIRED
        assert len(_journal_artifacts(tmp_path)) == 1
        assert harness.manager.current_generation == 1
        assert harness.built[0].close_calls == 1
        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.BROKEN
        assert not status.accepting
        assert status.pending_cleanup == 0
        assert status.last_failure is not None
        assert status.last_failure.phase == "transaction_recovery"
        assert status.last_failure.exception_type == "AtomicConfigTransactionError"

        await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("blocked"),
            ),
            ConfigUpdateErrorCode.UPDATE_BROKEN,
        )

        assert begin.call_count == 1
        assert len(harness.built) == 1
        abort.assert_not_called()
        rollback.assert_not_called()
        finalize.assert_not_called()
        await harness.coordinator.close()
        abort.assert_not_called()
        rollback.assert_not_called()
        finalize.assert_not_called()
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_recovery_required_preserves_terminal_identity_and_journal_through_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CommitInterrupted(BaseException):
        pass

    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        primary = _CommitInterrupted("commit interrupted")
        real_begin = AtomicConfigFileTransaction.begin
        transactions: list[AtomicConfigFileTransaction] = []

        def capture_begin(
            *args: object,
            **kwargs: object,
        ) -> AtomicConfigFileTransaction:
            transaction = real_begin(*args, **kwargs)  # type: ignore[arg-type]
            transactions.append(transaction)
            return transaction

        def interrupt_commit(transaction: AtomicConfigFileTransaction) -> None:
            transaction._state = (  # noqa: SLF001
                AtomicConfigTransactionState.RECOVERY_REQUIRED
            )
            raise primary

        abort = Mock(side_effect=AssertionError("unsafe abort attempted"))
        rollback = Mock(side_effect=AssertionError("unsafe rollback attempted"))
        finalize = Mock(side_effect=AssertionError("unsafe finalize attempted"))
        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "begin",
            Mock(side_effect=capture_begin),
        )
        monkeypatch.setattr(AtomicConfigFileTransaction, "commit", interrupt_commit)
        monkeypatch.setattr(AtomicConfigFileTransaction, "abort", abort)
        monkeypatch.setattr(AtomicConfigFileTransaction, "rollback", rollback)
        monkeypatch.setattr(AtomicConfigFileTransaction, "finalize", finalize)

        with pytest.raises(_CommitInterrupted) as raised:
            await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("terminal-recovery"),
            )

        assert raised.value is primary
        assert len(transactions) == 1
        assert transactions[0].state is AtomicConfigTransactionState.RECOVERY_REQUIRED
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.BROKEN
        artifacts_before = {
            artifact.name: (artifact.read_bytes(), artifact.stat().st_ino)
            for artifact in _owned_artifacts(tmp_path)
        }
        assert len(_journal_artifacts(tmp_path)) == 1

        await harness.coordinator.close()

        artifacts_after = {
            artifact.name: (artifact.read_bytes(), artifact.stat().st_ino)
            for artifact in _owned_artifacts(tmp_path)
        }
        assert artifacts_after == artifacts_before
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.BROKEN
        assert harness.coordinator.status_snapshot.last_failure is not None
        assert harness.coordinator.status_snapshot.last_failure.phase == "transaction_recovery"
        abort.assert_not_called()
        rollback.assert_not_called()
        finalize.assert_not_called()
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_prepare_failure_occurs_under_commit_lock_and_releases_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        real_begin = AtomicConfigFileTransaction.begin
        real_prepare = AtomicConfigFileTransaction.prepare
        real_abort = AtomicConfigFileTransaction.abort
        prepare_calls = 0
        abort_calls = 0

        def observed_begin(*args: object, **kwargs: object) -> AtomicConfigFileTransaction:
            assert harness.coordinator._commit_lock.locked()  # noqa: SLF001
            return real_begin(*args, **kwargs)  # type: ignore[arg-type]

        def fail_first_prepare(transaction: AtomicConfigFileTransaction) -> None:
            nonlocal prepare_calls
            assert harness.coordinator._commit_lock.locked()  # noqa: SLF001
            prepare_calls += 1
            if prepare_calls == 1:
                raise AtomicConfigTransactionError(
                    transaction.config_file,
                    "injected prepare failure",
                )
            real_prepare(transaction)

        def observed_abort(transaction: AtomicConfigFileTransaction) -> None:
            nonlocal abort_calls
            assert harness.coordinator._commit_lock.locked()  # noqa: SLF001
            abort_calls += 1
            real_abort(transaction)

        begin = Mock(side_effect=observed_begin)
        monkeypatch.setattr(AtomicConfigFileTransaction, "begin", begin)
        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "prepare",
            fail_first_prepare,
        )
        monkeypatch.setattr(AtomicConfigFileTransaction, "abort", observed_abort)

        await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("failed"),
            ),
            ConfigUpdateErrorCode.COMMIT_FAILED,
        )

        assert not harness.coordinator._commit_lock.locked()  # noqa: SLF001
        assert begin.call_count == 1
        assert prepare_calls == 1
        assert abort_calls == 1
        assert harness.built[0].close_calls == 1
        assert harness.coordinator.status_snapshot.accepting
        assert _owned_artifacts(tmp_path) == ()

        result = await harness.coordinator.update(
            base_snapshot=harness.initial_snapshot,
            config_file=ConfigFile.MODEL_RULES,
            candidate_bytes=_model_rules("retry"),
        )

        assert result.snapshot.generation == 2
        assert begin.call_count == 2
        assert prepare_calls == 2
        assert abort_calls == 1
        assert _owned_artifacts(tmp_path) == ()
        await harness.shutdown()

    run_async(scenario())


@pytest.mark.parametrize("phase", ["begin", "prepare", "commit"])
def test_atomic_transaction_phase_errors_map_to_commit_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        config_file = ConfigFile.MODEL_RULES
        target = tmp_path / FILENAMES[config_file]
        original = target.read_bytes()

        if phase == "begin":
            def fail_begin(
                _cls: type[AtomicConfigFileTransaction],
                *_args: object,
                **_kwargs: object,
            ) -> AtomicConfigFileTransaction:
                raise AtomicConfigTransactionError(config_file, "injected begin failure")

            monkeypatch.setattr(
                AtomicConfigFileTransaction,
                "begin",
                classmethod(fail_begin),
            )
        elif phase == "prepare":
            def fail_prepare(transaction: AtomicConfigFileTransaction) -> None:
                raise AtomicConfigTransactionError(
                    transaction.config_file,
                    "injected prepare failure",
                )

            monkeypatch.setattr(
                AtomicConfigFileTransaction,
                "prepare",
                fail_prepare,
            )
        else:
            def fail_commit(transaction: AtomicConfigFileTransaction) -> None:
                raise AtomicConfigTransactionError(
                    transaction.config_file,
                    "injected commit failure",
                )

            monkeypatch.setattr(
                AtomicConfigFileTransaction,
                "commit",
                fail_commit,
            )

        await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=config_file,
                candidate_bytes=_model_rules("candidate"),
            ),
            ConfigUpdateErrorCode.COMMIT_FAILED,
        )

        assert target.read_bytes() == original
        assert harness.manager.current_generation == 1
        assert harness.coordinator.status_snapshot.active_updates == 0
        assert harness.coordinator.status_snapshot.accepting
        assert harness.manager.pending_unpublished_generations == ()
        assert harness.built[0].close_calls == 1
        assert _owned_artifacts(tmp_path) == ()
        await harness.shutdown()

    run_async(scenario())


@pytest.mark.parametrize("terminal", [False, True])
def test_comments_backup_prepare_failure_precedes_transaction_and_cleans_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: bool,
) -> None:
    class _PrepareInterrupted(BaseException):
        pass

    async def scenario() -> None:
        harness = await _make_harness(
            tmp_path,
            monkeypatch,
            model_rules_bytes=_commented_model_rules("original"),
        )
        real_prepare = CommentsBackupLifecycle.prepare
        primary: BaseException = (
            _PrepareInterrupted("terminal prepare secret")
            if terminal
            else RuntimeError("ordinary prepare secret")
        )

        def fail_prepare(lifecycle: CommentsBackupLifecycle) -> None:
            if terminal:
                real_prepare(lifecycle)
            raise primary

        transaction_begin = Mock(
            side_effect=AssertionError("transaction began after backup failure")
        )
        monkeypatch.setattr(CommentsBackupLifecycle, "prepare", fail_prepare)
        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "begin",
            transaction_begin,
        )
        try:
            if terminal:
                with pytest.raises(_PrepareInterrupted) as raised:
                    await harness.coordinator.update(
                        base_snapshot=harness.initial_snapshot,
                        config_file=ConfigFile.MODEL_RULES,
                        candidate_bytes=_model_rules("candidate"),
                        comments_backup=True,
                    )
                assert raised.value is primary
            else:
                error = await _expect_update_error(
                    harness.coordinator.update(
                        base_snapshot=harness.initial_snapshot,
                        config_file=ConfigFile.MODEL_RULES,
                        candidate_bytes=_model_rules("candidate"),
                        comments_backup=True,
                    ),
                    ConfigUpdateErrorCode.COMMIT_FAILED,
                )
                assert "prepare secret" not in str(error)
            transaction_begin.assert_not_called()
            assert _comments_backups(tmp_path) == ()
            assert _owned_artifacts(tmp_path) == ()
            assert harness.built[0].close_calls == 1
            assert harness.coordinator.status_snapshot.accepting
        finally:
            await harness.shutdown()

    run_async(scenario())


@pytest.mark.parametrize("terminal", [False, True])
def test_comments_backup_abort_failure_is_retry_owned_after_transaction_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: bool,
) -> None:
    class _AbortInterrupted(BaseException):
        pass

    async def scenario() -> None:
        harness = await _make_harness(
            tmp_path,
            monkeypatch,
            model_rules_bytes=_commented_model_rules("original"),
        )
        events: list[str] = []
        real_rollback = AtomicConfigFileTransaction.rollback
        real_abort = CommentsBackupLifecycle.abort
        abort_calls = 0
        primary: BaseException = (
            _AbortInterrupted("terminal abort secret")
            if terminal
            else RuntimeError("ordinary abort secret")
        )

        def record_rollback(transaction: AtomicConfigFileTransaction) -> None:
            events.append("transaction_rollback")
            real_rollback(transaction)

        def fail_first_abort(lifecycle: CommentsBackupLifecycle) -> None:
            nonlocal abort_calls
            abort_calls += 1
            events.append("backup_abort")
            if abort_calls == 1:
                raise primary
            real_abort(lifecycle)

        harness.next_publish_error = RuntimeError("publish rejected")
        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "rollback",
            record_rollback,
        )
        monkeypatch.setattr(CommentsBackupLifecycle, "abort", fail_first_abort)

        if terminal:
            with pytest.raises(_AbortInterrupted) as raised:
                await harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                    candidate_bytes=_model_rules("candidate"),
                    comments_backup=True,
                )
            assert raised.value is primary
        else:
            await _expect_update_error(
                harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                    candidate_bytes=_model_rules("candidate"),
                    comments_backup=True,
                ),
                ConfigUpdateErrorCode.COMMIT_FAILED,
            )

        assert events[:2] == ["transaction_rollback", "backup_abort"]
        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.RUNNING
        assert not status.accepting
        assert status.pending_cleanup == 1
        assert len(_comments_backups(tmp_path)) == 1
        assert harness.manager.current_generation == 1

        await harness.coordinator.close()
        assert abort_calls == 2
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.STOPPED
        assert _comments_backups(tmp_path) == ()
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


@pytest.mark.parametrize("terminal", [False, True])
def test_comments_backup_publish_failure_is_postpublish_retry_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: bool,
) -> None:
    class _PublishInterrupted(BaseException):
        pass

    async def scenario() -> None:
        harness = await _make_harness(
            tmp_path,
            monkeypatch,
            model_rules_bytes=_commented_model_rules("original"),
        )
        events: list[str] = []
        real_publish = CommentsBackupLifecycle.publish
        real_finalize = AtomicConfigFileTransaction.finalize
        publish_calls = 0
        primary: BaseException = (
            _PublishInterrupted("terminal publish secret")
            if terminal
            else RuntimeError("ordinary publish secret")
        )

        def fail_first_publish(lifecycle: CommentsBackupLifecycle):  # type: ignore[no-untyped-def]
            nonlocal publish_calls
            publish_calls += 1
            events.append("backup_publish")
            if publish_calls == 1:
                raise primary
            return real_publish(lifecycle)

        def record_finalize(transaction: AtomicConfigFileTransaction) -> None:
            events.append("transaction_finalize")
            real_finalize(transaction)

        monkeypatch.setattr(
            CommentsBackupLifecycle,
            "publish",
            fail_first_publish,
        )
        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "finalize",
            record_finalize,
        )

        if terminal:
            with pytest.raises(_PublishInterrupted) as raised:
                await harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.MODEL_RULES,
                    candidate_bytes=_model_rules("candidate"),
                    comments_backup=True,
                )
            assert raised.value is primary
            assert events == ["backup_publish"]
            assert harness.coordinator.status_snapshot.pending_cleanup == 2
        else:
            result = await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("candidate"),
                comments_backup=True,
            )
            assert result.comments_backup is None
            assert result.cleanup_pending
            assert events == ["backup_publish", "transaction_finalize"]
            assert harness.coordinator.status_snapshot.pending_cleanup == 1

        assert harness.manager.current_generation == 2
        assert not harness.coordinator.status_snapshot.accepting
        await harness.coordinator.close()
        assert publish_calls == 2
        assert events == (
            ["backup_publish", "backup_publish", "transaction_finalize"]
            if terminal
            else ["backup_publish", "transaction_finalize", "backup_publish"]
        )
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.STOPPED
        assert len(_comments_backups(tmp_path)) == 1
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_ordinary_comments_backup_close_failure_stays_retryable_not_broken(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(
            tmp_path,
            monkeypatch,
            model_rules_bytes=_commented_model_rules("original"),
        )
        real_abort = CommentsBackupLifecycle.abort
        abort_calls = 0

        def fail_twice(lifecycle: CommentsBackupLifecycle) -> None:
            nonlocal abort_calls
            abort_calls += 1
            if abort_calls <= 2:
                raise RuntimeError("retryable backup cleanup secret")
            real_abort(lifecycle)

        harness.next_publish_error = RuntimeError("publish rejected")
        monkeypatch.setattr(CommentsBackupLifecycle, "abort", fail_twice)

        await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("candidate"),
                comments_backup=True,
            ),
            ConfigUpdateErrorCode.COMMIT_FAILED,
        )

        with pytest.raises(ConfigUpdateError) as first_close:
            await harness.coordinator.close()
        assert first_close.value.code is ConfigUpdateErrorCode.UPDATE_UNAVAILABLE
        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.STOPPING
        assert status.pending_cleanup == 1
        assert status.last_failure is not None
        assert status.last_failure.phase == "comments_backup_cleanup"
        assert "secret" not in repr(status.last_failure)

        await harness.coordinator.close()
        assert abort_calls == 3
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.STOPPED
        assert harness.coordinator.status_snapshot.pending_cleanup == 0
        assert _comments_backups(tmp_path) == ()
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_recovery_required_with_backup_retry_remains_sticky_broken(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        harness = await _make_harness(
            tmp_path,
            monkeypatch,
            model_rules_bytes=_commented_model_rules("original"),
        )
        real_backup_abort = CommentsBackupLifecycle.abort
        backup_abort_calls = 0

        def require_startup_recovery(
            transaction: AtomicConfigFileTransaction,
        ) -> None:
            transaction._state = (  # noqa: SLF001
                AtomicConfigTransactionState.RECOVERY_REQUIRED
            )
            raise AtomicConfigTransactionError(
                transaction.config_file,
                "injected recovery requirement",
            )

        def fail_backup_abort_twice(
            lifecycle: CommentsBackupLifecycle,
        ) -> None:
            nonlocal backup_abort_calls
            backup_abort_calls += 1
            if backup_abort_calls <= 2:
                raise RuntimeError("retryable backup cleanup secret")
            real_backup_abort(lifecycle)

        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "commit",
            require_startup_recovery,
        )
        monkeypatch.setattr(
            CommentsBackupLifecycle,
            "abort",
            fail_backup_abort_twice,
        )

        await _expect_update_error(
            harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=_model_rules("candidate"),
                comments_backup=True,
            ),
            ConfigUpdateErrorCode.UPDATE_BROKEN,
        )

        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.BROKEN
        assert status.pending_cleanup == 1
        assert len(_comments_backups(tmp_path)) == 1

        with pytest.raises(ConfigUpdateError) as failed_close:
            await harness.coordinator.close()
        assert failed_close.value.code is ConfigUpdateErrorCode.UPDATE_BROKEN
        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.BROKEN
        assert status.pending_cleanup == 1

        with pytest.raises(ConfigUpdateError) as still_broken:
            harness.coordinator.check_base(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
            )
        assert still_broken.value.code is ConfigUpdateErrorCode.UPDATE_BROKEN

        await harness.coordinator.close()
        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.BROKEN
        assert status.pending_cleanup == 0
        assert backup_abort_calls == 3
        assert _comments_backups(tmp_path) == ()

        with pytest.raises(ConfigUpdateError) as remains_broken:
            harness.coordinator.check_base(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
            )
        assert remains_broken.value.code is ConfigUpdateErrorCode.UPDATE_BROKEN
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_terminal_after_runtime_publish_preserves_disk_and_cleanup_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PostPublishInterrupted(BaseException):
        pass

    async def scenario() -> None:
        original = _commented_model_rules("original")
        candidate_bytes = _model_rules("candidate")
        harness = await _make_harness(
            tmp_path,
            monkeypatch,
            model_rules_bytes=original,
        )
        terminal = _PostPublishInterrupted("post-publish secret")
        harness.next_post_publish_error = terminal
        events: list[str] = []
        real_backup_publish = CommentsBackupLifecycle.publish
        real_finalize = AtomicConfigFileTransaction.finalize

        def record_backup_publish(lifecycle: CommentsBackupLifecycle):  # type: ignore[no-untyped-def]
            events.append("backup_publish")
            return real_backup_publish(lifecycle)

        def record_finalize(transaction: AtomicConfigFileTransaction) -> None:
            events.append("transaction_finalize")
            real_finalize(transaction)

        monkeypatch.setattr(
            CommentsBackupLifecycle,
            "publish",
            record_backup_publish,
        )
        monkeypatch.setattr(
            AtomicConfigFileTransaction,
            "finalize",
            record_finalize,
        )

        with pytest.raises(_PostPublishInterrupted) as raised:
            await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=candidate_bytes,
                comments_backup=True,
            )
        assert raised.value is terminal

        assert events == []
        assert harness.manager.current_generation == 2
        assert (
            tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
        ).read_bytes() == candidate_bytes
        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.RUNNING
        assert not status.accepting
        assert status.pending_cleanup == 2
        assert harness.built[0].close_calls == 0
        assert len(_comments_backups(tmp_path)) == 1

        await harness.coordinator.close()
        assert events == ["backup_publish", "transaction_finalize"]
        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.STOPPED
        assert status.pending_cleanup == 0
        assert harness.manager.current_generation == 2
        assert (
            tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
        ).read_bytes() == candidate_bytes
        backups = _comments_backups(tmp_path)
        assert len(backups) == 1
        assert backups[0].read_bytes() == original
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())


def test_terminal_during_owner_promotion_reclassifies_before_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PromotionInterrupted(BaseException):
        pass

    async def scenario() -> None:
        candidate_bytes = _model_rules("candidate")
        harness = await _make_harness(tmp_path, monkeypatch)
        terminal = _PromotionInterrupted("promotion secret")
        real_promote = harness.coordinator._promote_publication_owners  # noqa: SLF001
        promote_calls = 0

        def interrupt_twice(
            *,
            transaction: AtomicConfigFileTransaction,
            backup: CommentsBackupLifecycle | None,
        ) -> None:
            nonlocal promote_calls
            promote_calls += 1
            real_promote(transaction=transaction, backup=backup)
            if promote_calls <= 2:
                raise terminal

        monkeypatch.setattr(
            harness.coordinator,
            "_promote_publication_owners",
            interrupt_twice,
        )

        with pytest.raises(_PromotionInterrupted) as raised:
            await harness.coordinator.update(
                base_snapshot=harness.initial_snapshot,
                config_file=ConfigFile.MODEL_RULES,
                candidate_bytes=candidate_bytes,
            )
        assert raised.value is terminal
        assert promote_calls == 3
        assert harness.manager.current_generation == 2
        assert (
            tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
        ).read_bytes() == candidate_bytes
        status = harness.coordinator.status_snapshot
        assert status.state is ConfigUpdateState.RUNNING
        assert status.pending_cleanup == 1
        assert not status.accepting

        await harness.coordinator.close()
        assert harness.coordinator.status_snapshot.state is ConfigUpdateState.STOPPED
        assert harness.coordinator.status_snapshot.pending_cleanup == 0
        assert (
            tmp_path / FILENAMES[ConfigFile.MODEL_RULES]
        ).read_bytes() == candidate_bytes
        await harness.manager.shutdown()
        await harness.shared_client.aclose()

    run_async(scenario())
