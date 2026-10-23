'''everything_search 插件执行代码 — 对应 manifest.yaml + tool.yaml

基于 everything 库（PyPI: everything-sdk，Voidtools Everything 官方 SDK v3 的 Python 封装），
通过原生 IPC 调用 Everything 搜索引擎，实现毫秒级文件搜索与应用启动。

前置条件：
  1. pip install everything-sdk
  2. 安装并运行 Everything 1.5+（https://www.voidtools.com/downloads/ 下 "Everything 1.5 Alpha"）
     —— v3 SDK 的 Everything3_x64.dll 随 1.5 安装包提供；Everything 1.4 不兼容。
  3. 若自动探测不到 DLL，可用环境变量 EVERYTHING_SDK_DIR 指向其所在目录。
'''

import os
import subprocess

_CLIENT = None          # 成功时为 EverythingClient 实例，失败时为 dict（错误信息）
_DLL_PATH_CACHE = None

_SDK_VERSION = "1.5.0.1409a"

_DLL_CANDIDATES = (
    r"F:\Everything\main\Everything3_x64.dll",
    r"C:\Program Files\Everything 1.5a\Everything3_x64.dll",
    r"C:\Program Files (x86)\Everything 1.5a\Everything3_x64.dll",
    r"C:\Program Files\Everything\Everything3_x64.dll",
    r"C:\Program Files\Everything 1.4\Everything3_x64.dll",
)


def _err(message, **extra):
    out = {"ok": False, "error": message}
    out.update(extra)
    return out


def _find_dll():
    """按 环境变量 -> 常见安装路径 的顺序探测 Everything3_x64.dll。"""
    global _DLL_PATH_CACHE
    if _DLL_PATH_CACHE:
        return _DLL_PATH_CACHE
    candidates = []
    sdk_dir = os.environ.get("EVERYTHING_SDK_DIR")
    if sdk_dir:
        candidates.append(os.path.join(sdk_dir, "Everything3_x64.dll"))
    candidates.extend(_DLL_CANDIDATES)
    for p in candidates:
        if os.path.isfile(p):
            _DLL_PATH_CACHE = p
            return p
    return None


def _get_client():
    """懒加载并缓存 EverythingClient；失败时缓存错误 dict，避免重复抛栈。"""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    try:
        # 注意：everything 顶层包无 __init__.py，类实际位于 everything.client
        from everything.client import EverythingClient
    except Exception:
        _CLIENT = _err(
            "未安装 everything 库，请在后端 Python 环境执行：pip install everything-sdk"
        )
        return _CLIENT

    dll = _find_dll()
    if not dll:
        _CLIENT = _err(
            "未找到 Everything3_x64.dll（Everything SDK v3）。请安装 Everything 1.5+，"
            "或设置环境变量 EVERYTHING_SDK_DIR 指向 DLL 所在目录。"
        )
        return _CLIENT

    try:
        client = EverythingClient(dll)
        client.connect(_SDK_VERSION)
    except Exception as e:
        _CLIENT = _err(
            "连接 Everything 失败：{}。请确认 Everything 1.5+ 正在运行，且 SDK 版本匹配。".format(e)
        )
        return _CLIENT

    _CLIENT = client
    return _CLIENT


