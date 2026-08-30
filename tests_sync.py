"""
Проверка слоя синхронизации МойСклад → Postgres.

База — SQLite во временном файле (именно файл, а не :memory:, потому что
sync_account открывает несколько сессий, и каждому подключению к :memory:
досталась бы своя пустая база).

МойСклад подменён httpx.MockTransport: сам клиент — настоящий, поэтому под
проверку попадают и пагинация, и повторы на 429, и разбор дат.

Запуск:  .venv/bin/python -m pytest tests_sync.py -q
"""
import os
import tempfile
from datetime import datetime, timedelta

_TMP = tempfile.mkdtemp(prefix="widget-backend-tests-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/test.db"
os.environ["ADMIN_SECRET"] = "test-secret"
os.environ["SYNC_ENABLED"] = "0"
os.environ["SYNC_WINDOW_DAYS"] = "30"

import httpx
import pytest
import pytest_asyncio

from app import sync as sync_module
from app.database import AsyncSessionLocal, engine, Base
from app.models import (
    AppToken, SyncedAssortment, SyncedCounterparty, SyncedDictionary,
    SyncedDocument, SyncedProfitDaily, SyncedStock, SyncRun, SyncState,
)
from sqlalchemy import delete, func, select

ACCOUNT = "acc-1"
WIDGET = "dashboard"
TOKEN = "secret-token"

NOW = datetime.utcnow()
# Заведомо в прошлом: заливка истории идёт назад от «сейчас», и документ с
# moment в будущем ни в одно окно не попал бы. Округление до часа назад делает
# тесты независимыми от времени суток, в которое их запустили.
TODAY = (NOW - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class FakeMoySklad:
    """
    Мини-МойСклад: помнит документы и отдаёт их с фильтрами по moment/updated,
    постранично, как настоящий.
    """

    def __init__(self, documents=None):
        self.documents = documents or {}      # doc_type -> list[dict]
        self.counterparties = []
        self.assortment = []
        self.stock = []
        self.profit_by_day = {}               # 'YYYY-MM-DD' -> list[dict]
        self.dictionaries = {}                # path -> list[dict]
        self.money_rows = []
        self.calls = []                       # (path, params) — для проверок
        self.fail_with_429_once = set()
        self._429_served = set()

    def transport(self):
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/api/remap/1.2", "")
        params = dict(request.url.params)
        self.calls.append((path, params))

        if path in self.fail_with_429_once and path not in self._429_served:
            self._429_served.add(path)
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})

        if path.startswith("/entity/") and path.count("/") == 2:
            entity = path.rsplit("/", 1)[-1]
            if entity == "counterparty":
                return self._page(self.counterparties, params)
            if entity == "assortment":
                return self._page(self.assortment, params)
            if entity in self.dictionaries:
                return self._page(self.dictionaries[entity], params)
            if entity in self.documents:
                return self._page(self._filter_docs(entity, params), params)
            return self._page([], params)

        if path == "/report/counterparty":
            return self._page(
                [{"counterparty": {"id": c["id"]}, "balance": c.get("balance", 0)}
                 for c in self.counterparties], params)
        if path == "/report/stock/all":
            return self._page(self.stock, params)
        if path == "/report/profit/byproduct":
            day = (params.get("momentFrom") or "")[:10]
            return self._page(self.profit_by_day.get(day, []), params)
        if path == "/report/money/byaccount":
            return httpx.Response(200, json={"rows": self.money_rows})

        return self._page([], params)

    def _filter_docs(self, entity, params):
        rows = list(self.documents.get(entity, []))
        flt = params.get("filter") or ""
        for clause in flt.split(";"):
            if clause.startswith("updated>="):
                bound = clause[len("updated>="):]
                rows = [r for r in rows if (r.get("updated") or r["moment"]) >= bound]
            elif clause.startswith("moment>="):
                bound = clause[len("moment>="):]
                rows = [r for r in rows if r["moment"] >= bound]
            elif clause.startswith("moment<="):
                bound = clause[len("moment<="):]
                rows = [r for r in rows if r["moment"] <= bound]

        order = params.get("order") or ""
        key = "updated" if order.startswith("updated") else "moment"
        rows.sort(key=lambda r: r.get(key) or r["moment"])
        return rows

    @staticmethod
    def _page(rows, params):
        limit = int(params.get("limit", 1000))
        offset = int(params.get("offset", 0))
        window = rows[offset:offset + limit]
        return httpx.Response(
            200, json={"meta": {"size": len(rows)}, "rows": window}
        )


