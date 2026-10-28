const { app, BrowserWindow } = require('electron');

app.disableHardwareAcceleration();

app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1280, height: 900, webPreferences: {} });
    try {
        await win.loadURL('http://localhost:5173');
    } catch (e) {
        console.log('LOAD_ERR', String(e));
    }
    await new Promise(r => setTimeout(r, 3500));
    const script = `(() => {
    const dump = (el) => {
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return {
        cls: el.className && typeof el.className === 'string' ? el.className : '',
        tag: el.tagName,
        rect: { top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left), right: Math.round(r.right), w: Math.round(r.width), h: Math.round(r.height) },
        pos: cs.position, disp: cs.display, flexDir: cs.flexDirection, alignSelf: cs.alignSelf, alignItems: cs.alignItems,
        text: (el.textContent || '').slice(0, 24)
      };
    };
    const box = document.querySelector('.chat-box');
    const res = { box: dump(box), children: [] };
    if (box) {
      for (const c of box.children) {
        const entry = dump(c);
        entry.grandchildren = [];
        for (const g of c.children) entry.grandchildren.push(dump(g));
        res.children.push(entry);
      }
    }
    return res;
  })()`;
    const result = await win.webContents.executeJavaScript(script);
    console.log('LIVE_JSON_START');
    console.log(JSON.stringify(result));
    console.log('LIVE_JSON_END');
    app.quit();
}).catch(e => { console.error('ERR', e); app.quit(); });