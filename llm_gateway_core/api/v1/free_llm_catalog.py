"""Free-tier LLM catalog page and API.

Serves the filtered freellmapi.co snapshot built by
``llm_gateway_core.services.free_llm_catalog.FreeLlmCatalogService`` to the
`/v1/ui/free-models` page. Accessible to both roles (master and virtual
keys) -- there is nothing account-specific in the catalog.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from llm_gateway_core.config.paths import STATIC_DIR
from llm_gateway_core.utils.html_cache import get_template

if TYPE_CHECKING:
    from llm_gateway_core.services.runtime_config import AppServices

logger = logging.getLogger(__name__)

free_llm_catalog_router = APIRouter()


def _capture_app_services(request: Request) -> "AppServices":
    return cast("AppServices", request.app.state.services)


@free_llm_catalog_router.get(
    "/ui/free-models", response_class=HTMLResponse, tags=["Free LLM Catalog UI"]
)
async def get_free_models_page(request: Request) -> HTMLResponse:
    """Serve the free-tier LLM catalog HTML page."""
    html_path = STATIC_DIR / "free-models.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Free models page not found.")
    try:
        return HTMLResponse(content=await get_template(html_path))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error reading free models HTML: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load free models page.")


@free_llm_catalog_router.get(
    "/api/free-models", response_class=JSONResponse, tags=["Free LLM Catalog API"]
)
async def get_free_models(request: Request) -> JSONResponse:
    """Return the filtered free-tier LLM catalog snapshot. No caching."""
    services = _capture_app_services(request)
    status = await services.free_llm_catalog_service.get_status()
    return JSONResponse(content=status)
