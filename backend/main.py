"""智能体集群 - FastAPI 入口"""
import os
import asyncio
import zlib
from fastapi import FastAPI, Request, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers, MutableHeaders
from contextlib import asynccontextmanager

from core.store import init_store, load, upsert, get_store_paths, open_folder, pick_folder, set_root, ROOT
from api import agents, agent_groups, canvases, conversations, models, settings, commands, rules, param_presets, plugins_debug, asr, backgrounds

from datetime import datetime


ORCHESTRATOR_NAME = "集群编排师"

ORCHESTRATOR_SYSTEM_PROMPT = """你是「集群编排师」，一位资深的 AI Agent 架构师。用户用自然语言描述他们想要的多智能体集群，你负责通过 function calling 直接在系统里创建、修改、连接这些智能体和画布。

## 你的工作方式

1. **先理解再动手**：用户说「帮我搭一个产品研发集群」，你先把这个角色团队想清楚，列出需要哪些智能体、各自职责、协作顺序。
2. **先查能力清单**：动手之前先调用 `cluster.list_capabilities`，看看系统里当前有哪些命令、技能、插件可用。这一步非常重要——你需要根据每个智能体的职责，从能力清单里挑选合适的工具分配给它。
3. **一次一个工具调用**：按顺序调用 cluster.* 工具。通常的工作流是：
   - 先 `cluster.list_canvases` 看有没有同名画布（避免重复）
   - `cluster.create_canvas` 新建画布
   - 对每个角色：`cluster.list_agents` 看是否已有 → 没有则 `cluster.create_agent` 创建（**一定要写好 system_prompt，定义清楚角色的口吻、原则、边界；同时一定要根据角色职责从能力清单中选择 commands / skills / plugins 配置进去，不要留空**）
   - `cluster.add_node` 把智能体加到画布上
   - `cluster.connect` 把节点按协作顺序连起来（A→B 表示 B 依赖 A 的产出）
4. **修改也直接来**：用户说「把 XX 智能体的提示词改一下」「给它加个浏览器能力」「让 A 直接汇报给 C」，你直接调 cluster.update_agent / add_node / connect。涉及能力变更时先 `cluster.list_capabilities` 确认当前有哪些可用。
5. **操作完告知用户**：给一份简洁的总结，告诉用户你做了什么、当前画布状态是什么、每个智能体分配了哪些能力，附一段后续可聊的话。

## 能力配置原则（非常重要！）

每个智能体都需要根据其职责分配合适的命令、技能、插件。不要把三个列表都留空。

**三类能力说明：**
- **命令 (commands)**：系统内建工具，所有智能体都适用。常用的有 `file.read`（读文件）、`file.write`（写文件）、`file.patch`（补丁编辑）、`file.search`（搜索内容）、`cmd.run`（运行终端命令）、`workspace.search`（工作区语义检索）。`cluster.*` 系列是编排师自己用的，不要分配给普通智能体。
- **技能 (skills)**：专业工作流/领域能力，按需分配。例如「PPT 制作角色」→ `pptx`；「小说写作角色」→ `novel_writing` + `role_playing`；「网页 Demo 角色」→ `web_demo`。
- **插件 (plugins)**：外部功能扩展。例如「浏览器操控/网页测试角色」→ `browser_control`；「桌面自动化角色」→ `desktop_control`；「邮件通知角色」→ `send_email`；「HTTP 请求角色」→ `http_request`；「AI 绘画角色」→ `sd_webui`。

**常见角色 → 能力映射（参考）：**
| 角色 | 建议配置的命令 | 建议的技能 | 建议的插件 |
|------|--------------|-----------|-----------|
| 通用执行者 | file.read, file.write, file.search, cmd.run | — | — |
| 代码/开发 | file.read, file.write, file.patch, file.replace, cmd.run | — | github_connector |
| 文档/写作 | file.read, file.write, file.patch | novel_writing, role_playing | — |
| PPT/演示文稿 | file.read, file.write, cmd.run | pptx | — |
| 浏览器测试 | file.read | — | browser_control |
| 桌面自动化 | — | — | desktop_control |
| 邮件/通知 | file.read | — | send_email |
| AI 绘图 | file.write, cmd.run | — | sd_webui |
| HTTP API | file.read | — | http_request |

先用 `cluster.list_capabilities` 查一下当前可用的具体名称和描述，再做选择。

## 设计原则

- **每个智能体的 system_prompt 必须明确**：角色、背景、核心职责、输出风格、边界（明确不做什么）。
- **每个智能体都要有能力配置**：commands / skills / plugins 三类至少配一类。没有任何能力的智能体等于"光杆司令"，只能靠自身语言模型干想活。
- **画布就是依赖图**：连线方向 A→B = A 的产出喂给 B。启动时画布上所有节点会协作完成任务。
- **不要过度设计**：从 2-4 个角色起步，用户可以随时让你扩。
- **中文对话为主**，用户说什么语言就用什么语言。

## 可调用工具（cluster.* 系列）
你默认拥有全部 cluster.* 工具权限。列表详见 tools。

现在，等用户描述他们的集群愿景。"""


