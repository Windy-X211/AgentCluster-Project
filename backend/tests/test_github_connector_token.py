"""github_connector 插件令牌预应用机制单元测试。

验证 run() 入口在 kwargs 携带 token 且 action 非 set_token 时，
自动将 token 写入模块级状态，使能力参数预设的 token 对所有 action 生效。
"""
from __future__ import annotations

import sys
import importlib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# 将插件目录加入 sys.path 以导入 tool 模块
_PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "AppData" / "plugins" / "github_connector"
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import tool as gh


@pytest.fixture(autouse=True)
def _reset_token():
    """每个测试前后重置模块级令牌，避免测试间状态泄漏。"""
    gh._set_token_internal(None)
    yield
    gh._set_token_internal(None)


def _mock_response(status_code=200, json_body=None, headers=None):
    """构造一个模拟的 httpx.Response。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = ""
    resp.headers = headers or {
        "X-RateLimit-Limit": "5000",
        "X-RateLimit-Remaining": "4999",
        "X-RateLimit-Reset": "9999999999",
    }
    return resp


class TestPresetTokenAutoApply:
    """预应用 token 核心逻辑测试。"""

    def test_preset_token_applied_for_non_set_token_action(self):
        """kwargs 携带 token 且 action 非 set_token → 模块级 _token 被设置。"""
        with patch.object(gh.httpx, "request", return_value=_mock_response(
            json_body={"total_count": 0, "items": []}
        )):
            result = gh.run(action="search_repos", token="ghp_test123456", query="python")
        assert result["ok"] is True
        assert gh._get_token() == "ghp_test123456"

    def test_no_token_no_change(self):
        """kwargs 不携带 token → 不改变模块级 _token（保持 None）。"""
        with patch.object(gh.httpx, "request", return_value=_mock_response(
            json_body={"total_count": 0, "items": []}
        )):
            gh.run(action="search_repos", query="python")
        assert gh._get_token() is None

    def test_existing_token_preserved_when_no_token_in_kwargs(self):
        """已有令牌且 kwargs 不携带 token → 保持原有令牌。"""
        gh._set_token_internal("ghp_existing123")
        with patch.object(gh.httpx, "request", return_value=_mock_response(
            json_body={"total_count": 0, "items": []}
        )):
            gh.run(action="search_repos", query="python")
        assert gh._get_token() == "ghp_existing123"

    def test_preset_token_updates_existing(self):
        """kwargs 携带新 token 且与当前不同 → 更新模块级 _token。"""
        gh._set_token_internal("ghp_old_token123")
        with patch.object(gh.httpx, "request", return_value=_mock_response(
            json_body={"total_count": 0, "items": []}
        )):
            gh.run(action="search_repos", token="ghp_new_token456", query="python")
        assert gh._get_token() == "ghp_new_token456"

    def test_same_token_not_re_set(self):
        """kwargs 携带的 token 与当前相同 → 不重复设置（无副作用）。"""
        gh._set_token_internal("ghp_same_token1")
        with patch.object(gh.httpx, "request", return_value=_mock_response(
            json_body={"total_count": 0, "items": []}
        )):
            gh.run(action="search_repos", token="ghp_same_token1", query="python")
        assert gh._get_token() == "ghp_same_token1"

    def test_set_token_action_not_affected(self):
        """set_token action 本身行为不受预应用逻辑影响。"""
        result = gh.run(action="set_token", token="ghp_explicit_set123")
        assert result["ok"] is True
        assert result["token_masked"] == "ghp_****t123"
        assert gh._get_token() == "ghp_explicit_set123"

    def test_empty_string_token_ignored(self):
        """token 为空字符串 → 不设置。"""
        with patch.object(gh.httpx, "request", return_value=_mock_response(
            json_body={"total_count": 0, "items": []}
        )):
            gh.run(action="search_repos", token="", query="python")
        assert gh._get_token() is None

    def test_whitespace_token_stripped_and_set(self):
        """token 含空白 → strip 后设置。"""
        with patch.object(gh.httpx, "request", return_value=_mock_response(
            json_body={"total_count": 0, "items": []}
        )):
            gh.run(action="search_repos", token="  ghp_stripped_token  ", query="python")
        assert gh._get_token() == "ghp_stripped_token"

    def test_check_auth_uses_preset_token(self):
        """check_auth action 携带预设 token → 认证守卫通过，不再返回 AUTH_REQUIRED。"""
        with patch.object(gh.httpx, "request", return_value=_mock_response(
            json_body={"login": "testuser"}
        )):
            result = gh.run(action="check_auth", token="ghp_auth_check_123")
        assert result["ok"] is True
        assert result["data"]["authenticated"] is True
        assert result["data"]["username"] == "testuser"

    def test_check_auth_without_token_returns_auth_required(self):
        """check_auth action 不携带 token → 返回 AUTH_REQUIRED。"""
        result = gh.run(action="check_auth")
        assert result["ok"] is False
        assert result["code"] == "AUTH_REQUIRED"

    def test_fine_grained_token_preset(self):
        """fine-grained token (github_pat_) 预设也能自动应用。"""
        with patch.object(gh.httpx, "request", return_value=_mock_response(
            json_body={"total_count": 0, "items": []}
        )):
            gh.run(action="search_repos", token="github_pat_test_fine_grained", query="python")
        assert gh._get_token() == "github_pat_test_fine_grained"