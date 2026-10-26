/**
 * Electron 主进程 —— 替代 start_tray.py + app_window.py
 *
 * 职责：
 *  1. 创建 frameless BrowserWindow，内嵌前端 dist（生产）或开发服务器 URL（dev）
 *  2. 通过 Tray 提供系统托盘（替代 pystray）
 *  3. 通过 child_process 拉起 Python FastAPI 后端（替代 start_tray.py 的 start_backend）
 *  4. 单例互斥量（只允一个实例跑）
 *  5. IPC 注册：窗口控制 / 后端健康检查
 *
 * 与 pywebview 方案的关键差异：
 *  - 窗口控制 API 从 pywebview.api → Electron BrowserWindow API
 *  - 拖动区从 .pywebview-drag-region → -webkit-app-region: drag
 *  - 边缘缩放不再需要（Electron frameless 在 Win11 上自带 native resize）
 */

// === 调试日志（写到项目根目录） ===
const _fs = require('fs')
const _path = require('path')
const _LOG = _path.join(__dirname, '..', '..', 'electron-main.log')
function _log(m) { try { _fs.appendFileSync(_LOG, '[' + new Date().toISOString() + '] ' + m + '\n') } catch {} }
_log('=== Electron main.cjs starting ===')
process.on('uncaughtException', (e) => { _log('UNCAUGHT: ' + e.message); _log(e.stack || '') })
process.on('unhandledRejection', (e) => { _log('REJECTION: ' + (e?.message || e)) })

const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, shell, screen, dialog } = require('electron')
const path = require('path')
const fs = require('fs')
const { spawn } = require('child_process')

_log('app.isPackaged=' + app.isPackaged + ' argv=' + JSON.stringify(process.argv.slice(2)))

// ---------- 配置 ----------
// app.isPackaged: electron-builder 打包后自动为 true
// --prod 命令行参数: 直接跑 electron . 时强制用 dist/（开发机调试用）
const FORCE_PROD = process.argv.includes('--prod')
const TRAY_ONLY = process.argv.includes('--tray-only')
const IS_DEV = !app.isPackaged && !FORCE_PROD
const ROOT = path.resolve(__dirname, '..')               // frontend/ (开发) 或 app.asar/ (打包)
const PROJECT_ROOT = path.resolve(ROOT, '..')           // 项目根（backend/ 在这里）
// 后端端口：
//   开发/--prod 调试：直接用项目根/backend
//   真正打包后：resourcesPath/backend（通过 electron-builder extraResources 拷过去）
const BACKEND_DIR = path.join(PROJECT_ROOT, 'backend')
const BACKEND_PORT = 8870
const DIST_PORT = 5173        // prod 模式也用 5173 服务 dist → 浏览器也能访问
const DEV_URL = 'http://127.0.0.1:5173'
const PROD_URL = `http://127.0.0.1:${DIST_PORT}`
const PRELOAD = path.join(__dirname, 'preload.cjs')

// 进程句柄
let backendProc = null
let distServerProc = null
let mainWindow = null

// ---------- 后端进程管理 ----------

function _killPortIfBusy(port) {
  // 简化版：用 netstat 找到监听该端口的 PID，taskkill 掉
  try {
    const out = require('child_process').execSync(`netstat -ano | findstr :${port} | findstr LISTENING`, { encoding: 'utf8', timeout: 3000 })
    const lines = out.trim().split(/\r?\n/)
    for (const line of lines) {
      const parts = line.trim().split(/\s+/)
      const pid = parseInt(parts[parts.length - 1], 10)
      if (!pid || pid === process.pid) continue
      _log(`端口 ${port} 被 PID=${pid} 占用，执行 taskkill`)
      try {
        require('child_process').execSync(`taskkill /F /PID ${pid}`, { encoding: 'utf8', timeout: 3000 })
      } catch {}
    }
    // 等端口释放
    for (let i = 0; i < 10; i++) {
      try {
        require('child_process').execSync(`netstat -ano | findstr :${port} | findstr LISTENING`, { encoding: 'utf8', timeout: 1000 })
        require('child_process').execSync('timeout /t 0.3', { encoding: 'utf8', timeout: 1000 })
      } catch { break }
    }
  } catch {
    // netstat 找不到 LISTENING 行 → 端口空闲，正常
  }
}

