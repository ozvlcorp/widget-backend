import asyncio, os, sys
os.environ['DATABASE_URL'] = 'sqlite+aiosqlite:///:memory:'
os.environ['ADMIN_SECRET'] = 'sek'
sys.path.insert(0, '/workspace/widget-backend')
from fastapi.testclient import TestClient
from app import database
from app.models import AppToken, ContextSession, WidgetConfig
async def init_sqlite():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
database.init_db = init_sqlite
from app.main import app
ok = lambda b, m: print(f"   {'✓' if b else '✗ ПРОВАЛ'}  {m}")
H = {'X-Admin-Secret': 'sek'}

async def seed():
    async with database.AsyncSessionLocal() as db:
        db.add(WidgetConfig(widget_name='dashboard', app_secret='S3CR3T', app_uid='uid-1'))
        db.add(AppToken(widget_name='dashboard', account_name='jamshid', app_uid='uid-1',
                        access_token='ТОКЕН-АККАУНТА', account_id='acc-1'))
        db.add(ContextSession(context_key='CK', widget_name='dashboard', account_name='jamshid'))
        await db.commit()

with TestClient(app) as c:
    asyncio.get_event_loop().run_until_complete(seed())

    print("\n════ обзор базы (замена SELECT) ════")
    r = c.get('/admin/overview', headers=H)
    ok(r.status_code == 200, f"ответ {r.status_code}")
    w = r.json()['widgets']
    ok(len(w) == 1 and w[0]['widget_name'] == 'dashboard', f"виден виджет: {w[0]['widget_name']}")
    ok(w[0]['accounts'] == [{'account_name': 'jamshid', 'account_id': 'acc-1'}], f"аккаунты: {w[0]['accounts']}")
    ok(w[0]['app_secret_set'] is True and w[0]['app_uid'] == 'uid-1', "конфиг виден")
    ok('ТОКЕН-АККАУНТА' not in r.text and 'S3CR3T' not in r.text, "ни токен, ни секрет наружу не отдаются")
    ok(c.get('/admin/overview').status_code == 403, "без секрета — 403")

    print("\n════ переименование ════")
    r = c.post('/admin/widgets/dashboard/rename/interfood', headers=H)
    ok(r.status_code == 200, f"ответ {r.status_code}")
    ok(r.json()['accounts_moved'] == ['jamshid'] and r.json()['config_moved'], f"перенесено: {r.json()}")
    w = c.get('/admin/overview', headers=H).json()['widgets']
    ok(len(w) == 1 and w[0]['widget_name'] == 'interfood', f"осталось только: {[x['widget_name'] for x in w]}")
    ok(w[0]['app_uid'] == 'uid-1' and w[0]['accounts'][0]['account_id'] == 'acc-1', "токен и конфиг сохранены")

    print("\n════ токен работает под новым именем ════")
    c.put('/interfood/api/moysklad/vendor/1.0/context/CK2', json={'accountName': 'jamshid'})
    r = c.get('/interfood/token', params={'contextKey': 'CK2'})
    ok(r.status_code == 200 and r.json()['access_token'] == 'ТОКЕН-АККАУНТА', f"обмен работает → {r.status_code}")
    c.put('/dashboard/api/moysklad/vendor/1.0/context/CK3', json={'accountName': 'jamshid'})
    ok(c.get('/dashboard/token', params={'contextKey': 'CK3'}).status_code == 401, "старое имя больше не выдаёт токен")

    print("\n════ защита от затирания ════")
    ok(c.post('/admin/widgets/interfood/rename/interfood', headers=H).status_code == 400, "переименование в себя — 400")
    ok(c.post('/admin/widgets/нет-такого/rename/x', headers=H).status_code == 404, "нечего переносить — 404")
    asyncio.get_event_loop().run_until_complete(seed())  # снова заводим dashboard
    ok(c.post('/admin/widgets/dashboard/rename/interfood', headers=H).status_code == 409,
       "цель занята — 409, чужой токен не затёрт")
