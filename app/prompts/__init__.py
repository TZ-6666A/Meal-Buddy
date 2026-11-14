"""提示词加载。改提示词必须同步修改 PROMPT_VERSION。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPT_VERSION = "2026-11-13.1"

_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load(name: str) -> str:
    path = _DIR / f"{name}.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"提示词文件缺失或不可读：{path}") from exc
    if not text:
        raise RuntimeError(f"提示词文件为空：{path}")
    return text


def recommend_system(condition: str) -> str:
    extra = {"fitness": "recommend_fitness", "diabetes": "recommend_diabetes",
             "hypertension": "recommend_hypertension"}.get(condition)
    base = load("recommend_base")
    return f"{base}\n{load(extra)}" if extra else base
