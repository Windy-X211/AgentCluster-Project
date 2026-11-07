@echo off
setlocal
cd /d "%~dp0"

rem Clear ELECTRON_RUN_AS_NODE injected by IDE/CI host processes.
set "ELECTRON_RUN_AS_NODE="

rem ============================================================
rem  AgentCluster build script (ASCII only, keep CRLF line endings)
rem
rem    build.bat            rebuild frontend dist only
rem    build.bat install    npm install then rebuild dist
rem    build.bat exe        package Windows installer (electron-builder)
rem    build.bat all        npm install + rebuild dist + installer
rem    build.bat backend    syntax check backend python only
rem
rem  Env: NO_PAUSE=1 skips the final pause (useful for CI).
rem ============================================================

set "MODE=%~1"
if "%MODE%"=="" set "MODE=dist"

rem --- Locate Node ---
set "NODE_DIR="
if exist "%~dp0node-v20.18.0-win-x64\node.exe" set "NODE_DIR=%~dp0node-v20.18.0-win-x64"
if exist "%~dp0node\node.exe" set "NODE_DIR=%~dp0node"
if not defined NODE_DIR if exist "E:\app\node-v20.18.0-win-x64\node.exe" set "NODE_DIR=E:\app\node-v20.18.0-win-x64"
if not defined NODE_DIR if exist "C:\Program Files\nodejs\node.exe" set "NODE_DIR=C:\Program Files\nodejs"
if not defined NODE_DIR if exist "%LOCALAPPDATA%\Programs\nodejs\node.exe" set "NODE_DIR=%LOCALAPPDATA%\Programs\nodejs"
if not defined NODE_DIR for /f "delims=" %%i in ('where node 2^>nul') do if not defined NODE_DIR set "NODE_DIR=%%~dpi"
if defined NODE_DIR set "PATH=%NODE_DIR%;%PATH%"

rem --- Locate Python (for backend syntax check) ---
set "PYTHON=C:\Users\qiqi\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PYTHON%" for /f "delims=" %%i in ('where python 2^>nul') do set "PYTHON=%%i"
if not exist "%PYTHON%" set "PYTHON=python"

echo ============================================================
echo  AgentCluster build : %MODE%
echo ============================================================

if /i "%MODE%"=="backend" goto :backend

where node >nul 2>nul || goto :no_node
where npm  >nul 2>nul || goto :no_node
echo [build] node = 
node -v
echo [build] npm  = 
call npm -v

cd frontend || goto :error
if not exist "package.json" (
  echo [build] ERROR: frontend\package.json not found
  goto :error
)

rem --- Dependencies ---
if /i "%MODE%"=="install" goto :install
if /i "%MODE%"=="all" goto :install
if not exist "node_modules" goto :install
goto :build_dist

:install
echo [build] Installing npm dependencies...
call npm install || goto :error

:build_dist
if /i "%MODE%"=="exe" goto :package
if /i "%MODE%"=="install" goto :build_only
if /i "%MODE%"=="all" goto :build_only
goto :build_only

:build_only
echo [build] Building frontend (vue-tsc + vite)...
call npm run build || goto :error
if not exist "dist\index.html" (
  echo [build] ERROR: dist\index.html missing after build
  goto :error
)
echo [build] Frontend dist rebuilt successfully.
goto :backend

:package
echo [build] Packaging Windows installer (rebuilds dist via electron-builder)...
call npm run electron:build || goto :error
if not exist "release" (
  echo [build] WARNING: release directory not found
) else (
  echo [build] Installer output: frontend\release
  dir /b /o-d release
)

:backend
echo [build] Checking backend python syntax...
cd /d "%~dp0"
if not exist "backend\main.py" goto :error
pushd "%~dp0backend"
"%PYTHON%" -m compileall -q . || (popd & goto :error)
popd
if errorlevel 1 goto :error
echo [build] Backend syntax OK.

:done
cd /d "%~dp0frontend"
if /i not "%MODE%"=="backend" (
  echo [build] Latest dist assets:
  if exist "dist\assets" (
    powershell -NoProfile -Command "Get-ChildItem 'dist\assets' -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 5 Name,LastWriteTime | Format-Table -AutoSize"
  )
)
echo ============================================================
echo  BUILD OK
echo ============================================================
if /i "%MODE%"=="dist" echo Tip: restart the client or press Ctrl+R to load the new dist.
if not "%NO_PAUSE%"=="1" pause
endlocal
exit /b 0

:no_node
echo [build] ERROR: Node.js / npm not found in PATH.
if not "%NO_PAUSE%"=="1" pause
endlocal
exit /b 1

:error
echo ============================================================
echo  BUILD FAILED
echo ============================================================
if not "%NO_PAUSE%"=="1" pause
endlocal
exit /b 1