"""
Витрины дашборда из Postgres: проверяем, что цифры совпадают с тем, что фронт
считал сам, сходив в МойСклад, и что каждый аккаунт видит только своё.

Запуск:  .venv/bin/python -m pytest tests_data_api.py -q
"""
from datetime import date, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.database import AsyncSessionLocal, Base, engine
from app.main import app as fastapi_app
from app.models import (
    AppToken, SyncedAssortment, SyncedCounterparty, SyncedDictionary,
    SyncedDocument, SyncedProfitDaily, SyncedStock, SyncRun, SyncState,
)

WIDGET = "dashboard"
ACC_A, TOKEN_A = "acc-a", "token-a"
ACC_B, TOKEN_B = "acc-b", "token-b"

TODAY = date.today()
NOON = datetime.combine(TODAY, datetime.min.time()) + timedelta(hours=12)


def client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test"
    )


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def doc(account, doc_type, doc_id, moment, sum_kop, **kw):
    return SyncedDocument(
        account_id=account, doc_type=doc_type, doc_id=doc_id, moment=moment,
        name=kw.get("name", doc_id), sum_kop=sum_kop,
        payed_kop=kw.get("payed_kop"), rate=kw.get("rate", 1.0),
        applicable=kw.get("applicable", True),
        agent_id=kw.get("agent_id"), agent_name=kw.get("agent_name"),
        expense_item_name=kw.get("expense_item_name"),
        attributes=kw.get("attributes"),
        uuid_href=kw.get("uuid_href", f"https://online.moysklad.ru/{doc_id}"),
    )


@pytest_asyncio.fixture
async def seeded():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        for model in (SyncedDocument, SyncedCounterparty, SyncedAssortment,
                      SyncedStock, SyncedProfitDaily, SyncedDictionary,
                      SyncState, SyncRun, AppToken):
            await db.execute(delete(model))
        db.add_all([
            AppToken(widget_name=WIDGET, account_name="a", app_uid="u",
                     access_token=TOKEN_A, account_id=ACC_A, status="active"),
            AppToken(widget_name=WIDGET, account_name="b", app_uid="u",
                     access_token=TOKEN_B, account_id=ACC_B, status="active"),
        ])
        await db.commit()
    yield


async def add(*rows):
    async with AsyncSessionLocal() as db:
        db.add_all(rows)
        await db.commit()


# ─── Авторизация и изоляция ──────────────────────────────────────────────────

