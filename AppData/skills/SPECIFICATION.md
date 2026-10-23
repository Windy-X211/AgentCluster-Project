# 技能开发规范 (Skill Specification)

> 本规范描述 AgentCluster 技能系统的目录结构、SKILL.md 格式、依赖声明与渐进式加载机制。
> 遵循此规范创建的技能会被系统自动发现、自动安装依赖、自动注入到 AI 的可用能力列表。

## 1. 什么是"技能"

技能是一段结构化的 Markdown 指令 + 可选辅助代码，让 AI 在特定场景下获得专业能力。
与插件（Tool / Function Calling）不同，技能通过 prompt 注入工作，不需要有 Python `run()` 函数。

| 维度 | 插件 (Plugin) | 技能 (Skill) |
|------|--------------|-------------|
| 入口 | `run(**kwargs)` → 返回 dict | SKILL.md 正文 → 拼到 system prompt |
| 执行方式 | 代码直接执行 | AI 读取指令后按指令执行 |
| 适合场景 | 确定性的原子操作（HTTP 请求、文件操作） | 复杂的、需要推理的任务（写作、PPT 生成、规划） |
| LLM 感知 | function calling schema | name + description 写入 system prompt |

## 2. 目录结构

每个技能是一个独立文件夹，放在 `AppData/skills/` 下：

```
AppData/skills/
  my_skill/                ← 文件夹名（建议 kebab-case，与 frontmatter 的 name 一致）
    SKILL.md               ← 【必选】YAML frontmatter + Markdown 正文
    scripts/               ← 【可选】辅助脚本（Python / Shell / JS 等）
    references/            ← 【可选】参考文档（知识、模板、示例）
    assets/                ← 【可选】资源文件（图片、字体、CSV 数据等）
    package.json           ← 【可选】如果 scripts/ 里用 Node.js
    requirements.txt       ← 【可选】辅助脚本的 Python 依赖
```

**最小技能只需要一个 `SKILL.md` 文件**，没有其他任何东西也能正常工作。

## 3. SKILL.md 格式

```markdown
---
name: my-skill-name                      # 【必填】kebab-case，与文件夹名一致
description: 当用户要做 X 时加载此技能    # 【必填】AI 据此判断何时激活
label: 显示名称（可选）
version: "1.0.0"
author: YourName
tags: [writing, creative]

# 【可选】依赖声明 — 系统加载时自动检测安装
dependencies:
  pip:
    - "python-docx>=1.0.0"
  external:
    - name: "Node.js"
      url: "https://nodejs.org/"
      description: "生成式工具需要"
---

# 技能标题

## 何时使用

AI 读取的第一部分。明确告诉 AI 什么场景下加载这个技能。
用自然语言，AI 会把这段和用户请求做语义匹配。

## 工作流程

### 第 1 步：xxx
### 第 2 步：xxx

## 注意事项 / 边界

## 代码示例 / 模板

（Markdown 代码块可以嵌任何语言的代码）
```

### frontmatter 字段说明

| 字段 | 必选 | 类型 | 说明 |
|------|------|------|------|
| `name` | 是 | string | kebab-case，小写 + 连字符，与文件夹名一致 |
| `description` | 是 | string | 触发匹配的关键一句话。AI 读它来判断"要不要加载" |
| `label` | 否 | string | UI 显示名，不填则用 name |
| `version` | 否 | string | semver 版本号 |
| `author` | 否 | string | 作者 |
| `tags` | 否 | list[string] | 分类标签 |
| `dependencies` | **推荐** | object | 依赖声明（见第 5 节） |

## 4. 渐进式加载机制

技能采用三级渐进式加载，**零成本发现 + 按需加载正文**：

```
Level 1 — 发现（启动时）
  │  只扫描 SKILL.md 的 frontmatter
  │  把 {name, description} 写到 system prompt
  │  AI 知道有这个能力，但还看不到具体指令
  │
Level 2 — 激活（AI 判断相关时）
  │  AI 根据用户请求判断"这个技能有用"
  │  调 skill.invoke(name) → 读完整 SKILL.md body
  │  拼到 system prompt 里
  │  开始按指令工作
  │
Level 3 — 执行（按指令需要时）
     技能正文里提到 scripts/xxx.py 时
     AI 才去读那个脚本文件
```

好处：
- 启动极快（只扫描 frontmatter）
- AI 不会被所有技能的完整指令淹没
- 正文可以写得很详尽（比如 pptx 技能超过 12,000 字也没问题）

## 5. 依赖声明（dependencies）

与插件使用完全相同的依赖格式，写在 SKILL.md 的 YAML frontmatter 中。

### 5.1 完整格式

```yaml
---
name: pptx
description: 生成 PPT 演示文稿
dependencies:
  pip:
    - "python-pptx>=0.6.21"
    - "Pillow>=9.0.0"
  npm: true
  external:
    - name: "Node.js"
      url: "https://nodejs.org/"
      description: "如果使用 pptxgenjs 方案，需要 Node.js 环境"
---
```

### 5.2 简写

只有 pip 依赖时：

```yaml
dependencies: ["python-pptx>=0.6.21"]
```

### 5.3 检测与安装

| 类型 | 行为 |
|------|------|
| `pip` | 缺失时自动 `python -m pip install` |
| `npm` | 技能目录有 `package.json` 时自动 `npm install` |
| `external` | 只打印下载提示（名称 + URL + 描述），不自动安装 |

## 6. 编写指南

