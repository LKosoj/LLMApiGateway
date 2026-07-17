"""Secret-safe validation of the shared systemd/python-dotenv syntax."""

from __future__ import annotations

from docker._systemd_migration_fs import read_snapshot_bytes
from docker._systemd_migration_model import (
    RESERVED_ENVIRONMENT_KEYS,
    FileSnapshot,
    MigrationError,
)
from llm_gateway_core.config.environment import (
    EnvironmentSubsetError,
    parse_environment_subset,
)


_MAX_ENVIRONMENT_BYTES = 1024 * 1024


def environment_assignments(snapshot: FileSnapshot) -> list[dict[str, int | str | bool]]:
    if snapshot.size > _MAX_ENVIRONMENT_BYTES:
        raise MigrationError("environment-too-large")
    payload = read_snapshot_bytes(
        snapshot,
        name=snapshot.path.name,
        maximum_size=_MAX_ENVIRONMENT_BYTES,
    )
    try:
        values = parse_environment_subset(payload.decode("utf-8"))
    except UnicodeError:
        raise MigrationError("unsupported-environment-syntax") from None
    except EnvironmentSubsetError as error:
        if error.reason == "duplicate-key":
            raise MigrationError("duplicate-environment-keys", error.names) from None
        raise MigrationError("unsupported-environment-syntax", error.names) from None
    reserved = tuple(sorted(set(values) & set(RESERVED_ENVIRONMENT_KEYS)))
    if reserved:
        raise MigrationError("reserved-environment-keys", reserved)
    return [
        {"name": name, "occurrences": 1, "duplicate": False}
        for name in sorted(values)
    ]
