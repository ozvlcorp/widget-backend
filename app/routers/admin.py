"""
Admin endpoints for managing widget configurations.

These are internal endpoints — protect them with a reverse-proxy rule or
set ADMIN_SECRET in your environment and pass it as the X-Admin-Secret header.

  PUT  /admin/widgets/{widget_name}   — register or update a widget's app secret
  GET  /admin/widgets                 — list all registered widgets
  GET  /admin/widgets/{widget_name}   — get a single widget config (secret masked)
  GET  /admin/overview                — что лежит в базе: виджеты и их аккаунты
  POST /admin/widgets/{old}/rename    — переименовать пространство имён виджета
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select, update
from pydantic import BaseModel
from typing import Optional

from ..config import settings
from ..database import get_db
from ..models import AppToken, ContextSession, WidgetConfig

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"])


def _check_secret(x_admin_secret: Optional[str] = Header(default=None)):
    # Пустой ADMIN_SECRET раньше СНИМАЛ проверку — ручки, меняющие app_secret
    # любого виджета, оказывались открыты всему интернету. Теперь пусто = выключено.
    if not settings.admin_secret:
        raise HTTPException(503, "ADMIN_SECRET is not configured — admin endpoints are disabled")
    if x_admin_secret != settings.admin_secret:
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


@router.get("/overview", dependencies=[Depends(_check_secret)])
async def overview(db: AsyncSession = Depends(get_db)):
    """
    Что вообще лежит в базе — заменяет ручные SELECT'ы в psql.

    Токены не отдаём: это ключи полного доступа к МоемуСкладу, и админской
    ручке достаточно знать, что они есть.
    """
    cfgs = (await db.execute(select(WidgetConfig))).scalars().all()
    tokens = (await db.execute(select(AppToken))).scalars().all()
    ctx = (await db.execute(select(ContextSession))).scalars().all()

    names = sorted({c.widget_name for c in cfgs} | {t.widget_name for t in tokens})
    return {
        "widgets": [
            {
                "widget_name": n,
                "app_uid": next((c.app_uid for c in cfgs if c.widget_name == n), None),
                "app_secret_set": any(c.widget_name == n and c.app_secret for c in cfgs),
                "accounts": [
                    {"account_name": t.account_name, "account_id": t.account_id}
                    for t in tokens if t.widget_name == n
                ],
                "context_sessions": sum(1 for c in ctx if c.widget_name == n),
            }
            for n in names
        ]
    }


@router.post("/widgets/{source}/rename/{target}", dependencies=[Depends(_check_secret)])
async def rename_widget(source: str, target: str, db: AsyncSession = Depends(get_db)):
    """
    Переименовать пространство имён виджета вместе с установленными токенами.

    Именно переименовать, а не «удалить и завести заново»: в app_tokens лежит
    токен установленного приложения, и потеря строки означала бы переустановку
    приложения в аккаунте.

    Отказываемся, если у цели уже есть строки: у app_tokens первичный ключ
    (widget_name, account_name), и слияние молча затёрло бы чужой токен.
    """
    if source == target:
        raise HTTPException(400, "source and target are the same")

    src_tokens = (await db.execute(
        select(AppToken).where(AppToken.widget_name == source)
    )).scalars().all()
    src_cfg = await db.get(WidgetConfig, source)
    if not src_tokens and not src_cfg:
        raise HTTPException(404, f"Nothing stored under '{source}'")

    busy = (await db.execute(
        select(AppToken).where(AppToken.widget_name == target)
    )).scalars().all()
    if busy or await db.get(WidgetConfig, target):
        raise HTTPException(409, f"'{target}' already has data — merge it by hand")

    moved_accounts = [t.account_name for t in src_tokens]
    await db.execute(
        update(AppToken).where(AppToken.widget_name == source).values(widget_name=target)
    )
    if src_cfg:
        # widget_name — первичный ключ, UPDATE по нему через ORM не пройдёт:
        # переносим значения в новую строку и убираем старую.
        db.add(WidgetConfig(
            widget_name=target, app_secret=src_cfg.app_secret, app_uid=src_cfg.app_uid,
        ))
        await db.delete(src_cfg)
    # Контексты одноразовые и короткоживущие — переносить нечего.
    await db.execute(delete(ContextSession).where(ContextSession.widget_name == source))
    await db.commit()

    logger.info("Widget namespace renamed: '%s' → '%s'", source, target)
    return {
        "status": "ok",
        "from": source,
        "to": target,
        "accounts_moved": moved_accounts,
        "config_moved": bool(src_cfg),
    }
