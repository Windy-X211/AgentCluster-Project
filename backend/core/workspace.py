"""画布工作区：项目树聚合 + 回收站（基于 file_backup 快照）。

- 项目树：以 canvas.workdir 为根递归遍历，文件标注大小、修改时间、参与者。
- 归属：来源 file_backup 的跨会话文件操作记录（写/删前自动快照，含 agent_id/ts/op）。
- 回收站：file.delete 的快照记录即"被删文件"，支持还原（复用 restore_op）与彻底删除（purge_op）。

设计上不修改删除流程：智能体执行 file.delete 时 file_backup 已自动保存快照，
回收站直接消费这些 delete 记录，无需额外维护物理 trash 目录。
"""
import os
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path

from core.file_backup import list_all_ops_cached, restore_op, purge_op, snapshot_before_delete
from core.store import load

MAX_NODES = 8000
MAX_DEPTH = 4
MAX_CHILDREN = 300   # 单目录最多列出的子项数，超大目录（如浏览器配置）只显示前 N 项
# 顶层 3 层（工作区根 → 子目录 → 孙目录）不受全局节点预算限制：
# .git / node_modules 等大目录吃光预算时，只会让更深层级退化为叶子，
# 保证兄弟文件夹永远看得见、点得开（否则会出现"文件夹存在但点开是空的"）
GUARANTEE_DEPTH = 2   # _dir_node 的 depth < 该值时无视预算，始终完整列出并展开


def _norm_inner(p) -> str:
    """统一路径标识（Windows 下做 normcase，跨平台稳定匹配）。"""
    try:
        return os.path.normcase(os.path.abspath(str(p)))
    except Exception:
        return str(p)


# 路径归一化在每次扫描里会被调用上万次（每个节点一次），abspath 本身不便宜，做记忆化
@lru_cache(maxsize=200000)
def _norm_cached(p: str) -> str:
    return _norm_inner(p)


def _norm(p) -> str:
    return _norm_cached(str(p))


def _under(path: str, workdir: str) -> bool:
    """path 是否落在 workdir 内（含等价目录）。

    用 normcase+abspath 的前缀比较（不调用 resolve()：每次 resolve 在 Windows 上
    会做完整的路径解析/网络盘探测，聚合全部会话 ops 时开销很大）。
    """
    if not workdir:
        return False
    a = _norm(path)
    w = _norm(workdir)
    return a == w or a.startswith(w + os.sep)


_agents_cache = {"at": 0.0, "data": {}}


