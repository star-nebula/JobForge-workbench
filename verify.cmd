@echo off
chcp 65001 >nul
REM JobForge 一键验证入口（Windows）。参数原样转发给 scripts\verify.py，退出码透传。
REM   verify.cmd            全量：依赖预检 + pytest + 真实 HTTP 冒烟
REM   verify.cmd --fast     只跑 HTTP 冒烟
REM   verify.cmd -v         展开输出
setlocal
pushd "%~dp0"

if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" "%~dp0scripts\verify.py" %*
) else (
    python "%~dp0scripts\verify.py" %*
)
set "RC=%ERRORLEVEL%"

popd
exit /b %RC%
