# 功能拓展开发规范 (Extension Specification)

> 本规范描述 AgentCluster 功能拓展（Extension）系统的目录结构、manifest 格式、
> 渲染器协议与加载机制。遵循此规范创建的拓展可被「功能拓展」管理页安装/启用/卸载，
> 并在运行时由前端加载框架自动注册。
>
> **与「能力管理」插件的区别**：`AppData/plugins/` 下的插件是**后端工具插件**，
> 给智能体 function calling 调用的函数能力；`AppData/extensions/` 下的拓展是
> **前端/界面层拓展**，用于扩展本程序的界面样式与交互形态（可视化交互、屏幕答疑、
> 打电话、屏幕共享等）。

## 1. 目录结构

```
AppData/extensions/
  EXTENSION_SPEC.md              ← 本规范文档（随仓库首发）
  extensions_market.json         ← 内置市场清单（可安装的拓展元数据）
  installed.json                 ← 已安装拓展状态（运行时维护，勿手改）
  _builtin/                      ← 随仓库分发的内置拓展源（安装时拷贝到正式目录）
    visual_interaction/
      manifest.json
      renderer.js
      README.md
  <ext_id>/                      ← 每个已安装拓展一个目录（与 manifest.id 一致）
    manifest.json                ← 【必选】拓展元数据
    renderer.js                  ← viz-renderer 类拓展必选；前端渲染器代码
    backend.py                   ← backend-service 类拓展可选；后端服务代码
    README.md                    ← 【可选】说明文档
    *.js / *.json / ...          ← 【可选】拓展所需的其他资源
```

## 2. manifest.json（拓展元数据）

最小示例：

```json
{
  "id": "visual_interaction",
  "name": "可视化交互",
  "version": "1.0.0",
  "type": "viz-renderer",
  "author": "AgentCluster",
  "description": "解析智能体输出的 JSON，渲染为选择题/配图/流程图/思维导图/HTML/优化排布等富组件",
  "icon": "🧊",
  "tags": ["visualization", "interaction"],
  "frontend_files": ["renderer.js"],
  "viz_types": ["choice", "image", "flowchart", "mindmap", "html", "layout"]
}
```

### 字段说明

| 字段 | 必选 | 类型 | 说明 |
|------|------|------|------|
| `id` | 是 | string | 拓展唯一标识，kebab/snake_case，与目录名一致 |
| `name` | 是 | string | 显示名（中文） |
| `version` | 是 | string | semver 版本号 |
| `type` | 是 | string | 拓展类型：`viz-renderer` / `backend-service` / `ui-module` / `hybrid` |
| `author` | 否 | string | 作者 |
| `description` | 是 | string | 一句话描述 |
| `icon` | 否 | string | emoji 图标 |
| `tags` | 否 | string[] | 分类标签 |
| `frontend_files` | 否 | string[] | 前端需加载的文件名列表（默认 `["renderer.js"]`） |
| `system_prompt_file` | 否 | string | **system prompt 注入文件**（如 `system_prompt.md`）：拓展启用时其内容自动追加到所有智能体的 system prompt，让智能体天然获得该拓展的协议/能力说明；停用/卸载后自动撤销 |
| `viz_types` | 否 | string[] | viz-renderer 类拓展声明的可视化类型（见第 4 节） |
| `backend_entry` | 否 | string | backend-service 类拓展的后端入口模块名 |

### system_prompt_file 机制（拓展赋能智能体）

启用带 `system_prompt_file` 声明的拓展后，后端在每次构建智能体 system prompt 时
（`agent_runtime._system_prompt`）自动读取该文件并追加一段：

```
## 功能拓展协议 · <拓展名>（系统自动注入）
<system_prompt_file 文件内容>
```

