import ast
import io
import socket
import subprocess
from pathlib import Path

import pytest

from tests.ui_server_helpers import (
    get_free_port,
    isolated_gateway_process,
    stop_gateway_process,
    wait_for_gateway,
)


_PROCESS_APIS = {
    "subprocess": frozenset(
        {"Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
    ),
    "asyncio": frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
}


def _is_popen_call(call: ast.Call) -> bool:
    return (isinstance(call.func, ast.Name) and call.func.id == "Popen") or (
        isinstance(call.func, ast.Attribute) and call.func.attr == "Popen"
    )


def _command_arguments(call: ast.Call, api: tuple[str, str]) -> tuple[ast.AST, ...]:
    module, function = api
    if module == "asyncio" and function == "create_subprocess_exec":
        return tuple(call.args)

    keyword_name = (
        "cmd"
        if function in {"getoutput", "getstatusoutput", "create_subprocess_shell"}
        else "args"
    )
    if call.args:
        return (call.args[0],)
    return tuple(keyword.value for keyword in call.keywords if keyword.arg == keyword_name)


def _mentions_gateway_entrypoint(call: ast.Call, api: tuple[str, str]) -> bool:
    strings = [
        node.value
        for argument in _command_arguments(call, api)
        for node in ast.walk(argument)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    return (
        any("main.py" in value or "main:app" in value or "-m main" in value for value in strings)
        or any(left == "-m" and right == "main" for left, right in zip(strings, strings[1:]))
    )


def _assignment_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        return node.value
    return None


def _assignment_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
        targets = [node.target]
    else:
        return ()
    return tuple(
        target.id
        for assignment_target in targets
        for target in ast.walk(assignment_target)
        if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store)
    )


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    aliases = {name: name for name in _PROCESS_APIS}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for imported in node.names:
            if imported.name in _PROCESS_APIS:
                aliases[imported.asname or imported.name] = imported.name
    return aliases


def _canonical_api_reference(
    node: ast.AST,
    module_aliases: dict[str, str],
    function_aliases: dict[str, tuple[str, str]],
) -> tuple[str, str] | None:
    if isinstance(node, ast.Name):
        return function_aliases.get(node.id)
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return None
    module = module_aliases.get(node.value.id)
    if module is None or node.attr not in _PROCESS_APIS[module]:
        return None
    return module, node.attr


def _function_aliases(
    tree: ast.AST,
    module_aliases: dict[str, str],
) -> dict[str, tuple[str, str]]:
    aliases: dict[str, tuple[str, str]] = {}
    nodes = tuple(ast.walk(tree))
    for node in nodes:
        if not isinstance(node, ast.ImportFrom) or node.module not in _PROCESS_APIS:
            continue
        for imported in node.names:
            if imported.name in _PROCESS_APIS[node.module]:
                aliases[imported.asname or imported.name] = (node.module, imported.name)

    changed = True
    while changed:
        changed = False
        for node in nodes:
            value = _assignment_value(node)
            if value is None:
                continue
            api = _canonical_api_reference(value, module_aliases, aliases)
            if api is None:
                continue
            for name in _assignment_names(node):
                if name not in aliases:
                    aliases[name] = api
                    changed = True
    return aliases


def _references_popen(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Attribute) and child.attr == "Popen" for child in ast.walk(node))


def _is_getattr_popen(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "getattr"
        and len(call.args) >= 2
        and isinstance(call.args[1], ast.Constant)
        and call.args[1].value == "Popen"
    )


def _test_python_paths(repo_root: Path) -> tuple[Path, ...]:
    tests_dir = repo_root / "tests"
    paths = set(tests_dir.rglob("*.py"))
    for conftest_path in (repo_root / "conftest.py", tests_dir / "conftest.py"):
        if conftest_path.is_file():
            paths.add(conftest_path)
    return tuple(sorted(paths))


