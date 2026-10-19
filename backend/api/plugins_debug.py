"""插件调试面板路由 —— 挂载在 ``/api/plugins_debug`` 前缀下。

与存量 ``/api/commands/*`` 完全隔离：不注入 param_presets 固定值、不写全局注册表。

接口契约：
- ``POST /api/plugins_debug/list``
    入参 ``{root_dir: str}`` → 出参 PluginScanResult；``root_dir`` 为空/非目录 → 400。
- ``POST /api/plugins_debug/run``
    入参 ``{root_dir, plugin_name, params}`` → 出参 ExecutionResult（统一 200）；
    插件未找到/加载失败 → 404；参数校验失败 → 400；插件运行异常 → 200 + {ok:false, error}。
"""
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from plugins_debug import execute_plugin_at, scan_plugins_at

router = APIRouter()


class ListIn(BaseModel):
    root_dir: str = ""


class RunIn(BaseModel):
    root_dir: str = ""
    plugin_name: str = ""
    params: dict[str, Any] = {}


@router.post("/list")
def list_plugins_at(data: ListIn):
    """按指定根目录扫描插件，返回清单 + 参数定义 + 工具描述。"""
    try:
        return scan_plugins_at(data.root_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(400, f"无法访问目录：{e}")


@router.post("/run")
def run_plugin_at(data: RunIn):
    """在指定根目录下执行单个插件，传入用户勾选的参数。"""
    try:
        return execute_plugin_at(data.root_dir, data.plugin_name, data.params or {})
    except KeyError as e:
        message = e.args[0] if e.args else str(e)
        raise HTTPException(404, str(message))
    except ValueError as e:
        raise HTTPException(400, str(e))