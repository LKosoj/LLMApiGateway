from __future__ import annotations

import atexit
import importlib.abc
import sys
import threading
from collections.abc import Callable, MutableMapping, Sequence
from contextlib import AbstractContextManager
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any

from tests.main_lifespan_storage import (
    MainLifespanStorageIsolation,
    installed_main_lifespan_storage_isolation,
)


class MainImportIsolationError(RuntimeError):
    """Raised when production lifespan isolation cannot be proven."""


class _IsolatingMainLoader(importlib.abc.Loader):
    def __init__(
        self,
        delegate: importlib.abc.Loader,
        controller: MainImportIsolationController,
        resolution_token: int,
    ) -> None:
        self._delegate = delegate
        self._controller = controller
        self._resolution_token = resolution_token

    @property
    def resolution_token(self) -> int:
        return self._resolution_token

    def __getattr__(self, name: str) -> Any:
        self._controller.assert_loader_current(self)
        return getattr(self._delegate, name)

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        self._controller.begin_create(spec, self)
        try:
            create_module = getattr(self._delegate, "create_module", None)
            module = None if create_module is None else create_module(spec)
            self._controller.finish_create(spec, self)
            return module
        except BaseException:
            self._controller.abort_create(spec, self)
            raise

    def exec_module(self, module: ModuleType) -> None:
        self._controller.assert_loader_current(self)
        started = False
        try:
            self._controller.begin_exec(module, self)
            started = True
            exec_module = getattr(self._delegate, "exec_module", None)
            if exec_module is None:
                raise MainImportIsolationError(
                    "The main module loader does not support exec_module()."
                )
            exec_module(module)
            self._controller.finish_import(module, self)
        except BaseException:
            if started:
                self._controller.abort_exec(module, self)
            raise

    def get_code(self, fullname: str) -> Any:
        self._controller.assert_loader_current(self)
        return self._delegate.get_code(fullname)  # type: ignore[attr-defined]

    def get_source(self, fullname: str) -> str | None:
        self._controller.assert_loader_current(self)
        return self._delegate.get_source(fullname)  # type: ignore[attr-defined]

    def get_filename(self, fullname: str) -> str:
        self._controller.assert_loader_current(self)
        return self._delegate.get_filename(fullname)  # type: ignore[attr-defined]

    def is_package(self, fullname: str) -> bool:
        self._controller.assert_loader_current(self)
        return self._delegate.is_package(fullname)  # type: ignore[attr-defined]


class _MainIsolationFinder(importlib.abc.MetaPathFinder):
    def __init__(
        self,
        controller: MainImportIsolationController,
        delegates: Sequence[Any],
    ) -> None:
        self._controller = controller
        self._delegates = tuple(delegates)
        self._resolving = threading.local()

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname != "main":
            return None
        if target is not None:
            raise MainImportIsolationError(
                "Reloading main is forbidden while pytest storage isolation is active."
            )
        if getattr(self._resolving, "active", False):
            raise MainImportIsolationError(
                "Recursive main module resolution cannot be isolated safely."
            )
        resolution_token = self._controller.prepare_new_resolution()

        self._resolving.active = True
        try:
            for finder in self._delegates:
                find_spec = getattr(finder, "find_spec", None)
                if find_spec is None:
                    continue
                spec = find_spec(fullname, path, target)
                if spec is not None:
                    return self._controller.wrap_spec(spec, resolution_token)
        finally:
            self._resolving.active = False
        raise MainImportIsolationError(
            "The expected project main.py could not be resolved safely."
        )


