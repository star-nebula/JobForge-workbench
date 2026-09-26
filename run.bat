@echo off
chcp 65001 >nul
echo ============================================
echo   JobForge 求职工作台 - 启动中...
echo ============================================
echo.

REM 优先使用虚拟环境，否则用系统 Python
if exist venv\Scripts\python.exe (
    venv\Scripts\python.exe server.py
) else (
    python server.py
)

pause
