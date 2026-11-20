<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

interface Region { selector: string; name: string; icon: string; text: string }

const regions: Region[] = [
  {
    selector: '.region-settings', name: '设置中心', icon: '⚙️',
    text: '设置中心，共 10 个面板：\n· 模型接口：API 地址 / 密钥 / 可用模型（内置 6 个预设，可「获取模型列表」），可设为默认\n· 🗜 历史上下文压缩：开关 + 压缩线 / 保留最近对话条数 / 默认上下文窗口\n· 🤖 默认智能体：新建对话时自动选中\n· 🛡 权限设置：危险操作授权（手动确认 / 自动审核 / 完全权限）+ 工作范围（仅限工作区 / 指定范围 / 电脑全局）\n· 🎨 外观设置：头像风格 / 界面字体 / 文字大小 / 对话区显示 / 四套配色 / 背景（图片·视频·动画）\n· 📖 新手引导：再次开启「使用流程引导」或「区域功能引导」\n· 🔔 提示音：开关 + 试听完成音 / 错误音\n· 🎙️ 语音识别：安装并启用 SenseVoice，对话中用 🎤 同声传译\n· 🚀 启动方式：桌面客户端 / 浏览器网页 + 开机自启动（Windows）\n· 📂 数据存储：切换位置 / 迁移（只复制不覆盖）/ 各数据目录可改 json 或直接打开',
  },
  {
    selector: '.region-market', name: '市场', icon: 'shopping-cart',
    text: '下载 / 发布智能体、画布、背景、技能、插件、组合技与功能拓展。\n· 顶栏：GitHub 登录（上传 / 推送需登录，Token 仅存本机）\n· 同步最新：走大陆镜像，打开页面时自动静默同步\n· 💾 缓存占用：可「清理」，不影响已安装数据\n· 推送远程：把本地发布推送到远程仓库\n· 设置：远程仓库地址 / 分支 / 镜像源\n· 上传资源：按类型勾选本机资源 →「发布到市场（N）」，后台进行\n· 侧栏：📤 我的发布 / 🔥 推荐 / 智能体 / 画布 / 背景 / 技能 / 插件 / 组合技 / 功能拓展\n· 搜索：输入即搜（220ms 防抖），✕ 清除；勾选「未安装」隐藏本机已装资源\n· 卡片：预览 / 下载（已装则显示「已安装」）；「我的发布」里可下架\n· 预览弹窗：含 📁 文件列表与 🕰 版本历史，可安装指定版本\n· 资源未下载到本机时，顶部出现「从镜像同步」按钮',
  },
  {
    selector: '.region-extensions', name: '功能拓展', icon: '🧩',
    text: '安装 / 启用 / 停用 / 卸载界面与功能拓展插件（资源从市场下载）。\n· 只列出已安装的拓展；按住卡片拖动可排序（顺序会记住）\n· 状态标识：● 已启用（绿）/ ○ 已停用（灰）\n· 卡片操作：配置（拓展自带配置页时出现）/ 启用 ↔ 停用 / 卸载\n· 卸载确认「确认卸载「名称」？将删除其代码与配置。」\n· 拓展代码存于数据存储位置的 extensions\\ 目录（设置中心「📂 数据存储」可查看实际路径）\n· 没有拓展时提示去「市场」页面下载 → 市场分类「🧩 功能拓展」\n· 开发规范见项目 doc/spec/extension-spec.md',
  },
  {
    selector: '.region-plugins', name: '能力管理', icon: '🛠️',
    text: '能力管理页面，共五个 Tab：\n· ⚡ 默认能力：新建智能体时自动勾选的命令 / 技能 / 插件；⟳ 刷新能力可热重载；可设工具调用最大轮数\n· 📋 能力列表：按类型浏览全部命令 / 技能 / 插件，可新建分组与组合技、拖拽排序\n· 🔐 能力参数预设：为插件 / 技能预填 🔒 固定参数（加密保存、模型不可见）与 📝 可选参数\n· 🧪 插件调试：选目录 → ⟳ 重新扫描 → 选插件填参 → ▶ 运行；右栏看结果与日志（敏感参数自动脱敏）\n· 📦 依赖安装：查看 pip / npm / 外部应用依赖，⟳ 重新扫描 / 一键安装',
  },
  {
    selector: '.canvases-pane', name: '画布列表', icon: '🗂️',
    text: '管理所有画布（集群编排）。\n· 点 ▸/▾ 或标题行展开 / 折叠列表\n· + 新建画布（自动命名「画布 N」，并展开左侧智能体列表）\n· 单击切换当前画布；双击行内重命名（回车提交 / Esc 取消）\n· 右键：重命名 / 删除此画布；条目右侧 × 删除（仅剩一张时不显示）\n· 拖动条目调整顺序，也可拖进画布区复用其中的智能体与连线\n· ● 红点表示该画布正在运行',
  },
  {
    selector: '.left', name: '智能体列表区', icon: '📋',
    text: '管理所有智能体的入口。\n· 按组分类：顶部 + 新建组；点组卡片进入组详情，← 返回或点组名回列表\n· 组内 + 新建智能体（自动归入该组；「全部」组新建则不分组）\n· 拖拽组 / 智能体排序（上半区插前、下半区插后）；拖智能体到画布创建节点\n· 右键智能体：✏️ 编辑 / 🗑 删除 / ➕ 归纳到分组（新建组或选已有组）\n· 右键组：✏️ 重命名 / ➕ 新建智能体 / 🗑 删除分组 / 🧹 清空分组 / ⚠️ 删除组和智能体\n· 删除分组与清空分组会把智能体移回「全部」；删除组和智能体不可恢复\n· 卡片显示头像 / 名称 / 描述 / 能力标签（命令蓝·技能紫·插件绿，超出显示 +N）\n· ◂ / ▸ 折叠 / 展开列表',
  },
  {
    selector: '.center', name: '画布区', icon: '🎨',
    text: '集群编排的核心画布，改动自动保存。\n· 拖拽智能体到画布创建节点；滚轮缩放（鼠标为锚）、拖拽空白或空格 + 拖拽平移\n· 连线：点节点 🔗 起笔 → 左键落点 / 右键换线型（曲线·直线·圆弧·垂线）→ 点目标节点收笔\n· 双击节点头像或消息泡泡显示 / 隐藏气泡；双击节点其他位置打开编辑\n· 右键节点：✏️ 编辑 / 👁 感知范围 / ⏱ 触发设置（手动或 cron 定时）/ ⏸ 停用·▶ 启用 / 🗑 移除\n· 右键空白：撤销 / 重做 / 重置视图 / 工作区 / 仅显示运行中气泡 / 隐藏·显示所有气泡\n· 工具栏：▶ 运行·■ 停止 / 串并切换 / 🎨 配色 / 📁 工作区 / 📋 规则 / 缩放 % / 删除 / ✕ 关闭\n· 框选：Shift 选节点、Alt 选连线；Delete 删除选中；Esc 取消\n· 🗂 工作区浮层支持预览 / 编辑 / 运行 / 🗑 回收站，可 📌 钉住到画布',
  },
  {
    selector: '.right', name: '对话区', icon: '💬',
    text: '与智能体实时对话的窗口（上方是画布列表）。\n· 标题栏 ▸/▾ 折叠对话记录浮层；＋ 新建 创建会话（自动清理空会话）\n· 记录浮层：↑/↓ 排序、无分类 / 按集群 / 按工作区（记住选择）、单击切换、× 删除、右键多选\n· 第一行：🧠 智能体 / 🔌 模型 + 推理等级 / 📁 工作区（会话与画布工作区联动）\n· 输入框只有裸 Enter 发送，Ctrl / Shift / Meta + Enter 换行；可拖入文件 / 文件夹\n· 上下文指示器显示 token，边框绿→红标示进度，点击立即压缩历史\n· 🎤 语音输入（需启用语音识别）；输出中发送按钮变 ■ 停止\n· (▾过程) 展开 🧠 思考 / 工具调用 / 截图 / A2A 协同；🗜 历史压缩卡片可展开摘要\n· 右键头像换头像；右键消息：复制 / 修改重发 / 删除 / 撤销并还原 / 文件操作历史',
  },
  {
    selector: '.topbar', name: '顶部栏', icon: '🔧',
    text: '全局导航与入口。\n· 🛒 市场：下载 / 发布智能体、画布、背景、技能、插件、组合技、功能拓展\n· 🧩 功能拓展：安装 / 启用 / 停用 / 卸载拓展\n· 🛠 能力管理：默认能力 / 能力列表 / 参数预设 / 插件调试 / 依赖安装\n· ⚙ 设置：模型接口 / 压缩 / 默认智能体 / 权限 / 外观 / 引导 / 提示音 / 语音 / 启动 / 存储\n· 品牌区可拖动窗口；F11 切换真全屏\n· 非首页右上角有最小化 / 最大化还原 / 关闭；顶部 6px 透明条可拖动，双击最大化',
  },
]

