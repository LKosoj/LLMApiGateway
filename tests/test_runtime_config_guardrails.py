from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_PATH = PROJECT_ROOT / "providers.json"


def _is_env_reference(value: str) -> bool:
    return value.startswith("${") and value.endswith("}")


def _literal_provider_api_key_offenders(providers: list[object]) -> list[str]:
    offenders: list[str] = []
    for item in providers:
        if not isinstance(item, dict):
            continue
        for provider_name, details in item.items():
            if not isinstance(details, dict):
                continue
            api_key = details.get("apikey")
            if isinstance(api_key, str) and api_key and not _is_env_reference(api_key):
                offenders.append(provider_name)

            pools = details.get("upstream_key_pools")
            if not isinstance(pools, dict):
                continue
            for pool_name, pool in pools.items():
                if not isinstance(pool, dict):
                    continue
                keys = pool.get("keys")
                if not isinstance(keys, list):
                    continue
                for index, key_spec in enumerate(keys):
                    if not isinstance(key_spec, dict):
                        continue
                    pool_api_key = key_spec.get("apikey")
                    if isinstance(pool_api_key, str) and pool_api_key and not _is_env_reference(pool_api_key):
                        offenders.append(f"{provider_name}.upstream_key_pools.{pool_name}.keys[{index}]")
    return offenders


def test_worktree_providers_json_does_not_store_literal_api_keys():
    providers = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
    offenders = _literal_provider_api_key_offenders(providers)

    assert not offenders, (
        "providers.json must use environment placeholders for apikey; "
        f"literal values found for providers: {', '.join(sorted(offenders))}"
    )


def test_gitignore_has_local_runtime_config_overrides():
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "providers.local.json" in gitignore
    assert "providers.secret.json" in gitignore
    assert "models_*_rules.local.json" in gitignore
    assert "models_*_rules.secret.json" in gitignore
