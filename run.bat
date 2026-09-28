@echo off
chcp 65001 >nul
echo ============================================
echo   JobForge 求职工作台 - 启动中...
echo ============================================
echo.

REM 代码在 src\jobforge（包），运行时数据在 data\；PYTHONPATH 让 python -m 找到包
set "PYTHONPATH=%~dp0src"

REM 优先使用虚拟环境，否则用系统 Python
if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" -m jobforge.server
) else (
    python -m jobforge.server
)

pause
