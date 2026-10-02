"""画布工作区：项目树聚合 + 回收站（基于 file_backup 快照）。

- 项目树：以 canvas.workdir 为根递归遍历，文件标注大小、修改时间、参与者。
- 归属：来源 file_backup 的跨会话文件操作记录（写/删前自动快照，含 agent_id/ts/op）。
- 回收站：file.delete 的快照记录即"被删文件"，支持还原（复用 restore_op）与彻底删除（purge_op）。

设计上不修改删除流程：智能体执行 file.delete 时 file_backup 已自动保存快照，
回收站直接消费这些 delete 记录，无需额外维护物理 trash 目录。
"""
import os
import shutil
import time
from pathlib import Path

from core.file_backup import list_all_ops, restore_op, purge_op, snapshot_before_delete
from core.store import load

MAX_NODES = 4000
MAX_DEPTH = 4
MAX_CHILDREN = 300   # 单目录最多列出的子项数，超大目录（如浏览器配置）只显示前 N 项


def _norm(p) -> str:
    """统一路径标识（Windows 下做 normcase，跨平台稳定匹配）。"""
    try:
        return os.path.normcase(os.path.abspath(str(p)))
    except Exception:
        return str(p)


def _under(path: str, workdir: str) -> bool:
    """path 是否落在 workdir 内（含等价目录）。"""
    if not workdir:
        return False
    try:
        Path(path).resolve().relative_to(Path(workdir).resolve())
        return True
    except Exception:
        return False


def _agents() -> dict:
    try:
        return {a.get("id"): a.get("name") for a in (load("agents") or [])}
    except Exception:
        return {}


def _snapshot_size(snap_dir: str) -> int:
    """快照目录里所有文件的总大小 = 被删文件的近似大小。"""
    if not snap_dir:
        return 0
    d = Path(snap_dir)
    if not d.exists():
        return 0
    total = 0
    for f in d.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except Exception:
            pass
    return total


def _ops_under(workdir: str) -> list:
    return [op for op in list_all_ops()
            if op.get("path") and _under(op["path"], workdir)]


def _attr_map(ops: list, names: dict) -> dict:
    """按路径聚合操作：创建者 / 最后修改者 / 参与者列表。"""
    grouped: dict = {}
    for op in ops:
        grouped.setdefault(_norm(op.get("path")), []).append(op)

    attr = {}
    for key, lst in grouped.items():
        lst.sort(key=lambda o: o.get("ts", ""))
        creator = None
        for o in lst:
            if str(o.get("op", "")).startswith("file.write") and not o.get("before_exists"):
                creator = o
                break
        last = lst[-1]
        participants = []
        seen = set()
        for o in lst:
            aid = o.get("agent_id")
            if aid in seen:
                continue
            seen.add(aid)
            participants.append({
                "id": aid,
                "name": names.get(aid, f"?{aid}"),
                "last_op": o.get("op"),
                "last_ts": o.get("ts"),
            })
        attr[key] = {
            "created_by": names.get(creator["agent_id"]) if creator else None,
            "modified_by": names.get(last.get("agent_id")),
            "last_ts": last.get("ts"),
            "participants": participants,
        }
    return attr


