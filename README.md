# AgentCluster 智能体集群

> **用自然语言描述你的团队，在画布上把它们连成一张协作网——本地运行、数据自持、能力持续扩展。**

AgentCluster 是一个 **Windows 本地优先** 的多智能体编排平台，把「组团队、派活儿、协作交接」这套管理学原样搬到 AI 身上。通过可视化画布编排智能体集群，实现复杂任务的协同解决。系统托盘一键启停前后端 + 自动打开浏览器，数据全部本地 JSON 存储。

## ✨ 特性

### 🎨 画布编排
- **无限画布**：自由拖拽节点，创建复杂的智能体网络
- **可视化协作**：节点代表智能体，连线代表成果流向
- **串行 / 并行执行**：灵活切换执行模式
- **拖拽优化**：鼠标移出画布边界仍能平滑拖动
- **感知范围**：`link` 感知（仅连线邻居）或 `global` 感知（画布全体），节点右键可切换

### 🗂 画布工作区
- **独立工作目录**：每个画布绑定一个 `workdir`，智能体按画布隔离读写文件
- **内置文件浏览器**：浏览 / 编辑 / 重命名 / 复制 / 删除工作区文件
- **一键运行**：直接运行脚本文件，Python 交互式控制台支持实时输入 / 停止
- **回收站**：删除文件进入回收站，可恢复或彻底清理
- **快照备份**：写文件自动留存快照，误改可回溯（`AppData/file_backups/`）

### 🤝 A2A 消息协议
- **自研 a2a-lite 协议**：结构化信封消息系统（`backend/core/a2a.py`）
- **讨论与往返**：`cluster.discuss` / `cluster.send_message` 支持多智能体协作讨论与多轮往返
- **消息可视化**：对话中实时展示消息流转（from → to 信封卡片）

### 🔧 能力扩展
1. **命令工具**：文件读写 / 替换 / 查找、命令执行、长运行进程后台化与探活
2. **技能系统**：小说创作、角色扮演、Web 演示页面生成（内置 + `AppData/skills/`）
3. **插件生态**：浏览器控制、SMTP 邮件、HTTP 请求（Dify 兼容清单）
### 🧩 组织与治理
- **智能体分组**：左侧分组管理，支持拖拽排序
- **规则系统**：按画布隔离的规章制度，注入成员上下文（`AppData/rules/`）
- **参数预设**：模型参数预设一键套用（`AppData/param_presets/`）
- **存储位置可切换**：一键迁移 / 接管数据根目录

### 🎯 智能感知的运行时
- **快慢双通道**：默认快通道（把 `<continue>` 续跑提示动态附加到本轮用户消息，不占系统提示词），检测到该标记自动切换慢通道并动态剔除提示与标记
- **自动验证**：独立验证器（`validator.py` + `tool_stream_validator.py`）确保任务完成度
- **工作流模式**：`plan` / `spec` / `goal` 三种模式，配套工具、状态机与阶段提示词

### 🔒 本地优先
- **数据自持**：全部数据存储在本地 JSON 文件
- **隐私安全**：参数加密（Fernet），密钥永不出本地（`backend/.runtime/param_preset.key`）
- **离线运行**：无需依赖外部服务
- **单实例托盘**：Windows 命名互斥量防重复启动，避免多 Vite 抢端口互顶

## 🛠 环境搭建

### 1. 安装 Python 3.13+

```bash
# Windows 推荐使用官方安装包或 scoop
# scoop 安装
scoop install python

# 验证安装
python --version
pythonw --version
```

### 2. 安装 Node.js 20+

```bash
# Windows 推荐使用官方安装包或 scoop
scoop install nodejs

# 验证安装
node --version
npm --version
```

### 3. 创建 Python 虚拟环境（推荐）

```bash
cd AgentCluster

# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
# PowerShell
.\.venv\Scripts\Activate.ps1
# CMD
.\.venv\Scripts\activate.bat

# 安装后端依赖
cd backend
pip install -r requirements.txt
```

### 4. 安装前端依赖

```bash
cd ../frontend
npm install
```

### 5. 配置环境变量（可选）

```bash
# 设置模型 API 密钥（编辑 AppData/settings.json）
# 或在启动前设置环境变量
set OPENAI_API_KEY=your-api-key
set BASE_URL=https://api.openai.com/v1
```

### 6. 验证安装