const currentSelector = ref('')
const current = computed(() => regions.find(r => r.selector === currentSelector.value) || null)
const visited = ref<Set<string>>(new Set())

function onMove(e: MouseEvent) {
  const t = e.target as HTMLElement | null
  if (!t || !t.closest) return
  let sel = ''
  for (const r of regions) {
    if (t.closest(r.selector)) { sel = r.selector; break }
  }
  if (sel && sel !== currentSelector.value) {
    currentSelector.value = sel
    if (sel) visited.value.add(sel)
  }
}

const panelPos = ref({ x: 0, y: 0 })
let dragging = false
let dragStart = { mx: 0, my: 0, px: 0, py: 0 }

function initPanelPos() {
  panelPos.value = { x: window.innerWidth - 460, y: 60 }
}
function onHeadDown(e: MouseEvent) {
  if ((e.target as HTMLElement).closest('.rg-close')) return
  dragging = true
  dragStart = { mx: e.clientX, my: e.clientY, px: panelPos.value.x, py: panelPos.value.y }
  document.addEventListener('mousemove', onDragMove)
  document.addEventListener('mouseup', onDragUp)
}
function onDragMove(e: MouseEvent) {
  if (!dragging) return
  const x = dragStart.px + (e.clientX - dragStart.mx)
  const y = dragStart.py + (e.clientY - dragStart.my)
  panelPos.value = {
    x: Math.max(0, Math.min(x, window.innerWidth - 440)),
    y: Math.max(0, Math.min(y, window.innerHeight - 60)),
  }
}
function onDragUp() {
  dragging = false
  document.removeEventListener('mousemove', onDragMove)
  document.removeEventListener('mouseup', onDragUp)
}