ALL_ENDPOINTS = [
    ("/data/day-full", {"from": "2026-01-01", "to": "2026-01-31"}),
    ("/data/counterparties", {}),
    ("/data/stock", {}),
    ("/data/money", {}),
    ("/data/currencies", {}),
    ("/data/profit-by-product", {"from": "2026-01-01", "to": "2026-01-31"}),
    ("/data/cash-flow", {"from": "2026-01-01", "to": "2026-01-31"}),
    ("/data/rfm", {"from": "2026-01-01", "to": "2026-01-31"}),
    ("/data/receivables", {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,params", ALL_ENDPOINTS)
async def test_every_endpoint_requires_a_valid_token(seeded, path, params):
    async with client() as c:
        anon = await c.get(f"/{WIDGET}{path}", params=params)
        bad = await c.get(f"/{WIDGET}{path}", params=params,
                          headers=auth("not-a-real-token"))
    assert anon.status_code == 401, path
    assert bad.status_code == 401, path


@pytest.mark.asyncio
async def test_accounts_cannot_see_each_others_documents(seeded):
    await add(
        doc(ACC_A, "demand", "a1", NOON, 100_000, agent_id="cp1", agent_name="Клиент A"),
        doc(ACC_B, "demand", "b1", NOON, 999_000, agent_id="cp9", agent_name="Клиент B"),
    )
    rng = {"from": str(TODAY), "to": f"{TODAY} 23:59:59"}
    async with client() as c:
        a = (await c.get(f"/{WIDGET}/data/day-full", params=rng, headers=auth(TOKEN_A))).json()
        b = (await c.get(f"/{WIDGET}/data/day-full", params=rng, headers=auth(TOKEN_B))).json()

    assert a["stats"]["demandSum"] == 1000.0
    assert b["stats"]["demandSum"] == 9990.0
    assert [d["id"] for d in a["demands"]] == ["a1"]
    assert [d["id"] for d in b["demands"]] == ["b1"]


# ─── day-full ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_day_full_totals_counts_and_row_shape(seeded):
    yesterday_noon = NOON - timedelta(days=1)
    await add(
        doc(ACC_A, "demand", "d1", NOON, 250_000, agent_id="cp1", agent_name="ООО Ромашка"),
        doc(ACC_A, "retaildemand", "r1", NOON, 50_000),           # без контрагента
        doc(ACC_A, "paymentin", "p1", NOON, 120_000, agent_name="ООО Ромашка"),
        doc(ACC_A, "cashin", "c1", NOON, 30_000),
        doc(ACC_A, "customerorder", "o1", NOON, 400_000, agent_name="ООО Ромашка"),
        doc(ACC_A, "demand", "d0", yesterday_noon, 100_000, agent_name="Вчерашний"),
    )
    async with client() as c:
        r = await c.get(f"/{WIDGET}/data/day-full", headers=auth(TOKEN_A),
                        params={"from": str(TODAY - timedelta(days=3)),
                                "to": f"{TODAY} 23:59:59"})
    body = r.json()
    stats = body["stats"]

    assert stats["demandCount"] == 2 and stats["retailCount"] == 1
    assert stats["count"] == 3
    assert stats["demandSum"] == 3500.0      # 250000 + 100000 копеек
    assert stats["retailSum"] == 500.0
    assert stats["orderSum"] == 4000.0
    assert stats["paymentSum"] == 1500.0     # paymentin + cashin
    assert stats["customerOrderCount"] == 1
    assert stats["customerOrderSum"] == 4000.0

    # Сегодня и вчера отделены, раз попадают в период.
    assert body["todayStats"]["demandSum"] == 2500.0
    assert body["yesterdayStats"]["demandSum"] == 1000.0

    row = next(d for d in body["demands"] if d["id"] == "r1")
    assert row["type"] == "retaildemand"
    assert row["agent"]["name"] == "Chakana mijoz", "розница без контрагента"
    assert row["sum"] == 50_000, "суммы строк остаются в копейках, как во фронте"
    assert row["moment"].count("-") == 2 and " " in row["moment"], "формат YYYY-MM-DD HH:MM:SS"


@pytest.mark.asyncio
async def test_day_full_applies_currency_rate_and_skips_unapplicable(seeded):
    await add(
        doc(ACC_A, "demand", "usd", NOON, 100_00, rate=12500.0),  # 100.00 у.е.
        doc(ACC_A, "demand", "draft", NOON, 777_000, applicable=False),
    )
    async with client() as c:
        body = (await c.get(f"/{WIDGET}/data/day-full", headers=auth(TOKEN_A),
                            params={"from": str(TODAY), "to": f"{TODAY} 23:59:59"})).json()

    assert body["stats"]["demandCount"] == 1, "непроведённый документ не считается"
    assert body["stats"]["demandSum"] == 1_250_000.0, "сумма пересчитана по курсу"


@pytest.mark.asyncio
async def test_day_full_rejects_a_bad_range(seeded):
    async with client() as c:
        bad_date = await c.get(f"/{WIDGET}/data/day-full", headers=auth(TOKEN_A),
                               params={"from": "вчера", "to": str(TODAY)})
        reversed_ = await c.get(f"/{WIDGET}/data/day-full", headers=auth(TOKEN_A),
                                params={"from": str(TODAY), "to": "2020-01-01"})
        too_wide = await c.get(f"/{WIDGET}/data/day-full", headers=auth(TOKEN_A),
                               params={"from": "2000-01-01", "to": str(TODAY)})
    assert bad_date.status_code == 400
    assert reversed_.status_code == 400
    assert too_wide.status_code == 400


# ─── Контрагенты ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_counterparties_split_debtors_from_creditors(seeded):
    await add(
        SyncedCounterparty(account_id=ACC_A, cp_id="cp1", name="Должник",
                           balance_kop=-450_000, phone="+998901112233",
                           attributes=[{"id": "8666aeb7-192b-11f1-0a80-00f20005e1af",
                                        "value": "12345"}]),
        SyncedCounterparty(account_id=ACC_A, cp_id="cp2", name="Кредитор",
                           balance_kop=200_000),
        SyncedCounterparty(account_id=ACC_A, cp_id="cp3", name="Ноль", balance_kop=0),
        SyncedCounterparty(account_id=ACC_A, cp_id="cp4", name="В архиве",
                           balance_kop=-999_000, archived=True),
    )
    async with client() as c:
        body = (await c.get(f"/{WIDGET}/data/counterparties", headers=auth(TOKEN_A))).json()

    assert body["debtors"] == {"count": 1, "sum": 4500.0}
    assert body["creditors"] == {"count": 1, "sum": 2000.0}
    assert [r["id"] for r in body["rows"]] == ["cp1", "cp2"], "нулевые и архивные не в списке"
    debtor = body["rows"][0]
    assert debtor["telegramChatId"] == "12345"
    assert debtor["phone"] == "+998901112233"


# ─── Остатки, деньги, валюты ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stock_joins_assortment_for_code_and_uom(seeded):
    await add(
        SyncedStock(account_id=ACC_A, assortment_id="p1", store_id="", name="Товар 1",
                    stock=3, price_kop=100_000, sale_price_kop=150_000),
        SyncedStock(account_id=ACC_A, assortment_id="p2", store_id="", name="Без карточки",
                    stock=1, price_kop=50_000, sale_price_kop=60_000),
        SyncedAssortment(account_id=ACC_A, item_id="p1", name="Товар 1",
                         code="ART-1", uom="шт"),
    )
    async with client() as c:
        body = (await c.get(f"/{WIDGET}/data/stock", headers=auth(TOKEN_A))).json()

    assert body["productCount"] == 2
    assert body["totalQuantity"] == 4
    assert body["totalValue"] == 3500.0        # (3*100000 + 1*50000)/100
    first = body["rows"][0]
    assert first["name"] == "Товар 1" and first["code"] == "ART-1" and first["uom"] == "шт"
    assert first["sum"] == 300_000
    assert body["rows"][1]["code"] is None, "позиция без карточки всё равно в остатках"


@pytest.mark.asyncio
async def test_money_and_currencies(seeded):
    await add(
        SyncedDictionary(account_id=ACC_A, kind="money_account", entity_id="a1",
                         name="Касса", payload={"balance": 700_000}),
        SyncedDictionary(account_id=ACC_A, kind="money_account", entity_id="a2",
                         name="Пустой счёт", payload={"balance": 0}),
        SyncedDictionary(account_id=ACC_A, kind="currency", entity_id="c1", name="сум",
                         payload={"isoCode": "UZS", "symbol": "сўм", "name": "сум",
                                  "rate": 1, "multiplicity": 1, "default": True}),
        SyncedDictionary(account_id=ACC_A, kind="currency", entity_id="c2", name="доллар",
                         payload={"isoCode": "USD", "symbol": "$", "name": "доллар",
                                  "rate": 12500, "multiplicity": 1, "default": False}),
    )
    async with client() as c:
        money = (await c.get(f"/{WIDGET}/data/money", headers=auth(TOKEN_A))).json()
        cur = (await c.get(f"/{WIDGET}/data/currencies", headers=auth(TOKEN_A))).json()

    assert money["total"] == 7000.0
    assert [a["name"] for a in money["accounts"]] == ["Касса"], "нулевые счёта не показываем"
    usd = next(c for c in cur["rows"] if c["isoCode"] == "USD")
    assert usd["rate"] == 12500 and usd["isDefault"] is False


# ─── ABC ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_profit_by_product_aggregates_days(seeded):
    await add(
        SyncedProfitDaily(account_id=ACC_A, day=TODAY, assortment_id="p1", name="Товар 1",
                          uom="шт", item_type="product", sell_quantity=2,
                          sell_sum_kop=200_000, sell_cost_kop=120_000, profit_kop=80_000),
        SyncedProfitDaily(account_id=ACC_A, day=TODAY - timedelta(days=1),
                          assortment_id="p1", name="Товар 1", uom="шт", item_type="product",
                          sell_quantity=3, sell_sum_kop=300_000, sell_cost_kop=180_000,
                          profit_kop=120_000),
        SyncedProfitDaily(account_id=ACC_A, day=TODAY, assortment_id="p2", name="Товар 2",
                          uom="шт", item_type="product", sell_quantity=1,
                          sell_sum_kop=50_000, sell_cost_kop=40_000, profit_kop=10_000),
    )
    async with client() as c:
        rows = (await c.get(f"/{WIDGET}/data/profit-by-product", headers=auth(TOKEN_A),
                            params={"from": str(TODAY - timedelta(days=7)),
                                    "to": str(TODAY)})).json()["rows"]

    top = rows[0]
    assert top["assortmentId"] == "p1", "сортировка по выручке"
    assert top["quantity"] == 5
    assert top["revenue"] == 5000.0        # два дня сложились
    assert top["profit"] == 2000.0
    assert top["margin"] == pytest.approx(0.4)


# ─── ДДС ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cash_flow_groups_by_day_and_expense_item(seeded):
    yesterday = NOON - timedelta(days=1)
    await add(
        doc(ACC_A, "paymentin", "in1", yesterday, 500_000),
        doc(ACC_A, "cashin", "in2", NOON, 100_000),
        doc(ACC_A, "paymentout", "out1", NOON, 200_000, expense_item_name="Аренда"),
        doc(ACC_A, "cashout", "out2", NOON, 50_000, expense_item_name="Аренда"),
        doc(ACC_A, "cashout", "out3", NOON, 30_000),   # без статьи
    )
    async with client() as c:
        body = (await c.get(f"/{WIDGET}/data/cash-flow", headers=auth(TOKEN_A),
                            params={"from": str(TODAY - timedelta(days=3)),
                                    "to": f"{TODAY} 23:59:59"})).json()

    assert body["totalIn"] == 6000.0
    assert body["totalOut"] == 2800.0
    assert body["net"] == 3200.0
    assert body["inCount"] == 2 and body["outCount"] == 3
    assert [d["date"] for d in body["days"]] == sorted(d["date"] for d in body["days"])
    assert body["expenseItems"][0] == {"name": "Аренда", "sum": 2500.0}
    assert any(i["name"] == "—" for i in body["expenseItems"]), "расход без статьи не теряется"


# ─── RFM ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rfm_scores_segments_and_skips_anonymous_retail(seeded):
    rows = []
    # Пять клиентов с разной частотой и суммой, чтобы квинтили были осмысленными.
    for i in range(1, 6):
        for n in range(i):
            rows.append(doc(ACC_A, "demand", f"d{i}-{n}",
                            NOON - timedelta(days=(6 - i)), 100_000 * i,
                            agent_id=f"cp{i}", agent_name=f"Клиент {i}"))
    rows.append(doc(ACC_A, "retaildemand", "anon", NOON, 10_000))  # без контрагента
    await add(*rows)

    async with client() as c:
        body = (await c.get(f"/{WIDGET}/data/rfm", headers=auth(TOKEN_A),
                            params={"from": str(TODAY - timedelta(days=30)),
                                    "to": f"{TODAY} 23:59:59"})).json()

    ids = [c["id"] for c in body["customers"]]
    assert "cp5" == ids[0], "самый крупный клиент первым"
    assert len(body["customers"]) == 5, "анонимная розница в RFM не попадает"

    best = body["customers"][0]
    assert best["frequency"] == 5
    assert best["monetary"] == 25000.0        # 5 документов по 500000 копеек
    assert 1 <= best["r"] <= 5 and 1 <= best["f"] <= 5 and 1 <= best["m"] <= 5
    assert best["segment"] == "champions"
    assert body["totalMonetary"] == sum(c["monetary"] for c in body["customers"])


# ─── Дебиторка ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_receivables_terms_buckets_and_status(seeded):
    await add(
        SyncedCounterparty(account_id=ACC_A, cp_id="cp1", name="Клиент 1",
                           attributes=[{"name": "Срок оплаты", "value": 10}]),
        # Срок на документе перебивает срок контрагента.
        doc(ACC_A, "demand", "doc-own-term", NOON - timedelta(days=20), 100_000,
            payed_kop=0, agent_id="cp1", agent_name="Клиент 1",
            attributes=[{"name": "Срок оплаты", "value": 5}]),
        # Срок берётся с контрагента.
        doc(ACC_A, "demand", "doc-cp-term", NOON - timedelta(days=12), 200_000,
            payed_kop=50_000, agent_id="cp1", agent_name="Клиент 1"),
        # Срока нет нигде — по умолчанию.
        doc(ACC_A, "demand", "doc-default", NOON - timedelta(days=1), 300_000,
            payed_kop=0, agent_id="cp2", agent_name="Клиент 2"),
        # Полностью оплачен — в открытых не участвует.
        doc(ACC_A, "demand", "doc-paid", NOON - timedelta(days=3), 400_000,
            payed_kop=400_000, agent_id="cp2", agent_name="Клиент 2"),
    )
    async with client() as c:
        body = (await c.get(f"/{WIDGET}/data/receivables", headers=auth(TOKEN_A),
                            params={"windowDays": 365, "defaultTermDays": 30})).json()

    assert body["accrued"] == 10000.0                 # все четыре документа
    assert body["collected"] == 4500.0                # 50000 + 400000 копеек
    assert body["receivable"] == 5500.0
    assert body["docCount"] == 3, "оплаченный документ в открытых не числится"
    assert body["debtorCount"] == 2

    by_id = {d["id"]: d for d in body["docs"]}
    assert by_id["doc-own-term"]["termDays"] == 5, "срок с документа важнее срока клиента"
    assert by_id["doc-cp-term"]["termDays"] == 10, "срок подтянут с контрагента"
    assert by_id["doc-default"]["termDays"] == 30, "иначе значение по умолчанию"

    assert by_id["doc-cp-term"]["status"] == "partial"
    assert by_id["doc-own-term"]["status"] == "unpaid"
    assert by_id["doc-own-term"]["remaining"] == 1000.0
    assert by_id["doc-default"]["bucket"] == "current", "срок ещё не подошёл"
    assert by_id["doc-own-term"]["overdueDays"] == 15   # 20 дней назад, срок 5

    assert [b["key"] for b in body["buckets"]] == \
        ["current", "d1_7", "d8_30", "d31_90", "d90plus"]
    assert sum(b["sum"] for b in body["buckets"]) == pytest.approx(
        sum(d["remaining"] for d in body["docs"]))
    assert body["docs"][0]["overdueDays"] >= body["docs"][-1]["overdueDays"]