def build_tree(workdir: str) -> dict:
    """以 workdir 为根构建项目树，每个节点带 size/mtime/created_by/modified_by/participants。"""
    root = Path(workdir) if workdir else None
    if not root or not root.is_dir():
        return {"exists": False, "root": str(root) if root else "", "tree": [], "count": 0}

    names = _agents()
    attr = _attr_map(_ops_under(workdir), names)
    counter = {"n": 0}

    def make(path: Path, depth: int) -> dict:
        # 节点始终创建（顶层兄弟项绝不能被全局预算截断），预算只限制深层的子项遍历
        counter["n"] += 1
        rel = path.relative_to(root)
        is_dir = path.is_dir()
        a = attr.get(_norm(path)) or {}
        node = {
            "name": path.name or str(root),
            "path": str(path),
            "rel": "" if str(rel) == "." else rel.as_posix(),
            "is_dir": is_dir,
            "created_by": a.get("created_by"),
            "modified_by": a.get("modified_by"),
            "last_ts": a.get("last_ts"),
            "participants": a.get("participants", []),
            "children": [],
        }
        try:
            node["mtime"] = int(path.stat().st_mtime)
        except Exception:
            node["mtime"] = 0
        if is_dir:
            node["size"] = 0
            if depth < MAX_DEPTH and counter["n"] < MAX_NODES:
                try:
                    entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
                except Exception:
                    entries = []
                shown = 0
                for e in entries:
                    if counter["n"] >= MAX_NODES or shown >= MAX_CHILDREN:
                        node["truncated"] = True
                        break
                    child = make(e, depth + 1)
                    node["children"].append(child)
                    node["size"] += child.get("size", 0)
                    shown += 1
        else:
            try:
                node["size"] = path.stat().st_size
            except Exception:
                node["size"] = 0
        return node

    tree = []
    try:
        entries = sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except Exception:
        entries = []
    # 顶层全部枚举，确保兄弟文件夹都可见
    for e in entries:
        tree.append(make(e, 0))
    return {"exists": True, "root": str(root), "tree": tree, "count": counter["n"]}


def list_trash(workdir: str) -> list:
    """列出 workdir 内被智能体删除的文件（来源 file.delete 快照记录）。

    每项含 conv_id/op_idx（还原/彻底删除用）、name/rel/path、depth（项目树层级，
    用于前端按树位置绘制卡片）、size（快照总大小）、deleted_at（删除时间）、
    deleter（删除者）、restorable（快照在且目标已不存在）。
    """
    if not workdir:
        return []
    names = _agents()
    items = []
    wd_root = Path(workdir).resolve()
    for op in _ops_under(workdir):
        if op.get("op") != "file.delete":
            continue
        path = Path(op.get("path", ""))
        try:
            rel = path.resolve().relative_to(wd_root).as_posix()
        except Exception:
            rel = path.name
        snap_dir = op.get("snapshot_dir", "")
        snap_exists = bool(snap_dir) and Path(snap_dir).exists()
        item_dir = False
        if snap_exists:
            try:
                item_dir = any(f.is_dir() for f in Path(snap_dir).iterdir())
            except Exception:
                item_dir = False
        try:
            target_exists = path.exists()
        except Exception:
            target_exists = False
        aid = op.get("agent_id")
        deleter = "用户（手动）" if aid is None else names.get(aid, f"?{aid}")
        items.append({
            "conv_id": op.get("_conv_id"),
            "op_idx": op.get("_idx"),
            "name": path.name,
            "path": str(path),
            "rel": rel,
            "depth": max(0, len(rel.split("/")) - 1) if rel else 0,
            "is_dir": item_dir,
            "size": _snapshot_size(snap_dir),
            "deleted_at": op.get("ts"),
            "deleter": deleter,
            "deleter_id": op.get("agent_id"),
            "restorable": snap_exists and not target_exists,
            "purged": bool(op.get("purged")),
            "restored": bool(op.get("restored")),
            "note": op.get("note", ""),
        })
    # 按相对路径排序：父目录自然排在子文件前，贴合项目树位置
    items.sort(key=lambda x: (x["rel"].lower(), x["deleted_at"] or ""))
    return items


def restore_trash(conv_id, op_idx) -> dict:
    if conv_id is None or op_idx is None:
        return {"error": "缺少 conv_id / op_idx"}
    return restore_op(int(conv_id), int(op_idx))


def purge_trash(conv_id, op_idx) -> dict:
    if conv_id is None or op_idx is None:
        return {"error": "缺少 conv_id / op_idx"}
    return purge_op(int(conv_id), int(op_idx))


# ==============================================================
# 工作区内的文件操作（读/写/重命名/复制/删除进回收站）
# ==============================================================

# 手动删除使用哨兵会话 id 0 记录快照（与智能体的会话区分开，删除者显示"用户"）
USER_CONV_ID = 0

