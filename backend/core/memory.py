"""智能体长期记忆（记忆技能）核心模块。

机制：
- 存储域 memories/shared.json——**所有智能体共用一份记忆**（固定条目 + 滚动摘要），
  只有拥有记忆技能（memory_mode 开启）的智能体可读写，未开启的智能体不可见
  {summary_text, entries[{id, content, pinned, source, ts}], updated_at}
  entries 条目：{id, content, pinned, source, ts}
    - pinned=True 为「固定记忆」：用户明确要求记住的内容，系统整理永不触碰
    - source：manual（智能体手动写入）/ user_request（用户主动要求 → pinned）/ summary
- 两种模式（agent.memory_mode，全局总开关 settings.memory_enabled）：
  - manual 手动记忆：仅当用户主动提出记入记忆时，智能体调用 memory.write 写入
  - summary 摘要记忆：每轮对话收尾后系统自动用 LLM 把「已有摘要 + 本轮对话」
    整理成新的滚动摘要（合并去重、控制篇幅避免杂乱冗长），用户主动要求的
    内容仍走固定记忆（pinned），不会遗忘
- 旧版按智能体隔离的 memories/<agent_id>.json 会自动合并迁移进 shared.json
  （条目按内容去重、pinned 优先，旧文件改名 .bak 保留）
"""
import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

from core.store import ROOT, load

_logger = logging.getLogger("memory")

# 合法模式：off=关闭 / manual=手动记忆 / summary=摘要记忆
MODES = ("off", "manual", "summary")

# memory.* 命令名（与 commands.py 中 schema 注册保持一致）
MEMORY_COMMANDS = ("memory.read", "memory.write", "memory.delete")

_SHARED_FILE = "shared.json"

# —— 写并发保护：共享记忆文件的读-改-写必须串行 ——
_MEM_LOCK = threading.RLock()


def _lock() -> threading.RLock:
    return _MEM_LOCK


def mem_path() -> Path:
    return Path(ROOT) / "memories" / _SHARED_FILE


def _empty() -> dict:
    return {"summary_text": "", "entries": [], "updated_at": None}


def _migrate_legacy() -> None:
    """把旧版按智能体隔离的 memories/<agent_id>.json 合并迁移进 shared.json。

    条目按内容去重（pinned 优先保留），摘要取最长非空；迁移完的旧文件改名 .bak。
    仅在 shared.json 不存在且存在旧文件时执行一次。
    """
    root = Path(ROOT) / "memories"
    legacy = sorted(p for p in root.glob("*.json")
                    if p.name != _SHARED_FILE and p.stem.isdigit())
    if not legacy:
        return
    merged_entries: list = []
    best_summary = ""
    for p in legacy:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            _logger.warning("旧记忆文件 %s 读取失败（跳过）：%s", p.name, e)
            continue
        seen = {e.get("content") for e in merged_entries if isinstance(e, dict)}
        for e in (data.get("entries") or []):
            if not isinstance(e, dict) or not (e.get("content") or "").strip():
                continue
            if e["content"] in seen:
                continue
            seen.add(e["content"])
            merged_entries.append(e)
        s = (data.get("summary_text") or "").strip()
        if len(s) > len(best_summary):
            best_summary = s
    mem = _empty()
    mem["entries"] = merged_entries
    mem["summary_text"] = best_summary
    save_mem(mem)
    for p in legacy:
        try:
            p.rename(p.with_suffix(".json.bak"))
        except OSError:
            pass
    _logger.info("已合并 %d 个旧记忆文件进 shared.json", len(legacy))


def load_mem() -> dict:
    """读共享记忆；不存在时尝试迁移旧版文件，仍无则返回空结构（不落盘）。"""
    p = mem_path()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("summary_text", "")
                data.setdefault("entries", [])
                if not isinstance(data.get("entries"), list):
                    data["entries"] = []
                return data
        else:
            _migrate_legacy()
            if p.exists():
                return load_mem()
    except Exception as e:
        _logger.warning("共享记忆文件读取失败（按空记忆处理）：%s", e)
    return _empty()


def save_mem(mem: dict) -> None:
    """原子落盘（临时文件 + replace），并刷新 updated_at。"""
    mem["updated_at"] = datetime.utcnow().isoformat()
    p = mem_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _clean_entries(mem: dict) -> list:
    return [e for e in (mem.get("entries") or [])
            if isinstance(e, dict) and (e.get("content") or "").strip()]


