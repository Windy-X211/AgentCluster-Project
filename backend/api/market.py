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
from core import market_sync

router = APIRouter()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MARKET_ROOT = _PROJECT_ROOT / "market"
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

# 同步是耗时网络操作，同一时刻只允许一个
_sync_lock = threading.Lock()
_push_lock = threading.Lock()


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
    canvas_names = {c.get("name") for c in canvases}
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
            it["installed"] = it.get("name") in agent_names
        elif kind == "canvas":
            it["installed"] = it.get("name") in canvas_names
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
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(409, "已有同步任务进行中，请稍候")
    try:
        return market_sync.sync_market(force=force)
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _sync_lock.release()


@router.post("/push")
def push(payload: dict | None = None):
    """提交并推送本地 market/ 改动到远程仓库（官方地址，镜像不支持 push）。"""
    payload = payload or {}
    message = str(payload.get("message") or "").strip() or None
    if not _push_lock.acquire(blocking=False):
        raise HTTPException(409, "已有推送任务进行中，请稍候")
    try:
        return market_sync.push_market(message)
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _push_lock.release()


# ==============================================================
# 安装
# ==============================================================
@router.post("/install")
def install(payload: dict):
    kind = (payload.get("kind") or "").strip()
    item_id = _safe_id((payload.get("id") or "").strip())
    item = _find_item(kind, item_id)
    if not item:
        raise HTTPException(404, f"市场中未找到资源：{kind}/{item_id}")

    if kind == "agent":
        return _install_agent(item)
    if kind == "canvas":
        return _install_canvas(item)
    if kind == "extension":
        return _install_extension(item)
    if kind in ("background", "skill", "plugin"):
        return _install_directory(item)
    raise HTTPException(400, f"不支持的资源类型：{kind}")


def _install_agent(item: dict) -> dict:
    src = _item_dir(item) / (item.get("file") or "agent.json")
    if not src.exists():
        raise HTTPException(500, f"市场资源文件缺失：{src}")
    raw = json.loads(src.read_text(encoding="utf-8"))
    rec = {k: v for k, v in raw.items()
           if k not in ("id", "created_at", "sort_order", "group_id")}
    rec["id"] = store.next_id("agents")
    rec["created_at"] = datetime.utcnow().isoformat()
    rec.setdefault("enabled", True)
    store.upsert("agents", rec)
    return {"ok": True, "kind": "agent", "id": rec["id"], "name": rec.get("name")}


def _install_canvas(item: dict) -> dict:
    src = _item_dir(item) / (item.get("file") or "canvas.json")
    if not src.exists():
        raise HTTPException(500, f"市场资源文件缺失：{src}")
    raw = json.loads(src.read_text(encoding="utf-8"))

    # 市场画布内联了节点引用的智能体快照：按名称复用本地已有智能体，缺失则创建
    local_agents = store.load("agents")
    by_name = {a.get("name"): a.get("id") for a in local_agents}
    ref_to_id: dict[str, int] = {}
    created: list[str] = []
    for snap in raw.get("agents") or []:
        name = snap.get("name")
        if not name:
            continue
        if name in by_name:
            ref_to_id[name] = int(by_name[name])
            continue
        rec = {k: v for k, v in snap.items()
               if k not in ("id", "created_at", "sort_order", "group_id")}
        rec["id"] = store.next_id("agents")
        rec["created_at"] = datetime.utcnow().isoformat()
        rec.setdefault("enabled", True)
        store.upsert("agents", rec)
        ref_to_id[name] = rec["id"]
        created.append(name)

    nodes = []
    skipped = 0
    for n in raw.get("nodes") or []:
        ref = n.get("agent_ref")
        aid = ref_to_id.get(ref) if ref else n.get("agent_id")
        if aid is None:
            skipped += 1
            continue
        nodes.append({"id": n.get("id"), "agent_id": aid,
                      "x": n.get("x", 0), "y": n.get("y", 0)})

    rec = {
        "id": store.next_id("canvases"),
        "name": raw.get("name") or item.get("name") or "市场画布",
        "description": raw.get("description", ""),
        "nodes": nodes,
        "edges": raw.get("edges") or [],
        "created_at": datetime.utcnow().isoformat(),
        "workdir": None,
        "execution_mode": raw.get("execution_mode", "serial"),
        "ws_pin": None,
    }
    store.upsert("canvases", rec)
    return {"ok": True, "kind": "canvas", "id": rec["id"], "name": rec["name"],
            "created_agents": created, "skipped_nodes": skipped}


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
# 删除市场资源
# ==============================================================
@router.delete("/items/{kind}/{item_id}")
def delete_item(kind: str, item_id: str):
    """删除市场条目及其资源目录（不影响本机已安装副本）。"""
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
    return {"ok": True, "kind": kind, "id": item_id, "name": item.get("name")}


