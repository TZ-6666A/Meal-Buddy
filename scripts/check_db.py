"""检查 .env 里的 DATABASE_URL 是否能连上；失败时返回非零退出码。

用法：
  .\\.venv\\Scripts\\python.exe scripts\\check_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.preflight import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
