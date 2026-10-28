const { app, BrowserWindow } = require('electron');

app.disableHardwareAcceleration();

const sleep = ms => new Promise(r => setTimeout(r, ms));

app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1400, height: 950, webPreferences: {} });
    await win.loadURL('http://localhost:5173');
    await sleep(3500);

    const dumpScript = `(() => {
    const q = (s) => document.querySelector(s);
    const box = q('.chat-box');
    const info = {
      hasNoConv: !!q('.no-conv'),
      boxChildren: box ? box.children.length : -1,
      convItems: document.querySelectorAll('.conv-item').length,
      convNames: [...document.querySelectorAll('.conv-item .conv-name')].map(e => e.textContent).slice(0, 8),
      agentDd: q('.agent-dd') ? q('.agent-dd').textContent.slice(0, 40) : null,
    };
    return info;
  })()`;
    const before = await win.webContents.executeJavaScript(dumpScript);
    console.log('BEFORE', JSON.stringify(before));

    // 点击第一个会话项
    await win.webContents.executeJavaScript(`(() => { const c = document.querySelector('.conv-item'); if (c) c.click(); return !!c; })()`);
    await sleep(2500);

    const script = `(() => {
    const dump = (el) => {
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return {
        cls: (typeof el.className === 'string' ? el.className : ''),
        rect: { top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left), right: Math.round(r.right), w: Math.round(r.width), h: Math.round(r.height) },
        pos: cs.position, disp: cs.display, flexDir: cs.flexDirection, alignSelf: cs.alignSelf,
        txt: (el.textContent || '').slice(0, 20)
      };
    };
    const box = document.querySelector('.chat-box');
    const res = { box: dump(box), children: [] };
    if (box) for (const c of box.children) {
      const e = dump(c); e.kids = [...c.children].map(dump); res.children.push(e);
    }
    return res;
  })()`;
    const out = await win.webContents.executeJavaScript(script);
    console.log('LIVE2_JSON_START');
    console.log(JSON.stringify(out));
    console.log('LIVE2_JSON_END');
    app.quit();
}).catch(e => { console.error('ERR', e); app.quit(); });