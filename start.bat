@echo off
cd /d "%~dp0"

rem ============================================================
rem  AgentCluster Launcher  --  single entry for Electron app
rem    start.bat          = production (load dist/)
rem    start.bat dev      = development (Vite + Electron parallel)
rem  窗口隐藏由 launcher.vbs 的 SW_HIDE 负责
rem ============================================================

rem --- 依赖 PATH 中的 node/npm（请确保系统已配置 Node.js 环境变量）---

cd frontend

rem --- Check electron binary ---
if not exist "node_modules\electron\dist\electron.exe" (
  echo [AgentCluster] First run, downloading Electron binary...
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://npmmirror.com/mirrors/electron/v33.4.11/electron-v33.4.11-win32-x64.zip' -OutFile '%TEMP%\electron.zip'; if (-not (Test-Path 'node_modules\electron\dist')) { Expand-Archive '%TEMP%\electron.zip' 'node_modules\electron\dist' -Force }; Remove-Item '%TEMP%\electron.zip' -Force"
)

if /i "%1"=="dev" goto :dev
goto :prod

:dev
echo [AgentCluster] DEV mode starting...
echo Dev browser:  http://localhost:5173
echo Backend api:  http://127.0.0.1:8870/api/health
start "AgentCluster Dev" cmd /c "npm run electron:dev"
goto :end

:prod
echo [AgentCluster] PROD mode starting...
if not exist "dist\index.html" (
  echo [AgentCluster] dist missing, building frontend...
  call npm run build || goto :error
)
start "" /b node_modules\electron\dist\electron.exe . --prod
goto :end

:error
echo [AgentCluster] FAILED to start
timeout /t 3 >nul
exit /b 1

:end
exit /b 0
