"""
Витрины дашборда из собственного Postgres.

  GET /{widget_name}/data/day-full?from=&to=
  GET /{widget_name}/data/counterparties
  GET /{widget_name}/data/stock
  GET /{widget_name}/data/money
  GET /{widget_name}/data/currencies
  GET /{widget_name}/data/profit-by-product?from=&to=
  GET /{widget_name}/data/cash-flow?from=&to=
  GET /{widget_name}/data/rfm?from=&to=
  GET /{widget_name}/data/receivables?windowDays=&defaultTermDays=

Авторизация — тот же access_token, что виджет получает из /{widget}/token,
в заголовке Authorization: Bearer. Ответ всегда ограничен аккаунтом этого
токена: account_id берётся из app_tokens, а не из запроса, поэтому подставить
чужой аккаунт параметром нельзя.

Формы ответов дословно повторяют то, что фронт считал сам, сходив в МойСклад —
переключение сводится к замене адреса.

Ни одна ручка не ходит в api.moysklad.ru: всё читается из базы, которую
наполняет ночной синк. Поэтому ответы быстрые и укладываются в таймаут
Cloudflare, но показывают состояние на момент последнего прогона — его отдаёт
/{widget}/data/status.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from .. import analytics
from ..auth import CallerAccount, caller_account
from ..database import get_db

router = APIRouter(tags=["Data"])

# Верхняя граница периода. Год с лишним закрывает сравнения год-к-году, а
# запрос на десять лет положил бы и базу, и браузер.
MAX_RANGE_DAYS = 800


def _parse_moment(raw: str, field: str) -> datetime:
    """Принимаем формат МоегоСклада «YYYY-MM-DD HH:MM:SS» и голую дату."""
    text = raw.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise HTTPException(400, f"{field}: ожидается YYYY-MM-DD[ HH:MM:SS], получено {raw!r}")


def _range(from_: str, to: str) -> tuple[datetime, datetime]:
    start = _parse_moment(from_, "from")
    end = _parse_moment(to, "to")
    if end < start:
        raise HTTPException(400, "to не может быть раньше from")
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(400, f"период больше {MAX_RANGE_DAYS} дней")
    return start, end


@router.get("/{widget_name}/data/day-full")
async def get_day_full(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    caller: CallerAccount = Depends(caller_account),
    db: AsyncSession = Depends(get_db),
):
    start, end = _range(from_, to)
    return await analytics.day_full(db, caller.account_id, start, end, date.today())


@router.get("/{widget_name}/data/counterparties")
async def get_counterparties(
    caller: CallerAccount = Depends(caller_account),
    db: AsyncSession = Depends(get_db),
):
    return await analytics.debt_and_credit(db, caller.account_id)


@router.get("/{widget_name}/data/stock")
async def get_stock(
    caller: CallerAccount = Depends(caller_account),
    db: AsyncSession = Depends(get_db),
):
    return await analytics.stock_summary(db, caller.account_id)


@router.get("/{widget_name}/data/money")
async def get_money(
    caller: CallerAccount = Depends(caller_account),
    db: AsyncSession = Depends(get_db),
):
    return await analytics.money_report(db, caller.account_id)


@router.get("/{widget_name}/data/currencies")
async def get_currencies(
    caller: CallerAccount = Depends(caller_account),
    db: AsyncSession = Depends(get_db),
):
    return {"rows": await analytics.currencies(db, caller.account_id)}


@router.get("/{widget_name}/data/profit-by-product")
async def get_profit_by_product(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    caller: CallerAccount = Depends(caller_account),
    db: AsyncSession = Depends(get_db),
):
    start, end = _range(from_, to)
    rows = await analytics.profit_by_product(
        db, caller.account_id, start.date(), end.date()
    )
    return {"rows": rows}


@router.get("/{widget_name}/data/cash-flow")
async def get_cash_flow(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    caller: CallerAccount = Depends(caller_account),
    db: AsyncSession = Depends(get_db),
):
    start, end = _range(from_, to)
    return await analytics.cash_flow(db, caller.account_id, start, end)


@router.get("/{widget_name}/data/rfm")
async def get_rfm(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    caller: CallerAccount = Depends(caller_account),
    db: AsyncSession = Depends(get_db),
):
    start, end = _range(from_, to)
    return await analytics.rfm(db, caller.account_id, start, end)


@router.get("/{widget_name}/data/receivables")
async def get_receivables(
    windowDays: int = Query(365, ge=1, le=MAX_RANGE_DAYS),
    defaultTermDays: int = Query(30, ge=1, le=365),
    caller: CallerAccount = Depends(caller_account),
    db: AsyncSession = Depends(get_db),
):
    return await analytics.receivables(
        db, caller.account_id, windowDays, defaultTermDays, date.today()
    )
