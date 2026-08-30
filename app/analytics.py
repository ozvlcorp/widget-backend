"""
Витрины дашборда, посчитанные из собственного Postgres.

Формы ответов повторяют то, что фронт до сих пор считал сам, сходив в МойСклад:
DayStats, DemandRow, CashFlow, RfmResult, ReceivablesPnl и прочее из
src/api/moysklad.ts виджета. Это сделано намеренно — так переключение фронта
на свой бэкенд сводится к замене адреса, а не к переписыванию страниц, и цифры
до и после переключения обязаны совпадать.

Денежные величины хранятся в копейках в валюте документа. База считается как
sum_kop * rate и делится на 100 ровно один раз, в самом конце — как и во
фронте, чтобы не набегала ошибка округления.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    SyncedAssortment,
    SyncedCounterparty,
    SyncedDictionary,
    SyncedDocument,
    SyncedProfitDaily,
    SyncedStock,
)

# Атрибут «Telegram ID» на контрагенте. Значение зашито во фронте виджета,
# держим то же самое, чтобы кнопка напоминания продолжала работать.
TELEGRAM_ATTR_ID = "8666aeb7-192b-11f1-0a80-00f20005e1af"

SALE_TYPES = ("demand", "retaildemand")
PAYMENT_TYPES = ("paymentin", "cashin")
OUTFLOW_TYPES = ("paymentout", "cashout")

AGING_ORDER = ("current", "d1_7", "d8_30", "d31_90", "d90plus")

# Как во фронте: у розничной продажи покупателя обычно нет.
ANON_RETAIL_NAME = "Chakana mijoz"
NO_AGENT_NAME = "—"


def ms_moment(value: datetime) -> str:
    """Фронт ждёт «YYYY-MM-DD HH:MM:SS», а не ISO с T."""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _base_kop(row: SyncedDocument) -> int:
    return round((row.sum_kop or 0) * (row.rate or 1.0))


def _agent_name(row: SyncedDocument) -> str:
    if row.agent_name:
        return row.agent_name
    return ANON_RETAIL_NAME if row.doc_type == "retaildemand" else NO_AGENT_NAME


async def _documents(
    db: AsyncSession,
    account_id: str,
    doc_types: Sequence[str],
    start: datetime,
    end: datetime,
) -> list[SyncedDocument]:
    """Проведённые документы нужных типов за период. applicable=false не в счёт —
    так же, как фильтр `applicable=true` во фронте."""
    result = await db.execute(
        select(SyncedDocument).where(
            SyncedDocument.account_id == account_id,
            SyncedDocument.doc_type.in_(doc_types),
            SyncedDocument.moment >= start,
            SyncedDocument.moment <= end,
            SyncedDocument.applicable.is_(True),
        ).order_by(SyncedDocument.moment)
    )
    return list(result.scalars())


# ─── Дашборд и день ──────────────────────────────────────────────────────────

def _row(row: SyncedDocument, kind: str) -> dict[str, Any]:
    return {
        "id": row.doc_id,
        "moment": ms_moment(row.moment),
        "sum": row.sum_kop or 0,
        "rate": row.rate or 1.0,
        "agent": {"name": _agent_name(row)},
        "meta": {"uuidHref": row.uuid_href},
        "type": kind,
    }


def _stats(docs: Iterable[SyncedDocument]) -> dict[str, Any]:
    demand_k = retail_k = payment_k = order_k = 0
    demand_n = retail_n = order_n = 0

    for row in docs:
        value = _base_kop(row)
        if row.doc_type == "demand":
            demand_k += value
            demand_n += 1
        elif row.doc_type == "retaildemand":
            retail_k += value
            retail_n += 1
        elif row.doc_type in PAYMENT_TYPES:
            payment_k += value
        elif row.doc_type == "customerorder":
            order_k += value
            order_n += 1

    return {
        "count": demand_n + retail_n,
        "demandCount": demand_n,
        "retailCount": retail_n,
        "orderSum": (demand_k + retail_k) / 100,
        "demandSum": demand_k / 100,
        "retailSum": retail_k / 100,
        "paymentSum": payment_k / 100,
        "customerOrderCount": order_n,
        "customerOrderSum": order_k / 100,
    }


def _split_rows(docs: Iterable[SyncedDocument]) -> dict[str, list[dict[str, Any]]]:
    sales, payments, orders = [], [], []
    for row in docs:
        if row.doc_type in SALE_TYPES:
            sales.append(_row(row, row.doc_type))
        elif row.doc_type in PAYMENT_TYPES:
            payments.append(_row(row, row.doc_type))
        elif row.doc_type == "customerorder":
            orders.append(_row(row, "customerorder"))
    # Платежи и заказы фронт показывает свежими сверху.
    payments.sort(key=lambda r: r["moment"], reverse=True)
    orders.sort(key=lambda r: r["moment"], reverse=True)
    return {"demands": sales, "payments": payments, "orders": orders}


async def day_full(
    db: AsyncSession, account_id: str, start: datetime, end: datetime, today: date
) -> dict[str, Any]:
    """Ровно то, что фронт получал от getDayFull: сводка за период плюс готовые
    срезы за сегодня и вчера, если они попадают в период."""
    wanted = SALE_TYPES + PAYMENT_TYPES + ("customerorder",)
    docs = await _documents(db, account_id, wanted, start, end)

    yesterday = today - timedelta(days=1)

    def for_day(day: date) -> list[SyncedDocument]:
        return [d for d in docs if d.moment.date() == day]

    in_range = lambda d: start.date() <= d <= end.date()

    payload: dict[str, Any] = {
        "stats": _stats(docs),
        **_split_rows(docs),
        "todayStats": None,
        "todayRows": None,
        "yesterdayStats": None,
        "yesterdayRows": None,
    }

    if in_range(today):
        rows = for_day(today)
        payload["todayStats"] = _stats(rows)
        payload["todayRows"] = _split_rows(rows)
    if in_range(yesterday):
        rows = for_day(yesterday)
        payload["yesterdayStats"] = _stats(rows)
        payload["yesterdayRows"] = _split_rows(rows)

    return payload


# ─── Контрагенты: долги и кредиты ────────────────────────────────────────────

async def debt_and_credit(db: AsyncSession, account_id: str) -> dict[str, Any]:
    result = await db.execute(
        select(SyncedCounterparty).where(
            SyncedCounterparty.account_id == account_id,
            SyncedCounterparty.archived.is_(False),
        )
    )

    debtor_n = debtor_k = creditor_n = creditor_k = 0
    rows: list[dict[str, Any]] = []

    for cp in result.scalars():
        balance = cp.balance_kop or 0
        if balance == 0:
            continue
        # Отрицательный баланс = должны нам, положительный = должны мы.
        if balance < 0:
            debtor_n += 1
            debtor_k += abs(balance)
        else:
            creditor_n += 1
            creditor_k += balance

        telegram = None
        for attr in (cp.attributes or []):
            if attr.get("id") == TELEGRAM_ATTR_ID and attr.get("value"):
                telegram = str(attr["value"])
                break

        rows.append({
            "id": cp.cp_id,
            "name": cp.name,
            "balance": balance,
            "uuidHref": cp.uuid_href,
            "telegramChatId": telegram,
            "phone": cp.phone,
        })

    return {
        "debtors": {"count": debtor_n, "sum": debtor_k / 100},
        "creditors": {"count": creditor_n, "sum": creditor_k / 100},
        "rows": rows,
    }


# ─── Остатки, деньги, валюты ─────────────────────────────────────────────────

async def stock_summary(db: AsyncSession, account_id: str) -> dict[str, Any]:
    # Код и единицу измерения отчёт по остаткам не отдаёт — они в номенклатуре,
    # поэтому подтягиваем её слева: позиция без карточки в ассортименте всё
    # равно должна попасть в остатки.
    result = await db.execute(
        select(SyncedStock, SyncedAssortment)
        .outerjoin(
            SyncedAssortment,
            (SyncedAssortment.account_id == SyncedStock.account_id)
            & (SyncedAssortment.item_id == SyncedStock.assortment_id),
        )
        .where(SyncedStock.account_id == account_id)
    )
    rows: list[dict[str, Any]] = []
    total_quantity = 0.0
    total_value_kop = 0

    for stock, item in result.all():
        quantity = stock.stock or 0
        price = stock.price_kop or 0
        rows.append({
            "name": stock.name or (item.name if item else None),
            "code": item.code if item else None,
            "uom": item.uom if item else None,
            "uuidHref": stock.uuid_href or (item.uuid_href if item else None),
            "quantity": quantity,
            "price": price,
            "salePrice": stock.sale_price_kop or 0,
            "sum": round(quantity * price),
        })
        total_quantity += quantity
        total_value_kop += round(quantity * price)

    rows.sort(key=lambda r: r["sum"], reverse=True)
    return {
        "productCount": len(rows),
        "totalQuantity": total_quantity,
        "totalValue": total_value_kop / 100,
        "rows": rows,
    }


async def money_report(db: AsyncSession, account_id: str) -> dict[str, Any]:
    result = await db.execute(
        select(SyncedDictionary).where(
            SyncedDictionary.account_id == account_id,
            SyncedDictionary.kind == "money_account",
        )
    )
    accounts = []
    for row in result.scalars():
        balance = (row.payload or {}).get("balance") or 0
        if balance == 0:
            continue
        accounts.append({"name": row.name or "Kassa", "balance": balance / 100})
    return {"total": sum(a["balance"] for a in accounts), "accounts": accounts}


async def currencies(db: AsyncSession, account_id: str) -> list[dict[str, Any]]:
    result = await db.execute(
        select(SyncedDictionary).where(
            SyncedDictionary.account_id == account_id,
            SyncedDictionary.kind == "currency",
        )
    )
    out = []
    for row in result.scalars():
        payload = row.payload or {}
        rate = payload.get("rate") or 1
        multiplicity = payload.get("multiplicity") or 1
        out.append({
            "isoCode": payload.get("isoCode"),
            "symbol": payload.get("symbol"),
            "name": payload.get("name") or row.name,
            "rate": rate / multiplicity,
            "isDefault": bool(payload.get("default", False)),
        })
    return out


# ─── Прибыль по товарам (ABC) ────────────────────────────────────────────────

async def profit_by_product(
    db: AsyncSession, account_id: str, start: date, end: date
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(SyncedProfitDaily).where(
            SyncedProfitDaily.account_id == account_id,
            SyncedProfitDaily.day >= start,
            SyncedProfitDaily.day <= end,
        )
    )

    agg: dict[str, dict[str, Any]] = {}
    for row in result.scalars():
        item = agg.setdefault(row.assortment_id, {
            "name": row.name or NO_AGENT_NAME,
            "uom": row.uom or "шт",
            "uuidHref": row.uuid_href,
            "assortmentId": row.assortment_id,
            "assortmentType": row.item_type,
            "quantity": 0.0,
            "revenue_kop": 0,
            "profit_kop": 0,
        })
        item["quantity"] += row.sell_quantity or 0
        item["revenue_kop"] += row.sell_sum_kop or 0
        item["profit_kop"] += row.profit_kop or 0
        # Имя могло смениться — берём последнее непустое.
        if row.name:
            item["name"] = row.name

    out = []
    for item in agg.values():
        revenue = item.pop("revenue_kop") / 100
        profit = item.pop("profit_kop") / 100
        if item["quantity"] <= 0 and revenue <= 0:
            continue
        out.append({
            **item,
            "revenue": revenue,
            "profit": profit,
            "margin": (profit / revenue) if revenue > 0 else 0,
        })
    out.sort(key=lambda r: r["revenue"], reverse=True)
    return out


# ─── Движение денег ──────────────────────────────────────────────────────────

async def cash_flow(
    db: AsyncSession, account_id: str, start: datetime, end: datetime
) -> dict[str, Any]:
    docs = await _documents(
        db, account_id, PAYMENT_TYPES + OUTFLOW_TYPES, start, end
    )

    by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"in": 0, "out": 0})
    total_in = total_out = 0
    in_count = out_count = 0
    by_item: dict[str, int] = defaultdict(int)

    for row in docs:
        value = _base_kop(row)
        day = row.moment.strftime("%Y-%m-%d")
        if row.doc_type in PAYMENT_TYPES:
            by_day[day]["in"] += value
            total_in += value
            in_count += 1
        else:
            by_day[day]["out"] += value
            total_out += value
            out_count += 1
            by_item[row.expense_item_name or NO_AGENT_NAME] += value

    days = [
        {"date": day, "in": v["in"] / 100, "out": v["out"] / 100,
         "net": (v["in"] - v["out"]) / 100}
        for day, v in sorted(by_day.items())
    ]
    expense_items = sorted(
        ({"name": name, "sum": value / 100} for name, value in by_item.items()),
        key=lambda r: r["sum"], reverse=True,
    )

    return {
        "days": days,
        "totalIn": total_in / 100,
        "totalOut": total_out / 100,
        "net": (total_in - total_out) / 100,
        "inCount": in_count,
        "outCount": out_count,
        "expenseItems": expense_items,
    }


# ─── RFM ─────────────────────────────────────────────────────────────────────

def _quintile_scorer(values: list[float], higher_is_better: bool):
    """Тот же расчёт, что во фронте: границы по позициям в отсортированном ряду."""
    ordered = sorted(values)
    n = len(ordered)

    def at(p: float) -> float:
        return ordered[min(n - 1, max(0, int(p * n)))]

    breaks = [at(0.2), at(0.4), at(0.6), at(0.8)]

    def score(v: float) -> int:
        s = 1
        if v >= breaks[0]:
            s = 2
        if v >= breaks[1]:
            s = 3
        if v >= breaks[2]:
            s = 4
        if v >= breaks[3]:
            s = 5
        return s if higher_is_better else 6 - s

    return score


def _segment_for(r: int, f: int, m: int) -> str:
    fm = (f + m) / 2
    if r >= 4 and fm >= 4:
        return "champions"
    if r >= 3 and fm >= 3:
        return "loyal"
    if r >= 4 and f <= 2:
        return "new"
    if r >= 3 and fm <= 2:
        return "potential"
    if r <= 2 and fm >= 4:
        return "atRisk"
    if r <= 2 and fm >= 3:
        return "attention"
    if r <= 2 and fm <= 2:
        return "lost"
    return "hibernating"


async def rfm(
    db: AsyncSession, account_id: str, start: datetime, end: datetime
) -> dict[str, Any]:
    docs = await _documents(db, account_id, SALE_TYPES, start, end)

    agg: dict[str, dict[str, Any]] = {}
    for row in docs:
        # Анонимную розницу в RFM не берём — сегментировать некого.
        if not row.agent_id or not row.agent_name:
            continue
        cur = agg.setdefault(row.agent_id, {
            "id": row.agent_id, "name": row.agent_name,
            "uuidHref": None, "frequency": 0, "monetary_kop": 0,
            "last": row.moment,
        })
        cur["frequency"] += 1
        cur["monetary_kop"] += _base_kop(row)
        if row.moment > cur["last"]:
            cur["last"] = row.moment

    if not agg:
        return {"customers": [], "totalMonetary": 0}

    base = [{
        "id": c["id"],
        "name": c["name"],
        "uuidHref": c["uuidHref"],
        "recencyDays": max(0, round((end - c["last"]).total_seconds() / 86400)),
        "frequency": c["frequency"],
        "monetary": c["monetary_kop"] / 100,
        "lastDate": ms_moment(c["last"]),
    } for c in agg.values()]

    r_score = _quintile_scorer([c["recencyDays"] for c in base], False)
    f_score = _quintile_scorer([c["frequency"] for c in base], True)
    m_score = _quintile_scorer([c["monetary"] for c in base], True)

    customers = []
    for c in base:
        r = r_score(c["recencyDays"])
        f = f_score(c["frequency"])
        m = m_score(c["monetary"])
        customers.append({**c, "r": r, "f": f, "m": m, "segment": _segment_for(r, f, m)})

    customers.sort(key=lambda c: c["monetary"], reverse=True)
    return {
        "customers": customers,
        "totalMonetary": sum(c["monetary"] for c in customers),
    }


# ─── Дебиторка ───────────────────────────────────────────────────────────────

def _term_days(attributes: Optional[list]) -> Optional[int]:
    """Срок оплаты из кастомного атрибута, имя которого содержит «срок»."""
    for attr in (attributes or []):
        name = attr.get("name") or ""
        if "срок" in name.lower():
            try:
                value = float(attr.get("value"))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return round(value)
    return None


def _bucket_for(overdue_days: int) -> str:
    if overdue_days <= 0:
        return "current"
    if overdue_days <= 7:
        return "d1_7"
    if overdue_days <= 30:
        return "d8_30"
    if overdue_days <= 90:
        return "d31_90"
    return "d90plus"


async def receivables(
    db: AsyncSession,
    account_id: str,
    window_days: int,
    default_term_days: int,
    today: date,
) -> dict[str, Any]:
    start = datetime.combine(today - timedelta(days=window_days), datetime.min.time())
    end = datetime.combine(today, datetime.max.time())
    docs = await _documents(db, account_id, ("demand",), start, end)

    # Срок оплаты может быть задан на контрагенте, а не на документе.
    cp_terms: dict[str, int] = {}
    result = await db.execute(
        select(SyncedCounterparty).where(SyncedCounterparty.account_id == account_id)
    )
    for cp in result.scalars():
        term = _term_days(cp.attributes)
        if term is not None:
            cp_terms[cp.cp_id] = term

    accrued_k = collected_k = 0
    open_docs: list[dict[str, Any]] = []
    debtors: set[str] = set()

    for row in docs:
        rate = row.rate or 1.0
        sum_k = round((row.sum_kop or 0) * rate)
        payed_k = round((row.payed_kop or 0) * rate)
        accrued_k += sum_k
        collected_k += payed_k

        remaining_k = sum_k - payed_k
        if remaining_k <= 0:
            continue

        term = _term_days(row.attributes)
        if term is None and row.agent_id:
            term = cp_terms.get(row.agent_id)
        if term is None:
            term = default_term_days

        due = row.moment.date() + timedelta(days=term)
        overdue = (today - due).days

        debtors.add(row.agent_id or row.agent_name or "")
        open_docs.append({
            "id": row.doc_id,
            "docName": row.name or NO_AGENT_NAME,
            "agentName": _agent_name(row),
            "moment": ms_moment(row.moment),
            "sum": sum_k / 100,
            "payed": payed_k / 100,
            "remaining": remaining_k / 100,
            "termDays": term,
            "overdueDays": overdue,
            "bucket": _bucket_for(overdue),
            "status": "partial" if payed_k > 0 else "unpaid",
            "uuidHref": row.uuid_href,
        })

    open_docs.sort(key=lambda d: d["overdueDays"], reverse=True)
    buckets = [
        {
            "key": key,
            "count": sum(1 for d in open_docs if d["bucket"] == key),
            "sum": sum(d["remaining"] for d in open_docs if d["bucket"] == key),
        }
        for key in AGING_ORDER
    ]

    return {
        "accrued": accrued_k / 100,
        "collected": collected_k / 100,
        "receivable": (accrued_k - collected_k) / 100,
        "docCount": len(open_docs),
        "debtorCount": len(debtors),
        "buckets": buckets,
        "docs": open_docs,
    }