ORCHESTRATOR_RULE_CMDS = [
    "cluster.list_rules", "cluster.create_rule", "cluster.update_rule", "cluster.delete_rule",
]

ORCHESTRATOR_GROUP_CMDS = [
    "cluster.list_groups", "cluster.create_group", "cluster.update_group",
    "cluster.delete_group", "cluster.assign_agent_group",
]

ORCHESTRATOR_CAPABILITY_CMDS = [
    "cluster.list_capabilities",
]


def _ensure_orchestrator():
    """启动时如果没有「集群编排师」agent，就自动创建一个；已有则补齐缺失的 cluster.* 命令"""
    agents = load("agents")
    for a in agents:
        if a.get("name") == ORCHESTRATOR_NAME and any(
            str(c).startswith("cluster.") for c in (a.get("commands") or [])
        ):
            # 已存在 → 确保规章制度 / 分组工具 / 能力查询工具可用
            cmds = a.get("commands") or []
            missing = [c for c in ORCHESTRATOR_RULE_CMDS + ORCHESTRATOR_GROUP_CMDS + ORCHESTRATOR_CAPABILITY_CMDS if c not in cmds]
            if missing:
                a["commands"] = cmds + missing
                upsert("agents", a)
            return  # 已存在且加载了 cluster 工具
    item = {
        "id": max((a.get("id", 0) for a in agents), default=0) + 1,
        "name": ORCHESTRATOR_NAME,
        "description": "自然语言对话，帮你创建/修改多智能体集群",
        "system_prompt": ORCHESTRATOR_SYSTEM_PROMPT,
        "model": "",  # 留空走默认
        "interface_id": None,
        # 默认加载全部 cluster.* 命令 —— 这是编排师的核心能力
        "commands": [
            "cluster.list_agents", "cluster.list_capabilities",
            "cluster.create_agent", "cluster.update_agent", "cluster.delete_agent",
            "cluster.list_canvases", "cluster.create_canvas", "cluster.delete_canvas", "cluster.get_canvas",
            "cluster.add_node", "cluster.remove_node", "cluster.connect", "cluster.disconnect",
            # 规章制度（按画布隔离）
            *ORCHESTRATOR_RULE_CMDS,
            # 智能体分组（agent_groups）
            *ORCHESTRATOR_GROUP_CMDS,
            # 同时给基础文件能力，方便写系统提示词时参考
            "file.read", "file.write", "file.structure",
        ],
        "skills": [],
        "plugins": [],
        "trigger": {"type": "manual", "cron": "", "message": ""},
        "tags": ["系统", "编排"],
        "enabled": True,
        "created_at": datetime.utcnow().isoformat(),
    }
    upsert("agents", item)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_store()
    _ensure_orchestrator()
    yield


