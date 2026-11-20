# 起模型 + 编排 + 桌宠 + 提醒。模型命令在 config.json 里配。
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$Config = Get-Content (Join-Path $Root 'runtime\config.json') -Raw -Encoding UTF8 | ConvertFrom-Json

New-Item -ItemType Directory -Force (Join-Path $Root 'logs') | Out-Null
$Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $Python) { throw ' 找不到 python.exe' }

$Core = Start-Process $Python -ArgumentList @("$(Join-Path $Root 'runtime\core.py')") `
    -WorkingDirectory (Join-Path $Root 'runtime') -PassThru -WindowStyle Hidden
$Pet = Start-Process $Python -ArgumentList @("$(Join-Path $Root 'runtime\pet.py')") `
    -WorkingDirectory (Join-Path $Root 'runtime') -PassThru

Set-Content (Join-Path $Root 'runtime\pids.json') -Encoding UTF8 (ConvertTo-Json @{
    core = $Core.Id; pet = $Pet.Id
})
Write-Host "  起来了：http://127.0.0.1:$($Config.port)"
