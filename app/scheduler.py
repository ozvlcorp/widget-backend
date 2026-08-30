"""
Планировщик ночного синка.

APScheduler внутри того же процесса, что и API: отдельный воркер и брокер
очередей на этом объёме не окупаются — аккаунтов десятки, прогон один в сутки.
Состояние прогонов всё равно в Postgres (ms_sync_runs), поэтому перезапуск
контейнера ничего не теряет, а блокировка не даёт двум репликам синкать один
аккаунт одновременно.

Расписание берётся из SYNC_CRON (UTC). Выключить целиком — SYNC_ENABLED=0.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .sync import sync_all_accounts

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
JOB_ID = "moysklad-nightly-sync"


async def _run_nightly() -> None:
    logger.info("Ночной синк: старт")
    try:
        summary = await sync_all_accounts(trigger="cron")
        logger.info(
            "Ночной синк: готово — аккаунтов %s, успешно %s, с ошибкой %s",
            summary.get("accounts"), summary.get("ok"), summary.get("failed"),
        )
    except Exception:
        # Планировщик не должен умирать вместе с одним неудачным прогоном.
        logger.exception("Ночной синк упал целиком")


def start_scheduler() -> AsyncIOScheduler | None:
    global _scheduler
    if not settings.sync_enabled:
        logger.info("SYNC_ENABLED выключен — планировщик не поднимаем")
        return None
    if _scheduler is not None:
        return _scheduler

    try:
        trigger = CronTrigger.from_crontab(settings.sync_cron, timezone="UTC")
    except ValueError:
        # Кривой cron не должен ронять приложение целиком: API важнее синка.
        logger.error(
            "SYNC_CRON=%r — некорректное выражение, планировщик не запущен",
            settings.sync_cron,
        )
        return None

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _run_nightly,
        trigger=trigger,
        id=JOB_ID,
        # Пропущенный прогон (контейнер лежал) выполняем один раз, а не столько
        # раз, сколько было пропусков.
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("Планировщик синка запущен, расписание %r (UTC)", settings.sync_cron)
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def next_run_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time.isoformat() if job and job.next_run_time else None