class JsonGzipMiddleware:
    """只对 application/json 响应启用 gzip（工作区全量树等大 JSON，可缩到 ~1/15）。

    流式接口（text/plain 的对话流 / text/event-stream）完全不经过压缩，
    避免对聊天流式输出产生任何影响。

    ⚠️ 关键优化：
    - minimum_size 提高到 128KB：本地 loopback 网络带宽无限，小响应压缩/解压
      的 CPU 开销远大于传输时间，反而增加延迟（之前 agents 52KB 压缩花 400ms+）
    - 用 asyncio.to_thread 把 CPU 密集的 zlib.compress 放到线程池，不阻塞 event loop
      （之前同步压缩 24MB 大响应直接卡死整个服务 1.3 秒）
    """

    def __init__(self, app, minimum_size: int = 131072):
        self.app = app
        self.minimum_size = minimum_size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if "gzip" not in (headers.get("Accept-Encoding") or "").lower():
            await self.app(scope, receive, send)
            return
        state = {"initial": None, "compressor": None, "started": False, "skip": False}

        async def send_wrapper(message):
            t = message.get("type")
            if t == "http.response.start":
                state["initial"] = message
                h = Headers(raw=message["headers"])
                ct = h.get("content-type", "").partition(";")[0].strip().lower()
                state["skip"] = (ct != "application/json") or ("content-encoding" in h)
            elif t == "http.response.body":
                if state["skip"]:
                    if not state["started"]:
                        state["started"] = True
                        await send(state["initial"])
                    await send(message)
                    return
                body = message.get("body") or b""
                more = bool(message.get("more_body"))
                # 小响应不值得压缩：本地网络传输 ~0ms，压缩/解压 CPU 开销更大
                if not state["started"] and not more and len(body) < self.minimum_size:
                    state["started"] = True
                    await send(state["initial"])
                    await send(message)
                    return
                if state["compressor"] is None:
                    state["compressor"] = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
                comp = state["compressor"]
                # 把 CPU 密集的 zlib 操作放到线程池，不阻塞 event loop
                def _do_compress():
                    out = comp.compress(body)
                    out += comp.flush(zlib.Z_SYNC_FLUSH) if more else comp.flush()
                    return out
                out = await asyncio.to_thread(_do_compress)
                if not state["started"]:
                    state["started"] = True
                    mh = MutableHeaders(raw=list(state["initial"]["headers"]))
                    if "content-length" in mh:
                        del mh["content-length"]
                    mh["Content-Encoding"] = "gzip"
                    mh.add_vary_header("Accept-Encoding")
                    await send({**state["initial"], "headers": mh.raw})
                await send({"type": "http.response.body", "body": out, "more_body": more})
            else:
                await send(message)

        await self.app(scope, receive, send_wrapper)


app = FastAPI(title="AgentCluster", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(JsonGzipMiddleware, minimum_size=2048)

ROUTES = [
    ("agents", agents.router),
    ("agent_groups", agent_groups.router),
    ("canvases", canvases.router),
    ("conversations", conversations.router),
    ("model_interfaces", models.router),
    ("settings", settings.router),
    ("commands", commands.router),
    ("rules", rules.router),
    ("param_presets", param_presets.router),
    ("plugins_debug", plugins_debug.router),
    ("asr", asr.router),
    ("backgrounds", backgrounds.router),
]
for name, r in ROUTES:
    app.include_router(r, prefix=f"/api/{name}", tags=[name])


@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.2.0"}


# ==============================================================
# 开机自启动（Windows 注册表 HKCU Run 键）
# ==============================================================
_AUTOSTART_KEY_NAME = "AgentCluster"


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_autostart_cmd() -> str | None:
    """拼出自启动命令字符串（pythonw.exe launcher.py 的绝对路径形式）。找不到则返回 None。"""
    root = _project_root()
    launcher = os.path.join(root, "launcher.py")
    if not os.path.exists(launcher):
        return None
    # 优先用 pythonw.exe（无控制台），找不到再兜底 python.exe
    import shutil as _shutil
    pythonw = _shutil.which("pythonw")
    if not pythonw:
        # 尝试常见路径
        for p in [
            r"C:\Users\qiqi\AppData\Local\Programs\Python\Python314\pythonw.exe",
            r"F:\anaconda3\pythonw.exe",
        ]:
            if os.path.exists(p):
                pythonw = p
                break
    if not pythonw:
        pythonw = _shutil.which("python") or "python"
    # 用绝对路径，整个命令用引号包起来（注册表 Run 值里有空格时必须加引号）
    return f'"{pythonw}" "{launcher}"'


def _get_autostart_state() -> tuple[bool, str]:
    """返回 (是否启用, 当前命令字符串)。非 Windows 平台始终返回 (False, '')。"""
    if os.name != "nt":
        return False, ""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_READ,
        )
        try:
            val, _ = winreg.QueryValueEx(key, _AUTOSTART_KEY_NAME)
            winreg.CloseKey(key)
            return True, val
        except FileNotFoundError:
            winreg.CloseKey(key)
            return False, ""
    except Exception:
        return False, ""


