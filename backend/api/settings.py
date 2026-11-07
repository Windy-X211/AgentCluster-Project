from fastapi import APIRouter, HTTPException
from core.store import load, save

router = APIRouter()


@router.get("")
def get_settings():
    return load("settings")


@router.put("")
def update_settings(data: dict):
    cur = load("settings")
    cur.update(data)
    save("settings", cur)
    return cur


@router.get("/default_workdir")
def get_default_workdir():
    """读取全局默认工作区及其目录状态（设置页回显用，无副作用）。"""
    import os
    cur = load("settings") or {}
    dwd = (cur.get("default_workdir") or "").strip()
    return {
        "default_workdir": dwd,
        "exists": bool(dwd) and os.path.isdir(dwd),
    }


@router.put("/default_workdir")
def set_default_workdir(data: dict):
    """设置全局默认工作区（会话级 / 画布级都没设时生效）。

    - 传空串 = 清除默认工作区（回退到进程 CWD）
    - 非空 = 归一化为绝对路径并自动创建目录（不存在时）
    - 写入后立即失效运行时缓存，使改动当场生效（否则最多延迟 2s）
    """
    raw = (data.get("default_workdir") or "").strip()
    cur = load("settings") or {}
    if not raw:
        cur["default_workdir"] = ""
        save("settings", cur)
        _invalidate_cache()
        return {"default_workdir": "", "exists": False}

    import os
    abs_path = os.path.abspath(os.path.expanduser(raw))
    try:
        os.makedirs(abs_path, exist_ok=True)
    except Exception as e:
        raise HTTPException(400, f"无法创建目录：{e}")
    if not os.path.isdir(abs_path):
        raise HTTPException(400, f"不是有效目录：{abs_path}")

    cur["default_workdir"] = abs_path
    save("settings", cur)
    _invalidate_cache()
    return {"default_workdir": abs_path, "exists": True}


def _invalidate_cache() -> None:
    """让 commands 模块的 default_workdir 缓存立即失效（best-effort）。"""
    try:
        from core.commands import invalidate_default_workdir_cache
        invalidate_default_workdir_cache()
    except Exception:
        pass
