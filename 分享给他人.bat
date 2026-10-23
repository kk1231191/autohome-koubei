@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 分享口碑抓取台到公网

set "PY=D:\WorkBuddy\tools\autohome_venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [提示] 未找到项目虚拟环境, 改用系统 python
    set "PY=python"
)

echo.
echo   正在启动控制台并建立公网隧道, 请稍候...
echo   窗口会打印一个 https 分享链接, 直接发给对方即可。
echo   关闭本窗口 = 停止分享, 链接立即失效。
echo.

"%PY%" share.py

echo.
echo 分享已结束。
pause
