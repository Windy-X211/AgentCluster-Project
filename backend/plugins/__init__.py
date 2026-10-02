"""插件系统 — 遵循 Dify 插件规范（最小集）

AppData/plugins/
  http_request/
    manifest.yaml        ← 插件级元数据（name/label/version/description/author）
    tool.yaml            ← function calling schema（LLM 自动调的参数定义）
    tool.py              ← 真正执行，里面要有 run(**kwargs) 函数
  send_email/
    manifest.yaml
    tool.yaml
    tool.py

manifest.yaml 最小结构：
  version: "0.0.1"
  type: "plugin"
  name: "http_request"           ← kebab-case，与文件夹名一致
  label: { zh_Hans: "...", en_US: "..." }
  author: "AgentCluster"
  description:
    zh_Hans: "..."
    en_US: "..."
  icon: "🌐"
  tags: [...]

tool.yaml 最小结构（每个插件至少一个 tool）：
  identity:
    name: "http_request"
  description: "一句话，LLM 调它前读"
  parameters:
    - name: "method"
      type: string
      required: true
      description: "HTTP 方法"
      enum: ["GET", "POST"]
    - name: "url"
      type: string
      required: true
      description: "目标 URL"

tool.py：标准 run(**kwargs) 返回 dict。

对外 API（保持不变，agent_runtime 和 commands.py 无感知）：
  list_plugins() → dict[name] = {label, description, params, ...}
  run_plugin(name, **kwargs) → result dict
  build_plugin_tool(name) → OpenAI function-calling schema（commands.py 调用）
"""
import importlib.util, sys, re, json
from pathlib import Path
from typing import Any

import core.store as _store


def _plugins_root() -> Path:
    return _store.ROOT / "plugins"


_plugins_root().mkdir(parents=True, exist_ok=True)

PLUGIN_REGISTRY: dict[str, dict] = {}


# ==============================================================
# YAML 简易解析（manifest.yaml 和 tool.yaml 用）
# 递归下降，足够覆盖 Dify manifest/tool/skill 标准结构
# ==============================================================
def _parse_yaml(text: str) -> dict:
    raw_lines = text.splitlines()
    # 去掉注释和空行，保留缩进
    lines: list[tuple[int, str]] = []
    for ln in raw_lines:
        stripped = ln.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(ln) - len(stripped)
        lines.append((indent, ln.rstrip()))

    def _unquote(v: str) -> Any:
        v = v.strip()
        if not v: return ""
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            return v[1:-1]
        if v.lower() == "true": return True
        if v.lower() == "false": return False
        if v.lower() in ("null", "~"): return None
        try:
            if "." in v: return float(v)
            return int(v)
        except ValueError:
            pass
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            if not inner: return []
            return [_unquote(x) for x in inner.split(",")]
        return v

    # 递归：从 i 开始，base_indent 是当前 block 的缩进阈值
    def _parse_block(i: int, base_indent: int) -> tuple[Any, int]:
        # 判断是 list block 还是 dict block
        if i >= len(lines):
            return None, i
        first_indent, first_ln = lines[i]
        stripped_first = first_ln.strip()
        # 缩进不匹配 —— 结束当前 block
        if first_indent < base_indent:
            return None, i

        if stripped_first.startswith("- "):
            # list
            result: list[Any] = []
            item_indent = first_indent
            while i < len(lines):
                cur_indent, cur_ln = lines[i]
                if cur_indent < item_indent:
                    break
                if cur_indent > item_indent:
                    # 嵌套结构（如 list item 里还有子 dict）—— 交给递归处理
                    i += 1
                    continue
                cur_stripped = cur_ln.strip()
                if not cur_stripped.startswith("- "):
                    break
                item_text = cur_stripped[2:].strip()
                if ":" not in item_text:
                    # list item 是纯标量
                    result.append(_unquote(item_text))
                    i += 1
                    continue
                # list item 是 dict
                k, _, rest = item_text.partition(":")
                k = k.strip()
                rest = rest.strip()
                item_dict: dict[str, Any] = {}
                if rest:
                    item_dict[k] = _unquote(rest)
                    i += 1
                else:
                    i += 1
                # 收集缩进更大的行（属于这个 list item 的子 key）
                child_indent = item_indent + 2  # "- xxx:" 之后的子行缩进比 item 大
                while i < len(lines):
                    ci, cl = lines[i]
                    if ci <= item_indent:
                        break
                    cstripped = cl.strip()
                    if cstripped.startswith("- "):
                        # 嵌套 list
                        sub_list, i = _parse_block(i, ci)
                        # sub_list 放到 item_dict 里最后一个 key？不对
                        # 需要追踪 list item 里最后一个 k（空值的那个）
                        # 重新处理：用 item_dict 里的占位 key
                        break
                    # 普通 dict key
                    ck, _, crest = cstripped.partition(":")
                    ck = ck.strip(); crest = crest.strip()
                    if crest:
                        item_dict[ck] = _unquote(crest)
                        i += 1
                    else:
                        i += 1
                        sub_val, i = _parse_block(i, ci + 2)
                        item_dict[ck] = sub_val
                result.append(item_dict)
            return result, i

        # dict
        result: dict[str, Any] = {}
        while i < len(lines):
            cur_indent, cur_ln = lines[i]
            if cur_indent < base_indent:
                break
            if cur_indent > base_indent:
                i += 1
                continue
            cur_stripped = cur_ln.strip()
            if cur_stripped.startswith("- "):
                break  # 不该在 dict block 遇到 list item 同一层级
            if ":" not in cur_stripped:
                break
            k, _, rest = cur_stripped.partition(":")
            k = k.strip(); rest = rest.strip()
            if rest:
                result[k] = _unquote(rest)
                i += 1
            else:
                i += 1
                # 下一行是子 block
                if i < len(lines) and lines[i][0] > base_indent:
                    sub_val, i = _parse_block(i, lines[i][0])
                    result[k] = sub_val
                else:
                    result[k] = {}
        return result, i

    result, _ = _parse_block(0, 0)
    return result if isinstance(result, dict) else {}