def read_memory() -> dict:
    """读取共享记忆全文（命令与 API 共用）。"""
    mem = load_mem()
    entries = _clean_entries(mem)
    return {
        "summary_text": (mem.get("summary_text") or "").strip(),
        "pinned_entries": [e for e in entries if e.get("pinned")],
        "other_entries": [e for e in entries if not e.get("pinned")],
        "updated_at": mem.get("updated_at"),
    }


def add_entry(content: str, pinned: bool = False, source: str = "manual") -> dict | None:
    """追加一条记忆条目；内容为空返回 None。"""
    content = (content or "").strip()
    if not content:
        return None
    with _lock():
        mem = load_mem()
        entry = {
            "id": f"m-{uuid.uuid4().hex[:10]}",
            "content": content,
            "pinned": bool(pinned),
            "source": source,
            "ts": datetime.utcnow().isoformat(),
        }
        mem.setdefault("entries", []).append(entry)
        save_mem(mem)
        return entry


def delete_entry(entry_id: str) -> bool:
    with _lock():
        mem = load_mem()
        entries = mem.get("entries") or []
        kept = [e for e in entries if e.get("id") != entry_id]
        if len(kept) == len(entries):
            return False
        mem["entries"] = kept
        save_mem(mem)
        return True


def update_entry(entry_id: str, content: str | None = None, pinned: bool | None = None) -> dict | None:
    """编辑条目（管理界面用）：仅提供字段会被更新，其余保持。"""
    with _lock():
        mem = load_mem()
        for e in mem.get("entries") or []:
            if e.get("id") == entry_id:
                if content is not None and str(content).strip():
                    e["content"] = str(content).strip()
                if pinned is not None:
                    e["pinned"] = bool(pinned)
                save_mem(mem)
                return e
        return None


def clear_memory(keep_pinned: bool = True) -> dict:
    """清空记忆：keep_pinned=True 时保留固定记忆（默认）。摘要一并清空。"""
    with _lock():
        mem = load_mem()
        mem["entries"] = [e for e in (mem.get("entries") or []) if keep_pinned and e.get("pinned")]
        mem["summary_text"] = ""
        save_mem(mem)
        return mem


def set_summary_text(content: str) -> dict:
    """直接改写滚动摘要（管理界面编辑用）。"""
    with _lock():
        mem = load_mem()
        mem["summary_text"] = (content or "").strip()
        save_mem(mem)
        return mem


# ==============================================================
# 开关与模式（模式仍按智能体：决定各智能体如何参与共享记忆）
# ==============================================================
def memory_enabled_global(settings: dict | None = None) -> bool:
    """记忆功能全局总开关（settings.memory_enabled，默认开）。
    关闭后所有智能体立即失去记忆能力（记忆文件保留，重开即恢复）。"""
    st = settings if settings is not None else (load("settings") or {})
    return st.get("memory_enabled", True) is not False


def memory_mode_of(agent: dict | None) -> str:
    """某智能体实际生效的记忆模式（全局开关 × agent.memory_mode 归一化）。"""
    if not memory_enabled_global():
        return "off"
    m = (agent or {}).get("memory_mode") or "off"
    return m if m in ("manual", "summary") else "off"


# ==============================================================
# 摘要记忆：每轮对话收尾后的自动整理
# ==============================================================
_CONSOLIDATE_SYS = (
    "你是一个智能体集群的长期记忆整理器。请把「已有记忆摘要」和「最新对话」合并整理成一份新的记忆摘要，"
    "供集群中拥有记忆技能的智能体跨会话共用。要求：\n"
    "1. 保留：用户的偏好与习惯、关键事实与决定、项目背景、进行中的任务与待办、用户纠正过的错误认知；\n"
    "2. 合并去重：已有摘要与最新对话中的重复信息合并为一条；信息冲突时以最新对话为准；\n"
    "3. 精简：条目式中文输出，总长不超过 600 字，只保留对后续对话有价值的信息，忽略寒暄与过程细节；\n"
    "4. 用户明确要求记住的内容已单独保存在固定记忆中，不要把这些内容抄进摘要；\n"
    "5. 只输出摘要正文，不要标题、解释或前言；最新对话与记忆无关时原样保留已有摘要。"
)


