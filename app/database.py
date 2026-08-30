from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from .config import settings

engine = create_async_engine(settings.async_database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name != "postgresql":
            # Синтаксис ниже — постгресовый. На SQLite (тесты) create_all уже
            # создал всё нужное, доводить нечего.
            return
        # Add account_id column if it doesn't exist yet (safe to run on every startup)
        await conn.execute(text(
            "ALTER TABLE app_tokens ADD COLUMN IF NOT EXISTS account_id VARCHAR"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_app_tokens_account_id ON app_tokens (account_id)"
        ))
        # Токен — то, по чему data-ручки опознают аккаунт: без индекса каждый
        # запрос виджета читал бы таблицу целиком.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_app_tokens_access_token "
            "ON app_tokens (access_token)"
        ))
