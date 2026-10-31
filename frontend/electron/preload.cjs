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
  // 精灵悬浮球拓展（fairy_ball）：球窗/小对话窗/选区窗共用的桥
  fairy: {
    /** 打开主窗口（route 可选，如 '#/settings'） */
    openMain: (route) => ipcRenderer.invoke('fairy:openMain', route),
    /** 菜单动作：screenshot | quickchat | settings */
    action: (name, payload) => ipcRenderer.invoke('fairy:action', name, payload),

    /** 当前窗口位置（拖动计算用） */
    winPos: () => ipcRenderer.invoke('fairy:winPos'),
    /** 移动当前窗口到屏幕坐标 */
    moveTo: (x, y) => ipcRenderer.invoke('fairy:moveTo', x, y),
    /** 关闭当前窗口（frameless 自绘关闭按钮用） */
    closeWin: () => ipcRenderer.invoke('fairy:closeWin'),
  },
  // 区域截图选区窗桥
  snip: {
    /** 主进程把全屏截图 dataURL 发来（选区窗就绪后） */
    onImage: (callback) => {
      ipcRenderer.on('snip:image', (_e, dataUrl) => callback(dataUrl))
    },
    /** 选区完成/取消：{target:'main'|'quick'|'cancel', rect:{x,y,w,h}} */
    choose: (data) => ipcRenderer.invoke('snip:choose', data),
  },
  // 事件监听（主窗口最大化状态同步 / 精灵球截图提问 / 小窗补充截图）
  on: (channel, callback) => {
    const validChannels = ['win:maximized', 'fairy:shot', 'quick:shot']
    if (validChannels.includes(channel)) {
      ipcRenderer.on(channel, (_e, ...args) => callback(...args))
    }
  },
})
