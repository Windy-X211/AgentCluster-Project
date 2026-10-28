"""会话运行注册表：智能体运行与客户端连接解耦。

此前 POST /conversations/{cid}/send 的流式响应直接迭代 agent_reply，客户端断开
（关窗 / 断网 / 刷新）会触发 asyncio.CancelledError 中止整个运行。现在 send 端点把
agent_reply 放进后台任务跑，所有行协议写入事件缓冲并广播给在线订阅者：

- 客户端断开只取消订阅，运行继续（重开窗口 / 浏览器打开都能看到运行状态）
- 重连恢复：GET /{cid}/running 拿 send 时刻的基底快照 + GET /{cid}/events?since=0
  全量重放行协议，前端界面即可完整重建运行态
- 显式停止：POST /{cid}/stop 取消后台任务（pump 会补发 STOP + DONE 行）
"""
import asyncio
import copy
import time
from collections import deque

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


# conv_id → RunSession（运行中 + 结束后 TTL 内的）
_RUNS: dict[int, RunSession] = {}


def _cleanup() -> None:
    """懒清理：移除结束超过 FINISHED_TTL 的会话（无订阅者时才清）。"""
    now = time.time()
    for cid in list(_RUNS.keys()):
        s = _RUNS[cid]
        if s.ended_at is not None and (now - s.ended_at) > FINISHED_TTL:
            if not s.subscribers:
                _RUNS.pop(cid, None)


def get(conv_id: int) -> RunSession | None:
    _cleanup()
    return _RUNS.get(conv_id)


def is_running(conv_id: int) -> bool:
    s = _RUNS.get(conv_id)
    return bool(s and s.running)


def list_running() -> list[dict]:
    _cleanup()
    return [s.describe() for s in _RUNS.values() if s.running]


def stop(conv_id: int) -> bool:
    """取消该会话的后台运行任务；pump 兜底补发 STOP + DONE 行。"""
    s = _RUNS.get(conv_id)
    if not s or not s.running:
        return False
    s.task.cancel()
    return True


async def _pump(session: RunSession, conv_id: int, agent: dict, content: str, mode: str, canvas: dict | None) -> None:
    """后台消费 agent_reply：每行写入事件缓冲并广播；任何退出路径都保证 DONE 收尾。"""
    from core.agent_runtime import agent_reply  # 延迟导入，避免潜在循环依赖

    try:
        async for line in agent_reply(conv_id, agent, content, mode, canvas):
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
        if not session.saw_done:
            session.push("DONE\n")  # 异常/取消路径兜底，保证订阅者总能收到结束标记
        session.ended_at = time.time()
        _cleanup()


def start(conv_id: int, agent: dict, content: str, mode: str, canvas: dict | None, base_conv: dict) -> RunSession:
    """创建后台运行会话（须在事件循环内的协程中调用）。"""
    session = RunSession(conv_id, agent.get("id"), base_conv)
    session.task = asyncio.get_running_loop().create_task(
        _pump(session, conv_id, agent, content, mode, canvas)
    )
    _RUNS[conv_id] = session
    return session