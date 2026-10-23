@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 准备浏览器

set "PY=D:\WorkBuddy\tools\autohome_venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [提示] 未找到项目虚拟环境, 改用系统 python
    set "PY=python"
)

echo.
echo   正在把浏览器下载到项目目录 (约 150MB, 国内镜像)
echo   下载完成后, 这台电脑就不需要另外安装 Chrome 了。
echo.

"%PY%" setup_browser.py %*

echo.
pause
