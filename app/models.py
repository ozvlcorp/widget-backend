from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Float, Index, JSON, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB

from .database import Base

# На Postgres — jsonb (компактнее и индексируется), на SQLite в тестах — обычный
# JSON. Выбрано сразу: перевод json→jsonb на заполненной таблице потребовал бы
# ALTER ... USING с перезаписью всех строк.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")


class WidgetConfig(Base):
    """Per-widget configuration — one row per widget app registered on this backend."""
    __tablename__ = "widget_configs"

    widget_name = Column(String, primary_key=True)
    app_secret  = Column(String, nullable=False)   # MoySklad app secret for Vendor API calls
    app_uid     = Column(String, nullable=True)    # MoySklad appUid (optional, for reference)
    created_at  = Column(DateTime, server_default=func.now())
    updated_at  = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AppToken(Base):
    """Stores MoySklad access tokens per widget + account."""
    __tablename__ = "app_tokens"

    widget_name = Column(String, primary_key=True)
    account_name = Column(String, primary_key=True)
    app_uid = Column(String, nullable=False)
    access_token = Column(String, nullable=False)
    account_id = Column(String, nullable=True, index=True)  # MoySklad account UUID
    installed_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ContextSession(Base):
    """
    Short-lived mapping: contextKey → (widget_name, account_name).
    MoySklad generates a contextKey and passes it to the iframe URL.
    The frontend sends it here to get the access_token for its session.
    """
    __tablename__ = "context_sessions"

    context_key = Column(String, primary_key=True)
    widget_name = Column(String, nullable=False)
    account_name = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


# ─────────────────────────────────────────────────────────────────────────────
# Зеркало данных МойСклад в собственном Postgres.
#
# Всё, что ниже, наполняет ночной синк (app/sync.py). Приложение читает эти
# таблицы вместо того, чтобы ходить в api.moysklad.ru на каждый клик.
#
# Арендатор везде — account_id (UUID аккаунта МойСклад). Он же в app_tokens,
# так что по токену из запроса всегда понятно, чьи строки отдавать.
#
# Тип JSON, а не JSONB: на Postgres SQLAlchemy разворачивает его в jsonb, а в
# тестах на SQLite он тоже работает. Суммы храним в копейках целыми числами —
# как их отдаёт МойСклад, без промежуточных float.
# ─────────────────────────────────────────────────────────────────────────────

class SyncedDocument(Base):
    """
    Документы продаж и денег: demand, retaildemand, paymentin, cashin,
    paymentout, cashout, customerorder — в одной таблице с полем doc_type.

    Приложение почти всегда смотрит на них вместе (отгрузки + розница = выручка,
    приходы + приходные ордера = поступления), поэтому раздельные таблицы дали бы
    только UNION-ы на каждый запрос.
    """
    __tablename__ = "ms_documents"

    account_id = Column(String, primary_key=True)
    doc_type   = Column(String, primary_key=True)
    doc_id     = Column(String, primary_key=True)

    name       = Column(String, nullable=True)
    moment     = Column(DateTime, nullable=False, index=True)
    sum_kop    = Column(BigInteger, nullable=False, default=0)
    payed_kop  = Column(BigInteger, nullable=True)
    rate       = Column(Float, nullable=False, default=1.0)
    applicable = Column(Boolean, nullable=False, default=True)

    agent_id   = Column(String, nullable=True, index=True)
    agent_name = Column(String, nullable=True)

    expense_item_id   = Column(String, nullable=True)
    expense_item_name = Column(String, nullable=True)

    uuid_href  = Column(String, nullable=True)
    attributes = Column(JSONColumn, nullable=True)

    # Поле updated самого МойСклада — по нему инкрементальный синк понимает,
    # что документ правили задним числом.
    ms_updated = Column(DateTime, nullable=True, index=True)
    synced_at  = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_ms_documents_acc_type_moment", "account_id", "doc_type", "moment"),
        Index("ix_ms_documents_acc_agent_moment", "account_id", "agent_id", "moment"),
    )


