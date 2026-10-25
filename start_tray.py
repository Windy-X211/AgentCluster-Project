"""
⚠️ 废弃：此文件已被 Electron 主进程完全取代。

旧架构的职责拆分：
  1. pystray 托盘图标  →  Electron Tray + Menu
  2. start_backend()   →  Electron main.cjs 的 startBackend() (spawn python main.py)
  3. start_frontend()  →  Electron dev 模式 electron:dev（npm run dev + electron 并行）
  4. app_window.py 独立窗口 →  Electron BrowserWindow (frameless)
  5. 单例互斥量          →  Electron app.requestSingleInstanceLock()

统一入口：
  start.bat              （生产模式，Electron + 自动拉起后端）
  start.bat dev          （开发模式，Vite + Electron 并行）
  cd frontend && npm run electron
  cd frontend && npm run electron:dev
"""
import sys
print("[AgentCluster] start_tray.py 已废弃，请使用 start.bat 或 cd frontend && npm run electron")
sys.exit(0)
