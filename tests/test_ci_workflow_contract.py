import re
import subprocess
import sys
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"

BROWSER_TEST_MODULES = (
    "tests/test_admin_pricing_browser.py",
    "tests/test_api_keys_modal_errors.py",
    "tests/test_audio_ui.py",
    "tests/test_embeddings_ui.py",
    "tests/test_free_models_browser.py",
    "tests/test_images_ui.py",
    "tests/test_operation_cost_calculator_browser.py",
    "tests/test_overview_activity_pages.py",
    "tests/test_p1_4_url_state_stale_snapshot.py",
    "tests/test_p1_5_mobile_readability_browser.py",
    "tests/test_p1_7_api_keys_ux_browser.py",
    "tests/test_p2_1_docs_ux.py",
    "tests/test_p2_2_playground_chat.py",
    "tests/test_p2_4_free_models_search_split.py",
    "tests/test_quota_ui.py",
    "tests/test_r5_2_rules_tabs_browser.py",
    "tests/test_r5_2_ui_acceptance_browser.py",
    "tests/test_r5_2_ui_primitives_browser.py",
    "tests/test_r5_8_api_keys_quota_i18n_browser.py",
    "tests/test_r5_8_docs_playground_i18n_browser.py",
    "tests/test_r5_8_final_acceptance_browser.py",
    "tests/test_r5_8_rejections_translator_i18n.py",
    "tests/test_r5_8_rules_i18n_browser.py",
    "tests/test_r5_8_usage_i18n_browser.py",
    "tests/test_rerank_ui.py",
    "tests/test_rules_editor_capability_autofill_browser.py",
    "tests/test_rules_editor_concurrency_browser.py",
    "tests/test_rules_editor_entity_panel_browser.py",
    "tests/test_rules_editor_errors_browser.py",
    "tests/test_ui_regression.py",
    "tests/test_web_ui.py",
)
PINNED_ACTIONS = {
    "actions/checkout": "34e114876b0b11c390a56381ad16ebd13914f8d5",
    "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
}


def _workflow_text() -> str:
    assert WORKFLOW_PATH.is_file(), "CI workflow must exist at .github/workflows/ci.yml"
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _is_ignored(relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", relative_path],
        cwd=PROJECT_ROOT,
        check=False,
    )
    return result.returncode == 0


def _job_text(name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-z][a-z0-9_-]*:\n|\Z)",
        _workflow_text(),
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"CI job {name!r} is missing"
    return match.group("body")


def test_only_yml_workflows_are_reincluded_from_hidden_root_directories():
    assert not _is_ignored(".github/workflows/ci.yml")
    assert _is_ignored(".github/workflows/ci.yaml")
    assert _is_ignored(".github/dependabot.yml")


def test_topology_lock_is_reincluded_for_reproducible_builds():
    assert not _is_ignored("frontend/topology/package-lock.json")
    assert not _is_ignored("frontend/editor/package-lock.json")


def test_ci_has_independent_required_jobs():
    workflow = _workflow_text()
    jobs = set(re.findall(r"^  ([a-z][a-z0-9_-]*):\n", workflow, flags=re.MULTILINE))

    assert {
        "quality",
        "tests",
        "browser",
        "ui-runtime",
        "rules-editor",
        "topology",
        "docker",
    } <= jobs


def test_every_external_action_is_pinned_to_the_reviewed_commit() -> None:
    action_refs = re.findall(r"uses:\s+([^@\s]+)@([^\s#]+)", _workflow_text())

    assert len(action_refs) == 15
    assert {action for action, _ref in action_refs} == set(PINNED_ACTIONS)
    for action, ref in action_refs:
        assert re.fullmatch(r"[0-9a-f]{40}", ref)
        assert ref == PINNED_ACTIONS[action]