class MainImportIsolationController:
    """Install production lifespan wrappers before the first ``import main`` returns."""

    def __init__(
        self,
        project_root: Path,
        *,
        cleanup_token: object,
        module_registry: MutableMapping[str, ModuleType] | None = None,
        meta_path: list[Any] | None = None,
    ) -> None:
        self._expected_origin = (project_root / "main.py").resolve()
        self._module_registry = module_registry if module_registry is not None else sys.modules
        self._meta_path = meta_path if meta_path is not None else sys.meta_path
        self._lock = threading.RLock()
        self._cleanup_token = cleanup_token
        self._finder: _MainIsolationFinder | None = None
        self._spec: ModuleSpec | None = None
        self._loader: _IsolatingMainLoader | None = None
        self._delegate_loader: importlib.abc.Loader | None = None
        self._loader_reservations: list[
            tuple[ModuleSpec, _IsolatingMainLoader, importlib.abc.Loader]
        ] = []
        self._candidate_module: ModuleType | None = None
        self._resolution_epoch = 0
        self._pending_resolution_token: int | None = None
        self._current_resolution_token: int | None = None
        self._creating_token: int | None = None
        self._executing_module: ModuleType | None = None
        self._executing_token: int | None = None
        self._exec_attempted = False
        self._terminal_failure: str | None = None
        self._module: ModuleType | None = None
        self._installation: AbstractContextManager[MainLifespanStorageIsolation] | None = None
        self._owner: MainLifespanStorageIsolation | None = None
        self._closed = False

    @property
    def owner(self) -> MainLifespanStorageIsolation | None:
        return self._owner

    @property
    def finder(self) -> importlib.abc.MetaPathFinder | None:
        return self._finder

    @property
    def module(self) -> ModuleType | None:
        return self._module

    def install(self) -> None:
        with self._lock:
            if self._closed:
                raise MainImportIsolationError(
                    "A closed main import isolation controller cannot be reused."
                )
            if self._finder is not None:
                self.assert_integrity()
                return
            if "main" in self._module_registry:
                raise MainImportIsolationError(
                    "main was imported before the pytest storage isolation hook."
                )
            if any(isinstance(finder, _MainIsolationFinder) for finder in self._meta_path):
                raise MainImportIsolationError(
                    "Another main import isolation finder is already installed."
                )
            finder = _MainIsolationFinder(self, tuple(self._meta_path))
            self._meta_path.insert(0, finder)
            self._finder = finder

    def _raise_if_terminal_failed(self) -> None:
        if self._terminal_failure is not None:
            raise MainImportIsolationError(self._terminal_failure)

    def _invalidate_resolution(self) -> None:
        self._spec = None
        self._loader = None
        self._delegate_loader = None
        self._current_resolution_token = None
        self._creating_token = None

    def prepare_new_resolution(self) -> int:
        with self._lock:
            self._raise_if_terminal_failed()
            if self._closed or self._finder is None:
                raise MainImportIsolationError(
                    "The main import isolation hook is not active."
                )
            if self._module is not None:
                raise MainImportIsolationError(
                    "A distinct main module generation cannot be isolated safely."
                )
            if self._executing_token is not None:
                raise MainImportIsolationError(
                    "main execution is already in progress."
                )
            if self._creating_token is not None:
                raise MainImportIsolationError(
                    "main module creation is already in progress."
                )
            self._invalidate_resolution()
            self._resolution_epoch += 1
            self._pending_resolution_token = self._resolution_epoch
            return self._resolution_epoch

    def wrap_spec(self, spec: ModuleSpec, resolution_token: int) -> ModuleSpec:
        with self._lock:
            self._raise_if_terminal_failed()
            if self._closed or self._finder is None:
                raise MainImportIsolationError(
                    "main resolved after its isolation hook was removed."
                )
            if self._pending_resolution_token != resolution_token:
                raise MainImportIsolationError(
                    "A stale resolution cannot publish a main isolation loader."
                )
            if (
                self._module is not None
                or self._creating_token is not None
                or self._executing_token is not None
            ):
                raise MainImportIsolationError(
                    "main resolution completed after an owner became active."
                )
            origin = spec.origin
            try:
                resolved_origin = Path(origin).resolve() if origin is not None else None
            except (OSError, TypeError, ValueError):
                resolved_origin = None
            if resolved_origin != self._expected_origin:
                raise MainImportIsolationError(
                    "Resolved main module origin does not match the project main.py: "
                    f"{origin!r}."
                )
            loader = spec.loader
            if isinstance(loader, _IsolatingMainLoader):
                if loader._controller is not self:
                    raise MainImportIsolationError(
                        "The project main.py was wrapped by a foreign isolation loader."
                    )
                loader = loader._delegate
            if loader is None or not isinstance(loader, importlib.abc.Loader):
                raise MainImportIsolationError(
                    "The project main.py does not have a supported module loader."
                )
            isolating_loader = _IsolatingMainLoader(
                loader,
                self,
                resolution_token,
            )
            self._spec = spec
            self._loader = isolating_loader
            self._delegate_loader = loader
            self._pending_resolution_token = None
            self._current_resolution_token = resolution_token
            self._loader_reservations.append((spec, isolating_loader, loader))
            spec.loader = isolating_loader
            return spec

    def assert_loader_current(self, loader: _IsolatingMainLoader) -> None:
        with self._lock:
            self._raise_if_terminal_failed()
            if (
                self._closed
                or loader is not self._loader
                or loader.resolution_token != self._current_resolution_token
            ):
                raise MainImportIsolationError(
                    "A stale main isolation loader cannot delegate."
                )

    def begin_create(
        self,
        spec: ModuleSpec,
        loader: _IsolatingMainLoader,
    ) -> None:
        with self._lock:
            resolution_token = loader.resolution_token
            self._raise_if_terminal_failed()
            if self._closed or self._finder is None:
                raise MainImportIsolationError(
                    "main module creation started after isolation was removed."
                )
            if (
                loader is not self._loader
                or resolution_token != self._current_resolution_token
                or spec is not self._spec
            ):
                raise MainImportIsolationError(
                    "A stale main isolation loader cannot create a module."
                )
            if spec.loader is not loader:
                raise MainImportIsolationError(
                    "main loader metadata changed before module creation."
                )
            if self._creating_token is not None:
                raise MainImportIsolationError(
                    "main module creation is already in progress."
                )
            self._creating_token = resolution_token

    def finish_create(
        self,
        spec: ModuleSpec,
        loader: _IsolatingMainLoader,
    ) -> None:
        with self._lock:
            resolution_token = loader.resolution_token
            if (
                self._creating_token != resolution_token
                or self._current_resolution_token != resolution_token
                or spec is not self._spec
                or spec.loader is not loader
            ):
                raise MainImportIsolationError(
                    "main module creation lost its isolation owner."
                )
            self._creating_token = None

    def abort_create(
        self,
        spec: ModuleSpec,
        loader: _IsolatingMainLoader,
    ) -> None:
        with self._lock:
            resolution_token = loader.resolution_token
            if self._creating_token == resolution_token:
                self._creating_token = None
                if self._current_resolution_token == resolution_token:
                    self._invalidate_resolution()

    def begin_exec(
        self,
        module: ModuleType,
        loader: _IsolatingMainLoader,
    ) -> None:
        with self._lock:
            resolution_token = loader.resolution_token
            self._raise_if_terminal_failed()
            if self._closed or self._finder is None:
                raise MainImportIsolationError(
                    "main execution started after its isolation hook was removed."
                )
            if self._module is not None:
                raise MainImportIsolationError(
                    "The isolated main module was already executed."
                )
            if self._executing_token is not None:
                raise MainImportIsolationError(
                    "The isolated main module execution is already in progress."
                )
            if self._creating_token is not None:
                raise MainImportIsolationError(
                    "main execution started before module creation completed."
                )
            if self._exec_attempted:
                raise MainImportIsolationError(
                    "The isolated main module already had an execution attempt."
                )
            if (
                loader is not self._loader
                or resolution_token != self._current_resolution_token
            ):
                raise MainImportIsolationError(
                    "A stale main isolation loader cannot execute a module."
                )
            spec = self._spec
            if (
                spec is None
                or module.__spec__ is not spec
                or module.__spec__.loader is not loader
                or module.__loader__ is not loader
            ):
                raise MainImportIsolationError(
                    "main loader metadata changed before isolated execution."
                )
            if self._module_registry.get("main") is not module:
                raise MainImportIsolationError(
                    "main was replaced in sys.modules before isolated execution."
                )
            self._exec_attempted = True
            self._candidate_module = module
            self._executing_module = module
            self._executing_token = resolution_token

    def abort_exec(
        self,
        module: ModuleType,
        loader: _IsolatingMainLoader,
    ) -> None:
        with self._lock:
            resolution_token = loader.resolution_token
            if (
                self._executing_module is module
                and self._executing_token == resolution_token
            ):
                self._executing_module = None
                self._executing_token = None
                self._terminal_failure = (
                    "The main import isolation controller is terminal failed after "
                    "module execution aborted."
                )

    def finish_import(
        self,
        module: ModuleType,
        loader: _IsolatingMainLoader,
    ) -> None:
        with self._lock:
            resolution_token = loader.resolution_token
            if self._closed or self._finder is None:
                raise MainImportIsolationError(
                    "main finished importing after its isolation hook was removed."
                )
            if (
                self._executing_module is not module
                or self._executing_token != resolution_token
                or self._current_resolution_token != resolution_token
            ):
                raise MainImportIsolationError(
                    "main finished without its matching isolated execution owner."
                )
            spec = self._spec
            if (
                spec is None
                or module.__spec__ is not spec
                or module.__spec__.loader is not loader
                or module.__loader__ is not loader
            ):
                raise MainImportIsolationError(
                    "main loader metadata changed during isolated execution."
                )
            if self._module_registry.get("main") is not module:
                raise MainImportIsolationError(
                    "main was replaced in sys.modules before isolation was installed."
                )

            installation = installed_main_lifespan_storage_isolation(module)
            owner = installation.__enter__()
            self._installation = installation
            self._owner = owner
            self._module = module
            self._executing_module = None
            self._executing_token = None

    def assert_integrity(self) -> MainLifespanStorageIsolation | None:
        with self._lock:
            self._raise_if_terminal_failed()
            if self._closed:
                raise MainImportIsolationError(
                    "The main import isolation controller is closed."
                )
            if self._finder is None or self._finder not in self._meta_path:
                raise MainImportIsolationError(
                    "The main import isolation finder was removed during pytest."
                )
            current = self._module_registry.get("main")
            if self._module is None:
                if self._executing_token is not None:
                    raise MainImportIsolationError(
                        "main execution is still in progress without an isolation owner."
                    )
                if current is not None:
                    raise MainImportIsolationError(
                        "main is present without an active storage isolation owner."
                    )
                return None
            if current is not self._module:
                raise MainImportIsolationError(
                    "The isolated main module was removed or replaced in sys.modules."
                )
            loader = self._loader
            spec = self._spec
            if (
                loader is None
                or spec is None
                or self._module.__spec__ is not spec
                or spec.loader is not loader
                or self._module.__loader__ is not loader
                or loader.resolution_token != self._current_resolution_token
            ):
                raise MainImportIsolationError(
                    "The isolated main module loader metadata was rebound during pytest."
                )
            owner = self._owner
            if owner is None or not owner.owns(self._module.lifespan):
                raise MainImportIsolationError(
                    "main.lifespan was rebound during pytest."
                )
            if not owner.owns(self._module.app.router.lifespan_context):
                raise MainImportIsolationError(
                    "main.app.router.lifespan_context was rebound during pytest."
                )
            return owner

    def _close(self, cleanup_token: object) -> None:
        with self._lock:
            if cleanup_token is not self._cleanup_token:
                raise MainImportIsolationError(
                    "Invalid main import isolation cleanup capability."
                )
            if self._closed:
                return
            if self._executing_token is not None or self._creating_token is not None:
                raise MainImportIsolationError(
                    "Cannot close main import isolation during module execution."
                )
            self._closed = True
            finder = self._finder
            self._finder = None
            if finder is not None:
                self._meta_path[:] = [item for item in self._meta_path if item is not finder]

            installation = self._installation
            module = self._module
            candidate_module = self._candidate_module
            spec = self._spec
            loader = self._loader
            delegate_loader = self._delegate_loader
            loader_reservations = tuple(self._loader_reservations)
            registry_mismatch = bool(
                module is not None and self._module_registry.get("main") is not module
            )
            loader_mismatch = bool(
                module is not None
                and (
                    spec is None
                    or loader is None
                    or module.__spec__ is not spec
                    or spec.loader is not loader
                    or module.__loader__ is not loader
                )
            )
            self._installation = None
            self._owner = None
            self._module = None
            try:
                if installation is not None:
                    installation.__exit__(None, None, None)
            finally:
                for reserved_spec, reserved_loader, reserved_delegate in reversed(
                    loader_reservations
                ):
                    if reserved_spec.loader is reserved_loader:
                        reserved_spec.loader = reserved_delegate
                if candidate_module is not None:
                    candidate_module.__loader__ = delegate_loader
                self._spec = None
                self._loader = None
                self._delegate_loader = None
                self._loader_reservations.clear()
                self._candidate_module = None
                self._pending_resolution_token = None
                self._current_resolution_token = None
                self._creating_token = None
                self._executing_token = None
                if registry_mismatch:
                    raise MainImportIsolationError(
                        "The isolated main module was removed or replaced before cleanup."
                    )
                if loader_mismatch:
                    raise MainImportIsolationError(
                        "The isolated main module loader metadata changed before cleanup."
                    )


