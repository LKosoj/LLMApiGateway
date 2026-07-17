from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

from fastapi.staticfiles import StaticFiles


_GATEWAY_DB_DIR = "GATEWAY_DB_DIR"
_OWNER_ATTRIBUTE = "_llmgateway_test_storage_isolation_owner"
_MISSING = object()
_PROCESS_LIFESPAN_STORAGE_LOCK = threading.Lock()

LifespanFactory = Callable[[Any], AbstractAsyncContextManager[Any]]


class MainLifespanStorageIsolation:
    """Own temporary storage while the production test app lifespan is active."""

    def __init__(
        self,
        main_module: ModuleType,
        *,
        base_dir: Path | None = None,
    ) -> None:
        self._main_module = main_module
        self._base_dir = base_dir

    def wrap(self, lifespan_factory: LifespanFactory) -> LifespanFactory:
        @asynccontextmanager
        async def isolated_lifespan(app: Any) -> AsyncIterator[Any]:
            if not _PROCESS_LIFESPAN_STORAGE_LOCK.acquire(blocking=False):
                raise RuntimeError(
                    "Overlapping production test lifespans cannot safely share process globals."
                )
            try:
                with tempfile.TemporaryDirectory(
                    prefix="llmgateway_main_lifespan_",
                    dir=self._base_dir,
                ) as temp_dir:
                    async with self._isolated_storage(Path(temp_dir)):
                        async with lifespan_factory(app) as value:
                            yield value
            finally:
                _PROCESS_LIFESPAN_STORAGE_LOCK.release()

        setattr(isolated_lifespan, _OWNER_ATTRIBUTE, self)
        return isolated_lifespan

    def owns(self, lifespan_factory: object) -> bool:
        return getattr(lifespan_factory, _OWNER_ATTRIBUTE, None) is self

    @asynccontextmanager
    async def _isolated_storage(self, root: Path) -> AsyncIterator[None]:
        root = root.resolve()
        db_dir = root / "db"
        images_dir = root / "outputs" / "images"
        db_dir.mkdir(parents=True)
        images_dir.mkdir(parents=True)

        previous_db_dir = os.environ.get(_GATEWAY_DB_DIR, _MISSING)
        previous_project_root = self._main_module.PROJECT_ROOT
        previous_images_dir = self._main_module.OUTPUTS_IMAGES_DIR
        os.environ[_GATEWAY_DB_DIR] = str(db_dir)
        self._main_module.PROJECT_ROOT = root
        self._main_module.OUTPUTS_IMAGES_DIR = images_dir
        try:
            with self._isolated_outputs_mount(images_dir):
                yield
        finally:
            self._main_module.OUTPUTS_IMAGES_DIR = previous_images_dir
            self._main_module.PROJECT_ROOT = previous_project_root
            if previous_db_dir is _MISSING:
                os.environ.pop(_GATEWAY_DB_DIR, None)
            else:
                os.environ[_GATEWAY_DB_DIR] = str(previous_db_dir)

    @contextmanager
    def _isolated_outputs_mount(self, images_dir: Path) -> Iterator[None]:
        app = getattr(self._main_module, "app", None)
        if app is None:
            yield
            return

        matches = [
            route
            for route in app.routes
            if getattr(route, "name", None) == "outputs-images"
        ]
        if len(matches) != 1:
            raise RuntimeError("Expected exactly one outputs-images mount")
        mount = matches[0]
        original_app = mount.app
        isolated_app = StaticFiles(directory=images_dir, check_dir=False)
        mount.app = isolated_app
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            rebound = mount.app is not isolated_app
            mount.app = original_app
            if rebound and not failed:
                raise RuntimeError(
                    "Production outputs-images mount was rebound during a test."
                )


@contextmanager
def installed_main_lifespan_storage_isolation(
    main_module: ModuleType,
    *,
    base_dir: Path | None = None,
) -> Iterator[MainLifespanStorageIsolation]:
    """Install one owner over both public and FastAPI-composed lifespan entries."""

    original_lifespan = main_module.lifespan
    router = main_module.app.router
    original_router_lifespan = router.lifespan_context
    owner = MainLifespanStorageIsolation(main_module, base_dir=base_dir)
    wrapped_lifespan = owner.wrap(original_lifespan)
    wrapped_router_lifespan = owner.wrap(original_router_lifespan)

    main_module.lifespan = wrapped_lifespan
    router.lifespan_context = wrapped_router_lifespan
    try:
        yield owner
    finally:
        router_rebound = router.lifespan_context is not wrapped_router_lifespan
        lifespan_rebound = main_module.lifespan is not wrapped_lifespan
        router.lifespan_context = original_router_lifespan
        main_module.lifespan = original_lifespan
        if router_rebound or lifespan_rebound:
            raise RuntimeError("Production test lifespan isolation was rebound during a test.")
