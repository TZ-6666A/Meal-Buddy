# 饭搭子 Python API

FastAPI service for the HarmonyOS ArkTS client in `D:\HarmonyProject\AIhm`.

## Setup

PostgreSQL 18.6 is installed under `D:\PostgreSQL`. The app's isolated local cluster stores data in `D:\PostgreSQL\AIhmData`, listens on port `5433`, and contains the `fandazi` database. The backend connection string and a generated local password are in the ignored `.env` file.

**数据库地址只有一个来源：`.env` 里的 `DATABASE_URL`。** 启动脚本、Alembic 和集成测试都从这里读主机、端口和库名，不再各自写死 5433。

The Python environment and dependencies are already set up in this folder. Start from PowerShell with either:

```powershell
.\start-all.ps1   # 启动 PostgreSQL -> 检查数据库 -> 启动 API -> 打开网页版
.\run.ps1         # 只启动 API；启动前同样会检查数据库
```

两个脚本都会先执行 `scripts/check_db.py`。数据库连不上时不会启动 API，而是打印明确原因（端口不通 / 认证失败 / 库不存在）。单独检查：

```powershell
.\.venv\Scripts\python.exe scripts\check_db.py
```

Open `http://127.0.0.1:8000/docs` to inspect the API. The development server binds to localhost only.

### 附近美食推荐

网页会在你点击“授权位置”并同意浏览器定位后，把坐标发送给后端；后端使用高德周边搜索推荐附近餐馆的招牌菜。此功能需要高德开放平台的 **Web 服务 Key**（不是 JavaScript API Key）。

把 Key 填入 `C:\python\pyproject\first\.env` 的 `AMAP_KEY` 项，然后重启后端：

```dotenv
AMAP_KEY=你的高德Web服务Key
```

`.env` 已加入 Git 忽略规则，真实 Key 只保存在本机，不要粘贴到网页代码或提交到仓库。未配置 Key 时，附近推荐会提示配置方法，其他本地推荐仍可使用。

`/docs` and `/openapi.json` remain public for local development. Anyone who can reach them can inspect the API contract; do not expose them to an untrusted network.

## 数据库迁移

表结构由 Alembic 管理，应用启动时只检查表是否存在，不再隐式建表。完整流程（备份 / 升级 / 回滚 / 测试库）见 [`docs/migrations.md`](docs/migrations.md)。

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

历史上用 `sql/*.sql` 手工迁移过的库可以直接 `upgrade head`（初始迁移是幂等的）。**不要**在没有备份的情况下执行 `downgrade`。

## 安全配置

`APP_ENV` 默认为 `development`。设为 `production` 时，启动会强制检查：

- `API_TOKEN` 必须存在且至少 16 个字符，否则拒绝启动；
- `DATABASE_URL` 不能还是示例密码 `change_me`。

`GET /api/v1/health` 公开；其余 `/api/v1` 路由在配置了 `API_TOKEN` 后都需要 `X-API-Token` 头。

导出、清理、删除这三类敏感接口额外受 `require_sensitive_access` 保护：

- 配了 `API_TOKEN`：必须带正确的 token；
- 没配 token：只允许本机（loopback）访问，避免把开发服务开到 `0.0.0.0` 后局域网里任何人都能导出或清空数据；
- 导出响应带 `Cache-Control: no-store`，并记录调用方和导出条数；清理和清空同样写审计日志。

客户端**不再把 Token 写死在源码里**：

- 鸿蒙端：`entry/build-profile.json5` 的 `buildProfileFields.API_TOKEN`，由 `ApiConfig.ets` 读取；
- 网页端：`app/web/index.html` 从当前标签页的 `sessionStorage` 读取，401 时弹窗输入。

嵌在客户端里的 token 仍然可能被提取，这只是单用户原型的局域网防护，不等于用户身份认证。

### 数据隔离

当前产品是**单用户本地原型**：没有账号体系，`API_TOKEN` 是共享口令，不是用户身份。因此没有为记录、库存、偏好、聊天加 `owner_id`。

如果要支持多用户，最小改动是：给所有业务表加 `owner_id`，在 `get_db` 之外增加“当前用户”依赖，并在每个查询和删除上加 `where owner_id = current_user`；同时把共享 token 换成真正的登录态。在这些需求确定之前，不做半套身份系统。

## API

