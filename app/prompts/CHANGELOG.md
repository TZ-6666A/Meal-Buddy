# 提示词变更记录

每次修改 `.txt` 必须同时把 `app/prompts/__init__.py` 里的 `PROMPT_VERSION` 加一，
并在这里记录改了什么、为什么改、评测结果。

## 2026-10-24.1

- 初始版本：从 `app/services/meal_assistant.py` 的内联字符串迁移出来，内容与迁移前完全一致。
- 迁移前行为：聊天用 `SYSTEM_PROMPT`，推荐用 `_system_for()` 拼接基础指令 + 模式附加指令。

## 2026-10-24.2

- 删除未被调用的 `extract_preferences()` 和 `extract.txt`；偏好抽取统一使用本地规则，避免保留无效提示词。