def make_doc(doc_id, moment, total=10000, updated=None, agent=("cp-1", "ООО Ромашка")):
    return {
        "id": doc_id,
        "name": f"doc-{doc_id}",
        "moment": iso(moment),
        "updated": iso(updated or moment),
        "sum": total,
        "applicable": True,
        "rate": {"value": 1},
        "agent": {
            "name": agent[1],
            "meta": {
                "href": f"https://api.moysklad.ru/api/remap/1.2/entity/counterparty/{agent[0]}",
                "type": "counterparty",
            },
        },
        "meta": {"uuidHref": f"https://online.moysklad.ru/app/#demand/edit?id={doc_id}"},
    }


@pytest_asyncio.fixture
async def db_ready():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        for model in (SyncedDocument, SyncedCounterparty, SyncedAssortment,
                      SyncedStock, SyncedProfitDaily, SyncedDictionary,
                      SyncState, SyncRun, AppToken):
            await db.execute(delete(model))
        db.add(AppToken(widget_name=WIDGET, account_name="jamshid", app_uid="uid",
                        access_token=TOKEN, account_id=ACCOUNT))
        await db.commit()
    yield


async def run_sync(fake, **kw):
    sync_module._transport_override = fake.transport()
    kw.setdefault("account_name", "jamshid")
    try:
        return await sync_module.sync_account(ACCOUNT, WIDGET, TOKEN, **kw)
    finally:
        sync_module._transport_override = None


async def count(model, **where):
    async with AsyncSessionLocal() as db:
        stmt = select(func.count()).select_from(model)
        for key, value in where.items():
            stmt = stmt.where(getattr(model, key) == value)
        return (await db.execute(stmt)).scalar_one()


# ─── Заливка истории ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_loads_history_and_completes(db_ready):
    docs = [make_doc(f"d{i}", TODAY - timedelta(days=i * 20)) for i in range(6)]
    fake = FakeMoySklad({"demand": docs})

    await run_sync(fake)

    assert await count(SyncedDocument, doc_type="demand") == 6, "все документы залиты"

    async with AsyncSessionLocal() as db:
        state = await db.get(SyncState, (ACCOUNT, WIDGET, "doc:demand"))
    assert state.backfill_done is True, "заливка дошла до самого раннего документа"


@pytest.mark.asyncio
async def test_sync_is_idempotent(db_ready):
    docs = [make_doc(f"d{i}", TODAY - timedelta(days=i)) for i in range(5)]
    fake = FakeMoySklad({"demand": docs})

    await run_sync(fake)
    first = await count(SyncedDocument)
    await run_sync(fake)
    second = await count(SyncedDocument)

    assert first == second == 5, "повторный прогон не плодит дубли"


@pytest.mark.asyncio
async def test_document_fields_are_parsed(db_ready):
    moment = TODAY - timedelta(days=2)
    doc = make_doc("d1", moment, total=1234500)
    doc["payedSum"] = 500000
    doc["rate"] = {"value": 12.5}
    fake = FakeMoySklad({"demand": [doc]})

    await run_sync(fake)

    async with AsyncSessionLocal() as db:
        row = await db.get(SyncedDocument, (ACCOUNT, "demand", "d1"))
    assert row.sum_kop == 1234500
    assert row.payed_kop == 500000
    assert row.rate == 12.5
    assert row.agent_id == "cp-1"
    assert row.agent_name == "ООО Ромашка"
    assert row.moment == moment


