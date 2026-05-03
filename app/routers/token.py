"""
Token retrieval — namespaced by widget.

Frontend calls: GET /{widget_name}/token?contextKey=X

Resolution order:
  1. contextKey → ContextSession → account_name  (if MoySklad registered it first)
  2. contextKey → MoySklad Vendor API            (GET /api/vendor/1.0/context/{key} with app secret)
  3. accountId  → AppToken.account_id            (fallback if app secret not configured)
  4. account    → AppToken.account_name          (dev / direct access)
  5. 401 error
"""
import logging
import time
import uuid
import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from ..database import get_db
from ..models import AppToken, ContextSession, WidgetConfig

MS_VENDOR_API = "https://apps-api.moysklad.ru/api/vendor/1.0"

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Token"])


def _vendor_jwt(app_uid: str, secret: str) -> str:
    """One-time HS256 JWT required by MoySklad Vendor API."""
    now = int(time.time())
    return jwt.encode(
        {"sub": app_uid, "iat": now, "jti": str(uuid.uuid4()), "exp": now + 300},
        secret,
        algorithm="HS256",
    )


@router.get("/{widget_name}/token")
async def get_token(
    widget_name: str,
    contextKey: Optional[str] = None,
    account: Optional[str] = None,
    accountId: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    account_name: Optional[str] = None

    # Load widget config (app_secret stored in DB, not env var)
    widget_config = await db.get(WidgetConfig, widget_name)
    app_secret = widget_config.app_secret if widget_config else None

    # 1. Resolve account from contextKey via local ContextSession
    if contextKey:
        session = await db.get(ContextSession, contextKey)
        if session and session.widget_name == widget_name:
            account_name = session.account_name
        else:
            logger.warning("contextKey '%s' not in ContextSession for widget '%s'", contextKey, widget_name)

    # 2. Resolve contextKey via MoySklad Vendor API
    app_uid = widget_config.app_uid if widget_config else None
    if not account_name and contextKey and app_secret and app_uid:
        try:
            vendor_jwt = _vendor_jwt(app_uid, app_secret)
            async with httpx.AsyncClient(timeout=8) as http:
                resp = await http.post(
                    f"{MS_VENDOR_API}/context/{contextKey}",
                    headers={
                        "Authorization": f"Bearer {vendor_jwt}",
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                    },
                )
            if resp.status_code == 200:
                ctx = resp.json()
                resolved_account_id = ctx.get("accountId", "")
                if resolved_account_id:
                    result = await db.execute(
                        select(AppToken).where(
                            AppToken.widget_name == widget_name,
                            AppToken.account_id == resolved_account_id,
                        ).limit(1)
                    )
                    token_row = result.scalar_one_or_none()
                    if token_row:
                        logger.info(
                            "contextKey '%s' resolved via MoySklad Vendor API → account '%s'",
                            contextKey, token_row.account_name,
                        )
                        return {"access_token": token_row.access_token, "account_name": token_row.account_name}
                    logger.warning("Vendor API resolved accountId '%s' but not found in DB", resolved_account_id)
            else:
                logger.warning("MoySklad Vendor API returned %s for contextKey '%s'", resp.status_code, contextKey)
        except Exception as exc:
            logger.error("Failed to call MoySklad Vendor API: %s", exc)

    # 3. Fallback: resolve by accountId (passed directly in URL)
    if not account_name and accountId:
        result = await db.execute(
            select(AppToken).where(
                AppToken.widget_name == widget_name,
                AppToken.account_id == accountId,
            ).limit(1)
        )
        token_row = result.scalar_one_or_none()
        if token_row:
            logger.info("Resolved via accountId '%s' → account '%s'", accountId, token_row.account_name)
            return {"access_token": token_row.access_token, "account_name": token_row.account_name}
        logger.warning("accountId '%s' not found for widget '%s'", accountId, widget_name)

    # 4. Dev fallback: explicit account param
    if not account_name and account:
        account_name = account

    if not account_name:
        raise HTTPException(401, f"Cannot identify account for widget='{widget_name}'")

    token = await db.get(AppToken, (widget_name, account_name))
    if not token:
        raise HTTPException(404, f"No token for widget='{widget_name}' account='{account_name}'")

    return {"access_token": token.access_token, "account_name": account_name}
