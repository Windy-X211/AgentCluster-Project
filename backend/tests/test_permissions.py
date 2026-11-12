"""权限设置（操作权限模式 + 工作范围）回归测试。

覆盖 backend/core/permissions.py 的三块：
  1) 默认值与设置读取（auto / workspace / scope_paths）
  2) 工作范围校验（workspace / custom / global）在 file.* 与 cmd.run 上的落地
  3) 手动确认请求的登记 → 等待 → 允许 / 拒绝 / 超时 / 停止取消 全链路
"""
import asyncio
import sys
import threading
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """把 store 数据根重定向到临时目录（与 test_default_workdir 同套路）。"""
    import core.store as store
    import core.commands as commands
    from core import permissions

    monkeypatch.setattr(store, "ROOT", tmp_path)
    commands.invalidate_default_workdir_cache()
    permissions.invalidate_cache()
    # 保证每个用例都从干净的默认值起步
    s = store.load("settings") or {}
    s.update({"permission_mode": "auto", "work_scope_mode": "workspace", "scope_paths": []})
    store.save("settings", s)
    permissions.invalidate_cache()
    yield
    commands.invalidate_default_workdir_cache()
    permissions.invalidate_cache()


def _set(**kw):
    """写入设置并立即失效两层缓存（permissions 2s TTL + default_workdir 2s TTL）。"""
    import core.commands as commands
    from core import permissions
    from core.store import load, save

    s = load("settings") or {}
    s.update(kw)
    save("settings", s)
    permissions.invalidate_cache()
    commands.invalidate_default_workdir_cache()


# ==============================================================
# 默认值
# ==============================================================
def test_default_settings_have_permission_keys():
    from core.store import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["permission_mode"] == "auto"
    assert DEFAULT_SETTINGS["work_scope_mode"] == "workspace"
    assert DEFAULT_SETTINGS["scope_paths"] == []


def test_get_settings_reads_and_normalizes(tmp_path):
    from core import permissions

    assert permissions.get_settings()["permission_mode"] == "auto"
    _set(permission_mode="manual", work_scope_mode="custom", scope_paths=["  a  ", "", 1])
    cfg = permissions.get_settings()
    assert cfg["permission_mode"] == "manual"
    assert cfg["work_scope_mode"] == "custom"
    assert cfg["scope_paths"] == ["a"]  # 非字符串项被剔除

    _set(permission_mode="不存在的模式")
    assert permissions.get_settings()["permission_mode"] == "auto"  # 非法值回退默认


# ==============================================================
# 危险操作判定
# ==============================================================
def test_is_dangerous():
    from core import permissions

    assert permissions.is_dangerous("file.write")
    assert permissions.is_dangerous("file.delete")
    assert permissions.is_dangerous("cmd.run")
    assert permissions.is_dangerous("cluster.delete_agent")
    # 只读命令不算危险
    assert not permissions.is_dangerous("file.read")
    assert not permissions.is_dangerous("file.search")
    assert not permissions.is_dangerous("cluster.get_agent")
    # 工作流工具（plan/spec/goal）只改工作流状态
    assert not permissions.is_dangerous("plan.update")
    # 非命令（插件 / 外部工具）无法静态判断副作用 → 按危险处理
    assert permissions.is_dangerous("some_plugin.do_thing")


# ==============================================================
# 工作范围
# ==============================================================
def test_scope_workspace_blocks_outside_and_allows_inside(tmp_path):
    from core import permissions
    from core.commands import cmd_file_write

    ws = tmp_path / "ws"
    ws.mkdir()
    _set(default_workdir=str(ws), work_scope_mode="workspace")

    # 工作区内：放行
    inside = ws / "ok.txt"
    assert cmd_file_write(str(inside), "hi").get("ok") is True
    assert inside.exists()

    # 工作区外：拒绝，且不落盘
    outside = tmp_path / "outside.txt"
    res = cmd_file_write(str(outside), "hi")
    assert "error" in res and "工作范围限制" in res["error"]
    assert not outside.exists()


def test_scope_blocks_delete_and_cmd_run(tmp_path):
    from core import permissions
    from core.commands import cmd_file_delete, cmd_cmd_run

    ws = tmp_path / "ws"
    ws.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("x", encoding="utf-8")
    _set(default_workdir=str(ws), work_scope_mode="workspace")

    assert "error" in cmd_file_delete(str(victim))
    assert victim.exists()  # 没被删掉

    res = cmd_cmd_run("echo hi", cwd=str(tmp_path))
    assert "error" in res and "工作范围限制" in res["error"]


