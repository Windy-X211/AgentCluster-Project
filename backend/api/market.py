"""市场（Market）API —— 统一管理智能体 / 画布 / 背景 / 技能 / 插件 / 功能拓展资源。

市场资源独立托管在远程仓库（默认 ``AgentCluster-Market``），本地 ``market/``
是它的 git 工作副本；主源码仓库不再跟踪该目录（见 .gitignore）。
用户数据根 ``store.ROOT`` 在项目外，三者职责严格分离：

  market/                          ← 独立仓库工作副本（下载安装 / 发布上传）
    index.json                     ← 统一索引（所有资源的元数据）
    agents/<slug>/agent.json
    canvases/<slug>/canvas.json
    backgrounds/<id>/manifest.json + 资源文件
    skills/<id>/SKILL.md + 辅助文件
    plugins/<id>/manifest.yaml + 代码
    extensions/<id>/manifest.json + 代码

接口：
  GET  /api/market/items?kind=&q=        市场列表（kind 为空=全部；q 在当前范围内搜索）
  GET  /api/market/kinds                 分类及数量
  GET  /api/market/publishable?kind=     可发布的本机资源列表
  POST /api/market/install               从市场安装 {kind, id}
  POST /api/market/publish               把本机资源发布到市场 {kind, id, push?}
  GET  /api/market/config                远程仓库与镜像配置 + 本地 git 状态
  PUT  /api/market/config                保存仓库地址 / 镜像模板 / 分支
  POST /api/market/sync                  从远程同步市场（多镜像回退）
  POST /api/market/push                  提交并推送本地市场改动
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

import core.store as store
from core import github_auth, market_sync

router = APIRouter()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MARKET_ROOT = _PROJECT_ROOT.parent / "market"
INDEX_FILE = MARKET_ROOT / "index.json"

KIND_ORDER = [
    ("all", "推荐"),
    ("agent", "智能体"),
    ("canvas", "画布"),
    ("background", "背景"),
    ("skill", "技能"),
    ("plugin", "插件"),
    ("extension", "功能拓展"),
]
KIND_LABEL = dict(KIND_ORDER)

# 复制资源目录时排除的运行时垃圾
_EXCLUDE_DIRS = {"__pycache__", "_webengine_profile", "node_modules", ".git", ".pytest_cache", ".runtime"}
_EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".bak", ".tmp"}
_EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}

# 保护 index.json 读写 + 基于 index.json 的 git add/commit 操作
# 任何时刻只允许一个操作修改或依赖 index.json，避免 sync/publish/push 互相覆盖
_index_lock = threading.Lock()

# 安装（写 agents/agent_groups/canvases 记录）串行化：
# 保证一批资源分配到的 id 互不冲突，也不与并发安装撞号
_install_lock = threading.Lock()


# ==============================================================
# 索引读写
# ==============================================================
_index_cache: dict[str, Any] = {"mtime": -1.0, "data": None}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_index(force: bool = False) -> dict:
    """读取 market/index.json（按 mtime 缓存）。不存在时返回空索引。"""
    if not INDEX_FILE.exists():
        return {"version": "1.0", "updated_at": _now(), "items": []}
    try:
        mtime = INDEX_FILE.stat().st_mtime
    except OSError:
        mtime = -1.0
    if not force and _index_cache["data"] is not None and _index_cache["mtime"] == mtime:
        return _index_cache["data"]
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[market] 读取 index.json 失败：{e}")
        data = {"version": "1.0", "updated_at": _now(), "items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        data = {"version": "1.0", "updated_at": _now(), "items": []}
    _index_cache["data"] = data
    _index_cache["mtime"] = mtime
    return data


def _write_index(data: dict) -> None:
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now()
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, INDEX_FILE)
    _index_cache["data"] = data
    try:
        _index_cache["mtime"] = INDEX_FILE.stat().st_mtime
    except OSError:
        _index_cache["mtime"] = -1.0


def _find_item(kind: str, item_id: str) -> dict | None:
    for it in _load_index().get("items", []):
        if it.get("kind") == kind and it.get("id") == item_id:
            return it
    return None


def _item_dir(item: dict) -> Path:
    rel = item.get("dir") or ""
    return MARKET_ROOT / rel


def _safe_id(value: str) -> str:
    """资源 id 仅允许英文/数字/下划线/短横线，防路径穿越（市场 id 均为 slug）。"""
    if not value or not all(c.isalnum() or c in ("_", "-") for c in value):
        raise HTTPException(400, f"非法资源 id：{value}")
    return value


def _slugify(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return s or fallback


def _unique_name(base: str, taken: set[str]) -> str:
    """生成一个不与 ``taken`` 冲突的名称：重名时追加序号。

    - 普通名称：游戏团队 → 游戏团队2 → 游戏团队3（跳过已占用的序号）
    - 名称本身带数字后缀时按尾部数字递增：游戏团队2 → 游戏团队3，
      避免出现「游戏团队22」这种别扭的名字
    """
    name = (base or "").strip() or "未命名"
    if name not in taken:
        return name
    m = re.match(r"^(.*?)(\d+)$", name)
    if m:
        stem, num = m.group(1), int(m.group(2))
        if stem:
            i = num + 1
            while f"{stem}{i}" in taken:
                i += 1
            return f"{stem}{i}"
        # 纯数字名：直接递增（2024 → 2025）
        i = num + 1
        while str(i) in taken:
            i += 1
        return str(i)
    i = 2
    while f"{name}{i}" in taken:
        i += 1
    return f"{name}{i}"


def _alloc_local_id(domain: str) -> int:
    """为一个记录域分配**未被占用**的新 id。

    ``store.next_id`` 返回「文件目录里最大 id + 1」，正常情况下不会撞车；
    这里再复查一次，保证从市场安装的资源绝不会覆盖本机已有记录
    （同名智能体不改名，但 id 必须是全新的）。
    """
    for _ in range(1000):
        rid = store.next_id(domain)
        if store.get_by(domain, rid) is None:
            return rid
    raise HTTPException(500, f"无法为 {domain} 分配新 id")


def _copy_dir(src: Path, dest: Path, overwrite: bool = False) -> None:
    if dest.exists():
        if not overwrite:
            raise HTTPException(409, f"已存在：{dest.name}")
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _ignore(_d: str, names: list[str]):
        out = set()
        for n in names:
            p = Path(_d) / n
            if n in _EXCLUDE_DIRS or n in _EXCLUDE_FILES:
                out.add(n)
            elif p.is_file() and p.suffix.lower() in _EXCLUDE_SUFFIX:
                out.add(n)
        return out

    shutil.copytree(src, dest, ignore=_ignore)


# ==============================================================
# 本地安装状态
# ==============================================================
def _local_extension_ids() -> set[str]:
    f = store.ROOT / "extensions" / "installed.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return set(data.keys()) if isinstance(data, dict) else set()
    except Exception:
        return set()


def _installed_state(items: list[dict]) -> None:
    ext_ids = _local_extension_ids()
    try:
        agents = store.load("agents")
    except Exception:
        agents = []
    try:
        canvases = store.load("canvases")
    except Exception:
        canvases = []
    agent_names = {a.get("name") for a in agents}
    # 画布安装后会加序号改名（游戏团队 → 游戏团队2），名字对不上，
    # 因此用安装时写入的 market_id 做权威判定；名字匹配仅作旧数据的兜底。
    canvas_market_ids = {str(c.get("market_id")) for c in canvases if c.get("market_id")}
    canvas_names = {c.get("name") for c in canvases}
    agent_market_ids = {str(a.get("market_id")) for a in agents if a.get("market_id")}
    for it in items:
        kind, iid = it.get("kind"), it.get("id", "")
        if kind == "skill":
            it["installed"] = (store.ROOT / "skills" / iid).exists()
        elif kind == "plugin":
            it["installed"] = (store.ROOT / "plugins" / iid).exists()
        elif kind == "background":
            it["installed"] = (store.ROOT / "backgrounds" / iid).exists()
        elif kind == "extension":
            it["installed"] = iid in ext_ids
            it["enabled"] = it["installed"] and bool(
                _read_installed().get(iid, {}).get("enabled")
            )
        elif kind == "agent":
            # 智能体安装后不改名（重名也不改名），名字匹配仍然有效
            it["installed"] = (str(iid) in agent_market_ids) or (it.get("name") in agent_names)
        elif kind == "canvas":
            it["installed"] = (str(iid) in canvas_market_ids) or (it.get("name") in canvas_names)
        else:
            it["installed"] = False


def _read_installed() -> dict:
    f = store.ROOT / "extensions" / "installed.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_installed(data: dict) -> None:
    f = store.ROOT / "extensions" / "installed.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)


# ==============================================================
# 列表 / 搜索
# ==============================================================
@router.get("/kinds")
def list_kinds():
    items = _load_index().get("items", [])
    counts = {k: 0 for k, _ in KIND_ORDER}
    counts["all"] = len(items)
    for it in items:
        k = it.get("kind")
        if k in counts:
            counts[k] += 1
    return {
        "kinds": [{"key": k, "label": label, "count": counts.get(k, 0)} for k, label in KIND_ORDER],
        "updated_at": _load_index().get("updated_at"),
        "needs_sync": not market_sync.market_ready(),
    }


@router.get("/items")
def list_items(kind: str = "", q: str = ""):
    """市场资源列表。kind 为空或 all 时返回全部；q 在当前范围内搜索。"""
    items = list(_load_index().get("items", []))
    kind = (kind or "").strip()
    if kind and kind != "all":
        items = [it for it in items if it.get("kind") == kind]

    keyword = (q or "").strip().lower()
    if keyword:
        def _hit(it: dict) -> bool:
            hay = " ".join([
                str(it.get("name", "")), str(it.get("description", "")),
                str(it.get("id", "")), " ".join(str(t) for t in (it.get("tags") or [])),
            ]).lower()
            return keyword in hay
        items = [it for it in items if _hit(it)]

    # 推荐/列表排序：最近更新在前
    items.sort(key=lambda it: (it.get("updated_at") or ""), reverse=True)
    _installed_state(items)
    return {
        "kind": kind or "all",
        "total": len(items),
        "items": items,
        "updated_at": _load_index().get("updated_at"),
        "needs_sync": not market_sync.market_ready(),
    }


# ==============================================================
# 资源预览
# ==============================================================
@router.get("/item/{kind}/{item_id}")
def preview_item(kind: str, item_id: str):
    """获取单个市场资源的详情预览：按需拉取资源目录（如未缓存），再解析。"""
    item = _find_item(kind, item_id)
    if not item:
        raise HTTPException(404, f"市场中未找到资源：{kind}/{item_id}")

    # 按需拉取（如果还没缓存）
    try:
        folder = market_sync.ensure_item_local(kind, item_id)
    except Exception as e:
        raise HTTPException(502, f"拉取资源失败：{e}")
    if not folder.exists():
        raise HTTPException(404, f"资源目录不存在：{folder}")

    # 文件列表（递归，排除大文件和二进制）
    files: list[dict] = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(folder).as_posix()
        size = p.stat().st_size
        files.append({"path": rel, "size": size})

    # 按 kind 解析预览内容
    preview = _parse_preview(item, folder)
    return {"item": item, "files": files, "preview": preview}


def _parse_preview(item: dict, folder: Path) -> dict:
    """根据资源类型解析关键文件，返回结构化预览数据。"""
    kind = item.get("kind")
    out: dict = {"kind": kind}
    if kind == "agent":
        out["agent"] = _parse_agent(folder / (item.get("file") or "agent.json"))
    elif kind == "canvas":
        out["canvas"] = _parse_canvas(folder / (item.get("file") or "canvas.json"))
    elif kind == "background":
        out["background"] = _parse_background(folder / "manifest.json", folder)
    elif kind == "skill":
        out["skill"] = _parse_skill(folder / "SKILL.md")
    elif kind == "plugin":
        out["plugin"] = _parse_plugin(folder / "manifest.yaml", folder)
    elif kind == "extension":
        out["extension"] = _parse_extension(folder / "manifest.json", folder)
    return out


def _safe_read_text(path: Path, max_chars: int = 200_000) -> str:
    try:
        txt = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    if len(txt) > max_chars:
        txt = txt[:max_chars] + "\n…（文件过大，已截断）"
    return txt


def _parse_agent(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(_safe_read_text(path))
    except Exception:
        return {"error": "无法解析 agent.json"}
    return {
        "system_prompt": raw.get("system_prompt", ""),
        "model": raw.get("model", ""),
        "commands": raw.get("commands", []) or [],
        "tools": raw.get("tools", []) or [],
        "skills": raw.get("skills", []) or [],
        "interface_id": raw.get("interface_id"),
        "trigger": raw.get("trigger", {}),
        "max_tool_rounds": raw.get("max_tool_rounds"),
        "workflow_mode": raw.get("workflow_mode", ""),
        "perception_scope": raw.get("perception_scope", ""),
        "reasoning_level": raw.get("reasoning_level", ""),
        "raw_keys": sorted(raw.keys()),
    }


def _parse_canvas(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(_safe_read_text(path))
    except Exception:
        return {"error": "无法解析 canvas.json"}
    agents = raw.get("agents") or []
    nodes = raw.get("nodes") or []
    edges = raw.get("edges") or []
    agent_groups = raw.get("agent_groups") or []
    return {
        "execution_mode": raw.get("execution_mode", "serial"),
        "edge_shape": raw.get("edge_shape"),
        "agent_count": len(agents),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "agent_group_count": len(agent_groups),
        "agents": [
            {
                "name": a.get("name", ""),
                "description": a.get("description", ""),
                "model": a.get("model", ""),
                "group_name": a.get("group_name", ""),
                "commands": (a.get("commands") or [])[:8],
                "tool_count": len(a.get("tools") or []),
            }
            for a in agents
        ],
        "nodes": [
            {
                "id": n.get("id"),
                "agent_ref": n.get("agent_ref", ""),
                "x": n.get("x", 0), "y": n.get("y", 0),
            }
            for n in nodes
        ],
        "edges": [
            {
                "from_node": e.get("from_node") or e.get("source_node"),
                "to_node": e.get("to_node") or e.get("target_node"),
                "type": e.get("type", ""),
            }
            for e in edges
        ],
        "nodes_raw": nodes,
        "edges_raw": edges,
    }


def _parse_background(manifest_path: Path, folder: Path) -> dict:
    out: dict = {}
    if manifest_path.exists():
        try:
            mf = json.loads(_safe_read_text(manifest_path))
            out.update(mf)
        except Exception:
            pass
    # 如果有 background.html，尝试内联引用
    entry = out.get("entry") or "background.html"
    entry_path = folder / entry
    if entry_path.exists():
        content = _safe_read_text(entry_path, max_chars=50_000)
        # 只保留 <body> 或 <style> 部分
        out["entry_file"] = entry
        out["entry_content"] = content
        out["entry_size"] = entry_path.stat().st_size
    return out


def _parse_skill(skill_md_path: Path) -> dict:
    content = _safe_read_text(skill_md_path, max_chars=80_000)
    fm: dict = {}
    body = content
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end > 0:
            fm_text = content[3:end]
            body = content[end + 4:]
            try:
                import yaml
                fm = yaml.safe_load(fm_text) or {}
            except Exception:
                pass
    return {
        "frontmatter": fm,
        "body": body.strip(),
        "total_chars": len(content),
    }


def _parse_plugin(manifest_path: Path, folder: Path) -> dict:
    meta: dict = {}
    if manifest_path.exists():
        meta = _yaml_load(manifest_path) or {}
    # 读取 tool.yaml / tool.py 作为预览
    tool_yaml = folder / "tool.yaml"
    tool_info: dict = {}
    if tool_yaml.exists():
        try:
            import yaml as _yaml
            tool_info = _yaml.safe_load(_safe_read_text(tool_yaml, max_chars=30_000)) or {}
        except Exception:
            pass
    tool_py = folder / "tool.py"
    tool_source = ""
    if tool_py.exists():
        tool_source = _safe_read_text(tool_py, max_chars=15_000)
    return {
        "meta": meta,
        "tool_info": tool_info,
        "tool_source": tool_source,
    }


def _parse_extension(manifest_path: Path, folder: Path) -> dict:
    meta: dict = {}
    if manifest_path.exists():
        try:
            meta = json.loads(_safe_read_text(manifest_path))
        except Exception:
            pass
    # 读 README 作为说明
    readme_content = ""
    for name in ("README.md", "README", "readme.md"):
        p = folder / name
        if p.exists():
            readme_content = _safe_read_text(p, max_chars=50_000)
            break
    # backend.py 作为后端代码预览
    backend_content = ""
    bp = folder / "backend.py"
    if bp.exists():
        backend_content = _safe_read_text(bp, max_chars=15_000)
    return {
        "meta": meta,
        "readme": readme_content,
        "backend_code": backend_content,
    }


# ==============================================================
# 远程仓库配置 / 同步 / 推送
# ==============================================================
@router.get("/config")
def get_config():
    return {
        **market_sync.get_config(),
        "state": market_sync.repo_state(),
        "market_root": str(MARKET_ROOT),
        "market_ready": market_sync.market_ready(),
    }


@router.put("/config")
def update_config(payload: dict):
    try:
        return {"ok": True, "config": market_sync.save_config(payload or {})}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/sync")
def sync(payload: dict | None = None):
    """从远程市场仓库同步（多镜像自动回退）。阻塞调用，前端需加载态。"""
    payload = payload or {}
    force = bool(payload.get("force"))
    if not _index_lock.acquire(blocking=False):
        raise HTTPException(409, "已有同步或发布任务进行中，请稍候")
    try:
        return market_sync.sync_market(force=force)
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _index_lock.release()


# ==============================================================
# GitHub 账号（上传身份）
# ==============================================================
@router.get("/auth")
def get_auth():
    """当前登录状态。只返回用户名/头像/权限，不回传 token。"""
    data = github_auth.public_auth()
    if data.get("logged_in"):
        data["repo_permission"] = github_auth.check_push_permission(
            market_sync.get_config()["repo_url"]
        )
    return data


@router.post("/auth")
def login(payload: dict):
    """用「用户名 + Personal Access Token」登录，Token 只存本机。"""
    payload = payload or {}
    username = str(payload.get("username") or "")
    token = str(payload.get("token") or "")
    try:
        return github_auth.save_auth(username, token,
                                     market_sync.get_config()["repo_url"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, str(e))


@router.delete("/auth")
def logout():
    github_auth.clear_auth()
    return {"ok": True, "logged_in": False}


@router.post("/push")
def push(payload: dict | None = None):
    """提交并推送本地 market/ 改动到远程仓库（官方地址，镜像不支持 push）。"""
    payload = payload or {}
    message = str(payload.get("message") or "").strip() or None
    if not github_auth.get_token():
        raise HTTPException(401, "请先登录 GitHub 账号后再推送")
    if not _index_lock.acquire(blocking=False):
        raise HTTPException(409, "已有同步或发布任务进行中，请稍候")
    try:
        return market_sync.push_market(message)
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _index_lock.release()


# ==============================================================
# 安装
# ==============================================================
@router.post("/install")
def install(payload: dict):
    kind = (payload.get("kind") or "").strip()
    item_id = _safe_id((payload.get("id") or "").strip())
    requested_version = (payload.get("version") or "").strip()  # 可选：装指定版本
    item = _find_item(kind, item_id)
    if not item:
        raise HTTPException(404, f"市场中未找到资源：{kind}/{item_id}")

    # 如果指定了旧版本，从 versions 数组里找
    if requested_version and requested_version != item.get("version"):
        target = next(
            (v for v in (item.get("versions") or [])
             if v.get("version") == requested_version), None)
        if not target:
            raise HTTPException(404, f"该资源不存在版本 {requested_version}")
        item = {**target, "dir": target.get("dir") or _versioned_dir(item.get("dir", ""), requested_version)}

    # 按需拉取（如未缓存）
    try:
        market_sync.ensure_item_local(kind, item_id, requested_version=requested_version)
    except Exception as e:
        raise HTTPException(502, f"拉取资源失败：{e}")

    if kind == "agent":
        return _install_agent(item)
    if kind == "canvas":
        return _install_canvas(item)
    if kind == "extension":
        return _install_extension(item)
    if kind in ("background", "skill", "plugin"):
        return _install_directory(item)
    raise HTTPException(400, f"不支持的资源类型：{kind}")


def _versioned_dir(dir_path: str, version: str) -> str:
    """把 ``agents/ppt`` 变成 ``agents/ppt@1.0.0``（旧版备份目录规则）。"""
    if "@" in dir_path.split("/")[-1]:
        return dir_path  # 已是 @version 形式
    if not version:
        return dir_path
    parent, name = dir_path.rsplit("/", 1) if "/" in dir_path else ("", dir_path)
    new_name = f"{name}@{version}"
    return f"{parent}/{new_name}" if parent else new_name


def _install_agent(item: dict) -> dict:
    src = _item_dir(item) / (item.get("file") or "agent.json")
    if not src.exists():
        raise HTTPException(500, f"市场资源文件缺失：{src}")
    raw = json.loads(src.read_text(encoding="utf-8"))
    with _install_lock:
        rec = {k: v for k, v in raw.items()
               if k not in ("id", "created_at", "sort_order", "group_id", "market_id")}
        # 名字保持市场原样（重名也不改名），但 id 必须是全新的，不能与本机已有智能体撞号
        rec["id"] = _alloc_local_id("agents")
        rec["created_at"] = datetime.utcnow().isoformat()
        rec.setdefault("enabled", True)
        rec["market_id"] = item.get("id")
        store.upsert("agents", rec)
    return {"ok": True, "kind": "agent", "id": rec["id"], "name": rec.get("name")}


def _canvas_agent_snapshots(item: dict, raw: dict) -> list[tuple[str, dict]]:
    """读取画布包里的智能体快照，返回 [(唯一键, 快照), ...]。

    新版格式：画布目录下 agents/<slug>/agent.json；
    旧版格式（向后兼容）：智能体快照打包在 canvas.json.agents 里。

    唯一键用于在安装时精确还原节点引用——画布内出现**同名智能体**时，
    仅靠名字无法区分，必须靠这个键（发布侧写入 agent_key = 唯一目录名）。
    """
    agents_dir = _item_dir(item) / "agents"
    snaps: list[tuple[str, dict]] = []
    if agents_dir.is_dir():
        for a_file in sorted(agents_dir.rglob("agent.json")):
            try:
                snap = json.loads(a_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(snap, dict):
                continue
            key = str(snap.get("agent_key") or "").strip() or a_file.parent.name
            snaps.append((key, snap))
    if snaps:
        return snaps
    for idx, snap in enumerate(raw.get("agents") or []):
        if isinstance(snap, dict):
            key = str(snap.get("agent_key") or "").strip() or f"idx-{idx}"
            snaps.append((key, snap))
    return snaps


def _canvas_group_snapshots(item: dict, raw: dict) -> list[dict]:
    """读取画布包里的分组快照（canvas.json 内嵌优先，其次 agent_groups.json）。"""
    groups = [g for g in (raw.get("agent_groups") or []) if isinstance(g, dict)]
    if groups:
        return groups
    gf = _item_dir(item) / "agent_groups.json"
    if gf.exists():
        try:
            data = json.loads(gf.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [g for g in data if isinstance(g, dict)]
        except Exception:
            pass
    return []


def _install_canvas(item: dict) -> dict:
    src = _item_dir(item) / (item.get("file") or "canvas.json")
    if not src.exists():
        raise HTTPException(500, f"市场资源文件缺失：{src}")
    raw = json.loads(src.read_text(encoding="utf-8"))

    with _install_lock:
        # —— 第一步：安装智能体分组 ——
        # 与本机重名的分组**加序号新建**（游戏 → 游戏2），不复用本地同名分组，
        # 这样下载来的智能体归到自己的组里，不会和本地已有分组混在一起。
        local_group_names = {str(g.get("name") or "") for g in store.load("agent_groups")}
        group_by_name: dict[str, int] = {}    # 市场分组名 → 本地 group_id
        renamed_groups: dict[str, str] = {}   # 市场分组名 → 本地最终名
        created_groups: list[str] = []
        for gsnap in _canvas_group_snapshots(item, raw):
            gname = str(gsnap.get("name") or "").strip()
            if not gname:
                continue
            final_name = _unique_name(gname, local_group_names)
            local_group_names.add(final_name)
            grec = {
                "name": final_name,
                "description": gsnap.get("description", ""),
                "id": _alloc_local_id("agent_groups"),
                "created_at": datetime.utcnow().isoformat(),
            }
            store.upsert("agent_groups", grec)
            group_by_name.setdefault(gname, grec["id"])
            renamed_groups.setdefault(gname, final_name)
            created_groups.append(final_name)

        # —— 第二步：安装智能体 ——
        # 核心原则：**不依赖名称匹配本地任何已有智能体**。画布中每个智能体快照
        # 一律新建（分配全新本地 id），节点里的 agent_ref 字符串翻译到新 id。
        # 这样用户下载的画布永远拿到市场上那一套，不会意外绑到自己旧的同名智能体。
        # 重名智能体**不改名**（名字保持市场原样），只保证 id 互不冲突。
        key_to_id: dict[str, int] = {}      # agent_key → 新分配的本地 agent_id
        name_to_ids: dict[str, list[int]] = {}  # agent_ref(name) → 新 id 队列（同名时按序取用）
        created: list[str] = []
        for key, snap in _canvas_agent_snapshots(item, raw):
            name = str(snap.get("name") or "").strip()
            if not name:
                continue
            # 快照里 group_id 已被发布侧剥离；有 group_name 时映射到本地 group_id
            new_group_id: int | None = None
            gname = str(snap.get("group_name") or "").strip()
            if gname:
                new_group_id = group_by_name.get(gname)   # 可能是 None（组不存在时）
            rec = {k: v for k, v in snap.items()
                   if k not in ("id", "created_at", "sort_order", "group_id",
                                "group_name", "agent_key", "market_id")}
            rec["id"] = _alloc_local_id("agents")
            rec["created_at"] = datetime.utcnow().isoformat()
            rec["group_id"] = new_group_id
            rec.setdefault("enabled", True)
            rec["market_canvas_id"] = item.get("id")
            store.upsert("agents", rec)
            key_to_id[key] = rec["id"]
            name_to_ids.setdefault(name, []).append(rec["id"])
            created.append(name)

        # —— 第三步：构建画布节点，把市场里的 agent_ref 翻译成新分配的本地 id ——
        # 优先按 agent_key 精确匹配（能区分画布内同名智能体）；
        # 旧包没有 key 时按名字兜底：同名智能体按出现顺序依次取用，
        # 避免所有同名节点都绑到同一个智能体上。
        nodes = []
        skipped = 0
        name_cursor: dict[str, int] = {}
        for n in raw.get("nodes") or []:
            nkey = str(n.get("agent_key") or "").strip()
            ref = n.get("agent_ref")
            aid = key_to_id.get(nkey) if nkey else None
            if aid is None and ref:
                queue = name_to_ids.get(ref) or []
                if queue:
                    idx = name_cursor.get(ref, 0)
                    aid = queue[idx] if idx < len(queue) else queue[-1]
                    name_cursor[ref] = idx + 1
            if aid is None:
                skipped += 1
                continue
            nodes.append({"id": n.get("id"), "agent_id": aid,
                          "x": n.get("x", 0), "y": n.get("y", 0)})

        # —— 第四步：创建画布（与本机重名则加序号：游戏团队 → 游戏团队2）——
        local_canvas_names = {str(c.get("name") or "") for c in store.load("canvases")}
        base_name = str(raw.get("name") or item.get("name") or "市场画布").strip() or "市场画布"
        canvas_name = _unique_name(base_name, local_canvas_names)

        rec = {
            "id": _alloc_local_id("canvases"),
            "name": canvas_name,
            "description": raw.get("description", ""),
            "nodes": nodes,
            "edges": raw.get("edges") or [],
            "created_at": datetime.utcnow().isoformat(),
            "workdir": None,
            "execution_mode": raw.get("execution_mode", "serial"),
            "ws_pin": None,
            "market_id": item.get("id"),
        }
        store.upsert("canvases", rec)

    return {
        "ok": True, "kind": "canvas", "id": rec["id"], "name": rec["name"],
        "created_agents": created,
        "skipped_nodes": skipped,
        "created_agent_groups": created_groups,
        "renamed_agent_groups": renamed_groups,
    }


def _install_directory(item: dict) -> dict:
    kind, item_id = item["kind"], item["id"]
    src = _item_dir(item)
    if not src.exists():
        raise HTTPException(500, f"市场资源目录缺失：{src}")
    dest = store.ROOT / {"background": "backgrounds", "skill": "skills", "plugin": "plugins"}[kind] / item_id
    _copy_dir(src, dest)
    if kind in ("skill", "plugin"):
        try:
            if kind == "skill":
                from skills import load_skills
                load_skills(blocking=False)
            else:
                from plugins import load_plugins
                load_plugins(blocking=False)
        except Exception as e:
            print(f"[market] 技能/插件热重载失败（下次启动生效）：{e}")
    return {"ok": True, "kind": kind, "id": item_id, "name": item.get("name")}


def _install_extension(item: dict) -> dict:
    item_id = _safe_id(item["id"])
    src = _item_dir(item)
    if not src.exists():
        raise HTTPException(500, f"市场资源目录缺失：{src}")
    dest = store.ROOT / "extensions" / item_id
    _copy_dir(src, dest, overwrite=True)
    installed = _read_installed()
    installed[item_id] = {
        "enabled": True,
        "installed_at": datetime.utcnow().isoformat(),
        "version": item.get("version", "0.0.0"),
        "source": "builtin",
    }
    _write_installed(installed)
    return {"ok": True, "kind": "extension", "id": item_id, "name": item.get("name"),
            "need_restart": bool(item.get("backend_entry"))}


# ==============================================================
# "我的发布"筛选 + 下架（删除 + 可选远程推送）
# ==============================================================
@router.get("/my-items")
def my_items(q: str = ""):
    """返回「我的发布」条目。

    主口径是 author == 当前登录 GitHub 用户名；额外纳入「未登录时上传
    （author=AgentCluster）且尚未推送」的条目——否则用户之后登录，
    这些刚上传、还没推送的资源会从「我的发布」里凭空消失。
    未登录时返回空（前端引导登录）。
    """
    login = github_auth.get_login()
    if not login:
        return {"kind": "all", "total": 0, "items": [], "updated_at": _load_index().get("updated_at")}

    all_items = list(_load_index().get("items", []))
    push_state = market_sync.pending_push_paths()
    # 拷贝副本：_installed_state/_mark_push_state 会往条目里写临时字段，
    # 直接改索引缓存对象会把 installed/pushed 持久化进 index.json
    items = [dict(it) for it in all_items if (it.get("author") or "") == login]
    if push_state.get("known") and not push_state.get("all_pending"):
        pending = push_state.get("paths") or []
        for it in all_items:
            if (it.get("author") or "") != "AgentCluster":
                continue
            if _path_pending(_item_rel_dir(it), pending):
                items.append(dict(it))

    keyword = (q or "").strip().lower()
    if keyword:
        def _hit(it: dict) -> bool:
            hay = " ".join([
                str(it.get("name", "")), str(it.get("description", "")),
                str(it.get("id", "")), " ".join(str(t) for t in (it.get("tags") or [])),
            ]).lower()
            return keyword in hay
        items = [it for it in items if _hit(it)]

    items.sort(key=lambda it: (it.get("updated_at") or ""), reverse=True)
    _installed_state(items)
    _mark_push_state(items, push_state)
    return {
        "kind": "all",
        "total": len(items),
        "items": items,
        "updated_at": _load_index().get("updated_at"),
        "mine": True,
    }


def _item_rel_dir(item: dict) -> str:
    """条目在 market/ 下的相对目录（如 agents/my-agent），取不到时返回空串。"""
    rel = str(item.get("dir") or "").strip("/")
    if rel:
        return rel
    try:
        return _item_dir(item).relative_to(MARKET_ROOT).as_posix()
    except Exception:
        return ""


def _path_pending(rel: str, pending: list[str]) -> bool:
    if not rel:
        return False
    return any(p == rel or p.startswith(rel + "/") for p in pending)


def _mark_push_state(items: list[dict], state: dict | None = None) -> None:
    """给「我的发布」条目补充 pushed 字段（False=已发布到本地市场但未推送到远程）。

    pushed=None 表示无法判断（非 git 仓库 / 未安装 git），前端不展示标记。
    判定依据是 market/ 仓库的真实 git 状态：条目目录下有未提交改动，
    或本地提交尚未出现在 origin/<branch> 上，即视为未推送。
    """
    state = state or market_sync.pending_push_paths()
    if not state.get("known"):
        for it in items:
            it["pushed"] = None
        return
    if state.get("all_pending"):
        for it in items:
            it["pushed"] = False
        return
    pending = state.get("paths") or []
    for it in items:
        it["pushed"] = not _path_pending(_item_rel_dir(it), pending)


# ==============================================================
# 删除市场资源 / 下架
# ==============================================================
@router.delete("/items/{kind}/{item_id}")
def delete_item(kind: str, item_id: str, push: bool = False):
    """删除市场条目及其资源目录（不影响本机已安装副本）。push=True 时同时提交推送到远程仓库。"""
    if not _index_lock.acquire(blocking=False):
        raise HTTPException(409, "已有同步或发布任务进行中，请稍候")
    try:
        item_id = _safe_id(item_id)
        item = _find_item(kind, item_id)
        if not item:
            raise HTTPException(404, f"市场中未找到资源：{kind}/{item_id}")

        folder = _item_dir(item)
        try:
            resolved = folder.resolve()
            if resolved == MARKET_ROOT.resolve():
                raise HTTPException(400, "拒绝删除市场根目录")
            resolved.relative_to(MARKET_ROOT.resolve())
        except HTTPException:
            raise
        except ValueError:
            raise HTTPException(400, "非法资源路径")
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)

        data = _load_index(force=True)
        data["items"] = [it for it in data.get("items", [])
                         if not (it.get("kind") == kind and it.get("id") == item_id)]
        _write_index(data)

        result = {"ok": True, "kind": kind, "id": item_id, "name": item.get("name")}

        # 可选：推送到远程仓库（官方仓库；镜像只读不支持 push）
        if push:
            if not github_auth.get_token():
                result["push_error"] = "请先登录 GitHub 账号后再远程下架"
            else:
                try:
                    result["push"] = market_sync.push_market(f"unpublish {kind}/{item_id}")
                except Exception as e:
                    result["push_error"] = str(e)
        return result
    finally:
        _index_lock.release()


# ==============================================================
# 可发布的本机资源
# ==============================================================
@router.get("/publishable")
def publishable(kind: str = ""):
    kind = (kind or "").strip()
    if kind not in {"agent", "canvas", "background", "skill", "plugin", "extension"}:
        raise HTTPException(400, f"不支持的类型：{kind}")
    items = _publishable_items(kind)
    _mark_published(kind, items)
    return {"kind": kind, "items": items}


def _mark_published(kind: str, items: list[dict]) -> None:
    """标记本机资源是否已发布到市场。

    智能体/画布发布后市场 id 是 slug（不等于本地 id），按 item 里的
    origin_agent_id / origin_canvas_id 匹配，并追加名称校验——避免同步下载到的
    条目（其 origin id 是别人机器上的本地 id）碰巧与本机 id 相同而误标「已上传」。
    目录类（背景/技能/插件/拓展）发布 id 与本地目录名一致，直接按 id 匹配。
    """
    origin_key = {"agent": "origin_agent_id", "canvas": "origin_canvas_id"}.get(kind)
    published_ids: set[str] = set()
    published_origins: set[tuple[int, str]] = set()
    for it in _load_index().get("items", []):
        if it.get("kind") != kind:
            continue
        published_ids.add(str(it.get("id") or ""))
        if origin_key:
            try:
                published_origins.add((int(it.get(origin_key)), str(it.get("name") or "")))
            except (TypeError, ValueError):
                pass
    for item in items:
        if origin_key:
            try:
                key = (int(item.get("id")), str(item.get("name") or ""))
            except (TypeError, ValueError):
                item["published"] = False
                continue
            item["published"] = key in published_origins
        else:
            item["published"] = str(item.get("id") or "") in published_ids


def _publishable_items(kind: str) -> list[dict]:
    out: list[dict] = []
    if kind == "agent":
        for a in store.load("agents"):
            out.append({"id": str(a.get("id")), "name": a.get("name"),
                        "description": a.get("description", "")})
    elif kind == "canvas":
        for c in store.load("canvases"):
            out.append({"id": str(c.get("id")), "name": c.get("name"),
                        "description": c.get("description", "")})
    elif kind == "background":
        root = store.ROOT / "backgrounds"
        for d in sorted(root.iterdir()) if root.exists() else []:
            mf = d / "manifest.json"
            if not d.is_dir() or not mf.exists():
                continue
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({"id": d.name, "name": meta.get("name", d.name),
                        "description": meta.get("description", "")})
    elif kind == "skill":
        root = store.ROOT / "skills"
        for d in sorted(root.iterdir()) if root.exists() else []:
            if not d.is_dir() or not (d / "SKILL.md").exists():
                continue
            fm = _skill_frontmatter(d / "SKILL.md")
            name = fm.get("label") or fm.get("name") or d.name
            out.append({"id": d.name, "name": name,
                        "description": str(fm.get("description", "")).split("\n")[0][:200]})
    elif kind == "plugin":
        root = store.ROOT / "plugins"
        for d in sorted(root.iterdir()) if root.exists() else []:
            if not d.is_dir() or not (d / "manifest.yaml").exists():
                continue
            meta = _yaml_load(d / "manifest.yaml")
            out.append({"id": d.name,
                        "name": _localized(meta.get("label")) or d.name,
                        "description": _localized(meta.get("description"))})
    elif kind == "extension":
        root = store.ROOT / "extensions"
        installed = _read_installed()
        for eid in installed:
            mf = root / eid / "manifest.json"
            meta = {}
            if mf.exists():
                try:
                    meta = json.loads(mf.read_text(encoding="utf-8"))
                except Exception:
                    pass
            out.append({"id": eid, "name": meta.get("name", eid),
                        "description": meta.get("description", "")})
    return out


# ==============================================================
# 发布（本机资源 → 市场）
# ==============================================================
@router.post("/publish")
def publish(payload: dict):
    kind = (payload.get("kind") or "").strip()
    local_id = str(payload.get("id") or "").strip()
    if not local_id:
        raise HTTPException(400, "缺少资源 id")

    if not _index_lock.acquire(blocking=False):
        raise HTTPException(409, "已有同步或发布任务进行中，请稍候")
    try:
        # 版本号：显式指定优先，否则按同资源最新版本自动 +patch
        requested_version = (payload.get("version") or "").strip()
        if not requested_version:
            idx = _load_index(force=True).get("items", [])
            latest = next((it for it in idx
                           if it.get("kind") == kind and it.get("id") == local_id), None)
            # 从 versions 数组里找真正最新的
            real_latest = None
            if latest:
                real_latest = latest
                for v in (latest.get("versions") or []):
                    if _version_tuple(v.get("version", "0")) > _version_tuple(real_latest.get("version", "0")):
                        real_latest = v
            requested_version = _next_version(real_latest.get("version") if real_latest else None)

        if kind == "agent":
            result = _publish_agent(local_id, version=requested_version)
        elif kind == "canvas":
            result = _publish_canvas(local_id, version=requested_version)
        elif kind in ("background", "skill", "plugin", "extension"):
            result = _publish_directory(kind, local_id, version=requested_version)
        else:
            raise HTTPException(400, f"不支持的资源类型：{kind}")

        # 可选：发布后立即提交并推送到远程市场仓库（失败不回滚本地发布）
        if payload.get("push"):
            if not github_auth.get_token():
                result["push_error"] = "请先登录 GitHub 账号后再推送到远程仓库"
            else:
                try:
                    result["push"] = market_sync.push_market(
                        f"publish {kind}/{local_id} v{requested_version}")
                except Exception as e:
                    result["push_error"] = str(e)
        return result
    finally:
        _index_lock.release()


@router.get("/item/{kind}/{item_id}/versions")
def item_versions(kind: str, item_id: str):
    """返回某个资源的所有历史版本（含最新）。"""
    item = _find_item(kind, item_id)
    if not item:
        raise HTTPException(404, f"市场中未找到资源：{kind}/{item_id}")
    versions = [dict(item)]
    for v in (item.get("versions") or []):
        versions.append(dict(v))
    # 去重（同 version 取最新）
    seen: dict = {}
    for v in versions:
        seen[v.get("version", "")] = v
    out = sorted(seen.values(),
                 key=lambda x: _version_tuple(x.get("version", "0")), reverse=True)
    # 标记 latest
    if out:
        out[0]["latest"] = True
    return {
        "kind": kind,
        "id": item_id,
        "name": item.get("name"),
        "latest_version": item.get("version"),
        "total": len(out),
        "versions": out,
    }


# ==============================================================
# 缓存管理（按需拉取后的本地资源目录）
# ==============================================================
@router.get("/cache/overview")
def cache_overview():
    """返回 market/ 目录的缓存概况（空间占用、文件数、.git 状态等）。"""
    return market_sync.market_cache_overview()


@router.post("/cache/clean")
def cache_clean(payload: dict | None = None):
    """清理 market/ 目录下的资源缓存 + 独立的 archive zip 持久化缓存。

    默认保留 index.json 但清除 .git 和 zip。
    清理后资源列表/预览/安装会按需重新拉取（zip 会重新下载，约 40s）。
    """
    payload = payload or {}
    preserve_index = bool(payload.get("preserve_index", True))
    preserve_git = bool(payload.get("preserve_git", False))
    clean_zip = bool(payload.get("clean_zip", True))

    result = market_sync.clean_market_cache(
        preserve_index=preserve_index, preserve_git=preserve_git,
    )
    if clean_zip:
        result["zip"] = market_sync.clean_zip_cache()
        result["freed_bytes"] += result["zip"].get("freed_bytes", 0)
    result["overview_after"] = market_sync.market_cache_overview()
    return result


@router.get("/cache/item/{kind}/{item_id}")
def cache_item_status(kind: str, item_id: str):
    """查询单个资源在本地的缓存状态。"""
    status = market_sync.get_item_cache_status(kind, item_id)
    return status


def _save_item(item: dict) -> dict:
    """把一条市场资源写入索引。

    同 kind + id 已存在时：
    - 如果 version 相同（同版本重复发布）→ 原地覆盖（简单更新）
    - 如果 version 不同（新版本）→ 旧条目作为历史挂到 ``versions`` 数组里，
      旧目录改名追加 ``@<旧version>`` 后缀保留；新条目替换到顶层作为最新
    """
    data = _load_index(force=True)
    items = data.get("items", [])
    existing = next((it for it in items
                     if it.get("kind") == item.get("kind") and it.get("id") == item.get("id")), None)

    if not existing:
        items.append(item)
    elif existing.get("version") == item.get("version"):
        # 同版本原地覆盖
        idx = items.index(existing)
        items[idx] = item
    else:
        # 新版本 → 旧版挂 versions，旧目录改名保留
        _archive_old_version(existing, item)
        idx = items.index(existing)
        # 顶层：只保留最新条目；versions 数组挂在它上面
        merged = {**item}
        merged["versions"] = _merge_versions_list(existing, merged)
        items[idx] = merged

    items.sort(key=lambda it: (it.get("updated_at") or ""), reverse=True)
    data["items"] = items
    _write_index(data)
    return item


def _archive_old_version(old: dict, new: dict) -> None:
    """把旧版本目录 ``xxx/ppt`` 改名成 ``xxx/ppt@1.0.0`` 备份，供用户选旧版安装。"""
    old_dir = old.get("dir")
    new_dir = new.get("dir")
    if not old_dir or old_dir == new_dir:
        return
    old_version = old.get("version", "old")
    src = MARKET_ROOT / old_dir
    if src.exists() and src.is_dir():
        backup = src.parent / f"{src.name}@{old_version}"
        try:
            src.rename(backup)
        except OSError:
            # 目标已存在或其他异常 → 直接删除，旧版内容丢弃（索引里还留着条目指向它）
            import shutil as _sh
            _sh.rmtree(src, ignore_errors=True)


def _merge_versions_list(old_item: dict, new_item: dict) -> list:
    """把旧条目和它自带的 versions 数组合并，再追加当前 latest。

    历史条目的 dir 会自动改写为 ``原 dir@version`` 形式（匹配 _archive_old_version 的备份规则）。
    """
    versions: list = []
    # 旧条目上的 versions（可能已被多次追加）
    for v in (old_item.get("versions") or []):
        vv = dict(v)
        _ensure_versioned_dir(vv)
        versions.append(vv)
    # 旧条目本身（现在成了历史）
    old_entry = {k: v for k, v in old_item.items()
                 if k not in ("versions",)}
    if old_entry.get("version"):
        _ensure_versioned_dir(old_entry, base_dir=new_item.get("dir"))
        versions.append(old_entry)
    # 新版本条目作为最新
    new_entry = {k: v for k, v in new_item.items()
                 if k not in ("versions",)}
    if new_entry.get("version"):
        versions.append(new_entry)
    # 按版本倒序（最新在前）
    def _ver_key(v: dict) -> tuple:
        return _version_tuple(v.get("version", "0"))
    versions.sort(key=_ver_key, reverse=True)
    return versions


def _ensure_versioned_dir(v: dict, base_dir: str | None = None) -> None:
    """确保历史条目的 dir 指向 ``@version`` 备份目录。"""
    vver = v.get("version") or ""
    vdir = v.get("dir") or ""
    if vdir and vver and "@" not in vdir.rsplit("/", 1)[-1]:
        v["dir"] = _versioned_dir(vdir, vver)
    elif not vdir and base_dir and vver:
        v["dir"] = _versioned_dir(base_dir, vver)


def _version_tuple(v: str) -> tuple:
    """把 '1.2.3' 拆成 (1,2,3)，非数字段放后面避免异常。"""
    parts = []
    for p in str(v).split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(p)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _next_version(latest_version: str | None) -> str:
    """给定最新版本号，默认自动递增 patch：1.0.0 → 1.0.1。"""
    if not latest_version:
        return "1.0.0"
    parts = [0, 0, 0]
    for i, p in enumerate(str(latest_version).split(".")[:3]):
        try:
            parts[i] = int(p)
        except ValueError:
            parts[i] = 0
    parts[2] += 1
    return ".".join(str(x) for x in parts)


def _safe_author() -> str:
    """取当前登录 GitHub 账号作为**发布者**（= 上传者身份）。

    注意：author 字段语义是"谁把这条资源发布到市场的"，而不是资源文件里
    写的原始作者，所以直接取登录身份，不从资源 manifest/SKILL.md 等文件读取。
    未登录时回退为 AgentCluster。
    """
    return github_auth.get_login() or "AgentCluster"


# 发布到市场时要剥掉的智能体私有字段（模型配置因人而异，不应传播）
_AGENT_PRIVATE_FIELDS = (
    "id", "created_at", "sort_order", "group_id", "group_name",
    "model", "model_interface_id", "interface_id",
)


def _strip_agent_private(agent: dict) -> dict:
    """剥离智能体里不该上市场的字段（模型名、接口 id 等本地配置）。"""
    return {k: v for k, v in agent.items() if k not in _AGENT_PRIVATE_FIELDS}


def _publish_agent(local_id: str, version: str = "1.0.0") -> dict:
    try:
        aid = int(local_id)
    except ValueError:
        raise HTTPException(400, "智能体 id 必须是数字")
    agent = store.get_by("agents", aid)
    if not agent:
        raise HTTPException(404, f"本机智能体不存在：{aid}")
    slug = _slugify(agent.get("name", ""), f"agent-{aid}")
    dest = MARKET_ROOT / "agents" / slug
    dest.mkdir(parents=True, exist_ok=True)
    # 剥离模型/接口等本地私有配置（不同用户模型接口不同，不应传播）
    stripped = _strip_agent_private(agent)
    (dest / "agent.json").write_text(
        json.dumps(stripped, ensure_ascii=False, indent=2), encoding="utf-8")
    item = {
        "kind": "agent", "id": slug, "name": agent.get("name", slug),
        "description": agent.get("description", ""), "version": version,
        "author": _safe_author(), "icon": "🤖",
        "tags": [str(t) for t in (agent.get("tags") or [])],
        "dir": f"agents/{slug}", "file": "agent.json", "updated_at": _now(),
        "origin_agent_id": aid,
    }
    _save_item(item)
    return {"ok": True, "item": item}


def _publish_canvas(local_id: str, version: str = "1.0.0") -> dict:
    try:
        cid = int(local_id)
    except ValueError:
        raise HTTPException(400, "画布 id 必须是数字")
    canvas = store.get_by("canvases", cid)
    if not canvas:
        raise HTTPException(404, f"本机画布不存在：{cid}")

    agents = {a.get("id"): a for a in store.load("agents")}
    agent_groups = {g.get("id"): g for g in store.load("agent_groups")}

    # —— 收集画布引用的智能体 + 分组 ——
    seen_agent_ids: set[int] = set()
    group_ids_seen: set[int] = set()
    for n in canvas.get("nodes") or []:
        a = agents.get(n.get("agent_id"))
        if a and n.get("agent_id") not in seen_agent_ids:
            seen_agent_ids.add(n.get("agent_id"))
            gid = a.get("group_id")
            if gid is not None and gid in agent_groups:
                group_ids_seen.add(gid)

    # —— 为每个智能体分配画布内唯一的 key ——
    # key = 唯一目录名（同名智能体追加序号），安装时用它精确还原节点引用，
    # 避免画布内出现两个同名智能体时节点串位。
    agent_key_by_id: dict[int, str] = {}
    used_keys: set[str] = set()
    for agent_id in sorted(seen_agent_ids):
        a = agents.get(agent_id)
        if not a:
            continue
        base = _slugify(a.get("name", ""), f"agent-{agent_id}")
        key = base
        n = 2
        while key in used_keys:
            key = f"{base}-{n}"
            n += 1
        used_keys.add(key)
        agent_key_by_id[agent_id] = key

    # —— 画布节点：agent_ref 保留智能体名字（兼容旧包与前端预览），
    #    agent_key 记录唯一键（安装时精确匹配）——
    nodes = []
    for n in canvas.get("nodes") or []:
        a = agents.get(n.get("agent_id"))
        ref = a.get("name") if a else "未知智能体"
        node = {"id": n.get("id"), "agent_ref": ref,
                "x": n.get("x", 0), "y": n.get("y", 0)}
        key = agent_key_by_id.get(n.get("agent_id"))
        if key:
            node["agent_key"] = key
        nodes.append(node)

    # —— 画布目录：canvas.json + agents/*/agent.json + agent_groups.json ——
    slug = _slugify(canvas.get("name", ""), f"canvas-{cid}")
    dest = MARKET_ROOT / "canvases" / slug
    # 先清理旧格式（重新发布覆盖）
    if dest.exists():
        try:
            shutil.rmtree(dest)
        except OSError:
            pass
    dest.mkdir(parents=True, exist_ok=True)

    # 1. canvas.json —— 只存画布结构（节点/连线/模式），不含智能体快照
    canvas_payload = {
        "name": canvas.get("name") or slug,
        "description": canvas.get("description", ""),
        "nodes": nodes,
        "edges": canvas.get("edges") or [],
        "execution_mode": canvas.get("execution_mode", "serial"),
        # 保留 agents 快照字段（旧版画布兼容 + 便于预览端无额外 IO）
        "agents": [],
        "agent_groups": [],
    }

    # 2. agents/<key>/agent.json —— 每个智能体独立子目录
    agents_dir = dest / "agents"
    agents_dir.mkdir(exist_ok=True)
    agent_snapshots: list[dict] = []  # 也塞进 canvas.json.agents 便于预览
    for agent_id in sorted(seen_agent_ids):
        a = agents.get(agent_id)
        if not a:
            continue
        snap = _strip_agent_private(a)
        gid = a.get("group_id")
        if gid is not None and gid in agent_groups:
            snap["group_name"] = agent_groups[gid].get("name", "")
        # 画布内唯一键：同名智能体靠它区分（名字本身保持原样，不改名）
        snap["agent_key"] = agent_key_by_id[agent_id]
        agent_snapshots.append(snap)

        # 每个智能体写独立子目录（目录名 = 画布内唯一 key）
        a_dir = agents_dir / agent_key_by_id[agent_id]
        a_dir.mkdir(exist_ok=True)
        (a_dir / "agent.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. agent_groups.json —— 分组快照独立文件（画布内节点不会引用分组 id，只是描述性信息）
    groups_payload: list[dict] = []
    for gid in sorted(group_ids_seen):
        g = agent_groups.get(gid)
        if g:
            groups_payload.append({
                "name": g.get("name"),
                "description": g.get("description", ""),
            })
    if groups_payload:
        (dest / "agent_groups.json").write_text(
            json.dumps(groups_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # canvas.json 里也塞一份 agents/agent_groups（方便无额外 IO 就能解析）
    canvas_payload["agents"] = agent_snapshots
    canvas_payload["agent_groups"] = groups_payload
    (dest / "canvas.json").write_text(
        json.dumps(canvas_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    item = {
        "kind": "canvas", "id": slug, "name": canvas_payload["name"],
        "description": canvas_payload["description"], "version": version,
        "author": _safe_author(), "icon": "🗺",
        "tags": [f"{len(nodes)} 节点", f"{len(agent_snapshots)} 智能体"],
        "dir": f"canvases/{slug}",
        "file": "canvas.json", "updated_at": _now(), "origin_canvas_id": cid,
        "agent_count": len(agent_snapshots),
        "agent_group_count": len(groups_payload),
        "has_agents_dir": True,
    }
    _save_item(item)
    return {"ok": True, "item": item}


def _publish_directory(kind: str, local_id: str, version: str = "1.0.0") -> dict:
    folder = {"background": "backgrounds", "skill": "skills",
              "plugin": "plugins", "extension": "extensions"}[kind]
    src = store.ROOT / folder / local_id
    if not src.exists() or not src.is_dir():
        raise HTTPException(404, f"本机资源不存在：{src}")

    item: dict = {"kind": kind, "id": local_id, "dir": f"{folder}/{local_id}",
                  "updated_at": _now(), "version": version, "author": _safe_author()}

    if kind == "background":
        mf = src / "manifest.json"
        meta = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
        item.update(name=meta.get("name", local_id), description=meta.get("description", ""),
                    icon="🎨", tags=[meta.get("type", "animation")],
                    preview=meta.get("preview", ""), bg_type=meta.get("type"),
                    entry=meta.get("entry"))
    elif kind == "skill":
        fm = _skill_frontmatter(src / "SKILL.md")
        item.update(name=fm.get("label") or fm.get("name") or local_id,
                    description=str(fm.get("description", "")).split("\n")[0][:300],
                    version=str(fm.get("version", "1.0.0")),
                    icon="🧠",
                    tags=[str(t) for t in (fm.get("tags") or [])],
                    label=fm.get("label"), skill_name=fm.get("name", local_id))
    elif kind == "plugin":
        meta = _yaml_load(src / "manifest.yaml")
        item.update(name=_localized(meta.get("label")) or local_id,
                    description=_localized(meta.get("description")),
                    version=str(meta.get("version", "1.0.0")),
                    icon=meta.get("icon") or "🔧",
                    tags=[str(t) for t in (meta.get("tags") or [])])
    else:  # extension
        mf = src / "manifest.json"
        meta = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
        item.update(name=meta.get("name", local_id), description=meta.get("description", ""),
                    version=str(meta.get("version", "1.0.0")),
                    icon=meta.get("icon") or "🧩",
                    tags=[str(t) for t in (meta.get("tags") or [])],
                    ext_type=meta.get("type"), source="builtin",
                    frontend_files=meta.get("frontend_files", []),
                    backend_entry=meta.get("backend_entry"),
                    pages=meta.get("pages", []), config_page=meta.get("config_page"),
                    system_prompt_file=meta.get("system_prompt_file"),
                    viz_types=meta.get("viz_types", []))

    dest = MARKET_ROOT / folder / local_id
    _copy_dir(src, dest, overwrite=True)
    _save_item(item)
    return {"ok": True, "item": item}


def _skill_frontmatter(path: Path) -> dict:
    try:
        txt = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not txt.startswith("---"):
        return {}
    end = txt.find("\n---", 3)
    if end < 0:
        return {}
    try:
        import yaml
        return yaml.safe_load(txt[3:end]) or {}
    except Exception:
        return {}


def _yaml_load(path: Path) -> dict:
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _localized(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("zh_Hans") or next(iter(value.values()), "") or "")
    return str(value or "")