def _set_autostart(enable: bool) -> tuple[bool, str]:
    """启用或禁用自启动。返回 (实际状态, 命令字符串)。"""
    if os.name != "nt":
        return False, ""
    import winreg
    run_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    if enable:
        cmd = _build_autostart_cmd()
        if not cmd:
            raise RuntimeError("找不到 launcher.py，无法设置自启动")
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_path)
        winreg.SetValueEx(key, _AUTOSTART_KEY_NAME, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        return True, cmd
    else:
        # 先确保键存在再删值
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_path, 0, winreg.KEY_SET_VALUE)
            try:
                winreg.DeleteValue(key, _AUTOSTART_KEY_NAME)
            except FileNotFoundError:
                pass
            winreg.CloseKey(key)
        except FileNotFoundError:
            pass  # 连 Run 键都不存在 → 视为已关闭
        return False, ""


@app.get("/api/autostart")
def get_autostart():
    enabled, cmd = _get_autostart_state()
    return {"enabled": enabled, "command": cmd, "supported": os.name == "nt"}


@app.put("/api/autostart")
def set_autostart(body: dict):
    enable = bool(body.get("enabled"))
    try:
        enabled, cmd = _set_autostart(enable)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"设置自启动失败：{e}")
    return {"enabled": enabled, "command": cmd, "supported": os.name == "nt"}


# ---- 启动模式（客户端 / 网页端）----
# 写项目根的 .launch_mode.txt，供 start.bat 读取
_LAUNCH_MODE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".launch_mode.txt")

@app.get("/api/launch_mode")
def get_launch_mode():
    mode = "client"
    if os.path.exists(_LAUNCH_MODE_FILE):
        m = open(_LAUNCH_MODE_FILE, "r", encoding="utf-8").read().strip()
        if m in ("client", "web"):
            mode = m
    return {"mode": mode}


@app.put("/api/launch_mode")
def set_launch_mode(body: dict):
    mode = (body.get("mode") or "client").strip()
    if mode not in ("client", "web"):
        mode = "client"
    try:
        with open(_LAUNCH_MODE_FILE, "w", encoding="utf-8") as f:
            f.write(mode)
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(500, f"failed to write .launch_mode.txt: {e}")
    return {"mode": mode}


# ---- 数据存储路径 ----
@app.get("/api/storage/paths")
def storage_paths():
    """返回所有数据目录/文件的绝对路径 + 每域记录数，用于 UI 展示"""
    return get_store_paths()


@app.post("/api/storage/open")
def storage_open(payload: dict):
    """在系统资源管理器里打开指定路径"""
    path = payload.get("path", "")
    if not path:
        from fastapi import HTTPException
        raise HTTPException(400, "path required")
    ok = open_folder(path)
    return {"ok": ok, "path": path}


