"""
⚠️ 废弃：此文件已被 Electron BrowserWindow 取代。

旧架构：start_tray.py → app_window.py → pywebview 包装 5173
新架构：Electron main.cjs → BrowserWindow (frameless + 自绘标题栏)

请使用：
  start.bat              （生产模式，Electron + load dist/）
  start.bat dev          （开发模式，Vite + Electron 并行）
  cd frontend && npm run electron
  cd frontend && npm run electron:dev

窗口控制 API 已从 pywebview.api 迁移到 Electron IPC：
  见 frontend/src/utils/env.ts 的 windowAPI 统一适配层
"""
import sys
print("[AgentCluster] app_window.py 已废弃，请使用 Electron 启动 (start.bat)")
sys.exit(0)
