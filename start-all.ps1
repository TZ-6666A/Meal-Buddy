$ErrorActionPreference = 'Stop'

$PgBin   = 'D:\PostgreSQL\18\bin'
$PgData  = 'D:\PostgreSQL\AIhmData'
$PgLog   = 'D:\PostgreSQL\fandazi-postgres.log'
$Backend = 'C:\python\pyproject\first'
$WebUrl  = 'http://127.0.0.1:8000/'

function Test-Port([int]$Port) {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Wait-Port([int]$Port, [int]$Seconds) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-Port $Port) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

# 1. PostgreSQL
if (Test-Port 5433) {
    Write-Host '[1/2] PostgreSQL 已在运行 (5433)' -ForegroundColor DarkGray
} else {
    Write-Host '[1/2] 启动 PostgreSQL...' -ForegroundColor Cyan
    # 端口已写在 postgresql.conf 里，这里异步启动并轮询，避免 pg_ctl 输出管道被 postgres 继承导致阻塞
    Start-Process -FilePath "$PgBin\pg_ctl.exe" -ArgumentList @('-D', $PgData, '-l', $PgLog, 'start') -WindowStyle Hidden
    if (-not (Wait-Port 5433 20)) { throw "PostgreSQL 启动失败，查看 $PgLog" }
    Write-Host '      PostgreSQL 就绪 (5433)' -ForegroundColor Green
}

# 2. 后端 API
if (Test-Port 8000) {
    Write-Host '[2/2] 后端已在运行 (8000)' -ForegroundColor DarkGray
} else {
    Write-Host '[2/2] 启动后端...' -ForegroundColor Cyan
    Start-Process powershell -ArgumentList @(
        '-NoExit', '-Command', "Set-Location '$Backend'; .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
    )
    if (-not (Wait-Port 8000 30)) { throw '后端启动失败，查看弹出的后端窗口' }
    Write-Host '      后端就绪 (8000)' -ForegroundColor Green
}

Write-Host ''
Write-Host '  饭搭子网页版   http://127.0.0.1:8000/' -ForegroundColor Green
Write-Host '  API 文档       http://127.0.0.1:8000/docs' -ForegroundColor DarkGray
Start-Process $WebUrl
