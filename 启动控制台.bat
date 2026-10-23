@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 汽车之家口碑抓取控制台

set "PY=D:\WorkBuddy\tools\autohome_venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [提示] 未找到项目虚拟环境, 改用系统 python
    set "PY=python"
)

echo.
echo   正在启动控制台, 稍后浏览器会自动打开...
echo   关闭本窗口即停止服务。
echo.

start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8760"
"%PY%" web.py

echo.
echo 服务已退出。
pause
