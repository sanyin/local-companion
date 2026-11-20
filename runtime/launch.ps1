# 从脚本位置推断项目根目录，不写死盘符。
$Root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $Root

if (Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host '  已经在跑了：http://127.0.0.1:7860' -ForegroundColor Yellow
    Read-Host '  回车退出'
    exit 0
}

$Config = Join-Path $Root 'runtime\config.json'
if (-not (Test-Path $Config)) {
    Copy-Item (Join-Path $Root 'runtime\config.example.json') $Config
    Write-Host "  已生成 runtime\config.json，先去改里面的模型路径。" -ForegroundColor Yellow
    Read-Host '  回车退出'
    exit 0
}

$Persona = Join-Path $Root 'runtime\personality.md'
if (-not (Test-Path $Persona)) {
    Write-Host '  第一次用，先花三十秒回答四个问题。' -ForegroundColor Cyan
    & $Python (Join-Path $Root 'runtime\setup.py')
}

& (Join-Path $Root 'runtime\start.ps1')
