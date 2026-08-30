"""
Управление синхронизацией и её наблюдаемость.

  POST /admin/sync                              — прогнать все аккаунты
  POST /admin/sync/{widget_name}/{account_id}   — прогнать один
  GET  /admin/sync/status                       — состояние по всем аккаунтам
  GET  /{widget_name}/data/status               — свежесть данных для виджета

Запуск синка отвечает сразу и работает в фоне. Иначе первичная заливка истории
держала бы HTTP-соединение часами — а через Cloudflare оно и не проживёт
столько: там таймаут до origin 100 секунд, дальше 524.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CallerAccount, caller_account
from ..config import settings
from ..database import get_db
from ..models import AppToken, SyncRun, SyncState
from ..scheduler import next_run_time
from ..sync import sync_account, sync_all_accounts

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Sync"])

# Фоновые задачи держим за ссылки: без этого сборщик мусора может убрать
# задачу прямо посреди прогона — asyncio хранит на неё только слабую ссылку.
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _check_secret(x_admin_secret: Optional[str] = Header(default=None)) -> None:
    """Пустой ADMIN_SECRET выключает админку, а не открывает её."""
    if not settings.admin_secret:
        raise HTTPException(503, "Admin API disabled")
    if x_admin_secret != settings.admin_secret:
        raise HTTPException(401, "Unauthorized")


@router.post("/admin/sync", dependencies=[Depends(_check_secret)])
async def trigger_sync_all():
    _spawn(sync_all_accounts(trigger="manual"))
    return {"status": "started", "scope": "all accounts"}


@router.post(
    "/admin/sync/{widget_name}/{account_id}", dependencies=[Depends(_check_secret)]
)
async def trigger_sync_one(
    widget_name: str, account_id: str, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(AppToken).where(
            AppToken.widget_name == widget_name,
            AppToken.account_id == account_id,
            AppToken.status == "active",
        ).limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Account not installed for this widget")

    _spawn(sync_account(
        account_id, widget_name, row.access_token,
        account_name=row.account_name, trigger="manual",
    ))
    return {"status": "started", "widget": widget_name, "account_id": account_id}


def _state_payload(state: SyncState) -> dict:
    return {
        "entity": state.entity,
        "watermark": state.watermark.isoformat() if state.watermark else None,
        "backfill_done": state.backfill_done,
        "backfill_cursor": (
            state.backfill_cursor.isoformat() if state.backfill_cursor else None
        ),
        "last_success_at": (
            state.last_success_at.isoformat() if state.last_success_at else None
        ),
        "rows_synced": state.rows_synced,
        "last_error": state.last_error,
    }


@router.get("/admin/sync/status", dependencies=[Depends(_check_secret)])
async def sync_status_all(db: AsyncSession = Depends(get_db)):
    states = (await db.execute(select(SyncState))).scalars().all()
    runs = (
        await db.execute(
            select(SyncRun).order_by(SyncRun.started_at.desc()).limit(50)
        )
    ).scalars().all()

    by_account: dict[str, list[dict]] = {}
    for state in states:
        by_account.setdefault(f"{state.widget_name}/{state.account_id}", []).append(
            _state_payload(state)
        )

    return {
        "scheduler": {
            "enabled": settings.sync_enabled,
            "cron": settings.sync_cron,
            "next_run": next_run_time(),
        },
        "accounts": by_account,
        "recent_runs": [
            {
                "id": run.id,
                "widget": run.widget_name,
                "account_id": run.account_id,
                "trigger": run.trigger,
                "status": run.status,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "stats": run.stats,
                "error": run.error,
            }
            for run in runs
        ],
    }


@router.get("/{widget_name}/data/status")
async def data_status(
    caller: CallerAccount = Depends(caller_account),
    db: AsyncSession = Depends(get_db),
):
    """
    Свежесть данных для виджета.

    При ночном синке цифры за сегодня появляются только после прогона, поэтому
    интерфейсу нужно честно показывать, на какой момент данные, а не делать вид,
    что они текущие.
    """
    states = (
        await db.execute(
            select(SyncState).where(
                SyncState.account_id == caller.account_id,
                SyncState.widget_name == caller.widget_name,
            )
        )
    ).scalars().all()

    last_run = (
        await db.execute(
            select(SyncRun)
            .where(
                SyncRun.account_id == caller.account_id,
                SyncRun.widget_name == caller.widget_name,
                SyncRun.status == "ok",
            )
            .order_by(SyncRun.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    successes = [s.last_success_at for s in states if s.last_success_at]
    backfilling = [s.entity for s in states if not s.backfill_done]

    return {
        "account_id": caller.account_id,
        "synced_at": (
            last_run.finished_at.isoformat()
            if last_run and last_run.finished_at
            else (max(successes).isoformat() if successes else None)
        ),
        # Пока история доливается, старые периоды в базе неполные — интерфейсу
        # стоит об этом сказать, а не показывать провал в графике как факт.
        "backfill_in_progress": bool(backfilling),
        "backfill_pending": sorted(backfilling),
        "next_scheduled_run": next_run_time(),
        "entities": [_state_payload(s) for s in states],
    }
