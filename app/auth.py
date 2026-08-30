"""
Аутентификация запросов виджета к data-ручкам.

Фронт уже держит access_token МойСклад — тот самый, что выдала ручка
/{widget}/token. Его и присылает в Authorization: Bearer. По нему находим
строку в app_tokens и узнаём, чьи данные отдавать.

Отдельную сессионную куку или свой JWT не заводим: это была бы вторая система
учётных данных с тем же сроком жизни и той же зоной доверия, но с новым кодом,
который тоже можно ошибиться. Токен уже привязан к аккаунту — этого хватает.
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from .database import get_db
from .models import AppToken


class CallerAccount:
    """Кто спрашивает: аккаунт МойСклад и виджет, в котором его установили."""

    def __init__(self, account_id: str, widget_name: str, account_name: str, token: str):
        self.account_id = account_id
        self.widget_name = widget_name
        self.account_name = account_name
        self.access_token = token


async def caller_account(
    widget_name: str,
    authorization: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> CallerAccount:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Unauthorized")

    token = authorization[7:].strip()
    if not token:
        raise HTTPException(401, "Unauthorized")

    result = await db.execute(
        select(AppToken).where(
            AppToken.widget_name == widget_name,
            AppToken.access_token == token,
        ).limit(1)
    )
    row = result.scalar_one_or_none()
    # Причину наружу не раскрываем — она подсказывала бы, какие токены живые.
    if row is None or not row.account_id:
        raise HTTPException(401, "Unauthorized")

    return CallerAccount(row.account_id, row.widget_name, row.account_name, token)
