<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { usePluginDebugStore } from '@/stores'
import { api } from '@/api'
import PluginList from '@/components/plugin-debug/PluginList.vue'
import ParamForm from '@/components/plugin-debug/ParamForm.vue'
import ResultPanel from '@/components/plugin-debug/ResultPanel.vue'
import RunLog from '@/components/plugin-debug/RunLog.vue'

const store = usePluginDebugStore()

const toast = ref<{ msg: string; kind: 'ok' | 'err' } | null>(null)
let toastTimer: number | null = null
function showToast(msg: string, kind: 'ok' | 'err' = 'ok') {
  toast.value = { msg, kind }
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => (toast.value = null), 3000)
}

onMounted(async () => {
  // 面板启动预填项目默认插件目录（AppData/plugins），并自动加载列表
  if (!store.rootDir) {
    try {
      const paths = await api.storage.paths()
      store.rootDir = paths?.plugins_dir || ''
    } catch {
      /* 拿不到默认目录时保持为空，由用户手动选择 */
    }
  }
  if (store.rootDir) {
    await store.scan(store.rootDir)
  } else if (!store.scanError) {
    store.scanError = '未能自动获取默认插件目录，请点击「选择文件夹」选择插件目录'
  }
})

async function pick() {
  try {
    const path = await store.pickFolder()
    if (path) showToast(`已加载：${path}`, 'ok')
    else showToast('未选择目录（已取消，或系统选择框未弹出——若未弹出请重启后端服务）', 'err')
  } catch (e) {
    showToast(`打开文件夹选择器失败：${(e as Error).message}`, 'err')
  }
}

async function rescan() {
  if (!store.rootDir) {
    showToast('请先选择插件文件夹', 'err')
    return
  }
  await store.scan(store.rootDir)
}

async function run() {
  if (!store.selectedPlugin) {
    showToast('请先选择插件', 'err')
    return
  }
  try {
    await store.run()
  } catch (e) {
    showToast((e as Error).message, 'err')
  }
}
</script>

<template>
  <div class="panel">
    <aside class="pane-left glass">
      <div class="folder-bar">
        <button class="btn" :disabled="store.scanning" @click="pick">📁 选择文件夹</button>
        <button class="btn ghost" :disabled="store.scanning || !store.rootDir" @click="rescan">⟳ 重新扫描</button>
      </div>
      <div class="path" :title="store.rootDir">{{ store.rootDir || '未选择目录' }}</div>
      <PluginList class="list-area" />
    </aside>

    <section class="pane-center glass">
      <ParamForm>
        <template #run>
          <button class="btn run" :disabled="!store.selectedPlugin || store.running" @click="run">
            {{ store.running ? '运行中…' : '▶ 运行插件' }}
          </button>
          <span class="run-hint" v-if="!store.selectedPlugin">请先选择插件</span>
        </template>
      </ParamForm>
    </section>

    <section class="pane-right glass">
      <div class="block">
        <div class="block-title">执行结果</div>
        <ResultPanel />
      </div>
      <div class="block">
        <RunLog />
      </div>
    </section>

    <div v-if="toast" class="toast" :class="toast.kind">{{ toast.msg }}</div>
  </div>
</template>

<style scoped>
.panel {
  display: grid; grid-template-columns: 320px minmax(0, 1.1fr) minmax(0, 1fr); gap: 12px;
  height: 100%; min-height: 0; overflow: hidden;
}
.pane-left, .pane-center, .pane-right { padding: 14px; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
.pane-left { gap: 10px; }
.folder-bar { display: flex; gap: 8px; }
.folder-bar .btn { flex: 1; }
.path {
  font-family: Consolas, monospace; font-size: 11px; opacity: .65;
  background: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 6px; padding: 6px 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.list-area { flex: 1; min-height: 0; }
.pane-center { overflow: hidden; }
.pane-right { gap: 14px; overflow-y: auto; }
.btn.run { padding: 8px 24px; font-size: 14px; font-weight: 600; flex-shrink: 0; }
.btn.run:disabled { opacity: .45; cursor: not-allowed; transform: none; box-shadow: none; }
.run-hint { font-size: 12px; opacity: .5; }
.block-title { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; opacity: .55; margin-bottom: 8px; }
.toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  padding: 10px 18px; border-radius: 10px; font-size: 13px; z-index: 2000;
  backdrop-filter: blur(12px); box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
}
.toast.ok { background: rgba(100, 220, 160, 0.9); color: #06281a; }
.toast.err { background: rgba(255, 90, 90, 0.92); color: #2b0505; }
</style>