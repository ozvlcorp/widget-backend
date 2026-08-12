from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/widgets"
    cors_origins: list[str] = ["*"]

    # Защита /admin/*. Пусто = админские ручки ОТКЛЮЧЕНЫ (503), а не открыты.
    # Раньше пустое значение молча снимало проверку (`if settings.admin_secret
    # and ...`), и любой мог перезаписать app_secret любого виджета.
    admin_secret: str = ""

    # Сколько живёт contextKey. МойСклад выдаёт его на одно открытие виджета;
    # без срока запись оставалась валидной вечно, и ключ месячной давности
    # по-прежнему менялся на токен.
    context_key_ttl_seconds: int = 300

    # АВАРИЙНЫЙ переключатель: выдавать токен по одному accountId или имени
    # аккаунта, без проверки contextKey. Это дыра — кто знает имя аккаунта, тот
    # получает полный доступ к данным МойСклада. Нужен только чтобы пережить
    # окно, пока приложение не зарегистрировано в кабинете вендора.
    allow_insecure_account_fallback: bool = False

    @property
    def async_database_url(self) -> str:
        # Dokploy gives postgresql://, we need postgresql+asyncpg://
        return self.database_url.replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )

    class Config:
        env_file = ".env"


settings = Settings()
