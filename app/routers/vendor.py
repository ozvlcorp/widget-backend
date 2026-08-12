"""
MoySklad Vendor API endpoints — namespaced by widget.

Descriptor endpointBase for each widget:
  https://widget-backend-oymoysklad.com/{widget_name}

MoySklad calls:
  PUT    /{widget_name}/api/moysklad/vendor/1.0/apps/{appId}/{accountId}
  DELETE /{widget_name}/api/moysklad/vendor/1.0/apps/{appId}/{accountId}
"""
import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select
from pydantic import BaseModel
from typing import Optional

from ..database import get_db
from ..models import AppToken, ContextSession

logger = logging.getLogger(__name__)
router = APIRouter(tags=["MoySklad Vendor API"])


class AccessItem(BaseModel):
    resource: str
    scope: list[str]
    access_token: str


class ActivationRequest(BaseModel):
    appUid: str
    accountName: str
    access: list[AccessItem]
    cause: str
    subscription: Optional[dict] = None


class DeactivationRequest(BaseModel):
    appUid: str
    accountName: str
    cause: str


class ContextKeyRequest(BaseModel):
    accountName: str
    userId: Optional[str] = None
    uid: Optional[str] = None


@router.put("/{widget_name}/api/moysklad/vendor/1.0/apps/{app_id}/{account_id}")
async def activate(
    widget_name: str,
    app_id: str,
    account_id: str,
    body: ActivationRequest,
    db: AsyncSession = Depends(get_db),
):
    if not body.access:
        raise HTTPException(400, "No access token provided")

    access_token = body.access[0].access_token
    existing = await db.get(AppToken, (widget_name, body.accountName))

    if existing:
        existing.access_token = access_token
        existing.app_uid = body.appUid
        existing.account_id = account_id
    else:
        db.add(AppToken(
            widget_name=widget_name,
            account_name=body.accountName,
            app_uid=body.appUid,
            access_token=access_token,
            account_id=account_id,
        ))

    await db.commit()
    return {"status": "Activated"}


@router.get("/{widget_name}/api/moysklad/vendor/1.0/apps/{app_id}/{account_id}")
async def app_status(
    widget_name: str,
    app_id: str,
    account_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Статус приложения в аккаунте. МойСклад опрашивает его при установке —
    без обработчика ручка отвечала 405, и установка вставала."""
    result = await db.execute(
        select(AppToken).where(
            AppToken.widget_name == widget_name,
            AppToken.account_id == account_id,
        ).limit(1)
    )
    return {"status": "Activated" if result.scalar_one_or_none() else "Deactivated"}


@router.delete("/{widget_name}/api/moysklad/vendor/1.0/apps/{app_id}/{account_id}")
async def deactivate(
    widget_name: str,
    app_id: str,
    account_id: str,
    body: DeactivationRequest,
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        delete(AppToken).where(
            AppToken.widget_name == widget_name,
            AppToken.account_name == body.accountName,
        )
    )
    # Заодно выбрасываем контексты аккаунта: без этого висящий contextKey
    # продолжал бы менять себя на уже удалённый токен.
    await db.execute(
        delete(ContextSession).where(
            ContextSession.widget_name == widget_name,
            ContextSession.account_name == body.accountName,
        )
    )
    await db.commit()
    return {"status": "Deactivated"}


@router.put("/{widget_name}/api/moysklad/vendor/1.0/context/{context_key}")
async def register_context_key(
    widget_name: str,
    context_key: str,
    body: ContextKeyRequest,
    db: AsyncSession = Depends(get_db),
):
    """MoySklad calls this to register a contextKey before loading the iframe."""
    # Сам ключ в лог не пишем — это учётные данные; хватит отпечатка.
    logger.info(
        "Context registered: widget='%s' contextKey=%s",
        widget_name, hashlib.sha256(context_key.encode()).hexdigest()[:8],
    )
    existing = await db.get(ContextSession, context_key)
    if existing:
        existing.account_name = body.accountName
        existing.widget_name = widget_name
    else:
        db.add(ContextSession(
            context_key=context_key,
            widget_name=widget_name,
            account_name=body.accountName,
        ))
    await db.commit()
    return {}
