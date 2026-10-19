"""插件调试服务模块 —— 按任意根目录「只读扫描」与「纯净执行」。

与存量 ``plugins`` 模块的区别（目的在于不污染智能体运行时）：
1. 支持按用户指定的任意根目录扫描，而不固定 AppData/plugins；
2. 扫描结果只存入函数局部变量，**不写入全局 PLUGIN_REGISTRY**；
3. tool.py 动态导入使用「目录绝对路径哈希」模块键（``_pdebug_<hash>``），
   与存量 ``_plugin_<dir.name>`` 键空间隔离，避免互相覆盖 ``sys.modules``；
4. 执行不注入 ``core.param_presets`` 固定值，仅传用户勾选的参数；
5. 为每个插件单独暴露工具描述（tool.yaml.description）与插件 name，供调试面板填参。

对外接口：
- ``default_plugins_root() -> str``：面板启动预填的默认插件根目录。
- ``scan_plugins_at(root_dir) -> dict``：扫描指定目录，返回 PluginScanResult。
- ``execute_plugin_at(root_dir, plugin_name, params) -> dict``：执行单个插件，返回 ExecutionResult。

异常约定（由路由层映射为 HTTP 状态码）：
- 目录不存在 / 参数不合法 → ``ValueError``（→ 400）
- 插件未找到 / 插件加载失败 → ``KeyError``（→ 404）
- 插件 run() 抛出的异常不向外抛，统一收敛为 ExecutionResult{ok:false, error}（→ 200）
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import core.store as _store
from plugins import _description_or_default, _label_or_default, _parse_yaml

# 扫描跳过前缀（与存量 load_plugins 保持一致）
_SKIP_PREFIXES = ("_", ".")
# 单个插件参数数量上限（spec 4.1 性能约束）、插件数量上限
_MAX_PARAMS = 30
_MAX_PLUGINS = 50
# 插件三件套
_REQUIRED_FILES = ("manifest.yaml", "tool.yaml", "tool.py")

# 隔离的 run 函数缓存：key=隔离模块键，value=(tool.py 内容哈希, run 函数)。
# 文件未变时复用旧模块对象，保留插件的运行态（与存量 _PLUGIN_SOURCE_HASH 同策略）。
_DEBUG_MODULE_CACHE: dict[str, tuple[str, Any]] = {}


def default_plugins_root() -> str:
    """返回面板启动时预填的默认插件根目录（AppData/plugins 绝对路径）。"""
    return str((_store.ROOT / "plugins").resolve())


def _debug_module_key(plugin_dir: Path) -> str:
    """按目录绝对路径生成隔离模块键，避免与存量 ``_plugin_<name>`` 相互覆盖。"""
    digest = hashlib.sha1(str(plugin_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"_pdebug_{digest}"


def _file_hash(fp: Path) -> str:
    try:
        return hashlib.sha256(fp.read_bytes()).hexdigest()
    except Exception:
        return ""


def _read_yaml(fp: Path) -> tuple[dict, str | None]:
    """读取并解析 YAML，返回 (数据, 错误信息)。错误信息为 None 表示成功。"""
    try:
        text = fp.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return {}, f"{fp.name} 读取失败：{e}"
    try:
        data = _parse_yaml(text)
    except Exception as e:  # noqa: BLE001
        return {}, f"{fp.name} 解析失败：{e}"
    if not isinstance(data, dict):
        return {}, f"{fp.name} 格式异常"
    return data, None


def _import_run_fn(plugin_dir: Path, tool_py: Path) -> tuple[Any, str | None]:
    """动态导入 tool.py 并返回 run 函数（使用隔离模块键）。失败返回 (None, 原因)。"""
    key = _debug_module_key(plugin_dir)
    digest = _file_hash(tool_py)
    cached = _DEBUG_MODULE_CACHE.get(key)
    if cached is not None and digest and cached[0] == digest:
        return cached[1], None

    spec = importlib.util.spec_from_file_location(key, tool_py)
    if spec is None or spec.loader is None:
        return None, "无法加载 tool.py（模块规格无效）"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        return None, f"tool.py 执行失败：{e}"
    run_fn = getattr(mod, "run", None)
    if not callable(run_fn):
        return None, "tool.py 缺少 run() 函数"
    _DEBUG_MODULE_CACHE[key] = (digest, run_fn)
    return run_fn, None


def _parse_one_for_debug(plugin_dir: Path) -> dict:
    """解析单个子目录。

    成功返回 DebugPluginEntry（含内部 ``ok=True`` 与 ``_run`` 字段）；
    失败返回 FailedEntry（``ok=False`` + ``dir_name`` + ``load_error``）。
    绝不返回 None，也绝不向外抛异常（单插件失败须隔离）。
    """
    dir_name = plugin_dir.name

    missing = [f for f in _REQUIRED_FILES if not (plugin_dir / f).is_file()]
    if missing:
        return {"ok": False, "dir_name": dir_name, "load_error": f"缺少文件：{', '.join(missing)}"}

    manifest, manifest_err = _read_yaml(plugin_dir / "manifest.yaml")
    if manifest_err:
        return {"ok": False, "dir_name": dir_name, "load_error": manifest_err}
    tool_yaml, tool_err = _read_yaml(plugin_dir / "tool.yaml")
    if tool_err:
        return {"ok": False, "dir_name": dir_name, "load_error": tool_err}

    identity = tool_yaml.get("identity")
    identity_name = identity.get("name") if isinstance(identity, dict) else None
    name = str(manifest.get("name") or identity_name or dir_name)

    run_fn, run_err = _import_run_fn(plugin_dir, plugin_dir / "tool.py")
    if run_err:
        return {"ok": False, "dir_name": dir_name, "load_error": run_err}

    raw_params = tool_yaml.get("parameters")
    parameters = [p for p in raw_params if isinstance(p, dict)] if isinstance(raw_params, list) else []
    parameters = parameters[:_MAX_PARAMS]

    raw_tags = manifest.get("tags")
    tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []

    return {
        "ok": True,
        "name": name,
        "dir_name": dir_name,
        "label": _label_or_default(manifest.get("label")) or name,
        "description": _description_or_default(manifest.get("description")),
        "tool_description": _description_or_default(tool_yaml.get("description")),
        "version": str(manifest.get("version") or "0.0.1"),
        "author": str(manifest.get("author") or ""),
        "icon": str(manifest.get("icon") or ""),
        "tags": tags,
        "parameters": parameters,
        "_run": run_fn,
    }


def _resolve_dir(root_dir: str) -> Path:
    """解析并校验插件根目录。空 → ValueError；相对路径按项目根解析。"""
    raw = (root_dir or "").strip()
    if not raw:
        raise ValueError("root_dir required")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        root = _store.ROOT.parent / raw
    root = root.resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"目录不存在：{raw}")
    return root


def scan_plugins_at(root_dir: str) -> dict:
    """扫描 ``root_dir`` 下所有直接子目录，返回 PluginScanResult。

    只读快照：结果仅存于函数局部变量，不写入全局注册表。
    单个子目录解析失败不影响其他子目录（失败项进入 ``failed``）。
    """
    root = _resolve_dir(root_dir)
    result: dict[str, Any] = {"root_dir": str(root), "plugins": [], "failed": [], "error": None}

    try:
        subs = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except PermissionError:
        result["error"] = "无法读取该目录"
        print(f"[plugins_debug] 扫描 {root} 失败：权限不足")
        return result
    except OSError as e:
        result["error"] = f"无法读取该目录：{e}"
        print(f"[plugins_debug] 扫描 {root} 失败：{e}")
        return result

    print(f"[plugins_debug] 开始扫描 {root}")
    for sub in subs:
        if len(result["plugins"]) >= _MAX_PLUGINS:
            break
        if not sub.is_dir() or sub.name.startswith(_SKIP_PREFIXES):
            continue
        item = _parse_one_for_debug(sub)
        if item.pop("ok", False):
            item.pop("_run", None)
            result["plugins"].append(item)
        else:
            item.pop("_run", None)
            result["failed"].append(item)

    print(f"[plugins_debug] 扫描完成 {root}：成功 {len(result['plugins'])}，失败 {len(result['failed'])}")
    return result


def _coerce_value(name: str, value: Any, ptype: str) -> Any:
    """将前端传入的值转换为目标类型，失败时抛出 ValueError。"""
    if value is None:
        return None
    if ptype == "string":
        return value if isinstance(value, str) else str(value)
    if ptype == "integer":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"参数 {name} 须为整数")
    if ptype == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"参数 {name} 须为数字")
    if ptype == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "y", "on"):
            return True
        if text in ("false", "0", "no", "n", "off", ""):
            return False
        raise ValueError(f"参数 {name} 须为布尔值")
    if ptype in ("object", "array"):
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(f"参数 {name} 的 JSON 格式错误")
    # 未知类型 → 降级为字符串
    return value if isinstance(value, str) else str(value)


def _coerce_params(params: dict, definitions: list[dict]) -> dict:
    """按参数定义转换/校验用户传入的 params；未知键抛出 ValueError。"""
    by_name = {d.get("name"): d for d in definitions if isinstance(d, dict) and d.get("name")}
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        definition = by_name.get(key)
        if definition is None:
            raise ValueError(f"未知参数：{key}")
        ptype = str(definition.get("type") or "string")
        out[key] = _coerce_value(key, value, ptype)
    return out


def _locate_entry(root: Path, plugin_name: str) -> dict:
    """在 root 下定位插件条目。优先按目录名，回退按 manifest.name 匹配。"""
    name = (plugin_name or "").strip()
    if not name:
        raise KeyError("缺少 plugin_name")

    direct = root / name
    if direct.is_dir():
        entry = _parse_one_for_debug(direct)
        if not entry.get("ok"):
            raise KeyError(f"插件 {name} 加载失败：{entry.get('load_error')}")
        return entry

    for sub in root.iterdir():
        if not sub.is_dir() or sub.name.startswith(_SKIP_PREFIXES):
            continue
        candidate = _parse_one_for_debug(sub)
        if candidate.get("ok") and candidate.get("name") == name:
            return candidate
    raise KeyError(f"插件 {name} 未在 {root} 下找到")


def execute_plugin_at(root_dir: str, plugin_name: str, params: dict) -> dict:
    """在 ``root_dir`` 下定位并执行插件，返回 ExecutionResult。

    - 不注入 ``core.param_presets`` 固定值，``params`` 仅含用户勾选的参数；
    - 参数类型转换失败 → ValueError（→ 400）；
    - 插件未找到/加载失败 → KeyError（→ 404）；
    - run() 抛出的任何异常收敛为 ExecutionResult{ok:false, error}（→ 200）。
    """
    root = _resolve_dir(root_dir)
    entry = _locate_entry(root, plugin_name)
    name = entry.get("name") or plugin_name

    coerced = _coerce_params(params or {}, entry.get("parameters") or [])
    run_fn = entry.get("_run")
    if not callable(run_fn):
        raise KeyError(f"插件 {name} 缺少 run() 函数")

    start = time.perf_counter()
    try:
        data = run_fn(**coerced)
        duration_ms = int((time.perf_counter() - start) * 1000)
        if isinstance(data, dict) and data.get("error"):
            err = str(data.get("error"))
            print(f"[plugins_debug] 执行完成 {name}：失败（{err}） {duration_ms}ms")
            return {"ok": False, "data": None, "error": err, "duration_ms": duration_ms}
        print(f"[plugins_debug] 执行完成 {name}：成功 {duration_ms}ms")
        return {"ok": True, "data": data, "error": None, "duration_ms": duration_ms}
    except TypeError as e:
        duration_ms = int((time.perf_counter() - start) * 1000)
        print(f"[plugins_debug] 执行异常 {name}：参数不匹配（{e}）")
        return {"ok": False, "data": None, "error": f"参数不匹配：{e}", "duration_ms": duration_ms}
    except Exception as e:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - start) * 1000)
        print(f"[plugins_debug] 执行异常 {name}：{e}")
        return {"ok": False, "data": None, "error": str(e), "duration_ms": duration_ms}