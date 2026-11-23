# 从脚本位置推断项目根目录，不写死盘符。
$Root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $Root

$Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $Python) {
    Write-Host '  找不到 python.exe。装了 Python 之后要把它加进 PATH（安装时勾 "Add to PATH" 即可）。' -ForegroundColor Red
    Read-Host '  回车退出'
    exit 1
}

$Config = Join-Path $Root 'runtime\config.json'
$Port = 7860
if (Test-Path $Config) {
    try {
        $Port = (Get-Content $Config -Raw -Encoding UTF8 | ConvertFrom-Json).port
    } catch { $Port = 7860 }
}

if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "  已经在跑了：http://127.0.0.1:$Port" -ForegroundColor Yellow
    Start-Process "http://127.0.0.1:$Port"
    Start-Sleep -Seconds 1
    exit 0
}

if (-not (Test-Path $Config)) {
    Copy-Item (Join-Path $Root 'runtime\config.example.json') $Config
    Write-Host '  已生成 runtime\config.json（模型地址在 model_url 里）' -ForegroundColor DarkGray
}

$Persona = Join-Path $Root 'runtime\personality.md'
if (-not (Test-Path $Persona)) {
    Write-Host '  第一次用，先花三十秒回答四个问题。' -ForegroundColor Cyan
    & $Python (Join-Path $Root 'runtime\setup.py')
}

& (Join-Path $Root 'runtime\start.ps1')
