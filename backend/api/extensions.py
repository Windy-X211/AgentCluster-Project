"""功能拓展管理路由 —— 挂载在 ``/api/extensions`` 前缀下。

功能拓展（Extension）与「能力管理」里的后端工具插件（AppData/plugins/）不同：
拓展面向**前端/界面层**，用于扩展本程序的界面样式与交互形态（如可视化交互、
精灵悬浮球、临时对话等）。所有拓展资源**从市场下载**到用户数据目录的
``extensions/`` 下，项目源码仓库不再自带任何拓展实现（见 .gitignore）。

目录结构：
  <数据根>/extensions/
    EXTENSION_SPEC.md            ← 代码规范文档（随仓库首发）
    extensions_market.json       ← 旧版市场清单（已废弃，保留兼容兜底）
    installed.json               ← 已安装拓展状态（运行时维护）
    <ext_id>/                    ← 每个已安装拓展一个目录
      manifest.json              ← 拓展元数据
      renderer.js                ← 前端渲染器（viz-renderer 类拓展）
      backend.py                 ← 后端服务（backend-service 类拓展，可选）
      README.md

接口契约：
- ``GET  /api/extensions``                 → 市场清单 + 已安装状态合并视图
- ``GET  /api/extensions/active``          → 已启用拓展清单（含 renderer 代码文本）
- ``POST /api/extensions/install``         → 从市场清单安装指定拓展（走统一 market.install 流程）
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

# —— 存储根（用户数据目录）——
EXT_ROOT = _store.ROOT / "extensions"
INSTALLED_FILE = EXT_ROOT / "installed.json"

# 市场资源目录：独立 git 仓库工作副本（不在项目源码仓库里）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MARKET_ROOT = _PROJECT_ROOT.parent / "market"
MARKET_FILE = MARKET_ROOT / "index.json"
_MARKET_EXT_SRC = MARKET_ROOT / "extensions"

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
    """读取市场清单。

    统一索引 ``market/index.json`` 中 ``kind == "extension"`` 的条目即拓展清单；
    兼容旧版 ``AppData/extensions/extensions_market.json`` 的结构。
    """
    data = _read_json(MARKET_FILE, None)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        out: list[dict] = []
        for it in data["items"]:
            if it.get("kind") != "extension":
                continue
            meta = {k: v for k, v in it.items()
                    if k not in ("kind", "dir", "updated_at")}
            # index 用 ext_type 存 manifest.type，转回市场清单的 type 字段
            meta["type"] = it.get("ext_type") or it.get("type") or "hybrid"
            meta.setdefault("source", "market")   # 统一从市场下载
            out.append(meta)
        return out
    # —— 旧版清单兜底 ——
    legacy = EXT_ROOT / "extensions_market.json"
    data = _read_json(legacy, None)
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
    market_ids = {item.get("id") for item in market}
    for item in market:
        eid = item.get("id", "")
        st = installed.get(eid)
        item["installed"] = bool(st)
        item["enabled"] = bool(st and st.get("enabled"))
    # 把"已安装但不在市场清单里"的拓展（本地手动安装）也塞进 market 数组，
    # 让前端 ExtensionManagement.vue 能渲染。有 manifest 才展示，避免漏兜 installed.json 条目。
    for eid, st in installed.items():
        if eid in market_ids:
            continue
        mf = _read_manifest(eid)
        if not mf:
            continue
        market.append({
            "id": eid,
            "name": mf.get("name") or eid,
            "version": mf.get("version") or st.get("version", "0.0.0"),
            "type": mf.get("type") or "hybrid",
            "author": mf.get("author"),
            "description": mf.get("description", ""),
            "icon": mf.get("icon"),
            "tags": mf.get("tags") or [],
            "frontend_files": mf.get("frontend_files") or [],
            "viz_types": mf.get("viz_types") or [],
            "config_page": mf.get("config_page"),
            "source": "local",
            "installed": True,
            "enabled": bool(st.get("enabled")),
        })
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
    """从市场清单安装指定拓展。入参 ``{id: str}``。

    实际拷贝源是市场目录（独立 git 仓库工作副本），调用前用户应已通过
    市场管理完成同步（或首次使用前自动初始化）。
    """
    _ensure_dirs()
    ext_id = _safe_ext_id(payload.get("id", ""))
    market = _load_market()
    meta = next((m for m in market if m.get("id") == ext_id), None)
    if not meta:
        raise HTTPException(404, f"市场清单中未找到拓展：{ext_id}")

    dest = _ext_dir(ext_id)
    # 统一从市场目录拷贝（市场目录 = 独立 git 仓库工作副本，不在项目源码里）
    src = _MARKET_EXT_SRC / ext_id
    if not src.exists():
        raise HTTPException(
            500,
            f"市场中未找到拓展源码：{src}，请先打开「市场管理」→「同步市场」下载最新资源。"
        )
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest)

    installed = _load_installed()
    installed[ext_id] = {
        "enabled": True,
        "installed_at": datetime.utcnow().isoformat(),
        "version": meta.get("version", "0.0.0"),
        "source": "market",
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


async def mount_extension_backends(app: Any) -> int:
    """把所有已启用拓展的 backend.py 路由挂载到 /api/ext/<ext_id>/ 前缀。

    在 lifespan 中（init_store + auto_install 之后）调用一次：
    - 动态 import AppData/extensions/<id>/<entry>.py，取模块级 ``router``
    - include_router 挂载 /api/ext/<id> 前缀
    - 若模块定义 ``async def on_mount(app)``，在路由挂载后调用（可挂载子应用、
      启动拓展自有后台任务/连接池；MCP 服务端拓展用它托管 Streamable HTTP 子应用）
    - 若模块定义 ``on_unmount()``（同步或异步），进程关闭时由
      :func:`unmount_extension_backends` 统一调用
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
            # 统一注入 /config 路由：manifest 声明 config_page 的拓展自动返回该 HTML 页面。
            # 拓展自己的 backend.py 若已定义 /config 则先注册、优先匹配，此处不覆盖。
            config_page = mf.get("config_page")
            if config_page and "/" not in config_page and not config_page.startswith("."):
                _cfg_fp = _ext_dir(eid) / config_page
                if _cfg_fp.exists() and _cfg_fp.stat().st_size <= _MAX_FILE_BYTES:
                    def _make_cfg(fp):
                        from fastapi.responses import HTMLResponse
                        def _config():
                            try:
                                return HTMLResponse(fp.read_text(encoding="utf-8"))
                            except UnicodeDecodeError:
                                try:
                                    return HTMLResponse(fp.read_text(encoding="gbk"))
                                except Exception:
                                    return HTMLResponse("", status_code=500)
                        return _config
                    router.add_api_route("/config", _make_cfg(_cfg_fp), methods=["GET"])
            app.include_router(router, prefix=f"/api/ext/{eid}", tags=[f"ext-{eid}"])
            _EXT_BACKEND_MODULES[eid] = mod
            mounted += 1
            print(f"[extensions] 已挂载拓展后端：/api/ext/{eid}/")
            on_mount = getattr(mod, "on_mount", None)
            if callable(on_mount):
                ret = on_mount(app)
                if hasattr(ret, "__await__"):
                    await ret
                print(f"[extensions] 拓展 {eid} on_mount 完成")
        except Exception as e:
            print(f"[extensions] 拓展 {eid} 后端加载失败（不阻断启动）：{e}")
    return mounted


async def unmount_extension_backends() -> None:
    """进程关闭时调用各拓展注册的 ``on_unmount``（逆序，异常不阻断）。"""
    for eid in list(reversed(list(_EXT_BACKEND_MODULES.keys()))):
        mod = _EXT_BACKEND_MODULES.get(eid)
        hook = getattr(mod, "on_unmount", None)
        if not callable(hook):
            continue
        try:
            ret = hook()
            if hasattr(ret, "__await__"):
                await ret
            print(f"[extensions] 拓展 {eid} on_unmount 完成")
        except Exception as e:
            print(f"[extensions] 拓展 {eid} on_unmount 失败（忽略）：{e}")