@app.post("/api/storage/pick_folder")
def storage_pick_folder(payload: dict):
    """弹出系统文件夹选择器，返回所选路径（取消返回空串）。支持自定义对话框标题。"""
    initial = payload.get("initial", "") or ""
    title = payload.get("title") or "选择数据存储位置"
    path = pick_folder(initial, title)
    return {"path": path}


@app.post("/api/storage/set_location")
def storage_set_location(payload: dict):
    """切换存储根目录（不迁移数据，按新位置已有数据接管），成功后返回新 paths。"""
    new_path = (payload.get("path") or "").strip()
    if not new_path:
        from fastapi import HTTPException
        raise HTTPException(400, "path required")
    try:
        new_paths = set_root(new_path)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(400, str(e))
    # 让 skills / plugins 重新扫描新 ROOT
    try:
        from commands import reload_capabilities
        reload_capabilities()
    except Exception as e:
        print(f"[storage] reload_capabilities 失败（不阻断切换）：{e}")
    return {"ok": True, "paths": new_paths}


# ---- 文件浏览器（限定允许根目录内，防路径穿越）----
from pathlib import Path as _P


# 允许读取的根：AppData 数据目录 + 进程工作区（插件截图保存地）+ 临时截图兜底目录
_WORKSPACE_ROOT = _P.cwd().resolve()
_TEMP_SHOT_ROOT = _P.home() / "AppData" / "Local" / "Temp" / "agentcluster_screenshots"


def _safe_resolve(path_str: str) -> _P:
    """解析路径，确保在允许的根（AppData ROOT / 进程工作区 / 临时截图目录）内。返回 Path 对象。"""
    import core.store as _store
    p = _P(path_str).resolve()
    allowed = (_store.ROOT.resolve(), _WORKSPACE_ROOT, _TEMP_SHOT_ROOT.resolve())
    for root in allowed:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    from fastapi import HTTPException
    raise HTTPException(403, f"path 超出允许范围：{path_str}")


def _is_under(p: _P, root: _P) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _collect_workdir_roots() -> list[_P]:
    """收集所有画布 workdir + 会话级 workdir + 全局 default_workdir，供 /api/fs/image 放行工作区图片。

    智能体常把图片生成在画布工作区里（如 PPT 导出的 qa_png/overview.png），
    这些路径不在 _safe_resolve 的固定允许根内，缩略图走 /api/fs/image 加载时需放行。
    会话可携带自己的工作区（与画布不同），同样需要放行。
    """
    roots: list[_P] = []
    try:
        import core.store as _store
        for c in (_store.load("canvases") or []):
            wd = c.get("workdir")
            if wd:
                try:
                    roots.append(_P(wd).resolve())
                except Exception:
                    pass
        # 会话级工作区：走 list payload 缓存，只解析 workdir 字段，避免全量构建会话对象
        import json as _json
        for _key, _payload in _store.load_list_payloads("conversations", lambda c: str(c.get("id", 0))):
            try:
                wd = (_json.loads(_payload) or {}).get("workdir")
                if wd:
                    roots.append(_P(wd).resolve())
            except Exception:
                continue
        s = _store.load("settings") or {}
        dwd = s.get("default_workdir")
        if dwd:
            try:
                roots.append(_P(dwd).resolve())
            except Exception:
                pass
    except Exception:
        pass
    return roots


@app.get("/api/fs/list")
def fs_list(path: str = ""):
    """列出指定目录内容，返回 [{name, path, is_dir, size, modified}]"""
    import core.store as _store
    target = _safe_resolve(path or str(_store.ROOT))
    if not target.is_dir():
        from fastapi import HTTPException
        raise HTTPException(400, f"不是目录：{path}")
    items = []
    for entry in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            stat = entry.stat()
            items.append({
                "name": entry.name,
                "path": str(entry),
                "is_dir": entry.is_dir(),
                "size": stat.st_size if entry.is_file() else 0,
                "modified": int(stat.st_mtime),
                # 类型提示，方便前端选图标
                "ext": entry.suffix.lower().lstrip("."),
            })
        except PermissionError:
            continue
    return {"ok": True, "path": str(target), "root": str(_store.ROOT), "items": items}


