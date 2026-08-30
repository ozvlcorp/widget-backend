"""
Фоновая синхронизация МойСклад → собственный Postgres.

Зачем: виджет читает данные из этой базы, а не ходит в api.moysklad.ru на
каждый клик. Прогон запускает планировщик (app/scheduler.py) или ручка
/admin/sync/{widget}/{account_id}.

Как устроен прогон одного аккаунта:

  1. Свежий срез справочников и остатков — они читаются целиком, история не
     нужна: контрагенты, номенклатура, склады, юрлица, статьи расходов, валюты,
     остатки, балансы контрагентов, остатки по счетам.

  2. Догоняем изменения документов «вперёд» по полю updated самого МойСклада.
     Именно updated, а не moment: документ вчерашнего дня могли поправить
     сегодня, и фильтр по дате документа такую правку не увидел бы.

  3. Заливаем историю «назад» окнами по sync_window_days дней, пока не дойдём
     до самого раннего документа аккаунта. Каждое закрытое окно двигает
     backfill_cursor, поэтому прогон можно прервать в любой момент — следующий
     продолжит с того же места, а не начнёт сначала.

Прогон ограничен по времени (sync_max_seconds). Упереться в потолок — штатный
исход для первичной заливки, а не ошибка: курсоры сохранены, ночью продолжим.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import AsyncSessionLocal
from .models import (
    AppToken,
    SyncedAssortment,
    SyncedCounterparty,
    SyncedDictionary,
    SyncedDocument,
    SyncedProfitDaily,
    SyncedStock,
    SyncRun,
    SyncState,
)
from .moysklad import (
    MoyskladClient,
    MoyskladError,
    entity_id_from_href,
    ms_datetime,
    parse_ms_datetime,
)

logger = logging.getLogger(__name__)

# Документы, которые нужны страницам приложения: выручка, деньги, заказы.
DOCUMENT_TYPES: tuple[str, ...] = (
    "demand",
    "retaildemand",
    "paymentin",
    "cashin",
    "paymentout",
    "cashout",
    "customerorder",
)

# Справочники: (kind, путь МойСклад).
DICTIONARY_SOURCES: tuple[tuple[str, str], ...] = (
    ("organization", "/entity/organization"),
    ("store", "/entity/store"),
    ("expenseitem", "/entity/expenseitem"),
    ("currency", "/entity/currency"),
)

# Небольшой запас, на который откатываем watermark перед инкрементальным
# проходом: часы сервера МойСклад и нашего не совпадают до секунды, а потерять
# документ на границе окна дороже, чем перечитать десяток.
WATERMARK_OVERLAP = timedelta(minutes=5)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Подменяется только в тестах — боевой код всегда ходит в настоящий МойСклад.
_transport_override = None


async def _upsert(db: AsyncSession, model: Any, rows: Sequence[dict]) -> int:
    """
    INSERT ... ON CONFLICT DO UPDATE пачкой.

    Диалект выбираем по подключению: на бою Postgres, в тестах SQLite — обе
    поддерживают on_conflict_do_update, но конструкторы у них разные.
    """
    if not rows:
        return 0

    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    insert = sqlite_insert if dialect == "sqlite" else pg_insert

    pk_columns = [c.name for c in model.__table__.primary_key.columns]
    updatable = [c.name for c in model.__table__.columns if c.name not in pk_columns]

    # Пачками, чтобы не упереться в лимит параметров на один запрос
    # (у Postgres это 65535, у SQLite — 999 по умолчанию).
    per_row = len(model.__table__.columns)
    chunk_size = max(1, (900 if dialect == "sqlite" else 30000) // max(per_row, 1))

    written = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start:start + chunk_size]
        stmt = insert(model).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=pk_columns,
            set_={name: getattr(stmt.excluded, name) for name in updatable},
        )
        await db.execute(stmt)
        written += len(chunk)
    return written


async def _get_state(
    db: AsyncSession, account_id: str, widget_name: str, entity: str
) -> SyncState:
    state = await db.get(SyncState, (account_id, widget_name, entity))
    if state is None:
        state = SyncState(account_id=account_id, widget_name=widget_name, entity=entity)
        db.add(state)
        await db.flush()
    return state


class Deadline:
    """Бюджет времени на прогон. Истёк — доделываем текущее окно и выходим."""

    def __init__(self, seconds: int):
        self._until = _utcnow() + timedelta(seconds=seconds)

    @property
    def expired(self) -> bool:
        return _utcnow() >= self._until


# ─── Разбор строк МойСклад ───────────────────────────────────────────────────

def _agent_of(row: dict) -> tuple[Optional[str], Optional[str]]:
    agent = row.get("agent") or {}
    meta = agent.get("meta") or {}
    return entity_id_from_href(meta.get("href")), agent.get("name")


def _document_values(account_id: str, doc_type: str, row: dict) -> Optional[dict]:
    doc_id = row.get("id")
    moment = parse_ms_datetime(row.get("moment"))
    if not doc_id or moment is None:
        # Документ без id или даты нам всё равно не пригодится — пропускаем,
        # но не роняем прогон из-за одной кривой строки.
        return None

    agent_id, agent_name = _agent_of(row)
    expense_item = row.get("expenseItem") or {}
    expense_meta = expense_item.get("meta") or {}
    rate = ((row.get("rate") or {}).get("value")) or 1.0

    return {
        "account_id": account_id,
        "doc_type": doc_type,
        "doc_id": doc_id,
        "name": row.get("name"),
        "moment": moment,
        "sum_kop": int(row.get("sum") or 0),
        "payed_kop": int(row["payedSum"]) if row.get("payedSum") is not None else None,
        "rate": float(rate),
        "applicable": bool(row.get("applicable", True)),
        "agent_id": agent_id,
        "agent_name": agent_name,
        "expense_item_id": entity_id_from_href(expense_meta.get("href")),
        "expense_item_name": expense_item.get("name"),
        "uuid_href": (row.get("meta") or {}).get("uuidHref"),
        "attributes": row.get("attributes"),
        "ms_updated": parse_ms_datetime(row.get("updated")),
    }


# ─── Синхронизация отдельных сущностей ───────────────────────────────────────

async def _sync_documents_incremental(
    db: AsyncSession,
    client: MoyskladClient,
    account_id: str,
    widget_name: str,
    doc_type: str,
    deadline: Deadline,
) -> int:
    """Догоняет всё, что изменилось с прошлого прогона, включая правки задним числом."""
    state = await _get_state(db, account_id, widget_name, f"doc:{doc_type}")

    if state.watermark is None:
        # Первый прогон: инкрементальному проходу не от чего отталкиваться —
        # историю принесёт заливка назад. Ставим точку отсчёта на сейчас.
        state.watermark = _utcnow()
        await db.commit()
        return 0

    since = state.watermark - WATERMARK_OVERLAP
    params = {
        "filter": f"updated>={ms_datetime(since)}",
        "order": "updated,asc",
        "expand": "agent",
    }

    batch: list[dict] = []
    written = 0
    newest = state.watermark

    async for row in client.paginate(f"/entity/{doc_type}", params, limit=100):
        values = _document_values(account_id, doc_type, row)
        if values is None:
            continue
        batch.append(values)
        if values["ms_updated"] and values["ms_updated"] > newest:
            newest = values["ms_updated"]
        if len(batch) >= 500:
            written += await _upsert(db, SyncedDocument, batch)
            batch.clear()
            await db.commit()
        if deadline.expired:
            break

    if batch:
        written += await _upsert(db, SyncedDocument, batch)

    state.watermark = newest
    state.last_success_at = _utcnow()
    state.rows_synced = (state.rows_synced or 0) + written
    await db.commit()
    return written


async def _earliest_moment(client: MoyskladClient, doc_type: str) -> Optional[datetime]:
    """Дата самого раннего документа — чтобы знать, где заливка истории кончается."""
    page = await client.get(
        f"/entity/{doc_type}", {"order": "moment,asc", "limit": 1, "offset": 0}
    )
    rows = page.get("rows") or []
    if not rows:
        return None
    return parse_ms_datetime(rows[0].get("moment"))


async def _sync_documents_backfill(
    db: AsyncSession,
    client: MoyskladClient,
    account_id: str,
    widget_name: str,
    doc_type: str,
    deadline: Deadline,
) -> int:
    """
    Льёт историю окнами, двигаясь из настоящего в прошлое.

    Назад, а не вперёд, намеренно: свежие месяцы попадают в базу первыми, и
    дашборд становится полезным после первой же ночи, не дожидаясь, пока
    догрузятся данные пятилетней давности.
    """
    state = await _get_state(db, account_id, widget_name, f"doc:{doc_type}")
    if state.backfill_done:
        return 0

    cursor = state.backfill_cursor or _utcnow()

    floor: Optional[datetime] = None
    if settings.sync_backfill_max_days > 0:
        floor = _utcnow() - timedelta(days=settings.sync_backfill_max_days)

    earliest = await _earliest_moment(client, doc_type)
    if earliest is None:
        # Документов такого типа в аккаунте нет вовсе — заливать нечего.
        state.backfill_done = True
        state.backfill_cursor = cursor
        await db.commit()
        return 0
    if floor is not None and earliest < floor:
        earliest = floor

    window = timedelta(days=settings.sync_window_days)
    written = 0

    while cursor > earliest and not deadline.expired:
        window_start = max(cursor - window, earliest)
        params = {
            "filter": (
                f"moment>={ms_datetime(window_start)};"
                f"moment<={ms_datetime(cursor)}"
            ),
            "order": "moment,asc",
            "expand": "agent",
        }

        batch: list[dict] = []
        async for row in client.paginate(f"/entity/{doc_type}", params, limit=100):
            values = _document_values(account_id, doc_type, row)
            if values is None:
                continue
            batch.append(values)
            if len(batch) >= 500:
                written += await _upsert(db, SyncedDocument, batch)
                batch.clear()

        if batch:
            written += await _upsert(db, SyncedDocument, batch)

        # Курсор двигаем только после того, как окно записано целиком: иначе
        # прерванный прогон оставил бы в истории дыру и никогда к ней не вернулся.
        cursor = window_start
        state.backfill_cursor = cursor
        state.rows_synced = (state.rows_synced or 0) + written
        await db.commit()

    if cursor <= earliest:
        state.backfill_done = True
        state.last_success_at = _utcnow()
        await db.commit()

    return written


async def _sync_counterparties(
    db: AsyncSession, client: MoyskladClient, account_id: str
) -> int:
    """Контрагенты + их балансы из report/counterparty одним проходом."""
    balances: dict[str, int] = {}
    async for row in client.paginate("/report/counterparty"):
        cp = row.get("counterparty") or {}
        cp_id = cp.get("id") or entity_id_from_href((cp.get("meta") or {}).get("href"))
        if cp_id:
            balances[cp_id] = int(row.get("balance") or 0)

    batch: list[dict] = []
    written = 0
    async for row in client.paginate("/entity/counterparty"):
        cp_id = row.get("id")
        if not cp_id:
            continue
        batch.append({
            "account_id": account_id,
            "cp_id": cp_id,
            "name": row.get("name"),
            "phone": row.get("phone"),
            "archived": bool(row.get("archived", False)),
            "balance_kop": balances.get(cp_id),
            "uuid_href": (row.get("meta") or {}).get("uuidHref"),
            "attributes": row.get("attributes"),
            "ms_updated": parse_ms_datetime(row.get("updated")),
        })
        if len(batch) >= 500:
            written += await _upsert(db, SyncedCounterparty, batch)
            batch.clear()

    written += await _upsert(db, SyncedCounterparty, batch)
    await db.commit()
    return written


async def _sync_assortment(
    db: AsyncSession, client: MoyskladClient, account_id: str
) -> int:
    batch: list[dict] = []
    written = 0
    async for row in client.paginate("/entity/assortment"):
        item_id = row.get("id")
        meta = row.get("meta") or {}
        if not item_id:
            continue
        batch.append({
            "account_id": account_id,
            "item_id": item_id,
            "name": row.get("name"),
            "article": (row.get("article") or "").strip() or None,
            "code": (row.get("code") or "").strip() or None,
            "uom": (row.get("uom") or {}).get("name"),
            "item_type": meta.get("type"),
            "uuid_href": meta.get("uuidHref"),
            "archived": bool(row.get("archived", False)),
            "ms_updated": parse_ms_datetime(row.get("updated")),
        })
        if len(batch) >= 500:
            written += await _upsert(db, SyncedAssortment, batch)
            batch.clear()

    written += await _upsert(db, SyncedAssortment, batch)
    await db.commit()
    return written


async def _sync_stock(db: AsyncSession, client: MoyskladClient, account_id: str) -> int:
    """
    Остатки — снимок «здесь и сейчас», истории у них нет.

    Старые строки сносим целиком: позиция, у которой остаток обнулился,
    из отчёта просто исчезает, и upsert оставил бы её в базе навсегда.
    """
    batch: list[dict] = []
    rows_seen = 0
    fresh: list[dict] = []

    async for row in client.paginate("/report/stock/all"):
        meta = row.get("meta") or {}
        assortment_id = entity_id_from_href(meta.get("href"))
        if not assortment_id:
            continue
        rows_seen += 1
        fresh.append({
            "account_id": account_id,
            "assortment_id": assortment_id,
            "store_id": "",
            "name": row.get("name"),
            "stock": float(row.get("stock") or 0),
            "reserve": float(row.get("reserve") or 0),
            "in_transit": float(row.get("inTransit") or 0),
            "quantity": float(row.get("quantity") or 0),
            "price_kop": int(row.get("price") or 0),
            "sale_price_kop": int(row.get("salePrice") or 0),
            "uuid_href": meta.get("uuidHref"),
        })

    await db.execute(delete(SyncedStock).where(SyncedStock.account_id == account_id))
    for start in range(0, len(fresh), 500):
        batch = fresh[start:start + 500]
        await _upsert(db, SyncedStock, batch)
    await db.commit()
    return rows_seen


async def _sync_dictionaries(
    db: AsyncSession, client: MoyskladClient, account_id: str
) -> int:
    written = 0
    for kind, path in DICTIONARY_SOURCES:
        batch: list[dict] = []
        async for row in client.paginate(path):
            entity_id = row.get("id")
            if not entity_id:
                continue
            batch.append({
                "account_id": account_id,
                "kind": kind,
                "entity_id": entity_id,
                "name": row.get("name"),
                "payload": row,
            })
        written += await _upsert(db, SyncedDictionary, batch)
        await db.commit()

    # Остатки по счетам — не справочник, но читается так же целиком и нужен
    # плитке «Деньги». Ключа id у строк отчёта нет, поэтому собираем свой.
    money_batch: list[dict] = []
    page = await client.get("/report/money/byaccount")
    for index, row in enumerate(page.get("rows") or []):
        account = row.get("account") or {}
        organization = row.get("organization") or {}
        money_batch.append({
            "account_id": account_id,
            "kind": "money_account",
            "entity_id": account.get("id") or f"row-{index}",
            "name": account.get("name") or organization.get("name") or "Kassa",
            "payload": {"balance": row.get("balance") or 0},
        })
    await db.execute(
        delete(SyncedDictionary).where(
            SyncedDictionary.account_id == account_id,
            SyncedDictionary.kind == "money_account",
        )
    )
    written += await _upsert(db, SyncedDictionary, money_batch)
    await db.commit()
    return written


async def _sync_profit_daily(
    db: AsyncSession,
    client: MoyskladClient,
    account_id: str,
    widget_name: str,
    deadline: Deadline,
) -> int:
    """
    report/profit/byproduct по одному дню за раз.

    Посуточно — потому что ABC считается по произвольному интервалу, который
    выбирает пользователь, и из месячных срезов такой интервал не собрать.
    Дней в полной истории много, поэтому здесь тот же курсор: идём назад,
    сколько успеем за отведённое время, остальное — следующей ночью.
    """
    state = await _get_state(db, account_id, widget_name, "profit_daily")

    today = _utcnow().date()
    written = 0

    # 1. Вчерашний и сегодняшний день пересчитываем всегда: вчерашний мог
    #    дозаполниться поздними документами, сегодняшний ещё идёт.
    recent_days = [today, today - timedelta(days=1)]
    for day in recent_days:
        written += await _sync_profit_for_day(db, client, account_id, day)
    await db.commit()

    # 2. История назад от курсора.
    if state.backfill_done:
        state.last_success_at = _utcnow()
        await db.commit()
        return written

    cursor_dt = state.backfill_cursor or _utcnow()
    cursor = cursor_dt.date()

    floor_day: Optional[date] = None
    if settings.sync_backfill_max_days > 0:
        floor_day = today - timedelta(days=settings.sync_backfill_max_days)

    earliest = await _earliest_moment(client, "demand")
    earliest_day = earliest.date() if earliest else today
    if floor_day is not None and earliest_day < floor_day:
        earliest_day = floor_day

    while cursor > earliest_day and not deadline.expired:
        cursor -= timedelta(days=1)
        written += await _sync_profit_for_day(db, client, account_id, cursor)
        state.backfill_cursor = datetime.combine(cursor, datetime.min.time())
        await db.commit()

    if cursor <= earliest_day:
        state.backfill_done = True
        state.last_success_at = _utcnow()
        await db.commit()

    return written


async def _sync_profit_for_day(
    db: AsyncSession, client: MoyskladClient, account_id: str, day: date
) -> int:
    params = {
        "momentFrom": f"{day.isoformat()} 00:00:00",
        "momentTo": f"{day.isoformat()} 23:59:59",
    }
    batch: list[dict] = []
    async for row in client.paginate("/report/profit/byproduct", params):
        assortment = row.get("assortment") or {}
        meta = assortment.get("meta") or {}
        item_id = assortment.get("id") or entity_id_from_href(meta.get("href"))
        if not item_id:
            continue
        sell_sum = int(row.get("sellSum") or 0)
        sell_cost = int(row.get("sellCost") or 0)
        profit = row.get("profit")
        batch.append({
            "account_id": account_id,
            "day": day,
            "assortment_id": item_id,
            "name": assortment.get("name"),
            "uom": (assortment.get("uom") or {}).get("name"),
            "item_type": meta.get("type"),
            "uuid_href": meta.get("uuidHref"),
            "sell_quantity": float(row.get("sellQuantity") or 0),
            "sell_sum_kop": sell_sum,
            "sell_cost_kop": sell_cost,
            "profit_kop": int(profit) if profit is not None else sell_sum - sell_cost,
        })

    # День без продаж — не повод хранить вчерашние строки за этот день.
    await db.execute(
        delete(SyncedProfitDaily).where(
            SyncedProfitDaily.account_id == account_id,
            SyncedProfitDaily.day == day,
        )
    )
    return await _upsert(db, SyncedProfitDaily, batch)


# ─── Прогон целиком ──────────────────────────────────────────────────────────

async def _claim_run(
    db: AsyncSession, account_id: str, widget_name: str, trigger: str
) -> Optional[SyncRun]:
    """
    Занимает аккаунт под синк. None = его уже синкает кто-то другой.

    Прогон, который давно не подавал признаков жизни, считаем мёртвым: иначе
    падение контейнера посреди заливки заблокировало бы аккаунт навсегда.
    """
    stale_before = _utcnow() - timedelta(minutes=settings.sync_stale_run_minutes)
    result = await db.execute(
        select(SyncRun).where(
            SyncRun.account_id == account_id,
            SyncRun.widget_name == widget_name,
            SyncRun.status == "running",
        )
    )
    for row in result.scalars():
        heartbeat = row.heartbeat_at or row.started_at
        if heartbeat and heartbeat > stale_before:
            return None
        row.status = "failed"
        row.error = "прогон не подавал heartbeat — признан мёртвым"
        row.finished_at = _utcnow()

    run = SyncRun(
        id=str(uuid.uuid4()),
        account_id=account_id,
        widget_name=widget_name,
        trigger=trigger,
        status="running",
        started_at=_utcnow(),
        heartbeat_at=_utcnow(),
    )
    db.add(run)
    await db.commit()
    return run


async def _heartbeat(run_id: str, stop: asyncio.Event) -> None:
    """
    Отмечает прогон живым, пока он идёт.

    Без этого заливка истории, которой отведено больше sync_stale_run_minutes,
    сама себя объявляла бы мёртвой, и параллельный прогон полез бы в тот же
    аккаунт.
    """
    interval = max(30, settings.sync_stale_run_minutes * 60 // 4)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        try:
            async with AsyncSessionLocal() as db:
                saved = await db.get(SyncRun, run_id)
                if saved and saved.status == "running":
                    saved.heartbeat_at = _utcnow()
                    await db.commit()
        except Exception:
            # Пульс — вспомогательный: его сбой не повод ронять сам синк.
            logger.warning("Не удалось обновить heartbeat прогона %s", run_id, exc_info=True)


async def sync_account(
    account_id: str,
    widget_name: str,
    access_token: str,
    *,
    account_name: str,
    trigger: str = "cron",
) -> dict[str, Any]:
    """Один полный прогон по аккаунту. Возвращает статистику записанных строк."""
    deadline = Deadline(settings.sync_max_seconds)
    stats: dict[str, Any] = {}

    async with AsyncSessionLocal() as db:
        run = await _claim_run(db, account_id, widget_name, trigger)
        if run is None:
            logger.info("Аккаунт %s уже синкается — пропускаем", account_id)
            return {"skipped": "already running"}

    stop_heartbeat = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat(run.id, stop_heartbeat))

    try:
        async with MoyskladClient(access_token, transport=_transport_override) as client:
            async with AsyncSessionLocal() as db:
                stats["dictionaries"] = await _sync_dictionaries(db, client, account_id)
                stats["counterparties"] = await _sync_counterparties(db, client, account_id)
                stats["assortment"] = await _sync_assortment(db, client, account_id)
                stats["stock"] = await _sync_stock(db, client, account_id)

                for doc_type in DOCUMENT_TYPES:
                    stats[f"{doc_type}:incremental"] = await _sync_documents_incremental(
                        db, client, account_id, widget_name, doc_type, deadline
                    )

                for doc_type in DOCUMENT_TYPES:
                    stats[f"{doc_type}:backfill"] = await _sync_documents_backfill(
                        db, client, account_id, widget_name, doc_type, deadline
                    )

                stats["profit_daily"] = await _sync_profit_daily(
                    db, client, account_id, widget_name, deadline
                )

        stats["hit_deadline"] = deadline.expired
        async with AsyncSessionLocal() as db:
            saved = await db.get(SyncRun, run.id)
            if saved:
                saved.status = "ok"
                saved.finished_at = _utcnow()
                saved.stats = stats
                await db.commit()
        return stats

    except MoyskladError as exc:
        if exc.status in (401, 403):
            # Токен аннулирован: решение удалили или приостановили, а колбэк
            # деактивации до нас не дошёл (или пришёл и был потерян). Гасим
            # установку, иначе воркер будет ломиться в неё каждую ночь вечно.
            logger.warning(
                "Аккаунт %s: токен отвергнут (%s) — помечаю установку неактивной",
                account_id, exc.status,
            )
            async with AsyncSessionLocal() as db:
                install = await db.get(AppToken, (widget_name, account_name))
                if install and install.status == "active":
                    install.status = "token_revoked"
                    install.deactivated_at = _utcnow()
                    install.deactivation_cause = f"HTTP {exc.status} от МойСклад"
                    install.access_token = ""
                saved = await db.get(SyncRun, run.id)
                if saved:
                    saved.status = "failed"
                    saved.finished_at = _utcnow()
                    saved.stats = stats
                    saved.error = f"токен отвергнут: HTTP {exc.status}"
                await db.commit()
            return {"skipped": f"token revoked (HTTP {exc.status})"}
        raise

    except Exception as exc:
        logger.exception("Синк аккаунта %s провалился", account_id)
        async with AsyncSessionLocal() as db:
            saved = await db.get(SyncRun, run.id)
            if saved:
                saved.status = "failed"
                saved.finished_at = _utcnow()
                saved.stats = stats
                saved.error = f"{type(exc).__name__}: {exc}"[:2000]
                await db.commit()
        raise

    finally:
        stop_heartbeat.set()
        heartbeat_task.cancel()


async def sync_all_accounts(*, trigger: str = "cron") -> dict[str, Any]:
    """
    Прогон по всем установленным аккаунтам, по одному за раз.

    Последовательно намеренно: у каждого аккаунта свой лимит МойСклад, но
    процесс и база общие, и десяток параллельных заливок съест и то и другое.
    """
    async with AsyncSessionLocal() as db:
        # Только действующие установки. У приостановленной и удалённой токен
        # аннулирован МоимСкладом — ходить по ним значит гарантированно получать
        # 401 каждую ночь.
        result = await db.execute(select(AppToken).where(AppToken.status == "active"))
        installs = [
            (row.account_id, row.widget_name, row.access_token, row.account_name)
            for row in result.scalars()
            if row.account_id and row.access_token
        ]

    summary: dict[str, Any] = {"accounts": len(installs), "ok": 0, "failed": 0, "runs": {}}
    for account_id, widget_name, token, account_name in installs:
        try:
            summary["runs"][f"{widget_name}/{account_name}"] = await sync_account(
                account_id, widget_name, token,
                account_name=account_name, trigger=trigger,
            )
            summary["ok"] += 1
        except Exception as exc:
            summary["failed"] += 1
            summary["runs"][f"{widget_name}/{account_name}"] = {"error": str(exc)}
    return summary