# ─── Инкрементальный проход ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_incremental_picks_up_retroactive_edit(db_ready):
    """Правка старого документа должна доехать — она меняет updated, но не moment."""
    old_moment = TODAY - timedelta(days=200)
    docs = [make_doc("d1", old_moment, total=10000)]
    fake = FakeMoySklad({"demand": docs})
    await run_sync(fake)

    async with AsyncSessionLocal() as db:
        row = await db.get(SyncedDocument, (ACCOUNT, "demand", "d1"))
    assert row.sum_kop == 10000

    # Документ поправили уже после первого синка: moment прежний, updated новее
    # водяного знака, оставленного прошлым прогоном.
    docs[0]["sum"] = 99999
    docs[0]["updated"] = iso(NOW + timedelta(minutes=10))

    await run_sync(fake)

    async with AsyncSessionLocal() as db:
        row = await db.get(SyncedDocument, (ACCOUNT, "demand", "d1"))
    assert row.sum_kop == 99999, "инкрементальный проход подхватил правку задним числом"


# ─── Остатки и посуточная прибыль ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stock_snapshot_drops_vanished_rows(db_ready):
    fake = FakeMoySklad()
    href = "https://api.moysklad.ru/api/remap/1.2/entity/product/{}"
    fake.stock = [
        {"meta": {"href": href.format("p1")}, "name": "Товар 1", "stock": 5,
         "price": 1000, "salePrice": 2000},
        {"meta": {"href": href.format("p2")}, "name": "Товар 2", "stock": 3,
         "price": 1000, "salePrice": 2000},
    ]
    await run_sync(fake)
    assert await count(SyncedStock) == 2

    # У p2 остаток обнулился — из отчёта он просто исчезает.
    fake.stock = fake.stock[:1]
    await run_sync(fake)

    assert await count(SyncedStock) == 1, "позиция без остатка убрана, а не осталась навсегда"


@pytest.mark.asyncio
async def test_profit_day_is_replaced_not_accumulated(db_ready):
    day = TODAY.date().isoformat()
    fake = FakeMoySklad()
    fake.profit_by_day[day] = [
        {"assortment": {"id": "p1", "name": "Товар 1", "meta": {"type": "product"}},
         "sellQuantity": 2, "sellSum": 20000, "sellCost": 12000},
        {"assortment": {"id": "p2", "name": "Товар 2", "meta": {"type": "product"}},
         "sellQuantity": 1, "sellSum": 10000, "sellCost": 6000},
    ]
    await run_sync(fake)
    assert await count(SyncedProfitDaily) == 2

    # Документ за этот день отменили — в отчёте осталась одна позиция.
    fake.profit_by_day[day] = fake.profit_by_day[day][:1]
    await run_sync(fake)

    assert await count(SyncedProfitDaily) == 1, "день перезаписан целиком"

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(SyncedProfitDaily))).scalars().all()
    assert rows[0].profit_kop == 8000, "profit посчитан как выручка минус себестоимость"


# ─── Справочники ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dictionaries_and_counterparties(db_ready):
    fake = FakeMoySklad()
    fake.counterparties = [
        {"id": "cp-1", "name": "ООО Ромашка", "phone": "+998901112233",
         "balance": -450000, "meta": {}},
    ]
    fake.assortment = [
        {"id": "p1", "name": "Товар 1", "article": " A-1 ",
         "meta": {"type": "product"}, "uom": {"name": "шт"}},
    ]
    fake.dictionaries = {
        "organization": [{"id": "o1", "name": "ООО Моя Компания"}],
        "store": [{"id": "s1", "name": "Основной склад"}],
        "expenseitem": [{"id": "e1", "name": "Аренда"}],
        "currency": [{"id": "c1", "name": "сум", "isoCode": "UZS"}],
    }
    fake.money_rows = [{"account": {"id": "a1", "name": "Касса"}, "balance": 700000}]

    await run_sync(fake)

    async with AsyncSessionLocal() as db:
        cp = await db.get(SyncedCounterparty, (ACCOUNT, "cp-1"))
        item = await db.get(SyncedAssortment, (ACCOUNT, "p1"))
        money = await db.get(SyncedDictionary, (ACCOUNT, "money_account", "a1"))
    assert cp.balance_kop == -450000, "баланс подтянут из report/counterparty"
    assert cp.phone == "+998901112233"
    assert item.article == "A-1", "артикул очищен от пробелов"
    assert money.payload["balance"] == 700000

    assert await count(SyncedDictionary, kind="expenseitem") == 1


