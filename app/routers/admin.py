"""
Admin endpoints for managing widget configurations.

These are internal endpoints — protect them with a reverse-proxy rule or
set ADMIN_SECRET in your environment and pass it as the X-Admin-Secret header.

  PUT  /admin/widgets/{widget_name}   — register or update a widget's app secret
  GET  /admin/widgets                 — list all registered widgets
  GET  /admin/widgets/{widget_name}   — get a single widget config (secret masked)
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from ..config import settings
from ..database import get_db
from ..models import WidgetConfig

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"])


def _check_secret(x_admin_secret: Optional[str] = Header(default=None)):
    if settings.admin_secret and x_admin_secret != settings.admin_secret:
        raise HTTPException(403, "Forbidden")


class WidgetConfigRequest(BaseModel):
    app_secret: str
    app_uid: Optional[str] = None


class WidgetConfigResponse(BaseModel):
    widget_name: str
    app_uid: Optional[str]
    app_secret_set: bool


@router.put("/widgets/{widget_name}", dependencies=[Depends(_check_secret)])
async def upsert_widget_config(
    widget_name: str,
    body: WidgetConfigRequest,
    db: AsyncSession = Depends(get_db),
):
    existing = await db.get(WidgetConfig, widget_name)
    if existing:
        existing.app_secret = body.app_secret
        if body.app_uid is not None:
            existing.app_uid = body.app_uid
    else:
        db.add(WidgetConfig(
            widget_name=widget_name,
            app_secret=body.app_secret,
            app_uid=body.app_uid,
        ))
    await db.commit()
    logger.info("Widget config upserted for '%s'", widget_name)
    return {"status": "ok", "widget_name": widget_name}


@router.get("/widgets", dependencies=[Depends(_check_secret)])
async def list_widget_configs(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(WidgetConfig))
    rows = result.scalars().all()
    return [
        WidgetConfigResponse(
            widget_name=r.widget_name,
            app_uid=r.app_uid,
            app_secret_set=bool(r.app_secret),
        )
        for r in rows
    ]


@router.get("/widgets/{widget_name}", dependencies=[Depends(_check_secret)])
async def get_widget_config(widget_name: str, db: AsyncSession = Depends(get_db)):
    cfg = await db.get(WidgetConfig, widget_name)
    if not cfg:
        raise HTTPException(404, f"Widget '{widget_name}' not configured")
    return WidgetConfigResponse(
        widget_name=cfg.widget_name,
        app_uid=cfg.app_uid,
        app_secret_set=bool(cfg.app_secret),
    )
