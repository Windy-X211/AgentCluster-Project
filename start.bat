@echo off
cd /d "%~dp0"

rem Clear ELECTRON_RUN_AS_NODE injected by IDE/CI, otherwise Electron runs as Node
rem and main.cjs fails with "Cannot read properties of undefined (reading 'commandLine')".
set "ELECTRON_RUN_AS_NODE="

rem ============================================================
rem  AgentCluster Launcher
rem    start.bat          = production / reads .launch_mode.txt
rem    start.bat dev      = development (Vite + Electron)
rem    start.bat web      = force web mode
rem    start.bat client   = force client mode
rem ============================================================

rem --- SELF-MINIMIZE ---
if not "%~1"=="__hidden__" (
  start /min "" cmd /c ""%~f0" __hidden__ %*"
  exit /b 0
)
shift

rem --- Find Python ---
set "PYTHON=C:\Users\qiqi\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PYTHON%" for /f "delims=" %%i in ('where python 2^>nul') do set "PYTHON=%%i"
if not exist "%PYTHON%" set "PYTHON=python"

rem --- Find Node ---
set NODE_DIR=
if exist "%~dp0node-v20.18.0-win-x64\node.exe" set NODE_DIR=%~dp0node-v20.18.0-win-x64
if exist "%~dp0node\node.exe" set NODE_DIR=%~dp0node
rem fixed dev machine location
if "%NODE_DIR%"=="" if exist "E:\app\node-v20.18.0-win-x64\node.exe" set NODE_DIR=E:\app\node-v20.18.0-win-x64
rem common system locations
if "%NODE_DIR%"=="" if exist "C:\Program Files\nodejs\node.exe" set NODE_DIR=C:\Program Files\nodejs
if "%NODE_DIR%"=="" if exist "%LOCALAPPDATA%\Programs\nodejs\node.exe" set NODE_DIR=%LOCALAPPDATA%\Programs\nodejs
rem nvm-windows
if "%NODE_DIR%"=="" if exist "%APPDATA%\nvm" (
  for /d %%v in ("%APPDATA%\nvm\*") do if "%%~nxv" NEQ "" if exist "%%v\node.exe" set NODE_DIR=%%v
)
rem last fallback: where node
if "%NODE_DIR%"=="" for /f "delims=" %%i in ('where node 2^>nul') do if not defined NODE_DIR set NODE_DIR=%%~dpi
if defined NODE_DIR set "PATH=%NODE_DIR%;%PATH%"

rem --- Determine launch mode (BEFORE cd frontend) ---
set LAUNCH_MODE=client
if /i "%1"=="dev" goto :dev
if /i "%1"=="web" goto :web
if /i "%1"=="client" goto :prod

rem Read .launch_mode.txt from project root (current dir is still project root)
if exist ".launch_mode.txt" (
  for /f "usebackq delims=" %%a in (".launch_mode.txt") do set LAUNCH_MODE=%%a
)
if "%LAUNCH_MODE%"=="web" goto :web

cd frontend

rem --- Check electron binary (prod/web only) ---
if not exist "node_modules\electron\dist\electron.exe" (
  echo [AgentCluster] Downloading Electron binary...
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://npmmirror.com/mirrors/electron/v33.4.11/electron-v33.4.11-win32-x64.zip' -OutFile '%TEMP%\electron.zip'; if (-not (Test-Path 'node_modules/electron/dist')) { Expand-Archive '%TEMP%\electron.zip' 'node_modules/electron/dist' -Force }; Remove-Item '%TEMP%\electron.zip' -Force"
)
goto :prod

:dev
echo [AgentCluster] DEV mode...
echo Dev browser:  http://localhost:5173
echo Backend api:  http://127.0.0.1:8870/api/health
cd frontend
powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c npm run electron:dev' -WorkingDirectory '%cd%' -WindowStyle Hidden"
goto :end

:prod
echo [AgentCluster] PROD mode (client)...
if not exist "dist\index.html" (
  echo [AgentCluster] Building frontend...
  call npm run build || goto :error
)
start "" /b node_modules\electron\dist\electron.exe . --prod
goto :end

:web
echo [AgentCluster] WEB mode...
cd frontend
if not exist "dist\index.html" (
  echo [AgentCluster] Building frontend...
  call npm run build || goto :error
)
rem 1. Backend
powershell -NoProfile -Command "Start-Process '%PYTHON%' -ArgumentList '..\backend\main.py' -WorkingDirectory '%cd%' -WindowStyle Hidden"
timeout /t 4 >nul
rem 2. Dist server
powershell -NoProfile -Command "Start-Process 'node' -ArgumentList 'electron\serve-dist.cjs','.\dist','5173' -WorkingDirectory '%cd%' -WindowStyle Hidden"
timeout /t 1 >nul
rem 3. Tray-only Electron
start "" node_modules\electron\dist\electron.exe . --tray-only
rem 4. Browser
start http://localhost:5173
goto :end

:error
echo [AgentCluster] FAILED
timeout /t 3 >nul
exit /b 1

:end
exit /b 0