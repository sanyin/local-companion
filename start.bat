@echo off
chcp 65001 >nul 2>&1
title local-companion
cd /d "%~dp0"
where pwsh >nul 2>&1
if %errorlevel%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0runtime\launch.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0runtime\launch.ps1"
)
if errorlevel 1 pause
