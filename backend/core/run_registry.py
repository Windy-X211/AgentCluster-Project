"""会话运行注册表：智能体运行与客户端连接解耦。

此前 POST /conversations/{cid}/send 的流式响应直接迭代 agent_reply，客户端断开
（关窗 / 断网 / 刷新）会触发 asyncio.CancelledError 中止整个运行。现在 send 端点把
agent_reply 放进后台任务跑，所有行协议写入事件缓冲并广播给在线订阅者：

- 客户端断开只取消订阅，运行继续（重开窗口 / 浏览器打开都能看到运行状态）
- 重连恢复：GET /{cid}/running 拿 send 时刻的基底快照 + GET /{cid}/events?since=0
  全量重放行协议，前端界面即可完整重建运行态
- 显式停止：POST /{cid}/stop 取消后台任务（pump 会补发 STOP + DONE 行）

运行的隔离粒度是「会话 + 智能体」：同一会话里多个智能体可以并发输出，
停止时可只停其中一个（agent_id），也可停掉整个会话的全部运行。
"""
import asyncio
import copy
import time
from collections import deque

from core.store import get_by as _get_by

# 事件缓冲上限（行）：一次长任务的 T:/RN: 增量行远达不到这个量级，仅作内存保险
BUF_MAX = 200000
# 运行结束后缓冲与基底保留时长（秒），供晚到的前端补拉；超时懒清理
FINISHED_TTL = 600.0


class RunSession:
    """一次会话运行：后台任务 + 带序号的事件缓冲 + 实时订阅者。"""

    def __init__(self, conv_id: int, agent_id: int, base_conv: dict):
        self.conv_id = conv_id
        self.agent_id = agent_id
        # send 时刻的会话快照（messages 深拷贝）：重连恢复的基底。
        # store.get_by 返回缓存对象，agent_reply 会原地 append → 必须深拷贝隔离。
        self.base_conv: dict = copy.deepcopy(base_conv)
        self.events: deque = deque(maxlen=BUF_MAX)
        self.seq = 0
        self.subscribers: set = set()
        self.task: asyncio.Task | None = None
        # 本运行 spawn 的子任务（并行接力链的接收方等）→ 停止本运行时一并取消，
        # 避免只 cancel 父 task 但子 task 还在后台跑 LLM 造成泄漏。
        self.child_tasks: set[asyncio.Task] = set()
        # child task → 接收方 agent_id：串行接力下 B 的运行是 A 的 child task（不占
        # 独立 _RUNS 键），按 agent_id 精确停止时靠它路由到对应 child task
        self.child_task_agents: dict[asyncio.Task, int] = {}
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.cancelled = False
        self.saw_done = False  # agent_reply 已正常发过 DONE → finally 不再补发

    @property
    def running(self) -> bool:
        return self.ended_at is None and self.task is not None and not self.task.done()

    def push(self, line: str) -> None:
        """追加一个事件并广播给所有在线订阅者（事件循环内同步调用，天然原子）。"""
        self.seq += 1
        self.events.append((self.seq, line))
        for q in list(self.subscribers):
            q.put_nowait((self.seq, line))

    async def subscribe(self, since: int = 0):
        """从 since 之后重放缓冲，再实时跟随新事件；DONE 行后结束迭代。

        先同步快照再注册订阅者（两者之间无 await，原子），
        保证重放与实时推送之间不丢行、不重复。"""
        q: asyncio.Queue = asyncio.Queue()
        snap = [e for e in self.events if e[0] > since]
        self.subscribers.add(q)
        try:
            for _seq, line in snap:
                yield line
                if line.strip() == "DONE":
                    return
            while True:
                _seq, line = await q.get()
                yield line
                if line.strip() == "DONE":
                    return
        finally:
            self.subscribers.discard(q)

    def describe(self, include_base: bool = False) -> dict:
        out = {
            "conv_id": self.conv_id,
            "agent_id": self.agent_id,
            "running": self.running,
            "seq": self.seq,
            "started_at": self.started_at,
            "cancelled": self.cancelled,
        }
        if include_base:
            out["base_conv"] = {
                "id": self.base_conv.get("id"),
                "messages": self.base_conv.get("messages", []),
            }
        return out


# (conv_id, agent_id) → RunSession（运行中 + 结束后 TTL 内的）
_RUNS: dict[tuple[int, int], RunSession] = {}

# 运行期共享会话对象：同一会话的并发运行必须读写同一份 dict，
# 否则各自 get_by 的 deepcopy 会互相覆盖落库（并发按智能体运行的硬前提）。
_LIVE_CONVS: dict[int, dict] = {}


def live_conv(conv_id: int, snapshot: dict | None = None) -> dict:
    """取该会话运行期共享对象；不存在时用 snapshot（或磁盘最新）创建并缓存。"""
    c = _LIVE_CONVS.get(conv_id)
    if c is None:
        c = snapshot if snapshot is not None else _get_by("conversations", conv_id)
        if c is None:
            c = {"id": conv_id, "messages": []}
        _LIVE_CONVS[conv_id] = c
    return c


def _release_live_conv(conv_id: int) -> None:
    """该会话已无运行中的任务 → 丢弃共享对象（下次运行重新读盘拿到前端最终落库的版本）。"""
    if not any(k[0] == conv_id and s.running for k, s in _RUNS.items()):
        _LIVE_CONVS.pop(conv_id, None)


def peek_live_conv(conv_id: int) -> dict | None:
    """只读查询运行期共享对象（不存在返回 None，绝不创建）——
    供 API 端点在"运行中"时读写同一份对象，避免 get_by deepcopy 覆盖运行期写入。"""
    return _LIVE_CONVS.get(conv_id)