```bash
# 后端验证
cd backend
python main.py
# 访问 http://127.0.0.1:8870/api/health 应返回 {"status": "ok"}

# 前端验证（新终端）
cd frontend
npm run dev
# 访问 http://localhost:5173
```

## 🚀 快速开始

### 环境要求
- **Windows 10 / 11**（依赖 `pythonw` / 系统托盘）
- **Python 3.13+**
- **Node.js 20+**

### 克隆项目

```bash
git clone https://github.com/Windy-X211/AgentCluster-Project.git
cd 智能体集群
```

### 一键启动（推荐）

双击根目录下的 **`start.bat`**，托盘程序会自动：

1. 检测端口 8870 / 5173 占用情况，端口冲突自动换端口
2. 后台启动 FastAPI 后端 + Vite 前端
3. 健康检查通过后自动打开浏览器到 `http://localhost:5173`
4. 系统托盘常驻，可启动 / 停止 / 重启 / 打开界面 / 强制退出

### 手动启动（开发用）

```bash
# 1) 后端（http://127.0.0.1:8870）
cd backend
pip install -r requirements.txt
python main.py

# 2) 前端（http://localhost:5173）
cd frontend
npm install
npm run dev
```

## 📖 使用指南

### 四步搭建集群

1. **描述你的团队**  
   用自然语言描述角色分工，例如："产品经理 + 程序员 + QA"
2. **编排师自动搭建**  
   内置编排师智能体自动创建智能体、编写提示词、建立连线
3. **运行整队协作**  
   启动画布，各成员通过 A2A 消息接力工作
4. **迭代进化**  
   随时调整角色、连线、感知范围，集群越用越顺手

### 核心功能

#### 画布操作
- **节点拖拽**：左键拖动智能体节点
- **连线创建**：拖拽节点间的连线建立协作关系
- **右键菜单**：编辑智能体 / 发送消息测试 / 感知范围配置
- **运行控制**：单节点运行、集群整体运行

#### 对话界面
- **流式输出**：实时显示智能体思考和执行过程（HTTP + SSE）
- **消息溯源**：每条回复清楚标注来源智能体
- **集群模式**：与整个集群对话，或指定单个智能体

#### 智能体配置
- **系统提示词**：自定义智能体行为和角色
- **能力绑定**：关联命令、技能、插件
- **触发规则**：配置任务触发条件
- **感知范围**：`link`（仅连线邻居）或 `global`（画布全体）

#### 托盘菜单
| 菜单项 | 说明 |
|--------|------|
| 状态 | 实时健康检查（✅ / ⚠️ / ⏹） |
| 🚀 启动服务 | 拉起后端 + 前端 + 浏览器 |
| 🔄 重启全部 | 优雅 terminate → 兜底 taskkill |
| ⏹ 停止全部服务 | 停止子进程 + 释放端口 |
| 🌐 打开界面 | 前端 |
| ⚙ 后端健康检查 | `/api/health` |
| ❌ 退出 (停服务) | 后台线程停服务，立即关托盘 |
| ⚠️ 强制关闭托盘 | 只关托盘，子进程留给用户手动清理 |

## 🏗️ 架构

```
┌─────────────────────────────────────────┐
│ Windows 系统托盘启动器                   │
│ - start.bat                             │
│ - start_tray.py (pystray)               │
│ - 单实例互斥量 + 端口探活 + 健康轮询      │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│ Vue 3 SPA 前端                          │
│ - CanvasBoard 无限画布                  │
│ - MainView 流式对话（标明来源智能体）   │
│ - AgentEditDialog 智能体配置            │
│ - FileExplorerDialog 画布文件浏览器     │
│ - RulesDialog 规则 / ParamSelect 预设   │
│ - TopBar / RightPanel / WorkspaceOverlay │
└─────────────────┬───────────────────────┘
                  │ HTTP + SSE
┌─────────────────▼───────────────────────┐
│ FastAPI 后端 (localhost:8870)           │
│ - 9 个数据域路由                        │
│   agents / canvases / conversations     │
│   rules / param_presets / agent_groups  │
│   models / commands / settings          │
│ - 存储管理 + 带白名单的文件浏览器         │
│ - /api/fs/* 工作区文件读写接口           │
└─────────────────┬───────────────────────┘
                  │ 调度 + function calling
┌─────────────────▼───────────────────────┐
│ Core Engine (backend/core)              │
│ - agent_runtime 运行时引擎              │
│ - a2a 消息协议                          │
│ - commands 工具链 + pyconsole 控制台    │
│ - workflows plan/spec/goal 状态机       │
│ - intent_classifier 意图分类（备用未接线）│
│ - validator + tool_stream_validator     │
│ - param_presets + vault 加密             │
│ - workspace + file_backup 快照备份      │
│ - store 统一数据访问 + llm 多模型容错    │
└─────────────────┬───────────────────────┘
┌─────────────────▼───────────────────────┐
│ 扩展层                                  │
│ - 插件：browser_control / SMTP / HTTP   │
│ - 技能：小说创作 / 角色扮演 / Web 演示  │
│ - 规划：MCP 外部服务器接入 + 插件市场    │
│ - 存储：AppData JSON + Fernet 加密      │
└─────────────────────────────────────────┘
```