- `GET /api/v1/health`
- `GET|POST /api/v1/meals`、`DELETE /api/v1/meals/{entry_id}`
- `GET|POST /api/v1/pantry`、`DELETE /api/v1/pantry/{item_id}`
- `POST /api/v1/recommendations`
- `POST /api/v1/recommendations/stream` — SSE，依次推送 `accepted` / `thinking` / `done` 或 `error`
- `GET|POST /api/v1/preferences`、`DELETE /api/v1/preferences/{note_id}`
- `GET /api/v1/chat/history`、`POST /api/v1/chat`
- `POST /api/v1/decisions`、`.../{id}/answer`、`.../{id}/draw|again`、`.../{id}/pick`、`.../{id}/feedback`
- `GET /api/v1/usage` — 当日调用次数、token、估算成本、最近一次耗时和失败类型
- `GET /api/v1/watch-recommendations` — 按预计观看时长和类型推荐精选下饭内容，跳转平台搜索页
- `GET /api/v1/export` — 导出全部数据（敏感接口）
- `POST /api/v1/maintenance/prune` — 清理旧会话和聊天（敏感接口）
- `DELETE /api/v1/data` — 清空全部数据，请求体需 `{"confirm": "DELETE"}`（敏感接口）

## 模型结果与降级

推荐响应里的 `source` 明确区分三种来源：

| source | 含义 | `degraded` |
| --- | --- | --- |
| `llm` | 真实模型返回 | `false` |
| `demo` | 未配置 `OPENAI_API_KEY`，返回内置示例菜谱 | `true` |
| `fallback` | 模型调用失败或结果被忌口全部过滤，使用演示菜谱或本地决策引擎 | `true` |

响应同时带 `prompt_version`、`model`（仅 `llm` 时有值）、`duration_ms`。

降级链：

1. 正常调用模型（`source=llm`）
2. 模型失败 / 返回的菜全被忌口过滤 → 内置演示菜谱（`source=fallback`）
3. 演示菜谱也被忌口过滤 → 本地决策引擎 `decision.py` 挑一道（`source=fallback`）
4. 本地也挑不出来 → 返回 422，提示“按你的忌口没找到合适的菜”，而不是 502

忌口过滤在模型输出之后还会再拦一次（菜名和食材都会检查）。`鸡蛋` 这类忌口会展开成 `蛋 / 炒蛋 / 蒸蛋 / 蛋花…`。

## 日志与用量

每次模型调用记录一行 `llm_usage`，字段包括 endpoint、模型名、提示词版本、是否成功、耗时、prompt/completion token、估算成本和失败类型（异常类名）。**不记录**密钥、完整提示词或用户原文。

`/api/v1/usage` 汇总当日数据，并给出 `last_duration_ms` 和 `last_failure`。
`DAILY_TOKEN_BUDGET`（0 表示不限）用于限制当日总 token；超出后推荐/对话返回 429。
`LLM_PRICE_PER_1K_INPUT` / `LLM_PRICE_PER_1K_OUTPUT` 用于估算成本。

## 提示词

提示词在 `app/prompts/*.txt`，由 `app/prompts/__init__.py` 加载。修改提示词后必须：

1. 把 `PROMPT_VERSION` 加一
2. 在 `app/prompts/CHANGELOG.md` 记录改动
3. 跑一次离线评测

## 测试与评测

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe scripts\eval_recommend.py --offline   # 不调模型
.\.venv\Scripts\python.exe scripts\eval_recommend.py             # 调真实模型，需确认
```

单元测试使用临时 SQLite，不会读写真实的 PostgreSQL 数据。

PostgreSQL 集成测试需要显式指定测试库，库名必须含 `test`，否则拒绝运行：

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://postgres:<密码>@127.0.0.1:5433/fandazi_test'
.\.venv\Scripts\python.exe -m pytest tests\test_postgres_integration.py -q
```

未设置 `TEST_DATABASE_URL` 时这组测试自动跳过。覆盖：迁移结构、饮食记录、偏好冲突、反馈约束、导出、清理。

离线评测 `eval/cases.json` 覆盖：无食材、时间限制、过敏、忌口（avoid）、多种忌口组合、健身/控糖/低盐模式、食材匹配、无合适结果。

`RATE_LIMIT_PER_MINUTE` 默认每分钟 10 次，按客户端 IP 限制推荐/对话接口。计数保存在进程内存，重启清零，多 worker 不共享。
