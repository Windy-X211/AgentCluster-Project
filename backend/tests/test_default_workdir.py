"""默认工作区（settings.default_workdir）回归测试。

背景：default_workdir 字段在数据模型里早就存在，但存在两个真实缺陷：
  1) core/commands.py::_effective_workdir 里缓存写成 `_DEFAULT_WORKDIR_CACHE = {...}`
     （重新绑定名字）→ Python 视为局部变量 → 读取抛 UnboundLocalError →
     被 except Exception 静默吞掉 → 兜底恒为 None，字段形同虚设。
  2) agent_runtime 只在画布有 workdir 时注入 WORKDIR_CTX，独立对话模式
     完全不会用到 settings.default_workdir。

这里锁住「设置 → 运行时生效」的完整链路，避免以后再被悄悄改坏。
"""
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """把 store 的数据根目录重定向到临时目录，避免污染真实用户数据。

    注意：core.store.load/save 在**调用时**才读取模块级 ROOT，
    因此只要改 core.store.ROOT，所有已 import 该函数的模块
    （api.settings / core.commands 等）都会跟着重定向。
    """
    import core.store as store

    monkeypatch.setattr(store, "ROOT", tmp_path)

    # commands 模块内部带 default_workdir 缓存，每次测试前后都清干净
    import core.commands as commands
    commands.invalidate_default_workdir_cache()
    yield
    commands.invalidate_default_workdir_cache()


def test_effective_workdir_falls_back_to_settings(tmp_path):
    """核心回归：default_workdir 生效（早期版本因 UnboundLocalError 恒返回 None）。"""
    from core.commands import _effective_workdir, invalidate_default_workdir_cache
    from core.store import load, save

    target = tmp_path / "default_ws"
    target.mkdir()

    s = load("settings") or {}
    s["default_workdir"] = str(target)
    save("settings", s)
    invalidate_default_workdir_cache()

    assert _effective_workdir() == str(target)


def test_effective_workdir_cache_hit_and_invalidate(tmp_path):
    """缓存命中仍然返回正确值；主动失效后重新读盘仍正确。"""
    from core.commands import _effective_workdir, invalidate_default_workdir_cache
    from core.store import load, save

    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()

    s = load("settings") or {}
    s["default_workdir"] = str(a)
    save("settings", s)
    invalidate_default_workdir_cache()
    assert _effective_workdir() == str(a)

    # 二次调用走缓存分支
    assert _effective_workdir() == str(a)

    # 改设置 + 失效缓存 → 立即读到新值
    s = load("settings") or {}
    s["default_workdir"] = str(b)
    save("settings", s)
    invalidate_default_workdir_cache()
    assert _effective_workdir() == str(b)


def test_effective_workdir_none_when_unset():
    """未设置 default_workdir 时返回 None（回退进程 CWD 由上层决定）。"""
    from core.commands import _effective_workdir, invalidate_default_workdir_cache
    from core.store import load, save

    s = load("settings") or {}
    s["default_workdir"] = ""
    save("settings", s)
    invalidate_default_workdir_cache()

    assert _effective_workdir() is None


def test_effective_workdir_ignores_missing_dir(tmp_path):
    """目录不存在 → 视为未设置（不返回脏路径）。"""
    from core.commands import _effective_workdir, invalidate_default_workdir_cache
    from core.store import load, save

    s = load("settings") or {}
    s["default_workdir"] = str(tmp_path / "does_not_exist")
    save("settings", s)
    invalidate_default_workdir_cache()

    assert _effective_workdir() is None


def test_get_default_workdir_reads_settings(tmp_path):
    """get_default_workdir 只读 settings（不受 WORKDIR_CTX 影响）。"""
    from core.commands import get_default_workdir
    from core.store import load, save

    target = tmp_path / "gw"
    target.mkdir()
    s = load("settings") or {}
    s["default_workdir"] = str(target)
    save("settings", s)

    assert get_default_workdir() == str(target)


def test_set_default_workdir_endpoint_normalizes_and_creates(tmp_path):
    """设置接口：归一化为绝对路径 + 自动创建目录。"""
    from api.settings import set_default_workdir
    from core.store import load

    new_dir = tmp_path / "nested" / "created"
    res = set_default_workdir({"default_workdir": str(new_dir)})

    assert res["exists"] is True
    assert os.path.isabs(res["default_workdir"])
    assert Path(res["default_workdir"]).is_dir()
    assert load("settings")["default_workdir"] == res["default_workdir"]


def test_set_default_workdir_endpoint_clears(tmp_path):
    """传空串 = 清除默认工作区。"""
    from api.settings import set_default_workdir
    from core.store import load, save

    target = tmp_path / "to_clear"
    target.mkdir()
    s = load("settings") or {}
    s["default_workdir"] = str(target)
    save("settings", s)

    res = set_default_workdir({"default_workdir": ""})
    assert res["default_workdir"] == ""
    assert res["exists"] is False
    assert load("settings")["default_workdir"] == ""


def test_set_default_workdir_endpoint_invalidates_cache(tmp_path):
    """设置后立即生效（缓存被失效，无需等 2s TTL）。"""
    from api.settings import set_default_workdir
    from core.commands import _effective_workdir, invalidate_default_workdir_cache
    from core.store import load, save

    s = load("settings") or {}
    s["default_workdir"] = ""
    save("settings", s)
    invalidate_default_workdir_cache()
    assert _effective_workdir() is None

    target = tmp_path / "immediate"
    set_default_workdir({"default_workdir": str(target)})

    assert _effective_workdir() == str(target)


def test_get_default_workdir_endpoint_reports_existence(tmp_path):
    """回显接口：返回路径 + 目录是否存在。"""
    from api.settings import get_default_workdir
    from core.store import load, save

    s = load("settings") or {}
    s["default_workdir"] = str(tmp_path / "nope")
    save("settings", s)
    res = get_default_workdir()
    assert res["exists"] is False

    target = tmp_path / "yes"
    target.mkdir()
    s = load("settings") or {}
    s["default_workdir"] = str(target)
    save("settings", s)
    res = get_default_workdir()
    assert res["exists"] is True


def test_agent_runtime_falls_back_to_default_workdir(tmp_path):
    """agent_reply 在画布/会话都没设 workdir 时，回落到默认工作区并注入 WORKDIR_CTX。"""
    import asyncio

    from core.agent_runtime import agent_reply
    from core.store import load, save

    target = tmp_path / "runtime_ws"
    target.mkdir()
    s = load("settings") or {}
    s["default_workdir"] = str(target)
    save("settings", s)

    captured = {}

    import core.agent_runtime as ar

    real_chat = ar.chat

    async def fake_chat(*args, **kwargs):
        """替换 LLM 调用：记录当前 WORKDIR_CTX，然后产出一句文本收尾。"""
        captured["workdir"] = ar.WORKDIR_CTX.get()
        yield "text", "ok"
        yield "done", ""

    ar.chat = fake_chat
    try:
        async def _run():
            async for _ in agent_reply(
                conversation_id=999999,
                agent={"id": 1, "name": "T", "system_prompt": "x", "commands": []},
                user_input="hi",
                mode="independent",
                canvas=None,
            ):
                pass
        asyncio.run(_run())
    finally:
        ar.chat = real_chat

    assert captured.get("workdir") == str(target)
