"""Admin endpoint for querying governance rejection events."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from ...config.paths import STATIC_DIR
from ...db.rejections_db import VALID_CATEGORIES, RejectionsDB
from ...middleware.auth import ROLE_MASTER
from ...utils.html_cache import get_template

admin_rejections_router = APIRouter()


def _require_master(request: Request) -> None:
    role = getattr(request.state, "api_key_role", None)
    if role != ROLE_MASTER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the master API key can access rejection events",
        )


@admin_rejections_router.get(
    "/ui/rejections", response_class=HTMLResponse, include_in_schema=False
)
async def get_rejections_page(request: Request):
    _require_master(request)
    html_path = STATIC_DIR / "rejections.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Rejections UI page not found.")
    return HTMLResponse(content=await get_template(html_path))


@admin_rejections_router.get("/admin/rejections")
async def list_rejections(
    request: Request,
    api_key_id: int | None = None,
    category: str | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    _require_master(request)
    if category is not None and category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unknown category. Allowed values: "
                + ", ".join(sorted(VALID_CATEGORIES))
            ),
        )
    db: RejectionsDB | None = getattr(request.app.state, "rejections_db", None)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="RejectionsDB is not initialized",
        )
    try:
        items, total = await db.get_rejections(
            api_key_id=api_key_id,
            category=category,
            since=since,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"items": items, "total": total}
