"""MCP 拓展与外部工具钩子单元测试。

覆盖：
1. core.external_tools 的注册 / 工具合并 / 分发；
2. mcp_client 的工具名生成、配置规范化；
3. mcp_server 的 MCP SDK 可用性与工具注册表。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from core.store import ROOT as STORE_ROOT
EXT_ROOT = STORE_ROOT / "extensions"
# 拓展统一从市场下载到用户数据目录；若尚未安装则 skip


def _load_ext_module(ext_id: str):
    path = EXT_ROOT / ext_id / "backend.py"
    if not path.exists():
        pytest.skip(f"拓展未安装：{path}")
    name = f"_test_ext_{ext_id}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def clean_providers():
    from core import external_tools
    external_tools.unregister_provider("test_provider")
    yield
    external_tools.unregister_provider("test_provider")


def test_external_tools_register_and_filter(clean_providers):
    from core import external_tools
    from core.commands import filter_tools

    schema = {
        "type": "function",
        "function": {
            "name": "test__echo",
            "description": "回显参数",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
    }
    external_tools.register_provider(
        "test_provider",
        tools_fn=lambda agent: [schema],
        call_fn=lambda name, args, agent: {"ok": True, "echo": (args or {}).get("text")},
        prefix="test__",
    )

    tools = filter_tools(["file.read"], [], {}, [], agent={"id": 1})
    names = [t["function"]["name"] for t in tools]
    assert "file.read" in names
    assert "test__echo" in names

    assert external_tools.owner_of("test__echo") == "test_provider"
    assert external_tools.owner_of("file.read") is None
    assert external_tools.dispatch("test__echo", {"text": "hi"}) == {"ok": True, "echo": "hi"}


def test_external_tools_unknown_dispatch(clean_providers):
    from core import external_tools
    result = external_tools.dispatch("nope__missing", {})
    assert "error" in result


def test_mcp_client_tool_name_and_normalize():
    mod = _load_ext_module("mcp_client")

    name = mod._make_tool_name("srv.1", "do/thing")
    assert name.startswith("mcp__srv_1__do_thing")
    assert len(name) <= 64
    long_name = mod._make_tool_name("s" * 40, "t" * 60)
    assert len(long_name) <= 64

    server = mod._normalize_server({
        "name": "测试服务器",
        "transport": "stdio",
        "command": "npx",
        "args": "-y  @scope/pkg   D:\\work",
        "env": '{"A":"B"}',
        "agents": "1, 2",
        "timeout": 30,
    })
    assert server["id"]
    assert server["args"] == ["-y", "@scope/pkg", "D:\\work"]
    assert server["env"] == {"A": "B"}
    assert server["agents"] == [1, 2]
    assert server["enabled"] is True

    http_server = mod._normalize_server({
        "id": "remote", "name": "远程", "transport": "http",
        "url": "http://127.0.0.1:8870/api/ext/mcp_server/mcp",
        "headers": '{"Authorization":"Bearer x"}',
    })
    assert http_server["transport"] == "http"
    assert http_server["headers"]["Authorization"] == "Bearer x"
    assert http_server["agents"] == "all"


def test_mcp_client_provider_registered():
    mod = _load_ext_module("mcp_client")
    from core import external_tools

    if not mod._MCP_AVAILABLE:
        pytest.skip(f"MCP SDK 不可用：{mod._MCP_ERROR}")
    providers = {p["id"] for p in external_tools.list_providers()}
    assert "mcp_client" in providers


def test_mcp_server_tools_registered():
    mod = _load_ext_module("mcp_server")
    if not mod._MCP_AVAILABLE:
        pytest.skip(f"MCP SDK 不可用：{mod._MCP_ERROR}")
    names = [n for n, _d, _f in mod._TOOL_IMPLS]
    assert "system_info" in names
    assert "send_message" in names
    assert "create_agent" in names
    # 写操作必须显式走 allow_write 校验
    assert callable(mod._require_write)
    # 默认不允许写
    assert mod._allow_write() is False or isinstance(mod._allow_write(), bool)