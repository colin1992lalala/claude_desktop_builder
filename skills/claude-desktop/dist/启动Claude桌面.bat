@echo off
chcp 65001 >nul
title Claude Desktop Dashboard
echo ============================================
echo    Claude Desktop Dashboard 启动器
echo ============================================
echo.

REM 定位到脚本所在目录
cd /d "%~dp0"

REM 使用项目自带的 Python（可移植：依次找 上级目录python312 / 同目录python / PATH 中 python）
set PY=python
if exist "%~dp0..\python312\python.exe" (
    set "PY=%~dp0..\python312\python.exe"
) else if exist "%~dp0..\..\python312\python.exe" (
    set "PY=%~dp0..\..\python312\python.exe"
) else if exist "%~dp0python\python.exe" (
    set "PY=%~dp0python\python.exe"
)

REM 检查端口是否已被占用
netstat -ano | findstr ":8723" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [提示] 服务已在运行，直接打开浏览器...
    start http://127.0.0.1:8723
    timeout /t 3 >nul
    exit
)

echo [步骤 1/2] 启动本地服务 (http://127.0.0.1:8723) ...
start "Claude Desktop Server" cmd /k "%PY%" server.py --port 8723

echo [步骤 2/2] 等待服务就绪并在浏览器中打开...
timeout /t 2 >nul

REM 等待端口就绪
:wait
netstat -ano | findstr ":8723" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    timeout /t 1 >nul
    goto wait
)

start "" http://127.0.0.1:8723
echo [完成] 仪表盘已在浏览器中打开。
echo        关闭本窗口不会停止服务；停止服务请关闭 "Claude Desktop Server" 窗口。
echo.
timeout /t 5 >nul