### 6.1 description 怎么写

`description` 是 AI 的触发开关。好的描述应该包含：
- **关键词**：用户可能说的话（中英文都写更好）
- **触发模式**：明确边界，不要过宽也不要过窄

```yaml
# ✅ 好：包含多个触发词 + 明确边界
description: 生成 PPT 演示文稿。当用户说"做个 PPT""生成幻灯片""PPT 模板""演示文稿"或任何包含"PPT""幻灯片""presentation""slide"的请求时加载。

# ❌ 差：太模糊
description: PPT 技能
```

### 6.2 正文结构建议

```markdown
---
name: example-skill
description: ...
---

# 标题（与 name 基本一致即可）

## 何时使用    ← 必写：明确触发场景

## 工作流程    ← 建议写：AI 按步骤走不会乱
  ### 第 1 步：...
  ### 第 2 步：...

## 输出格式    ← 建议写：明确交付物长什么样
  ```
  交付物模板...
  ```

## 注意事项    ← 建议写：边界、禁忌、安全约束

## 示例        ← 可选：一个或多个完整示例
```

### 6.3 scripts/ 辅助脚本

如果技能涉及复杂的文件处理（如 pptx 解包/打包），可以在 `scripts/` 下放 Python 脚本。
AI 在技能正文里写清楚何时调用哪个脚本、传入什么参数、期望什么输出。

```markdown
# 技能正文片段示例
## 处理 PPTX 文件
解压：`python scripts/unpack.py input.pptx -o output_dir`
修改 XML：用标准文件操作编辑 output_dir/ppt/slides/slide1.xml
打包：`python scripts/pack.py output_dir -o result.pptx`
```

### 6.4 assets/ 资源

放静态资源：CSV 数据表、图片、字体、模板文件等。
在技能正文里用相对路径引用。

### 6.5 references/ 参考文档

放知识密集型的参考资料：规范文档、API 说明、最佳实践等。
正文里用"参考 references/xxx.md"引导 AI 按需读取。

## 7. 完整示例

### 示例 A：极简技能（只有 SKILL.md）

```markdown
---
name: role-playing
description: 角色扮演。当用户说"角色扮演""扮演""模拟对话""你是一个"或任何想要 AI 扮演特定角色时加载。
version: "1.0.0"
author: AgentCluster
tags: [creative, roleplay]
---

# 角色扮演技能

## 何时使用
用户希望你扮演一个特定角色（历史人物、虚构角色、专业身份等）并与其对话。

## 工作流程
1. 明确角色设定（如果用户没给就主动问）
2. 进入角色：用角色的口吻、知识边界、行为方式回应用户
3. 保持一致性：不突然跳出角色，除非用户明确要求

## 注意事项
- 不模仿真实在世人物进行欺骗
- 不做医疗/法律/金融等专业领域的确定性建议
- 保持有趣但不越界
```

### 示例 B：带依赖和脚本的复杂技能

```
AppData/skills/docx-generator/
├── SKILL.md
├── scripts/
│   ├── docx_to_text.py
│   └── add_heading.py
├── references/
│   └── docx-style-guide.md
└── assets/
    └── default-template.docx
```

**SKILL.md：**
```yaml
---
name: docx-generator
description: 生成 Word(.docx) 文档。当用户说"写个 Word""生成文档""docx""Word 文件""写一份报告"或包含"文档""Word""docx"的请求时加载。
version: "1.1.0"
dependencies:
  pip: ["python-docx>=1.0.0"]
tags: [document, word, docx]
---
```

## 8. 加载流程

```
程序启动 → load_skills()
  ├─ 扫描 AppData/skills/*/SKILL.md
  ├─ 对每个目录：
  │    ├─ 检查 SKILL.md 是否存在
  │    ├─ 解析 YAML frontmatter（name / description / dependencies 等）
  │    ├─ 【NEW】如果有 dependencies 字段：
  │    │    ├─ pip: 检测已安装的，对缺失的逐个 pip install
  │    │    ├─ npm: 目录有 package.json 时执行 npm install
  │    │    └─ external: 打印下载提示（一次性）
  │    ├─ 检测 scripts/ / references/ / assets/ 是否存在
  │    └─ 注册到 SKILL_REGISTRY
  └─ 返回 {name: {label, description, tags, ...}}

运行时 → AI 在 system prompt 里看到所有技能的 name + description
  └─ AI 判断某个技能相关 → skill.invoke(name) 读完整正文 → 拼到 prompt
```

## 9. 故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| 技能不加载 | SKILL.md 缺失 / frontmatter name 为空 | 确认 SKILL.md 存在且 name 字段有效 |
| AI 没触发技能 | description 写得太模糊 | 加更多关键词，覆盖用户可能的说法 |
| 脚本执行报 ImportError | 依赖没声明 | 在 dependencies.pip 里声明，重启让系统自动装 |
| 外部应用提示一直在 | 提示只会打印一次（进程内缓存） | 重启进程会重置提示缓存 |
| 大技能加载慢 | 正文太长（超过 FULL_READ_LIMIT=12000 字） | 正文超过阈值时会走"只返回目录"的渐进加载，AI 需要按章节读取 |

## 10. 规范演进

- 当前版本：v1.0（2026-10）
- 兼容 [Agent Skills 开放标准](https://agentskills.io)（Anthropic 2025 年发布）
- `dependencies` 字段为本系统扩展，其他平台会忽略它
- 与插件规范共用同一套 `dependencies` 格式和依赖管理器