// 检测系统 Node.js（Electron 内置 Node 不能 spawn 外部 http server，会 ACCESS_VIOLATION）
function _detectSystemNode() {
  // Electron 内置 Node 不能 spawn 外部 http server（会 ACCESS_VIOLATION），
  // 必须用系统 Node.js；纯走 PATH，保证跨设备可用
  try {
    const where = require('child_process').execSync('where node', { encoding: 'utf8', timeout: 2000 }).trim()
    const first = where.split('\n')[0].trim()
    if (first && fs.existsSync(first)) {
      _log('Found system node via PATH: ' + first)
      return first
    }
  } catch {
    _log('where node failed, PATH 中找不到 Node.js')
  }
  return null
}

function _detectPython() {
  // 优先项目内 venv（相对路径，跨设备可用），否则走 PATH
  const venvCandidates = [
    path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe'),
    path.join(PROJECT_ROOT, 'venv', 'Scripts', 'python.exe'),
  ]
  for (const p of venvCandidates) {
    if (fs.existsSync(p)) {
      _log('Found venv python: ' + p)
      return p
    }
  }
  // 走 PATH（会用用户系统配置的 python）
  _log('Falling back to PATH "python"')
  return 'python'
}

function startBackend() {
  return new Promise((resolve, reject) => {
    // ---- 端口冲突防御 ----
    // 如果 8870 已在监听，先查占用者 PID：若是我们自己上次残留的 backend，先 kill 再重启
    _killPortIfBusy(BACKEND_PORT)

    const python = _detectPython()
    _log('startBackend: python=' + python + ' cwd=' + BACKEND_DIR)

    backendProc = spawn(python, ['-u', 'main.py'], {
      cwd: BACKEND_DIR,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      shell: false,
      env: process.env,
    })

    _log('Backend spawned PID=' + backendProc.pid)

    // 简单日志：把 stdout/stderr 追加写文件
    const logPath = path.join(BACKEND_DIR, 'electron_backend.log')
    backendProc.stdout?.on('data', (buf) => fs.appendFileSync(logPath, buf.toString()))
    backendProc.stderr?.on('data', (buf) => fs.appendFileSync(logPath, buf.toString()))

    backendProc.on('error', (err) => {
      _log('Backend spawn ERROR: ' + err.message)
      reject(err)
    })

    backendProc.on('exit', (code) => {
      _log('Backend exit code=' + code)
      backendProc = null
    })

    // 等待端口就绪（最多 25s）
    let waited = 0
    const interval = setInterval(async () => {
      waited += 0.5
      try {
        const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/health`, { signal: AbortSignal.timeout(1000) })
        if (res.ok) {
          clearInterval(interval)
          console.log(`[Electron] 后端就绪 (PID=${backendProc?.pid})`)
          resolve()
        }
      } catch {}
      if (waited > 25) {
        clearInterval(interval)
        console.warn('[Electron] 后端启动超时')
        reject(new Error('backend timeout'))
      }
    }, 500)
  })
}

function stopBackend() {
  if (!backendProc) return
  try {
    backendProc.kill('SIGTERM')
    setTimeout(() => {
      if (backendProc && !backendProc.killed) {
        try { backendProc.kill('SIGKILL') } catch {}
      }
    }, 3000)
  } catch {}
  backendProc = null
}

// ---------- Prod 模式静态文件服务器 ----------
// 用 Node.js 脚本服务 dist/ 到 localhost:5173（内置 SPA fallback）
// 好处：Electron 和浏览器都能通过 HTTP 访问，避开 file:// 的坑
//       子路径（/settings、/agents）→ 自动回 index.html，Vue Router 接管
function startDistServer() {
  return new Promise((resolve, reject) => {
    const distDir = path.join(ROOT, 'dist')
    const distIndex = path.join(distDir, 'index.html')
    if (!fs.existsSync(distIndex) || fs.statSync(distIndex).size < 100) {
      reject(new Error('dist/index.html 无效（请先 npm run build）'))
      return
    }

    // ---- 端口冲突防御 ----
    // 如果 5173 已在监听（比如用户已经开了 dev 模式 Vite），直接用现成的就行
    try {
      const existing = require('child_process').execSync(
        `netstat -ano | findstr :${DIST_PORT} | findstr LISTENING`,
        { encoding: 'utf8', timeout: 2000 }
      )
      if (existing && existing.trim()) {
        _log(`端口 ${DIST_PORT} 已被占用（可能是 Vite dev server），复用`)
        resolve()
        return
      }
    } catch {
      // 端口空闲，正常启动
    }

    // 用系统 Node.js（Electron 内置 Node spawn 外部脚本会 ACCESS_VIOLATION）
    // 按优先级找：项目本地 node > 用户目录 node > 环境变量 node
    let nodeBin = _detectSystemNode()
    if (!nodeBin) {
      _log('⚠️ 找不到系统 Node.js，dist server 启动失败')
      reject(new Error('找不到 Node.js（请安装 Node.js 或检查 PATH）'))
      return
    }
    const serveScript = path.join(__dirname, 'serve-dist.cjs')
    _log(`startDistServer: node=${nodeBin} script=${serveScript} port=${DIST_PORT}`)

    distServerProc = require('child_process').spawn(
      nodeBin, [serveScript, distDir, String(DIST_PORT)],
      { 
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,  // ← 隐藏 Node.js 黑窗口
        shell: false,
      }
    )

    distServerProc.stdout.on('data', d => _log('[dist-server] ' + d.toString().trim()))
    distServerProc.stderr.on('data', d => _log('[dist-server-err] ' + d.toString().trim()))
    distServerProc.on('exit', (code) => {
      _log('Dist server exit code=' + code)
      distServerProc = null
    })

    // 等待端口就绪（最多 10s）
    let waited = 0
    const interval = setInterval(async () => {
      waited += 0.5
      try {
        const res = await fetch(`http://127.0.0.1:${DIST_PORT}/index.html`, { signal: AbortSignal.timeout(1000) })
        if (res.ok) {
          clearInterval(interval)
          _log(`✅ Dist server 就绪 (PID=${distServerProc?.pid}, port=${DIST_PORT})`)
          resolve()
        }
      } catch {}
      if (waited > 10) {
        clearInterval(interval)
        reject(new Error('dist server timeout'))
      }
    }, 500)
  })
}

