import json
import os
from pathlib import Path

import pytest

from llm_gateway_core.config import environment
from llm_gateway_core.config.config_store import ConfigFile
from llm_gateway_core.config.validation import resolve_provider_config_api_keys
from llm_gateway_core.services.config_updates import ConfigUpdateError, ConfigUpdateErrorCode
from tests._async_compat import run_async
from tests.test_config_update_transaction import FILENAMES, _make_harness


@pytest.mark.parametrize("explicit", [False, True], ids=["project-env", "explicit-env"])
@pytest.mark.parametrize("operation", ["update", "resync"])
def test_config_update_reads_new_environment_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool, operation: str,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("REFRESH_EXISTING_KEY=original\n", encoding="utf-8")
    monkeypatch.setattr(environment, "PROJECT_DOTENV", env_path)
    if explicit:
        monkeypatch.setenv(environment.ENVIRONMENT_FILE_KEY, os.fspath(env_path))
    else:
        monkeypatch.delenv(environment.ENVIRONMENT_FILE_KEY, raising=False)
    monkeypatch.delenv("REFRESH_EXISTING_KEY", raising=False)
    monkeypatch.delenv("REFRESH_NEW_KEY", raising=False)
    environment.load_application_environment.cache_clear()
    try:
        environment.load_application_environment()

        async def scenario() -> None:
            harness = await _make_harness(tmp_path, monkeypatch)
            try:
                env_path.write_text(
                    "REFRESH_EXISTING_KEY=changed\nREFRESH_NEW_KEY=public,personal-key\n",
                    encoding="utf-8",
                )
                candidate = json.dumps([{
                    "primary": {
                        "baseUrl": "https://primary.example/v1",
                        "apikey": "${REFRESH_NEW_KEY}",
                    },
                }]).encode()
                if operation == "update":
                    result = await harness.coordinator.update(
                        base_snapshot=harness.initial_snapshot,
                        config_file=ConfigFile.PROVIDERS,
                        candidate_bytes=candidate,
                    )
                else:
                    (tmp_path / FILENAMES[ConfigFile.PROVIDERS]).write_bytes(candidate)
                    result = await harness.coordinator.resync(
                        base_snapshot=harness.initial_snapshot,
                    )
                provider = result.snapshot.config_loader.providers_config["primary"]
                assert resolve_provider_config_api_keys(provider) == ["public", "personal-key"]
                assert os.environ["REFRESH_EXISTING_KEY"] == "original"
                assert result.snapshot.generation == 2
            finally:
                await harness.shutdown()

        run_async(scenario())
    finally:
        environment.load_application_environment.cache_clear()


@pytest.mark.parametrize("operation", ["update", "resync"])
def test_config_update_rejects_invalid_explicit_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    env_path = tmp_path / "runtime.env"
    env_path.write_text("not an assignment\n", encoding="utf-8")
    monkeypatch.setenv(environment.ENVIRONMENT_FILE_KEY, os.fspath(env_path))

    async def scenario() -> None:
        harness = await _make_harness(tmp_path, monkeypatch)
        try:
            candidate = b'[{"primary":{"baseUrl":"https://changed.example/v1","apikey":"public"}}]'
            if operation == "update":
                action = harness.coordinator.update(
                    base_snapshot=harness.initial_snapshot,
                    config_file=ConfigFile.PROVIDERS,
                    candidate_bytes=candidate,
                )
            else:
                (tmp_path / FILENAMES[ConfigFile.PROVIDERS]).write_bytes(candidate)
                action = harness.coordinator.resync(base_snapshot=harness.initial_snapshot)
            before = (tmp_path / FILENAMES[ConfigFile.PROVIDERS]).read_bytes()
            with pytest.raises(ConfigUpdateError) as caught:
                await action
            assert caught.value.code == ConfigUpdateErrorCode.VALIDATION_FAILED
            assert harness.manager.current_generation == 1
            assert harness.built == []
            assert (tmp_path / FILENAMES[ConfigFile.PROVIDERS]).read_bytes() == before
        finally:
            await harness.shutdown()

    run_async(scenario())
