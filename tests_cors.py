"""
CORS: наши поддомены должны работать без ручного перечисления.

Воспроизводит боевой случай. В Dokploy у сервиса стоял
CORS_ORIGINS=["https://interfood.oymoysklad.com"], виджет открывался с
https://mml.oymoysklad.com, и предварительный запрос браузера получал
400 «Disallowed CORS origin». Наружу это выглядело не как ошибка, а как
«ничего не изменилось»: фронт молча уходил на прямой путь в МойСклад.

Запуск:  .venv/bin/python -m pytest tests_cors.py -q
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings

WIDGET_ORIGIN = "https://mml.oymoysklad.com"


def build_app(allow_origins):
    """Ровно та же обвязка, что в app/main.py — иначе тест проверял бы не то."""
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_origin_regex=settings.cors_origin_regex or None,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


async def preflight(app, origin):
    """То, что реально шлёт браузер перед GET с заголовком Authorization."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        return await c.options("/health", headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        })


@pytest.mark.asyncio
async def test_own_subdomain_allowed_even_when_not_listed():
    """Именно этот случай и сломался на бою."""
    app = build_app(["https://interfood.oymoysklad.com"])
    r = await preflight(app, WIDGET_ORIGIN)

    assert r.status_code == 200, f"ожидали 200, получили {r.status_code}: {r.text}"
    assert r.headers.get("access-control-allow-origin") == WIDGET_ORIGIN
    assert "authorization" in r.headers.get("access-control-allow-headers", "").lower()


@pytest.mark.asyncio
async def test_new_subdomain_needs_no_configuration():
    """Новый виджет на новом поддомене должен работать сразу после деплоя."""
    app = build_app([])
    r = await preflight(app, "https://novyj-vidzhet.oymoysklad.com")
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://novyj-vidzhet.oymoysklad.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [
    "https://evil.com",
    # Суффиксная подделка: строка начинается как наша, но продолжается чужим
    # доменом. Сверка идёт по fullmatch, поэтому не проходит.
    "https://mml.oymoysklad.com.attacker.com",
    "https://oymoysklad.com.evil.io",
    # Схема тоже часть сверки.
    "http://mml.oymoysklad.com",
])
async def test_foreign_origins_still_rejected(origin):
    app = build_app([])
    r = await preflight(app, origin)
    assert r.headers.get("access-control-allow-origin") != origin, (
        f"чужой origin не должен разрешаться: {origin}"
    )


@pytest.mark.asyncio
async def test_explicit_list_still_works():
    """Правило добавляется к CORS_ORIGINS, а не подменяет его."""
    app = build_app(["https://partner.example.com"])
    r = await preflight(app, "https://partner.example.com")
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://partner.example.com"
