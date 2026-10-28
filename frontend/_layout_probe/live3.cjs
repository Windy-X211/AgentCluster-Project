const { app, BrowserWindow } = require('electron');
app.disableHardwareAcceleration();
const sleep = ms => new Promise(r => setTimeout(r, ms));

app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1400, height: 950, webPreferences: {} });
    await win.loadURL('http://localhost:5173');
    await sleep(8000);
    const info = await win.webContents.executeJavaScript(`(() => ({
    bodyText: (document.body.innerText || '').replace(/\\s+/g,' ').slice(0, 300),
    convItems: document.querySelectorAll('.conv-item').length,
    isEmptyCenter: !!document.querySelector('.empty-center'),
    boxChildren: document.querySelectorAll('.chat-box > *').length,
    chatHTML: (document.querySelector('.chat-box')?.innerHTML || '').slice(0, 200)
  }))()`);
    console.log('INFO', JSON.stringify(info, null, 2));
    app.quit();
}).catch(e => { console.error('ERR', e); app.quit(); });