function stopDistServer() {
  if (!distServerProc) return
  try { distServerProc.kill('SIGTERM') } catch {}
  distServerProc = null
}

// ---------- 启动中窗口（简化版） ----------

// ---------- 启动中特效窗口 ----------

let _splashWindow = null

function createSplash() {
  if (_splashWindow) return _splashWindow

  // 图标路径（与原 launch_splash.py 一致）
  const iconPaths = [
    path.join(ROOT, 'public', 'icon.png'),
    path.join(ROOT, 'public', 'icon.jpg'),
    path.join(PROJECT_ROOT, '1791461860623.webp.jpg'),
  ]
  let iconDataUrl = ''
  for (const p of iconPaths) {
    if (fs.existsSync(p)) {
      try {
        const img = nativeImage.createFromPath(p)
        if (!img.isEmpty()) {
          iconDataUrl = img.resize({ width: 144, height: 144 }).toDataURL()
          break
        }
      } catch {}
    }
  }

  _splashWindow = new BrowserWindow({
    width: 520, height: 340,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    show: false,
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })

  const iconHtml = iconDataUrl
    ? `<img src="${iconDataUrl}" width="72" height="72" style="border-radius:18px;background:rgba(91,140,255,0.12);padding:10px;box-shadow:0 8px 24px rgba(91,140,255,0.25)">`
    : `<div style="width:92px;height:92px;border-radius:22px;background:linear-gradient(135deg,#5b8cff,#a78bfa);display:flex;align-items:center;justify-content:center;font-size:42px;color:#fff;font-weight:700">✦</div>`

  const splashHtml = `<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;background:transparent;width:100%;height:100%;overflow:hidden}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;color:#eaf1ff;user-select:none}
.card{position:absolute;inset:4px;border-radius:26px;background:#0f1626;border:1px solid #243450;box-shadow:0 20px 60px rgba(0,0,0,0.5),0 0 80px rgba(91,140,255,0.1);overflow:hidden}
.glow-top{position:absolute;left:40px;right:40px;top:34px;height:46px;background:linear-gradient(180deg,rgba(91,140,255,0.22),transparent);border-radius:8px}
.close-x{position:absolute;right:18px;top:10px;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:18px;color:#94a8cf;cursor:pointer;border-radius:8px;transition:background 0.15s}
.close-x:hover{background:rgba(255,100,100,0.15);color:#ff9a9a}
.content{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:30px}
.icon-wrap{margin-bottom:18px;animation:float 3s ease-in-out infinite}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}
.title{font-size:16px;font-weight:600;letter-spacing:0.5px;margin-bottom:4px}
.subtitle{font-size:11px;color:#94a8cf;margin-bottom:18px}
.step{font-size:13px;color:#c9d6ef;margin-bottom:10px;min-height:18px;transition:opacity 0.3s}
.progress{width:280px;height:3px;background:rgba(255,255,255,0.08);border-radius:2px;overflow:hidden;position:relative}
.progress-bar{height:100%;width:12%;background:linear-gradient(90deg,#5b8cff,#a78bfa);border-radius:2px;transition:width 0.4s ease-out}
.progress-bar.done{background:linear-gradient(90deg,#3ddc97,#5bf0b0)}
.bottom-text{position:absolute;bottom:18px;left:0;right:0;text-align:center;font-size:11px;color:rgba(148,168,207,0.5)}
.spinner{position:absolute;top:-1px;left:0;width:12%;height:100%;background:linear-gradient(90deg,transparent,rgba(255,255,255,0.3),transparent);animation:spin 1.6s ease-in-out infinite}
@keyframes spin{0%{transform:translateX(-100%)}100%{transform:translateX(1000%)}}
.fade-in{animation:fadeIn 0.32s ease-out}
@keyframes fadeIn{from{opacity:0;transform:scale(0.97)}to{opacity:1;transform:scale(1)}}
</style></head><body>
<div class="card fade-in" id="card">
  <div class="glow-top"></div>
  <div class="close-x" id="closeX">×</div>
  <div class="content">
    <div class="icon-wrap">${iconHtml}</div>
    <div class="title">AgentCluster 启动中</div>
    <div class="subtitle">智能体集群 · 桌面客户端</div>
    <div class="step" id="step">正在初始化启动器...</div>
    <div class="progress"><div class="progress-bar" id="bar"><div class="spinner" id="spinner"></div></div></div>
  </div>
  <div class="bottom-text">ESC 关闭</div>
</div>
<script>
const stepEl=document.getElementById('step'),barEl=document.getElementById('bar'),spinnerEl=document.getElementById('spinner'),closeX=document.getElementById('closeX');
let _progress=0,_done=false;
// ESC/右键/× 关闭
document.addEventListener('keydown',e=>{if(e.key==='Escape')window.close()});
document.addEventListener('contextmenu',e=>{e.preventDefault();window.close()});
closeX.addEventListener('click',()=>window.close());

window.electronAPI?.onSplashStep?.((text,pct)=>{
  if(!text)return;
  if(text.startsWith('DONE')){
    _done=true;
    stepEl.textContent='✅ 启动完成';
    stepEl.style.color='#3ddc97';
    barEl.classList.add('done');
    spinnerEl.remove();
    barEl.style.width='100%';
    setTimeout(()=>window.close(),650);
    return;
  }
  stepEl.textContent=text;
  if(typeof pct==='number'){
    _progress=Math.min(pct,96);
    barEl.style.width=_progress+'%';
  }
});
// 初始 spinner 占位
setTimeout(()=>barEl.style.width='18%',120);
</script></body></html>`

  _splashWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(splashHtml))
  _splashWindow.on('closed', () => { _splashWindow = null })
  _splashWindow.show()
  return _splashWindow
}

