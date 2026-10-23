# 插件开发规范 (Plugin Specification)

> 本规范描述 AgentCluster 插件系统的目录结构、文件格式、依赖声明与加载机制。
> 遵循此规范创建的插件会被系统自动发现、自动安装依赖、自动注册为可调用工具。

## 1. 目录结构

每个插件是一个独立文件夹，放在 `AppData/plugins/` 下：

```
AppData/plugins/
  my_plugin/               ← 文件夹名 = kebab-case / snake_case，与 manifest.yaml 的 name 字段一致
    manifest.yaml          ← 【必选】插件级元数据
    tool.yaml              ← 【必选】LLM 工具定义（function calling schema）
    tool.py                ← 【必选】执行代码（必须有 run(**kwargs) 函数）
    requirements.txt       ← 【可选】额外 pip 依赖（也可写在 manifest 的 dependencies 里）
    package.json           ← 【可选】npm 依赖（如果插件用 Node.js）
    *.py / *.json / ...    ← 【可选】插件需要的其他模块/配置文件
```

## 2. manifest.yaml（插件元数据）

最小示例：

```yaml
version: "1.0.0"
type: "plugin"
name: "my_plugin"                    # 必须与文件夹名一致
label:
  zh_Hans: "我的插件"
  en_US: "My Plugin"
author: "YourName"
description:
  zh_Hans: "一句话描述插件做什么"
  en_US: "One-sentence description"
icon: "🔧"                            # emoji 图标
tags: ["utility"]
```

### 字段说明

| 字段 | 必选 | 类型 | 说明 |
|------|------|------|------|
| `version` | 是 | string | 版本号，遵循 semver |
| `type` | 是 | string | 固定 `"plugin"` |
| `name` | 是 | string | kebab-case 或 snake-case，必须与文件夹名一致 |
| `label` | 是 | dict | 多语言显示名，至少提供 `zh_Hans` 或 `en_US` |
| `author` | 否 | string | 作者名或团队名 |
| `description` | 是 | dict | 多语言描述 |
| `icon` | 否 | string | emoji 图标 |
| `tags` | 否 | list[string] | 分类标签 |
| `dependencies` | **推荐** | object | 依赖声明（见第 4 节） |

## 3. tool.yaml（工具定义）

LLM 通过此文件知道插件能做什么、需要什么参数：

```yaml
identity:
  name: "my_plugin"                   # 必须与 manifest 的 name 一致

description: "执行某个操作，LLM 调之前会读这句话"

parameters:
  - name: "input_text"
    type: string
    required: true
    description: "用户要处理的文本"
  - name: "max_length"
    type: integer
    required: false
    description: "最大输出长度"
    default: 100
  - name: "mode"
    type: string
    required: false
    description: "处理模式"
    enum: ["fast", "deep"]

outputs:                              # 可选：描述 tool.py run() 的返回字段
  - name: "result"
    type: string
```

### parameter 字段说明

| 字段 | 必选 | 类型 | 说明 |
|------|------|------|------|
| `name` | 是 | string | 参数名 |
| `type` | 是 | string | `"string"` / `"integer"` / `"number"` / `"boolean"` |
| `required` | 否 | bool | 是否必填，默认 false |
| `description` | 否 | string | LLM 如何使用这个参数 |
| `default` | 否 | any | 默认值 |
| `enum` | 否 | list | 可选值列表 |

## 4. tool.py（执行代码）

必须暴露一个 `run(**kwargs)` 函数，返回 dict 结果：

```python
"""my_plugin —— 工具实现"""

def run(input_text: str, max_length: int = 100, mode: str = "fast") -> dict:
    """插件执行入口。

    Args:
        input_text: 用户传入的文本
        max_length: 最大输出长度
        mode: 处理模式

    Returns:
        {"result": "...", "meta": {...}}  或  {"error": "..."}
    """
    try:
        # 你的业务逻辑
        result = input_text[:max_length]
        return {"result": result, "mode": mode}
    except Exception as e:
        return {"error": str(e)}
```

### 最佳实践

- **总是 try-except**：捕获所有异常，返回 `{"error": "..."}` 而非让程序崩溃
- **kwargs 兼容**：函数签名可以用明确的参数名，LLM 会按 tool.yaml 传参
- **不要阻塞过久**：`run()` 应在合理时间内返回，耗时操作放到子线程
- **状态管理**：如果插件需要跨调用保持状态（如窗口句柄、连接），用模块级变量保存

## 5. 依赖声明（dependencies）

在 `manifest.yaml` 中声明依赖，系统加载插件时会**自动检测并安装**。

### 5.1 完整格式

```yaml
dependencies:
  # Python pip 包（缺失时自动 pip install）
  pip:
    - "requests>=2.28.0"
    - "Pillow>=9.0.0"
    - "python-docx>=1.0.0"

  # Node.js 包（插件目录有 package.json 时自动 npm install）
  # 写 true 表示"检查 package.json 并安装"
  npm: true

  # 外部应用（只打印下载提示，不自动安装）
  external:
    - name: "Everything Search"
      url: "https://www.voidtools.com/"
      description: "快速文件搜索工具，everything_search 插件必需"

    - name: "Python 3.10+"
      url: "https://www.python.org/downloads/"
      description: "运行插件所需的 Python 版本"
```

