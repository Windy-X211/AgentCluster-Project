const { app, BrowserWindow } = require('electron');
const path = require('path');

app.disableHardwareAcceleration();

app.whenReady().then(async () => {
    const win = new BrowserWindow({
        show: false,
        width: 460,
        height: 860,
        webPreferences: { offscreen: false },
    });
    await win.loadFile(path.join(__dirname, 'index.html'));
    await new Promise(r => setTimeout(r, 400));
    const result = await win.webContents.executeJavaScript('window.__probe()');
    console.log('PROBE_JSON_START');
    console.log(JSON.stringify(result, null, 2));
    console.log('PROBE_JSON_END');
    app.quit();
}).catch(e => { console.error('ERR', e); app.quit(); });