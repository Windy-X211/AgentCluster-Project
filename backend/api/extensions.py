"""功能拓展管理路由 —— 挂载在 ``/api/extensions`` 前缀下。

功能拓展（Extension）与「能力管理」里的后端工具插件（AppData/plugins/）不同：
拓展面向**前端/界面层**，用于扩展本程序的界面样式与交互形态（如可视化交互、
屏幕答疑、打电话、屏幕共享等）。拓展代码保存在 ``AppData/extensions/`` 下。

目录结构：
  AppData/extensions/
    EXTENSION_SPEC.md            ← 代码规范文档（随仓库首发）
    extensions_market.json       ← 内置市场清单（可安装的拓展元数据）
    installed.json               ← 已安装拓展状态（运行时维护）
    _builtin/<ext_id>/           ← 随仓库分发的内置拓展源（安装时拷贝到正式目录）
    <ext_id>/                    ← 每个已安装拓展一个目录
      manifest.json              ← 拓展元数据
      renderer.js                ← 前端渲染器（viz-renderer 类拓展）
      backend.py                 ← 后端服务（backend-service 类拓展，可选）
      README.md

接口契约：
- ``GET  /api/extensions``                 → 市场清单 + 已安装状态合并视图
- ``GET  /api/extensions/active``          → 已启用拓展清单（含 renderer 代码文本）
- ``POST /api/extensions/install``         → 从市场清单安装指定拓展（拷贝到 installed 目录）
- ``POST /api/extensions/{ext_id}/enable`` → 启用
- ``POST /api/extensions/{ext_id}/disable``→ 停用
- ``DELETE /api/extensions/{ext_id}``      → 卸载
- ``GET  /api/extensions/{ext_id}/file``   → 读取拓展内文件内容（前端加载 renderer.js）
"""
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import core.store as _store

router = APIRouter()

# —— 存储根 ——
EXT_ROOT = _store.ROOT / "extensions"
MARKET_FILE = EXT_ROOT / "extensions_market.json"
INSTALLED_FILE = EXT_ROOT / "installed.json"

# 市场清单里指向的「内置拓展源」目录：仓库自带、随程序分发
# 市场清单中 source == "builtin" 的拓展，从该目录拷贝安装
_BUILTIN_SRC = Path(__file__).resolve().parent.parent.parent / "AppData" / "extensions" / "_builtin"

# renderer.js 等前端文件大小上限（防读巨型文件打满内存）
_MAX_FILE_BYTES = 2 * 1024 * 1024


def _ensure_dirs() -> None:
    EXT_ROOT.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[extensions] 读取 {path} 失败：{e}")
    return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_market() -> list[dict]:
    """读取市场清单。若不存在则回退空清单。"""
    data = _read_json(MARKET_FILE, None)
    if isinstance(data, dict) and isinstance(data.get("extensions"), list):
        return data["extensions"]
    if isinstance(data, list):
        return data
    return []


def _load_installed() -> dict[str, dict]:
    """已安装拓展状态：{ ext_id: { enabled, installed_at, version, source } }"""
    data = _read_json(INSTALLED_FILE, None)
    if isinstance(data, dict):
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    return {}


def _save_installed(data: dict[str, dict]) -> None:
    _write_json(INSTALLED_FILE, data)


def _ext_dir(ext_id: str) -> Path:
    return EXT_ROOT / ext_id


def _read_manifest(ext_id: str) -> dict | None:
    mf = _ext_dir(ext_id) / "manifest.json"
    if not mf.exists():
        return None
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[extensions] 拓展 {ext_id} manifest 解析失败：{e}")
        return None


def _safe_ext_id(ext_id: str) -> str:
    """校验 ext_id 仅含安全字符，防路径穿越。"""
    if not ext_id or not all(c.isalnum() or c in ("_", "-") for c in ext_id):
        raise HTTPException(400, f"非法拓展 id：{ext_id}")
    return ext_id


# —— 路由 ——

@router.get("")
def list_extensions():
    """返回市场清单 + 已安装状态合并视图。"""
    _ensure_dirs()
    market = _load_market()
    installed = _load_installed()
    for item in market:
        eid = item.get("id", "")
        st = installed.get(eid)
        item["installed"] = bool(st)
        item["enabled"] = bool(st and st.get("enabled"))
    installed_view: dict[str, dict] = {}
    for eid, st in installed.items():
        mf = _read_manifest(eid)
        installed_view[eid] = {**st, "manifest": mf or {}}
    return {"market": market, "installed": installed_view}


@router.get("/active")
def list_active():
    """返回所有已启用拓展的清单，含 renderer.js 代码文本（前端启动时拉取并注册）。"""
    _ensure_dirs()
    installed = _load_installed()
    out: list[dict] = []
    for eid, st in installed.items():
        if not st.get("enabled"):
            continue
        mf = _read_manifest(eid)
        if not mf:
            continue
        ext_dir = _ext_dir(eid)
        files: dict[str, str] = {}
        for fname in (mf.get("frontend_files") or ["renderer.js"]):
            fp = ext_dir / fname
            if fp.exists() and fp.is_file():
                try:
                    if fp.stat().st_size <= _MAX_FILE_BYTES:
                        files[fname] = fp.read_text(encoding="utf-8")
                except Exception as e:
                    print(f"[extensions] 读取 {eid}/{fname} 失败：{e}")
        out.append({"id": eid, "manifest": mf, "files": files})
    return {"extensions": out}