@app.get("/api/fs/read")
def fs_read(path: str = ""):
    """读取文件内容。只允许文本文件（按扩展名白名单）。"""
    if not path:
        from fastapi import HTTPException
        raise HTTPException(400, "path required")
    fp = _safe_resolve(path)
    if not fp.is_file():
        from fastapi import HTTPException
        raise HTTPException(400, f"不是文件：{path}")

    ALLOWED_TEXT_EXTS = {
        "json", "yaml", "yml", "md", "txt", "toml", "ini", "cfg",
        "py", "js", "ts", "vue", "html", "css", "scss", "svg",
        "log", "csv", "xml", "env",
    }
    ext = fp.suffix.lower().lstrip(".")
    if ext and ext not in ALLOWED_TEXT_EXTS:
        from fastapi import HTTPException
        raise HTTPException(400, f"不支持读取的文件类型：.{ext}")
    if fp.stat().st_size > 2 * 1024 * 1024:  # 2 MB 限制
        from fastapi import HTTPException
        raise HTTPException(400, "文件过大（>2MB），不支持在线编辑")

    try:
        content = fp.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # 尝试 gbk（Windows 常见）
        try:
            content = fp.read_text(encoding="gbk")
        except Exception as e:
            from fastapi import HTTPException
            raise HTTPException(400, f"编码无法识别：{e}")

    return {
        "ok": True,
        "path": str(fp),
        "name": fp.name,
        "ext": ext,
        "size": fp.stat().st_size,
        "content": content,
    }


@app.put("/api/fs/write")
def fs_write(payload: dict):
    """保存文件内容。内容通过 UTF-8 写入。"""
    path = payload.get("path", "")
    content = payload.get("content", "")
    if not path:
        from fastapi import HTTPException
        raise HTTPException(400, "path required")
    fp = _safe_resolve(path)
    if fp.is_dir():
        from fastapi import HTTPException
        raise HTTPException(400, f"目标是目录：{path}")
    # 确保父目录存在
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(fp), "size": len(content.encode("utf-8"))}


@app.delete("/api/fs/delete")
def fs_delete(payload: dict):
    """删除文件（只允许删除文件，不允许删目录）。"""
    path = payload.get("path", "")
    if not path:
        from fastapi import HTTPException
        raise HTTPException(400, "path required")
    fp = _safe_resolve(path)
    if fp.is_dir():
        from fastapi import HTTPException
        raise HTTPException(400, "不允许删目录")
    fp.unlink(missing_ok=True)
    return {"ok": True}


# ---- 截图/图片展示：把工作区里保存的图片以字节流返回给 <img> 渲染 ----
_IMG_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


@app.get("/api/fs/image")
def fs_image(path: str = ""):
    """读取本地图片文件并以字节流返回（供 <img> 展示，如 browser_control 的 share 截图）。

    路径安全：复用 _safe_resolve，限定在 AppData ROOT / 进程工作区 / 临时截图目录内。
    仅允许图片扩展名，防把任意二进制当图片外泄。
    """
    if not path:
        from fastapi import HTTPException
        raise HTTPException(400, "path required")
    try:
        fp = _safe_resolve(path)
    except Exception:
        # _safe_resolve 拒绝（403）→ 放宽到画布工作区 / default_workdir：
        # 智能体常把图片生成在工作区里，缩略图走本接口加载需放行这些路径。
        p = _P(path).resolve()
        if not any(_is_under(p, r) for r in _collect_workdir_roots()):
            from fastapi import HTTPException
            raise HTTPException(403, f"path 超出允许范围：{path}")
        fp = p
    if not fp.is_file():
        from fastapi import HTTPException
        raise HTTPException(404, f"文件不存在：{path}")
    mime = _IMG_EXT_MIME.get(fp.suffix.lower(), "")
    if not mime:
        from fastapi import HTTPException
        raise HTTPException(400, f"不支持读取的图片类型：{fp.suffix}")
    try:
        raw = fp.read_bytes()
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(500, f"读取失败：{e}")
    from fastapi.responses import Response
    return Response(content=raw, media_type=mime)


