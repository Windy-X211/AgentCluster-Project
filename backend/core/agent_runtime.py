"""智能体运行时 - 完整 tool call 链路 + 独立/关联对话模式
流式输出用简易协议（SSE 风格的前缀行）：
  T:<text>                普通文本
  TS:<name>               tool_call 开始（名称首次确定，参数仍在流式中）→ 前端可流式显示"正在调用: xxx"
  C:<id>|<name>|<args>    tool_call 完整（id、名称、JSON参数）→ 参数收齐，结束流式状态
  R:<id>|<content>        tool_result（工具执行结果）
  F:<final_text>          最终完整文本（方便前端完整展示）
  SPEAK:<agent_id>        切换发言者（画布气泡 / 流动连线跟随）
  A2A:<envelope json>     A2A 消息信封（前端插入信封卡片，新开流式消息）
  DONE                    结束

  ⚠️ 行协议用 \n 做分隔符，因此 T:/RN: 等载荷文本中的 \n 必须在 yield 前用
     _esc 转义为 \\n，前端收到后用 _unesc 还原。
"""
import json, asyncio, os, time
from datetime import datetime
from typing import Any
from core.store import load, save, upsert, get_by, next_id
from core.llm import chat_with_failover as chat
from core.commands import run_command, filter_tools, COMMAND_META, WORKDIR_CTX
from core.a2a import (
    TURN_CTX, collect_relay_targets, collect_call_targets,
    build_handoff_envelope, build_call_envelope,
    render_envelope, mark_envelope_done, CALL_TOOL, A2A_HANDOFF_ENABLED,
    _resolve_target,
)
from plugins import PLUGIN_REGISTRY, run_plugin
from core.intent_classifier import classify_intent
from core.validator import verify_completion


def _esc(s: str) -> str:
    """转义行协议载荷中的换行符，避免 payload 内的 \n 破坏行分隔。
    同时转义 \\ 为 \\\\，还原时可唯一反解。"""
    return s.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r')


# ===== 分层自适应循环参数 =====
MAX_VERIFY_RETRIES = 3    # 验证器不通过时，最多让模型修正几轮
FAST_PATH_NO_TOOLS = True # 快通道不注入 tools（简单任务不需要工具调用能力）

# ===== A2A 消息接力参数 =====
A2A_MAX_RELAY_DEPTH = 6   # 消息接力（cluster.send_message / 自动回复）允许的最大 relay 深度；
                          # 超过则不再处理发送，配合 MAX_CALL 预算双重防递归爆炸


DEFAULT_MAX_TOOL_ROUNDS = 50  # 避免 LLM 死循环的默认上限，可在 settings.max_tool_rounds 自定义


# ===== Token 预算控制 =====
# 工具/消息硬截断：防止 base64 截图、长 HTML 等把 messages 撑爆 context window
TOOL_RESULT_MAX_CHARS = 8000       # 单个 tool result 进 LLM 的上限（字符）
HISTORY_MSG_MAX_CHARS = 12000      # 单条历史消息进 LLM 的上限（兜底）
BASE64_KEEP_HEAD = 600             # base64 超长时保留的前缀字符（说明 + 前几百字节即可）

# ===== 图片识别 =====
# 识别 image/* 的 MIME —— 这些类型需要塞进多模态 content 而不是纯文本
_IMAGE_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_IMAGE_MAGIC_BYTES = [
    b"\x89PNG",         # PNG
    b"\xff\xd8\xff",    # JPEG
    b"GIF87a", b"GIF89a",  # GIF
    b"RIFF",            # WebP (RIFF...WEBP)
]


def _looks_like_image(data: bytes | str) -> bool:
    """判断一段 bytes 是否是常见图片格式。"""
    if isinstance(data, str):
        try:
            data = data.encode("latin-1")
        except Exception:
            return False
    if not data or len(data) < 4:
        return False
    for magic in _IMAGE_MAGIC_BYTES:
        if data.startswith(magic):
            return True
    # WebP 额外检查 RIFF....WEBP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def _mime_from_path(path: str) -> str | None:
    """从文件扩展名推断 MIME type（仅处理图片类型）。"""
    lower = str(path).lower()
    for ext, mime in _IMAGE_EXT_TO_MIME.items():
        if lower.endswith(ext):
            return mime
    return None


def _data_url_from_file(path: str) -> str | None:
    """读本地图片文件，返回 OpenAI 多模态可用的 data URL（data:image/png;base64,...）。
    文件不存在 / 不是图片 / 读取失败 返回 None。"""
    mime = _mime_from_path(path)
    if not mime:
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return None
    if not _looks_like_image(raw):
        return None
    import base64 as _b64
    b64 = _b64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _build_image_followup(result: Any) -> dict | None:
    """从 tool result 里提取图片，返回一条 user role 的多模态消息。

    识别两种来源：
      1. result["file"] —— 插件返回的落盘文件路径（如 browser_control screenshot）
      2. result["data"]  —— base64 字符串且 content-type 暗示图片（如 tool.yaml outputs 标注 images）

    返回 OpenAI 规范的多模态 user message 字典；没识别到图片返回 None。
    图片塞 content 数组里紧跟一张 text 说明，让模型知道这是工具返回的结果图片。
    """
    if not isinstance(result, dict):
        return None

    data_urls: list[str] = []

    # —— 来源 1：result["file"] 是本地图片路径 ——
    file_path = result.get("file")
    if isinstance(file_path, str) and file_path:
        url = _data_url_from_file(file_path)
        if url:
            data_urls.append(url)

    # —— 来源 2：result["data"] 是 base64 图片（且没有 file 字段时回退用它） ——
    if not data_urls:
        data_field = result.get("data")
        mime_hint = result.get("content_type") or result.get("mime") or ""
        if isinstance(data_field, str) and len(data_field) > 1000:
            # 纯 base64（不含 data: 前缀）且 mime 暗示图片 → 组装 data URL
            if mime_hint and mime_hint.startswith("image/") and "," not in data_field[:50]:
                data_urls.append(f"data:{mime_hint};base64,{data_field}")
            # 已经是 data URL 格式 → 直接用
            elif data_field.startswith("data:image/"):
                data_urls.append(data_field)

    if not data_urls:
        return None

    # 组装多模态 content
    content: list[dict] = [
        {"type": "text", "text": "这是工具返回的结果图片，请基于它回答。"},
    ]
    for url in data_urls[:4]:  # 最多 4 张，防止撑爆
        content.append({"type": "image_url", "image_url": {"url": url, "detail": "low"}})

    return {"role": "user", "content": content}


def _truncate_tool_result(result: Any) -> str:
    """把工具执行结果序列化成 JSON，并对大字段做智能截断。

    核心策略：
      - base64 截图：只保留前 BASE64_KEEP_HEAD 字符 + 总长度说明（LLM 看了也没用它直接读 base64）
      - data 字段（通常是文本/HTML/JSON）：超过 TOOL_RESULT_MAX_CHARS 就截断
      - 整体兜底：序列化后超过 TOOL_RESULT_MAX_CHARS 再全局截一次
    """
    if isinstance(result, dict):
        # base64 识别：浏览器截图/文件读取常见
        data = result.get("data")
        if isinstance(data, str) and len(data) > 4000 and all(
            c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n" for c in data[:200]
        ):
            # 疑似 base64（截图）：只保留前缀 + 总长度说明
            truncated_data = data[:BASE64_KEEP_HEAD] + f"...[base64 截断，原始长度 {len(data)} 字符]"
            result = {**result, "data": truncated_data}
            # 有 file 字段（截图落盘）时，附加说明让 agent 知道图片在哪
            shot_file = result.get("file")
            if shot_file:
                result["_note"] = (
                    f"截图已保存到: {shot_file}（用 file.read(path='{shot_file}') 可读取图片二进制，"
                    f"或直接打开查看）。完整 base64 已截断，不在对话历史里重复。"
                )
            else:
                result["_note"] = "截图 base64 已截断，如需完整数据请重新 screenshot 并自行保存。"
        elif isinstance(data, str) and len(data) > TOOL_RESULT_MAX_CHARS:
            result = {**result, "data": data[:TOOL_RESULT_MAX_CHARS] + f"...[截断，原始 {len(data)} 字符]"}
        # html 字段兜底
        html = result.get("html")
        if isinstance(html, str) and len(html) > TOOL_RESULT_MAX_CHARS:
            result = {**result, "html": html[:TOOL_RESULT_MAX_CHARS] + f"...[截断，原始 {len(html)} 字符]"}

    result_str = json.dumps(result, ensure_ascii=False)
    if len(result_str) > TOOL_RESULT_MAX_CHARS:
        result_str = result_str[:TOOL_RESULT_MAX_CHARS] + f"...[整体结果截断，原始 {len(result_str)} 字符]"
    return result_str


def _truncate_history_content(content: str | None, max_chars: int = HISTORY_MSG_MAX_CHARS) -> str:
    """单条历史消息内容兜底截断，防止数据库里已存的长消息重新爆掉 context。"""
    if not isinstance(content, str):
        return content  # type: ignore[return-value]
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"...[历史消息截断，原始 {len(content)} 字符]"