function splashStep(text, pct) {
  if (_splashWindow && !_splashWindow.isDestroyed()) {
    _splashWindow.webContents.send('splash:step', text, pct)
  }
}

function closeSplash() {
  if (_splashWindow && !_splashWindow.isDestroyed()) {
    _splashWindow.close()
    _splashWindow = null
  }
}

// ---------- 工具：等待前端就绪（dev 模式下 Vite 可能还没起来） ----------

async function waitFrontendReady(url, timeoutMs = 60000) {
  if (!url.startsWith('http')) return true  // 生产模式 file:// 直接 OK
  const t0 = Date.now()
  while (Date.now() - t0 < timeoutMs) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(1500) })
      if (res.ok) return true
    } catch {}
    await new Promise(r => setTimeout(r, 800))
  }
  console.warn(`[Electron] 前端 ${url} 等待超时`)
  return false
}

// ---------- 主窗口 ----------

async function createMainWindow() {
  // dev 模式先等 Vite 起来
  if (IS_DEV) {
    await waitFrontendReady(DEV_URL)
  }
  // 窗口图标：优先项目根的 agentcluster.ico（Windows 多分辨率最佳），再 fallback png
  const _winIconPath = fs.existsSync(path.join(PROJECT_ROOT, 'agentcluster.ico'))
    ? path.join(PROJECT_ROOT, 'agentcluster.ico')
    : (fs.existsSync(path.join(ROOT, 'public', 'icon.png')) ? path.join(ROOT, 'public', 'icon.png') : undefined)

  mainWindow = new BrowserWindow({
    width: 1280, height: 820,
    minWidth: 960, minHeight: 600,
    frame: false,              // frameless，靠前端自绘标题栏
    resizable: true,
    movable: true,
    fullscreenable: true,
    title: '智能体集群 AgentCluster',
    backgroundColor: '#0a0e1a', // 与前端 --bg-base 一致，防白闪
    icon: _winIconPath,
    show: false,
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      devTools: IS_DEV,
    },
  })

  // 生产模式：加载本地 dist/index.html（先检查 dist 有效性，空文件 = 白屏）
  let startURL
  if (IS_DEV) {
    startURL = DEV_URL
  } else {
    // Prod 模式：统一走 HTTP（Python http.server 服务 dist）
    // 好处：Electron 和浏览器都能通过 localhost 访问，避开 file:// 协议坑
    startURL = PROD_URL + '/?app=electron'
  }
  _log('createMainWindow: startURL=' + startURL)

  mainWindow.loadURL(startURL)

  // ---- 诊断日志（白屏时救命用）----
  mainWindow.webContents.on('did-fail-load', (_e, errCode, errDesc, url) => {
    _log('❌ 主窗口加载失败: ' + errCode + ' ' + errDesc + ' url=' + url)
    // 自动开 DevTools 让用户看到错误
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  })
  mainWindow.webContents.on('render-process-gone', (_e, details) => {
    _log('❌ 渲染进程挂了: reason=' + details.reason + ' exitCode=' + details.exitCode)
  })
  mainWindow.webContents.on('console-message', (_e, level, msg, line, sourceId) => {
    _log('[renderer][' + level + '] ' + msg + ' (' + sourceId + ':' + line + ')')
  })
  mainWindow.webContents.on('did-finish-load', () => {
    _log('✅ 主窗口 did-finish-load, url=' + mainWindow.webContents.getURL())
  })

  // 显示策略：等 Vue 数据加载完（app:ready IPC）再 show，防白屏闪烁
  // 兜底：8 秒内没收到就强制 show（防止 IPC 丢失导致窗口永远不显示）
  let _showed = false
  const _doShow = (from) => {
    if (_showed) return
    _showed = true
    _log(`✅ 显示主窗口 (${from})`)
    closeSplash()
    mainWindow.show()
  }
  
  mainWindow.once('ready-to-show', () => {
    _log('✅ ready-to-show (等 Vue app:ready...)')
    // ready-to-show 只标记，不立即 show
    // 如果 8 秒后还没收到 app:ready，兜底显示
    setTimeout(() => _doShow('timeout-fallback'), 8000)
  })

  // Vue 数据加载完后发 app:ready
  ipcMain.on('app:ready', () => _doShow('vue-app-ready'))

  mainWindow.on('closed', () => { mainWindow = null })

  // 新窗口走系统默认浏览器
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  // 开发工具快捷键（F12 / Ctrl+Shift+I）
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.key === 'F12' || (input.control && input.shift && input.key.toLowerCase() === 'i')) {
      mainWindow.webContents.toggleDevTools()
    }
  })
}