# ─── Блокировка и устойчивость ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_second_run_is_skipped_while_one_is_running(db_ready):
    async with AsyncSessionLocal() as db:
        db.add(SyncRun(id="run-1", account_id=ACCOUNT, widget_name=WIDGET,
                       trigger="cron", status="running",
                       started_at=datetime.utcnow(),
                       heartbeat_at=datetime.utcnow()))
        await db.commit()

    fake = FakeMoySklad({"demand": [make_doc("d1", TODAY)]})
    result = await run_sync(fake)

    assert result == {"skipped": "already running"}
    assert await count(SyncedDocument) == 0, "второй прогон не тронул данные"


@pytest.mark.asyncio
async def test_stale_run_is_reclaimed(db_ready):
    """Контейнер упал посреди синка — аккаунт не должен остаться заблокированным."""
    long_ago = datetime.utcnow() - timedelta(hours=6)
    async with AsyncSessionLocal() as db:
        db.add(SyncRun(id="run-dead", account_id=ACCOUNT, widget_name=WIDGET,
                       trigger="cron", status="running",
                       started_at=long_ago, heartbeat_at=long_ago))
        await db.commit()

    fake = FakeMoySklad({"demand": [make_doc("d1", TODAY)]})
    await run_sync(fake)

    assert await count(SyncedDocument) == 1, "мёртвый прогон отпустил аккаунт"
    async with AsyncSessionLocal() as db:
        dead = await db.get(SyncRun, "run-dead")
    assert dead.status == "failed"


@pytest.mark.asyncio
async def test_429_is_retried(db_ready):
    fake = FakeMoySklad({"demand": [make_doc("d1", TODAY)]})
    fake.fail_with_429_once.add("/entity/demand")

    await run_sync(fake)

    assert await count(SyncedDocument) == 1, "запрос повторён после 429"


@pytest.mark.asyncio
async def test_malformed_row_does_not_break_the_run(db_ready):
    good = make_doc("d1", TODAY)
    broken = {"id": "d2", "moment": "не дата", "sum": 100}
    no_id = {"moment": iso(TODAY), "sum": 100}
    fake = FakeMoySklad({"demand": [good, broken, no_id]})

    await run_sync(fake)

    assert await count(SyncedDocument) == 1, "кривые строки пропущены, прогон дошёл до конца"