def _num(value):
    """把 0 / '' / None 统一视为「不限」，返回 None；否则返回 int。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _build_query(query, name_only=False, ext="", path="", size_min=None,
                 size_max=None, mdate_min="", mdate_max="", hidden=0):
    """把结构化参数翻译成 Everything 查询语法。"""
    parts = []
    q = (query or "").strip()
    if q:
        parts.append("name:" + q if name_only else q)

    if ext:
        e = ext.strip().lstrip(".")
        if e:
            parts.append("ext:" + e)

    if path:
        p = path.strip().rstrip("\\/")
        if p:
            parts.append('path:"' + p + '"')

    smin, smax = _num(size_min), _num(size_max)
    if smin is not None:
        parts.append("size:>={}".format(smin))
    if smax is not None:
        parts.append("size:<={}".format(smax))

    if mdate_min:
        parts.append("dm:>=" + mdate_min.strip())
    if mdate_max:
        parts.append("dm:<=" + mdate_max.strip())

    # hidden: 0=排除隐藏(默认) / 1=仅隐藏 / 2=全部
    if hidden == 1:
        parts.append("attrib:H")
    elif hidden != 2:
        parts.append("!attrib:H")

    return " ".join(parts)


def _serialize(item):
    """把 SearchResult 序列化为 LLM 友好的 dict。"""
    modified = getattr(item, "modified_time", None)
    return {
        "name": getattr(item, "name", "") or "",
        "path": getattr(item, "full_path", "") or "",
        "parent": getattr(item, "parent_path", "") or "",
        "ext": getattr(item, "extension", "") or "",
        "size": int(getattr(item, "size", 0) or 0),
        "is_folder": bool(getattr(item, "is_folder", False)),
        "modified": modified.strftime("%Y-%m-%d %H:%M:%S") if modified else "",
    }


def _do_search(client, query, name_only, ext, path, size_min, size_max,
               mdate_min, mdate_max, hidden, limit, offset):
    search_text = _build_query(
        query, name_only=name_only, ext=ext, path=path,
        size_min=size_min, size_max=size_max,
        mdate_min=mdate_min, mdate_max=mdate_max, hidden=hidden,
    )
    if not search_text.replace("!attrib:H", "").strip():
        return _err("query / ext / path 至少提供一个", query_used=search_text)

    limit = max(1, min(int(limit or 30), 1000))
    offset = max(0, int(offset or 0))

    try:
        # SDK 单次最多取 1000 条，这里取 limit+offset 后再本地切片
        results, total = client.search(
            search_text,
            match_path=False,
            regex=False,
            max_results=min(limit + offset, 1000),
            offset=0,
        )
    except Exception as e:
        return _err("Everything 查询失败：{}".format(e), query_used=search_text)

    items = [_serialize(r) for r in results]
    items = items[offset:offset + limit]

    return {
        "ok": True,
        "action": "search",
        "query_used": search_text,
        "total": int(total or 0),
        "count": len(items),
        "results": items,
    }


def _do_open(client, query):
    q = (query or "").strip()
    if not q:
        return _err("open 模式必须提供 query（应用名）")
    if q.lower().endswith(".exe"):
        q = q[:-4]

    found = _do_search(client, q, True, "exe", "", None, None, "", "", 2, 5, 0)
    if not found.get("ok"):
        return found

    results = found.get("results") or []
    if not results:
        return _err(
            '未找到名为 "{}" 的可执行程序'.format(query),
            query_used=found.get("query_used"),
            hint="可先用 action=search, ext=exe 确认程序名",
        )

    target = results[0]
    target_path = target.get("path", "")
    if not target_path or not os.path.isfile(target_path):
        return _err("目标文件不存在或已被移动：{}".format(target_path))

    try:
        if hasattr(os, "startfile"):
            os.startfile(target_path)  # noqa: S606 — 打开本机文件，非 shell 拼接
        else:
            subprocess.Popen(
                [target_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        return _err("启动失败：{}".format(e), path=target_path)

    return {
        "ok": True,
        "action": "open",
        "opened": target_path,
        "name": target.get("name", ""),
        "message": "已启动：{}".format(target_path),
    }


def run(action: str = "search", query: str = "", name_only: bool = False,
        ext: str = "", path: str = "", size_min: int = 0, size_max: int = 0,
        mdate_min: str = "", mdate_max: str = "", hidden: int = 0,
        limit: int = 30, offset: int = 0) -> dict:
    """Everything 搜索入口。返回 dict；失败时返回 {'ok': False, 'error': '...'}。"""
    action = (action or "search").strip().lower()
    if action not in ("search", "open"):
        return _err('action 必须是 "search" 或 "open"')

    client = _get_client()
    if isinstance(client, dict):        # 初始化失败，缓存的是错误信息
        return client

    if action == "open":
        return _do_open(client, query)

    return _do_search(client, query, name_only, ext, path, size_min, size_max,
                      mdate_min, mdate_max, hidden, limit, offset)