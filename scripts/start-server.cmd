@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 找不到 .venv\Scripts\python.exe，请先在仓库根目录创建虚拟环境
  pause
  exit /b 1
)

echo ============================================
echo  1/3  先自检 NapCat 连通性
echo ============================================
".venv\Scripts\python.exe" scripts\check_napcat.py
echo.

echo ============================================
echo  2/3  停止旧的 8080 进程
echo ============================================
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8080" ^| findstr "LISTENING"') do (
  echo   杀掉旧的进程 PID %%p
  taskkill /f /pid %%p >nul 2>&1
)
timeout /t 1 >nul
echo  完成

echo.
echo ============================================
echo  3/3  启动服务（含日报调度器）
echo  Ctrl+C 可停止
echo ============================================
".venv\Scripts\python.exe" -m server.run

echo.
echo 服务已退出。
pause
