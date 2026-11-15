const fs=require('fs'),path=require('path');
function walk(dir){let out=[];for(const f of fs.readdirSync(dir)){const p=path.join(dir,f);if(fs.statSync(p).isDirectory())out=out.concat(walk(p));else if(/\.(vue|ts)$/.test(p))out.push(p)}return out}
const files=walk('c:/Users/qiqi/智能体集群/frontend/src');
const re=/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{2300}-\u{23FF}]/gu;
const lines=[];
for(const f of files){const c=fs.readFileSync(f,'utf8');let m;while((m=re.exec(c))){const line=c.slice(0,m.index).split('\n').length;const full=c.split('\n')[line-1];lines.push(path.basename(f)+':L'+line+': '+full.trim().slice(0,120))}}
fs.writeFileSync(path.join(__dirname,'emoji-scan.txt'),lines.join('\n'));
console.log(lines.length+' lines');
