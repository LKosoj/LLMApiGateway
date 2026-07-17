from __future__ import annotations

import importlib.abc
import inspect
import os
import subprocess
import tempfile
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import tests.main_lifespan_import as main_lifespan_import_module
from tests.main_lifespan_import import (
    MainImportIsolationController,
    MainImportIsolationError,
    _make_cleanup_handle,
    get_main_import_isolation,
)
from tests.main_lifespan_boundary import find_main_lifespan_boundary_violations


@asynccontextmanager
async def _fake_lifespan(_app: object) -> AsyncIterator[None]:
    yield


def _populate_fake_main(module: ModuleType, project_root: Path) -> None:
    module.PROJECT_ROOT = project_root
    module.OUTPUTS_IMAGES_DIR = project_root / "outputs" / "images"
    module.lifespan = _fake_lifespan
    module.app = SimpleNamespace(
        router=SimpleNamespace(lifespan_context=_fake_lifespan)
    )


class _FakeLoader(importlib.abc.Loader):
    def __init__(
        self,
        project_root: Path,
        *,
        failure: BaseException | None = None,
        reenter: bool = False,
        create_failure: BaseException | None = None,
    ) -> None:
        self.project_root = project_root
        self.failure = failure
        self.reenter = reenter
        self.create_failure = create_failure
        self.create_calls = 0
        self.exec_calls = 0
        self.metadata_calls = 0

    def create_module(self, _spec: ModuleSpec) -> ModuleType | None:
        self.create_calls += 1
        if self.create_failure is not None:
            failure = self.create_failure
            self.create_failure = None
            raise failure
        return None

    def exec_module(self, module: ModuleType) -> None:
        self.exec_calls += 1
        if self.failure is not None:
            raise self.failure
        if self.reenter:
            module.__loader__.exec_module(module)
        _populate_fake_main(module, self.project_root)

    def get_code(self, _fullname: str) -> object:
        self.metadata_calls += 1
        return object()


class _FakeFinder(importlib.abc.MetaPathFinder):
    def __init__(self, spec: ModuleSpec, *, fresh_specs: bool = False) -> None:
        self.spec = spec
        self.loader = spec.loader
        self.fresh_specs = fresh_specs
        self.calls: list[tuple[str, object]] = []
        self.returned_specs: dict[int, ModuleSpec] = {}
        self.block_on_call: int | None = None
        self.block_entered = threading.Event()
        self.block_release = threading.Event()

    def find_spec(self, fullname, path=None, target=None):
        self.calls.append((fullname, target))
        call_number = len(self.calls)
        if self.block_on_call == call_number:
            self.block_entered.set()
            if not self.block_release.wait(timeout=5):
                raise TimeoutError("test finder resolution was not released")
        if fullname != "main":
            return None
        if not self.fresh_specs:
            return self.spec
        spec = ModuleSpec("main", self.loader, origin=self.spec.origin)
        self.returned_specs[call_number] = spec
        return spec


def _controller_fixture(
    tmp_path: Path,
    *,
    origin: Path | None = None,
    failure: BaseException | None = None,
    reenter: bool = False,
    create_failure: BaseException | None = None,
    fresh_specs: bool = False,
) -> tuple[
    MainImportIsolationController,
    dict[str, ModuleType],
    list[object],
    _FakeFinder,
    _FakeLoader,
    Callable[[], None],
]:
    project_root = tmp_path / "project"
    project_root.mkdir()
    main_path = project_root / "main.py"
    main_path.write_text("# fake main\n", encoding="utf-8")
    loader = _FakeLoader(
        project_root,
        failure=failure,
        reenter=reenter,
        create_failure=create_failure,
    )
    spec = ModuleSpec("main", loader, origin=str(origin or main_path))
    delegate = _FakeFinder(spec, fresh_specs=fresh_specs)
    registry: dict[str, ModuleType] = {}
    meta_path: list[object] = [delegate]
    cleanup_token = object()
    controller = MainImportIsolationController(
        project_root,
        cleanup_token=cleanup_token,
        module_registry=registry,
        meta_path=meta_path,
    )
    cleanup = _make_cleanup_handle(controller, cleanup_token)
    return controller, registry, meta_path, delegate, loader, cleanup


def _execute_fake_import(
    controller: MainImportIsolationController,
    registry: dict[str, ModuleType],
) -> tuple[ModuleType, object, object]:
    finder = controller.finder
    assert finder is not None
    spec = finder.find_spec("main")
    assert spec is not None
    assert spec.loader is not None
    module = ModuleType("main")
    module.__spec__ = spec
    module.__loader__ = spec.loader
    registry["main"] = module
    spec.loader.exec_module(module)
    return module, module.lifespan, module.app.router.lifespan_context


def test_controller_install_is_lazy_and_ignores_other_modules(tmp_path: Path) -> None:
    controller, registry, meta_path, delegate, loader, cleanup = _controller_fixture(
        tmp_path
    )

    controller.install()

    assert registry == {}
    assert loader.exec_calls == 0
    assert meta_path[0] is controller.finder
    assert controller.finder is not None
    assert controller.finder.find_spec("json") is None
    assert delegate.calls == []
    cleanup()
    assert meta_path == [delegate]