@router.post("/install")
def install_extension(payload: dict):
    """从市场清单安装指定拓展。入参 ``{id: str}``。"""
    _ensure_dirs()
    ext_id = _safe_ext_id(payload.get("id", ""))
    market = _load_market()
    meta = next((m for m in market if m.get("id") == ext_id), None)
    if not meta:
        raise HTTPException(404, f"市场清单中未找到拓展：{ext_id}")

    dest = _ext_dir(ext_id)
    source = meta.get("source", "builtin")
    if source == "builtin":
        src = _BUILTIN_SRC / ext_id
        if not src.exists():
            raise HTTPException(500, f"内置拓展源不存在：{src}")
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
    elif source == "local":
        if not dest.exists():
            raise HTTPException(400, f"本地拓展目录不存在：{dest}，请先手动放入")
    else:
        raise HTTPException(400, f"不支持的安装来源：{source}")

    installed = _load_installed()
    installed[ext_id] = {
        "enabled": True,
        "installed_at": datetime.utcnow().isoformat(),
        "version": meta.get("version", "0.0.0"),
        "source": source,
    }
    _save_installed(installed)
    return {"ok": True, "id": ext_id, "manifest": _read_manifest(ext_id)}


@router.post("/{ext_id}/enable")
def enable_extension(ext_id: str):
    ext_id = _safe_ext_id(ext_id)
    installed = _load_installed()
    if ext_id not in installed:
        raise HTTPException(404, f"未安装的拓展：{ext_id}")
    installed[ext_id]["enabled"] = True
    _save_installed(installed)
    return {"ok": True, "id": ext_id}


@router.post("/{ext_id}/disable")
def disable_extension(ext_id: str):
    ext_id = _safe_ext_id(ext_id)
    installed = _load_installed()
    if ext_id not in installed:
        raise HTTPException(404, f"未安装的拓展：{ext_id}")
    installed[ext_id]["enabled"] = False
    _save_installed(installed)
    return {"ok": True, "id": ext_id}


@router.delete("/{ext_id}")
def uninstall_extension(ext_id: str):
    ext_id = _safe_ext_id(ext_id)
    installed = _load_installed()
    if ext_id not in installed:
        raise HTTPException(404, f"未安装的拓展：{ext_id}")
    dest = _ext_dir(ext_id)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    installed.pop(ext_id, None)
    _save_installed(installed)
    return {"ok": True, "id": ext_id}


@router.get("/{ext_id}/file")
def read_extension_file(ext_id: str, path: str = Query(...)):
    """读取拓展内指定文件内容（文本）。仅限 manifest.frontend_files 声明的文件。"""
    ext_id = _safe_ext_id(ext_id)
    if not path or "/" in path or "\\" in path or path.startswith("."):
        raise HTTPException(400, f"非法文件名：{path}")
    mf = _read_manifest(ext_id)
    if not mf:
        raise HTTPException(404, f"拓展不存在或 manifest 缺失：{ext_id}")
    allowed = set(mf.get("frontend_files") or ["renderer.js"])
    if path not in allowed:
        raise HTTPException(403, f"未在 manifest.frontend_files 中声明的文件：{path}")
    fp = _ext_dir(ext_id) / path
    if not fp.exists():
        raise HTTPException(404, f"文件不存在：{path}")
    if fp.stat().st_size > _MAX_FILE_BYTES:
        raise HTTPException(413, "文件过大（>2MB）")
    try:
        content = fp.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            content = fp.read_text(encoding="gbk")
        except Exception as e:
            raise HTTPException(400, f"编码无法识别：{e}")
    return {"ok": True, "id": ext_id, "path": path, "content": content}

def auto_install_first_time() -> None:
    """首次启动（installed.json 不存在时）自动安装并启用所有 source=builtin 且
    非 preview 的拓展，让用户开箱即用核心功能（如 visual_interaction）。
    用户仍可在管理页卸载后重装体验完整流程。
    """
    _ensure_dirs()
    if INSTALLED_FILE.exists():
        return
    market = _load_market()
    installed: dict[str, dict] = {}
    for meta in market:
        if meta.get("source") != "builtin" or meta.get("status") == "preview":
            continue
        ext_id = meta.get("id", "")
        if not ext_id:
            continue
        try:
            src = _BUILTIN_SRC / ext_id
            dest = _ext_dir(ext_id)
            if not src.exists():
                continue
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(src, dest)
            installed[ext_id] = {
                "enabled": True,
                "installed_at": datetime.utcnow().isoformat(),
                "version": meta.get("version", "0.0.0"),
                "source": "builtin",
            }
            print(f"[extensions] 首次启动自动安装：{ext_id}")
        except Exception as e:
            print(f"[extensions] 自动安装 {ext_id} 失败：{e}")
    if installed:
        _save_installed(installed)