### 技术栈

**后端**
- Python 3.13
- FastAPI 0.110+ / Uvicorn 0.29+
- Pydantic 2.6+
- APScheduler 3.10+
- HTTPX 0.27+ / PyPDF 4.0+
- 本地 JSON 存储 + Fernet 加密

**前端**
- Vue 3.4+ / Vue Router 4.3+
- Vite 5.4+
- Pinia 2.1+ 状态管理
- VueUse 10.11+
- TypeScript 5.5+
- SCSS（`styles/liquid.scss`）

**桌面 / 启动器**
- `pystray` 系统托盘
- `pythoncom` / `win32com` 快捷方式生成
- Windows 命名互斥量（单实例）

## 📁 项目结构

```
智能体集群/
├── backend/                    # FastAPI 后端
│   ├── api/                    # API 路由（9 个数据域）
│   │   ├── agents.py           # 智能体接口
│   │   ├── agent_groups.py     # 智能体分组接口
│   │   ├── canvases.py         # 画布接口（含工作区文件 / 控制台）
│   │   ├── commands.py         # 能力清单 / 插件接口
│   │   ├── conversations.py    # 对话接口
│   │   ├── models.py           # 模型接口
│   │   ├── param_presets.py    # 参数预设接口
│   │   ├── rules.py            # 规章制度接口
│   │   └── settings.py         # 设置接口
│   ├── core/                   # 核心引擎
│   │   ├── agent_runtime.py    # 运行循环（工具循环 + 流式输出）
│   │   ├── a2a.py              # A2A 信封协议（discuss / send_message）
│   │   ├── commands.py         # 命令工具链 + cluster.* 编排工具
│   │   ├── workflows.py        # plan / spec / goal 工作流状态机
│   │   ├── pyconsole.py        # Python 交互式控制台
│   │   ├── intent_classifier.py# 意图分类（备用未接线）
│   │   ├── validator.py        # 独立验证器
│   │   ├── tool_stream_validator.py # 工具流式校验
│   │   ├── param_presets.py    # 参数预设业务
│   │   ├── vault.py            # 参数加密（Fernet）
│   │   ├── workspace.py        # 工作区文件管理
│   │   ├── file_backup.py      # 写文件快照备份
│   │   ├── store.py            # 统一数据访问
│   │   └── llm.py              # 多模型容错接入
│   ├── plugins/                # 插件桥接
│   │   ├── http_request.py     # HTTP 请求插件
│   │   ├── send_email.py       # SMTP 邮件插件
│   │   ├── mcp_bridge.py       # MCP 服务器桥接（规划中）
│   │   └── mcp_marketplace.py  # MCP 插件市场（规划中）
│   ├── skills/                 # 内置技能
│   │   ├── novel_writing.py    # 小说创作
│   │   └── role_playing.py     # 角色扮演
│   ├── mcp_vendor/             # MCP Python 二方包（已 vendored，待接通）
│   ├── .runtime/               # 运行时密钥（param_preset.key）
│   ├── main.py                 # FastAPI 入口（含 /api/fs/* 文件接口）
│   └── requirements.txt
│
├── frontend/                   # Vue 3 前端
│   ├── src/
│   │   ├── components/         # CanvasBoard / MainView / AgentEditDialog /
│   │   │                       # FileExplorerDialog / RulesDialog /
│   │   │                       # ParamSelect / TopBar / RightPanel /
│   │   │                       # WorkspaceOverlay / AgentList
│   │   ├── views/              # Home.vue / Settings.vue
│   │   ├── stores/             # Pinia 状态（agent/canvas/conversation/
│   │   │                       # rules/model/settings/layout）
│   │   ├── composables/        # 可复用组合式函数
│   │   ├── styles/liquid.scss  # 液态玻璃风格样式
│   │   ├── utils/              # avatar.ts / markdown.ts
│   │   ├── api.ts              # HTTP 客户端封装
│   │   ├── router.ts           # 路由
│   │   └── App.vue
│   ├── public/                 # icon.png 等静态资源
│   ├── vite.config.ts
│   └── package.json
│
├── AppData/                    # 用户数据存储（可切换位置）
│   ├── settings.json           # 全局设置
│   ├── mcp_servers.json        # MCP 服务器配置（预留，待接通）
│   ├── agents/                 # 智能体配置
│   ├── agent_groups/           # 智能体分组
│   ├── canvases/               # 画布配置 + 工作区文件
│   ├── conversations/          # 对话记录
│   ├── rules/                  # 规章制度
│   ├── workflows/              # 工作流状态（plan/spec/goal）
│   ├── file_backups/           # 文件快照备份
│   ├── skills/                 # 技能包（SKILL.md）
│   ├── plugins/                # 插件（manifest / tool.yaml / tool.py）
│   ├── model_interfaces/       # 模型接口配置
│   ├── param_presets/          # 参数预设
│   └── _legacy/                # 迁移前的旧格式数据
│
├── start.bat                   # Windows 一键启动（自动选 pythonw / python）
├── start_tray.py               # 系统托盘启动器（单实例 + 健康检查 + 菜单）
└── .agentcluster.pid           # 运行时 PID 文件
```