def test_resolution_only_replaces_loader_and_invalidates_saved_shim(
    tmp_path: Path,
) -> None:
    controller, _registry, _meta_path, delegate, loader, cleanup = _controller_fixture(
        tmp_path
    )
    controller.install()
    finder = controller.finder
    assert finder is not None

    first_spec = finder.find_spec("main")
    assert first_spec is not None and first_spec.loader is not None
    stale_loader = first_spec.loader
    second_spec = finder.find_spec("main")
    assert second_spec is first_spec
    assert second_spec.loader is not stale_loader
    assert second_spec.loader.resolution_token > stale_loader.resolution_token
    assert delegate.calls == [("main", None), ("main", None)]

    with pytest.raises(MainImportIsolationError, match="stale"):
        stale_loader.create_module(first_spec)
    with pytest.raises(MainImportIsolationError, match="stale"):
        stale_loader.get_code("main")
    with pytest.raises(MainImportIsolationError, match="stale"):
        _ = stale_loader.project_root
    assert loader.create_calls == 0
    assert loader.metadata_calls == 0
    cleanup()


def test_fresh_resolution_keeps_saved_spec_loader_stale_until_cleanup(
    tmp_path: Path,
) -> None:
    controller, _registry, _meta_path, delegate, loader, cleanup = _controller_fixture(
        tmp_path,
        fresh_specs=True,
    )
    controller.install()
    finder = controller.finder
    assert finder is not None
    first_spec = finder.find_spec("main")
    assert first_spec is not None and first_spec.loader is not None
    first_wrapper = first_spec.loader

    second_spec = finder.find_spec("main")
    assert second_spec is not None and second_spec is not first_spec
    assert second_spec.loader is not first_wrapper
    with pytest.raises(MainImportIsolationError, match="stale"):
        first_spec.loader.exec_module(ModuleType("main"))
    assert loader.exec_calls == 0

    cleanup()
    assert first_spec.loader is loader
    assert second_spec.loader is loader
    assert delegate.calls == [("main", None), ("main", None)]


def test_saved_spec_loader_stays_guarded_during_next_resolution_window(
    tmp_path: Path,
) -> None:
    controller, _registry, _meta_path, delegate, loader, cleanup = _controller_fixture(
        tmp_path,
        fresh_specs=True,
    )
    controller.install()
    finder = controller.finder
    assert finder is not None
    first_spec = finder.find_spec("main")
    assert first_spec is not None and first_spec.loader is not None
    delegate.block_on_call = 2
    outcome: dict[str, object] = {}

    def resolve_again() -> None:
        try:
            outcome["spec"] = finder.find_spec("main")
        except BaseException as exc:
            outcome["error"] = exc

    resolver = threading.Thread(target=resolve_again)
    resolver.start()
    assert delegate.block_entered.wait(timeout=5)
    try:
        with pytest.raises(MainImportIsolationError, match="stale"):
            first_spec.loader.exec_module(ModuleType("main"))
        assert loader.exec_calls == 0
    finally:
        delegate.block_release.set()
        resolver.join(timeout=5)

    assert not resolver.is_alive()
    assert "error" not in outcome
    second_spec = outcome["spec"]
    assert isinstance(second_spec, ModuleSpec)
    cleanup()
    assert first_spec.loader is loader
    assert second_spec.loader is loader


def test_stale_resolution_cannot_replace_create_owner_or_mutate_its_spec(
    tmp_path: Path,
) -> None:
    controller, _registry, _meta_path, delegate, loader, cleanup = _controller_fixture(
        tmp_path,
        fresh_specs=True,
    )
    controller.install()
    finder = controller.finder
    assert finder is not None
    delegate.block_on_call = 1
    outcome: dict[str, object] = {}

    def resolve_slow() -> None:
        try:
            outcome["spec"] = finder.find_spec("main")
        except BaseException as exc:
            outcome["error"] = exc

    slow = threading.Thread(target=resolve_slow)
    slow.start()
    assert delegate.block_entered.wait(timeout=5)
    current_spec = finder.find_spec("main")
    assert current_spec is not None and current_spec.loader is not None
    current_loader = current_spec.loader
    controller.begin_create(current_spec, current_loader)
    delegate.block_release.set()
    slow.join(timeout=5)

    assert not slow.is_alive()
    assert isinstance(outcome.get("error"), MainImportIsolationError)
    assert "stale resolution" in str(outcome["error"])
    stale_spec = delegate.returned_specs[1]
    assert stale_spec.loader is loader
    assert current_spec.loader is current_loader
    controller.abort_create(current_spec, current_loader)
    cleanup()
    assert current_spec.loader is loader
    assert stale_spec.loader is loader