TEXT_EXTS = {
    "json", "yaml", "yml", "md", "txt", "toml", "ini", "cfg",
    "py", "js", "ts", "vue", "html", "css", "scss", "svg",
    "log", "csv", "xml", "env", "ps1", "bat", "sh",
}


def _resolve_under(workdir: str, path: str) -> Path | None:
    """把 path 解析为 workdir 内的绝对路径（防路径穿越）；不在其内返回 None。"""
    if not workdir:
        return None
    wd = Path(workdir).resolve()
    p = Path(path)
    if not p.is_absolute():
        p = wd / p
    try:
        rp = p.resolve()
    except Exception:
        return None
    try:
        rp.relative_to(wd)
        return rp
    except ValueError:
        return None


def read_file(workdir: str, path: str) -> dict:
    """读取工作区内文本文件（UTF-8，失败回退 gbk；≤2MB）。"""
    p = _resolve_under(workdir, path)
    if not p:
        return {"error": "路径不在工作区内"}
    if not p.is_file():
        return {"error": f"不是文件或不存在：{path}"}
    ext = p.suffix.lower().lstrip(".")
    if ext and ext not in TEXT_EXTS:
        return {"error": f"不支持读取的文件类型：.{ext}"}
    if p.stat().st_size > 2 * 1024 * 1024:
        return {"error": "文件过大（>2MB），不支持在线编辑"}
    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            content = p.read_text(encoding="gbk")
        except Exception as e:
            return {"error": f"编码无法识别：{e}"}
    return {"ok": True, "path": str(p), "name": p.name, "ext": ext,
            "size": p.stat().st_size, "content": content}


def write_file(workdir: str, path: str, content: str) -> dict:
    """写文件（UTF-8），自动创建父目录。"""
    p = _resolve_under(workdir, path)
    if not p:
        return {"error": "路径不在工作区内"}
    if p.is_dir():
        return {"error": "目标是目录"}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content or "", encoding="utf-8")
    return {"ok": True, "path": str(p), "size": len((content or "").encode("utf-8"))}


def rename_file(workdir: str, path: str, new_name: str) -> dict:
    """重命名文件/目录（仅改名，不移动）。"""
    p = _resolve_under(workdir, path)
    if not p or not p.exists():
        return {"error": "文件不存在"}
    new_name = (new_name or "").strip()
    if not new_name or "/" in new_name or "\\" in new_name:
        return {"error": "新名称非法"}
    dest = p.with_name(new_name)
    if dest.exists():
        return {"error": f"已存在同名：{new_name}"}
    p.rename(dest)
    return {"ok": True, "path": str(dest), "old_path": str(p)}


def copy_file(workdir: str, src: str, dest_dir: str) -> dict:
    """复制文件/目录到目标目录（同名自动加" (副本N)"后缀）。"""
    sp = _resolve_under(workdir, src)
    if not sp or not sp.exists():
        return {"error": "源文件不存在"}
    dp = _resolve_under(workdir, dest_dir)
    if not dp or not dp.is_dir():
        return {"error": "目标目录不存在"}
    dest = dp / sp.name
    if dest.exists():
        stem, suf = sp.stem, sp.suffix
        i = 1
        while True:
            cand = dp / f"{stem} (副本{i}){suf}"
            if not cand.exists():
                dest = cand
                break
            i += 1
    if sp.is_file():
        shutil.copy2(sp, dest)
    else:
        shutil.copytree(sp, dest)
    return {"ok": True, "path": str(dest)}


def delete_to_trash(workdir: str, path: str) -> dict:
    """删除文件/目录并进入回收站（快照记录在哨兵会话 USER_CONV_ID 下）。"""
    p = _resolve_under(workdir, path)
    if not p or not p.exists():
        return {"error": "文件不存在"}
    tool_call_id = f"user_{int(time.time() * 1000)}"
    rec = snapshot_before_delete(conv_id=USER_CONV_ID, tool_call_id=tool_call_id,
                                 agent_id=None, path=str(p))
    if p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)
    return {"ok": True, "path": str(p),
            "restorable": bool(rec.get("snapshot_dir")), "record": rec}
