"""
Token retrieval — namespaced by widget.

Frontend calls: GET /{widget_name}/token?contextKey=X

Порядок разрешения:
  1. contextKey → ContextSession (МойСклад зарегистрировал его заранее), с проверкой срока
  2. contextKey → MoySklad Vendor API (POST /api/vendor/1.0/context/{key} с app secret)
  3. 401

Запасные пути по accountId и по имени аккаунта ОТКЛЮЧЕНЫ по умолчанию. Они
отдавали access_token любому, кто знает или угадает имя аккаунта: ручка открыта
наружу, авторизации на ней нет. Включаются переменной
ALLOW_INSECURE_ACCOUNT_FALLBACK=1 — только как временная мера, пока приложение
не зарегистрировано в кабинете вендора.
"""
import hashlib
import logging
import time
import uuid
import httpx
import jwt
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from ..config import settings
from ..database import get_db
from ..models import AppToken, ContextSession, WidgetConfig

MS_VENDOR_API = "https://apps-api.moysklad.ru/api/vendor/1.0"

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Token"])


def _fp(secret: Optional[str]) -> str:
    """Короткий отпечаток для логов: сам ключ — учётные данные, в логах ему не место."""
    if not secret:
        return "-"
    return hashlib.sha256(secret.encode()).hexdigest()[:8]


def _vendor_jwt(app_uid: str, secret: str) -> str:
    """One-time HS256 JWT required by MoySklad Vendor API."""
    now = int(time.time())
    return jwt.encode(
        {"sub": app_uid, "iat": now, "jti": str(uuid.uuid4()), "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _is_fresh(created_at) -> bool:
    """contextKey живёт ограниченное время — просроченную запись не принимаем."""
    if created_at is None:
        return False
    ttl = settings.context_key_ttl_seconds
    if ttl <= 0:
        return True
    moment = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - moment <= timedelta(seconds=ttl)


async def _token_for_account_id(db: AsyncSession, widget_name: str, account_id: str):
    result = await db.execute(
        select(AppToken).where(
            AppToken.widget_name == widget_name,
            AppToken.account_id == account_id,
            # Приостановленная и удалённая установки остаются в таблице ради
            # сохранённой конфигурации, но токена по ним выдавать нельзя.
            AppToken.status == "active",
        ).limit(1)
    )
    return result.scalar_one_or_none()


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
    app_uid = widget_config.app_uid if widget_config else None

    # 1. contextKey, заранее зарегистрированный МойСкладом
    if contextKey:
        session = await db.get(ContextSession, contextKey)
        if session and session.widget_name == widget_name:
            if _is_fresh(session.created_at):
                account_name = session.account_name
                # Ключ одноразовый. Документация: «Повторное использование одного
                # и того же contextKey не рекомендуется, так как в будущем может
                # быть запрещено». Гасим сразу после обмена, чтобы перехваченный
                # ключ нельзя было предъявить второй раз внутри его пяти минут.
                await db.delete(session)
                await db.commit()
            else:
                # Просроченный ключ не оставляем в базе: он больше ни на что не годен.
                await db.delete(session)
                await db.commit()
                logger.warning("contextKey %s expired for widget '%s'", _fp(contextKey), widget_name)
        else:
            logger.warning("contextKey %s not registered for widget '%s'", _fp(contextKey), widget_name)

    # 2. Спрашиваем МойСклад, чей это contextKey
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
                resolved_account_id = resp.json().get("accountId", "")
                if resolved_account_id:
                    token_row = await _token_for_account_id(db, widget_name, resolved_account_id)
                    if token_row:
                        logger.info(
                            "contextKey %s resolved via Vendor API for widget '%s'",
                            _fp(contextKey), widget_name,
                        )
                        return {"access_token": token_row.access_token, "account_name": token_row.account_name}
                    logger.warning(
                        "Vendor API resolved an account for widget '%s' but it is not installed", widget_name
                    )
            else:
                logger.warning(
                    "Vendor API returned %s for contextKey %s", resp.status_code, _fp(contextKey)
                )
        except Exception as exc:
            logger.error("Failed to call MoySklad Vendor API: %s", exc)

    # 3. Запасные пути — дыра, включается только вручную (см. модульный докстринг).
    if not account_name and settings.allow_insecure_account_fallback:
        if accountId:
            token_row = await _token_for_account_id(db, widget_name, accountId)
            if token_row:
                logger.warning(
                    "INSECURE FALLBACK: token issued by accountId for widget '%s'", widget_name
                )
                return {"access_token": token_row.access_token, "account_name": token_row.account_name}
        if account:
            logger.warning(
                "INSECURE FALLBACK: token issued by account name for widget '%s'", widget_name
            )
            account_name = account

    if not account_name:
        # Причину наружу не раскрываем: она подсказывала бы, какие имена аккаунтов
        # существуют. Подробности — в логах.
        raise HTTPException(401, "Unauthorized")

    token = await db.get(AppToken, (widget_name, account_name))
    if not token or token.status != "active" or not token.access_token:
        raise HTTPException(401, "Unauthorized")

    return {"access_token": token.access_token, "account_name": account_name}