// ---------- Tray ----------

let tray = null
let _trayRefreshTimer = null
let _trayStatus = '⏹ 已停止'

async function _checkHealth() {
  const result = { backend: false, frontend: false }
  const opener = fetch(`http://127.0.0.1:${BACKEND_PORT}/api/health`, { signal: AbortSignal.timeout(1500) }).then(r => r.ok).catch(() => false)
  const url = IS_DEV ? DEV_URL : DEV_URL
  const opener2 = fetch(url, { signal: AbortSignal.timeout(1500) }).then(r => r.ok).catch(() => false)
  const [b, f] = await Promise.all([opener, opener2])
  result.backend = b; result.frontend = f
  return result
}

function _buildTrayMenuTemplate(statusText) {
  if (TRAY_ONLY) {
    // Web 模式：后端+dist server 由 start.bat 启动
    return [
      { label: `Web 模式 · ${statusText}`, enabled: false },
      { type: 'separator' },
      { label: '🌐 打开浏览器', default: true, click: () => shell.openExternal(`http://localhost:5173`) },
      { label: '🔄 重启全部', click: async () => {
          _dialogSync('重启提示', '请直接关闭 start.bat 窗口后重新运行')
      }},
      { type: 'separator' },
      { label: '⚙ 打开设置', click: () => shell.openExternal(`http://localhost:5173/#/settings`) },
      { type: 'separator' },
      { label: '❌ 退出并停止服务', click: () => {
          _killWebProcesses()
          app.quit()
      }},
    ]
  }
  return [
    { label: `状态: ${statusText}`, enabled: false },
    { type: 'separator' },
    { label: '🚀 启动服务', click: () => {
      // Electron 下默认就是启动了（app.whenReady 已经自动拉了后端+窗口），
      // 如果用户手动关了窗口，这里重新打开
      if (!mainWindow || !mainWindow.isVisible()) {
        if (!mainWindow) { createMainWindow().catch(() => {}); return }
        mainWindow.show(); mainWindow.focus()
      }
    }},
    { label: '🔄 重启全部', click: async () => {
      splashStep('正在重启服务...', 10); createSplash()
      stopBackend()
      splashStep('正在重启后端...', 35)
      await startBackend().catch(() => {})
      splashStep('正在准备界面...', 80)
      if (!mainWindow || !mainWindow.isVisible()) {
        if (!mainWindow) await createMainWindow().catch(() => {})
        else { mainWindow.show(); mainWindow.focus() }
      }
      splashStep('DONE')
      setTimeout(closeSplash, 800)
    }},
    { label: '⏹ 停止全部服务', click: () => {
      stopBackend()
      if (mainWindow) { mainWindow.close() }
      _updateTrayStatus('⏹ 已停止')
    }},
    { type: 'separator' },
    { label: '🌐 打开界面', default: true, click: async () => {
      if (!mainWindow) { await createMainWindow().catch(() => {}) }
      else if (!mainWindow.isVisible()) { mainWindow.show() }
      mainWindow?.focus()
    }},
    { label: '🌐 用浏览器打开', click: () => shell.openExternal(IS_DEV ? DEV_URL : DEV_URL) },
    { label: '⚙ 后端健康检查', click: async () => {
      try {
        const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/health`, { signal: AbortSignal.timeout(1500) })
        shell.openExternal(`http://127.0.0.1:${BACKEND_PORT}/api/health`)
      } catch {
        _dialogSync('后端未运行', '后端服务未启动，请先点"🚀 启动服务"')
      }
    }},
    { type: 'separator' },
    { label: '❌ 退出程序', click: () => {
      stopBackend()
      app.quit()
    }},
  ]
}