def test_scope_custom_paths(tmp_path):
    from core import permissions

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _set(work_scope_mode="custom", scope_paths=[str(a)], default_workdir=str(tmp_path / "ws"))

    assert permissions.check_path(str(a / "f.txt")) is None
    assert permissions.check_path(str(b / "f.txt")) is not None
    # 只读路径不受范围限制（check_path 只用于写/删/命令，这里校验其本身语义）
    assert "工作范围限制" in (permissions.check_path(str(b / "f.txt")) or "")


def test_scope_custom_without_paths_falls_back_to_workspace(tmp_path):
    from core import permissions

    ws = tmp_path / "ws"
    ws.mkdir()
    _set(default_workdir=str(ws), work_scope_mode="custom", scope_paths=[])

    assert permissions.check_path(str(ws / "f.txt")) is None
    assert permissions.check_path(str(tmp_path / "f.txt")) is not None


def test_scope_global_and_full_mode_disable_check(tmp_path):
    from core import permissions

    outside = tmp_path / "f.txt"
    _set(work_scope_mode="global")
    assert permissions.check_path(str(outside)) is None

    # 完全权限模式：连工作范围一起关掉
    _set(work_scope_mode="workspace", default_workdir=str(tmp_path / "ws"),
         permission_mode="full")
    assert permissions.check_path(str(outside)) is None


# ==============================================================
# 手动确认（manual 模式）
# ==============================================================
def test_begin_request_only_in_manual_mode_for_dangerous_tools():
    from core import permissions

    # auto（默认）→ 不拦
    assert permissions.begin_request("file.write", {"path": "a"}) is None
    # manual + 只读 → 不拦
    _set(permission_mode="manual")
    assert permissions.begin_request("file.read", {"path": "a"}) is None
    # manual + 危险 → 拦
    req = permissions.begin_request("file.write", {"path": "a" * 4000}, conv_id=7)
    assert req is not None
    assert req["tool"] == "file.write"
    assert req["conv_id"] == 7
    assert isinstance(req["args"], dict)  # 超长参数已截断
    # 拒绝并消费掉，别把请求留在全局表里影响后续用例
    permissions.resolve(req["id"], False)
    assert asyncio.run(permissions.wait(req["id"], timeout=1)) is False
    assert permissions.pending_list() == []


def test_confirm_approve_and_deny():
    from core import permissions

    _set(permission_mode="manual")
    req = permissions.begin_request("cmd.run", {"command": "echo hi"})
    assert req is not None

    # 另一个线程代用户点「允许」
    threading.Timer(0.05, permissions.resolve, args=(req["id"], True)).start()
    assert asyncio.run(permissions.wait(req["id"], timeout=5)) is True
    # 已消费掉，不会重复出现
    assert permissions.pending_list() == []

    req2 = permissions.begin_request("cmd.run", {"command": "echo hi"})
    assert req2 is not None
    threading.Timer(0.05, permissions.resolve, args=(req2["id"], False)).start()
    assert asyncio.run(permissions.wait(req2["id"], timeout=5)) is False


def test_confirm_timeout_means_denied():
    from core import permissions

    _set(permission_mode="manual")
    req = permissions.begin_request("file.delete", {"path": "x"})
    assert req is not None
    assert asyncio.run(permissions.wait(req["id"], timeout=0.2)) is False
    assert permissions.pending_list() == []


def test_cancel_conversation_denies_pending():
    from core import permissions

    _set(permission_mode="manual")
    req = permissions.begin_request("file.delete", {"path": "x"}, conv_id=42)
    assert req is not None
    assert permissions.cancel_conversation(42) == 1  # 点「停止」→ 立即按拒绝唤醒
    assert asyncio.run(permissions.wait(req["id"], timeout=1)) is False
    # 其它会话不受影响
    assert permissions.cancel_conversation(999) == 0