class SyncedCounterparty(Base):
    """Контрагенты: имя, телефон, кастомные атрибуты и баланс из report/counterparty."""
    __tablename__ = "ms_counterparties"

    account_id   = Column(String, primary_key=True)
    cp_id        = Column(String, primary_key=True)
    name         = Column(String, nullable=True)
    phone        = Column(String, nullable=True)
    archived     = Column(Boolean, nullable=False, default=False)
    balance_kop  = Column(BigInteger, nullable=True)
    uuid_href    = Column(String, nullable=True)
    attributes   = Column(JSONColumn, nullable=True)
    ms_updated   = Column(DateTime, nullable=True)
    synced_at    = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SyncedAssortment(Base):
    """Номенклатура — товары, модификации, серии, услуги."""
    __tablename__ = "ms_assortment"

    account_id = Column(String, primary_key=True)
    item_id    = Column(String, primary_key=True)
    name       = Column(String, nullable=True)
    article    = Column(String, nullable=True)
    code       = Column(String, nullable=True)
    uom        = Column(String, nullable=True)
    item_type  = Column(String, nullable=True)
    uuid_href  = Column(String, nullable=True)
    archived   = Column(Boolean, nullable=False, default=False)
    ms_updated = Column(DateTime, nullable=True)
    synced_at  = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SyncedStock(Base):
    """
    Снимок остатков. store_id пустой строкой = совокупно по всем складам
    (NULL в первичном ключе Postgres не работает как значение).
    """
    __tablename__ = "ms_stock"

    account_id     = Column(String, primary_key=True)
    assortment_id  = Column(String, primary_key=True)
    store_id       = Column(String, primary_key=True, default="")
    name           = Column(String, nullable=True)
    stock          = Column(Float, nullable=False, default=0)
    reserve        = Column(Float, nullable=False, default=0)
    in_transit     = Column(Float, nullable=False, default=0)
    quantity       = Column(Float, nullable=False, default=0)
    price_kop      = Column(BigInteger, nullable=True)
    sale_price_kop = Column(BigInteger, nullable=True)
    uuid_href      = Column(String, nullable=True)
    synced_at      = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SyncedProfitDaily(Base):
    """
    report/profit/byproduct, разложенный по дням.

    Посуточная гранулярность нужна, чтобы ABC считался по произвольному
    интервалу, который выберет пользователь: месячные срезы такого не позволяют.
    """
    __tablename__ = "ms_profit_daily"

    account_id    = Column(String, primary_key=True)
    day           = Column(Date, primary_key=True)
    assortment_id = Column(String, primary_key=True)

    name          = Column(String, nullable=True)
    uom           = Column(String, nullable=True)
    item_type     = Column(String, nullable=True)
    uuid_href     = Column(String, nullable=True)
    sell_quantity = Column(Float, nullable=False, default=0)
    sell_sum_kop  = Column(BigInteger, nullable=False, default=0)
    sell_cost_kop = Column(BigInteger, nullable=False, default=0)
    profit_kop    = Column(BigInteger, nullable=False, default=0)
    synced_at     = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_ms_profit_daily_acc_day", "account_id", "day"),
    )


class SyncedDictionary(Base):
    """
    Мелкие справочники одной таблицей: organization, store, expenseitem,
    currency, а также срез report/money/byaccount. Заводить под каждый по
    таблице смысла нет — читаются они целиком и всегда по kind.
    """
    __tablename__ = "ms_dictionaries"

    account_id = Column(String, primary_key=True)
    kind       = Column(String, primary_key=True)
    entity_id  = Column(String, primary_key=True)
    name       = Column(String, nullable=True)
    payload    = Column(JSONColumn, nullable=True)
    synced_at  = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SyncState(Base):
    """
    Где остановился синк по каждой сущности каждого аккаунта.

    watermark — верхняя граница уже загруженного: для документов это значение
    updated последнего увиденного документа, для посуточных отчётов — последний
    закрытый день. Следующий прогон стартует отсюда, а не с нуля.
    """
    __tablename__ = "ms_sync_state"

    account_id  = Column(String, primary_key=True)
    widget_name = Column(String, primary_key=True)
    entity      = Column(String, primary_key=True)

    watermark        = Column(DateTime, nullable=True)
    backfill_done    = Column(Boolean, nullable=False, default=False)
    # Докуда назад уже дозалита история. Пока backfill_done=False, воркер
    # продолжает шагать отсюда в прошлое.
    backfill_cursor  = Column(DateTime, nullable=True)
    last_success_at  = Column(DateTime, nullable=True)
    last_error       = Column(Text, nullable=True)
    rows_synced      = Column(BigInteger, nullable=False, default=0)
    updated_at       = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SyncRun(Base):
    """
    Журнал прогонов — и одновременно блокировка: строка со status='running'
    не даёт второму инстансу начать синк того же аккаунта.
    """
    __tablename__ = "ms_sync_runs"

    id          = Column(String, primary_key=True)
    account_id  = Column(String, nullable=False, index=True)
    widget_name = Column(String, nullable=False)
    trigger     = Column(String, nullable=False)          # cron | manual | install
    status      = Column(String, nullable=False)          # running | ok | failed
    started_at  = Column(DateTime, server_default=func.now())
    finished_at = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    stats       = Column(JSONColumn, nullable=True)
    error       = Column(Text, nullable=True)
