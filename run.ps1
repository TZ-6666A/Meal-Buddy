$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$Python = '.\.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    Write-Host '找不到虚拟环境 .venv，请先创建并安装 requirements.txt。' -ForegroundColor Red
    exit 1
}

# 数据库检查不通过就不启动 API，避免服务起来了但每个请求都 500。
& $Python scripts\check_db.py
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host '后端未启动：数据库检查未通过，请按上面的提示修正 .env 或先启动 PostgreSQL。' -ForegroundColor Red
    exit 1
}

& $Python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
