/**
 * Preload 脚本 —— 通过 contextBridge 暴露安全 API 给前端
 *
 * 前端调用方式：
 *   window.electronAPI.window.minimize()
 *   window.electronAPI.window.toggleMax()
 *   window.electronAPI.window.setBounds(x, y, w, h)
 *   ...
 *
 * 比 pywebview.api 更清晰的命名空间，同时保持 pywebview 风格的 camelCase
 */

const { contextBridge, ipcRenderer } = require('electron')

// 后端端口（和 main.cjs 里的 BACKEND_PORT 保持一致）
const BACKEND_PORT = 8870

contextBridge.exposeInMainWorld('electronAPI', {
  // ⚠️ API 基础地址（prod 模式下 file:// 协议 fetch('/api/...') 会变成 file:///api/...，
  // 必须用绝对 URL 才能打到后端！）
  apiBase: `http://127.0.0.1:${BACKEND_PORT}`,
  backendPort: BACKEND_PORT,

  // 主窗口显示信号：Vue 数据加载完后调这个，主进程才 show 窗口（防白屏闪烁）
  notifyReady: () => ipcRenderer.send('app:ready'),

  // 启动中窗口进度（内部用，splash 窗口自己监听）
  onSplashStep: (callback) => {
    ipcRenderer.on('splash:step', (_e, text, pct) => callback(text, pct))
  },
  // 窗口控制
  window: {
    minimize: () => ipcRenderer.invoke('win:minimize'),
    toggleMax: () => ipcRenderer.invoke('win:toggleMax'),
    maximize: () => ipcRenderer.invoke('win:maximize'),
    restore: () => ipcRenderer.invoke('win:restore'),
    close: () => ipcRenderer.invoke('win:close'),
    getBounds: () => ipcRenderer.invoke('win:getBounds'),
    setBounds: (x, y, w, h) => ipcRenderer.invoke('win:setBounds', { x, y, width: w, height: h }),
    resize: (w, h) => ipcRenderer.invoke('win:resize', w, h),
    move: (x, y) => ipcRenderer.invoke('win:move', x, y),
    isMaximized: () => ipcRenderer.invoke('win:isMaximized'),
  },
  // 后端状态
  api: {
    health: () => ipcRenderer.invoke('api:health'),
  },
  // 事件监听（主窗口最大化状态同步）
  on: (channel, callback) => {
    const validChannels = ['win:maximized']
    if (validChannels.includes(channel)) {
      ipcRenderer.on(channel, (_e, ...args) => callback(...args))
    }
  },
})