onMounted(() => {
  initPanelPos()
  document.addEventListener('mouseover', onMove)
})
onBeforeUnmount(() => {
  document.removeEventListener('mouseover', onMove)
  document.removeEventListener('mousemove', onDragMove)
  document.removeEventListener('mouseup', onDragUp)
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="rg-mask">
      <div class="rg-panel popup-surface" :style="{ left: panelPos.x + 'px', top: panelPos.y + 'px' }">
        <div class="rg-head" @mousedown="onHeadDown">
          <span class="rg-title"><AppIcon name="mouse" /> 区域功能引导</span>
          <button class="rg-close" @click="emit('close')"><AppIcon name="x" /></button>
        </div>
        <div class="rg-hint">将鼠标移至任一区域，此处自动显示其功能说明（可拖动本窗口）</div>
        <div class="rg-body">
          <template v-if="current">
            <div class="rg-card" :key="current.selector">
              <div class="rg-card-head">
                <span class="rg-card-icon"><AppIcon :name="current.icon" /></span>
                <span class="rg-card-name">{{ current.name }}</span>
              </div>
              <div class="rg-card-text">{{ current.text }}</div>
            </div>
          </template>
          <div v-else class="rg-empty">
            <div class="rg-empty-icon">👆</div>
            <div class="rg-empty-text">移动鼠标到市场、功能拓展、能力管理、设置、画布列表、智能体列表、画布区、对话区或顶部栏</div>
          </div>
        </div>
        <div class="rg-progress">
          <span v-for="r in regions" :key="r.selector" class="rg-dot"
                :class="{ active: currentSelector === r.selector, done: visited.has(r.selector) }"
                :title="r.name" />
        </div>
        <div class="rg-footer">
          <span class="rg-count">已查看 {{ visited.size }} / {{ regions.length }}</span>
          <button class="btn" @click="emit('close')">完成引导</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.rg-mask { position: fixed; inset: 0; z-index: 3200; pointer-events: none; }
.rg-panel {
  position: fixed; width: 440px;
  padding: 14px 16px; display: flex; flex-direction: column; gap: 10px;
  pointer-events: auto;
}
.rg-head { display: flex; align-items: center; justify-content: space-between; cursor: move; user-select: none; }
.rg-title { font-size: calc(14px * var(--font-scale)); font-weight: 600; }
.rg-close {
  width: 26px; height: 26px; border-radius: 6px; cursor: pointer; border: 0;
  background: rgba(var(--surface-rgb), 0.08); color: rgb(var(--text-rgb)); font-size: 13px;
}
.rg-close:hover { background: rgba(var(--surface-rgb), 0.16); }
.rg-hint { font-size: calc(11px * var(--font-scale)); opacity: .5; line-height: 1.5; }
.rg-body { height: 300px; overflow-y: auto; }
.rg-card {
  padding: 12px 14px; border-radius: 10px;
  background: rgba(var(--accent-rgb), 0.1); border: 1px solid rgba(var(--accent-rgb), 0.25);
  animation: rg-fade .25s ease;
}
@keyframes rg-fade { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
.rg-card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.rg-card-icon { font-size: calc(18px * var(--font-scale)); }
.rg-card-name { font-size: calc(14px * var(--font-scale)); font-weight: 600; color: var(--accent-ink); }
.rg-card-text { font-size: calc(12px * var(--font-scale)); line-height: 1.7; white-space: pre-line; opacity: .85; }
.rg-empty { text-align: center; padding: 24px 0; }
.rg-empty-icon { font-size: calc(32px * var(--font-scale)); margin-bottom: 8px; }
.rg-empty-text { font-size: calc(12px * var(--font-scale)); opacity: .5; line-height: 1.6; }
.rg-progress { display: flex; gap: 6px; justify-content: center; }
.rg-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: rgba(var(--surface-rgb), 0.2); transition: all .2s;
}
.rg-dot.done { background: rgba(var(--accent-rgb), 0.5); }
.rg-dot.active { background: var(--accent); transform: scale(1.3); }
.rg-footer { display: flex; align-items: center; justify-content: space-between; }
.rg-count { font-size: calc(11px * var(--font-scale)); opacity: .5; }
.btn {
  padding: 6px 16px; border-radius: 8px; font-size: calc(12px * var(--font-scale)); cursor: pointer;
  border: 1px solid rgba(var(--accent-rgb), 0.3); background: rgba(var(--accent-rgb), 0.2); color: var(--accent-ink);
}
.btn:hover { background: rgba(var(--accent-rgb), 0.35); }
</style>