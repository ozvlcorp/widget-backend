import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db
from .routers import vendor, token, admin

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


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
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# No prefix — widget_name is the first path segment
app.include_router(vendor.router)
app.include_router(token.router)
app.include_router(admin.router)


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}
