@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "%~dp0start_tray.py"
) else (
    start "" python "%~dp0start_tray.py"
)
echo AgentCluster launcher started
echo Frontend: http://localhost:5173
echo Backend:  http://127.0.0.1:8870
timeout /t 2 >nul
