# 饭搭子 Python API

FastAPI service for the HarmonyOS ArkTS client in `D:\HarmonyProject\AIhm`.

## Setup

PostgreSQL 18.6 is installed under `D:\PostgreSQL`. The app's isolated local cluster stores data in `D:\PostgreSQL\AIhmData`, listens on port `5433`, and contains the `fandazi` database. The backend connection string and a generated local password are in the ignored `.env` file.

The Python environment and dependencies are already set up in this folder. Start the API from PowerShell with:

```powershell
.\run.ps1
```

Open `http://127.0.0.1:8000/docs` to inspect the API. The development server binds to localhost only.

`/docs` and `/openapi.json` remain public for local development. Anyone who can reach them can inspect the API contract; do not expose them to an untrusted network.

## API token and device testing

Set `API_TOKEN` in the ignored `.env` file to a long random value. Leave it blank only for local development; in that case the API logs `未设置 API_TOKEN，接口无鉴权` and protected endpoints are open. `GET /api/v1/health` is public. All other `/api/v1` routes require the `X-API-Token` header when a token is configured.

For the web client, set the local `API_TOKEN` constant near the top of `app/web/index.html`. For HarmonyOS, set the constant in `entry/src/main/ets/common/ApiClient.ets`; do not commit a real token. A token embedded in browser or app code can be extracted, so this is a practical LAN safeguard for a single-user prototype, not user identity or production-grade authentication. For a physical device, set `API_BASE_URL` to the computer's private LAN address, bind Uvicorn to `0.0.0.0` only on a trusted private network, and restrict access with the OS firewall. Keep `.env` on the server.

`RATE_LIMIT_PER_MINUTE` defaults to 10 per client IP for recommendation and chat endpoints. The limit is held in process memory and resets on restart; multiple worker processes do not share it.

当前网页端仅支持浅色主题。

To enable a real model, set an OpenAI-compatible `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL` in `.env`. The key stays on the server. Without a model key, `/api/v1/recommendations` returns clearly marked demo recipes so the client flow can be tested before choosing a model provider.

Without a model key, `/api/v1/recommendations` returns clearly marked demo recipes so the client flow can be tested before choosing a model provider.

## API

- `GET /api/v1/health`
- `GET|POST /api/v1/meals`
- `GET|POST /api/v1/pantry`
- `DELETE /api/v1/pantry/{item_id}`
- `POST /api/v1/recommendations`
- `POST /api/v1/recommendations/stream` — SSE，依次推送 `accepted` / `thinking` / `done` 或 `error`
- `GET|POST /api/v1/preferences`
- `GET /api/v1/chat/history`、`POST /api/v1/chat`
- `POST /api/v1/decisions`
- `GET /api/v1/usage` — 当日调用次数、token、估算成本
- `GET /api/v1/export` — 导出全部数据
- `DELETE /api/v1/data` — 清空全部数据，请求体需 `{"confirm": "DELETE"}`

## 模型不可用时

推荐接口有三级降级：

1. 正常调用模型（`source=llm`）
2. 模型失败或返回的菜全被忌口过滤 → 返回内置演示菜谱（`source=fallback`）
3. 演示菜谱也被忌口过滤 → 用本地决策引擎 `decision.py` 挑一道（`source=fallback`）
4. 以上都失败才返回 502

没有配置 `OPENAI_API_KEY` 时直接返回演示菜谱（`source=demo`）。

## 提示词

提示词在 `app/prompts/*.txt`，由 `app/prompts/__init__.py` 加载。修改提示词后必须：

1. 把 `PROMPT_VERSION` 加一
2. 在 `app/prompts/CHANGELOG.md` 记录改动
3. 跑一次离线评测

响应里的 `prompt_version` 字段可用于对照日志定位是哪一版提示词。

## 用量与预算

每次模型调用会记录一行 `llm_usage` 日志，并在 `/api/v1/usage` 汇总当日数据。
在 `.env` 里设置 `DAILY_TOKEN_BUDGET`（0 表示不限）后，超出预算的推荐/对话请求返回 429。
`LLM_PRICE_PER_1K_INPUT` / `LLM_PRICE_PER_1K_OUTPUT` 用于估算成本。

## 测试与评测

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe scripts\eval_recommend.py --offline   # 不调模型
.\.venv\Scripts\python.exe scripts\eval_recommend.py             # 调真实模型，需确认
```

测试使用临时 SQLite，不会读写真实的 PostgreSQL 数据。

This first version is a single-user local prototype. There is no Alembic setup. Existing PostgreSQL databases need the reviewed SQL scripts under `sql/` applied manually; `create_all` creates missing tables but does not add columns or indexes to existing tables. Apply the `*_up.sql` script before running code that depends on its fields. The matching `*_down.sql` rolls those changes back, and dropping the feedback table also drops its saved feedback rows.
