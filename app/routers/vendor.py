"""
MoySklad Vendor API endpoints — namespaced by widget.

Descriptor endpointBase for each widget:
  https://widget-backend.oymoysklad.com/{widget_name}

MoySklad calls (server-to-server, authenticated with a JWT signed by the app secret):
  PUT    /{widget_name}/api/moysklad/vendor/1.0/apps/{appId}/{accountId}
  DELETE /{widget_name}/api/moysklad/vendor/1.0/apps/{appId}/{accountId}
  PUT    /{widget_name}/api/moysklad/vendor/1.0/context/{contextKey}
"""
import logging

import httpx
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete
from pydantic import BaseModel
from typing import Optional

from ..database import get_db
from ..models import AppToken, ContextSession, WidgetConfig

logger = logging.getLogger(__name__)
router = APIRouter(tags=["MoySklad Vendor API"])

MS_REMAP_API = "https://api.moysklad.ru/api/remap/1.2"

# KPI custom fields to provision on the organization (юр.лицо).
# match_key is the substring used for idempotent matching by name.
KPI_FIELDS = [
    ("KPI 7 (неделя)", "7"),
    ("KPI 30 (месяц)", "30"),
    ("KPI 365 (год)", "365"),
]


async def _verify_ms_jwt(widget_name: str, authorization: Optional[str], db: AsyncSession) -> None:
    """
    Verify the inbound MoySklad Vendor JWT (Authorization: Bearer <jwt>) using the
    widget's app secret. Fail closed: missing config, missing header, or bad
    signature -> 401. This prevents anyone from forging install / context calls.
    """
    widget_config = await db.get(WidgetConfig, widget_name)
    app_secret = widget_config.app_secret if widget_config else None
    if not app_secret:
        logger.warning("Vendor call rejected: widget '%s' has no app_secret configured", widget_name)
        raise HTTPException(401, f"Widget '{widget_name}' is not configured")

    if not authorization or not authorization.lower().startswith("bearer "):
        logger.warning("Vendor call rejected: missing bearer token (widget '%s')", widget_name)
        raise HTTPException(401, "Missing authorization")

    token = authorization.split(" ", 1)[1].strip()
    try:
        jwt.decode(token, app_secret, algorithms=["HS256"], options={"verify_aud": False})
    except Exception as exc:
        logger.warning("Vendor call rejected: invalid JWT (widget '%s'): %s", widget_name, exc)
        raise HTTPException(401, "Invalid authorization")


async def _provision_kpi_fields(access_token: str) -> None:
    """
    Idempotently create the KPI custom fields on the organization metadata.
    Only the missing ones are created (matched by name containing 'KPI' + 7/30/365).
    Never logs the access token. Failures here must not break activation.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json;charset=utf-8",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.get(f"{MS_REMAP_API}/entity/organization/metadata/attributes", headers=headers)
        resp.raise_for_status()
        existing = resp.json().get("rows", [])
        existing_names = [(a.get("name") or "") for a in existing]

        for name, match_num in KPI_FIELDS:
            already = any("KPI" in n and match_num in n for n in existing_names)
            if already:
                logger.info("KPI provisioning: '%s' already present, skipping", name)
                continue
            create = await http.post(
                f"{MS_REMAP_API}/entity/organization/metadata/attributes",
                headers=headers,
                json={"name": name, "type": "double", "required": False},
            )
            if create.status_code in (200, 201):
                logger.info("KPI provisioning: created field '%s'", name)
            else:
                logger.warning("KPI provisioning: failed to create '%s' (status %s)", name, create.status_code)


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
    authorization: Optional[str] = Header(default=None),
):
    await _verify_ms_jwt(widget_name, authorization, db)

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

    # Idempotently provision KPI custom fields on the organization. Server-side,
    # so the browser token can stay read-only. Failures must not break activation.
    try:
        await _provision_kpi_fields(access_token)
    except Exception as exc:
        logger.error("KPI provisioning failed for widget '%s' account '%s': %s",
                     widget_name, body.accountName, exc)

    return {"status": "Activated"}


@router.delete("/{widget_name}/api/moysklad/vendor/1.0/apps/{app_id}/{account_id}")
async def deactivate(
    widget_name: str,
    app_id: str,
    account_id: str,
    body: DeactivationRequest,
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(default=None),
):
    await _verify_ms_jwt(widget_name, authorization, db)

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
    authorization: Optional[str] = Header(default=None),
):
    """MoySklad calls this (authenticated) to register a contextKey before loading the iframe."""
    await _verify_ms_jwt(widget_name, authorization, db)

    logger.info(
        "Context registration: widget='%s' contextKey='%s…' account='%s'",
        widget_name, context_key[:8], body.accountName,
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