def test_stale_resolution_cannot_replace_exec_owner_or_mutate_its_spec(
    tmp_path: Path,
) -> None:
    controller, registry, _meta_path, delegate, loader, cleanup = _controller_fixture(
        tmp_path,
        fresh_specs=True,
    )
    controller.install()
    finder = controller.finder
    assert finder is not None
    delegate.block_on_call = 1
    outcome: dict[str, object] = {}

    def resolve_slow() -> None:
        try:
            outcome["spec"] = finder.find_spec("main")
        except BaseException as exc:
            outcome["error"] = exc

    slow = threading.Thread(target=resolve_slow)
    slow.start()
    assert delegate.block_entered.wait(timeout=5)
    current_spec = finder.find_spec("main")
    assert current_spec is not None and current_spec.loader is not None
    current_loader = current_spec.loader
    module = ModuleType("main")
    module.__spec__ = current_spec
    module.__loader__ = current_loader
    registry["main"] = module
    controller.begin_exec(module, current_loader)
    delegate.block_release.set()
    slow.join(timeout=5)

    assert not slow.is_alive()
    assert isinstance(outcome.get("error"), MainImportIsolationError)
    assert "stale resolution" in str(outcome["error"])
    stale_spec = delegate.returned_specs[1]
    assert stale_spec.loader is loader
    assert current_spec.loader is current_loader
    controller.abort_exec(module, current_loader)
    registry.pop("main")
    cleanup()
    assert current_spec.loader is loader
    assert stale_spec.loader is loader


def test_create_module_failure_allows_fresh_resolution_but_invalidates_old_shim(
    tmp_path: Path,
) -> None:
    failure = RuntimeError("create failed")
    controller, _registry, _meta_path, delegate, loader, cleanup = _controller_fixture(
        tmp_path,
        create_failure=failure,
    )
    controller.install()
    finder = controller.finder
    assert finder is not None
    first_spec = finder.find_spec("main")
    assert first_spec is not None and first_spec.loader is not None
    stale_loader = first_spec.loader

    with pytest.raises(RuntimeError) as raised:
        stale_loader.create_module(first_spec)
    assert raised.value is failure
    assert controller.assert_integrity() is None
    second_spec = finder.find_spec("main")
    assert second_spec is first_spec
    assert second_spec.loader is not stale_loader
    assert second_spec.loader.create_module(second_spec) is None
    assert loader.create_calls == 2
    assert delegate.calls == [("main", None), ("main", None)]

    with pytest.raises(MainImportIsolationError, match="stale"):
        stale_loader.exec_module(ModuleType("main"))
    assert loader.exec_calls == 0
    cleanup()


def test_create_failure_keeps_saved_fresh_spec_guarded_until_retry_cleanup(
    tmp_path: Path,
) -> None:
    failure = RuntimeError("create failed")
    controller, _registry, _meta_path, _delegate, loader, cleanup = (
        _controller_fixture(
            tmp_path,
            create_failure=failure,
            fresh_specs=True,
        )
    )
    controller.install()
    finder = controller.finder
    assert finder is not None
    first_spec = finder.find_spec("main")
    assert first_spec is not None and first_spec.loader is not None

    with pytest.raises(RuntimeError) as raised:
        first_spec.loader.create_module(first_spec)
    assert raised.value is failure
    with pytest.raises(MainImportIsolationError, match="stale"):
        first_spec.loader.exec_module(ModuleType("main"))
    assert loader.exec_calls == 0

    second_spec = finder.find_spec("main")
    assert second_spec is not None and second_spec is not first_spec
    cleanup()
    assert first_spec.loader is loader
    assert second_spec.loader is loader


def test_first_import_wraps_both_lifespan_entries_and_cleanup_restores_them(
    tmp_path: Path,
) -> None:
    controller, registry, meta_path, delegate, loader, cleanup = _controller_fixture(
        tmp_path
    )
    controller.install()
    module, wrapped_public, wrapped_router = _execute_fake_import(controller, registry)
    owner = controller.assert_integrity()
    assert owner is not None
    assert owner.owns(wrapped_public)
    assert owner.owns(wrapped_router)
    assert loader.exec_calls == 1
    assert delegate.calls == [("main", None)]

    cleanup()

    assert module.lifespan is _fake_lifespan
    assert module.app.router.lifespan_context is _fake_lifespan
    assert module.__spec__.loader is loader
    assert module.__loader__ is loader
    assert meta_path == [delegate]
    cleanup()
    assert module.__spec__.loader is loader
    assert module.__loader__ is loader


@pytest.mark.parametrize("loader_attribute", ("spec", "module"))
def test_repeated_direct_exec_is_rejected_before_delegate_or_module_mutation(
    tmp_path: Path,
    loader_attribute: str,
) -> None:
    controller, registry, _meta_path, _delegate, loader, cleanup = _controller_fixture(
        tmp_path
    )
    controller.install()
    module, wrapped_public, wrapped_router = _execute_fake_import(controller, registry)
    isolating_loader = (
        module.__spec__.loader
        if loader_attribute == "spec"
        else module.__loader__
    )
    assert isolating_loader is not None

    with pytest.raises(MainImportIsolationError, match="already executed"):
        isolating_loader.exec_module(module)

    assert loader.exec_calls == 1
    assert module.lifespan is wrapped_public
    assert module.app.router.lifespan_context is wrapped_router
    assert module.__spec__.loader is isolating_loader
    assert module.__loader__ is isolating_loader
    cleanup()