# —— 拓展 system prompt 注入（通用机制）——
# manifest 声明 system_prompt_file 的已启用拓展，其文件内容会被自动追加到
# 所有智能体的 system prompt，让智能体天然获得该拓展的能力协议（如 viz 输出格式），
# 用户无需在智能体提示词里手动指明。停用/卸载拓展后自动撤销注入。
_SP_CACHE: dict[str, tuple[float, str]] = {}   # { ext_id: (mtime, content) }


def collect_extension_system_prompts() -> list[dict]:
    """收集所有已启用拓展的 system prompt 注入段。

    返回 [{ id, name, content }]，content 为 system_prompt_file 文件文本。
    读取失败/未声明/未启用的拓展自动跳过。文件内容按 mtime 缓存。
    """
    _ensure_dirs()
    installed = _load_installed()
    out: list[dict] = []
    for eid, st in installed.items():
        if not st.get("enabled"):
            continue
        mf = _read_manifest(eid)
        if not mf:
            continue
        fname = mf.get("system_prompt_file")
        if not fname or not isinstance(fname, str):
            continue
        # 防路径穿越：仅允许纯文件名
        if "/" in fname or "\\" in fname or fname.startswith("."):
            continue
        fp = _ext_dir(eid) / fname
        if not fp.exists() or not fp.is_file():
            continue
        try:
            mtime = fp.stat().st_mtime
            cached = _SP_CACHE.get(eid)
            if cached and cached[0] == mtime:
                content = cached[1]
            else:
                if fp.stat().st_size > _MAX_FILE_BYTES:
                    print(f"[extensions] {eid}/{fname} 超过 2MB，跳过注入")
                    continue
                content = fp.read_text(encoding="utf-8")
                _SP_CACHE[eid] = (mtime, content)
            if content.strip():
                out.append({"id": eid, "name": mf.get("name", eid), "content": content.strip()})
        except Exception as e:
            print(f"[extensions] 读取 {eid}/{fname} 失败：{e}")
    return out

# —— 拓展后端服务挂载（backend-service / hybrid 类拓展的 backend.py）——
# manifest 声明 backend_entry 的已启用拓展，其 <backend_entry>.py 中定义的 APIRouter
# 会在启动时被动态加载并挂载到 /api/ext/<ext_id>/ 前缀。
# 挂载只发生在启动时（include_router 是静态注册）；拓展停用后由各路由内部
# 调用 is_extension_enabled 自检并返回 503，前端/宿主据此感知"已停用"。

import importlib.util
import sys as _sys

# 已加载的拓展后端模块 { ext_id: module }
_EXT_BACKEND_MODULES: dict[str, Any] = {}


def is_extension_enabled(ext_id: str) -> bool:
    """查询某拓展当前是否已安装且启用（供拓展后端路由自检）。"""
    installed = _load_installed()
    st = installed.get(ext_id)
    return bool(st and st.get("enabled"))


def _safe_entry(entry: Any) -> str | None:
    """校验 backend_entry：纯模块名/文件名（防路径穿越），返回模块 stem。"""
    if not entry or not isinstance(entry, str):
        return None
    if "/" in entry or "\\" in entry or entry.startswith("."):
        return None
    return entry[:-3] if entry.endswith(".py") else entry


def mount_extension_backends(app: Any) -> int:
    """把所有已启用拓展的 backend.py 路由挂载到 /api/ext/<ext_id>/ 前缀。

    在 lifespan 中（init_store + auto_install 之后）调用一次：
    - 动态 import AppData/extensions/<id>/<entry>.py，取模块级 ``router``
    - include_router 挂载 /api/ext/<id> 前缀
    - 加载失败不阻断启动（打印告警）
    返回成功挂载的拓展数。
    """
    _ensure_dirs()
    installed = _load_installed()
    mounted = 0
    for eid, st in installed.items():
        if not st.get("enabled"):
            continue
        mf = _read_manifest(eid)
        if not mf:
            continue
        stem = _safe_entry(mf.get("backend_entry"))
        if not stem:
            continue
        fp = _ext_dir(eid) / f"{stem}.py"
        if not fp.exists():
            continue
        try:
            mod_name = f"_ext_backend_{eid}"
            spec = importlib.util.spec_from_file_location(mod_name, fp)
            mod = importlib.util.module_from_spec(spec)
            _sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            router = getattr(mod, "router", None)
            if router is None:
                print(f"[extensions] 拓展 {eid} 的 {stem}.py 未定义 router，跳过后端挂载")
                continue
            app.include_router(router, prefix=f"/api/ext/{eid}", tags=[f"ext-{eid}"])
            _EXT_BACKEND_MODULES[eid] = mod
            mounted += 1
            print(f"[extensions] 已挂载拓展后端：/api/ext/{eid}/")
        except Exception as e:
            print(f"[extensions] 拓展 {eid} 后端加载失败（不阻断启动）：{e}")
    return mounted