def _sanitize_tool_arguments(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """清洗 LLM 生成的 tool 参数。

    LLM 常在字符串值里混进 markdown 符号、尾部中文、反引号等：
      - URL 被 `` ` `` 包裹 → Playwright page.goto() 会报 "invalid URL"
      - URL 后跟中文说明 → 整段粘进去
      - selector 里夹反引号

    统一清洗后，再交给 command/plugin 执行。对非字符串值原样保留。
    """
    import re as _re

    if not isinstance(args, dict):
        return args

    # 需要重点清洗的字段名（URL/selector 类）
    _URL_FIELDS = {"url", "link", "href", "target_url", "download_url"}
    _SELECTOR_FIELDS = {"selector", "selectors", "css", "xpath"}

    def _clean_str(val: str, field: str) -> str:
        s = val.strip()
        # 1) 去掉首尾反引号 ` `  `` ` ``
        s = _re.sub(r'^`{1,3}|`{1,3}$', '', s).strip()
        if field in _URL_FIELDS:
            # 2) URL 专用：从字符串中抽出第一个合法 http(s) URL
            #    合法 URL 字符 = [A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]
            m = _re.search(r'(https?://[A-Za-z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]+)', s)
            if m:
                s = m.group(1)
        elif field in _SELECTOR_FIELDS:
            # 3) selector 专用：去掉首尾可能混进来的中文说明
            s = _re.sub(r'[\u4e00-\u9fff].*$', '', s).strip()
        # 4) 通用：去掉首尾多余空白
        return s.strip()

    cleaned: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and v and k.lower() in (_URL_FIELDS | _SELECTOR_FIELDS | {"text", "js", "code", "query"}):
            cleaned[k] = _clean_str(v, k.lower())
        elif isinstance(v, str) and v:
            # 其他字符串也轻量清洗（去首尾反引号 + trim）
            cleaned[k] = _re.sub(r'^`{1,3}|`{1,3}$', '', v).strip()
        else:
            cleaned[k] = v
    return cleaned


def _max_tool_rounds(agent: dict | None = None) -> int:
    """工具调用最大轮数：智能体自定义 > 全局 settings.max_tool_rounds > 默认 50（最小 1）。"""
    try:
        if agent and agent.get("max_tool_rounds"):
            return max(1, int(agent["max_tool_rounds"]))
        v = int((load("settings") or {}).get("max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS))
        return max(1, v)
    except Exception:
        return DEFAULT_MAX_TOOL_ROUNDS


# ===== 并行模式下的模型分配器 =====
# 同一家 API endpoint 下同一个 model name 视为一个"模型槽"——一个槽同一时刻
# 只能给一个 agent 用（接口后端是单实例 / 同模型名并发冲突）。
# 串行模式全部 agent 直接走 default_model，不做任何分配。
_MODEL_KEY_LOCK = asyncio.Lock()                                 # 保护占用表的原子操作
# 占用表: (interface_url, model_name) → set[owner_tag]
# owner_tag 形式为 "<conversation_id>|<agent_id>|<asyncio_task_id>"，保证不同 task 互不干扰
_MODEL_OCCUPANCIES: dict[tuple[str, str], set[str]] = {}


async def _occupy_model(agent: dict, interface: dict | None, owner_tag: str,
                         canvas: dict | None) -> str:
    """并行模式下等待 → 占用一个模型槽。

    - 串行模式：直接算好 default_model 返回，不做任何登记。
    - 并行模式：在 Lock 内从 interface.models[] 里选一个当前空闲的槽，登记后返回。
      如果所有备选都被占，则轮询等待（每次等 0.2s 后重新判断）。

    上层（agent_reply 的 finally 块）必须调用 _release_model(owner_tag) 释放。
    释放动作**不依赖返回值**，只靠 owner_tag 从全局占用表里删掉。"""
    exec_mode = (canvas or {}).get("execution_mode", "serial") if canvas else "serial"

    # 先算 default_model（尊重 agent.model 覆盖）
    default_model = (agent.get("model")
                     or (interface or {}).get("default_model")
                     or "gpt-4o-mini")

    # agent 自己指定了 model → 不做抢占分配
    if agent.get("model") or exec_mode != "parallel":
        if exec_mode != "parallel":
            # 串行：直接返回，不登记
            return default_model
        # 并行但指定了 model → 走下面的排队登记逻辑（确保独占）
        ordered = [default_model]
    else:
        # —— 并行模式，未指定 agent.model：从 interface.models[] 里挑一个空闲槽 ——
        models_list = (interface or {}).get("models") or []
        if not models_list:
            models_list = [default_model]
        ordered: list[str] = []
        if default_model in models_list:
            ordered.append(default_model)
        for m in models_list:
            if m not in ordered:
                ordered.append(m)

    url = (interface or {}).get("url") or "_"
    while True:
        async with _MODEL_KEY_LOCK:
            for m in ordered:
                key = (url, m)
                holders = _MODEL_OCCUPANCIES.setdefault(key, set())
                # 自己可能已在占用表里（嵌套 relay 同 task 重入），视为空闲可重占
                if not holders or holders == {owner_tag}:
                    holders.add(owner_tag)
                    return m
        # 所有备选都被占 → 等释放后重检
        await asyncio.sleep(0.2)


def _release_all_for_owner(owner_tag: str) -> None:
    """finally 里释放 owner_tag 占用的所有模型槽（可能跨多个接口/模型）。"""
    for key, holders in list(_MODEL_OCCUPANCIES.items()):
        if owner_tag in holders:
            holders.discard(owner_tag)
            if not holders:
                _MODEL_OCCUPANCIES.pop(key, None)


# ===== 后台 receiver 任务登记（防"前端已结束、后台继续跑"的孤儿任务泄漏）=====
# 并行模式下用 asyncio.create_task 启动的接收方回复，只有在正常流程末尾被 await；
# 一旦客户端断连/点停止/生成器抛异常，局部 parallel_tasks 列表丢失，这些 fire-and-forget
# 任务会继续跑 LLM/工具/落库甚至递归 spawn。把它们登记到 turn["_child_tasks"] 共享集合
# （child_turn = dict(turn) 是浅拷贝，集合对象全程共享），根 agent_reply 退出时统一取消。


def _register_child_task(turn: dict | None, task: asyncio.Task) -> None:
    """把一个后台 receiver 任务登记到 turn 共享集合，便于根调用退出时统一取消。"""
    if turn is None:
        return
    pool: set = turn.setdefault("_child_tasks", set())
    pool.add(task)

    def _on_done(_t: asyncio.Task) -> None:
        pool.discard(_t)

    task.add_done_callback(_on_done)


def _cancel_pending_child_tasks(turn: dict | None) -> None:
    """取消 turn 中所有尚未完成的后台 receiver 任务（根 agent_reply finally 调用）。

    task.cancel() 是同步操作，在生成器 finally（含 GeneratorExit 路径）里安全可用，
    无需 await。被取消的子任务其 CancelledError 会沿 await 链向上传，中断 httpx 流式
    LLM 调用与工具执行；子任务若再 spawn 过子子任务，同样登记在同一集合，一并取消。"""
    if not turn:
        return
    pool: set = turn.get("_child_tasks") or set()
    for t in list(pool):
        if not t.done():
            t.cancel()


def _agent_tools(agent: dict) -> list:
    return filter_tools(agent.get("commands", []), agent.get("plugins", []), PLUGIN_REGISTRY)


def _tool_capability_brief(tools: list) -> str:
    """把 tools 渲染成 system prompt 里的精简能力清单：工具名 + 一句话描述（不注入参数细节）。"""
    lines: list[str] = []
    for t in tools or []:
        fn = t.get("function") or {}
        name = fn.get("name") or ""
        desc = (fn.get("description") or "").strip().replace("\n", " ")
        # 只取第一句/截断作一句话描述
        for sep in ("。", "！", "？", "."):
            if sep in desc:
                cut = desc.split(sep, 1)[0]
                if len(cut) >= 8:
                    desc = cut
                    break
        if len(desc) > 80:
            desc = desc[:77] + "..."
        lines.append(f"- {name}：{desc or '（无描述）'}")
    return "\n".join(lines)


def _rules_for_agent(agent_id, canvas_id=None) -> list[str]:
    """取应注入该智能体的规章制度内容（按画布隔离）。
    - canvas_id 为 None → 不注入（独立对话无画布集群）
    - rule.canvas_id 必须等于当前画布
    - rule.agent_ids 为 None → 默认注入该画布所有；为列表 → 仅选中的智能体
    """
    if canvas_id is None:
        return []
    try:
        rules = load("rules")
    except Exception:
        return []
    out: list[str] = []
    for r in rules:
        if r.get("canvas_id") != canvas_id:
            continue
        if not r.get("enabled", True):
            continue
        content = (r.get("content") or "").strip()
        if not content:
            continue
        ids = r.get("agent_ids")
        if ids is None:
            out.append(content)
        elif agent_id is not None and agent_id in ids:
            out.append(content)
    return out


def _system_prompt(agent: dict, tools: list, cluster_context: str = "", canvas_id=None, canvas: dict | None = None, wd_probe=None) -> str:
    parts = [agent.get("system_prompt") or "你是一个有用的智能体。"]
    agent_name = agent.get("name", "智能体")
    if agent_name: parts.append(f"\n你的名字: {agent_name}")

    # 工作区文件夹（画布级属性，independent / linked 模式都注入）
    if canvas and canvas.get("workdir"):
        wd = canvas["workdir"]
        dir_ok = os.path.isdir(wd)
        probe_note = ""
        if wd_probe:
            ok, note = wd_probe
            if ok:
                probe_note = "\n- ✅ 系统刚执行过写权限探测，**本进程可正常写入**该目录——无需怀疑权限，直接写。"
            else:
                probe_note = f"\n- ❌ 系统写权限探测失败：{note}\n  不要继续尝试写入了，请立刻告知用户工作区没有写权限。"
        parts.append(
            f"\n## 工作区文件夹（硬性约束，必须遵守）\n"
            f"当前画布绑定了工作区：`{wd}`\n"
            f"- 所有 file.read / file.write / file.structure / cmd.run 会以它为基准解析相对路径；"
            f"- 保存文件**一律用相对路径**，例如 file.write(path='report.md', content='...') "
            f"会写入 `{wd}\\report.md`；\n"
            f"- ⚠️ **禁止写入 backend/、frontend/、AppData/、node_modules/ 等系统目录**——"
            f"即使你手搓了绝对路径也会污染项目，请始终用相对路径让文件自动落在工作区内；"
            f"- 不要在后端目录下生成任何产出物，所有产出集中在 `{wd}` 方便用户统一查看。"
            + ("\n- 系统已自动创建此目录。" if not dir_ok else "")
            + probe_note
        )
    else:
        # 无 workdir 的兜底提示（独立对话模式）
        probe_note = ""
        if wd_probe:
            ok, note = wd_probe
            if ok:
                probe_note = "\n- ✅ 系统探测到一个默认工作区可用。"
            else:
                probe_note = f"\n- ❌ 默认工作区写权限探测失败：{note}"
        parts.append(
            "\n## 文件输出规则（硬性约束）\n"
            "- 禁止写入 backend/、frontend/、AppData/ 等系统目录——"
            "这些是项目源码和数据库，不能被产出物污染；\n"
            "- 若需要保存文件，请先用绝对路径指定一个清晰的用户目录，"
            "或等待画布绑定工作区后再使用相对路径。"
            + probe_note
        )
    # 规章制度（画布顶部「📋 规章制度」维护；按当前画布隔离，默认全员，可限定智能体）
    rules = _rules_for_agent(agent.get("id"), canvas_id)
    if rules:
        numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(rules, 1))
        parts.append(f"\n## 规章制度（集群工作流程与规则，必须遵守）\n{numbered}")
    # 技能 prompt 拼接（如果有）
    skills = agent.get("skills", [])
    if skills:
        from skills import SKILL_REGISTRY
        skill_prompts = []
        skill_index = []
        for s in skills:
            meta = SKILL_REGISTRY.get(s)
            if not meta:
                continue
            if meta.get("description"):
                skill_index.append(f"- {meta.get('label') or s}：{meta['description']}")
            if meta.get("prompt"):
                body = meta["prompt"]
                # 能力参数预设：技能的可选参数以文字描述附加进技能提示，仅供模型参考
                try:
                    from core.param_presets import get_optional_desc
                    optional = get_optional_desc("skill", s)
                    if optional:
                        extra = ("\n\n【能力参数预设 · 可选参数（仅供模型参考选择）】\n"
                                 + "\n".join(f"- {k}：{v}" for k, v in optional.items()))
                        body = body + extra
                except Exception:
                    pass
                skill_prompts.append(f"--- 技能：{meta.get('label') or s} ---\n{body}")
        if skill_index:
            parts.append("\n## 技能索引（任务相关时优先考虑加载/遵循对应技能）\n" + "\n".join(skill_index))
        if skill_prompts:
            parts.append("\n## 你加载了以下技能，请结合使用：\n" + "\n\n".join(skill_prompts))
    # 工具能力：完整清单 + 主动调用规则（按任务自动选工具，不等用户点名）
    if tools:
        capability = _tool_capability_brief(tools)
        tool_names = [t["function"]["name"] for t in tools]
        parts.append(
            "\n## 可用工具（已注入为 function calling — 必须按需直接调用）\n"
            "你拥有下列工具。遇到需要它们才能更好完成的任务时，"
            "**直接发起 function calling**，不要先声明'我将调用…'、不要说'我可以…'、"
            "不要用自然语言描述你准备做什么——直接调用。\n"
            "优先顺序：拿到用户输入 → 判断是否需要某个工具 → 发起 function calling → "
            "拿到结果 → 继续；不要跳过工具先给结论、不要等用户点名工具。\n"
            f"可用工具：{', '.join(tool_names)}\n"
            f"能力清单：\n{capability}\n"
            "调用约定：用 function calling 调用（正文不要输出 JSON）；"
            "收到工具结果后基于结果继续；一轮可连续多轮调用。"
        )
    elif not tools:
        parts.append("\n## 能力\n你当前没有额外工具，直接基于角色知识与对话上下文回答即可。")
    if cluster_context:
        parts.append(f"\n【集群上下文】{cluster_context}")
    return "\n".join(parts)


def _execute_tool(name: str, arguments: dict,
                  conv_id: int | None = None,
                  tool_call_id: str | None = None,
                  agent_id: int | None = None) -> dict:
    """根据 name 分发到 command 或 plugin。
    命令以 COMMAND_META 为准（file.* / cmd.run / cluster.*，随 AppData v2 扩展），
    不再用硬编码白名单，避免新增命令（如 cluster.*）被误发到插件系统。

    conv_id / tool_call_id / agent_id 可选：传入时在执行前注入 commands.BACKUP_CTX，
    让 file.* 写/删命令自动做快照备份。"""
    import re

    def _fix_bare_identifiers(raw: str) -> str:
        """把 JSON 里的裸标识符值修复成字符串。
        例：{"to_agent": 总经理, "x": 123} → {"to_agent": "总经理", "x": 123}
        策略：匹配 "key": 后面紧跟的、非引号非数字非 true/false/null 的标识符，加引号。
        """
        # 匹配模式：冒号 + 可选空格 + (非引号/非数字开头/非 null/true/false) + 逗号或 }
        pattern = r'''("(?:[^"\\]|\\.)*"\s*:\s*)(?!")(?![0-9{\[]|null|true|false\b)([\u4e00-\u9fff\w]+)'''
        return re.sub(pattern, r'\1"\2"', raw)

    # 注入备份上下文（如果三个都有）
    backup_token = None
    if conv_id is not None and tool_call_id and agent_id is not None:
        from core.commands import BACKUP_CTX
        backup_token = BACKUP_CTX.set({"conv_id": conv_id, "tool_call_id": tool_call_id, "agent_id": agent_id})

    try:
        # 剥掉 _raw 兜底键
        raw_args = arguments.pop("_raw", None) if isinstance(arguments, dict) else None
        if raw_args and not arguments:
            # JSON 解析失败 → 尝试三重解析
            arguments = None
            # 1) 直接 json.loads
            try:
                arguments = json.loads(raw_args)
            except Exception:
                pass
            # 2) 裸标识符修复后 json.loads
            if arguments is None:
                try:
                    fixed = _fix_bare_identifiers(raw_args)
                    arguments = json.loads(fixed)
                except Exception:
                    pass
            # 3) 还是解不了 → 友好错误
            if arguments is None:
                # 尝试再 fix 一次转义引号问题
                try:
                    fixed2 = _fix_bare_identifiers(raw_args).replace("\\", "\\\\")
                    arguments = json.loads(fixed2)
                except Exception:
                    return {"error": (
                        f"tool arguments 解析失败，原始参数：{raw_args[:300]}，"
                        f"请确保 JSON 格式合法（字符串值必须用双引号包裹，如 \"to_agent\": \"总经理\"）"
                    )}

        try:
            arguments = _sanitize_tool_arguments(name, arguments or {})
            if name in COMMAND_META:
                return run_command(name, **(arguments or {}))
            # 插件执行：注入能力参数预设的固定参数（模型不可见、不可改，固定值优先覆盖）
            from core.param_presets import get_fixed_values
            fixed = get_fixed_values("plugin", name)
            if fixed:
                arguments = {**(arguments or {}), **fixed}
            return run_plugin(name, **(arguments or {}))
        except TypeError as e:
            return {"error": f"tool {name} 参数不匹配: {e}，已收到参数: {list((arguments or {}).keys())}，请修正后重试"}
        except Exception as e:
            return {"error": f"tool {name} 执行异常: {e}"}
    finally:
        if backup_token is not None:
            from core.commands import BACKUP_CTX
            BACKUP_CTX.reset(backup_token)


# —— 自动回复分类器（轻量 LLM）：判断被联系智能体的正文是否为给发送方的直接回复 ——
_AUTO_REPLY_SYSTEM = (
    "你是消息类型分类器。判断「接收方智能体 B 的回复正文」是否属于给「发送方智能体 A」的直接回复。\n"
    "- reply：B 的正文是针对 A 的消息的直接回答或回应（回答问题、回应观点、确认收到、感谢、"
    "澄清、向 A 汇报进展等，期望 A 知晓或继续对话）。\n"
    "- output：B 的正文是任务产出/交付物/转交给其他成员的成果，不是给 A 的直接回复"
    "（例如完成的代码、报告文档、转给 C 的内容，A 无需知晓）。\n"
    "只输出 JSON：{\"kind\": \"reply\" 或 \"output\", \"confidence\": 0-1 浮点数}"
)


async def _should_auto_reply(interface: dict, model: str, sender_message: str,
                             reply_text: str, sender_name: str) -> bool:
    """判断被联系智能体的正文是否为给发送方的直接回复。
    True=正文应自动回传发送方；False=不回传（任务产出/转发给其他成员）。
    默认 False（保守：A 可能只是派发任务，避免把任务产出误发给 A）。"""
    try:
        messages = [
            {"role": "system", "content": _AUTO_REPLY_SYSTEM},
            {"role": "user", "content": (
                f"【发送方 {sender_name} 的消息】\n{(sender_message or '')[:1500]}\n\n"
                f"【接收方 B 的回复正文】\n{(reply_text or '')[:1500]}"
            )},
        ]
        async for kind, payload in chat(interface, model, messages, stream=False):
            if kind == "message":
                content = (payload.get("content") or "").strip()
                try:
                    if content.startswith("```"):
                        content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
                    parsed = json.loads(content)
                    kind_r = str(parsed.get("kind", "output")).strip().lower()
                    confidence = float(parsed.get("confidence", 0.0))
                    if kind_r == "reply" and confidence >= 0.6:
                        return True
                    return False
                except Exception:
                    lower = content.lower()
                    return "reply" in lower and "output" not in lower
    except Exception:
        pass
    return False


def _agent_on_canvas_edges(canvas: dict, agent: dict) -> bool:
    """该智能体在画布上是否有连线（任一入边/出边）——有连线才具备 A2A 交接能力。"""
    node_ids = {n.get("id") for n in canvas.get("nodes", []) if n.get("agent_id") == agent.get("id")}
    if not node_ids:
        return False
    return any(e.get("from_node") in node_ids or e.get("to_node") in node_ids
               for e in canvas.get("edges", []))


def _call_neighbors(canvas: dict, agent: dict) -> tuple[list[dict], list[dict]]:
    """画布上与该智能体有连线（任一方向）且启用的邻居。
    返回 (can_send, can_receive)：call 双向连通 → 收发集合相同，但按方向边拆分展示。
    - can_send: 出边下游 ∪ 入边上游（_agents_connected 任一方向即可 call）
    - can_receive: 入边上游 ∪ 出边下游（同上，对称）"""
    aid = agent.get("id")
    my_nodes = {n.get("id") for n in canvas.get("nodes", []) if n.get("agent_id") == aid}
    if not my_nodes:
        return [], []
    all_agents = {a.get("id"): a for a in (load("agents") or [])}
    nmap = {n.get("id"): n for n in canvas.get("nodes", [])}

    out_ids: set = set()
    in_ids: set = set()
    for e in canvas.get("edges", []):
        if e.get("from_node") in my_nodes:
            tn = nmap.get(e.get("to_node"))
            if tn and tn.get("agent_id") and tn.get("agent_id") != aid:
                out_ids.add(tn.get("agent_id"))
        if e.get("to_node") in my_nodes:
            fn = nmap.get(e.get("from_node"))
            if fn and fn.get("agent_id") and fn.get("agent_id") != aid:
                in_ids.add(fn.get("agent_id"))

    def _peers(ids: set) -> list[dict]:
        peers = []
        for pid in sorted(ids):
            a = all_agents.get(pid)
            if a and a.get("enabled") is not False:
                peers.append(a)
        return peers

    # call 权限 = 任一方向连通（与 _agents_connected 一致）
    both = out_ids | in_ids
    return _peers(both), _peers(both)


def _format_member(a: dict, my_id=None) -> str:
    """精简的集群成员展示：名字（agent_id=X）+ 一行简述（description 或 system_prompt 首句 ≤ 50 字）。"""
    name = a.get("name", "?")
    aid = a.get("id")
    brief = ""
    desc = (a.get("description") or "").strip()
    sp = (a.get("system_prompt") or "").strip().split("\n")[0]
    src = desc or sp
    if src:
        brief = src[:50] + ("…" if len(src) > 50 else "")
    me = "（← 你自己）" if (my_id is not None and aid == my_id) else ""
    disabled = "【已停用，不参与沟通】" if a.get("enabled") is False else ""
    tail = f" — {brief}" if brief else ""
    return f"- {name}（agent_id={aid}）{me}{disabled}{tail}"


def _directed_neighbors(canvas: dict, agent: dict) -> dict:
    """按连线方向分类邻居：
    - mutual:   双向连线（两个方向 edge 都存在）
    - outgoing: 仅你出边指向的（只有 from_agent→对方 的 edge）
    - incoming: 仅对方出边指向你的（只有 对方→from_agent 的 edge）
    以上三类合并即可 call（任一方向连通即双向消息权）。
    """
    aid = agent.get("id")
    my_nodes = {n.get("id") for n in canvas.get("nodes", []) if n.get("agent_id") == aid}
    if not my_nodes:
        return {"mutual": [], "outgoing": [], "incoming": []}
    all_agents = {a.get("id"): a for a in (load("agents") or [])}
    nmap = {n.get("id"): n for n in canvas.get("nodes", [])}

    out_nodes = set()
    in_nodes = set()
    for e in canvas.get("edges", []):
        if e.get("from_node") in my_nodes:
            tn = nmap.get(e.get("to_node"))
            if tn and tn.get("agent_id") and tn.get("agent_id") != aid:
                out_nodes.add(tn.get("agent_id"))
        if e.get("to_node") in my_nodes:
            fn = nmap.get(e.get("from_node"))
            if fn and fn.get("agent_id") and fn.get("agent_id") != aid:
                in_nodes.add(fn.get("agent_id"))

    mutual_ids = out_nodes & in_nodes
    out_only = out_nodes - mutual_ids
    in_only = in_nodes - mutual_ids

    def _peers(ids: set) -> list[dict]:
        peers = []
        for pid in sorted(ids):
            a = all_agents.get(pid)
            if a and a.get("enabled") is not False:
                peers.append(a)
        return peers

    return {
        "mutual": _peers(mutual_ids),
        "outgoing": _peers(out_only),
        "incoming": _peers(in_only),
    }


def _build_cluster_context(canvas: dict, agent: dict) -> tuple[str, dict]:
    """集群对话上下文：按 perception_scope 决定感知范围，精简提示词，清晰区分沟通权限。
    返回 (context_text, {agent_id: name})，后者用于给历史消息加发言者前缀。"""
    all_agents = {a.get("id"): a for a in (load("agents") or [])}
    nodes = canvas.get("nodes", [])
    my_id = agent.get("id")
    perception = agent.get("perception_scope", "link")
    speaker_names: dict[int, str] = {}

    # —— 步骤 1：选感知到的成员 ——
    if perception == "global":
        # 全局感知：画布上所有启用成员
        visible = [all_agents[n.get("agent_id")] for n in nodes if n.get("agent_id") and all_agents.get(n.get("agent_id"))]
        visible = [a for a in visible if a.get("enabled") is not False]
        seen_ids = {a.get("id") for a in visible}
    else:
        # link 感知：仅连线邻居（任一方向）+ 自己
        nd = _directed_neighbors(canvas, agent)
        link_ids = {a.get("id") for a in nd["mutual"] + nd["outgoing"] + nd["incoming"]}
        seen_ids = link_ids | {my_id}
        visible = [a for a in all_agents.values() if a.get("id") in seen_ids]

    # 自己单独处理（始终感知自己）
    me = all_agents.get(my_id)
    if me and me not in visible:
        visible = [me] + visible

    # 去重 + 保持顺序（画布 nodes 顺序，self 在前）
    ordered = []
    added_ids = set()
    if me:
        ordered.append(me)
        added_ids.add(my_id)
    for n in nodes:
        aid = n.get("agent_id")
        if aid in seen_ids and aid not in added_ids:
            a = all_agents.get(aid)
            if a:
                ordered.append(a)
                added_ids.add(aid)
            elif aid not in added_ids:
                # 补一个占位 agent
                ordered.append({"id": aid, "name": f"?{aid}"})
                added_ids.add(aid)

    for a in ordered:
        speaker_names[a.get("id")] = a.get("name", "?")

    # —— 步骤 2：沟通权限（基于全部连线，不管感知范围）——
    nd = _directed_neighbors(canvas, agent)
    bidirectional = nd["mutual"]                    # 双向连线（A→B + B→A 都存在）
    outgoing = nd["outgoing"]                       # 仅我有出边指向对方（A→B 只有这一条）
    incoming = nd["incoming"]                       # 仅对方有出边指向我（B→A 只有这一条）
    # 我能主动发 call 的：双向 + 仅出边指向的
    can_initiate = bidirectional + outgoing
    link_ids = {a.get("id") for a in bidirectional + outgoing + incoming}
    # 画布上的全部 agent（含已停用）
    all_on_canvas = {all_agents[n.get("agent_id")].get("id"): all_agents[n.get("agent_id")]
                     for n in nodes if n.get("agent_id") and all_agents.get(n.get("agent_id"))}

    # 不可 call 但在画布上可见（global 模式）或完全不可见（link 模式）
    not_linked_visible = []  # global 模式：画布上可见但无连线 → 不能 call
    invisible = []           # link 模式：画布上存在但不可见（无连线）→ 只存在于画布上但你看不到
    if perception == "global":
        invisible = []
        not_linked_visible = [a for a in all_on_canvas.values()
                              if a.get("id") != my_id and a.get("enabled") is not False
                              and a.get("id") not in link_ids]
    else:
        invisible = [a for a in all_on_canvas.values()
                     if a.get("id") != my_id and a.get("enabled") is not False
                     and a.get("id") not in seen_ids]
        not_linked_visible = []

    # —— 步骤 3：组装精简提示词 ——
    scope_label = "仅连线邻居" if perception == "link" else "画布全体"
    member_lines = [_format_member(a, my_id) for a in ordered]

    bidir_names = [a.get("name") for a in bidirectional]
    out_names = [a.get("name") for a in outgoing]
    in_names = [a.get("name") for a in incoming]

    parts = [
        f"你正在画布「{canvas.get('name')}」中以集群模式工作，当前感知范围：{scope_label}。",
        f"画布共 {len(nodes)} 个智能体，你当前可感知 {len(ordered)} 个。",
        "【你可感知的成员】\n" + ("\n".join(member_lines) if member_lines else "- （画布暂无成员）"),
    ]

    # 沟通权限（核心：严格按连线方向区分）
    if link_ids:
        disc_lines = []
        if bidir_names:
            disc_lines.append(
                f"- 双向沟通（既有你→对方也有对方→你，发送消息权限双向，可互相发消息）："
                f"{', '.join(bidir_names)}"
            )
        if out_names:
            disc_lines.append(
                f"- 单向出边（仅你→对方，你可主动发 cluster.send_message 给对方，但对方不能主动发你，"
                f"对方只能等你发消息后回复）：{', '.join(out_names)}"
            )
        if in_names:
            disc_lines.append(
                f"- 单向入边（仅对方→你，你**不能**主动发 cluster.send_message 给对方，"
                f"只能等对方发消息过来后再回复）：{', '.join(in_names)}"
            )
        names_can_initiate = [a.get("name") for a in can_initiate]
        parts.append(
            "【cluster.send_message 权限（to_agent 只能填以下列出的名字或 agent_id）】\n"
            + "\n".join(disc_lines)
            + (f"\n→ 你可**主动发起** call 的成员：{', '.join(names_can_initiate) or '（无）'}")
        )
    else:
        parts.append("【cluster.send_message 权限】你在画布上没有任何连线，无法与其他成员消息。")

    # —— 协作沟通（仅 cluster.send_message 工具，不再暴露正文 @提及 快捷通道 ——
    #    原因：@提及 会被自动并入 cluster.send_message 队列，与显式调用混用导致 message 字段
    #    承载整段回复，破坏 "call=结构化咨询 / 正文=给用户的回复" 的语义边界）——
    if link_ids:
        parts.append(
            "【协作沟通（cluster.send_message）】与邻居协作/咨询/澄清 → 直接调用 cluster.send_message(to_agent, message)，"
            "message 字段只写要发给对方的核心问题/信息，不要把整段回复都塞进去。"
            "对方也可用 cluster.send_message 回复你，支持往返多轮。"
            "打招呼/寒暄/用户直接问你简单问题 → 直接输出文字回复，不要调用 cluster.send_message。"
        )

    # 不可 call 的（global 模式可见但不能 call / link 模式不可见）
    if perception == "global" and not_linked_visible:
        names = ", ".join(a.get("name") for a in not_linked_visible)
        parts.append(
            f"【画布上存在但与你无连线（可看到但不能 call）】{names}"
        )
    elif perception == "link" and invisible:
        names = ", ".join(a.get("name") for a in invisible)
        parts.append(
            f"【画布上存在但你不可感知（无连线）】{names}"
        )

    # 对话历史里带 [名字] 前缀（程序自动添加，标识发言者）
    parts.append(
        "对话历史里带 [成员名] 前缀的发言来自对应成员——该前缀由系统自动添加，仅用于标识发言者身份。"
        "你回复时**禁止**在正文开头自行添加 [角色名] 之类的前缀（如 [游戏项目总经理]），"
        "直接输出回复正文即可，系统会自动标注你的身份。"
        "不要声称无法感知其他智能体。"
    )

    # —— A2A 协议说明 ——
    if _agent_on_canvas_edges(canvas, agent):
        if A2A_HANDOFF_ENABLED:
            parts.append(
                "【A2A 协议】你可用两类沟通工具：\n"
                "· cluster.send_message — 多轮沟通/咨询（kind=message）：向连线成员提问、澄清、共同推敲；"
                "对方可 call 回你往返。按需直接调用。\n"
                "· cluster.handoff — 成果移交（kind=handoff）：有明确成果需下游接力时使用；"
                "用户直接向你提问时不要调用。\n"
                "规则：打招呼/寒暄/用户直接问你简单问题 → 直接输出文字回复用户，不要调用沟通工具。"
            )
        else:
            parts.append(
                "【cluster.send_message 使用说明】成果移交协议已暂时停用，你拥有 cluster.send_message 工具：\n"
                "向连线成员提问、澄清、共同推敲任务时，直接调用 cluster.send_message(to_agent, message)；"
                "对方也可用它回复你，支持往返多轮。\n"
                "打招呼/寒暄/用户直接问你简单问题 → 直接输出文字回复用户，不要调用沟通工具。"
            )
    else:
        parts.append(
            "【沟通权限】你在当前画布上没有连线，无法使用 cluster.send_message。"
        )
    return "\n".join(p for p in parts if p), speaker_names


async def _drain_agent_reply(conversation_id: int, target: dict, input_text: str,
                             mode: str, canvas: dict | None, turn: dict) -> list[str]:
    """并行模式辅助：运行 agent_reply 并收集所有输出行到列表（供主生成器后续 yield）。"""
    lines: list[str] = []
    try:
        async for line in agent_reply(conversation_id, target, input_text, mode, canvas, turn):
            lines.append(line)
    except Exception as e:
        lines.append(f"RN:（并行接收方 {target.get('name', '?')} 异常：{e}）\n")
    return lines


async def agent_reply(conversation_id: int, agent: dict, user_input: str, mode: str = "independent", canvas: dict | None = None, turn: dict | None = None):
    """让智能体回复，以 async generator 输出；最终结果会自动入库。
    链首调用创建本轮 A2A turn（visited/handoffs 等共享状态），递归接力时复用传入的 turn。
    is_relay=True 表示本段是接力链（下游），入库消息带 relay:true 供前端聊天区过滤。"""
    is_relay = turn is not None
    if turn is None:
        turn = {
            "visited": set(), "handoffs": [], "calls": [], "call_count": 0,
            "skip_auto_ids": set(), "relayed": 0,
            "current_agent_id": agent.get("id"),
            "all_agents": {a.get("id"): a for a in (load("agents") or [])},
            "canvas": canvas,
            "reply_to_msg_id": None, "reply_to_from": None,
            "relay_depth": 0,   # 防递归爆炸：只有链首能触发 relay
            "_child_tasks": set(),  # 共享的后台 receiver 任务池（根退出时统一取消）
        }
    else:
        turn["relay_depth"] = turn.get("relay_depth", 0) + 1
        if turn["relay_depth"] > A2A_MAX_RELAY_DEPTH:
            # 超过最大接力深度直接退出递归，防止 A→B→A→B 无限往返
            return
    turn["current_agent_id"] = agent.get("id")
    token = TURN_CTX.set(turn)

    # —— 注入 WORKDIR_CTX：画布有 workdir 则优先使用，否则让 commands.py 回落到 settings.default_workdir ——
    _workdir = (canvas or {}).get("workdir") if canvas else None
    _wd_probe = None  # (ok: bool, note: str)
    if _workdir:
        try:
            os.makedirs(_workdir, exist_ok=True)
        except Exception as e:
            _wd_probe = (False, f"目录创建失败: {e}")
        if os.path.isdir(_workdir):
            try:
                # 快速探测：写一个临时字节再删除
                import tempfile, uuid
                probe_file = os.path.join(_workdir, f".write_probe_{uuid.uuid4().hex[:8]}.tmp")
                with open(probe_file, "w", encoding="utf-8") as pf: pf.write("ok")
                os.remove(probe_file)
                _wd_token = WORKDIR_CTX.set(_workdir)
                _wd_probe = (True, "")
            except Exception as e:
                _wd_token = None
                _wd_probe = (False, f"写权限探测失败: {e}")
        else:
            _wd_token = None
    else:
        _wd_token = None

    # 把 probe 结果传递给 _system_prompt（通过 turn 共享上下文，不污染 canvas/store）
    turn["_wd_probe"] = _wd_probe

    # —— 并行模式下的模型分配：每个 agent 占用一个不同的模型槽 ——
    cur_task = asyncio.current_task()
    owner_tag = f"{conversation_id}|{agent.get('id')}|{id(cur_task)}"
    interface = get_by("model_interfaces", agent.get("interface_id")) or (load("model_interfaces") or [{}])[0]
    try:
        selected_model = await _occupy_model(agent, interface, owner_tag, canvas)
    except Exception:
        selected_model = agent.get("model") or interface.get("default_model") or "gpt-4o-mini"
    turn["_model"] = selected_model
    turn["_model_interface"] = interface

    try:
        try:
            async for line in _agent_reply_inner(conversation_id, agent, user_input, mode, canvas, turn, is_relay):
                yield line
        except Exception as e:
            # 兜底：_agent_reply_inner 里如果有漏网异常（非 chat 层，比如 upsert 失败），
            # 也要把错误传出去 + DONE，保证 SSE 流正常结束
            yield f"E:❌ 运行时异常：{e}\n"
            yield "DONE\n"
    finally:
        TURN_CTX.reset(token)
        if _wd_token is not None:
            WORKDIR_CTX.reset(_wd_token)
        # 释放本 agent 占用的所有模型槽
        _release_all_for_owner(owner_tag)
        # 根调用（非接力段）：客户端断连 / 点停止 / 异常退出时，统一取消本轮 spawn 的
        # 所有后台 receiver 任务，避免"前端已显示运行完毕、后台仍在跑 LLM/工具"的泄漏。
        # 正常完成时这些任务均已 done()，cancel 是 no-op；接力段的子任务与根共享同一
        # turn["_child_tasks"] 集合（dict 浅拷贝共享同一 set 对象），一并被取消。
        if not is_relay:
            _cancel_pending_child_tasks(turn)


async def _agent_reply_inner(conversation_id: int, agent: dict, user_input: str, mode: str, canvas: dict | None, turn: dict, is_relay: bool = False):
    conv = get_by("conversations", conversation_id) or {"id": conversation_id, "messages": []}

    # 选定模型 + 接口：由 agent_reply 入口统一分配好存进 turn（并行模式下可能挑了
    # default_model 之外的槽；串行模式直接等于 agent.model 或 interface.default_model）
    interface = turn.get("_model_interface") or get_by("model_interfaces", agent.get("interface_id")) or (load("model_interfaces") or [{}])[0]
    model = turn.get("_model") or agent.get("model") or interface.get("default_model") or "gpt-4o-mini"

    # 可用 tools（linked 集群模式下，仅画布上有连线的智能体注入 call 发送消息工具；
    # handoff 工具仅在 A2A 成果移交协议启用时注入）
    tools = _agent_tools(agent)
    has_edges = False
    if mode == "linked" and canvas:
        has_edges = _agent_on_canvas_edges(canvas, agent)
        if has_edges and not any(t["function"]["name"] == "cluster.send_message" for t in tools):
            tools.append(CALL_TOOL)
        if has_edges and A2A_HANDOFF_ENABLED and not any(t["function"]["name"] == "cluster.handoff" for t in tools):
            from core.a2a import HANDOFF_TOOL
            tools.append(HANDOFF_TOOL)

    # 构建历史
    messages = []
    cluster_context = ""
    speaker_names: dict[int, str] = {}
    if mode == "linked" and canvas:
        cluster_context, speaker_names = _build_cluster_context(canvas, agent)
    _wd_probe = turn.get("_wd_probe") if turn else None
    messages.append({"role": "system", "content": _system_prompt(agent, tools, cluster_context, canvas.get("id") if canvas else None, canvas, _wd_probe)})
    for m in conv.get("messages", []):
        # a2a 信封只用于前端展示，不进 LLM 历史（未知 role 会被接口拒绝）
        if m.get("role") == "a2a":
            continue
        # 防御：跳过 role 缺失/空/非标准的损坏历史消息（旧版数据 / 序列化异常导致）
        _m_role = m.get("role")
        if not _m_role or _m_role not in ("system", "user", "assistant", "tool"):
            continue
        # tool role / tool_calls 历史也要带上
        content = _truncate_history_content(m.get("content", ""))
        if mode == "linked" and _m_role == "assistant" and m.get("agent_id") and content:
            # 给历史消息标注发言者，让智能体分得清谁说了什么
            name = speaker_names.get(m["agent_id"])
            if name:
                content = f"[{name}] {content}"
        entry = {"role": _m_role, "content": content}
        if m.get("tool_calls"): entry["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id"): entry["tool_call_id"] = m["tool_call_id"]
        messages.append(entry)
    messages.append({"role": "user", "content": user_input})

    # 保存用户消息（接力段的 user 输入是 A2A 信封渲染文本，前端聊天区过滤）
    user_entry = {"role": "user", "content": user_input, "ts": datetime.utcnow().isoformat()}
    if is_relay:
        user_entry["relay"] = True
    conv.setdefault("messages", []).append(user_entry)

    # —— 节流落库：流式期间每 500ms 把当前 conv 快照写盘，进程硬退出时最多丢半秒数据 ——
    _LAST_FLUSH = {"t": 0.0}
    FLUSH_INTERVAL = 0.5

    def _maybe_flush(delta_msg: dict | None = None) -> None:
        """把当前 conv 落盘；delta_msg 是刚收到的一条（可选），避免额外 list 拼接。"""
        # 节流：距上次 flush 不足 FLUSH_INTERVAL 且没有强制，就跳过
        now = time.monotonic()
        if (now - _LAST_FLUSH["t"]) < FLUSH_INTERVAL and delta_msg is None:
            return
        try:
            upsert("conversations", conv)
        except Exception as _e:
            print(f"[runtime] WARN 节流 flush 失败: {_e}")
        _LAST_FLUSH["t"] = now

    # 立即写一次（用户消息刚入库），让中途 crash 至少保留用户输入
    _maybe_flush()

    final_text_parts = []
    tool_history = []  # 本轮执行的工具列表（用于入库 assistant message 的 tool_calls 字段）
    parallel_tasks = []  # 并行模式：并发接收方任务 [(asyncio_task, env)]
    max_tool_rounds = _max_tool_rounds(agent)
    assistant_reasoning = ""  # 让循环外可访问
    _last_assistant_text = ""  # 让循环外可访问

    # ===== 分层自适应路由：先判断快/慢通道 =====
    # linked 集群模式默认走复杂通道（可能需要 A2A 消息），跳过分类节省 token
    intent_route = "complex"
    intent_method = "default"
    if mode != "linked":
        try:
            intent = await classify_intent(interface, model, user_input, tools, cluster_mode=False)
            intent_route = intent["route"]
            intent_method = intent.get("method", "llm")
        except Exception:
            intent_route = "complex"

    # —— 快通道：简单问答，单次推理直接返回 ——
    if intent_route == "simple":
        # 快通道可以选择不注入 tools（模型就不会输出 tool_calls）
        fast_tools = None if FAST_PATH_NO_TOOLS else (tools or None)
        fast_tool_choice = "none" if FAST_PATH_NO_TOOLS else "auto"

        fast_content_parts = []
        fast_reasoning_parts = []
        fast_tool_calls = []
        _fast_upstream_error = False

        try:
            async for kind, payload in chat(interface, model, messages, stream=True,
                                             tools=fast_tools, tool_choice=fast_tool_choice):
                if kind == "text":
                    fast_content_parts.append(payload)
                    yield f"T:{_esc(payload)}\n"
                elif kind == "reasoning":
                    fast_reasoning_parts.append(payload)
                    yield f"RN:{_esc(payload)}\n"
                elif kind == "tool_call_start":
                    # 名称已确定，参数仍在流式 → 前端立刻显示"正在调用: xxx"
                    yield f"TS:{payload.get('name') or ''}\n"
                elif kind == "tool_call":
                    # 快通道意外遇到 tool_call（模型忽略了 tool_choice=none）→ 降级到慢通道
                    fast_tool_calls.append(payload)
                    yield f"C:{payload['id']}|{payload['name']}|{json.dumps(payload['arguments'], ensure_ascii=False)}\n"
                elif kind == "tool_call_invalid":
                    # 实时检测：工具调用格式错误 → 广播纠错提示，但不执行（快通道降级时也不加入）
                    yield f"RN:⚠️ 实时检测：工具 {payload.get('name') or '?'} 参数非法 —— {payload.get('reason', '')[:200]}\n"
                elif kind == "error":
                    # 快通道上游 400 → 降级到慢通道重试（慢通道 payload 不同：传了 tools）
                    _err_body = payload.get("detail", "")[:200]
                    yield f"RN:⚠️ 快通道上游 HTTP {payload.get('status')}：{_err_body}，尝试降级到慢通道\n"
                    if payload.get("payload_diag"):
                        yield f"RN:【快通道诊断】{payload['payload_diag'][:800]}\n"
                    _fast_upstream_error = True
        except Exception as e:
            err_msg = f"❌ LLM 调用失败：{e}"
            yield f"E:{err_msg}\n"
            # 入库错误记录，给用户一个交代
            conv["messages"].append({"role": "assistant", "content": err_msg, "ts": datetime.utcnow().isoformat(),
                                      "agent_id": agent.get("id"), "error": True})
            upsert("conversations", conv)
            yield "DONE\n"
            return

        fast_text = "".join(fast_content_parts)
        assistant_reasoning = "".join(fast_reasoning_parts).strip()

        if fast_tool_calls:
            # 降级：把刚收到的 tool_call 当作慢通道第一轮的开始，执行后继续循环
            yield f"RN:（快通道意外触发工具调用，自动降级到慢通道处理）\n"
            _last_assistant_text = fast_text
            assistant_msg_fast: dict = {"role": "assistant", "content": fast_text,
                                         "tool_calls": fast_tool_calls, "ts": datetime.utcnow().isoformat()}
            if assistant_reasoning:
                assistant_msg_fast["reasoning"] = assistant_reasoning
            messages.append(assistant_msg_fast)
            # 执行这些 tool_calls
            for tc in fast_tool_calls:
                try:
                    args = json.loads(tc.get("arguments", "{}")) if isinstance(tc.get("arguments"), str) else (tc.get("arguments") or {})
                except Exception:
                    args = {}
                result = _execute_tool(tc["name"], args,
                                      conv_id=conversation_id,
                                      tool_call_id=tc["id"],
                                      agent_id=agent.get("id"))
                result_str = _truncate_tool_result(result)
                tool_msg = {"role": "tool", "tool_call_id": tc["id"], "content": result_str}
                messages.append(tool_msg)
                # 多模态：tool result 带图片 → 追加一条 user role 多模态消息（tool role 不支持 images）
                img_followup = _build_image_followup(result)
                if img_followup:
                    messages.append(img_followup)
                conv["messages"].append({**tool_msg, "ts": datetime.utcnow().isoformat(),
                                         **({"relay": True} if is_relay else {})})
                yield f"R:{tc['id']}|{result_str}\n"
                tool_history.append({"name": tc["name"], "args": args, "result": result})
            # 进入慢通道循环
        else:
            if _fast_upstream_error:
                # 快通道上游 400 → 直接降级到慢通道循环（不存空 assistant 消息）
                # 慢通道会重新调用 LLM，且带上 tools —— payload 不同，有可能成功
                yield "RN:（快通道上游错误，降级到慢通道重试）\n"
            else:
                # 快通道正常结束
                final_text_parts.append(fast_text)
                # 入库 assistant 消息（无 tool_calls）
                stored = {"role": "assistant", "content": fast_text, "ts": datetime.utcnow().isoformat(),
                          "agent_id": agent.get("id")}
                if assistant_reasoning:
                    stored["reasoning"] = assistant_reasoning
                if is_relay:
                    stored["relay"] = True
                conv["messages"].append(stored)
                yield f"F:{fast_text.strip()}\n"
                upsert("conversations", conv)
                yield "DONE\n"
                # 直接返回，不走后续的接力流程（简单问答不需要接力）
                return

    # —— 慢通道：多轮 Tool Call Loop + 独立验证器 ——
    verify_retry_count = 0  # 验证器未通过时的修正轮数

    for round_idx in range(max_tool_rounds):
        is_last_round = (round_idx == max_tool_rounds - 1)
        tool_choice = "auto"
        if is_last_round and tools:
            tool_choice = "none"  # 强制让模型输出文字（但验证器会兜底）

        # 调 LLM
        assistant_content_parts = []
        assistant_reasoning_parts_round = []
        assistant_tool_calls = []
        # 实时检测：本轮中收集到的非法工具调用（用于流式结束后注入纠错反馈）
        _round_invalid_calls: list[dict] = []
        _upstream_error = False  # 上游返回非 200（如 400）时置位，跳过本轮后续处理

        try:
            async for kind, payload in chat(interface, model, messages, stream=True, tools=tools or None, tool_choice=tool_choice):
                if kind == "text":
                    assistant_content_parts.append(payload)
                    yield f"T:{_esc(payload)}\n"
                elif kind == "reasoning":
                    assistant_reasoning_parts_round.append(payload)
                    yield f"RN:{_esc(payload)}\n"
                elif kind == "tool_call_start":
                    # 名称首次确定即广播（参数还在流）→ 画布气泡/聊天区立刻显示"正在调用: xxx"
                    yield f"TS:{payload.get('name') or ''}\n"
                elif kind == "tool_call":
                    tc = {"id": payload["id"], "type": "function", "function": {"name": payload["name"], "arguments": json.dumps(payload["arguments"], ensure_ascii=False)}}
                    assistant_tool_calls.append(tc)
                    _tc_args_json = json.dumps(payload["arguments"], ensure_ascii=False)
                    yield f"C:{payload['id']}|{payload['name']}|{_tc_args_json}\n"
                elif kind == "tool_call_invalid":
                    # 实时检测：工具调用格式非法 → 不执行，记录后注入纠错反馈让模型重试
                    _round_invalid_calls.append(payload)
                    yield f"RN:⚠️ 实时检测：工具 {payload.get('name') or '?'} 参数非法 —— {payload.get('reason', '')[:200]}\n"
                elif kind == "error":
                    # 上游 HTTP 错误（400 等）→ 记录诊断信息并终止本轮
                    _err_body = payload.get("detail", "")[:300]
                    _diag = payload.get("payload_diag", "")
                    yield f"E:❌ 上游返回 HTTP {payload.get('status')}：{_err_body[:200]}\n"
                    if _diag:
                        yield f"RN:【诊断】{_diag[:1500]}\n"
                    _upstream_error = True
        except Exception as e:
            # LLM 调用失败（网关不通/超时等）— 输出错误 + 落库 + DONE，防止生成器中断导致前端卡死
            err_msg = f"❌ 第 {round_idx + 1} 轮 LLM 调用失败：{e}"
            yield f"E:{err_msg}\n"
            # 入库错误 assistant 消息，保留已有的 tool_history
            conv["messages"].append({
                "role": "assistant", "content": err_msg, "ts": datetime.utcnow().isoformat(),
                "agent_id": agent.get("id"), "tool_history": tool_history, "error": True,
            })
            upsert("conversations", conv)
            yield "DONE\n"
            return

        # 上游返回非 200（400 等）→ 同一 payload 重试仍会失败，直接终止循环避免空回复
        if _upstream_error:
            _err_saved = "".join(assistant_content_parts) or f"❌ 上游 LLM 返回 HTTP 错误（详情见上方诊断），无法完成本轮对话。"
            conv["messages"].append({
                "role": "assistant", "content": _err_saved, "ts": datetime.utcnow().isoformat(),
                "agent_id": agent.get("id"), "error": True,
            })
            upsert("conversations", conv)
            yield "DONE\n"
            return

        assistant_text = "".join(assistant_content_parts)
        assistant_reasoning = "".join(assistant_reasoning_parts_round).strip()
        _last_assistant_text = assistant_text

        # 把 assistant 消息入库（仅当有 tool_calls 时 —— 否则统一走循环外的 final 保存）
        assistant_msg: dict = {"role": "assistant", "content": assistant_text, "ts": datetime.utcnow().isoformat()}
        if assistant_reasoning:
            assistant_msg["reasoning"] = assistant_reasoning
        if assistant_tool_calls:
            assistant_msg["tool_calls"] = assistant_tool_calls
            stored = {**assistant_msg, "agent_id": agent.get("id")}
            if is_relay:
                stored["relay"] = True
            conv["messages"].append(stored)
        messages.append(assistant_msg)

        # —— 实时检测纠错：本轮若发现非法工具调用，注入 system 反馈让模型修正 ——
        if _round_invalid_calls:
            _invalid_lines = []
            for iv in _round_invalid_calls:
                _invalid_lines.append(
                    f"- 工具 {iv.get('name') or '?'}：{iv.get('reason', '参数非法')}"
                )
            _invalid_block = "\n".join(_invalid_lines)
            _correction = (
                "[实时检测 · 工具调用格式错误]\n"
                "你上一轮发起的工具调用被实时检测器判定为非法，**已中止执行**，未产生任何副作用。\n"
                f"问题清单：\n{_invalid_block}\n"
                "\n正确格式要求：\n"
                "1. 必须通过 function calling 机制调用工具，**严禁**在正文里手写工具调用；\n"
                "2. 工具名必须是可用工具列表中的真实名称（如 cmd.run / file.write / file.read 等）；\n"
                "3. 参数必须是合法 JSON 对象，字符串值必须用双引号包裹，例如：\n"
                "   - 正确：cmd.run: {\"command\": \"dir assets\\\\models 2>nul\"}\n"
                "   - 正确：file.write: {\"path\": \"report.md\", \"content\": \"...\"}\n"
                "   - 错误：file.write<|channel|>commentary: {...}  ← 禁止夹带通道标签\n"
                "   - 错误：{\"to_agent\": 总经理}  ← 裸标识符未加引号\n"
                "\n请立即用正确格式重新发起调用，不要重复相同的错误。"
            )
            messages.append({"role": "user", "content": _correction})
            yield f"RN:（已注入实时检测纠错反馈，让模型下一轮修正）\n"

            # 如果本轮所有工具调用都非法（没有可执行的合法调用）→ 直接继续循环让模型重试
            if not assistant_tool_calls:
                yield f"RN:（本轮工具调用全部非法，跳过执行，进入下一轮修正）\n"
                continue

        # —— 有 tool_calls → 执行并回灌，继续循环 ——
        if assistant_tool_calls:
            for tc in assistant_tool_calls:
                fn = tc["function"]
                try:
                    args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
                except Exception:
                    args = {}
                result = _execute_tool(fn["name"], args,
                                      conv_id=conversation_id,
                                      tool_call_id=tc["id"],
                                      agent_id=agent.get("id"))
                result_str = _truncate_tool_result(result)
                tool_msg = {"role": "tool", "tool_call_id": tc["id"], "content": result_str}
                messages.append(tool_msg)
                # 多模态：tool result 带图片 → 追加一条 user role 多模态消息（tool role 不支持 images）
                img_followup = _build_image_followup(result)
                if img_followup:
                    messages.append(img_followup)
                tool_entry = {**tool_msg, "ts": datetime.utcnow().isoformat()}
                if is_relay:
                    tool_entry["relay"] = True
                conv["messages"].append(tool_entry)
                yield f"R:{tc['id']}|{result_str}\n"
                tool_history.append({"name": fn["name"], "args": args, "result": result})

            # —— 串行/并行模式：cluster.send_message 后的行为差异 ——
            if mode == "linked" and canvas:
                has_call = any(tc["function"]["name"] == "cluster.send_message" for tc in assistant_tool_calls)
                if has_call:
                    exec_mode = canvas.get("execution_mode", "serial")
                    if exec_mode == "serial":
                        # 串行：智能体发送消息后停止运行，接收方开始运行
                        final_text_parts.append(assistant_text)
                        break
                    else:
                        # 并行：智能体发送消息后可以继续运行，接收方并发启动
                        yield f"RN:（并行模式：发送消息后继续运行，接收方并发启动）\n"
                        for target, d in collect_call_targets(turn, agent):
                            env = build_call_envelope(conversation_id, agent, target, d)
                            fresh = get_by("conversations", conversation_id) or conv
                            fresh.setdefault("messages", []).append({
                                "role": "a2a", "envelope": env, "ts": datetime.utcnow().isoformat(),
                            })
                            upsert("conversations", fresh)
                            yield f"SPEAK:{target.get('id')}\n"
                            yield f"A2A:{json.dumps(env, ensure_ascii=False)}\n"
                            child_turn = dict(turn)
                            child_turn["reply_to_msg_id"] = env.get("message_id")
                            child_turn["reply_to_from"] = agent.get("id")
                            task = asyncio.create_task(_drain_agent_reply(
                                conversation_id, target, render_envelope(env),
                                "linked", canvas, child_turn
                            ))
                            _register_child_task(turn, task)
                            parallel_tasks.append((task, env))

            continue  # 继续下一轮（串行模式下不会到这里，已经 break 了）

        # —— 无 tool_calls → 模型声称完成，进入独立验证 ——
        # 情况 A：没调用过任何工具 → 纯知识回答，直接采纳（与快通道类似）
        if not tool_history:
            final_text_parts.append(assistant_text)
            break

        # 情况 B：调用过工具但模型说"我做完了" → 必须过验证器
        yield f"RN:（进入独立验证器检查...）\n"
        verification = await verify_completion(
            interface, model, user_input, tool_history, assistant_text,
            retry_count=verify_retry_count, max_retries=MAX_VERIFY_RETRIES,
        )

        if verification.get("adopt"):
            # 验证通过 → 采纳
            score = verification.get("score", 1.0)
            method = verification.get("method", "unknown")
            yield f"RN:✓ 验证通过（score={score:.2f}, method={method}）\n"
            final_text_parts.append(assistant_text)
            break
        else:
            # 验证未通过 → 把反馈回灌进 messages，让模型修正
            verify_retry_count += 1
            feedback = verification.get("feedback", "任务似乎未完成，请继续处理")
            next_step = verification.get("next_step", "")
            score = verification.get("score", 0.0)
            yield f"RN:✗ 验证未通过（score={score:.2f}，第 {verify_retry_count}/{MAX_VERIFY_RETRIES} 次修正）\n"
            yield f"RN:验证反馈：{feedback[:200]}{'...' if len(feedback) > 200 else ''}\n"

            # 回灌一条 system 消息（位置放在最前面不行，放在刚结束的 assistant 之后作为新指引）
            correction_parts = ["[验证器反馈] 你上一轮的回复未能完成用户请求。"]
            if feedback:
                correction_parts.append(f"问题：{feedback}")
            if next_step:
                correction_parts.append(f"建议下一步：{next_step}")
            if score < 0.3:
                correction_parts.append("⚠️ 完成度评分极低，请彻底重新审视用户请求，不要跳过任何必要步骤。")

            # 注：用 user role 而非 system —— 部分国产模型（glm/sensenova）不允许 system 出现在对话中间，
            # 会直接返回 400。改用 user role 在所有 OpenAI 兼容接口上都能正常回灌纠错指令。
            messages.append({
                "role": "user",
                "content": "\n".join(correction_parts) + "\n请基于上述反馈继续处理，必要时重新调用工具。",
            })

            # 硬保障：如果验证重试次数超过上限但还没到 max_tool_rounds 的最后一轮，
            # 再给一次机会；如果已经是最后一轮或验证重试耗尽，强制采纳避免死循环
            if verify_retry_count >= MAX_VERIFY_RETRIES:
                yield f"RN:（验证重试已达上限，强制采纳当前回复以防止死循环）\n"
                final_text_parts.append(assistant_text)
                break

            # 还有轮数 → 继续循环让模型修正
            if is_last_round:
                # 虽然是最后一轮，但为了让模型再修正一次，我们在 messages 里追加了修正指引
                # 利用 max_tool_rounds 的剩余空间；如果确实没空间了，就强制采纳
                final_text_parts.append(assistant_text)
                break
            continue

        break  # 兜底：正常情况下上面已经 break 了

    # —— 并行模式：等待所有并发接收方完成，输出其流式内容 ——
    for task, env in parallel_tasks:
        try:
            lines = await task
            for line in lines:
                yield line
        except Exception as e:
            yield f"RN:（并行任务异常：{e}）\n"
        mark_envelope_done(conversation_id, env.get("message_id"))

    # 存最终 assistant 文字（如果最后一轮没文字但执行了工具，前端可以不展示）
    final_text = "".join(final_text_parts)
    # 去掉首尾连续空行，保留中间换行
    final_text = final_text.strip("\n")
    final_entry = {
        "role": "assistant",
        "content": final_text,
        "ts": datetime.utcnow().isoformat(),
        "agent_id": agent.get("id"), "reasoning": assistant_reasoning or None, "tool_history": tool_history,
    }
    if is_relay:
        final_entry["relay"] = True
    conv["messages"].append(final_entry)

    yield f"F:{final_text}\n"
    # 接力前先落库当前智能体产出，避免下游脏写覆盖
    upsert("conversations", conv)

    # ===== 消息接力：cluster.send_message / 自动回复 在任意 relay 层级都处理
    #     （受 MAX_CALL 预算 + A2A_MAX_RELAY_DEPTH 双重约束防爆炸）；
    #      成果接力（handoff）仅链首（relay_depth==0）触发防环 =====
    if mode == "linked" and canvas and turn.get("relay_depth", 0) <= A2A_MAX_RELAY_DEPTH:
        # 0) 本轮显式 cluster.send_message 联系过的目标（供后续 "是否已回复上游" 判定使用）
        contacted: set = set()
        for th in tool_history:
            if th.get("name") == "cluster.send_message":
                _t, _e = _resolve_target(turn, (th.get("args") or {}).get("to_agent"))
                if _t:
                    contacted.add(_t.get("id"))

        # 0.5) 自动回复（条件触发）：被上游联系后，判断正文是否为给上游的直接回复。
        #     当 agent 已经 cluster.send_message 转发给第三方 → 视为显式转发意图，不自动回传上游。
        sender_id = turn.get("reply_to_from")
        if sender_id is not None and sender_id != agent.get("id") and final_text:
            already_to_sender = (sender_id in contacted) or any(
                d.get("from_id") == agent.get("id") and d.get("to_agent_id") == sender_id
                for d in turn.get("calls", [])
            )
            # 转发检测：本轮显式 cluster.send_message 过除上游外的其他成员 → 视为 "派发/转发"，不回传上游
            forwarded_to_other = bool(contacted - {sender_id})
            if not already_to_sender and not forwarded_to_other:
                _sender_name = turn.get("all_agents", {}).get(sender_id, {}).get("name", str(sender_id))
                _should_send = False
                try:
                    _should_send = await asyncio.wait_for(
                        _should_auto_reply(interface, model, user_input, final_text, _sender_name),
                        timeout=10.0
                    )
                except asyncio.TimeoutError:
                    # 超时后保守处理：不自动回复（避免误发任务产出）
                    _should_send = False
                except Exception:
                    # 其他异常也保守处理
                    _should_send = False
                if _should_send:
                    turn.setdefault("calls", []).append({
                        "from_id": agent.get("id"),
                        "to_agent_id": sender_id,
                        "message": final_text,
                        "reply_to": turn.get("reply_to_msg_id"),
                        "via": "auto_reply",
                    })
                    yield f"RN:（正文回复已自动发回给 {_sender_name}）\n"

        # 1) cluster.send_message 排队的消息（含自动回复；允许往返多轮）
        call_targets = collect_call_targets(turn, agent)
        exec_mode = canvas.get("execution_mode", "serial")

        if exec_mode == "parallel" and len(call_targets) > 1:
            # 并行：所有接收方并发启动，最后统一输出
            tasks = []
            for target, d in call_targets:
                env = build_call_envelope(conversation_id, agent, target, d)
                fresh = get_by("conversations", conversation_id) or conv
                fresh.setdefault("messages", []).append({
                    "role": "a2a", "envelope": env, "ts": datetime.utcnow().isoformat(),
                })
                upsert("conversations", fresh)
                yield f"SPEAK:{target.get('id')}\n"
                yield f"A2A:{json.dumps(env, ensure_ascii=False)}\n"
                child_turn = dict(turn)
                child_turn["reply_to_msg_id"] = env.get("message_id")
                child_turn["reply_to_from"] = agent.get("id")
                task = asyncio.create_task(_drain_agent_reply(
                    conversation_id, target, render_envelope(env),
                    "linked", canvas, child_turn,
                ))
                _register_child_task(turn, task)
                tasks.append((task, env))
            for task, env in tasks:
                try:
                    lines = await task
                    for line in lines:
                        yield line
                except Exception as e:
                    yield f"RN:（并行任务异常：{e}）\n"
                mark_envelope_done(conversation_id, env.get("message_id"))
        else:
            # 串行：顺序处理（智能体发送消息后停止，接收方依次运行）
            for target, d in call_targets:
                env = build_call_envelope(conversation_id, agent, target, d)
                fresh = get_by("conversations", conversation_id) or conv
                fresh.setdefault("messages", []).append({
                    "role": "a2a", "envelope": env, "ts": datetime.utcnow().isoformat(),
                })
                upsert("conversations", fresh)
                yield f"SPEAK:{target.get('id')}\n"
                yield f"A2A:{json.dumps(env, ensure_ascii=False)}\n"
                # 下游回复 call 时默认 reply_to 本条
                turn["reply_to_msg_id"] = env.get("message_id")
                turn["reply_to_from"] = agent.get("id")
                async for line in agent_reply(
                    conversation_id, target, render_envelope(env),
                    mode="linked", canvas=canvas, turn=turn,
                ):
                    yield line
                mark_envelope_done(conversation_id, env.get("message_id"))
                turn["reply_to_msg_id"] = None
                turn["reply_to_from"] = None

        # 2) 成果接力（A2A_HANDOFF_ENABLED=False 时 collect_relay_targets 返回空）
        for target, h in collect_relay_targets(turn, canvas, agent):
            env = build_handoff_envelope(conversation_id, agent, target, final_text, h)
            # 每目标重载会话再追加信封，避免覆盖下游已写入的消息
            fresh = get_by("conversations", conversation_id) or conv
            fresh.setdefault("messages", []).append({
                "role": "a2a", "envelope": env, "ts": datetime.utcnow().isoformat(),
            })
            upsert("conversations", fresh)
            yield f"SPEAK:{target.get('id')}\n"
            yield f"A2A:{json.dumps(env, ensure_ascii=False)}\n"
            async for line in agent_reply(
                conversation_id, target, render_envelope(env),
                mode="linked", canvas=canvas, turn=turn,
            ):
                yield line
            mark_envelope_done(conversation_id, env.get("message_id"))

    yield "DONE\n"