# ---- 文件上传/路径直通（前端拖拽文件/文件夹到对话输入框）----
@app.post("/api/fs/upload")
async def fs_upload(
    files: list[UploadFile] = File(default_factory=list),
    absolute_paths: str | None = Form(None),       # ← 毫秒级路径直通（本地拖文件首选）
    relative_paths: str | None = Form(None),
):
    """接收 multipart/form-data，两种模式：

    【模式 A · 路径直通（毫秒级 · 推荐）】
      前端只传 absolute_paths（JSON 数组），files 为空：
        POST /api/fs/upload  absolute_paths=["F:/project/doc.docx", "F:/project/data.xlsx"]
      后端验证每个路径在 ROOT 内且是文件 → 直接返回原路径，不读任何文件内容、不复制磁盘。

    【模式 B · 二进制上传（兜底）】
      前端传 files[] + 可选 relative_paths：读取文件内容 → 写入 ROOT/uploads/ → 返回新路径。
      仅用于 ROOT 外文件或浏览器内部拖入（无文件系统路径）的场景。

    前端典型策略：先尝试模式 A（从 text/uri-list 拿路径），后端拒绝才走模式 B。
    """
    import json as _json
    results = []
    idx = 0

    # —— 模式 A：路径直通 ——
    abs_list: list[str] = []
    if absolute_paths:
        try:
            parsed = _json.loads(absolute_paths)
            if isinstance(parsed, list):
                abs_list = [str(p) for p in parsed if p]
        except Exception:
            abs_list = []
    if abs_list and not files:
        # 纯路径直通：不读二进制、不复制文件
        for p_str in abs_list:
            p_str = p_str.replace("\\", "/").strip()
            try:
                fp = _safe_resolve(p_str)
                if not fp.is_file():
                    # 路径不是文件（可能是目录）→ 跳过，让前端知道
                    results.append({
                        "name": os.path.basename(p_str.rstrip("/")),
                        "path": p_str,
                        "size": 0,
                        "content_type": "",
                        "ext": "",
                        "upload_index": idx,
                        "skipped": True,
                        "skip_reason": "not_a_file",
                    })
                else:
                    st = fp.stat()
                    results.append({
                        "name": fp.name,
                        "path": str(fp),
                        "size": st.st_size,
                        "content_type": "",
                        "ext": fp.suffix.lstrip("."),
                        "upload_index": idx,
                        "via": "path",   # ← 告诉前端这是直通路径
                    })
            except HTTPException as e:
                results.append({
                    "name": os.path.basename(p_str.rstrip("/")),
                    "path": p_str,
                    "size": 0,
                    "content_type": "",
                    "ext": "",
                    "upload_index": idx,
                    "skipped": True,
                    "skip_reason": str(e.detail) if e.detail else "outside_root",
                })
            idx += 1
        return {"ok": True, "files": results}

    # —— 模式 B：二进制上传 ——
    if not files:
        raise HTTPException(400, "未收到任何文件或路径")

    uploads_dir = ROOT / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    MAX_SIZE = 50 * 1024 * 1024  # 单文件 50 MB
    import time

    # 解析 relative_paths（前端可能传也可能不传）
    rel_paths: list[str] = []
    if relative_paths:
        try:
            parsed = _json.loads(relative_paths)
            if isinstance(parsed, list) and len(parsed) == len(files):
                rel_paths = [str(p) for p in parsed]
        except Exception:
            rel_paths = []
    if not rel_paths:
        rel_paths = [(up.filename or f"file_{i}") for i, up in enumerate(files)]

    def _sanitize_rel(p: str) -> str:
        import re
        p = p.replace("\\", "/").lstrip("./").lstrip("/")
        p = re.sub(r'[\\/:*?"<>|\s]+', '_', p)
        return (p.rstrip('_').rstrip('.') or "file")

    now_ms = int(time.time() * 1000)
    pid = os.getpid()

    for i, up in enumerate(files):
        filename = (up.filename or "upload").replace("\\", "/").split("/")[-1]
        ext = os.path.splitext(filename)[1]
        rel = _sanitize_rel(rel_paths[i]) if i < len(rel_paths) else filename
        safe_name = f"{now_ms}_{pid}_{rel}"
        dest = uploads_dir / safe_name

        content = await up.read()
        if len(content) > MAX_SIZE:
            raise HTTPException(413, f"文件过大（>50MB）：{rel}")

        try:
            dest.write_bytes(content)
        except Exception as e:
            raise HTTPException(500, f"保存失败：{e}")

        results.append({
            "name": filename,
            "relative_path": rel,
            "path": str(dest),
            "size": len(content),
            "content_type": up.content_type or "",
            "ext": ext.lstrip("."),
            "upload_index": i,
            "via": "upload",
        })

    return {"ok": True, "files": results}


