"""权限与工作范围闸门 —— 设置页「权限设置」的后端执行端。

设置键（存 settings.json，见 store.DEFAULT_SETTINGS）：
    permission_mode   操作权限模式：manual 手动确认 / auto 自动审核（默认）/ full 完全权限
    work_scope_mode   工作范围：workspace 仅限工作区（默认）/ custom 指定范围 / global 电脑全局
    scope_paths       指定范围的目录列表（work_scope_mode=custom 时生效）

两类检查的执行位置不同：
  1) **手动确认**需要与前端交互（阻塞等待用户点允许/拒绝），只能放在异步上下文里，
     由 agent_runtime._exec_tool_lines 在派发工具**之前**完成：
         begin_request() → 发 `P:{json}` 行 → wait() 阻塞 → 用户经
         POST /api/permissions/confirm 回填 → resolve() 唤醒。
     子智能体（agent.spawn 内部）没有 SSE 生成器，改走事件队列：
         begin_request() → _emit({"t":"permission", ...})（SUB: 行）→ wait_sync()。
  2) **工作范围**是纯本地判断，放在 commands.py 的文件写/删命令与 cmd.run 里
     （check_path），这样用户手动跑命令的入口（api/commands）也一并受约束。

模式语义：
    manual —— 危险操作逐个问用户；超时 / 用户拒绝 / 运行被停止 → 返回错误，不执行。
    auto   —— 危险操作直接放行（默认，日常使用零打扰）。
    full   —— 连工作范围也不检查（完全信任，等同关闭本模块）。

注意：本模块与 commands.py 互相 import（scope 要读 workdir，commands 要读 scope），
因此**一律在函数内部延迟 import**，避免模块加载期环形依赖。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from typing import Any

from core.store import load

# 操作权限模式 / 工作范围的合法取值（设置页与 PUT /api/settings 校验共用）
VALID_PERMISSION_MODES = ("manual", "auto", "full")
VALID_SCOPE_MODES = ("workspace", "custom", "global")

# 手动确认等待时长（秒）：超时按「拒绝」处理，避免一次忘点把运行永远挂住
CONFIRM_TIMEOUT = 120.0

# 危险（需授权）命令：文件修改/删除、命令执行、集群写操作、派生子智能体。
# 只读命令（file.read / file.search / cluster.get_* …）不在其列 → 任何模式都不拦。
DANGEROUS_COMMANDS = {
    # 文件修改 / 删除
    "file.write", "file.delete", "file.replace", "file.patch",
    # 命令执行
    "cmd.run",
    # 派生子智能体（子任务同样会跑工具）
    "agent.spawn",
    # 集群编排写操作
    "cluster.create_agent", "cluster.update_agent", "cluster.delete_agent",
    "cluster.create_group", "cluster.update_group", "cluster.delete_group",
    "cluster.assign_agent_group", "cluster.create_canvas", "cluster.delete_canvas",
    "cluster.add_node", "cluster.remove_node", "cluster.connect", "cluster.disconnect",
    "cluster.create_rule", "cluster.update_rule", "cluster.delete_rule",
    "cluster.create_conversation", "cluster.update_conversation", "cluster.continue_conversation",
    "cluster.stop_agent",
}
# 需要受工作范围约束的命令（= 危险集里「会落到磁盘 / 会起进程」的那部分）
SCOPE_COMMANDS = {"file.write", "file.delete", "file.replace", "file.patch", "cmd.run"}
# 工作流工具（plan.*/spec.*/goal.*）只改工作流状态文件，不碰用户磁盘 → 不算危险
_WORKFLOW_PREFIXES = ("plan", "spec", "goal")

# 设置短 TTL 缓存（每个工具调用都读 settings.json 会拖慢派发，与 default_workdir 同策略）
_CACHE_TTL = 2.0
_cache: dict = {"at": 0.0, "cfg": None}


def get_settings() -> dict:
    """读取权限设置（2s TTL 缓存，异常时回退默认值：auto + 仅限工作区）。"""
    now = time.time()
    cached = _cache
    if cached["cfg"] is not None and (now - cached["at"]) < _CACHE_TTL:
        return cached["cfg"]
    try:
        s = load("settings") or {}
    except Exception:
        s = {}
    mode = s.get("permission_mode") or "auto"
    scope = s.get("work_scope_mode") or "workspace"
    if mode not in VALID_PERMISSION_MODES:
        mode = "auto"
    if scope not in VALID_SCOPE_MODES:
        scope = "workspace"
    paths = s.get("scope_paths") or []
    if not isinstance(paths, list):
        paths = []
    cfg = {
        "permission_mode": mode,
        "work_scope_mode": scope,
        "scope_paths": [p.strip() for p in paths if isinstance(p, str) and p.strip()],
    }
    cached["cfg"] = cfg
    cached["at"] = now
    return cfg


def invalidate_cache() -> None:
    """设置写入后立即失效缓存（PUT /api/settings 调用，否则最多延迟 2s 生效）。"""
    _cache["cfg"] = None
    _cache["at"] = 0.0


# ==============================================================
# 危险操作判定
# ==============================================================
def is_dangerous(name: str) -> bool:
    """该工具是否属于「危险操作」（manual 模式下需要用户逐个确认）。

    命令按 DANGEROUS_COMMANDS 判定；**非命令**（插件 / 外部工具 / MCP）无法静态
    判断副作用 → 一律按危险处理（宁可多问一次）。
    """
    if not name:
        return False
    if name.split(".", 1)[0] in _WORKFLOW_PREFIXES:
        return False
    if name in DANGEROUS_COMMANDS:
        return True
    try:
        from core.commands import COMMAND_META
        return name not in COMMAND_META
    except Exception:
        return True


# ==============================================================
# 工作范围校验
# ==============================================================
def scope_roots() -> list[str] | None:
    """允许操作的根目录列表；返回 None 表示不限制（电脑全局 / 完全权限）。"""
    cfg = get_settings()
    if cfg["permission_mode"] == "full":
        return None
    mode = cfg["work_scope_mode"]
    if mode == "global":
        return None
    if mode == "custom" and cfg["scope_paths"]:
        roots: list[str] = []
        for p in cfg["scope_paths"]:
            ap = os.path.abspath(os.path.expanduser(p))
            if os.path.isdir(ap):
                roots.append(ap)
        if roots:
            return roots
        # 指定的目录全都不存在 → 回退工作区，避免「配了范围反而哪儿都写不了」
    try:
        from core.commands import _effective_workdir
        wd = _effective_workdir()
    except Exception:
        wd = None
    # 没有工作区时与 _resolve 的兜底一致：进程 CWD
    return [os.path.abspath(wd or os.getcwd())]


def _under(path: str, root: str) -> bool:
    """path 是否位于 root 之内（含 root 自身）；跨盘符 / 非法路径按否处理。"""
    try:
        c = os.path.normcase(os.path.abspath(path))
        r = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([c, r]) == r
    except Exception:
        return False


def check_path(path: str, op: str = "此操作") -> str | None:
    """工作范围校验：返回 None 表示放行，否则返回给模型/用户看的错误文案。

    path 必须已是绝对路径（commands._resolve / cwd_eff 解析后再传进来）。
    """
    roots = scope_roots()
    if roots is None:
        return None
    try:
        p = os.path.abspath(os.path.expanduser(str(path)))
    except Exception:
        return None
    if any(_under(p, r) for r in roots):
        return None
    scope_desc = "允许的工作范围"
    return (
        f"工作范围限制：不允许在{scope_desc}之外执行{op}（{p}）。"
        f"当前允许的范围：{'；'.join(roots)}。"
        f"如需放宽，请在「设置 → 权限设置」中调整工作范围。"
    )


# ==============================================================
# 手动确认（manual 模式）
# ==============================================================
# 待确认请求表：request_id → dict（event 线程间唤醒，approved 结果位）
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()


def _brief_args(args: Any) -> Any:
    """参数摘要：截断超长内容，避免 P: 行 / 弹窗被大文件内容撑爆。"""
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        text = str(args)
    if len(text) <= 1200:
        try:
            return json.loads(text)
        except Exception:
            return {"_raw": text}
    return {"_raw": text[:1200] + "…（已截断）"}


def begin_request(name: str, args: Any, *, conv_id: int | None = None,
                  agent_id: int | None = None, tool_call_id: str | None = None) -> dict | None:
    """manual 模式且工具危险 → 登记待确认请求并返回 P: 行 payload；否则返回 None（放行）。"""
    try:
        cfg = get_settings()
        if cfg["permission_mode"] != "manual":
            return None
        if not is_dangerous(name):
            return None
        label = name
        try:
            from core.commands import COMMAND_META
            label = (COMMAND_META.get(name) or {}).get("label") or name
        except Exception:
            pass
        rid = uuid.uuid4().hex[:16]
        req = {
            "id": rid,
            "tool": name,
            "label": label,
            "args": _brief_args(args),
            "conv_id": conv_id,
            "agent_id": agent_id,
            "tool_call_id": tool_call_id,
            "created_at": time.time(),
            "event": threading.Event(),
            "approved": False,
        }
        with _pending_lock:
            _pending[rid] = req
        return {
            "id": rid,
            "tool": name,
            "label": label,
            "args": req["args"],
            "conv_id": conv_id,
            "agent_id": agent_id,
            "timeout": int(CONFIRM_TIMEOUT),
        }
    except Exception:
        # 闸门自身故障时放行：不能因为权限模块出错把整个工具链打挂
        return None


def _finish(rid: str) -> bool:
    with _pending_lock:
        req = _pending.pop(rid, None)
    if req is None:
        return False
    return bool(req.get("approved"))


def wait_sync(rid: str, timeout: float | None = None) -> bool:
    """阻塞等待用户确认（子智能体等同步上下文用）；超时 / 已被取消 → 拒绝。"""
    with _pending_lock:
        req = _pending.get(rid)
    if req is None:
        return False
    req["event"].wait(timeout if timeout is not None else CONFIRM_TIMEOUT)
    return _finish(rid)


async def wait(rid: str, timeout: float | None = None) -> bool:
    """异步等待用户确认（主循环用）；内部转线程阻塞，不占事件循环。"""
    return await asyncio.to_thread(wait_sync, rid, timeout)


def resolve(rid: str, approve: bool) -> bool:
    """用户点了允许 / 拒绝（HTTP 线程调用）→ 唤醒等待方。返回是否找到了该请求。"""
    with _pending_lock:
        req = _pending.get(rid)
    if req is None:
        return False
    req["approved"] = bool(approve)
    req["event"].set()
    return True


def pending_list() -> list[dict]:
    """当前待确认请求（前端刷新后可经 GET /api/permissions/pending 补拉）。"""
    with _pending_lock:
        items = list(_pending.values())
    return [
        {
            "id": r["id"], "tool": r["tool"], "label": r.get("label") or r["tool"],
            "args": r.get("args"), "conv_id": r.get("conv_id"), "agent_id": r.get("agent_id"),
            "timeout": int(CONFIRM_TIMEOUT),
            "remaining": max(0, int(CONFIRM_TIMEOUT - (time.time() - r["created_at"]))),
        }
        for r in items
    ]


def cancel_conversation(conv_id: int | None) -> int:
    """运行被停止时取消该会话挂起的确认请求（一律按拒绝收场），返回取消条数。"""
    if conv_id is None:
        return 0
    n = 0
    with _pending_lock:
        targets = [r for r in _pending.values() if r.get("conv_id") == conv_id]
    for r in targets:
        r["approved"] = False
        r["event"].set()
        n += 1
    return n
