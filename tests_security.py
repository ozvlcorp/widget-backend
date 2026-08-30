"""Проверка починки бэкенда. БД — SQLite в памяти, Vendor API не вызываем."""
import asyncio, os, sys
os.environ['DATABASE_URL'] = 'sqlite+aiosqlite:///:memory:'
os.environ['ADMIN_SECRET'] = ''            # проверим, что админка выключена
sys.path.insert(0, '/workspace/widget-backend')

from fastapi.testclient import TestClient
from app import database
from app.models import AppToken, ContextSession
from app.config import settings

# init_db в оригинале шлёт ALTER TABLE в диалекте Postgres — на SQLite не нужен
async def init_sqlite():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
database.init_db = init_sqlite

from app.main import app
ok = lambda b, m: print(f"   {'✓' if b else '✗ ПРОВАЛ'}  {m}")

async def seed():
    async with database.AsyncSessionLocal() as db:
        db.add(AppToken(widget_name='interfood', account_name='jamshid',
                        app_uid='uid', access_token='СЕКРЕТНЫЙ-ТОКЕН', account_id='acc-1'))
        await db.commit()

with TestClient(app) as c:
    asyncio.get_event_loop().run_until_complete(seed())

    print("\n════ дыра: выдача токена без проверки ════")
    r = c.get('/interfood/token', params={'account': 'jamshid'})
    ok(r.status_code == 401, f"по имени аккаунта → {r.status_code} (ждём 401)")
    ok('СЕКРЕТНЫЙ-ТОКЕН' not in r.text, "токен в ответе не утёк")
    r = c.get('/interfood/token', params={'accountId': 'acc-1'})
    ok(r.status_code == 401, f"по accountId → {r.status_code} (ждём 401)")
    ok('СЕКРЕТНЫЙ-ТОКЕН' not in r.text, "токен в ответе не утёк")

    print("\n════ обычная работа: contextKey ════")
    r = c.put('/interfood/api/moysklad/vendor/1.0/context/CK-1', json={'accountName': 'jamshid'})
    ok(r.status_code == 200, f"МойСклад зарегистрировал contextKey → {r.status_code}")
    r = c.get('/interfood/token', params={'contextKey': 'CK-1'})
    ok(r.status_code == 200 and r.json().get('access_token') == 'СЕКРЕТНЫЙ-ТОКЕН', "свежий contextKey отдаёт токен")

    print("\n════ одноразовость contextKey ════")
    # CK-1 выше уже обменяли на токен. Ключ одноразовый, поэтому второй обмен
    # обязан провалиться — и состарить эту запись для проверки TTL уже нельзя.
    r = c.get('/interfood/token', params={'contextKey': 'CK-1'})
    ok(r.status_code == 401, f"повторный обмен того же ключа → {r.status_code} (ждём 401)")

    print("\n════ срок жизни contextKey ════")
    c.put('/interfood/api/moysklad/vendor/1.0/context/CK-TTL', json={'accountName': 'jamshid'})
    async def age_key():
        from datetime import datetime, timedelta, timezone
        async with database.AsyncSessionLocal() as db:
            s = await db.get(ContextSession, 'CK-TTL')
            s.created_at = datetime.now(timezone.utc) - timedelta(seconds=settings.context_key_ttl_seconds + 60)
            await db.commit()
    asyncio.get_event_loop().run_until_complete(age_key())
    r = c.get('/interfood/token', params={'contextKey': 'CK-TTL'})
    ok(r.status_code == 401, f"просроченный contextKey → {r.status_code} (ждём 401)")
    r = c.get('/interfood/token', params={'contextKey': 'CK-TTL'})
    ok(r.status_code == 401, "повторно — тоже 401 (запись удалена)")

    print("\n════ чужой виджет не получит токен ════")
    c.put('/other/api/moysklad/vendor/1.0/context/CK-2', json={'accountName': 'jamshid'})
    r = c.get('/interfood/token', params={'contextKey': 'CK-2'})
    ok(r.status_code == 401, f"contextKey другого виджета → {r.status_code}")

    print("\n════ админка при пустом ADMIN_SECRET ════")
    r = c.get('/admin/widgets')
    ok(r.status_code == 503, f"без секрета → {r.status_code} (ждём 503, раньше было 200)")
    r = c.put('/admin/widgets/interfood', json={'app_secret': 'угнал'})
    ok(r.status_code == 503, f"перезапись конфига → {r.status_code}")

    print("\n════ Vendor API: статус приложения ════")
    r = c.get('/interfood/api/moysklad/vendor/1.0/apps/APP/acc-1')
    ok(r.status_code == 200 and r.json().get('status') == 'Activated', f"установленный аккаунт → {r.json() if r.status_code==200 else r.status_code}")
    r = c.get('/interfood/api/moysklad/vendor/1.0/apps/APP/acc-999')
    ok(r.status_code == 200 and r.json().get('status') == 'Deactivated', "неустановленный аккаунт → Deactivated")

    print("\n════ удаление приложения ════")
    c.put('/interfood/api/moysklad/vendor/1.0/context/CK-3', json={'accountName': 'jamshid'})
    r = c.request('DELETE', '/interfood/api/moysklad/vendor/1.0/apps/APP/acc-1',
                  json={'appUid': 'uid', 'accountName': 'jamshid', 'cause': 'x'})
    ok(r.status_code == 200, f"удаление → {r.status_code}")
    r = c.get('/interfood/token', params={'contextKey': 'CK-3'})
    ok(r.status_code == 401, "висевший contextKey после удаления не работает")
