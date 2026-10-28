const { app, BrowserWindow } = require('electron');
app.disableHardwareAcceleration();
const sleep = ms => new Promise(r => setTimeout(r, ms));

app.whenReady().then(async () => {
    const win = new BrowserWindow({ show: false, width: 1400, height: 950, webPreferences: {} });
    await win.loadURL('http://localhost:5173');
    await sleep(8000);
    const out = await win.webContents.executeJavaScript(`(() => {
    const boxes = [...document.querySelectorAll('.chat-box')].map(b => {
      const r = b.getBoundingClientRect();
      return { cls: b.className, kids: b.children.length, rect: {t:Math.round(r.top),b:Math.round(r.bottom),l:Math.round(r.left),r:Math.round(r.right)} };
    });
    // 找含"桌宠控制"的最小元素并回溯祖先
    let node = null;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      if ((walker.currentNode.nodeValue||'').includes('桌宠控制')) { node = walker.currentNode.parentElement; break; }
    }
    const chain = [];
    let e = node;
    let depth = 0;
    while (e && depth < 12) {
      const r = e.getBoundingClientRect();
      chain.push({ cls: (typeof e.className==='string'?e.className:''), tag: e.tagName, kids: e.children.length,
        rect: {t:Math.round(r.top),b:Math.round(r.bottom),l:Math.round(r.left),r:Math.round(r.right),w:Math.round(r.width),h:Math.round(r.height)},
        flexDir: getComputedStyle(e).flexDirection, disp: getComputedStyle(e).display });
      e = e.parentElement; depth++;
    }
    return { boxCount: boxes.length, boxes, chain };
  })()`);
    console.log('OUT4_START');
    console.log(JSON.stringify(out, null, 1));
    console.log('OUT4_END');
    app.quit();
}).catch(e => { console.error('ERR', e); app.quit(); });