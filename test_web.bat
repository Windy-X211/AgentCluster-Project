@echo off
cd /d "%~dp0frontend"
echo WEB mode starting (simple test)...

set "PYTHON=C:\Users\qiqi\AppData\Local\Programs\Python\Python314\python.exe"

echo Starting backend...
start "" "%PYTHON%" ..\backend\main.py
echo Backend PID: 55772

timeout /t 3 >nul

echo Starting dist server...
start "" node electron\serve-dist.cjs .\dist 5173

timeout /t 2 >nul

echo Opening browser...
start http://localhost:5173

echo Done.
exit /b 0