def test_browser_marker_is_registered_and_applied_to_every_browser_module():
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]

    assert any(marker.partition(":")[0].strip() == "browser" for marker in markers)
    for module in BROWSER_TEST_MODULES:
        content = (PROJECT_ROOT / module).read_text(encoding="utf-8")
        if module == "tests/test_r5_8_rejections_translator_i18n.py":
            assert re.search(r"^@pytest\.mark\.browser$", content, flags=re.MULTILINE), module
            continue
        assert re.search(r"^pytestmark = pytest\.mark\.browser$", content, flags=re.MULTILINE), module


def test_browser_marker_collects_exactly_164_tests_from_the_expected_modules():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "browser"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    node_ids = [line for line in result.stdout.splitlines() if "::" in line]
    collected_modules = {node_id.partition("::")[0] for node_id in node_ids}

    assert result.returncode == 0, output
    assert "PytestUnknownMarkWarning" not in output
    assert len(node_ids) == 164
    assert collected_modules == set(BROWSER_TEST_MODULES)


def test_quality_job_uses_the_pinned_lock_and_runs_every_quality_gate():
    quality = _job_text("quality")

    assert "python -m pip install -r requirements-dev.txt" in quality
    assert "bash scripts/compile_requirements.sh" in quality
    assert "python scripts/check_dependency_contract.py --verify-bootstrap" in quality
    assert "git diff --exit-code" in quality
    assert "git status --porcelain" in quality
    for lock_path in (
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-research.txt",
        "requirements-container.txt",
        "compatibility-constraints.txt",
        "security-constraints.txt",
    ):
        assert quality.count(lock_path) >= 2
    assert "python -m pip check" in quality
    assert "python scripts/check_dependency_contract.py" in quality
    assert "python -m pip_audit -r requirements-dev.txt" in quality
    assert "python -m ruff check ." in quality
    assert "tests/test_dependencies_sync.py" in quality
    assert "tests/test_ci_workflow_contract.py" in quality
    assert "python -m playwright install" not in quality
    assert "pip install pytest" not in quality
    assert "pip install ruff" not in quality
    assert "pip install playwright" not in quality


def test_tests_job_runs_only_non_browser_tests_with_the_measured_coverage_gate():
    tests = _job_text("tests")

    assert f"actions/setup-node@{PINNED_ACTIONS['actions/setup-node']}" in tests
    assert 'node-version: "22"' in tests
    assert "frontend/ui-runtime/package-lock.json" in tests
    assert "frontend/editor/package-lock.json" in tests
    assert "python -m pip install -r requirements-dev.txt" in tests
    assert "npm ci --prefix frontend/ui-runtime" in tests
    assert "npm ci --prefix frontend/editor" in tests
    assert "python -m pip check" in tests
    assert 'python -m pytest -q -m "not browser"' in tests
    assert "--cov=llm_gateway_core" in tests
    assert "--cov=main" in tests
    assert "--cov-report=term-missing:skip-covered" in tests
    assert "--cov-fail-under=80" in tests
    assert "python -m playwright install" not in tests


def test_browser_job_proves_collection_and_cannot_silently_skip_tests():
    browser = _job_text("browser")

    assert "python -m pip install -r requirements-dev.txt" in browser
    assert 'node-version: "22"' in browser
    assert "npm ci --prefix frontend/ui-runtime" in browser
    assert "python -m pip check" in browser
    assert "python -m playwright install --with-deps chromium" in browser
    assert "python -m pytest --fixtures -q" in browser
    assert "python -m pytest --collect-only -q -m browser" in browser
    assert 'test "$collected" -eq 164' in browser
    assert "python -m pytest -q -rs -m browser" in browser
    assert "browser-pytest.log" in browser
    assert "skipped" in browser
    assert "164 passed" in browser


