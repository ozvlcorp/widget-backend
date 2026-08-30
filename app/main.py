import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import init_db
from .routers import vendor, token, admin, sync_admin, data
from .scheduler import shutdown_scheduler, start_scheduler

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(
    title="OY MoySklad Widget Backend",
    description=(
        "Single backend for all OY MoySklad widgets.\n\n"
        "Each widget is namespaced by its name in the URL:\n"
        "- `/{widget_name}/api/moysklad/vendor/1.0/...` — MoySklad calls this\n"
        "- `/{widget_name}/token?account=X` — frontend calls this\n\n"
        "Set `endpointBase` in the widget descriptor to `https://your-domain.com/{widget_name}`"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Берём из настроек, а не хардкодом: раньше CORS_ORIGINS в конфиге был,
    # но игнорировался, и сузить его без правки кода было нельзя.
    allow_origins=settings.cors_origins,
    # Наши собственные поддомены разрешены всегда — см. cors_origin_regex.
    allow_origin_regex=settings.cors_origin_regex or None,
    allow_methods=["*"],
    allow_headers=["*"],
)

# No prefix — widget_name is the first path segment
app.include_router(vendor.router)
app.include_router(token.router)
app.include_router(admin.router)
app.include_router(sync_admin.router)
app.include_router(data.router)


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}