def test_reentrant_direct_exec_is_rejected_before_second_delegate_call(
    tmp_path: Path,
) -> None:
    controller, registry, _meta_path, _delegate, loader, cleanup = _controller_fixture(
        tmp_path,
        reenter=True,
    )
    controller.install()
    finder = controller.finder
    assert finder is not None
    spec = finder.find_spec("main")
    assert spec is not None and spec.loader is not None
    module = ModuleType("main")
    module.__spec__ = spec
    module.__loader__ = spec.loader
    registry["main"] = module

    with pytest.raises(MainImportIsolationError, match="already in progress"):
        spec.loader.exec_module(module)

    assert loader.exec_calls == 1
    assert not hasattr(module, "lifespan")
    assert controller.module is None
    assert controller.owner is None
    registry.pop("main")
    with pytest.raises(MainImportIsolationError, match="terminal failed"):
        controller.assert_integrity()
    assert controller.finder is not None
    with pytest.raises(MainImportIsolationError, match="terminal failed"):
        controller.finder.find_spec("main")
    cleanup()
    assert module.__spec__.loader is loader
    assert module.__loader__ is loader


def test_failed_module_exec_preserves_exception_and_publishes_no_owner(
    tmp_path: Path,
) -> None:
    failure = RuntimeError("import failed")
    controller, registry, _meta_path, _delegate, loader, cleanup = _controller_fixture(
        tmp_path,
        failure=failure,
    )
    controller.install()
    finder = controller.finder
    assert finder is not None
    spec = finder.find_spec("main")
    assert spec is not None and spec.loader is not None
    module = ModuleType("main")
    module.__spec__ = spec
    module.__loader__ = spec.loader
    registry["main"] = module

    with pytest.raises(RuntimeError) as raised:
        spec.loader.exec_module(module)
    assert raised.value is failure
    assert loader.exec_calls == 1
    assert controller.module is None
    assert controller.owner is None
    registry.pop("main")
    with pytest.raises(MainImportIsolationError, match="terminal failed"):
        controller.assert_integrity()
    assert controller.finder is not None
    delegate_calls = len(_delegate.calls)
    with pytest.raises(MainImportIsolationError, match="terminal failed"):
        controller.finder.find_spec("main")
    assert len(_delegate.calls) == delegate_calls
    cleanup()
    assert module.__spec__.loader is loader
    assert module.__loader__ is loader


def test_wrong_main_origin_fails_before_delegate_exec(tmp_path: Path) -> None:
    wrong_origin = tmp_path / "other" / "main.py"
    controller, _registry, _meta_path, _delegate, loader, cleanup = _controller_fixture(
        tmp_path,
        origin=wrong_origin,
    )
    controller.install()

    with pytest.raises(MainImportIsolationError, match="origin"):
        assert controller.finder is not None
        controller.finder.find_spec("main")
    assert loader.exec_calls == 0
    cleanup()


def test_preloaded_main_fails_closed(tmp_path: Path) -> None:
    controller, registry, meta_path, delegate, _loader, _cleanup = _controller_fixture(
        tmp_path
    )
    registry["main"] = ModuleType("main")

    with pytest.raises(MainImportIsolationError, match="before"):
        controller.install()
    assert meta_path == [delegate]


def test_reload_and_distinct_reimport_fail_before_exec(tmp_path: Path) -> None:
    controller, registry, _meta_path, delegate, loader, cleanup = _controller_fixture(
        tmp_path
    )
    controller.install()
    module, _wrapped_public, _wrapped_router = _execute_fake_import(controller, registry)
    finder = controller.finder
    assert finder is not None

    with pytest.raises(MainImportIsolationError, match="Reloading"):
        finder.find_spec("main", target=module)
    registry.pop("main")
    with pytest.raises(MainImportIsolationError, match="distinct"):
        finder.find_spec("main")
    assert loader.exec_calls == 1
    assert delegate.calls == [("main", None)]

    registry["main"] = module
    cleanup()


def test_integrity_rebind_fails_and_cleanup_restores_exact_callables(
    tmp_path: Path,
) -> None:
    controller, registry, _meta_path, _delegate, _loader, cleanup = _controller_fixture(
        tmp_path
    )
    controller.install()
    module, _wrapped_public, _wrapped_router = _execute_fake_import(controller, registry)
    module.lifespan = lambda _app: None

    with pytest.raises(MainImportIsolationError, match="lifespan was rebound"):
        controller.assert_integrity()
    with pytest.raises(RuntimeError, match="was rebound"):
        cleanup()
    assert module.lifespan is _fake_lifespan
    assert module.app.router.lifespan_context is _fake_lifespan


def test_controller_has_no_usable_indirect_zero_argument_cleanup(
    tmp_path: Path,
) -> None:
    controller, registry, meta_path, delegate, _loader, cleanup = _controller_fixture(
        tmp_path
    )
    controller.install()
    module, wrapped_public, wrapped_router = _execute_fake_import(controller, registry)

    def stop(candidate: MainImportIsolationController) -> None:
        candidate._close()

    with pytest.raises(TypeError, match="required positional argument"):
        stop(controller)
    owner = controller.assert_integrity()
    assert owner is not None
    assert owner.owns(module.lifespan)
    assert module.lifespan is wrapped_public
    assert module.app.router.lifespan_context is wrapped_router
    assert meta_path[0] is controller.finder
    cleanup()
    assert meta_path == [delegate]


