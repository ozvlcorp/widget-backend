"""
MoySklad Vendor API endpoints — namespaced by widget.

Descriptor endpointBase for each widget:
  https://widget-backend-oymoysklad.com/{widget_name}

MoySklad calls:
  PUT    /{widget_name}/api/moysklad/vendor/1.0/apps/{appId}/{accountId}
  DELETE /{widget_name}/api/moysklad/vendor/1.0/apps/{appId}/{accountId}
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete
from pydantic import BaseModel
from typing import Optional

from ..database import get_db
from ..models import AppToken, ContextSession

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
    import logging
    logger = logging.getLogger(__name__)
    logger.info(
        "Context registration: widget='%s' contextKey='%s' account='%s'",
        widget_name, context_key, body.accountName,
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