def test_ui_runtime_job_uses_the_exact_lock_and_rejects_bundle_drift():
    ui_runtime = _job_text("ui-runtime")

    assert 'node-version: "22"' in ui_runtime
    assert "frontend/ui-runtime/package-lock.json" in ui_runtime
    assert "npm ci --prefix frontend/ui-runtime" in ui_runtime
    assert "npm test --prefix frontend/ui-runtime" in ui_runtime
    assert "npm run build --prefix frontend/ui-runtime" in ui_runtime
    assert "npm run check --prefix frontend/ui-runtime" in ui_runtime
    assert "npm run check:i18n --prefix frontend/ui-runtime" in ui_runtime
    assert "git diff --exit-code" in ui_runtime
    assert "git status --porcelain" in ui_runtime
    assert "static/vendor/ui-runtime.bundle.js" in ui_runtime


def test_rules_editor_job_uses_the_exact_lock_and_rejects_bundle_drift():
    editor = _job_text("rules-editor")

    assert 'node-version: "22"' in editor
    assert "frontend/editor/package-lock.json" in editor
    assert "npm ci --prefix frontend/editor" in editor
    assert "npm test --prefix frontend/editor" in editor
    assert "npm run build --prefix frontend/editor" in editor
    assert "npm run check --prefix frontend/editor" in editor
    assert "git diff --exit-code" in editor
    assert "git status --porcelain" in editor
    assert "static/editor.js" in editor


def test_generated_topology_and_versioned_docker_image_are_checked():
    topology = _job_text("topology")
    docker = _job_text("docker")

    assert "npm ci" in topology
    assert "npm run build" in topology
    assert "git diff --exit-code" in topology
    assert "git status --porcelain" in topology
    assert "python3 scripts/check_product_version.py --print" in docker
    assert '"${GITHUB_REF_TYPE}" == "tag"' in docker
    assert '"${GITHUB_REF_NAME}" != "v${product_version}"' in docker
    assert "--target version-contract" in docker
    assert "docker build --target version-contract ." in docker
    assert "LLMGATEWAY_EXPECTED_PRODUCT_VERSION=0.0.0" in docker
    assert "docker compose config -q" in docker
    assert "docker build" in docker
    assert "--build-arg LLMGATEWAY_EXPECTED_PRODUCT_VERSION=" in docker
    assert "LLMGATEWAY_BUILD_VERSION" not in docker
    assert "docker image inspect llmapigateway:ci" in docker
    assert "org.opencontainers.image.version" in docker
    assert "git show -s --format=%cI HEAD" in docker
    assert "scripts/check_container_image.py llmapigateway:ci" in docker
    assert "scripts/check_container_image.py llmapigateway:ci-dirty" in docker
    assert "R3_3_DIRTY_CONTEXT_SENTINEL" in docker
    assert "llm_gateway_core/providers.json" in docker
    assert 'cmp "${clean_manifest}" "${dirty_manifest}"' in docker
    assert "docker/install_cloakbrowser_asset.py" in docker
    assert "--arch arm64" in docker
    assert "llm_gateway_core/diagnostics/sentinel.json" in docker
    assert "llm_gateway_core/cache/sentinel.bin" in docker
    assert "llm_gateway_core/credentials.json" in docker
    assert "static/private.key" in docker
    for adversarial_path in (
        "static/dev-diagnostic.txt",
        "llm_gateway_core/debug_dump.txt",
        "llm_gateway_core/id_rsa",
        "llm_gateway_core/service-account.json",
        "static/playwright-report/index.html",
        "llm_gateway_core/.arbitrary-hidden",
        "static/.arbitrary-hidden",
        "llm_gateway_core/rogue.py",
        "static/rogue.html",
        "docker/id_rsa",
        "docker/nested/service-account.json",
        "docker/rogue.sh",
        "scripts/nested/id_rsa",
        "scripts/rogue.py",
        "examples/arbitrary-child.md",
    ):
        assert adversarial_path in docker
    assert "rm -rf" in docker
    assert "--network none" in docker
    assert "import gpt_researcher, main" in docker
    assert "page.evaluate('1 + 1') == 2" in docker
    assert "document.title = 'offline'" in docker