- 注入对**所有智能体**生效（含画布集群成员与独立对话智能体），用户无需在智能体提示词里手动指明
- 实时生效：拓展停用/卸载后下一次请求即撤销；文件修改按 mtime 自动刷新
- 文件内容按拓展用途自定义（如 visual_interaction 注入 viz 输出协议）

### 拓展类型

| type | 作用 | 必备文件 |
|------|------|----------|
| `viz-renderer` | 解析智能体输出的 JSON，渲染为富组件 | `renderer.js` |
| `backend-service` | 提供后端能力（打电话、屏幕共享等） | `backend.py` |
| `ui-module` | 注入界面元素/样式 | `renderer.js` |
| `hybrid` | 同时含前端渲染与后端服务 | `renderer.js` + `backend.py` |

## 3. renderer.js（前端渲染器）

renderer.js 是一段在**浏览器主世界**执行的 JavaScript，运行时可访问：
- `window.Vue` —— Vue 3 运行时（含 `h`、`defineComponent`、`ref`、`reactive`、`computed`、`watch`、`onMounted`、`onUnmounted` 等）
- `window.__registerVizRenderer__(type, component)` —— 注册可视化渲染器
- `window.__registerUIModule__(id, install)` —— 注册 UI 模块（install(app) 在 Vue app 上挂载）
- `window.__extAPI__` —— 本程序提供给拓展的 API（见第 5 节）

### 3.1 viz-renderer 协议

调用 `window.__registerVizRenderer__(type, component)` 注册一个渲染器：
- `type`：可视化类型字符串（如 `"choice"`）
- `component`：一个 Vue 渲染函数组件 `(props) => VNode`，props 包含：
  - `data`：解析后的 JSON 数据对象
  - `raw`：原始 JSON 字符串
  - `extAPI`：`window.__extAPI__` 引用

示例（选择题渲染器）：

```javascript
(function () {
  const { h, defineComponent } = window.Vue;

  const Choice = defineComponent({
    props: { data: Object, raw: String, extAPI: Object },
    setup(props) {
      return () => h('div', { class: 'viz-choice' }, [
        h('div', { class: 'viz-q' }, props.data.question || ''),
        h('div', { class: 'viz-opts' },
          (props.data.options || []).map((o, i) =>
            h('button', {
              class: 'viz-opt',
              onClick: () => props.extAPI?.sendUserInput?.(typeof o === 'string' ? o : o.value)
            }, typeof o === 'string' ? o : o.label)
          )
        )
      ]);
    }
  });

  window.__registerVizRenderer__('choice', Choice);
})();
```

### 3.2 安全约束

- renderer.js 在主世界执行，**可访问 DOM 与 fetch**，请勿执行恶意代码
- 渲染富组件时优先用 `window.Vue.h` 构造 VNode，不要直接 innerHTML 注入未转义内容
- 需要图片/文件资源时走 `extAPI.imageURL(path)` / `extAPI.fetch(path)`，勿硬编码本地路径

## 4. 可视化交互协议（viz 协议）

智能体在对话中输出 markdown 代码块，语言标记为 `viz:<type>`，代码块内容为 JSON：

~~~
```viz:choice
{
  "question": "请选择部署方案",
  "options": ["方案A：云端", "方案B：本地", "方案C：混合"]
}
```
~~~

前端消息渲染时识别 `viz:<type>` 代码块，替换为对应渲染器组件。未注册该 type 时
回退显示原始 JSON 代码块。

### 内置可视化类型（由 visual_interaction 拓展提供）

| type | 数据结构 | 说明 |
|------|----------|------|
| `choice` | `{question, options:[string\|{label,value}], multiple?}` | 选择题，先选择再点「提交」按钮发送 |
| `fill` | `{question, blanks:[{label, placeholder?, type?}\|string]}` | 填空题，输入后点「提交」按钮发送（Enter 也提交）；单空可用顶层 `{question, label?, placeholder?}` 简写 |
| `image` | `{url, caption?, width?}` | 配图，支持本地/远程，可放大 |
| `flowchart` | `{nodes:[{id,label}], edges:[{from,to,label?}]}` | 流程图（简易 SVG） |
| `mindmap` | `{root:{label, children:[...]}}` | 思维导图（树形） |
| `html` | `{html, sandbox?}` | HTML 缩略卡片，可交互，可放大全屏 |
| `layout` | `{columns, items:[{type, data}]}` | 优化排布：多列网格嵌套任意 viz |

