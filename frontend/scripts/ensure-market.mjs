#!/usr/bin/env node
/**
 * 打包前确保 ../market 存在。
 *
 * 市场资源已拆分到独立仓库 AgentCluster-Market，主源码仓库不再跟踪 market/。
 * 在干净的克隆 / CI 环境里，打包前先用本脚本按大陆镜像下载归档并解压；
 * 本地已有 market/index.json 时直接跳过。
 *
 * 环境变量：
 *   AGENTCLUSTER_MARKET_REPO    仓库地址（默认 GitHub 官方）
 *   AGENTCLUSTER_MARKET_BRANCH  分支（默认 main）
 *   AGENTCLUSTER_MARKET_MIRRORS 镜像模板，换行或逗号分隔，{url} 为占位符
 */
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { unzipSync } from 'fflate'

const HERE = dirname(fileURLToPath(import.meta.url))
const MARKET_DIR = resolve(HERE, '..', '..', 'market')

if (existsSync(join(MARKET_DIR, 'index.json'))) {
    console.log(`[market] 已存在，跳过下载：${MARKET_DIR}`)
    process.exit(0)
}

const REPO = process.env.AGENTCLUSTER_MARKET_REPO || 'https://github.com/Windy-X211/AgentCluster-Market.git'
const BRANCH = process.env.AGENTCLUSTER_MARKET_BRANCH || 'main'
const MIRRORS = (process.env.AGENTCLUSTER_MARKET_MIRRORS
    || 'https://ghfast.top/{url}\nhttps://gh-proxy.com/{url}')
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean)

function repoParts(url) {
    let host = 'github.com'
    let path = ''
    if (url.startsWith('git@')) {
        const m = /^git@([^:]+):(.+)$/.exec(url)
        if (!m) throw new Error(`无法解析仓库地址：${url}`)
        host = m[1]
        path = m[2]
    } else {
        const m = /^(?:https?:\/\/)?([^/]+)\/(.+)$/.exec(url)
        if (!m) throw new Error(`无法解析仓库地址：${url}`)
        host = m[1]
        path = m[2]
    }
    path = path.replace(/\.git$/, '').replace(/^\/+|\/+$/g, '')
    const [owner, repo] = path.split('/')
    if (!owner || !repo) throw new Error(`无法解析仓库地址：${url}`)
    return { host, owner, repo }
}

const { owner, repo } = repoParts(REPO)
const archive = `https://github.com/${owner}/${repo}/archive/refs/heads/${BRANCH}.zip`
const candidates = [
    ...MIRRORS.map((t) => (t.includes('{url}') ? t.replaceAll('{url}', archive) : `${t.replace(/\/+$/, '')}/${archive}`)),
    archive,
]

let buffer = null
let used = ''
for (const url of candidates) {
    try {
        console.log(`[market] 尝试下载：${url}`)
        const res = await fetch(url, { headers: { 'User-Agent': 'AgentCluster-Market/1.0' } })
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
        buffer = new Uint8Array(await res.arrayBuffer())
        used = url
        break
    } catch (e) {
        console.warn(`[market] 失败：${url} → ${e.message}`)
    }
}
if (!buffer) {
    console.error('[market] 所有源均下载失败。请检查网络或手动 clone 到 market/ 后重试。')
    process.exit(1)
}

const files = unzipSync(buffer)
const names = Object.keys(files)
// GitHub 归档通常带一层 <repo>-<branch>/ 前缀
const prefix = names.length && names[0].includes('/') ? `${names[0].split('/')[0]}/` : ''

mkdirSync(MARKET_DIR, { recursive: true })
let written = 0
for (const [name, data] of Object.entries(files)) {
    if (!name.startsWith(prefix) || name.endsWith('/')) continue
    const rel = name.slice(prefix.length)
    if (!rel || rel.split('/').includes('..')) continue
    const dest = join(MARKET_DIR, rel)
    mkdirSync(dirname(dest), { recursive: true })
    writeFileSync(dest, data)
    written += 1
}

console.log(`[market] 已同步 ${written} 个文件 → ${MARKET_DIR}（来源：${used}）`)