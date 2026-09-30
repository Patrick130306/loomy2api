@echo off
rem ---------------------------------------------------------------------------
rem loomy2api - stop script：按端口找出监听进程并结束
rem ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
set "PORT=17890"

set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    echo 结束 PID %%p ...
    taskkill /PID %%p /F >nul 2>&1
    set "FOUND=1"
)

if not defined FOUND (
    echo 端口 %PORT% 上没有监听进程，loomy2api 可能本来就没在跑
) else (
    echo loomy2api 已停止
)
endlocal
