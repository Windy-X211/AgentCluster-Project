"""智能体集群 - FastAPI 入口"""
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from core.store import init_store, load, upsert, get_store_paths, open_folder, pick_folder, set_root, ROOT
from api import agents, canvases, conversations, models, settings, commands, rules, param_presets

from datetime import datetime


ORCHESTRATOR_NAME = "集群编排师"

ORCHESTRATOR_SYSTEM_PROMPT = """你是「集群编排师」，一位资深的 AI Agent 架构师。用户用自然语言描述他们想要的多智能体集群，你负责通过 function calling 直接在系统里创建、修改、连接这些智能体和画布。

## 你的工作方式

1. **先理解再动手**：用户说「帮我搭一个产品研发集群」，你先把这个角色团队想清楚，列出需要哪些智能体、各自职责、协作顺序。
2. **一次一个工具调用**：按顺序调用 cluster.* 工具。通常的工作流是：
   - 先 `cluster.list_canvases` 看有没有同名画布（避免重复）
   - `cluster.create_canvas` 新建画布
   - 对每个角色：`cluster.list_agents` 看是否已有 → 没有则 `cluster.create_agent` 创建（一定要写好 system_prompt，定义清楚角色的口吻、原则、边界）
   - `cluster.add_node` 把智能体加到画布上
   - `cluster.connect` 把节点按协作顺序连起来（A→B 表示 B 依赖 A 的产出）
3. **修改也直接来**：用户说「把 XX 智能体的提示词改一下」「加一个 XXX 角色」「让 A 直接汇报给 C」，你直接调 cluster.update_agent / add_node / connect。
4. **操作完告知用户**：给一份简洁的总结，告诉用户你做了什么、当前画布状态是什么，附一段后续可聊的话，比如「要不要让 XX 角色直接写一份 PRD 草稿？」。

## 设计原则

- **每个智能体的 system_prompt 必须明确**：角色、背景、核心职责、输出风格、边界（明确不做什么）。
- **画布就是依赖图**：连线方向 A→B = A 的产出喂给 B。启动时画布上所有节点会协作完成任务。
- **不要过度设计**：从 2-4 个角色起步，用户可以随时让你扩。
- **中文对话为主**，用户说什么语言就用什么语言。

## 可调用工具（cluster.* 系列）
你默认拥有全部 cluster.* 工具权限。列表详见 tools。

现在，等用户描述他们的集群愿景。"""


ORCHESTRATOR_RULE_CMDS = [
    "cluster.list_rules", "cluster.create_rule", "cluster.update_rule", "cluster.delete_rule",
]


def _ensure_orchestrator():
    """启动时如果没有「集群编排师」agent，就自动创建一个；已有则补齐缺失的 cluster.* 命令"""
    agents = load("agents")
    for a in agents:
        if a.get("name") == ORCHESTRATOR_NAME and any(
            str(c).startswith("cluster.") for c in (a.get("commands") or [])
        ):
            # 已存在 → 确保规章制度工具可用
            cmds = a.get("commands") or []
            missing = [c for c in ORCHESTRATOR_RULE_CMDS if c not in cmds]
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
            "cluster.list_agents", "cluster.create_agent", "cluster.update_agent", "cluster.delete_agent",
            "cluster.list_canvases", "cluster.create_canvas", "cluster.delete_canvas", "cluster.get_canvas",
            "cluster.add_node", "cluster.remove_node", "cluster.connect", "cluster.disconnect",
            # 规章制度（按画布隔离）
            *ORCHESTRATOR_RULE_CMDS,
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


app = FastAPI(title="AgentCluster", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ROUTES = [
    ("agents", agents.router),
    ("canvases", canvases.router),
    ("conversations", conversations.router),
    ("model_interfaces", models.router),
    ("settings", settings.router),
    ("commands", commands.router),
    ("rules", rules.router),
    ("param_presets", param_presets.router),
]
for name, r in ROUTES:
    app.include_router(r, prefix=f"/api/{name}", tags=[name])


@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.2.0"}


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
    """弹出系统文件夹选择器，返回所选路径（取消返回空串）。"""
    initial = payload.get("initial", "") or ""
    path = pick_folder(initial)
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
    fp = _safe_resolve(path)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=int(os.getenv("PORT", "8870")), reload=False)
