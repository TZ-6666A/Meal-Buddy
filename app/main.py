from contextlib import asynccontextmanager
from pathlib import Path

import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.api import public_router, router
from app.config import get_settings
from app.db import missing_tables
from app.logging_config import setup_logging
from app.security import require_token
from app.services import watch
from app.services.meal_assistant import close_clients

setup_logging()
settings = get_settings()
logger = logging.getLogger("fandazi.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 生产环境缺 API_TOKEN 等关键配置时直接拒绝启动。
    settings.validate_for_startup()
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
    try:
        yield
    finally:
        await close_clients()
        await watch.close_search_client()
        await watch.close_cover_client()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-API-Token"],
)
app.include_router(public_router)
app.include_router(router, dependencies=[Depends(require_token)])

WEB_PAGE_PATH = Path(__file__).with_name("web").joinpath("index.html")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def web_preview() -> HTMLResponse:
    return HTMLResponse(WEB_PAGE_PATH.read_text(encoding="utf-8"))