function _updateTrayStatus(newStatus) {
  if (!tray || newStatus === _trayStatus) return
  _trayStatus = newStatus
  tray.setToolTip(`AgentCluster  ${newStatus}`)
  try { tray.setContextMenu(Menu.buildFromTemplate(_buildTrayMenuTemplate(newStatus))) } catch {}
}

// Web 模式退出时：杀掉 start.bat 启动的 python + node（按端口匹配）
function _killWebProcesses() {
  try {
    const { execSync } = require('child_process')
    for (const port of [BACKEND_PORT, 5173]) {
      try {
        const out = execSync(`netstat -ano | findstr :${port} | findstr LISTENING`, { encoding: 'utf8' })
        const m = out.trim().split(/\s+/)
        const pid = parseInt(m[m.length - 1], 10)
        if (pid && pid !== process.pid) {
          try { process.kill(pid, 'SIGTERM') } catch {}
          try { execSync(`taskkill /F /PID ${pid}`, { encoding: 'utf8', stdio: 'pipe' }) } catch {}
        }
      } catch {}
    }
  } catch {}
}

function _dialogSync(title, msg) {
  try {
    dialog.showMessageBoxSync({ message: `${title}\n\n${msg}`, type: 'info', title: 'AgentCluster' })
  } catch {}
}