_ACTIVE_CONTROLLER: MainImportIsolationController | None = None


def _close_main_import_isolation(
    controller: MainImportIsolationController,
    cleanup_token: object,
) -> None:
    global _ACTIVE_CONTROLLER

    try:
        controller._close(cleanup_token)
    except BaseException:
        if controller._closed and _ACTIVE_CONTROLLER is controller:
            _ACTIVE_CONTROLLER = None
        raise
    if _ACTIVE_CONTROLLER is controller:
        _ACTIVE_CONTROLLER = None


def _make_cleanup_handle(
    controller: MainImportIsolationController,
    cleanup_token: object,
) -> Callable[[], None]:
    def cleanup() -> None:
        _close_main_import_isolation(controller, cleanup_token)

    return cleanup


def install_main_import_isolation(
    project_root: Path,
) -> tuple[MainImportIsolationController, Callable[[], None]]:
    global _ACTIVE_CONTROLLER

    if _ACTIVE_CONTROLLER is not None:
        _ACTIVE_CONTROLLER.assert_integrity()
        raise MainImportIsolationError(
            "The main import isolation controller is already installed."
        )
    cleanup_token = object()
    controller = MainImportIsolationController(
        project_root,
        cleanup_token=cleanup_token,
    )
    controller.install()
    cleanup = _make_cleanup_handle(controller, cleanup_token)
    try:
        atexit.register(cleanup)
    except BaseException:
        controller._close(cleanup_token)
        raise
    _ACTIVE_CONTROLLER = controller
    return controller, cleanup


def get_main_import_isolation() -> MainImportIsolationController:
    controller = _ACTIVE_CONTROLLER
    if controller is None:
        raise MainImportIsolationError(
            "The pytest main import isolation controller is not installed."
        )
    return controller
