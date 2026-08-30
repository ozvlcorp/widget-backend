"""
Общая настройка тестов.

Переменные окружения выставляются здесь и до импорта app: модуль app.database
создаёт engine на импорте, и если каждый файл тестов будет ставить свой
DATABASE_URL, победит тот, что импортировался первым, а остальные молча уедут
в чужую базу.

База — SQLite во временном файле, а не :memory:. Синк и ручки открывают по
несколько сессий, а каждому подключению к :memory: досталась бы своя пустая
база.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="widget-backend-tests-")

os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP}/test.db")
os.environ.setdefault("ADMIN_SECRET", "test-secret")
os.environ.setdefault("SYNC_ENABLED", "0")
os.environ.setdefault("SYNC_WINDOW_DAYS", "30")
