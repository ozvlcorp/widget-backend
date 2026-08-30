"""
Серверный клиент JSON API 1.2 МойСклад.

Отличия от браузерного клиента виджета:
  * лимит параллельных запросов держится на каждый токен отдельно — аккаунтов
    много, а МойСклад считает «не более 5 одновременных» по токену;
  * 429 переживаем с экспоненциальной паузой, уважая Retry-After;
  * постраничная выгрузка идёт потоком (yield), а не собирается в список: за три
    года документов у крупного аккаунта сотни тысяч, и держать их в памяти
    целиком незачем.

Документация: https://dev.moysklad.ru/doc/api/remap/1.2/
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, date
from typing import Any, AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)

BASE = "https://api.moysklad.ru/api/remap/1.2"

# МойСклад допускает 5 одновременных запросов на токен. Берём 4, чтобы оставить
# запас живому виджету, который ходит в API теми же учётными данными.
MAX_PARALLEL_PER_TOKEN = 4

MAX_RETRIES = 4
PAGE_LIMIT = 1000
# При expand= МойСклад режет страницу до 100 — это его ограничение, не наше.
PAGE_LIMIT_EXPANDED = 100


class MoyskladError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"MoySklad API {status}: {body[:300]}")
        self.status = status
        self.body = body


def ms_datetime(value: datetime | date) -> str:
    """МойСклад ждёт «YYYY-MM-DD HH:MM:SS» — не ISO-8601 с T."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return f"{value.isoformat()} 00:00:00"


def parse_ms_datetime(raw: Optional[str]) -> Optional[datetime]:
    """Разбирает «YYYY-MM-DD HH:MM:SS[.mmm]». Мусор — не повод валить весь синк."""
    if not raw:
        return None
    text = raw.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    logger.warning("Не разобрал дату МойСклад: %r", raw)
    return None


def entity_id_from_href(href: Optional[str]) -> Optional[str]:
    """Из meta.href достаём UUID сущности — он всегда последний сегмент пути."""
    if not href:
        return None
    tail = href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return tail or None


class MoyskladClient:
    """
    Живёт на время одного прогона синка одного аккаунта: семафор внутри, поэтому
    переиспользовать один экземпляр между аккаунтами нельзя — лимит у них разный.
    """

    def __init__(
        self,
        token: str,
        *,
        timeout: float = 120.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self._token = token
        self._sem = asyncio.Semaphore(MAX_PARALLEL_PER_TOKEN)
        self._client = httpx.AsyncClient(
            # Шов для тестов: подменяем транспорт, а не сам клиент, чтобы
            # повторы, семафор и разбор страниц проверялись настоящие.
            transport=transport,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json;charset=utf-8",
                "Accept-Encoding": "gzip",
            },
            # Пул держим тёплым: за прогон уходят тысячи запросов, и новое TLS-рукопожатие
            # на каждый стоило бы больше самих запросов.
            limits=httpx.Limits(max_connections=MAX_PARALLEL_PER_TOKEN,
                                max_keepalive_connections=MAX_PARALLEL_PER_TOKEN),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "MoyskladClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict:
        url = path if path.startswith("http") else f"{BASE}{path}"
        async with self._sem:
            return await self._get_with_retry(url, params)

    async def _get_with_retry(self, url: str, params: Optional[dict[str, Any]]) -> dict:
        delay = 1.0
        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            try:
                resp = await self._client.get(url, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                # Сеть моргнула — это стоит повторить, но не бесконечно.
                last_error = exc
                if attempt == MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                pause = float(retry_after) if retry_after and retry_after.isdigit() else delay
                logger.info("429 от МойСклад, пауза %.1fs (попытка %d)", pause, attempt + 1)
                await asyncio.sleep(pause)
                delay *= 2
                continue

            # 5xx у МойСклад бывают точечными; 4xx (кроме 429) повторять бессмысленно.
            if resp.status_code >= 500:
                last_error = MoyskladError(resp.status_code, resp.text)
                if attempt == MAX_RETRIES - 1:
                    raise last_error
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if resp.status_code >= 400:
                raise MoyskladError(resp.status_code, resp.text)

            return resp.json()

        raise last_error or MoyskladError(429, "исчерпаны повторы после 429")

    async def paginate(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        *,
        limit: int = PAGE_LIMIT,
    ) -> AsyncIterator[dict]:
        """
        Идёт по страницам последовательно и отдаёт строки по одной.

        Последовательно, а не веером по offset: у сущности с фильтром по updated
        набор меняется под ногами, и параллельные offset'ы дают дыры. Скорость
        здесь не главное — прогон и так фоновый.
        """
        offset = 0
        query = dict(params or {})
        while True:
            query["limit"] = limit
            query["offset"] = offset
            page = await self.get(path, query)
            rows = page.get("rows") or []
            for row in rows:
                yield row
            if len(rows) < limit:
                return
            offset += limit
            # Защита от бесконечного цикла, если МойСклад вдруг перестанет
            # уменьшать страницу: size знает, сколько всего.
            size = (page.get("meta") or {}).get("size")
            if isinstance(size, int) and offset >= size:
                return