def _forbidden_process_calls(repo_root: Path) -> tuple[str, ...]:
    allowed_process_owners = {
        repo_root / "tests" / "deep_research_process_fixture.py",
        repo_root / "tests" / "ui_server_helpers.py",
    }
    violations: set[str] = set()
    for path in _test_python_paths(repo_root):
        if path in allowed_process_owners:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_aliases = _module_aliases(tree)
        function_aliases = _function_aliases(tree, module_aliases)
        for node in ast.walk(tree):
            forbidden = (
                isinstance(node, ast.ImportFrom)
                and node.module == "subprocess"
                and any(imported.name == "Popen" for imported in node.names)
            )
            value = _assignment_value(node)
            forbidden = forbidden or (value is not None and _references_popen(value))
            if isinstance(node, ast.Call):
                api = _canonical_api_reference(node.func, module_aliases, function_aliases)
                forbidden = forbidden or _is_popen_call(node) or _is_getattr_popen(node)
                forbidden = forbidden or (
                    api is not None and _mentions_gateway_entrypoint(node, api)
                )
            if forbidden:
                violations.add(f"{path.relative_to(repo_root)}:{node.lineno}")
    return tuple(sorted(violations))


class _FakeProcess:
    def __init__(self, *, returncode: int | None, communicate_timeouts: int = 0) -> None:
        self.returncode = returncode
        self.communicate_timeouts = communicate_timeouts
        self.stdin = io.StringIO("input")
        self.stdout = io.StringIO("stdout")
        self.stderr = io.StringIO("stderr")
        self.events: list[str] = []
        self.reaped = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.events.append("terminate")

    def kill(self) -> None:
        self.events.append("kill")

    def wait(self, timeout: float | None = None) -> int:
        self.events.append("wait")
        if self.returncode is None:
            self.returncode = -9 if "kill" in self.events else -15
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.events.append("communicate" if timeout is not None else "communicate-unbounded")
        if timeout is not None and self.communicate_timeouts:
            self.communicate_timeouts -= 1
            raise subprocess.TimeoutExpired("gateway", timeout)
        if self.returncode is None:
            self.returncode = -9 if "kill" in self.events else -15
        self.reaped = True
        return self.stdout.getvalue(), self.stderr.getvalue()


def test_isolated_gateway_process_copies_env_and_uses_canonical_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    proc = _FakeProcess(returncode=None)
    captured: dict[str, object] = {}
    stopped: list[object] = []

    def _fake_popen(args: list[str], **kwargs: object) -> _FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return proc

    def _fake_stop(candidate: object) -> str:
        stopped.append(candidate)
        return ""

    monkeypatch.setattr("tests.ui_server_helpers.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("tests.ui_server_helpers.stop_gateway_process", _fake_stop)
    inherited_env = {"GATEWAY_DB_DIR": "/checkout/db", "KEEP": "value"}
    non_normalized_temp_path = tmp_path / "nested" / ".."
    expected_db_path = (tmp_path / "db").resolve()
    expected_outputs_path = (tmp_path / "outputs").resolve()

    with isolated_gateway_process(env=inherited_env, temp_path=non_normalized_temp_path) as yielded:
        assert yielded is proc

    assert inherited_env == {"GATEWAY_DB_DIR": "/checkout/db", "KEEP": "value"}
    assert captured["args"] == ["./.venv/bin/python", "main.py"]
    captured_kwargs = captured["kwargs"]
    assert isinstance(captured_kwargs, dict)
    assert captured_kwargs["env"] is not inherited_env
    assert captured["kwargs"] == {
        "env": {
            "GATEWAY_DB_DIR": str(expected_db_path),
            "GATEWAY_OUTPUTS_DIR": str(expected_outputs_path),
            "KEEP": "value",
        },
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
    }
    assert (expected_outputs_path / "images").is_dir()
    assert stopped == [proc]


def test_isolated_gateway_process_cleans_up_when_body_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    proc = _FakeProcess(returncode=None)
    stopped: list[object] = []
    monkeypatch.setattr(
        "tests.ui_server_helpers.subprocess.Popen",
        lambda *args, **kwargs: proc,
    )
    monkeypatch.setattr(
        "tests.ui_server_helpers.stop_gateway_process",
        lambda candidate: stopped.append(candidate),
    )

    with pytest.raises(RuntimeError, match="fixture body failed"):
        with isolated_gateway_process(env={}, temp_path=tmp_path):
            raise RuntimeError("fixture body failed")

    assert stopped == [proc]


def test_isolated_gateway_process_rejects_checkout_db_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]

    def _unexpected_popen(*args: object, **kwargs: object) -> object:
        pytest.fail("Popen must not run when test storage resolves to the checkout DB")

    monkeypatch.setattr("tests.ui_server_helpers.subprocess.Popen", _unexpected_popen)

    with pytest.raises(ValueError, match="checkout DB directory"):
        with isolated_gateway_process(env={}, temp_path=repo_root):
            pytest.fail("invalid storage path must fail before entering the context")