def _agents() -> dict:
    """智能体名映射（短 TTL 缓存：build_tree 每次请求都要用，避免反复读盘解析）。"""
    now = time.time()
    if not _agents_cache["data"] or now - _agents_cache["at"] > 2.0:
        try:
            _agents_cache["data"] = {a.get("id"): a.get("name") for a in (load("agents") or [])}
        except Exception:
            return {}
        _agents_cache["at"] = now
    return _agents_cache["data"]


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
    return [op for op in list_all_ops_cached()
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


# ==============================================================
# 项目树扫描：节点缓存 + rev 版本号
#
# - 每个路径的节点对象会被缓存；子项未变（文件 size/mtime 一致）时直接复用旧对象，
#   前端据此只更新真正变动的行，界面不闪。
# - 每个工作区维护 rev：树内容有变化时 +1。前端带上次 rev 轮询，
#   未变化时只回一个极小的 {changed:false}，既实时又省流量。
# - 扫描用 os.scandir（一次系统调用拿全目录项的 stat），替代旧版每项 3 次 stat。
# ==============================================================
_node_cache: dict = {}     # 绝对路径 -> 节点 dict（跨扫描复用，未变的节点保持同一对象）
_node_cache_lock = threading.Lock()
_rev_lock = threading.Lock()
_revs: dict = {}           # workdir -> rev
_scan_locks: dict = {}     # workdir -> Lock（同工作区扫描串行，保护共享缓存）
_MAX_NODE_CACHE = 120000


def _scan_lock(workdir: str) -> threading.Lock:
    key = _norm(workdir)
    with _node_cache_lock:
        lk = _scan_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _scan_locks[key] = lk
        return lk


def _bump_rev(workdir: str, changed: bool) -> int:
    key = _norm(workdir or "")
    with _rev_lock:
        cur = int(_revs.get(key, 0))
        if changed:
            cur += 1
            _revs[key] = cur
        return cur


def get_rev(workdir: str) -> int:
    """当前工作区树的版本号（build_tree 内容变化时自增）。"""
    with _rev_lock:
        return int(_revs.get(_norm(workdir or ""), 0))


def _scan_entries(d: str):
    """枚举目录入口：(名称, 是否目录, 大小, mtime)；目录不可读返回 None。"""
    try:
        out = []
        with os.scandir(d) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    out.append((e.name, e.is_dir(follow_symlinks=False), st.st_size, int(st.st_mtime)))
                except OSError:
                    continue
        return out
    except OSError:
        return None


def _fast_norm(p: str) -> str:
    """扫描内部的路径归一化：路径都由绝对 root 逐级拼接而来，只需 normcase。"""
    return os.path.normcase(p)


def _child_rel(parent_rel: str, name: str) -> str:
    return name if not parent_rel else parent_rel + "/" + name


def _new_node(name: str, path: str, rel: str, is_dir: bool, mtime: int, size: int) -> dict:
    return {
        "name": name or path,
        "path": path,
        "rel": rel,
        "is_dir": is_dir,
        "created_by": None,
        "modified_by": None,
        "last_ts": None,
        "participants": [],
        "children": [],
        "mtime": mtime,
        "size": 0 if is_dir else size,
    }


def _leaf_node(path: str, rel: str, name: str, is_dir: bool, mtime: int,
               size: int, counter: dict) -> tuple[dict, bool]:
    """文件（或超出深度预算的目录）节点：size/mtime 未变则复用缓存对象。"""
    counter["n"] += 1
    key = path          # 扫描内所有路径都由同一绝对 root 拼接，无需再归一化
    old = _node_cache.get(key)
    if old and old.get("is_dir") == is_dir and old.get("mtime") == mtime and old.get("size") == size:
        return old, False
    node = _new_node(name, path, rel, is_dir, mtime, size)
    _node_cache[key] = node
    return node, True


def _dir_node(path: str, rel: str, name: str, depth: int, counter: dict, seen: set) -> tuple[dict, bool]:
    """目录节点：子项未变时整支复用旧节点对象（前端合并可整支跳过）。"""
    key = path
    seen.add(key)
    counter["n"] += 1
    entries = _scan_entries(path) or []
    old = _node_cache.get(key)
    old_children = (old or {}).get("children") or []
    old_by_name = {c.get("name"): c for c in old_children}
    changed = old is None
    children = []
    total = 0
    shown = 0
    truncated = False
    for ename, is_dir, sz, mt in sorted(entries, key=lambda x: (not x[1], x[0].lower())):
        if shown >= MAX_CHILDREN:
            truncated = True
            break
        over = counter["n"] >= MAX_NODES
        guaranteed = depth < GUARANTEE_DEPTH
        if over and not guaranteed:
            # 预算耗尽且已超出保证展示的层级 → 本目录剩余条目不再列出
            truncated = True
            break
        cpath = path + os.sep + ename
        crel = _child_rel(rel, ename)
        if is_dir and depth < MAX_DEPTH and (not over or guaranteed):
            child, ch = _dir_node(cpath, crel, ename, depth + 1, counter, seen)
            # 目录可能是"沿用的旧对象"，只能用子树自身的"有变化"结果判断
            if ch:
                changed = True
        else:
            # 超出深度/预算：目录退化为叶子节点（size=0，不再展开）
            child, ch = _leaf_node(cpath, crel, ename, is_dir, mt, 0 if is_dir else sz, counter)
            seen.add(cpath)
            if ch or old_by_name.get(ename) is not child:
                changed = True
        children.append(child)
        total += child.get("size", 0)
        shown += 1
    if [c.get("name") for c in old_children] != [c.get("name") for c in children]:
        changed = True
    if bool((old or {}).get("truncated")) != truncated:
        changed = True
    if not changed and old is not None:
        # 整棵子树无变化：沿用旧目录对象
        return old, False
    try:
        mtime_self = int(os.stat(path).st_mtime)
    except Exception:
        mtime_self = 0
    node = _new_node(name, path, rel, True, mtime_self, 0)
    node["size"] = total
    node["children"] = children
    if truncated:
        node["truncated"] = True
    _node_cache[key] = node
    return node, changed


def _prune_node_cache(root: str, seen: set) -> bool:
    """丢弃本次扫描未见到的缓存节点（被删除/移走的路径），并控制缓存总量。

    返回是否有缓存被剔除（= 有东西消失，也算"树变了"）。
    """
    prefix = root + os.sep
    removed = False
    with _node_cache_lock:
        if len(_node_cache) > _MAX_NODE_CACHE:
            _node_cache.clear()
            return True
        for k in list(_node_cache):
            if k not in seen and k.startswith(prefix):
                _node_cache.pop(k, None)
                removed = True
    return removed


def _apply_attrs(nodes: list, attr: dict) -> None:
    """把"参与者/创建者/最后修改者"（来自文件操作记录）挂到节点上。"""
    for n in nodes:
        a = attr.get(_fast_norm(n.get("path") or "")) or {}
        n["created_by"] = a.get("created_by")
        n["modified_by"] = a.get("modified_by")
        n["last_ts"] = a.get("last_ts")
        n["participants"] = a.get("participants", [])
        if n.get("children"):
            _apply_attrs(n["children"], attr)


def build_tree(workdir: str) -> dict:
    """以 workdir 为根构建项目树（增量扫描），每个节点带 size/mtime/创建者/修改者/参与者。

    返回值含 rev：树内容变化时自增，供前端差量轮询。
    """
    root = os.path.abspath(workdir) if workdir else ""
    if not root or not os.path.isdir(root):
        return {"exists": False, "root": root,
                "tree": [], "count": 0, "rev": get_rev(workdir or "")}

    names = _agents()
    attr = _attr_map(_ops_under(workdir), names)
    counter = {"n": 0}
    seen: set = set()
    tree = []
    changed = False
    with _scan_lock(workdir):
        entries = _scan_entries(root) or []
        # 顶层全部枚举，确保兄弟文件夹都可见
        for name, is_dir, sz, mt in sorted(entries, key=lambda x: (not x[1], x[0].lower())):
            p = root + os.sep + name
            if is_dir:
                node, ch = _dir_node(p, name, name, 0, counter, seen)
            else:
                node, ch = _leaf_node(p, name, name, False, mt, sz, counter)
                seen.add(p)
            tree.append(node)
            changed = changed or ch
        changed = changed or _prune_node_cache(root, seen)
        if attr:
            # 无任何文件操作记录的工作区直接跳过（绝大多数轮询都是空 attr）
            _apply_attrs(tree, attr)
    rev = _bump_rev(workdir, changed)
    return {"exists": True, "root": root, "tree": tree, "count": counter["n"], "rev": rev}


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

# 可通过"运行"（双击 / 右键）启动的文件扩展名。
# 值 = 运行该文件所需的命令模板（{f} 会被替换成带引号的绝对路径），
# 经系统 shell 执行；Windows 下依赖 PATH 中的运行时（python / node / java / powershell 等）。
RUN_EXTS = {
    "py": "python {f}",
    "js": "node {f}",
    "mjs": "node {f}",
    "cjs": "node {f}",
    "ts": "npx tsx {f}",
    "tsx": "npx tsx {f}",
    "sh": "bash {f}",
    "bat": "{f}",
    "cmd": "{f}",
    "ps1": "powershell -NoProfile -ExecutionPolicy Bypass -File {f}",
    "psm1": "powershell -NoProfile -ExecutionPolicy Bypass -File {f}",
    "html": "__open__ {f}",
    "htm": "__open__ {f}",
    "exe": "{f}",
    "msi": "msiexec /i {f}",
    "jar": "java -jar {f}",
}


def shell_quote(s: str) -> str:
    """给路径加双引号，内部双引号做转义。"""
    return '"' + str(s).replace('"', '\\"') + '"'


def build_run_command(abs_path: str, ext: str) -> str | None:
    """按扩展名把（已解析好的）绝对路径拼成 shell 命令。

    扩展名不在 RUN_EXTS 中返回 None；__open__ 模板会被替换为系统打开命令
    （Windows: start / macOS: open / Linux: xdg-open）。
    """
    import os as _os
    template = RUN_EXTS.get(ext.lower().lstrip("."))
    if not template:
        return None
    quoted = shell_quote(abs_path)
    if template == "__open__ {f}":
        uname = _os.uname().sysname.lower() if hasattr(_os, "uname") else ""
        opener = "start" if _os.name == "nt" else ("open" if uname.startswith("darwin") else "xdg-open")
        # Windows 的 start 第一个参数是窗口标题，需显式空标题，否则路径会被当作标题
        cmd = f'start "" {quoted}' if opener == "start" else f"{opener} {quoted}"
        return cmd
    return template.replace("{f}", quoted)

def open_with_system(path: str) -> dict:
    """用系统默认程序打开文件。

    Windows 用 os.startfile（等价资源管理器双击）；macOS 用 open；Linux 用 xdg-open。
    """
    p = Path(path)
    if not p.exists():
        return {"error": f"文件不存在：{path}"}
    try:
        if os.name == "nt":
            os.startfile(str(p))
        else:
            uname = os.uname().sysname.lower() if hasattr(os, "uname") else ""
            opener = "open" if uname.startswith("darwin") else "xdg-open"
            subprocess.Popen([opener, str(p)])
    except Exception as e:
        return {"error": f"打开失败：{e}"}
    return {"ok": True, "path": str(p), "opened": True}


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
    """读取工作区内文件。文本走 UTF-8（失败回退 gbk；≤2MB）；
    Office/PDF（docx/doc/xls/xlsx/ppt/pptx/pdf）走专用库抽取纯文本。
    """
    p = _resolve_under(workdir, path)
    if not p:
        return {"error": "路径不在工作区内"}
    if not p.is_file():
        return {"error": f"不是文件或不存在：{path}"}
    ext = p.suffix.lower().lstrip(".")
    # —— Office 文档：走 commands 里的抽取函数（避免二进制解码乱码）——
    OFFICE_EXTS = {"docx", "doc", "xlsx", "xls", "pptx", "ppt", "pdf"}
    if ext in OFFICE_EXTS:
        try:
            from core.commands import _extract_office_text
            content = _extract_office_text(p)
        except Exception as e:
            return {"error": f"Office 文档解析失败：{e}"}
        return {"ok": True, "path": str(p), "name": p.name, "ext": ext,
                "size": p.stat().st_size, "content": content, "office": True}
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
    """复制文件/目录到目标目录（同名自动加" (副本N)"后缀）。

    - src 为绝对路径时按绝对路径使用（用于跨工作区粘贴：从 A 工作区复制后到 B 工作区粘贴）
    - src 为相对路径时解析到本工作区内
    - dest_dir 永远限制在工作区内（不允许跨工作区写入）
    """
    sp = Path(src)
    if sp.is_absolute():
        if not sp.exists():
            return {"error": f"源文件不存在（跨工作区路径）：{src}"}
    else:
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