def _client():
    """只挂载被测路由的小 app（不 import main，避免触发主应用 lifespan 副作用）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import permissions as perm_api
    from api import settings as settings_api

    app = FastAPI()
    app.include_router(settings_api.router, prefix="/api/settings")
    app.include_router(perm_api.router, prefix="/api/permissions")
    return TestClient(app)


def test_confirm_endpoint_roundtrip():
    """HTTP 层：POST /api/permissions/confirm 唤醒阻塞的 wait()。"""
    from core import permissions

    _set(permission_mode="manual")
    req = permissions.begin_request("cmd.run", {"command": "echo hi"})
    assert req is not None

    client = _client()
    r = client.post("/api/permissions/confirm",
                    json={"request_id": req["id"], "approve": True})
    assert r.status_code == 200, r.text
    assert r.json()["approved"] is True
    assert asyncio.run(permissions.wait(req["id"], timeout=5)) is True

    # 不存在的请求 → 404（前端据此关弹窗）
    r2 = client.post("/api/permissions/confirm",
                     json={"request_id": "nope", "approve": True})
    assert r2.status_code == 404

    # 挂起列表可被补拉（断线重连场景）
    req3 = permissions.begin_request("file.delete", {"path": "x"})
    assert req3 is not None
    listed = client.get("/api/permissions/pending").json()["requests"]
    assert any(item["id"] == req3["id"] for item in listed)
    permissions.resolve(req3["id"], False)
    assert asyncio.run(permissions.wait(req3["id"], timeout=1)) is False


async def _drive_gated(name: str, args: dict, approve: bool | None):
    """跑一遍 _exec_tool_gated：返回 (发出的行列表, 结果 dict)。

    approve=None 表示不产生确认请求（auto/full 模式或非危险工具）；
    否则在收到 P: 行后由后台线程代用户点允许(True)/拒绝(False)。
    """
    import json as _json

    import core.agent_runtime as ar
    from core import permissions

    out: dict = {}
    lines: list[str] = []
    gen = ar._exec_tool_gated("tc-test", name, args, out, conv_id=1, agent_id=1)
    try:
        try:
            first = await gen.__anext__()
        except StopAsyncIteration:
            first = None  # 没有任何 yield（auto 模式直接执行完）
        if first is not None:
            lines.append(first)
            if first.startswith("P:") and approve is not None:
                rid = _json.loads(first[2:])["id"]
                threading.Timer(0.05, permissions.resolve, args=(rid, approve)).start()
            async for line in gen:
                lines.append(line)
    finally:
        await gen.aclose()
    return lines, out.get("result") or {}


def test_gated_executor_approve_and_deny():
    """manual 模式：先 yield P: 确认行，允许 → 真执行；拒绝 → 返回拒绝错误。"""
    _set(permission_mode="manual", work_scope_mode="global")

    lines, result = asyncio.run(_drive_gated("cmd.run", {"command": "echo hi"}, True))
    assert lines and lines[0].startswith("P:")
    assert "error" not in result, result          # 允许 → 命令真的跑了
    assert "hi" in str(result.get("stdout", ""))

    lines2, result2 = asyncio.run(_drive_gated("cmd.run", {"command": "echo hi"}, False))
    assert lines2 and lines2[0].startswith("P:")
    assert "拒绝" in str(result2.get("error", ""))


def test_gated_executor_auto_mode_has_no_prompt():
    """auto（默认）/ 只读工具：不发 P: 行，直接执行。"""
    _set(permission_mode="auto", work_scope_mode="global")
    lines, result = asyncio.run(_drive_gated("cmd.run", {"command": "echo hi"}, None))
    assert not any(l.startswith("P:") for l in lines)
    assert "error" not in result, result

    _set(permission_mode="manual")
    lines2, _ = asyncio.run(_drive_gated("file.read", {"path": "whatever"}, None))
    assert not any(l.startswith("P:") for l in lines2)  # 只读工具不该被打断


def test_settings_update_validates_permission_keys():
    client = _client()
    ok = client.put("/api/settings", json={"permission_mode": "manual"})
    assert ok.status_code == 200
    assert ok.json()["permission_mode"] == "manual"

    bad = client.put("/api/settings", json={"permission_mode": "whatever"})
    assert bad.status_code == 400
    bad2 = client.put("/api/settings", json={"work_scope_mode": "universe"})
    assert bad2.status_code == 400
    bad3 = client.put("/api/settings", json={"scope_paths": "not-a-list"})
    assert bad3.status_code == 400