**提交按钮模式**：`choice` / `fill` 统一为「先作答 → 点提交按钮 → 发送后锁定」。
提交结果通过 `extAPI.sendUserInput` 回填到对话输入框并发送：
- choice：`"选项A, 选项C"`（多选逗号分隔）
- fill：`"姓名: 张三\n年龄: 25"`（有 label 用 `label: value` 格式，每空一行）

### 触发协议备选

除 ` ```viz:type` 代码块外，也支持裸 JSON 对象触发（消息内容整体是 JSON 且含 `__viz__` 字段）：

```json
{"__viz__": {"type": "choice", "data": {...}}}
```

## 5. 本程序提供给拓展的 API（window.__extAPI__）

| 方法 | 签名 | 说明 |
|------|------|------|
| `sendUserInput(text)` | `(text: string) => void` | 把用户在渲染器中的选择/输入回填到对话输入框并发送 |
| `insertInput(text)` | `(text: string) => void` | 仅回填到输入框，不自动发送 |
| `imageURL(path)` | `(path: string) => string` | 把本地图片路径转为可加载的 URL |
| `fetchAPI(path, opts)` | `(path, opts) => Promise<Response>` | 调用本程序后端 API（BASE 已自动拼接） |
| `openLightbox(src)` | `(src: string) => void` | 打开图片灯箱放大查看 |
| `getSettings()` | `() => object` | 获取当前用户设置 |
| `t(key)` | `(key: string) => string` | i18n（预留） |

## 6. 安装/启用/卸载流程

```
功能拓展管理页
  ├─ 展示市场清单（extensions_market.json）+ 已安装状态
  ├─ 安装：source=builtin → 从 _builtin/<id>/ 拷贝到 <id>/；source=local → 仅登记
  ├─ 启用/停用：写 installed.json 的 enabled 字段
  └─ 卸载：删除 <id>/ 目录 + 移除 installed.json 记录

前端启动
  ├─ GET /api/extensions/active → 拉取已启用拓展清单（含 renderer.js 代码文本）
  ├─ 对每个拓展：new Function(rendererCode)() 执行 → 内部调 __registerVizRenderer__ 注册
  └─ 消息渲染时识别 viz 代码块 → 查注册表派发
```

## 7. 开发一个新拓展的步骤

1. 在 `AppData/extensions/_builtin/<ext_id>/` 下建目录（随仓库分发）或直接在
   `AppData/extensions/<ext_id>/` 下建目录（本地）
2. 写 `manifest.json`（参考第 2 节）
3. 写 `renderer.js`（viz-renderer）或 `backend.py`（backend-service）
4. 在 `extensions_market.json` 中登记一条元数据（`source: "builtin"`）
5. 在功能拓展管理页点击「安装」→「启用」
6. 测试：让智能体输出对应 viz 代码块，验证渲染

## 8. 故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| 拓展不加载 | manifest.json 缺失或 JSON 非法 | 检查 manifest 格式 |
| 渲染器不生效 | renderer.js 执行报错 | 打开浏览器控制台查看异常 |
| viz 代码块显示为原文 | 对应 type 未注册 | 确认拓展已启用且 renderer.js 注册了该 type |
| 安装失败 | _builtin 源目录不存在 | 确认拓展源放在 _builtin/<id>/ 下 |

## 9. 规范版本

- 当前版本：v1.0（2026-10）
- 后续将支持：远程市场拉取、拓展间依赖、拓展配置页、样式注入沙箱