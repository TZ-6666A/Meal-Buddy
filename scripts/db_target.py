"""打印 DATABASE_URL 的主机、端口、库名，供启动脚本使用（不输出密码）。

用法：
  .\\.venv\\Scripts\\python.exe scripts\\db_target.py
输出：
  127.0.0.1 5433 fandazi
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.preflight import PreflightError, describe_target  # noqa: E402

try:
    target = describe_target(get_settings().database_url)
except PreflightError as exc:
    print(f"[db_target] {exc}", file=sys.stderr)
    raise SystemExit(1) from exc

print(f"{target.host} {target.port} {target.database}")
