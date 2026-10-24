from contextlib import asynccontextmanager
from pathlib import Path

import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.api import public_router, router
from app.config import get_settings
from app.db import Base, engine
from app.logging_config import setup_logging
from app.security import require_token
from app.services.meal_assistant import close_clients
from app import models  # noqa: F401

setup_logging()
settings = get_settings()
logger = logging.getLogger("fandazi.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.api_token:
        logger.warning("未设置 API_TOKEN，接口无鉴权")
    # Existing PostgreSQL databases get schema changes from the reviewed SQL scripts.
    Base.metadata.create_all(
        bind=engine,
        tables=[table for table in Base.metadata.sorted_tables if table.name != "recommendation_feedback"],
    )
    try:
        yield
    finally:
        await close_clients()


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

WEB_PAGE = Path(__file__).with_name("web").joinpath("index.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def web_preview() -> HTMLResponse:
    return HTMLResponse(WEB_PAGE)
