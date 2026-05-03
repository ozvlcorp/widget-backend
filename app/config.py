from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/widgets"
    cors_origins: list[str] = ["*"]
    # Optional: protect /admin/* endpoints with a shared secret
    # Set ADMIN_SECRET in .env; leave empty to disable auth (rely on network-level protection)
    admin_secret: str = ""

    @property
    def async_database_url(self) -> str:
        # Dokploy gives postgresql://, we need postgresql+asyncpg://
        return self.database_url.replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )

    class Config:
        env_file = ".env"


settings = Settings()