# ==============================================================
# 可发布的本机资源
# ==============================================================
@router.get("/publishable")
def publishable(kind: str = ""):
    kind = (kind or "").strip()
    if kind not in {"agent", "canvas", "background", "skill", "plugin", "extension"}:
        raise HTTPException(400, f"不支持的类型：{kind}")
    return {"kind": kind, "items": _publishable_items(kind)}


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
            out.append({"id": d.name, "name": d.name, "description": ""})
    elif kind == "plugin":
        root = store.ROOT / "plugins"
        for d in sorted(root.iterdir()) if root.exists() else []:
            if not d.is_dir() or not (d / "manifest.yaml").exists():
                continue
            out.append({"id": d.name, "name": d.name, "description": ""})
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
    if kind == "agent":
        result = _publish_agent(local_id)
    elif kind == "canvas":
        result = _publish_canvas(local_id)
    elif kind in ("background", "skill", "plugin", "extension"):
        result = _publish_directory(kind, local_id)
    else:
        raise HTTPException(400, f"不支持的资源类型：{kind}")

    # 可选：发布后立即提交并推送到远程市场仓库（失败不回滚本地发布）
    if payload.get("push"):
        try:
            result["push"] = market_sync.push_market(f"publish {kind}/{local_id}")
        except Exception as e:
            result["push_error"] = str(e)
    return result


def _save_item(item: dict) -> dict:
    """把一条市场资源写入索引（同 kind + id 视为更新，其余条目原样保留）。"""
    data = _load_index(force=True)
    items = [it for it in data.get("items", [])
             if not (it.get("kind") == item.get("kind") and it.get("id") == item.get("id"))]
    items.append(item)
    items.sort(key=lambda it: (it.get("updated_at") or ""), reverse=True)
    data["items"] = items
    _write_index(data)
    return item


def _publish_agent(local_id: str) -> dict:
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
    (dest / "agent.json").write_text(
        json.dumps(agent, ensure_ascii=False, indent=2), encoding="utf-8")
    item = {
        "kind": "agent", "id": slug, "name": agent.get("name", slug),
        "description": agent.get("description", ""), "version": "1.0.0",
        "author": "AgentCluster", "icon": "🤖",
        "tags": [str(t) for t in (agent.get("tags") or [])],
        "dir": f"agents/{slug}", "file": "agent.json", "updated_at": _now(),
        "origin_agent_id": aid,
    }
    _save_item(item)
    return {"ok": True, "item": item}


def _publish_canvas(local_id: str) -> dict:
    try:
        cid = int(local_id)
    except ValueError:
        raise HTTPException(400, "画布 id 必须是数字")
    canvas = store.get_by("canvases", cid)
    if not canvas:
        raise HTTPException(404, f"本机画布不存在：{cid}")

    agents = {a.get("id"): a for a in store.load("agents")}
    refs: list[dict] = []
    seen: set[int] = set()
    nodes = []
    for n in canvas.get("nodes") or []:
        a = agents.get(n.get("agent_id"))
        ref = a.get("name") if a else "未知智能体"
        nodes.append({"id": n.get("id"), "agent_ref": ref,
                      "x": n.get("x", 0), "y": n.get("y", 0)})
        if a and n.get("agent_id") not in seen:
            seen.add(n.get("agent_id"))
            refs.append({k: v for k, v in a.items()
                         if k not in ("id", "created_at", "sort_order", "group_id")})

    slug = _slugify(canvas.get("name", ""), f"canvas-{cid}")
    dest = MARKET_ROOT / "canvases" / slug
    dest.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": canvas.get("name") or slug,
        "description": canvas.get("description", ""),
        "nodes": nodes,
        "edges": canvas.get("edges") or [],
        "execution_mode": canvas.get("execution_mode", "serial"),
        "agents": refs,
    }
    (dest / "canvas.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    item = {
        "kind": "canvas", "id": slug, "name": payload["name"],
        "description": payload["description"], "version": "1.0.0",
        "author": "AgentCluster", "icon": "🗺",
        "tags": [f"{len(nodes)} 节点"], "dir": f"canvases/{slug}",
        "file": "canvas.json", "updated_at": _now(), "origin_canvas_id": cid,
        "agent_count": len(refs),
    }
    _save_item(item)
    return {"ok": True, "item": item}


def _publish_directory(kind: str, local_id: str) -> dict:
    folder = {"background": "backgrounds", "skill": "skills",
              "plugin": "plugins", "extension": "extensions"}[kind]
    src = store.ROOT / folder / local_id
    if not src.exists() or not src.is_dir():
        raise HTTPException(404, f"本机资源不存在：{src}")

    item: dict = {"kind": kind, "id": local_id, "dir": f"{folder}/{local_id}",
                  "updated_at": _now(), "version": "1.0.0", "author": "AgentCluster"}

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
                    author=fm.get("author", "AgentCluster"), icon="🧠",
                    tags=[str(t) for t in (fm.get("tags") or [])],
                    label=fm.get("label"), skill_name=fm.get("name", local_id))
    elif kind == "plugin":
        meta = _yaml_load(src / "manifest.yaml")
        item.update(name=_localized(meta.get("label")) or local_id,
                    description=_localized(meta.get("description")),
                    version=str(meta.get("version", "1.0.0")),
                    author=meta.get("author", "AgentCluster"),
                    icon=meta.get("icon") or "🔧",
                    tags=[str(t) for t in (meta.get("tags") or [])])
    else:  # extension
        mf = src / "manifest.json"
        meta = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
        item.update(name=meta.get("name", local_id), description=meta.get("description", ""),
                    version=str(meta.get("version", "1.0.0")),
                    author=meta.get("author", "AgentCluster"),
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