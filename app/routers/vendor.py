"""
MoySklad Vendor API endpoints — namespaced by widget.

Descriptor endpointBase for each widget:
  https://widget-backend-oymoysklad.com/{widget_name}

MoySklad calls:
  PUT    /{widget_name}/api/moysklad/vendor/1.0/apps/{appId}/{accountId}
  DELETE /{widget_name}/api/moysklad/vendor/1.0/apps/{appId}/{accountId}

Жизненный цикл установки описан в Vendor API 1.0, раздел «Деактивация решения
на аккаунте». Существенное для этого файла:

  * Приостановка и удаление приходят ОДНИМ И ТЕМ ЖЕ DELETE, различаются полем
    cause (Suspend / Uninstall), и обрабатывать их требуется по-разному:
    при Suspend настройки и конфигурацию установки удалять нельзя, при
    Uninstall их рекомендуется сохранить для переустановки.
  * Оба запроса должны быть идемпотентными — МойСклад их повторяет и дублирует.
  * При активации с cause=Resume нужно вернуть Activated, если решение может
    продолжить работу с ранее сохранёнными настройками.
  * Для cause=TariffChanged и Autoprolongation блок access НЕ ПРИХОДИТ.
    Отвечать на это ошибкой нельзя: 4xx переводит решение в ActivationFailed.
  * Токен аннулируется МоимСкладом в момент деактивации — на нашей стороне он
    после этого бесполезен, поэтому очищаем его, а не храним мёртвым.
"""
import hashlib
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select
from pydantic import BaseModel
from typing import Optional

from ..database import get_db
from ..models import AppToken, ContextSession

logger = logging.getLogger(__name__)
router = APIRouter(tags=["MoySklad Vendor API"])

# Причины деактивации из документации Vendor API.
CAUSE_SUSPEND = "Suspend"
CAUSE_UNINSTALL = "Uninstall"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AccessItem(BaseModel):
    resource: str
    scope: list[str]
    access_token: str


class ActivationRequest(BaseModel):
    appUid: str
    accountName: str
    # Отсутствует при TariffChanged и Autoprolongation — там менять нечего,
    # приходит только описание подписки.
    access: Optional[list[AccessItem]] = None
    cause: Optional[str] = None
    subscription: Optional[dict] = None


class DeactivationRequest(BaseModel):
    appUid: Optional[str] = None
    accountName: str
    cause: Optional[str] = None


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
    access_token = body.access[0].access_token if body.access else None
    existing = await db.get(AppToken, (widget_name, body.accountName))

    if access_token is None and existing is None:
        # Нечего активировать и нечем: без токена и без прошлой установки
        # решение работать не сможет.
        raise HTTPException(400, "No access token provided")

    if existing:
        # Смена тарифа и автопродление приходят без access — прошлый токен
        # остаётся действующим, перетирать его на None нельзя.
        if access_token:
            existing.access_token = access_token
        existing.app_uid = body.appUid
        existing.account_id = account_id
        existing.status = "active"
        existing.deactivated_at = None
        existing.deactivation_cause = None
    else:
        db.add(AppToken(
            widget_name=widget_name,
            account_name=body.accountName,
            app_uid=body.appUid,
            access_token=access_token,
            account_id=account_id,
            status="active",
        ))

    await db.commit()

    logger.info(
        "Активация: widget='%s' account='%s' cause=%s",
        widget_name, body.accountName, body.cause,
    )
    # Настроек, которые пользователь обязан задать руками, у решения нет:
    # дашборд работает сразу после установки. Поэтому SettingsRequired не
    # возвращаем ни при Install, ни при Resume.
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
            AppToken.status == "active",
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
    cause = body.cause or CAUSE_UNINSTALL
    install = await db.get(AppToken, (widget_name, body.accountName))

    # Идемпотентность: МойСклад повторяет и дублирует эти запросы. 204 — «нечего
    # отключать», и повтор уже обработанного удаления сюда же и попадает.
    if install is None or install.status != "active":
        return Response(status_code=204)

    install.status = "suspended" if cause == CAUSE_SUSPEND else "uninstalled"
    install.deactivated_at = _utcnow()
    install.deactivation_cause = cause
    # МойСклад аннулировал токен ещё до этого запроса — хранить его нет смысла,
    # а как учётные данные он лежать не должен.
    install.access_token = ""

    # Контексты живут минуты и без токена бесполезны — выбрасываем в обоих
    # случаях, к «конфигурации установки» они не относятся.
    await db.execute(
        delete(ContextSession).where(
            ContextSession.widget_name == widget_name,
            ContextSession.account_name == body.accountName,
        )
    )
    await db.commit()

    logger.info(
        "Деактивация: widget='%s' account='%s' cause=%s — строка установки сохранена",
        widget_name, body.accountName, cause,
    )
    # Тело ответа по документации пустое.
    return Response(status_code=200)


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
