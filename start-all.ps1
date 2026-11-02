$ErrorActionPreference = 'Stop'

$PgBin   = 'D:\PostgreSQL\18\bin'
$PgData  = 'D:\PostgreSQL\AIhmData'
$PgLog   = 'D:\PostgreSQL\fandazi-postgres.log'
$Backend = 'C:\python\pyproject\first'
$Python  = Join-Path $Backend '.venv\Scripts\python.exe'
$WebUrl  = 'http://127.0.0.1:8000/'

# .env 是相对当前目录读取的，必须先切到项目目录，否则会读到默认配置（5432）。
Set-Location $Backend

function Test-Port([string]$TargetHost, [int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($TargetHost, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(800)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Wait-Port([string]$TargetHost, [int]$Port, [int]$Seconds) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-Port $TargetHost $Port) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

# 数据库地址只在 .env 的 DATABASE_URL 里维护；这里向 Python 要主机/端口/库名，不重复解析也不打印密码。
if (-not (Test-Path $Python)) { throw "找不到 $Python，请先创建 .venv 并安装 requirements.txt。" }
$DbTarget = (& $Python (Join-Path $Backend 'scripts\db_target.py') 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $DbTarget) {
    throw "读取 DATABASE_URL 失败：$DbTarget"
}
$DbParts = $DbTarget -split '\s+'
$DbHost = $DbParts[0]
$DbPort = [int]$DbParts[1]
$DbName = $DbParts[2]
$IsLocal = $DbHost -in @('127.0.0.1', 'localhost', '::1')

# 1. PostgreSQL
if (Test-Port $DbHost $DbPort) {
    Write-Host "[1/2] PostgreSQL 已在运行 ($DbHost`:$DbPort/$DbName)" -ForegroundColor DarkGray
} elseif (-not $IsLocal) {
    throw "无法连接远程数据库 $DbHost`:$DbPort/$DbName，请先确认网络和数据库实例。"
} else {
    Write-Host "[1/2] 启动本地 PostgreSQL（端口 $DbPort）..." -ForegroundColor Cyan
    # 端口由 postgresql.conf 决定；这里异步启动并轮询，避免 pg_ctl 输出管道被 postgres 继承导致阻塞
    Start-Process -FilePath "$PgBin\pg_ctl.exe" -ArgumentList @('-D', $PgData, '-l', $PgLog, 'start') -WindowStyle Hidden
    if (-not (Wait-Port $DbHost $DbPort 20)) { throw "PostgreSQL 启动失败，查看 $PgLog" }
    Write-Host "      PostgreSQL 就绪 ($DbPort)" -ForegroundColor Green
}

# 2. 数据库可连接性 + 后端 API
Write-Host '[2/2] 检查数据库连接...' -ForegroundColor Cyan
# 端口打开不等于可接受连接：PostgreSQL 冷启动时可能仍在恢复，重试几次再判定失败。
$DbCheck = ''
for ($i = 0; $i -lt 15; $i++) {
    $DbCheck = (& $Python (Join-Path $Backend 'scripts\check_db.py') 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 1
}
if ($DbCheck) { Write-Host $DbCheck }
if ($LASTEXITCODE -ne 0) {
    throw '数据库检查未通过，后端未启动。请按上面的提示修正 .env 或数据库实例。'
}

if (Test-Port '127.0.0.1' 8000) {
    Write-Host '[2/2] 后端已在运行 (8000)' -ForegroundColor DarkGray
} else {
    Write-Host '[2/2] 启动后端...' -ForegroundColor Cyan
    Start-Process powershell -ArgumentList @(
        '-NoExit', '-Command', "Set-Location '$Backend'; .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
    )
    if (-not (Wait-Port '127.0.0.1' 8000 30)) { throw '后端启动失败，查看弹出的后端窗口' }
    Write-Host '      后端就绪 (8000)' -ForegroundColor Green
}

Write-Host ''
Write-Host '  饭搭子网页版   http://127.0.0.1:8000/' -ForegroundColor Green
Write-Host '  API 文档       http://127.0.0.1:8000/docs' -ForegroundColor DarkGray
Start-Process $WebUrl