function createTray() {
  // 图标：优先项目 agentcluster.ico（Windows 多分辨率最佳），否则 png fallback
  const iconPaths = [
    path.join(PROJECT_ROOT, 'agentcluster.ico'),
    path.join(ROOT, 'public', 'icon.png'),
    path.join(ROOT, 'dist', 'icon.png'),
    path.join(ROOT, 'public', 'icon.jpg'),
    path.join(PROJECT_ROOT, '1791461860623.webp.jpg'),
  ]
  let img = null
  for (const p of iconPaths) {
    if (fs.existsSync(p)) {
      img = nativeImage.createFromPath(p)
      if (!img.isEmpty()) break
      img = null
    }
  }
  if (!img || img.isEmpty()) {
    // fallback: 用 Electron 自带的 icon
    try {
      const exePath = app.getPath('exe')
      const resources = path.join(path.dirname(exePath), 'resources')
      for (const f of ['icon.png', 'default_app.asar/icon.png']) {
        const p = path.join(resources, f)
        if (fs.existsSync(p)) { img = nativeImage.createFromPath(p); break }
      }
    } catch {}
  }
  if (!img || img.isEmpty()) {
    img = nativeImage.createEmpty()
  }

  tray = new Tray(img)
  tray.setToolTip('AgentCluster  正在启动...')
  tray.setContextMenu(Menu.buildFromTemplate(_buildTrayMenuTemplate('⏹ 已停止')))

  // 左键 = 打开界面（default: true 的项）
  tray.on('click', async () => {
    if (!mainWindow) { await createMainWindow().catch(() => {}) }
    else if (!mainWindow.isVisible()) { mainWindow.show() }
    mainWindow?.focus()
  })
  tray.on('double-click', async () => {
    if (!mainWindow) { await createMainWindow().catch(() => {}) }
    else if (!mainWindow.isVisible()) { mainWindow.show() }
    mainWindow?.focus()
  })

  // 每 3s 刷一次状态（与原 start_tray.py 一致）
  _trayRefreshTimer = setInterval(async () => {
    const h = await _checkHealth()
    let status
    if (h.backend && h.frontend) status = '✅ 运行中'
    else if (!h.backend && !h.frontend) status = '⏹ 已停止'
    else status = `⚠️ 部分失败 (后端=${h.backend?'OK':'FAIL'}, 前端=${h.frontend?'OK':'FAIL'})`
    _updateTrayStatus(status)
  }, 3000)
}

// ---------- IPC：窗口控制 ----------

