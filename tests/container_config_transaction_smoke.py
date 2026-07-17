"""Real-image smoke for atomic writes through the mounted config directory."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

from llm_gateway_core.config.config_store import (
    AtomicConfigFileTransaction,
    ConfigFile,
    ConfigSourceBundle,
)
from llm_gateway_core.config.loader import ConfigLoader


def _artifacts(parent: Path) -> list[Path]:
    return sorted(parent.glob(".llmgateway-config-txn-*"))


def main() -> None:
    loader = ConfigLoader()
    initial = ConfigSourceBundle.capture(
        loader.project_root,
        overrides=loader.configured_paths,
    )
    target_file = ConfigFile.MODEL_RULES
    target = initial[target_file]
    if not target.exists or target.metadata is None:
        raise AssertionError("model rules smoke target must exist")
    parent = target.path.parent
    before_other = {
        config_file: document.digest
        for config_file, document in initial.documents.items()
        if config_file is not target_file
    }
    expected_mode = stat.S_IMODE(target.metadata.mode)
    expected_owner = (target.metadata.uid, target.metadata.gid)
    committed_bytes = b'{"container_smoke":"committed"}\r\n'
    rollback_candidate = b'{"container_smoke":"rolled-back"}\n'

    commit = AtomicConfigFileTransaction.begin(target, committed_bytes)
    commit.prepare()
    commit.commit()
    commit.finalize()

    committed_bundle = initial.recapture()
    committed = committed_bundle[target_file]
    assert committed.content == committed_bytes
    assert committed.metadata is not None
    assert stat.S_IMODE(committed.metadata.mode) == expected_mode
    assert (committed.metadata.uid, committed.metadata.gid) == expected_owner

    rollback = AtomicConfigFileTransaction.begin(committed, rollback_candidate)
    rollback.prepare()
    rollback.commit()
    rollback.rollback()

    final_bundle = committed_bundle.recapture()
    final = final_bundle[target_file]
    assert final.content == committed_bytes
    assert final.metadata is not None
    assert stat.S_IMODE(final.metadata.mode) == expected_mode
    assert (final.metadata.uid, final.metadata.gid) == expected_owner
    assert {
        config_file: document.digest
        for config_file, document in final_bundle.documents.items()
        if config_file is not target_file
    } == before_other
    assert _artifacts(parent) == []

    print(
        json.dumps(
            {
                "artifacts": 0,
                "gid": final.metadata.gid,
                "mode": oct(stat.S_IMODE(final.metadata.mode)),
                "sha256": hashlib.sha256(committed_bytes).hexdigest(),
                "uid": final.metadata.uid,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
