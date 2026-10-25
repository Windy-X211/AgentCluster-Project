@echo off
cd /d "%~dp0"

rem ============================================================
rem  AgentCluster Launcher  --  single entry for Electron app
rem    start.bat          = production (load dist/)
rem    start.bat dev      = development (Vite + Electron parallel)
rem ============================================================

rem --- SELF-MINIMIZE: hide the cmd window that launches this bat ---
rem First run: launch ourselves minimized and exit immediately
rem Second run (minimized): do the actual work
if not "%~1"=="__hidden__" (
  start /min "" cmd /c ""%~f0" __hidden__ %*"
  exit /b 0
)
rem shift off the __hidden__ marker
shift

rem --- Locate Node ---
set NODE_DIR=
if exist "%~dp0node-v20.18.0-win-x64\node.exe" set NODE_DIR=%~dp0node-v20.18.0-win-x64
if exist "%~dp0node\node.exe" set NODE_DIR=%~dp0node
if defined NODE_DIR set "PATH=%NODE_DIR%;%PATH%"

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
start "" node_modules\electron\dist\electron.exe . --prod
goto :end

:error
echo [AgentCluster] FAILED to start
timeout /t 3 >nul
exit /b 1

:end
exit /b 0