# ==============================================================
# manifest.yaml + tool.yaml 解析
# ==============================================================
def _load_yaml_file(fp: Path) -> dict:
    try:
        return _parse_yaml(fp.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[plugins] WARN 解析 {fp} 失败：{e}")
        return {}


def _label_or_default(label: Any) -> str:
    """manifest.yaml 的 label 可能是 {zh_Hans: ..., en_US: ...} 或纯字符串。"""
    if isinstance(label, dict):
        return label.get("zh_Hans") or label.get("en_US") or ""
    return str(label) if label else ""


def _description_or_default(d: Any) -> str:
    if isinstance(d, dict):
        return d.get("zh_Hans") or d.get("en_US") or ""
    return str(d) if d else ""


def _parameter_to_schema(param: dict) -> dict:
    """tool.yaml parameter → OpenAI function-calling 参数 schema。"""
    schema: dict[str, Any] = {
        "type": param.get("type", "string"),
        "description": param.get("description", ""),
    }
    if "enum" in param:
        schema["enum"] = list(param["enum"])
    if "default" in param:
        schema["default"] = param["default"]
    return schema


def _build_function_schema(tool_yaml: dict, manifest: dict) -> dict | None:
    """把 tool.yaml 的 parameters 列表转成 OpenAI function schema。"""
    tool_name = tool_yaml.get("identity", {}).get("name") or manifest.get("name")
    if not tool_name:
        return None
    params_list: list[dict] = tool_yaml.get("parameters", []) or []
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in params_list:
        name = p.get("name")
        if not name: continue
        props[name] = _parameter_to_schema(p)
        if p.get("required"):
            required.append(name)
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": _description_or_default(tool_yaml.get("description", {})) or manifest.get("description", ""),
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


# ==============================================================
# 加载 & 注册
# ==============================================================
def _register_one(plugin_dir: Path) -> None:
    manifest_fp = plugin_dir / "manifest.yaml"
    tool_fp = plugin_dir / "tool.yaml"
    tool_py = plugin_dir / "tool.py"

    if not manifest_fp.exists() or not tool_fp.exists() or not tool_py.exists():
        return

    manifest = _load_yaml_file(manifest_fp)
    tool_yaml = _load_yaml_file(tool_fp)

    name = manifest.get("name") or tool_yaml.get("identity", {}).get("name") or plugin_dir.name
    if not name:
        return

    # 动态导入 tool.py
    spec = importlib.util.spec_from_file_location(f"_plugin_{plugin_dir.name}", tool_py)
    if spec is None or spec.loader is None:
        return
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as e:
        print(f"[plugins] WARN 执行 {tool_py} 失败：{e}")
        return

    if not hasattr(mod, "run"):
        print(f"[plugins] WARN {tool_py} 没有 run() 函数")
        return

    # 从 tool.yaml 拿参数列表（兼容旧 PLUGIN_PARAMS 字段）
    params_list: list[dict] = tool_yaml.get("parameters", []) or []
    param_names: list[str] = [p.get("name") for p in params_list if p.get("name")]
    if not param_names and hasattr(mod, "PLUGIN_PARAMS"):
        param_names = list(getattr(mod, "PLUGIN_PARAMS", []))

    # function calling schema
    fn_schema = _build_function_schema(tool_yaml, manifest)

    PLUGIN_REGISTRY[name] = {
        "label": _label_or_default(manifest.get("label")) or manifest.get("name", plugin_dir.name),
        "description": _description_or_default(manifest.get("description")),
        "version": manifest.get("version", "0.0.1"),
        "author": manifest.get("author", ""),
        "icon": manifest.get("icon", ""),
        "tags": manifest.get("tags", []),
        "params": param_names,              # 兼容旧 API
        "parameters": params_list,           # 完整参数定义（带 required/enum/default）
        "function_schema": fn_schema,        # OpenAI function-calling 直接用
        "run": mod.run,
        "_source_dir": str(plugin_dir),
        "_source_tool_py": str(tool_py),
    }


def load_plugins() -> dict[str, dict]:
    """扫描 AppData/plugins/*/，重建 PLUGIN_REGISTRY。"""
    registry: dict[str, dict] = {}
    if not _plugins_root().exists():
        globals()["PLUGIN_REGISTRY"] = registry
        return registry
    for sub in sorted(_plugins_root().iterdir()):
        if not sub.is_dir() or sub.name.startswith(("_", ".")):
            continue
        _register_one(sub)
    registry = {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_") and kk != "run"}
                for k, v in PLUGIN_REGISTRY.items()}
    globals()["PLUGIN_REGISTRY"] = PLUGIN_REGISTRY  # 保留 run
    return registry


def list_plugins() -> dict:
    """只返回元数据（不含 run 和内部字段），给 /api/commands/plugins 用。"""
    return {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_") and kk != "run" and kk != "function_schema"}
            for k, v in PLUGIN_REGISTRY.items()}


def run_plugin(name: str, **kwargs: Any) -> Any:
    p = PLUGIN_REGISTRY.get(name) or PLUGIN_REGISTRY.get(name.replace("_", "-"))
    if not p:
        return {"error": f"unknown plugin: {name}"}
    try:
        return p["run"](**kwargs)
    except TypeError as e:
        # 参数不对友好提示
        return {"error": f"参数不匹配：{e}（需要的参数：{p.get('params', [])}）"}
    except Exception as e:
        return {"error": str(e)}


def build_plugin_tool(name: str) -> dict | None:
    """commands.py 调用：把插件转换成 OpenAI function calling 的 tool 条目。"""
    p = PLUGIN_REGISTRY.get(name) or PLUGIN_REGISTRY.get(name.replace("_", "-"))
    if not p:
        return None
    if p.get("function_schema"):
        return p["function_schema"]
    # 兜底：用 description + params 粗略生成
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": p.get("description", p.get("label", "")),
            "parameters": {
                "type": "object",
                "properties": {pn: {"type": "string", "description": f"参数 {pn}"} for pn in p.get("params", [])},
                "required": p.get("params", []),
            },
        },
    }


# 启动时加载一次
load_plugins()
