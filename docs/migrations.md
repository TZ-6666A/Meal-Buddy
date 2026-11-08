# 数据库迁移（Alembic）

数据库连接串只有一个来源：`.env` 里的 `DATABASE_URL`。启动脚本、迁移、集成测试都从这里读，端口不再写死在多个脚本里。

迁移脚本只支持 PostgreSQL。SQLite 仅用于单元测试，测试里用 `Base.metadata.create_all`，不走 Alembic。

## 当前状态

- 初始迁移：`migrations/versions/0001_initial_schema.py`
- 覆盖：`meal_entries`、`pantry_items`、`preference_notes`、`decision_sessions`、`chat_messages`、`recommendation_feedback`，以及 `updated_at`、常用索引和反馈表约束。
- 登录系统迁移：`migrations/versions/0002_users_and_ownership.py`
  - 新增 `users`、`user_sessions` 两张表；
  - 五张业务表加 `user_id` 归属列（可空，应用启动引导会把历史 NULL 行归到管理员名下）并幂等补外键；
  - `pantry_items` 的食材名唯一索引从全局唯一改为 `(user_id, lower(name))` 按用户唯一。
- 邮箱绑定迁移：`migrations/versions/0003_email_binding.py`
  - `users` 加可空 `email` 列（小写存储）和唯一索引 `uq_users_email`；
  - 新增 `email_verifications` 表存验证码哈希（发码限频、防爆破）。
- 迁移里的 DDL 使用 `IF NOT EXISTS`，因此历史上由 `create_all` + `sql/*.sql` 建出来的库可以直接升级，不会因为表已存在而失败。
- 应用启动时**不再**自动建表。表缺失会直接报错并提示执行 `alembic upgrade head`。

## 备份（升级前必做）

在 PowerShell 里执行，`pg_dump` 会交互式提示密码，不会把密码写进命令历史：

```powershell
New-Item -ItemType Directory -Force D:\backup | Out-Null
& 'D:\PostgreSQL\18\bin\pg_dump.exe' -h 127.0.0.1 -p 5433 -U postgres -Fc `
    -f "D:\backup\fandazi-$(Get-Date -Format yyyyMMdd-HHmm).dump" fandazi
```

主机、端口、库名与 `.env` 里的 `DATABASE_URL` 保持一致。恢复：

```powershell
& 'D:\PostgreSQL\18\bin\pg_restore.exe' -h 127.0.0.1 -p 5433 -U postgres `
    -d fandazi --clean --if-exists "D:\backup\fandazi-YYYYMMDD-HHmm.dump"
```

## 升级

```powershell
cd C:\python\pyproject\first
.\.venv\Scripts\python.exe -m alembic current      # 当前版本
.\.venv\Scripts\python.exe -m alembic upgrade head # 升到最新
.\.venv\Scripts\python.exe -m alembic current      # 确认
```

老库（没有 alembic_version 表）有两种做法：

1. 直接 `upgrade head`：迁移是幂等的，会把缺的列/索引/表补上。**推荐**。
2. 如果你已经手工执行过 `sql/20261024_lifecycle_up.sql`，也可以 `alembic stamp head` 只记录版本、不再执行 DDL。执行前先用 `\d` 核对表结构。

## 回滚

```powershell
.\.venv\Scripts\python.exe -m alembic downgrade -1     # 回退一个版本
.\.venv\Scripts\python.exe -m alembic downgrade base   # 回到空库
```

`0001_initial_schema` 的 downgrade 会 **删除全部业务表和数据**（包括 `recommendation_feedback`）。执行前必须备份。

## 新增迁移

```powershell
.\.venv\Scripts\python.exe -m alembic revision -m "describe change"
```

然后手写 `upgrade()` / `downgrade()`。建议：

- 加列用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，老库可重复执行。
- 加索引用 `CREATE INDEX IF NOT EXISTS`。
- 先在测试库验证，再对开发库执行。
- 不要用 `--autogenerate` 直接生成后不检查：模型里的 `server_default`、约束名和手工 SQL 需要人工核对。

## 测试库

集成测试只接受库名里含 `test` 的数据库，并通过 `TEST_DATABASE_URL` 指定。第一次使用先建库（不会碰开发库）：

```powershell
& 'D:\PostgreSQL\18\bin\createdb.exe' -h 127.0.0.1 -p 5433 -U postgres fandazi_test
```

然后：

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://postgres:<密码>@127.0.0.1:5433/fandazi_test'
.\.venv\Scripts\python.exe -m pytest tests\test_postgres_integration.py -q
```

测试开始时会在这个测试库里 `downgrade base` 再 `upgrade head`，每个用例前 `TRUNCATE` 所有业务表。**只对库名含 `test` 的库生效**，没有设置 `TEST_DATABASE_URL` 时整组测试自动跳过。
