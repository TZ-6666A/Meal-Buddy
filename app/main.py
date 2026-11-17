from contextlib import asynccontextmanager
from pathlib import Path

import gzip
import hashlib
import logging
import re
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from app.accounts import ensure_bootstrap
from app.api import admin_router, public_router, router
from app.config import get_settings
from app.db import SessionLocal, missing_tables
from app.logging_config import setup_logging
from app.security import get_current_user
from app.services import watch
from app.services.meal_assistant import close_clients
from app.services.recipe_photos import close_client as close_recipe_photo_client

setup_logging()
settings = get_settings()
logger = logging.getLogger("fandazi.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 生产环境缺 API_TOKEN 等关键配置时直接拒绝启动。
    settings.validate_for_startup()
    logger.info(
        "llm_configured=%s model=%s",
        bool(settings.openai_api_key),
        settings.openai_model,
    )
    if not settings.api_token:
        logger.warning("未设置 API_TOKEN，接口无鉴权（仅适用于本地开发）")
    # 表结构由 Alembic 管理，启动时只做检查，不再隐式建表。
    try:
        absent = missing_tables()
    except Exception as exc:
        raise RuntimeError(
            "无法读取数据库表结构：请先确认 PostgreSQL 已启动，并运行 scripts/check_db.py。"
        ) from exc
    if absent:
        raise RuntimeError(
            "数据库缺少表：" + "、".join(absent) + "。请先执行 alembic upgrade head（见 docs/migrations.md）。"
        )
    # 账号引导（幂等）：确保管理员存在，历史无主数据归入管理员名下。
    with SessionLocal() as bootstrap_db:
        moved = ensure_bootstrap(bootstrap_db)
    if moved:
        logger.info("bootstrap_backfilled user_rows=%d", moved)
    try:
        yield
    finally:
        await close_clients()
        await watch.close_search_client()
        await watch.close_cover_client()
        await close_recipe_photo_client()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-API-Token"],
)


@app.middleware("http")
async def add_server_timing(request: Request, call_next):
    """记录慢接口，方便在线上定位长尾，不记录查询参数或凭据。"""
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.0f}"
    path = request.url.path
    if (
        elapsed_ms >= 1000
        and path.startswith("/api/")
        and not path.endswith("/recipe-photo")
        and "/stream" not in path
    ):
        logger.warning(
            "slow_request path=%s status=%s duration_ms=%.0f",
            path,
            response.status_code,
            elapsed_ms,
        )
    return response


app.include_router(public_router)
app.include_router(admin_router)
app.include_router(router, dependencies=[Depends(get_current_user)])

DISH_PHOTO_DIR = Path(__file__).with_name("web").joinpath("dish-photos")
_DISH_PHOTO_NAME = re.compile(r"[a-z0-9-]+\.jpg")

WEB_PAGE_PATH = Path(__file__).with_name("web").joinpath("index.html")
_WEB_PAGE_BYTES = WEB_PAGE_PATH.read_bytes()
_WEB_PAGE_GZIP = gzip.compress(_WEB_PAGE_BYTES, compresslevel=9, mtime=0)
_WEB_PAGE_ETAG = '"' + hashlib.sha256(_WEB_PAGE_BYTES).hexdigest()[:16] + '"'


@app.get("/dish-photos/{filename}", include_in_schema=False)
def dish_photo(filename: str) -> FileResponse:
    """自托管的中餐成品图（人工挑选或本地生成后提交到仓库），不走 Pixabay。"""
    if not _DISH_PHOTO_NAME.fullmatch(filename):
        raise HTTPException(status_code=404)
    path = DISH_PHOTO_DIR.joinpath(filename)
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


@app.get("/", include_in_schema=False)
def web_preview(request: Request) -> Response:
    """单页应用入口：预压缩、ETag 和短缓存，避免慢网每次重传整页。"""
    headers = {
        "Cache-Control": "public, max-age=0, s-maxage=300",
        "ETag": _WEB_PAGE_ETAG,
        "Vary": "Accept-Encoding",
    }
    if request.headers.get("if-none-match") == _WEB_PAGE_ETAG:
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(_WEB_PAGE_GZIP, media_type="text/html", headers=headers)
    return Response(_WEB_PAGE_BYTES, media_type="text/html", headers=headers)
