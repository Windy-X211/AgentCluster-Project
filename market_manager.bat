@echo off
rem 独立启动「市场管理」程序（不依赖主程序 / Electron）
cd /d "%~dp0"

set "PYW=pythonw"
where pythonw >nul 2>nul || set "PYW=python"

start "" %PYW% "%~dp0market_manager.pyw"