def test_in_progress_cleanup_refusal_preserves_active_controller_for_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    controller, registry, meta_path, _delegate, _loader, cleanup = _controller_fixture(
        tmp_path
    )
    controller.install()
    finder = controller.finder
    assert finder is not None
    spec = finder.find_spec("main")
    assert spec is not None and spec.loader is not None
    module = ModuleType("main")
    module.__spec__ = spec
    module.__loader__ = spec.loader
    registry["main"] = module
    controller.begin_exec(module, spec.loader)

    with monkeypatch.context() as isolated_global:
        isolated_global.setattr(
            main_lifespan_import_module,
            "_ACTIVE_CONTROLLER",
            controller,
        )
        with pytest.raises(MainImportIsolationError, match="during module execution"):
            cleanup()
        assert main_lifespan_import_module._ACTIVE_CONTROLLER is controller
        assert meta_path[0] is finder

        controller.abort_exec(module, spec.loader)
        registry.pop("main")
        cleanup()
        assert main_lifespan_import_module._ACTIVE_CONTROLLER is None


def test_first_installer_alone_receives_cleanup_capability() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    code = (
        "import tempfile\n"
        "from pathlib import Path\n"
        "from tests.main_lifespan_import import (\n"
        "    MainImportIsolationError, get_main_import_isolation,\n"
        "    install_main_import_isolation,\n"
        ")\n"
        "with tempfile.TemporaryDirectory() as raw_root:\n"
        "    root = Path(raw_root)\n"
        "    (root / 'main.py').write_text('# fake main\\n', encoding='utf-8')\n"
        "    controller, cleanup = install_main_import_isolation(root)\n"
        "    assert get_main_import_isolation() is controller\n"
        "    try:\n"
        "        install_main_import_isolation(root)\n"
        "    except MainImportIsolationError as exc:\n"
        "        assert 'already installed' in str(exc)\n"
        "    else:\n"
        "        raise AssertionError('second installer received cleanup access')\n"
        "    cleanup()\n"
        "    cleanup()\n"
        "    try:\n"
        "        get_main_import_isolation()\n"
        "    except MainImportIsolationError:\n"
        "        pass\n"
        "    else:\n"
        "        raise AssertionError('closed controller remained globally active')\n"
    )
    result = subprocess.run(
        [str(repo_root / ".venv" / "bin" / "python"), "-c", code],
        cwd=repo_root,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_root_pytest_configure_is_one_shot_before_cleanup_registration() -> None:
    root_conftest = __import__("conftest")
    controller = get_main_import_isolation()
    owner_before = controller.assert_integrity()
    captured_cleanups: list[object] = []

    class FakeConfig:
        def add_cleanup(self, callback) -> None:
            captured_cleanups.append(callback)

    with pytest.raises(pytest.UsageError, match="already configured"):
        root_conftest.pytest_configure(FakeConfig())

    assert captured_cleanups == []
    assert get_main_import_isolation() is controller
    assert controller.assert_integrity() is owner_before


def test_root_pytest_configure_cleans_up_when_registration_fails() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    code = (
        "import sys\n"
        "import conftest as root_conftest\n"
        "from tests.main_lifespan_import import (\n"
        "    MainImportIsolationError, _MainIsolationFinder,\n"
        "    get_main_import_isolation,\n"
        ")\n"
        "failure = RuntimeError('cleanup registration failed')\n"
        "class FakeConfig:\n"
        "    def add_cleanup(self, callback):\n"
        "        raise failure\n"
        "try:\n"
        "    root_conftest.pytest_configure(FakeConfig())\n"
        "except RuntimeError as exc:\n"
        "    assert exc is failure\n"
        "else:\n"
        "    raise AssertionError('registration failure was swallowed')\n"
        "try:\n"
        "    get_main_import_isolation()\n"
        "except MainImportIsolationError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('failed configure leaked the active controller')\n"
        "assert not any(isinstance(item, _MainIsolationFinder) for item in sys.meta_path)\n"
    )
    result = subprocess.run(
        [str(repo_root / ".venv" / "bin" / "python"), "-c", code],
        cwd=repo_root,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_active_session_owner_wraps_late_import_and_from_import() -> None:
    import main
    from main import lifespan

    controller = get_main_import_isolation()
    owner = controller.assert_integrity()
    assert owner is not None
    assert owner.owns(main.lifespan)
    assert owner.owns(lifespan)
    assert owner.owns(main.app.router.lifespan_context)
    assert "async def lifespan" in inspect.getsource(main)
    loader = main.__spec__.loader
    assert loader is not None
    assert Path(loader.get_filename("main")).resolve() == Path(main.__file__).resolve()


def test_active_controller_rejects_indirect_close_without_cleanup_capability() -> None:
    """Private token introspection and ``atexit._run_exitfuncs`` are out of scope."""

    controller = get_main_import_isolation()
    owner = controller.assert_integrity()
    assert owner is not None

    def stop(candidate: MainImportIsolationController) -> None:
        candidate._close()

    def stop_with_forged_token(candidate: MainImportIsolationController) -> None:
        candidate._close(object())

    with pytest.raises(TypeError, match="required positional argument"):
        stop(controller)
    with pytest.raises(MainImportIsolationError, match="cleanup capability"):
        stop_with_forged_token(controller)

    assert get_main_import_isolation() is controller
    assert controller.assert_integrity() is owner


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    (
        (
            "import sys\nremove = sys.modules.pop\nremove('main')\n",
            (3,),
        ),
        (
            "import sys\nreplace = sys.modules.__setitem__\n"
            "replace('main', fake)\n",
            (3,),
        ),
        (
            "import main\n"
            "main.__dict__.__ior__({'lifespan': fake})\n",
            (2,),
        ),
        (
            "import main\n"
            "dict.__ior__(main.__dict__, {'lifespan': fake})\n",
            (2,),
        ),
        (
            "import main\nfrom operator import ior\n"
            "ior(main.__dict__, {'lifespan': fake})\n",
            (3,),
        ),
        (
            "def safe(main):\n    main.lifespan = fake\n",
            (),
        ),
        (
            "import main\n"
            "gateway = main.app\n"
            "def safe():\n"
            "    gateway = local_object\n"
            "    gateway.router = fake\n",
            (),
        ),
        (
            "import main\nmain.__dict__.update(test_marker=True)\n",
            (),
        ),
        (
            "import main\nmain.__dict__ |= {'test_marker': True}\n",
            (),
        ),
        (
            "import main\nfrom unittest.mock import patch\n"
            "patch.dict(main.__dict__, values={'lifespan': fake})\n",
            (3,),
        ),
        (
            "import main\nfrom unittest.mock import patch\n"
            "patch.object(target=main, attribute='lifespan', new=fake)\n",
            (3,),
        ),
        (
            "import main\nfrom unittest.mock import patch\n"
            "patch.multiple(target=main.app.router, lifespan_context=fake)\n",
            (3,),
        ),
        (
            "def test_x(monkeypatch):\n"
            "    monkeypatch.setattr('main.lifespan', fake)\n",
            (2,),
        ),
        (
            "def test_x(monkeypatch):\n"
            "    monkeypatch.delattr('main.lifespan')\n",
            (2,),
        ),
        (
            "import main\nfrom unittest.mock import patch\n"
            "patch.dict(main.__dict__, values={'test_marker': True})\n",
            (),
        ),
        (
            "import main\n"
            "safe = lambda main: setattr(main, 'lifespan', fake)\n",
            (),
        ),
        (
            "import main\n"
            "unsafe = lambda: setattr(main, 'lifespan', fake)\n",
            (2,),
        ),
        (
            "import main\n"
            "[setattr(main, 'lifespan', fake) for main in values]\n",
            (),
        ),
        (
            "import main\n"
            "[setattr(main, 'lifespan', fake) for item in values]\n",
            (2,),
        ),
        (
            "import main\n"
            "def safe(values):\n"
            "    for main in values:\n"
            "        main.lifespan = fake\n",
            (),
        ),
        (
            "import main\n"
            "def safe(manager):\n"
            "    with manager as main:\n"
            "        main.lifespan = fake\n",
            (),
        ),
        (
            "import main\n"
            "def safe():\n"
            "    try:\n"
            "        pass\n"
            "    except Exception as main:\n"
            "        main.lifespan = fake\n",
            (),
        ),
        (
            "import main\n"
            "def safe(value):\n"
            "    match value:\n"
            "        case {'main': main}:\n"
            "            main.lifespan = fake\n",
            (),
        ),
        (
            "import main\n"
            "router = main.app.router\n"
            "class Holder:\n"
            "    router = router\n"
            "    router.lifespan_context = fake\n",
            (5,),
        ),
        (
            "import main\n"
            "router = main.app.router\n"
            "local = safe_object\n"
            "class Holder:\n"
            "    router = local\n"
            "    router.lifespan_context = fake\n",
            (),
        ),
        (
            "import main\nimport importlib\nimportlib.reload(module=main)\n",
            (3,),
        ),
        (
            "import main\nfrom unittest.mock import patch\n"
            "patch.dict('main.__dict__', {'lifespan': fake})\n",
            (3,),
        ),
        (
            "from unittest.mock import patch\n"
            "patch.dict(in_dict='main.app.router.__dict__', "
            "values={'lifespan_context': fake})\n",
            (2,),
        ),
        (
            "from unittest.mock import patch\n"
            "patch.multiple('main.app.router', lifespan_context=fake)\n",
            (2,),
        ),
        (
            "from unittest.mock import patch\n"
            "patch.multiple(target='main', lifespan=fake)\n",
            (2,),
        ),
        (
            "import main\nfrom unittest.mock import patch\n"
            "patch.dict('main.__dict__', {'test_marker': True})\n",
            (),
        ),
        (
            "import main\ndef test_x(monkeypatch):\n"
            "    monkeypatch.setitem(dic=main.__dict__, name='lifespan', value=fake)\n",
            (3,),
        ),
        (
            "import main\ndef test_x(monkeypatch):\n"
            "    monkeypatch.delitem(dic=main.__dict__, name='lifespan')\n",
            (3,),
        ),
        (
            "import main\ndef test_x(monkeypatch):\n"
            "    monkeypatch.setitem(dic=main.__dict__, name='test_marker', value=True)\n",
            (),
        ),
        (
            "import runpy\nrunpy.run_module('main')\n",
            (2,),
        ),
        (
            "from runpy import run_module\nrun_module(mod_name='main')\n",
            (2,),
        ),
        (
            "import runpy\nrunpy.run_path('main.py')\n",
            (2,),
        ),
        (
            "from runpy import run_path\nrun_path(path_name='main.py')\n",
            (2,),
        ),
        (
            "from tests.main_lifespan_import import _close_main_import_isolation\n"
            "_close_main_import_isolation()\n",
            (2,),
        ),
        (
            "from tests.main_lifespan_import import _close_main_import_isolation\n"
            "close = _close_main_import_isolation\nclose()\n",
            (3,),
        ),
        (
            "import tests.main_lifespan_import as isolation\n"
            "isolation._close_main_import_isolation()\n",
            (2,),
        ),
        (
            "from tests.main_lifespan_import import get_main_import_isolation\n"
            "get_main_import_isolation()._close()\n",
            (2,),
        ),
        (
            "from tests.main_lifespan_import import get_main_import_isolation\n"
            "controller = get_main_import_isolation()\ncontroller._close()\n",
            (3,),
        ),
        (
            "from tests.main_lifespan_import import get_main_import_isolation\n"
            "def stop(controller):\n"
            "    controller._close()\n"
            "stop(get_main_import_isolation())\n",
            (),
        ),
        (
            "from tests.main_lifespan_import import MainImportIsolationController\n"
            "MainImportIsolationController._close(controller)\n",
            (2,),
        ),
        (
            "from tests.main_lifespan_import import _ACTIVE_CONTROLLER\n"
            "_ACTIVE_CONTROLLER._close()\n",
            (2,),
        ),
        (
            "import main\nfrom unittest import mock\n"
            "p = mock.patch\np.object(main, 'lifespan', fake)\n",
            (4,),
        ),
        (
            "import main\nfrom unittest import mock\n"
            "pd = mock.patch.dict\n"
            "pd(main.__dict__, {'lifespan': fake})\n",
            (4,),
        ),
        (
            "import main\ndef test_x(monkeypatch):\n"
            "    change = monkeypatch.setattr\n"
            "    change(main, 'lifespan', fake)\n",
            (4,),
        ),
        (
            "import main\ndef test_x(monkeypatch):\n"
            "    change = monkeypatch.delattr\n"
            "    change(main, 'lifespan')\n",
            (4,),
        ),
        (
            "import main\ndef test_x(monkeypatch):\n"
            "    change = monkeypatch.setitem\n"
            "    change(main.__dict__, 'lifespan', fake)\n",
            (4,),
        ),
        (
            "import main\ndef test_x(monkeypatch):\n"
            "    change = monkeypatch.delitem\n"
            "    change(main.__dict__, 'lifespan')\n",
            (4,),
        ),
        (
            "def patch(target):\n    return target\n"
            "patch('main.lifespan')\n",
            (),
        ),
        (
            "import main\nhelper.setattr(main, 'lifespan', fake)\n",
            (),
        ),
        (
            "import main\nhelper.delattr(main, 'lifespan')\n",
            (),
        ),
        (
            "import main\n"
            "helper.setitem(main.__dict__, 'lifespan', fake)\n",
            (),
        ),
        (
            "import main\nhelper.delitem(main.__dict__, 'lifespan')\n",
            (),
        ),
        (
            "import main\nfrom unittest import mock\n"
            "p = mock.patch\np = safe_helper\n"
            "p.object(main, 'lifespan', fake)\n",
            (5,),
        ),
    ),
    ids=(
        "bound-pop-alias",
        "bound-setitem-alias",
        "bound-ior",
        "dict-ior",
        "operator-ior",
        "parameter-shadow",
        "local-shadow",
        "unrelated-update",
        "unrelated-ior",
        "patch-dict-values-keyword",
        "patch-object-keywords",
        "patch-multiple-target-keyword",
        "monkeypatch-setattr-dotted",
        "monkeypatch-delattr-dotted",
        "patch-dict-unrelated-keyword",
        "lambda-parameter-shadow",
        "lambda-outer-main",
        "comprehension-target-shadow",
        "comprehension-outer-main",
        "for-target-shadow",
        "with-target-shadow",
        "except-target-shadow",
        "match-target-shadow",
        "class-rhs-global-fallback",
        "class-local-safe-shadow",
        "reload-module-keyword",
        "patch-dict-string-target",
        "patch-dict-string-keyword-target",
        "patch-multiple-string-target",
        "patch-multiple-string-keyword-target",
        "patch-dict-string-unrelated-key",
        "monkeypatch-setitem-keywords",
        "monkeypatch-delitem-keywords",
        "monkeypatch-setitem-unrelated-key",
        "runpy-run-module",
        "runpy-run-module-keyword",
        "runpy-run-path",
        "runpy-run-path-keyword",
        "cleanup-helper-call",
        "cleanup-helper-alias-call",
        "cleanup-module-call",
        "active-controller-close-call",
        "active-controller-close-alias",
        "indirect-close-runtime-guarded",
        "controller-unbound-close-call",
        "active-controller-global-close-call",
        "unittest-mock-patch-object-alias",
        "unittest-mock-patch-dict-alias",
        "monkeypatch-bound-setattr-alias",
        "monkeypatch-bound-delattr-alias",
        "monkeypatch-bound-setitem-alias",
        "monkeypatch-bound-delitem-alias",
        "local-patch-safe",
        "arbitrary-helper-setattr-safe",
        "arbitrary-helper-delattr-safe",
        "arbitrary-helper-setitem-safe",
        "arbitrary-helper-delitem-safe",
        "same-scope-stale-patch-taint-conservative",
    ),
)
def test_mutation_boundary_is_scope_and_key_aware(
    tmp_path: Path,
    source: str,
    expected_lines: tuple[int, ...],
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_boundary.py").write_text(source, encoding="utf-8")

    assert find_main_lifespan_boundary_violations(tmp_path) == tuple(
        f"tests/test_boundary.py:{line}" for line in expected_lines
    )


def test_cleanup_calls_are_allowed_only_in_bootstrap_and_internal_owner(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tmp_path / "conftest.py").write_text(
        "from tests.main_lifespan_import import _close_main_import_isolation\n"
        "_close_main_import_isolation(controller)\n",
        encoding="utf-8",
    )
    (tests_dir / "main_lifespan_import.py").write_text(
        "def close_internal(controller):\n"
        "    controller._close()\n",
        encoding="utf-8",
    )
    (tests_dir / "test_boundary.py").write_text(
        "from tests.main_lifespan_import import _close_main_import_isolation\n"
        "_close_main_import_isolation(controller)\n",
        encoding="utf-8",
    )

    assert find_main_lifespan_boundary_violations(tmp_path) == (
        "tests/test_boundary.py:2",
    )


def test_runpy_run_path_matches_only_exact_repo_main_literal(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    main_path = tmp_path / "main.py"
    source = (
        "import runpy\n"
        f"runpy.run_path({str(main_path)!r})\n"
        "runpy.run_path('tests/other.py')\n"
    )
    (tests_dir / "test_boundary.py").write_text(source, encoding="utf-8")

    assert find_main_lifespan_boundary_violations(tmp_path) == (
        "tests/test_boundary.py:2",
    )


@pytest.mark.parametrize("target_kind", ("file", "directory"))
def test_nested_pytest_installs_owner_before_descendant_conftest_and_module_scope(
    target_kind: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tests_root = repo_root / "tests"
    with tempfile.TemporaryDirectory(
        prefix="_main_import_hook_probe_",
        dir=tests_root,
    ) as raw_probe_dir:
        probe_dir = Path(raw_probe_dir)
        (probe_dir / "conftest.py").write_text(
            "import main\n"
            "from tests.main_lifespan_import import get_main_import_isolation\n"
            "owner = get_main_import_isolation().assert_integrity()\n"
            "assert owner is not None and owner.owns(main.lifespan)\n",
            encoding="utf-8",
        )
        test_file = probe_dir / "test_module_scope_owner.py"
        test_file.write_text(
            "from pathlib import Path\n"
            "import os\n"
            "from fastapi.testclient import TestClient\n"
            "import main\n"
            "from tests.test_main_lifespan_storage import (\n"
            "    _guard_checkout_sqlite_connections,\n"
            "    _isolated_production_dependencies,\n"
            ")\n"
            "checkout_db = Path(__file__).resolve().parents[2] / 'db'\n"
            "with _guard_checkout_sqlite_connections(checkout_db) as observed:\n"
            "    with _isolated_production_dependencies():\n"
            "        with TestClient(main.app) as client:\n"
            "            assert client.get('/health').status_code == 200\n"
            "            db_dir = Path(os.environ['GATEWAY_DB_DIR']).resolve()\n"
            "            assert not db_dir.is_relative_to(checkout_db.resolve())\n"
            "assert observed\n"
            "def test_module_scope_owner_was_active():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        target = test_file if target_kind == "file" else probe_dir
        result = subprocess.run(
            [
                str(repo_root / ".venv" / "bin" / "python"),
                "-m",
                "pytest",
                "-q",
                str(target),
            ],
            cwd=repo_root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_nested_pytest_rejects_main_preloaded_before_root_conftest() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tests_root = repo_root / "tests"
    with tempfile.TemporaryDirectory(
        prefix="_main_preload_probe_",
        dir=tests_root,
    ) as raw_probe_dir:
        test_file = Path(raw_probe_dir) / "test_never_runs.py"
        test_file.write_text("def test_never_runs():\n    assert True\n", encoding="utf-8")
        code = (
            "import sys, types, pytest; "
            "sys.modules['main'] = types.ModuleType('main'); "
            f"raise SystemExit(pytest.main(['-q', {str(test_file)!r}]))"
        )
        result = subprocess.run(
            [str(repo_root / ".venv" / "bin" / "python"), "-c", code],
            cwd=repo_root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert result.returncode != 0
    assert "main was imported before" in result.stdout + result.stderr
