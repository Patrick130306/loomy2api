@echo off
rem ---------------------------------------------------------------------------
rem loomy2api - Windows startup script
rem 直接运行本脚本，或由「任务计划程序」在登录时调用。
rem 服务自己写日志到 logs\gateway.log，这里不需要控制台重定向。
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist "logs" mkdir "logs"

set "PY=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo 启动 loomy2api（Ctrl+C 停止，或运行 stop.cmd）
echo   面板: http://127.0.0.1:17890/panel
echo   日志: %~dp0logs\gateway.log

"%PY%" -m loomy2api serve
endlocal
