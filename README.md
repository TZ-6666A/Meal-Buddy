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

## Docker 部署（Ubuntu 服务器）

服务器需安装 Docker Engine 和 Docker Compose 插件。将仓库克隆到服务器后，在项目根目录创建 `.env`，至少设置：

```dotenv
APP_ENV=production
POSTGRES_PASSWORD=使用 openssl rand -hex 32 生成的随机值
API_TOKEN=至少 16 个字符的随机值
OPENAI_API_KEY=你的模型服务密钥
```

`.env` 只放服务器，不要提交 Git。首次部署执行：

```bash
docker compose up -d db
docker compose run --rm app alembic upgrade head
docker compose up -d --build app
docker compose ps
```

应用监听服务器 TCP 8000 端口，可先用 `http://服务器公网IP:8000` 验证；在云防火墙中只开放需要的端口。PostgreSQL 数据保存在 Docker 命名卷 `postgres_data`，更新应用时运行 `git pull` 后执行 `docker compose up -d --build app`。不要删除该数据卷。

### HTTPS 反向代理（定位功能必需）

浏览器的 `navigator.geolocation` 只在安全上下文里可用：`https://` 或 `localhost`。用 `http://公网IP:8000` 访问时，浏览器会直接拒绝定位且不弹权限框，页面只能退回手动选城市。因此公网使用必须配 HTTPS。

Caddy 会自动申请并续期 Let's Encrypt 证书，配置最少：

```bash
sudo apt install -y caddy
```

`/etc/caddy/Caddyfile`：

```
tz369.xyz {
    reverse_proxy localhost:8000
}
```

```bash
sudo systemctl reload caddy    # 或 restart
sudo systemctl status caddy
```

前提与排错：

- 域名 A 记录指向本机公网 IP，且解析已生效（`nslookup 你的域名 8.8.8.8` 能看到该 IP）。
- **80 和 443 端口必须由 Caddy 占用**。Ubuntu 上预装的 nginx 会抢占 80，导致证书签发的 HTTP-01 校验失败：
  `sudo systemctl disable --now nginx`。
- 云厂商安全组需放行 80、443；Caddy 申请证书时对外访问 80。
- 验证：`curl -I https://你的域名` 返回 200，且 `docker compose logs app` 里能看到真实客户端 IP 而不是 `172.x.x.x`。

应用侧已经配合反代做了两件事，换域名时无需再改代码：`compose.yaml` 里 `FORWARDED_ALLOW_IPS` 让 uvicorn 采信代理写入的 `X-Forwarded-For`；`app/security.py` 的 `client_host()` 只在直连对端是内网地址时才读该头，避免外部伪造。限流和 IP 粗定位都走这个函数。

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
- `ADMIN_PASSWORD` 必须存在且至少 8 位（用于首次启动引导管理员账号）；
- `DATABASE_URL` 不能还是示例密码 `change_me`。

### 账号与登录

网页端使用用户名 + 密码登录，会话由后端签发的 **HttpOnly Cookie** 承载（前端不保存任何凭据）：

- 新用户通过**邀请码注册**（`INVITE_CODE`，留空表示关闭注册）；
- 数据全部按账号隔离：吃饭记录、库存、口味、聊天、决策会话各归各的账号；
- 登录会话默认 30 天（`SESSION_TTL_DAYS`），密码用 scrypt 哈希存储；
- 首次启动引导管理员账号（`ADMIN_USERNAME`/`ADMIN_PASSWORD`），历史数据自动归入其名下；
- 管理员登录后页面多出「管理」入口：用户管理（建号/重置密码/停用启用）、使用统计（用户量、记录量、AI 调用与花费）、查看用户数据（只读）。

`X-API-Token` 仍保留为**脚本通道**：携带正确 token 的请求映射到管理员账号，供命令行/自动化调用。`GET /api/v1/health` 公开。

敏感操作权限：

- `/export`、`DELETE /data`：登录用户，且只作用于**自己的**数据（导出响应带 `Cache-Control: no-store` 并写审计日志）；
- `/maintenance/prune`、`/usage`、`/admin/*`：仅管理员；
- `/auth/register`、`/auth/login` 有独立的按 IP 限流，防止在线爆破。

开发模式（`APP_ENV=development` 且未配 `API_TOKEN`）默认免登录，自动以 `dev` 管理员身份运行；想本地体验真实登录流程，在 `.env` 设 `DEV_AUTO_LOGIN=false` 后重启，浏览器打开页面即可注册/登录。

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
