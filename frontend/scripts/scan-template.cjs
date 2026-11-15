const fs=require('fs'),path=require('path');
const ROOT=path.resolve(__dirname,'..');
const SRC=path.join(ROOT,'src');
function walk(dir,out=[]){for(const f of fs.readdirSync(dir)){const p=path.join(dir,f);const st=fs.statSync(p);if(st.isDirectory())walk(p,out);else if(f.endsWith('.vue')&&f!=='AppIcon.vue')out.push(p)}return out}
const re=/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{2300}-\u{23FF}]/gu;
for(const f of walk(SRC)){const c=fs.readFileSync(f,'utf8');const s=c.indexOf('<template');const e=c.lastIndexOf('</template>');if(s<0)continue;const tpl=c.slice(s,e);const prefix=c.slice(0,s);const jsOffset=prefix.length;let m;const hits=new Map();while((m=re.exec(tpl))){const absIdx=jsOffset+m.index;const absLine=c.slice(0,absIdx).split('\n').length;const line=c.split('\n')[absLine-1]||'';
// 跳过已在 AppIcon 标签内
if(line.includes('<AppIcon')&&line.includes(m[0]))continue;
// 跳过 script/setup 块
const before=c.slice(0,absIdx);const tmplStart=before.lastIndexOf('<template');const scriptStart=before.lastIndexOf('<script');if(scriptStart>tmplStart)continue;
// 跳过注释和字符串
if(line.trim().startsWith('//')||line.includes("'")&&line.includes(m[0])||line.includes('"')&&line.includes(m[0]))continue;
if(!hits.has(absLine))hits.set(absLine,[]);hits.get(absLine).push(m[0])}
if(hits.size){console.log(path.relative(ROOT,f)+':');for(const [l,v] of hits)console.log('  L'+l+': '+(c.split('\n')[l-1]||'').trim().slice(0,100))}}