def test_docker_job_runs_a_bounded_hermetic_health_smoke():
    docker = _job_text("docker")

    assert "trap cleanup EXIT" in docker
    assert 'docker rm --force "${container_name}"' in docker
    assert 'docker rm --force "${missing_config_name}"' in docker
    assert 'sudo rm -rf "${smoke_root}"' in docker
    assert "sudo chown -R 10001:10001" in docker
    assert '"${smoke_root}/missing-config"' in docker
    assert 'sudo chown -R 10001:10001 "${smoke_root}"' not in docker
    assert '"baseUrl": "http://127.0.0.1:1/v1"' in docker
    assert 'models_fallback_rules.json' in docker
    assert "container-config-preflight: reason=mandatory-config-missing" in docker
    assert '-mindepth 1 -maxdepth 1 -printf \'%f\\n\'' in docker
    assert "--publish 127.0.0.1::9000" in docker
    assert "deadline=$((SECONDS + 60))" in docker
    assert docker.count("--connect-timeout 2 --max-time 5") >= 2
    assert '"${health_url}/health"' in docker
    assert "curl --fail --silent --show-error --head" in docker
    assert 'payload["status"] == "ok"' in docker
    assert '"10001:10001"' in docker
    assert "id -u" in docker and "id -g" in docker
    assert docker.count("--read-only") >= 2
    assert docker.count("--tmpfs /app/logs:rw,mode=0770,uid=10001,gid=10001") >= 2
    assert "--tmpfs /app/outputs/images:rw,mode=0770,uid=10001,gid=10001" in docker
    assert "--tmpfs /app/outputs:rw,mode=0770,uid=10001,gid=10001" in docker
    assert "--tmpfs /tmp:rw,mode=1777" in docker
    assert "/app/.ci-write-denied" in docker
    assert "/opt/venv/.ci-write-denied" in docker
    assert "/opt/cloakbrowser/.ci-write-denied" in docker
    assert "/app/db/.ci-write-allowed" in docker
    assert 'os.replace(temporary, destination)' in docker
    assert '"${smoke_root}/config/models_operation_rules.json"' in docker
    assert "/healthz" not in docker


def test_docker_job_verifies_generated_image_volume_lifecycle_and_recovery():
    docker = _job_text("docker")

    assert "Verify persistent generated-image volume lifecycle" in _workflow_text()
    assert 'project_a="llmgateway-r34-a"' in docker
    assert 'project_b="llmgateway-r34-b"' in docker
    assert 'volume_a="${project_a}_gateway_outputs"' in docker
    assert "compose_a up -d --no-build --force-recreate llm-gateway" in docker
    assert "compose_a down" in docker
    assert "Authorization: Bearer ci-smoke-key" in docker
    assert 'sentinel_mtime_ns="$(python3 -c \'import time; print(time.time_ns())\')"' in docker
    assert 'docker exec --interactive "${container}" python - "${sentinel_mtime_ns}" <<\'PY\'' in docker
    assert 'docker exec --interactive "${gateway_a}" python - "${sentinel_mtime_ns}" <<\'PY\'' in docker
    assert "GeneratedImageStorage(Path(\"/app/outputs/images\"))" in docker
    assert "image_deadbeef_0.png" in docker
    assert "st_mtime_ns == int(sys.argv[1])" in docker
    assert "os.utime(published.path, ns=(expected_mtime_ns,) * 2)" in docker
    assert "compose_b run --rm --no-deps outputs-init" in docker
    assert "project B output volume survived down -v" in docker
    assert "image_storage_cli backup" in docker
    assert "image_storage_cli restore" in docker
    assert "tampered generated-image backup unexpectedly restored" in docker
    assert "incomplete restore marker did not block startup probe" in docker
    assert docker.count("compose_a down -v") >= 2
    assert docker.count("compose_b down -v") >= 2
