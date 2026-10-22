"""_with_preset 固定参数预设感知机制单元测试。

验证当参数被固定预设时，智能体能从 tool description 中感知到
哪些参数已预设（不暴露预设值），同时参数仍从 schema 中剔除。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.commands import _with_preset


def _make_tool(name="demo", desc="演示工具", props=None, required=None):
    """构造一个模拟的 OpenAI function tool schema。"""
    properties = props or {
        "action": {"type": "string", "description": "操作类型"},
        "token": {"type": "string", "description": "认证令牌"},
    }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or ["action"],
            },
        },
    }


class TestWithPresetFixedNotice:
    """固定参数预设感知测试。"""

    @patch("core.param_presets.get_optional_desc", return_value={})
    @patch("core.param_presets.get_fixed_values", return_value={"token": "ghp_secret123"})
    def test_fixed_param_removed_from_schema(self, _mock_fixed, _mock_opt):
        """固定参数仍从 properties/required 中剔除。"""
        tool = _make_tool(required=["action", "token"])
        result = _with_preset(tool, "demo")
        props = result["function"]["parameters"]["properties"]
        required = result["function"]["parameters"]["required"]
        assert "token" not in props
        assert "token" not in required
        assert "action" in props

    @patch("core.param_presets.get_optional_desc", return_value={})
    @patch("core.param_presets.get_fixed_values", return_value={"token": "ghp_secret123"})
    def test_fixed_param_notice_in_description(self, _mock_fixed, _mock_opt):
        """description 包含固定参数已预设的告知信息。"""
        tool = _make_tool()
        result = _with_preset(tool, "demo")
        desc = result["function"]["description"]
        assert "固定参数" in desc
        assert "已自动注入" in desc
        assert "token" in desc

    @patch("core.param_presets.get_optional_desc", return_value={})
    @patch("core.param_presets.get_fixed_values", return_value={"token": "ghp_secret123"})
    def test_fixed_param_value_not_exposed(self, _mock_fixed, _mock_opt):
        """description 不暴露预设的实际值。"""
        tool = _make_tool()
        result = _with_preset(tool, "demo")
        desc = result["function"]["description"]
        assert "ghp_secret123" not in desc

    @patch("core.param_presets.get_optional_desc", return_value={})
    @patch("core.param_presets.get_fixed_values", return_value={"token": "ghp_secret123"})
    def test_fixed_param_notice_says_auto_effect(self, _mock_fixed, _mock_opt):
        """告知信息说明参数运行时自动生效。"""
        tool = _make_tool()
        result = _with_preset(tool, "demo")
        desc = result["function"]["description"]
        assert "自动生效" in desc

    @patch("core.param_presets.get_optional_desc", return_value={})
    @patch("core.param_presets.get_fixed_values", return_value={"token": "ghp_secret123", "api_key": "sk_xyz"})
    def test_multiple_fixed_params_all_listed(self, _mock_fixed, _mock_opt):
        """多个固定参数都在告知信息中列出。"""
        tool = _make_tool(props={
            "action": {"type": "string", "description": "操作"},
            "token": {"type": "string", "description": "令牌"},
            "api_key": {"type": "string", "description": "API密钥"},
        })
        result = _with_preset(tool, "demo")
        desc = result["function"]["description"]
        assert "token" in desc
        assert "api_key" in desc
        assert "ghp_secret123" not in desc
        assert "sk_xyz" not in desc


class TestWithPresetOptionalUnaffected:
    """可选参数预设行为不受影响。"""

    @patch("core.param_presets.get_optional_desc", return_value={"email": "接收方邮箱"})
    @patch("core.param_presets.get_fixed_values", return_value={})
    def test_optional_param_hint_injected(self, _mock_fixed, _mock_opt):
        """可选参数提示仍注入到参数 description。"""
        tool = _make_tool(props={
            "action": {"type": "string", "description": "操作"},
            "email": {"type": "string", "description": "邮箱"},
        })
        result = _with_preset(tool, "demo")
        email_desc = result["function"]["parameters"]["properties"]["email"]["description"]
        assert "能力参数预设提示" in email_desc
        assert "接收方邮箱" in email_desc


class TestWithPresetFixedAndOptional:
    """固定 + 可选参数同时存在。"""

    @patch("core.param_presets.get_optional_desc", return_value={"email": "接收方邮箱"})
    @patch("core.param_presets.get_fixed_values", return_value={"token": "ghp_secret123"})
    def test_both_fixed_and_optional_in_description(self, _mock_fixed, _mock_opt):
        """固定和可选参数的告知信息都在 description 中。"""
        tool = _make_tool(props={
            "action": {"type": "string", "description": "操作"},
            "token": {"type": "string", "description": "令牌"},
            "email": {"type": "string", "description": "邮箱"},
        })
        result = _with_preset(tool, "demo")
        desc = result["function"]["description"]
        assert "固定参数" in desc
        assert "可选参数" in desc
        assert "ghp_secret123" not in desc
        props = result["function"]["parameters"]["properties"]
        assert "token" not in props
        assert "email" in props


class TestWithPresetNoPreset:
    """无预设时原样返回。"""

    @patch("core.param_presets.get_optional_desc", return_value={})
    @patch("core.param_presets.get_fixed_values", return_value={})
    def test_no_preset_returns_original(self, _mock_fixed, _mock_opt):
        """无预设时 tool 原样返回。"""
        tool = _make_tool()
        result = _with_preset(tool, "demo")
        assert result["function"]["description"] == "演示工具"
        assert "token" in result["function"]["parameters"]["properties"]