function registerIPC() {
  // 窗口控制
  ipcMain.handle('win:minimize', () => { _log('[IPC] win:minimize'); mainWindow?.minimize() })
  ipcMain.handle('win:maximize', () => { _log('[IPC] win:maximize'); return mainWindow?.isMaximized() || mainWindow?.maximize() })
  ipcMain.handle('win:unmaximize', () => { _log('[IPC] win:unmaximize'); mainWindow?.unmaximize() })
  ipcMain.handle('win:toggleMax', () => {
    _log('[IPC] win:toggleMax, mainWindow=', !!mainWindow, 'isMaximized=', mainWindow?.isMaximized())
    if (!mainWindow) return false
    if (mainWindow.isMaximized()) { mainWindow.unmaximize(); return false }
    mainWindow.maximize(); return true
  })
  ipcMain.handle('win:restore', () => { _log('[IPC] win:restore'); mainWindow?.unmaximize() || mainWindow?.show() })
  ipcMain.handle('win:close', () => { _log('[IPC] win:close'); mainWindow?.close() })
  ipcMain.handle('win:getBounds', () => {
    if (!mainWindow) return { x: 0, y: 0, width: 1280, height: 820 }
    const b = mainWindow.getBounds()
    return { x: b.x, y: b.y, width: b.width, height: b.height }
  })
  ipcMain.handle('win:setBounds', (_e, bounds) => {
    if (!mainWindow) return
    mainWindow.setBounds({
      x: Math.round(bounds.x),
      y: Math.round(bounds.y),
      width: Math.round(bounds.width),
      height: Math.round(bounds.height),
    })
  })
  ipcMain.handle('win:resize', (_e, w, h) => mainWindow?.setSize(Math.round(w), Math.round(h)))
  ipcMain.handle('win:move', (_e, x, y) => mainWindow?.setPosition(Math.round(x), Math.round(y)))
  ipcMain.handle('win:isMaximized', () => mainWindow?.isMaximized() ?? false)

  // 后端健康检查
  ipcMain.handle('api:health', async () => {
    try {
      const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/api/health`, { signal: AbortSignal.timeout(1500) })
      return res.ok
    } catch { return false }
  })

  // 最大化状态变化同步回前端（前端按钮需要知道 isMax）
  mainWindow?.on('maximize', () => mainWindow.webContents.send('win:maximized', true))
  mainWindow?.on('unmaximize', () => mainWindow.webContents.send('win:maximized', false))
}

// ---------- App 生命周期 ----------

// 单例锁：Windows 受限/沙箱环境下 requestSingleInstanceLock 会静默返回 false，
// Electron 框架随即自动 quit app（不是抛异常，没法 try-catch）。
// 所以开发模式直接跳过，打包后再启用。
if (app.isPackaged) {
  const gotLock = app.requestSingleInstanceLock()
  _log('isPackaged单例锁 gotLock=' + gotLock)
  if (!gotLock) {
    _log('已有实例在跑，退出本次')
    app.quit()
  } else {
    app.on('second-instance', async () => {
      _log('second-instance: 把已有窗口拉到前台')
      if (!mainWindow) await createMainWindow()
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.show()
      mainWindow.focus()
    })
  }
} else {
  _log('开发模式，跳过单例锁')
}

app.whenReady().then(async () => {
  if (TRAY_ONLY) {
    _log('=== TRAY-ONLY 模式：不启动后端/不建主窗口 ===')
    createTray()
    return
  }

  createSplash()
  splashStep('正在初始化启动器...', 5)

  // Prod 模式：先起 dist server（让 127.0.0.1:5173 可访问）
  if (!IS_DEV) {
    splashStep('正在启动前端静态服务...', 15)
    try {
      await startDistServer()
    } catch (err) {
      console.warn('[Electron] Dist server 启动失败:', err.message)
      try { _dialogSync('前端构建缺失', err.message) } catch {}
    }
  }

  // 启动后端并等待就绪（必须在 createMainWindow 之前完成，否则 Home.vue 的 fetch 会因后端未就绪而失败）
  splashStep('正在启动后端服务...', 25)
  try {
    await startBackend()
    splashStep('后端就绪', 55)
  } catch (err) {
    console.warn('[Electron] 后端拉起失败（窗口仍可打开）:', err.message)
    splashStep('后端启动超时', 55)
  }

  try {
    splashStep('正在准备界面...', 70)
    await createMainWindow()
    createTray()
    registerIPC()
    splashStep('DONE')
    setTimeout(closeSplash, 800)
  } catch (err) {
    console.error('[Electron] 初始化失败:', err)
    splashStep('界面加载失败', 90)
    setTimeout(closeSplash, 2000)
  }
})

app.on('window-all-closed', () => {
  // 不关托盘，保持后台运行
})

app.on('before-quit', () => {
  if (TRAY_ONLY) {
    _killWebProcesses()
  } else {
    stopBackend()
    stopDistServer()
  }
})

app.on('activate', async () => {
  if (BrowserWindow.getAllWindows().length === 0) await createMainWindow()
})

