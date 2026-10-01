@echo off
chcp 65001 >nul
REM 用法：
REM   run.bat            启动。同端口已有实例则取消本次启动，不动旧进程。
REM   run.bat restart    一键重启。先按三重判定安全结束本端口的旧实例，再以新代码启动。
REM 判定逻辑在 server.py（认不出是 JobForge / PID 对不上 / 正在抓取或批量分析，一律只提示不动手）。
set "EXTRA="
if /I "%~1"=="restart" set "EXTRA=--restart"

echo ============================================
echo   JobForge 求职工作台 - 启动中...
echo ============================================
echo.

REM 代码在 src\jobforge（包），运行时数据在 data\；PYTHONPATH 让 python -m 找到包
set "PYTHONPATH=%~dp0src"

REM 优先使用虚拟环境，否则用系统 Python
if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" -m jobforge.server %EXTRA%
) else (
    python -m jobforge.server %EXTRA%
)

pause