async def _llm_text(interface: dict, model: str, sys_prompt: str, user_prompt: str) -> str | None:
    """单次 LLM 调用（非流式），返回文本；失败返回 None。"""
    from core.llm import chat_with_failover
    req_msgs = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]
    try:
        async for kind, data in chat_with_failover(interface, model, req_msgs, stream=False, tool_choice="none"):
            if kind == "message":
                c = (data or {}).get("content")
                if isinstance(c, str) and c.strip():
                    return c.strip()
            elif kind == "error":
                _logger.warning("记忆摘要 LLM 调用失败：%s", data)
                return None
    except Exception as e:
        _logger.warning("记忆摘要 LLM 调用异常：%s", e)
    return None


async def summarize_and_store(agent: dict, interface: dict, model: str,
                              user_text: str, assistant_text: str) -> dict | None:
    """摘要记忆模式：把本轮对话整理进共享滚动摘要并落盘（agent_runtime 收尾异步调用）。

    整理语义：新摘要 = LLM(已有摘要 + 本轮对话)，合并去重、控制篇幅；
    固定记忆（pinned）独立保存，整理不触碰。任何失败仅记日志，不影响对话。
    """
    agent_id = agent.get("id")
    if not agent_id or memory_mode_of(agent) != "summary":
        return None
    if not (user_text or "").strip() and not (assistant_text or "").strip():
        return None
    try:
        with _lock():
            mem = load_mem()
        old_summary = (mem.get("summary_text") or "").strip()
        pinned = [e for e in _clean_entries(mem) if e.get("pinned")]
        segs = []
        if old_summary:
            segs.append("【已有记忆摘要】\n" + old_summary[:3000])
        if pinned:
            segs.append("【固定记忆（已单独保存，不要抄进摘要）】\n"
                        + "\n".join(f"- {e['content']}" for e in pinned[:30]))
        segs.append("【最新对话】\n[用户] " + (user_text or "")[:3000]
                    + "\n[智能体] " + (assistant_text or "")[:3000])
        new_summary = await _llm_text(interface, model, _CONSOLIDATE_SYS, "\n\n".join(segs))
        if not new_summary:
            return None
        with _lock():
            mem = load_mem()
            mem["summary_text"] = new_summary[:2500]
            save_mem(mem)
        _logger.info("共享摘要记忆已整理（agent=%s，%d 字）", agent_id, len(new_summary))
        return {"agent_id": agent_id, "chars": len(new_summary)}
    except Exception as e:
        _logger.warning("agent=%s 摘要记忆整理失败（忽略）：%s", agent_id, e)
        return None


# ==============================================================
# system prompt 注入
# ==============================================================
def memory_prompt_block(agent: dict) -> str:
    """拥有记忆技能的智能体的 system prompt 记忆段：共享记忆内容 + 两种模式的使用规则。"""
    mode = memory_mode_of(agent)
    if mode == "off":
        return ""
    mem = load_mem()
    entries = _clean_entries(mem)
    pinned = [e for e in entries if e.get("pinned")]
    normal = [e for e in entries if not e.get("pinned")]
    summary_text = (mem.get("summary_text") or "").strip()

    lines = [
        "\n## 长期记忆（记忆技能已启用）",
        "以下是你与集群中其他拥有记忆技能的智能体共享的长期记忆——在对话中主动运用这些信息，让用户感到你记得他。",
    ]
    if pinned:
        lines.append("### 固定记忆（用户明确要求记住，绝不遗忘）")
        lines.extend(f"- {e['content']}" for e in pinned)
    if summary_text:
        lines.append("### 摘要记忆（系统自动整理的会话摘要）")
        lines.append(summary_text)
    if normal:
        lines.append("### 其他记忆")
        lines.extend(f"- {e['content']}" for e in normal)
    if not pinned and not normal and not summary_text:
        lines.append("（当前还没有任何记忆内容）")

    if mode == "manual":
        lines.append(
            "使用规则（手动记忆模式）：只有用户明确要求「记住 / 记入记忆 / 帮我记着」时，"
            "才调用 memory_write 写入记忆——用户主动要求记住的一律 pinned=true（固定记忆、永不遗忘）；"
            "用户没有要求时不要主动写入任何记忆。用户问到过往信息而记忆段落没写时，调用 memory_read 查看。"
        )
    else:
        lines.append(
            "使用规则（摘要记忆模式）：系统会在每轮对话后自动整理摘要记忆，常规信息无需你手动写入；"
            "但当用户明确要求记住某事时，必须调用 memory_write 且 pinned=true，"
            "确保该内容进入固定记忆、永不遗忘。用户问到过往信息而记忆段落没写时，调用 memory_read 查看。"
        )
    return "\n".join(lines)