def test_test_process_boundary_has_no_direct_launches() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert _forbidden_process_calls(repo_root) == ()


def test_test_process_boundary_recurses_and_rejects_direct_and_aliased_popen(
    tmp_path: Path,
) -> None:
    nested_tests = tmp_path / "tests" / "nested"
    nested_tests.mkdir(parents=True)
    (tmp_path / "conftest.py").write_text('Popen(["worker"])\n', encoding="utf-8")
    (nested_tests / "test_alias.py").write_text(
        'import subprocess as sp\nsp.Popen(["worker"])\n',
        encoding="utf-8",
    )

    assert _forbidden_process_calls(tmp_path) == (
        "conftest.py:1",
        "tests/nested/test_alias.py:2",
    )


@pytest.mark.parametrize(
    "source",
    (
        "from subprocess import Popen as spawn\n",
        "runner = sp.Popen\n",
        "runner: object = sp.Popen\n",
        "(runner := sp.Popen)\n",
        'runner = getattr(sp, "Popen")\n',
    ),
    ids=("import-alias", "assignment", "annotated-assignment", "walrus", "getattr"),
)
def test_test_process_boundary_rejects_popen_imports_and_references(
    tmp_path: Path,
    source: str,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_reference.py").write_text(source, encoding="utf-8")

    assert _forbidden_process_calls(tmp_path) == ("tests/test_reference.py:1",)


def test_test_process_boundary_tracks_assigned_subprocess_api_alias(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_alias.py").write_text(
        "import subprocess as sp\n"
        "runner = sp.run\n"
        'runner(["python", "main.py"])\n',
        encoding="utf-8",
    )

    assert _forbidden_process_calls(tmp_path) == ("tests/test_alias.py:3",)


@pytest.mark.parametrize(
    "source",
    (
        'subprocess.run(["python", Path("main.py")])\n',
        'subprocess.check_call(["uvicorn", "main:app"])\n',
        'subprocess.call(["python", "-m", "main"])\n',
        'subprocess.getoutput("python main.py")\n',
        'subprocess.getstatusoutput("python -m main")\n',
        'asyncio.create_subprocess_exec("python", "-m", "main")\n',
    ),
    ids=(
        "main-path",
        "uvicorn-app",
        "python-module",
        "getoutput",
        "getstatusoutput",
        "asyncio-exec",
    ),
)
def test_test_process_boundary_rejects_direct_gateway_entrypoints(
    tmp_path: Path,
    source: str,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_entrypoint.py").write_text(source, encoding="utf-8")

    assert _forbidden_process_calls(tmp_path) == ("tests/test_entrypoint.py:1",)


def test_test_process_boundary_ignores_gateway_text_outside_command_arguments(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_safe_echo.py").write_text(
        'subprocess.run(["echo", "ok"], env={"SCRIPT": "main.py"})\n'
        'subprocess.run(args=["echo", "ok"], cwd="main.py")\n'
        'subprocess.run(["echo", "ok"], input="main:app")\n',
        encoding="utf-8",
    )

    assert _forbidden_process_calls(tmp_path) == ()


def test_test_process_boundary_allows_non_launch_subprocess_helpers(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_command.py").write_text(
        'subprocess.list2cmdline(["python", "main.py"])\n',
        encoding="utf-8",
    )

    assert _forbidden_process_calls(tmp_path) == ()


def test_test_process_boundary_allows_popen_only_in_exact_process_owners(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "ui_server_helpers.py").write_text(
        'subprocess.Popen(["python", "main.py"])\n',
        encoding="utf-8",
    )
    (tests_dir / "deep_research_process_fixture.py").write_text(
        'subprocess.Popen(["python", "worker.py"])\n',
        encoding="utf-8",
    )
    (tests_dir / "process_helpers.py").write_text(
        'subprocess.Popen(["python", "worker.py"])\n',
        encoding="utf-8",
    )

    assert _forbidden_process_calls(tmp_path) == ("tests/process_helpers.py:1",)


def test_get_free_port_releases_temporary_socket() -> None:
    port = get_free_port()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_stop_gateway_process_reaps_already_exited_process_and_closes_pipes() -> None:
    proc = _FakeProcess(returncode=0)

    output = stop_gateway_process(proc, timeout_seconds=0.01)  # type: ignore[arg-type]

    assert output == "stdout\nstderr"
    assert proc.events == ["communicate"]
    assert proc.stdin.closed
    assert proc.stdout.closed
    assert proc.stderr.closed

    assert stop_gateway_process(proc, timeout_seconds=0.01) == ""  # type: ignore[arg-type]
    assert proc.events == ["communicate", "wait"]


def test_stop_gateway_process_kills_after_terminate_timeout() -> None:
    proc = _FakeProcess(returncode=None, communicate_timeouts=1)

    stop_gateway_process(proc, timeout_seconds=0.01)  # type: ignore[arg-type]

    assert proc.events == ["terminate", "communicate", "kill", "communicate-unbounded"]
    assert proc.returncode == -9
    assert proc.reaped
    assert proc.stdout.closed


def test_stop_gateway_process_reaps_when_two_bounded_attempts_would_timeout() -> None:
    proc = _FakeProcess(returncode=None, communicate_timeouts=2)

    stop_gateway_process(proc, timeout_seconds=0.01)  # type: ignore[arg-type]

    assert proc.events == ["terminate", "communicate", "kill", "communicate-unbounded"]
    assert proc.communicate_timeouts == 1
    assert proc.reaped
    assert proc.stdin.closed
    assert proc.stdout.closed
    assert proc.stderr.closed


def test_wait_for_gateway_cleans_up_process_that_exits_during_startup() -> None:
    proc = _FakeProcess(returncode=17)

    with pytest.raises(pytest.fail.Exception, match="stdout"):
        wait_for_gateway("http://127.0.0.1:1", proc, retries=1, delay_seconds=0)  # type: ignore[arg-type]

    assert proc.events == ["communicate"]
    assert proc.stdin.closed
    assert proc.stdout.closed
    assert proc.stderr.closed


def test_wait_for_gateway_cleans_up_process_after_health_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess(returncode=None)

    def _unavailable(*args: object, **kwargs: object) -> object:
        return type("Response", (), {"status_code": 503})()

    monkeypatch.setattr("tests.ui_server_helpers.requests.get", _unavailable)

    with pytest.raises(pytest.fail.Exception, match="failed to start"):
        wait_for_gateway("http://127.0.0.1:1", proc, retries=1, delay_seconds=0)  # type: ignore[arg-type]

    assert proc.events == ["terminate", "communicate"]
    assert proc.stdout.closed


def test_provider_mock_closes_socket_when_thread_start_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests import test_ui_regression

    closed_servers: list[object] = []
    real_server_close = test_ui_regression.HTTPServer.server_close

    def _track_server_close(server: object) -> None:
        closed_servers.append(server)
        real_server_close(server)  # type: ignore[arg-type]

    def _fail_start(thread: object) -> None:
        raise RuntimeError("thread start failed")

    def _unexpected_cleanup_call(*args: object, **kwargs: object) -> None:
        pytest.fail("shutdown/join must not run if Thread.start failed")

    monkeypatch.setattr(test_ui_regression.HTTPServer, "server_close", _track_server_close)
    monkeypatch.setattr(test_ui_regression.HTTPServer, "shutdown", _unexpected_cleanup_call)
    monkeypatch.setattr(test_ui_regression.threading.Thread, "start", _fail_start)
    monkeypatch.setattr(test_ui_regression.threading.Thread, "join", _unexpected_cleanup_call)

    fixture = test_ui_regression.provider_mock.__wrapped__()
    with pytest.raises(RuntimeError, match="thread start failed"):
        next(fixture)

    assert len(closed_servers) == 1
    assert closed_servers[0].socket.fileno() == -1  # type: ignore[attr-defined]
