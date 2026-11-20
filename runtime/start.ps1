# 起编排 + 桌宠 + 提醒。
# 模型**不在这里起**——它是一个独立进程，见 README「第二步：起模型」。
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$Config = Get-Content (Join-Path $Root 'runtime\config.json') -Raw -Encoding UTF8 | ConvertFrom-Json

$Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $Python) { throw '找不到 python.exe（装了 Python 之后要把它加进 PATH）' }

New-Item -ItemType Directory -Force (Join-Path $Root 'logs') | Out-Null

# 先看一眼模型在不在。不在就直说，别让她答不上话还让人以为是她的问题。
$ModelUrl = if ($Config.model_url) { $Config.model_url } else { 'http://127.0.0.1:8081' }
try {
    Invoke-RestMethod "$ModelUrl/health" -TimeoutSec 3 | Out-Null
    Write-Host "  模型在：$ModelUrl" -ForegroundColor DarkGray
} catch {
    Write-Host "  ! 模型没响应（$ModelUrl）" -ForegroundColor Yellow
    Write-Host '    先按 README「第二步：起模型」把 llama-server 起起来，再双击一次。' -ForegroundColor Yellow
}

$Core = Start-Process $Python -ArgumentList @("$(Join-Path $Root 'runtime\core.py')") `
    -WorkingDirectory (Join-Path $Root 'runtime') -PassThru -WindowStyle Hidden
$Pet = Start-Process $Python -ArgumentList @("$(Join-Path $Root 'runtime\pet.py')") `
    -WorkingDirectory (Join-Path $Root 'runtime') -PassThru
$Reminder = Start-Process $Python -ArgumentList @("$(Join-Path $Root 'runtime\reminder_popup.py')") `
    -WorkingDirectory (Join-Path $Root 'runtime') -PassThru -WindowStyle Hidden

Set-Content (Join-Path $Root 'runtime\pids.json') -Encoding UTF8 (ConvertTo-Json @{
    core = $Core.Id; pet = $Pet.Id; reminder = $Reminder.Id
})

Write-Host "  起来了：http://127.0.0.1:$($Config.port)" -ForegroundColor Green
Write-Host '  右下角那颗球就是她。点一下就能说话。' -ForegroundColor DarkGray
