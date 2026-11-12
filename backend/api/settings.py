from fastapi import APIRouter, HTTPException
from core.store import (
    load, save, load_capability_groups, save_capability_groups,
    load_combos, save_combos,
)

router = APIRouter()


@router.get("")
def get_settings():
    return load("settings")


@router.put("")
def update_settings(data: dict):
    cur = load("settings")
    _validate(data)
    cur.update(data)
    save("settings", cur)
    # 权限设置改了要立刻生效（否则最多延迟 2s 缓存）
    if any(k in data for k in ("permission_mode", "work_scope_mode", "scope_paths")):
        try:
            from core import permissions
            permissions.invalidate_cache()
        except Exception:
            pass
    return cur


def _validate(data: dict) -> None:
    """校验权限设置的取值（其它键保持原有的「来什么存什么」宽松行为）。"""
    if "permission_mode" in data:
        from core.permissions import VALID_PERMISSION_MODES
        if data["permission_mode"] not in VALID_PERMISSION_MODES:
            raise HTTPException(400, f"permission_mode 必须是 {VALID_PERMISSION_MODES} 之一")
    if "work_scope_mode" in data:
        from core.permissions import VALID_SCOPE_MODES
        if data["work_scope_mode"] not in VALID_SCOPE_MODES:
            raise HTTPException(400, f"work_scope_mode 必须是 {VALID_SCOPE_MODES} 之一")
    if "scope_paths" in data:
        paths = data["scope_paths"]
        if not isinstance(paths, list):
            raise HTTPException(400, "scope_paths 必须是路径数组")
        data["scope_paths"] = [str(p).strip() for p in paths if str(p or "").strip()]


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


# ==============================================================
# 能力分组（独立文件存储，不放在 settings.json 中）
# ==============================================================
_VALID_TYPES = ("commands", "skills", "plugins")


@router.get("/capability_groups/{type_}")
def get_capability_groups(type_: str):
    """读取某类型（commands / skills / plugins）的用户自定义分组。"""
    if type_ not in _VALID_TYPES:
        raise HTTPException(400, f"type 必须是 {_VALID_TYPES} 之一")
    return load_capability_groups(type_)


@router.put("/capability_groups/{type_}")
def set_capability_groups(type_: str, data: dict):
    """保存某类型（commands / skills / plugins）的用户自定义分组。

    body：{"组名": ["能力名1", "能力名2"], ...}
    """
    if type_ not in _VALID_TYPES:
        raise HTTPException(400, f"type 必须是 {_VALID_TYPES} 之一")
    if not isinstance(data, dict):
        raise HTTPException(400, "body 必须是 {组名: [能力名, ...]} 的对象")
    save_capability_groups(type_, data)
    return {"ok": True, "type": type_, "groups": data}


# ==============================================================
# 组合技（跨类型能力组合）
# ==============================================================
@router.get("/combos")
def get_combos():
    """读取用户自定义「组合技」（可同时包含命令/技能/插件）。"""
    return load_combos()


@router.put("/combos")
def set_combos(data: dict):
    """保存用户自定义「组合技」。

    body：{"组合名": {"description": str, "members": [{"type","key"}, ...]}, ...}
    """
    if not isinstance(data, dict):
        raise HTTPException(400, "body 必须是 {组合名: {description, members}} 的对象")
    save_combos(data)
    return {"ok": True, "combos": load_combos()}