### 5.2 简写格式

如果只有 pip 依赖，可以用最简写法：

```yaml
dependencies: ["requests", "Pillow>=9.0.0"]
```

### 5.3 依赖检测机制

| 类型 | 检测方式 | 安装方式 | 失败处理 |
|------|----------|----------|----------|
| pip | importlib + `pip show` | `python -m pip install` | 打印 WARN，不阻塞插件加载 |
| npm | 检查 `package.json` 是否存在 | `npm install` | 打印 WARN，不阻塞插件加载 |
| external | 不检测 | 只打印名称 + 下载 URL | 一次性提示，不阻塞 |

### 5.4 requirements.txt 兼容

如果插件目录下有 `requirements.txt` 但没有 manifest 的 `dependencies.pip`，
系统**不会**自动读 requirements.txt（保持 manifest 为唯一依赖声明源）。
建议把核心依赖统一写到 manifest 中。

## 6. 加载流程

```
程序启动 → load_plugins()
  ├─ 扫描 AppData/plugins/*/
  ├─ 对每个目录：
  │    ├─ 检查 manifest.yaml / tool.yaml / tool.py 是否都存在
  │    ├─ 解析 manifest.yaml 元数据
  │    ├─ 【NEW】解析 dependencies 字段
  │    │    ├─ pip: 检测已安装的，对缺失的逐个 pip install
  │    │    ├─ npm: 目录有 package.json 时执行 npm install
  │    │    └─ external: 打印下载提示（一次性）
  │    ├─ 动态 import tool.py
  │    └─ 注册到 PLUGIN_REGISTRY
  └─ 已删除/损坏的插件自动从注册表移除
```

### 热重载

修改插件文件后，调用 `load_plugins()` 即可刷新注册表，无需重启程序：

```python
from plugins import load_plugins
load_plugins()  # 会自动重新检测依赖
```

## 7. 完整示例

### 示例 A：简单 HTTP 请求插件

```
AppData/plugins/http_request/
├── manifest.yaml
├── tool.yaml
└── tool.py
```

**manifest.yaml：**
```yaml
version: "0.0.1"
type: "plugin"
name: "http_request"
label:
  zh_Hans: "HTTP 请求"
  en_US: "HTTP Request"
author: "AgentCluster"
description:
  zh_Hans: "发起 GET/POST 请求访问外部 HTTP API"
  en_US: "Make HTTP requests to external APIs"
icon: "🌐"
tags: ["network", "http"]
dependencies:
  pip: ["requests>=2.28.0"]
```

**tool.yaml：**
```yaml
identity:
  name: "http_request"
description: "发起 HTTP 请求，返回响应内容"
parameters:
  - name: "url"
    type: string
    required: true
    description: "目标 URL"
  - name: "method"
    type: string
    required: false
    default: "GET"
    enum: ["GET", "POST", "PUT", "DELETE"]
    description: "HTTP 方法"
  - name: "headers"
    type: string
    required: false
    description: "可选的 JSON headers 字符串"
  - name: "body"
    type: string
    required: false
    description: "请求体（POST/PUT 时使用）"
  - name: "timeout"
    type: integer
    required: false
    default: 10
    description: "超时秒数"
```

**tool.py：**
```python
import json

def run(url: str, method: str = "GET", headers: str = "", body: str = "", timeout: int = 10) -> dict:
    try:
        import requests
        hdrs = json.loads(headers) if headers else {}
        resp = requests.request(method, url, headers=hdrs, data=body, timeout=timeout)
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": resp.text,
        }
    except ImportError:
        return {"error": "requests 库未安装。请确保 dependencies 已正确声明。"}
    except Exception as e:
        return {"error": str(e)}
```

### 示例 B：需要外部应用的插件

```yaml
# manifest.yaml
name: "everything_search"
description: "Everything 快速文件搜索"
dependencies:
  pip: ["pywin32>=306"]
  external:
    - name: "Everything Search"
      url: "https://www.voidtools.com/"
      description: "必须先安装 Everything 并启用 HTTP 服务器（端口默认 8080）"
```

加载时会自动安装 pywin32，并在控制台打印 Everything 的下载提示。

## 8. 故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| 插件不加载 | 缺少 manifest.yaml / tool.yaml / tool.py | 检查三文件是否齐全 |
| `import xxx` 失败 | 依赖没声明或 pip install 失败 | 在 dependencies.pip 里声明，重启程序让它自动装 |
| npm install 没执行 | 目录下没 package.json 或 npm 不在 PATH | 确认 package.json 存在，手动 npm install |
| 依赖安装失败但插件还在 | 依赖检测失败不会阻止加载 | 在 tool.py 的 run() 里 try-except 返回 error |
| 依赖提示一直出现 | external 下载提示只会打印一次 | 重启程序会重置提示缓存 |

## 9. 规范演进

- 当前版本：v1.0（2026-10）
- 与 Dify plugin 规范兼容（manifest.yaml / tool.yaml 字段对齐）
- 新增 `dependencies` 字段为本系统扩展，其他平台会忽略它
