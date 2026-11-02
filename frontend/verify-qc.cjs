// 最终验证：按钮像素可见性 + 选中智能体名与默认智能体对比
const { app, BrowserWindow } = require('electron')
const fs = require('fs')
const path = require('path')
const { spawn } = require('child_process')

const PORT = 18173

app.whenReady().then(async () => {
  const server = spawn('python', ['-m', 'http.server', String(PORT), '--directory', path.resolve(__dirname, 'dist')], { stdio: 'ignore' })
  await new Promise(r => setTimeout(r, 1500))

  // 拉默认智能体名（对比基准）
  let defaultAgent = null
  try {
    const s = await fetch('http://127.0.0.1:8870/api/settings').then(r => r.json())
    const id = Number((s.data || s).default_agent_id)
    const a = await fetch('http://127.0.0.1:8870/api/agents').then(r => r.json())
    const list = Array.isArray(a) ? a : (a.data || a.list || [])
    defaultAgent = list.find(x => x.id === id) || null
  } catch (e) { console.error('拉取默认智能体失败:', e.message) }

  const win = new BrowserWindow({
    width: 480, height: 740, frame: false, backgroundColor: '#0a0e1a',
    show: false, webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: false },
  })
  await win.loadURL(`http://127.0.0.1:${PORT}/#/quickchat`)
  await new Promise(r => setTimeout(r, 7000))
  const img = await win.webContents.capturePage()
  fs.writeFileSync(path.resolve(__dirname, 'verify-qc.png'), img.toPNG())

  const info = await win.webContents.executeJavaScript(`(() => {
    const name = document.querySelector('.agent-dd-trigger .dd-name')?.textContent?.trim() || null
    return { selectedAgentName: name }
  })()`)
  console.log('设置中的默认智能体:', defaultAgent ? `#${defaultAgent.id} ${defaultAgent.name}` : '(未设置)')
  console.log('小窗下拉选中的智能体:', info.selectedAgentName)
  console.log('是否同步:', defaultAgent && info.selectedAgentName === defaultAgent.name ? '✓ 一致' : '✗ 不一致')
  server.kill()
  app.quit()
}).catch(e => { console.error(e); app.exit(1) })