@pytest.mark.asyncio
async def test_run_is_recorded_with_stats(db_ready):
    fake = FakeMoySklad({"demand": [make_doc("d1", TODAY)]})
    await run_sync(fake, trigger="manual")

    async with AsyncSessionLocal() as db:
        runs = (await db.execute(select(SyncRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].status == "ok"
    assert runs[0].trigger == "manual"
    assert runs[0].finished_at is not None
    assert isinstance(runs[0].stats, dict)


# ─── HTTP-ручки: авторизация и наблюдаемость ─────────────────────────────────

import httpx as _httpx
from app.main import app as fastapi_app

OTHER_TOKEN = "other-secret-token"
OTHER_ACCOUNT = "acc-2"


def _client():
    return _httpx.AsyncClient(
        transport=_httpx.ASGITransport(app=fastapi_app),
        base_url="http://test",
    )


@pytest_asyncio.fixture
async def two_accounts(db_ready):
    async with AsyncSessionLocal() as db:
        db.add(AppToken(widget_name=WIDGET, account_name="other", app_uid="uid",
                        access_token=OTHER_TOKEN, account_id=OTHER_ACCOUNT))
        db.add(SyncState(account_id=ACCOUNT, widget_name=WIDGET, entity="doc:demand",
                         backfill_done=True, last_success_at=datetime.utcnow()))
        db.add(SyncState(account_id=OTHER_ACCOUNT, widget_name=WIDGET,
                         entity="doc:demand", backfill_done=False))
        await db.commit()
    yield


@pytest.mark.asyncio
async def test_data_status_requires_bearer_token(two_accounts):
    async with _client() as c:
        assert (await c.get(f"/{WIDGET}/data/status")).status_code == 401
        r = await c.get(f"/{WIDGET}/data/status",
                        headers={"Authorization": "Bearer no-such-token"})
        assert r.status_code == 401
        assert TOKEN not in r.text, "чужой токен наружу не утекает"


@pytest.mark.asyncio
async def test_data_status_is_scoped_to_the_calling_account(two_accounts):
    async with _client() as c:
        mine = await c.get(f"/{WIDGET}/data/status",
                           headers={"Authorization": f"Bearer {TOKEN}"})
        theirs = await c.get(f"/{WIDGET}/data/status",
                             headers={"Authorization": f"Bearer {OTHER_TOKEN}"})

    assert mine.status_code == theirs.status_code == 200
    assert mine.json()["account_id"] == ACCOUNT
    assert theirs.json()["account_id"] == OTHER_ACCOUNT
    assert mine.json()["backfill_in_progress"] is False
    assert theirs.json()["backfill_in_progress"] is True, "долив истории виден интерфейсу"
    assert len(mine.json()["entities"]) == 1, "чужие сущности в ответ не попали"


@pytest.mark.asyncio
async def test_admin_sync_requires_secret(two_accounts):
    async with _client() as c:
        assert (await c.get("/admin/sync/status")).status_code == 401
        r = await c.get("/admin/sync/status",
                        headers={"X-Admin-Secret": "wrong-secret"})
        assert r.status_code == 401
        r = await c.get("/admin/sync/status",
                        headers={"X-Admin-Secret": "test-secret"})
        assert r.status_code == 200
        assert r.json()["scheduler"]["cron"] == "30 2 * * *"


@pytest.mark.asyncio
async def test_manual_sync_rejects_unknown_account(two_accounts):
    async with _client() as c:
        r = await c.post(f"/admin/sync/{WIDGET}/acc-does-not-exist",
                         headers={"X-Admin-Secret": "test-secret"})
    assert r.status_code == 404


# ─── Жизненный цикл установки: Suspend / Uninstall / Resume ──────────────────
#
# Требования Vendor API 1.0, раздел «Деактивация решения на аккаунте»:
# Suspend и Uninstall приходят одним DELETE и обрабатываются по-разному,
# запросы идемпотентны, при Resume возвращаем Activated.

VENDOR_PATH = f"/{WIDGET}/api/moysklad/vendor/1.0/apps/APP-ID/{ACCOUNT}"


def _activation(cause, token=TOKEN, with_access=True):
    body = {"appUid": "uid", "accountName": "jamshid", "cause": cause}
    if with_access:
        body["access"] = [{"resource": "https://api.moysklad.ru/api/remap/1.2",
                           "scope": ["admin"], "access_token": token}]
    return body


async def _install_row():
    async with AsyncSessionLocal() as db:
        return await db.get(AppToken, (WIDGET, "jamshid"))


@pytest.mark.asyncio
async def test_suspend_keeps_the_install_row(db_ready):
    async with _client() as c:
        r = await c.request("DELETE", VENDOR_PATH,
                            json={"accountName": "jamshid", "cause": "Suspend"})
    assert r.status_code == 200
    assert r.content == b"", "тело ответа по документации пустое"

    row = await _install_row()
    assert row is not None, "при Suspend конфигурацию установки удалять нельзя"
    assert row.status == "suspended"
    assert row.access_token == "", "аннулированный токен не храним"
    assert row.deactivation_cause == "Suspend"


@pytest.mark.asyncio
async def test_uninstall_also_keeps_settings_for_reinstall(db_ready):
    async with _client() as c:
        r = await c.request("DELETE", VENDOR_PATH,
                            json={"accountName": "jamshid", "cause": "Uninstall"})
    assert r.status_code == 200

    row = await _install_row()
    assert row is not None, "данные установки рекомендуется сохранять для переустановки"
    assert row.status == "uninstalled"
    assert row.access_token == ""


@pytest.mark.asyncio
async def test_deactivation_is_idempotent(db_ready):
    async with _client() as c:
        first = await c.request("DELETE", VENDOR_PATH,
                                json={"accountName": "jamshid", "cause": "Suspend"})
        second = await c.request("DELETE", VENDOR_PATH,
                                 json={"accountName": "jamshid", "cause": "Suspend"})
        never = await c.request("DELETE", VENDOR_PATH,
                                json={"accountName": "no-such-account", "cause": "Uninstall"})
    assert first.status_code == 200
    assert second.status_code == 204, "повтор уже обработанного удаления → 204"
    assert never.status_code == 204, "аккаунт никогда не был установлен → 204"


@pytest.mark.asyncio
async def test_resume_reactivates_with_activated_status(db_ready):
    async with _client() as c:
        await c.request("DELETE", VENDOR_PATH,
                        json={"accountName": "jamshid", "cause": "Suspend"})
        r = await c.put(VENDOR_PATH, json=_activation("Resume", token="new-token"))

    assert r.status_code == 200
    assert r.json() == {"status": "Activated"}, "решение продолжает работу с сохранёнными настройками"

    row = await _install_row()
    assert row.status == "active"
    assert row.access_token == "new-token"
    assert row.deactivated_at is None


@pytest.mark.asyncio
async def test_tariff_change_without_access_block_does_not_fail(db_ready):
    """
    При TariffChanged и Autoprolongation блок access не приходит. Ответ 4xx
    перевёл бы решение в ActivationFailed, то есть смена тарифа у клиента
    ломала бы установку.
    """
    async with _client() as c:
        r = await c.put(VENDOR_PATH, json=_activation("TariffChanged", with_access=False))

    assert r.status_code == 200, f"смена тарифа не должна валить активацию: {r.text}"
    assert r.json() == {"status": "Activated"}

    row = await _install_row()
    assert row.access_token == TOKEN, "прежний токен остался действующим"


@pytest.mark.asyncio
async def test_suspended_install_gets_no_token_and_no_data(db_ready):
    async with _client() as c:
        await c.put(f"/{WIDGET}/api/moysklad/vendor/1.0/context/CK-1",
                    json={"accountName": "jamshid"})
        await c.request("DELETE", VENDOR_PATH,
                        json={"accountName": "jamshid", "cause": "Suspend"})

        by_context = await c.get(f"/{WIDGET}/token", params={"contextKey": "CK-1"})
        by_bearer = await c.get(f"/{WIDGET}/data/status",
                                headers={"Authorization": f"Bearer {TOKEN}"})

    assert by_context.status_code == 401
    assert by_bearer.status_code == 401, "по приостановленной установке данные не отдаём"


@pytest.mark.asyncio
async def test_context_key_is_single_use(db_ready):
    async with _client() as c:
        await c.put(f"/{WIDGET}/api/moysklad/vendor/1.0/context/CK-2",
                    json={"accountName": "jamshid"})
        first = await c.get(f"/{WIDGET}/token", params={"contextKey": "CK-2"})
        second = await c.get(f"/{WIDGET}/token", params={"contextKey": "CK-2"})

    assert first.status_code == 200
    assert first.json()["access_token"] == TOKEN
    assert second.status_code == 401, "ключ гасится сразу после обмена"


# ─── Синк и аннулированный токен ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_skips_non_active_installs(db_ready):
    async with AsyncSessionLocal() as db:
        row = await db.get(AppToken, (WIDGET, "jamshid"))
        row.status = "suspended"
        row.access_token = ""
        await db.commit()

    fake = FakeMoySklad({"demand": [make_doc("d1", TODAY)]})
    sync_module._transport_override = fake.transport()
    try:
        summary = await sync_module.sync_all_accounts()
    finally:
        sync_module._transport_override = None

    assert summary["accounts"] == 0, "приостановленную установку воркер не трогает"
    assert await count(SyncedDocument) == 0


@pytest.mark.asyncio
async def test_revoked_token_disables_the_install_instead_of_failing_forever(db_ready):
    """
    Токен аннулируется при удалении и приостановке решения. Если колбэк
    деактивации до нас не дошёл, синк не должен ломиться в этот аккаунт каждую
    ночь — установку надо погасить.
    """
    class Revoked(FakeMoySklad):
        def _handle(self, request):
            return _httpx.Response(401, json={"errors": [{"error": "Unauthorized"}]})

    result = await run_sync(Revoked())

    assert "skipped" in result, f"401 не должен выбрасывать исключение: {result}"
    row = await _install_row()
    assert row.status == "token_revoked"
    assert row.access_token == ""

    # И на следующую ночь этот аккаунт уже не берётся в работу.
    summary = await sync_module.sync_all_accounts()
    assert summary["accounts"] == 0
