# -*- coding: utf-8 -*-
"""
⚠️ 废弃：此文件已被 Electron 内嵌 splash 窗口取代。

Electron main.cjs 里的 createSplash() 函数已实现相同功能
（frameless + 透明 + 进度条 + alwaysOnTop）。
启动中窗口特效现在由 Electron 主进程在 App ready 时自动创建/关闭。
"""
import sys
print("[AgentCluster] launch_splash.py 已废弃，启动中窗口由 Electron 主进程接管")
sys.exit(0)
