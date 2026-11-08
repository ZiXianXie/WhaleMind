@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Whale Mind 插件安装器

echo.
echo   Whale Mind 0.1.0 - 插件安装器
echo   ----------------------------------------
echo   本程序会把 6 个记忆插件装到你的 AstrBot 里。
echo   全程双击即可，不需要命令行知识。
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*

echo.
pause