# ---- 根级文件原始字节（供 FileViewer 预览不属于任何画布的文件：上传附件/外部路径等）----
@app.get("/api/fs/raw")
def fs_raw(path: str = "", v: int = 0):
    """返回文件原始字节（二进制）。允许 ROOT/uploads 等非画布路径；≤50MB；扩展名白名单。"""
    if not path:
        raise HTTPException(400, "path required")
    try:
        fp = _safe_resolve(path)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(403, f"path 不可访问：{path}")
    if not fp.is_file():
        raise HTTPException(404, f"文件不存在：{path}")
    ext = fp.suffix.lower().lstrip(".")
    from api.canvases import _RAW_EXT_MIME, _RAW_MAX_BYTES
    mime = _RAW_EXT_MIME.get(ext)
    if not mime:
        raise HTTPException(415, f"不支持预览的文件类型：.{ext}")
    if fp.stat().st_size > _RAW_MAX_BYTES:
        raise HTTPException(413, "文件过大（>50MB），无法在线预览")
    from fastapi.responses import FileResponse
    return FileResponse(fp, media_type=mime)


@app.get("/api/fs/office-pdf")
def fs_office_pdf(path: str = ""):
    """doc/docx/ppt 经 Office COM 转 PDF（带磁盘缓存），返回 PDF 文件流。
    供聊天附件 FileViewer 使用——这些文件不属于任何画布 workdir。"""
    if not path:
        raise HTTPException(400, "path required")
    try:
        fp = _safe_resolve(path)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(403, f"path 不可访问：{path}")
    if not fp.is_file():
        raise HTTPException(404, f"文件不存在：{path}")
    from core import office_preview
    if fp.suffix.lower().lstrip(".") not in office_preview.SUPPORTED_EXTS:
        raise HTTPException(415, "仅支持 doc / docx / ppt 转换")
    r = office_preview.convert_to_pdf(str(fp))
    if "error" in r:
        raise HTTPException(400, r["error"])
    from fastapi.responses import FileResponse
    return FileResponse(r["pdf"], media_type="application/pdf")


@app.post("/api/fs/run")
def fs_run(path: str = ""):
    """用系统默认程序打开指定文件（Windows 下 os.startfile；其他平台用命令）。"""
    if not path:
        raise HTTPException(400, "path required")
    try:
        fp = _safe_resolve(path)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(403, f"path 不可访问：{path}")
    if not fp.exists():
        raise HTTPException(404, f"文件不存在：{path}")
    import platform, subprocess
    try:
        if os.name == "nt":
            os.startfile(str(fp))  # type: ignore[attr-defined]
        elif platform.system().lower() == "darwin":
            subprocess.Popen(["open", str(fp)])
        else:
            subprocess.Popen(["xdg-open", str(fp)])
    except Exception as e:
        raise HTTPException(500, f"打开失败：{e}")
    return {"ok": True, "path": str(fp)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=int(os.getenv("PORT", "8870")), reload=False)
