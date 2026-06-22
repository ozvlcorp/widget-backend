"""
Token retrieval — namespaced by widget.

Frontend calls: GET /{widget_name}/token?contextKey=X

Security model (fail-closed):
  The ONLY trusted way to identify the account is to validate the contextKey
  against the MoySklad Vendor API (POST /api/vendor/1.0/context/{key}, authorized
  with a JWT signed by the app secret). A token is issued only when:
    - the widget has app_uid + app_secret configured, AND
    - MoySklad confirms the context, AND
    - the accountId returned by MoySklad matches a stored AppToken for this widget.
  accountId is used ONLY for this match, never as a standalone authorization.
  There are deliberately NO accountId / account URL fallbacks — they allowed
  issuing any account's token without proof of context.
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
from ..models import AppToken, WidgetConfig

MS_VENDOR_API = "https://apps-api.moysklad.ru/api/vendor/1.0"

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Token"])


def _ck(contextKey: Optional[str]) -> str:
    """Short, non-sensitive prefix of a contextKey for logging."""
    if not contextKey:
        return "<none>"
    return contextKey[:8] + "…"


def _vendor_jwt(app_uid: str, secret: str) -> str:
    """One-time JWT to authenticate this app to the MoySklad Vendor API."""
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
    db: AsyncSession = Depends(get_db),
):
    if not contextKey:
        raise HTTPException(401, "contextKey is required")

    # Widget must be configured with its MoySklad app credentials. Fail closed.
    widget_config = await db.get(WidgetConfig, widget_name)
    app_secret = widget_config.app_secret if widget_config else None
    app_uid = widget_config.app_uid if widget_config else None
    if not app_secret or not app_uid:
        logger.warning(
            "Token request rejected: widget '%s' has no app credentials configured (ck=%s)",
            widget_name, _ck(contextKey),
        )
        raise HTTPException(401, f"Widget '{widget_name}' is not configured for context validation")

    # Validate contextKey against MoySklad Vendor API — the only trusted path.
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
    except Exception as exc:
        logger.error("Failed to call MoySklad Vendor API (ck=%s): %s", _ck(contextKey), exc)
        raise HTTPException(502, "Could not validate context with MoySklad")

    if resp.status_code != 200:
        logger.warning(
            "MoySklad Vendor API returned %s for contextKey %s (widget '%s')",
            resp.status_code, _ck(contextKey), widget_name,
        )
        raise HTTPException(401, "Invalid or expired contextKey")

    resolved_account_id = (resp.json() or {}).get("accountId", "")
    if not resolved_account_id:
        logger.warning("Vendor API confirmed context but returned no accountId (ck=%s)", _ck(contextKey))
        raise HTTPException(401, "Context did not resolve to an account")

    # accountId is used ONLY to match a stored token for this widget.
    result = await db.execute(
        select(AppToken).where(
            AppToken.widget_name == widget_name,
            AppToken.account_id == resolved_account_id,
        ).limit(1)
    )
    token_row = result.scalar_one_or_none()
    if not token_row:
        logger.warning(
            "Context valid but no token stored for widget '%s' account_id '%s' (ck=%s)",
            widget_name, resolved_account_id, _ck(contextKey),
        )
        raise HTTPException(404, "No token registered for this account; reinstall the app")

    logger.info(
        "contextKey %s validated → widget '%s' account '%s'",
        _ck(contextKey), widget_name, token_row.account_name,
    )
    return {"access_token": token_row.access_token, "account_name": token_row.account_name}