def _cleanup() -> None:
    """懒清理：移除结束超过 FINISHED_TTL 的会话（无订阅者时才清）。"""
    now = time.time()
    for key in list(_RUNS.keys()):
        s = _RUNS[key]
        if s.ended_at is not None and (now - s.ended_at) > FINISHED_TTL:
            if not s.subscribers:
                _RUNS.pop(key, None)


def _find(conv_id: int, agent_id: int | None = None) -> list[RunSession]:
    """该会话的运行（按开始时间升序）；给了 agent_id 则只取该智能体的运行。"""
    _cleanup()
    out = [s for k, s in _RUNS.items()
           if k[0] == conv_id and (agent_id is None or k[1] == agent_id)]
    out.sort(key=lambda s: s.started_at)
    return out


def get(conv_id: int, agent_id: int | None = None) -> RunSession | None:
    """取一个运行会话；未指定 agent_id 时返回最早开始的那个（兼容单运行调用方）。"""
    runs = _find(conv_id, agent_id)
    return runs[0] if runs else None


def runs_of(conv_id: int) -> list[RunSession]:
    return _find(conv_id)


def is_running(conv_id: int, agent_id: int | None = None) -> bool:
    return any(s.running for s in _find(conv_id, agent_id))


def list_running() -> list[dict]:
    _cleanup()
    return [s.describe() for s in _RUNS.values() if s.running]


def stop(conv_id: int, agent_id: int | None = None) -> bool:
    """取消该会话的后台运行任务；agent_id 给定时只停该智能体，否则停掉全部。

    - **指定 agent_id（精确停止）**：只取消父 pump task，**不**取消 child_tasks。
      并行 A2A 接力模式下接收方 spawn 出来的 child task 继续运行并独立输出完成，
      避免"停 A 把 spawn 的 B/C 也一起停了"的问题。
      例外：该 agent_id 正是某个 child task 的接收方（串行接力 B 挂在 A 的运行下，
      不占独立 _RUNS 键）→ 取消对应 child task，B 的事件流补 STOP 行。
    - **全停（agent_id=None）**：先取消所有 child_tasks 再取消父 task，整条链终止。
    """
    stopped = False
    for s in _find(conv_id):
        if agent_id is None:
            # 全停：先取消所有子任务再取消父 task
            for ct in list(s.child_tasks):
                if not ct.done():
                    ct.cancel()
                    stopped = True
        else:
            # 精确停止：路由到接收方为该 agent 的 child task（串行接力链）
            for ct, aid in list(s.child_task_agents.items()):
                if aid == agent_id and not ct.done():
                    ct.cancel()
                    stopped = True
    for s in _find(conv_id, agent_id):
        if s.running:
            s.task.cancel()
            stopped = True
    return stopped


def _on_session_children_done(session: RunSession) -> None:
    """当 session.task + 所有 child_tasks 都 done 时被调用（child_tasks 的 done callback 触发）：
    兜底补 DONE + 设 ended_at。用于精确停止后 child_tasks 继续运行完成的场景。"""
    if session.ended_at is not None or session.saw_done:
        return
    if session.task is not None and not session.task.done():
        return
    if any(not ct.done() for ct in session.child_tasks):
        return
    # 所有任务都结束了，清理
    session.push("DONE\n")
    session.ended_at = time.time()
    _release_live_conv(session.conv_id)
    _cleanup()


async def _pump(session: RunSession, conv_id: int, agent: dict, content: str, mode: str,
                canvas: dict | None, workdir: str | None = None) -> None:
    """后台消费 agent_reply：每行写入事件缓冲并广播；任何退出路径都保证 DONE 收尾。

    精确停止（stop 指定 agent_id）时 pump 被 cancel，但若 session.child_tasks 里还有
    并行 spawn 的接收方在运行，则不立即发 DONE/设 ended_at，而是等 child_tasks 全部
    结束后由 _on_session_children_done 统一清理。"""
    from core.agent_runtime import agent_reply  # 延迟导入，避免潜在循环依赖

    try:
        async for line in agent_reply(conv_id, agent, content, mode, canvas, workdir=workdir, run_session=session):
            session.push(line)
            if line.strip() == "DONE":
                session.saw_done = True
    except asyncio.CancelledError:
        # 显式停止（POST /stop）：agent_reply 的 finally 已清理模型槽/子任务
        session.cancelled = True
        session.push("STOP\n")
    except Exception as e:
        import traceback
        print(f"[run_registry] pump FATAL for conv {conv_id}: {e}\n{traceback.format_exc()}", flush=True)
        err_text = str(e).replace("\n", "\\n").replace("\r", "\\r")
        session.push(f"E:后端崩溃：{err_text}\n")
    finally:
        # 如果还有 child_tasks 没完成（精确停止后它们继续运行），延迟清理到 child_tasks 全 done 时
        if session.child_tasks and any(not ct.done() for ct in session.child_tasks):
            # 不设 ended_at、不 push DONE，等 _on_session_children_done 回调
            return
        if not session.saw_done:
            session.push("DONE\n")  # 异常/取消路径兜底，保证订阅者总能收到结束标记
        session.ended_at = time.time()
        _release_live_conv(conv_id)
        _cleanup()


def start(conv_id: int, agent: dict, content: str, mode: str, canvas: dict | None,
          base_conv: dict, workdir: str | None = None) -> RunSession:
    """创建后台运行会话（须在事件循环内的协程中调用）。键 = (conv_id, agent_id)。"""
    agent_id = agent.get("id")
    session = RunSession(conv_id, agent_id, base_conv)
    session.task = asyncio.get_running_loop().create_task(
        _pump(session, conv_id, agent, content, mode, canvas, workdir)
    )
    _RUNS[(conv_id, agent_id)] = session
    return session