"""启动前检查：确认 DATABASE_URL 指向的数据库真的能连上。

数据库地址只从 DATABASE_URL 读取，端口不再散落在启动脚本里。
输出和异常信息都会去掉密码，方便直接贴到终端或日志。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.config import Settings, get_settings


class PreflightError(RuntimeError):
    """配置或数据库不可用。消息可以直接展示给使用者。"""


@dataclass(frozen=True)
class DatabaseTarget:
    host: str
    port: int
    database: str

    def __str__(self) -> str:
        return f"{self.host}:{self.port}/{self.database}"


def describe_target(database_url: str) -> DatabaseTarget:
    try:
        url = make_url(database_url)
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都当作配置错误
        raise PreflightError("DATABASE_URL 格式不正确，应形如 postgresql+psycopg://用户:密码@主机:端口/库名") from exc
    if not url.host or not url.database:
        raise PreflightError("DATABASE_URL 缺少主机或库名")
    if not url.port:
        raise PreflightError("DATABASE_URL 缺少端口，请显式写明（例如 :5433）")
    return DatabaseTarget(host=url.host, port=url.port, database=url.database)


def _redact(message: str, settings: Settings) -> str:
    password = make_url(settings.database_url).password
    if password:
        message = message.replace(password, "***")
    return message


def _hint(exc: Exception, target: DatabaseTarget) -> str:
    text_value = str(getattr(exc, "orig", exc)).lower()
    if "password authentication failed" in text_value or "authentication failed" in text_value:
        return "数据库认证失败：请检查 DATABASE_URL 里的用户名和密码。"
    if "does not exist" in text_value and "database" in text_value:
        return f"数据库 {target.database} 不存在：请先创建数据库或修正 DATABASE_URL。"
    if "could not connect" in text_value or "connection refused" in text_value or "timeout" in text_value:
        return (
            f"无法连接数据库 {target}：请确认 PostgreSQL 已启动，"
            "并且 DATABASE_URL 的主机和端口与实例一致。"
        )
    return f"数据库 {target} 检查失败：{type(exc).__name__}"


def check_database(settings: Settings | None = None) -> DatabaseTarget:
    settings = settings or get_settings()
    target = describe_target(settings.database_url)
    # 连接超时 5 秒：端口被防火墙丢弃时不要卡住启动流程。
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        raise PreflightError(_hint(exc, target)) from exc
    except SQLAlchemyError as exc:
        raise PreflightError(
            f"数据库 {target} 检查失败：{_redact(str(exc), settings)}"
        ) from exc
    finally:
        engine.dispose()
    return target


def main() -> int:
    try:
        target = check_database()
    except PreflightError as exc:
        print(f"[preflight] {exc}", file=sys.stderr)
        return 1
    print(f"[preflight] 数据库可连接：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
