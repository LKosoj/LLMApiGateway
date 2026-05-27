"""Translator Debugger API.

Master-only endpoint that runs a request through the full OpenAI ↔ Anthropic
translation pipeline and returns each intermediate step so developers can
inspect payload transformations without sending a real request to a provider.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ...config.paths import STATIC_DIR
from ...middleware.auth import ROLE_MASTER
from ...services.translator_debug import build_translation_steps
from ...utils.html_cache import get_template

logger = logging.getLogger(__name__)

translator_debug_router = APIRouter()


class TranslatorDebugRequest(BaseModel):
    source_format: Literal["openai", "anthropic"]
    target_format: Literal["openai", "anthropic"]
    request_body: dict[str, Any]
    mock_provider_response: dict[str, Any] | None = None


class TranslatorDebugStep(BaseModel):
    step: int
    name: str
    title: str
    payload: Any
    notes: str | None


class TranslatorDebugResponse(BaseModel):
    steps: list[TranslatorDebugStep]


def _require_master(request: Request) -> None:
    role = getattr(request.state, "api_key_role", None)
    if role != ROLE_MASTER:
        raise HTTPException(
            status_code=403,
            detail="This endpoint is reserved for the master API key.",
        )


@translator_debug_router.get(
    "/ui/translator-debug",
    response_class=HTMLResponse,
    tags=["Translator Debugger UI"],
)
async def get_translator_debug_page(request: Request) -> HTMLResponse:
    """Serve the Translator Debugger HTML page (master-only via middleware)."""
    html_path = STATIC_DIR / "translator-debug.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Translator debug page not found.")
    try:
        return HTMLResponse(content=await get_template(html_path))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error reading translator debug HTML: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load translator debug page.")


@translator_debug_router.post(
    "/admin/translator/debug",
    response_class=JSONResponse,
    tags=["Translator Debugger API"],
)
async def run_translator_debug(
    request: Request,
    body: TranslatorDebugRequest,
) -> JSONResponse:
    """Run the translation pipeline and return 7 intermediate steps.

    Requires the master API key. Virtual key holders receive 403.
    """
    _require_master(request)

    try:
        raw_steps = build_translation_steps(
            source_format=body.source_format,
            target_format=body.target_format,
            request_body=body.request_body,
            mock_provider_response=body.mock_provider_response,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error running translation pipeline: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=f"Translation failed: {exc}",
        )

    response = TranslatorDebugResponse(
        steps=[TranslatorDebugStep(**s) for s in raw_steps]
    )
    return JSONResponse(content=response.model_dump())
