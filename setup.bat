@echo off
chcp 65001 >nul
echo ============================================
echo   JobForge 求职工作台 - 一键安装脚本
echo ============================================
echo.

REM 检查 Python 是否安装
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+
    echo 下载地址: https://www.python.org/downloads/
    echo 安装时请勾选 "Add Python to PATH"
    pause
    exit /b 1
)

echo [1/3] Python 版本:
python --version
echo.

echo [2/3] 创建虚拟环境...
if not exist venv (
    python -m venv venv
    echo 虚拟环境创建完成
) else (
    echo 虚拟环境已存在，跳过
)
echo.

echo [3/3] 安装依赖...
call venv\Scripts\activate.bat
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.

echo ============================================
echo   安装完成！双击 run.bat 启动服务
echo ============================================
pause