## 🎬 演示

演示截图请查看 `介绍/演示图片/` 目录：
- `image_30.png` — 完整界面展示
- `image_31.png` — 画布编排界面
- `image_32.png` — 智能体协作关系
- `image_33.png` — 集群配置面板

产品介绍 PPT：`智能体集群介绍/AgentCluster-产品介绍-实拍.html`

## 🛠 常见问题

**Q：启动时浏览器反复弹出？**  
托盘通过 Windows 命名互斥量（`Local\AgentCluster_Tray_SingleInstance`）保证单实例。若仍出现多实例，通常是老进程未退出——先运行「⚠️ 强制关闭托盘」再重试。

**Q：端口被占用怎么办？**  
启动器会自动探测 8870 / 5173，冲突时会换端口；托盘状态栏显示 ⚠️ 时请检查 `tray_startup.log` / `debug.log`。

**Q：数据能搬到别的机器吗？**  
把整个 `AppData/` 目录拷走即可（含加密密钥在 `backend/.runtime/`，一起拷）。也可以在「设置 → 存储位置」一键迁移数据根目录。

## 🤝 贡献

欢迎贡献代码、报告问题或提出建议！

1. Fork 项目
2. 创建特性分支 `git checkout -b feature/xxx`
3. 提交更改 `git commit -m "Add xxx"`
4. 推送分支 `git push origin feature/xxx`
5. 提交 Pull Request

## 📝 开发路线图

**已完成**
- [x] 画布工作目录与内置文件浏览器（运行 / 编辑 / 回收站）
- [x] 规章制度系统（按画布隔离）
- [x] 参数预设 + 智能体分组
- [x] 工作流模式（plan / spec / goal）
- [x] Python 交互式控制台
- [x] 工具流式校验器（防止工具调用参数被幻觉 / 截断）
- [x] 写文件快照备份、存储位置可切换
- [x] Windows 系统托盘单实例启动器（start_tray.py）
- [x] 多模型容错接入（core/llm.py）

**规划中**
- [ ] MCP 外部服务器接入 + 插件市场（backend/mcp_vendor 已 vendored，待接通）
- [ ] 集群模板市场（一键复用编排方案）
- [ ] 协作编辑模式（多人同画布）
- [ ] 性能监控面板（运行时指标可视化）
- [ ] 插件开发 SDK（标准化第三方插件接入）
- [ ] 跨平台支持（macOS / Linux 托盘 + 启动器）


## 📞 联系

如有问题或建议，请通过以下方式联系：
- Issues: 项目 GitHub Issues
- 邮箱：483636702@qq.com

## 🙏 致谢

感谢所有贡献者和测试用户的支持！

---

**AgentCluster v2.0.1** — 让 AI 团队像人类团队一样协作