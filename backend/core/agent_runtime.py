"""智能体运行时 - 完整 tool call 链路 + 独立/关联对话模式
流式输出用简易协议（SSE 风格的前缀行）：
  T:<text>                普通文本
  TS:<name>               tool_call 开始（名称首次确定，参数仍在流式中）→ 前端可流式显示"正在调用: xxx"
  C:<id>|<name>|<args>    tool_call 完整（id、名称、JSON参数）→ 参数收齐，结束流式状态
  R:<id>|<content>        tool_result（工具执行结果，截断后不含图片二进制）
  I:<id>|<refs_json>      图片引用（可选，紧接 R: 行；refs_json = [{type, src}, ...]）
                          type="data_url" src=data:image/...（完整 base64）
                          type="file"   src=本地图片绝对路径
  F:<final_text>          最终完整文本（方便前端完整展示）
  RESET                   清空本轮已流出的正文/思考（快通道检测到 <continue> 切换慢通道时）
  SPEAK:<agent_id>        切换发言者（画布气泡 / 流动连线跟随）
  A2A:<envelope json>     A2A 消息信封（前端插入信封卡片，新开流式消息）
  DONE                    结束

  ⚠️ 行协议用 \n 做分隔符，因此 T:/RN: 等载荷文本中的 \n 必须在 yield 前用
     _esc 转义为 \\n，前端收到后用 _unesc 还原。
"""
import json, asyncio, os, time, re, logging, threading
from datetime import datetime
from typing import Any
from core.store import load, save, upsert, get_by, next_id
import core.store as store  # 运行时访问 store.ROOT（可变，set_root() 会修改）
from core.llm import chat_with_failover as chat, ModelDegenerateError, ModelRepetitionError, _encode_tool_name
from core.commands import run_command, filter_tools, COMMAND_META, WORKDIR_CTX
from core.a2a import (
    TURN_CTX, collect_relay_targets, collect_call_targets,
    build_handoff_envelope, build_call_envelope,
    render_envelope, mark_envelope_done, CALL_TOOL, A2A_HANDOFF_ENABLED,
    _resolve_target,
)
from plugins import PLUGIN_REGISTRY, run_plugin, set_stop_event, reset_stop_event
from core.validator import verify_completion

logger = logging.getLogger("agent_runtime")


def _esc(s: str) -> str:
    """转义行协议载荷中的换行符，避免 payload 内的 \n 破坏行分隔。
    同时转义 \\ 为 \\\\，还原时可唯一反解。"""
    return s.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r')


# ===== <continue> 续跑标记：快通道动态注入到本轮用户消息（不进 system prompt） =====
# 机制：快通道把续跑提示**附加在发给模型的当前用户消息末尾**（仅存在于本次请求的
# 内存 messages 中，不落库、不进历史）；后端流式检测到 <continue> → 自动切换慢通道，
# 切换时把该提示从用户消息中动态剔除。标记与提示块本身任何情况下都不允许出现在
# 对话界面 / 落库内容 / LLM 上下文。
CONTINUE_TAG = "<continue>"
_CONTINUE_HINT = (
    "\n\n[系统追加 · 非用户输入 · 任务续跑标记] "
    "若任务尚未结束（仍有必要步骤未完成、结果不完整），在回复正文最末尾**单独一行**输出：<continue>；"
    "仅当任务已全部完成、无需任何后续操作时才不输出。"
    "⚠️ 以下情形一律【不输出】此标记——寒暄/问候（你好、早上好、嗨等）、感谢、"
    "纯知识问答、简单解释、确认/否认、闲聊：这类输入随你的回复即告完成，无需后续操作。"
    "仅当确实仍需调用工具或继续多步骤操作时才输出该标记。"
    "该标记仅供系统自动识别，不会展示给用户——不要向用户提及或解释它，"
    "输出时一行只有这一个标记，不附加任何其他文字。"
)
_CONTINUE_RE = re.compile(r"<\s*continue\s*>", re.I)
# 流式判定用：从 '<' 起、到缓冲末尾为止，是否仍可能是未写完的 <continue> 前缀
_CONTINUE_PARTIAL_RE = re.compile(
    r"^<\s*(?:c(?:o(?:n(?:t(?:i(?:n(?:u(?:e)?)?)?)?)?)?)?)?\s*$", re.I
)
# 模型内部 tokenizer 标记：<unk> / <|endoftext|> / <|im_start|> / <|user|> 等
# 这些 token 永远不应该出现在正常文本里，必须清洗掉防止污染上下文
_MODEL_TOKEN_RE = re.compile(
    r"<\s*[|]?\s*(?:unk(?:now)?|endoftext|startoftext|im_start|im_end|user|assistant|system|tool(?:_call(?:_result)?)?|tool_result|eos|bos)\s*[|]?\s*>",
    re.I,
)
# 连续 30+ 个相同字符/短序列的垃圾重复（模型崩溃时会疯狂重复某个 token，如 80 个 <unk>、50 个 : 等）
# 阈值 30 避免误杀 Markdown 分隔线（--- 只有 3 个）
_GARBAGE_REPEAT_RE = re.compile(r"(.{1,3})\1{30,}")


def strip_continue(text):
    """动态剔除：连同 <continue> 标记、续跑提示块、模型内部 token 一起移除。

    - 提示块：快通道只注入在本次请求的用户消息里，正常不落库；此处兜底，
      防止模型复述指令时把整块提示带回上下文/输出。
    - 标记：大小写/标签内空白不敏感。
    - 模型内部 token：<unk> / <|endoftext|> 等 tokenizer 标记，防止污染上下文。
    - 垃圾重复：模型崩溃时的连续相同字符（如 80 个 <unk>、大量 `(` 等），整体移除。
    """
    if not isinstance(text, str) or not text:
        return text
    if _CONTINUE_HINT in text:
        text = text.replace(_CONTINUE_HINT, "")
    text = _CONTINUE_RE.sub("", text)
    # 模型内部 token 全量清洗（<unk> 等）
    text = _MODEL_TOKEN_RE.sub("", text)
    # 连续 20+ 个相同字符/短序列 → 整体移除（模型 token 流崩溃标志）
    text = _GARBAGE_REPEAT_RE.sub("", text)
    return text


class _ContinueScanner:
    """流式 <continue> 剥离器：标记可能被拆分到多个 chunk，需缓存尾部未判定字符。

    feed() 返回可安全下发的文本；close() 在流结束时吐出缓存（未写完的前缀原样保留）；
    found 表示流中是否出现过完整标记。
    """

    def __init__(self):
        self._buf = ""
        self.found = False

    def feed(self, text: str) -> str:
        if not text:
            return ""
        buf = self._buf + text
        out: list[str] = []
        i = 0
        n = len(buf)
        while i < n:
            if buf[i] == "<":
                m = _CONTINUE_RE.match(buf, i)
                if m:
                    self.found = True
                    i = m.end()
                    continue
                # 缓冲末尾可能是未写完的标记前缀（如 "<cont"）→ 暂存等下一个 chunk
                if _CONTINUE_PARTIAL_RE.match(buf, i):
                    break
            out.append(buf[i])
            i += 1
        self._buf = buf[i:]
        return "".join(out)

    def close(self) -> str:
        s, self._buf = self._buf, ""
        return s


# ===== 分层自适应循环参数 =====
MAX_VERIFY_RETRIES = 3    # 验证器不通过时，最多让模型修正几轮
FAST_PATH_NO_TOOLS = False # 快通道也允许使用工具（与慢通道一致注入 tools + tool_choice=auto）

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


def _guess_image_mime_from_b64_prefix(b64_head: str) -> str | None:
    """从 base64 字符串前 16 字符推断图片 MIME type。

    兜底识别：当工具返回 data 是纯 base64 却漏了 content_type/mime 时用。
    覆盖桌面控制 screenshot（PNG）、浏览器截图（PNG）、WebP、JPEG、GIF 等常见格式。
    """
    if b64_head.startswith("iVBORw0KGgo"):   # PNG 魔数 \x89PNG
        return "image/png"
    if b64_head.startswith("/9j/"):           # JPEG 魔数 \xFF\xD8\xFF
        return "image/jpeg"
    if b64_head.startswith("R0lGOD"):         # GIF87a / GIF89a 魔数
        return "image/gif"
    if b64_head.startswith("UklGR"):          # WebP RIFF....WEBP
        return "image/webp"
    return None


def _extract_data_urls(result: Any) -> list[str]:
    """从 tool result 里提取所有可转为 data URL 的图片，返回 data URL 列表。

    识别来源（覆盖所有已知截图/生图工具 + 兜底）：
      1. result["image_data_url"] —— 已是完整 data URL（browser_control share 模式）
      2. result["data"] —— base64 且 content_type/mime 暗示图片，或已是 data URL
      3. result["data"] —— 纯 base64 长字符串，无 mime hint，通过 base64 前缀推断
         （兜底 desktop_control inspect 等工具可能漏写 content_type 的情况）
    """
    data_urls: list[str] = []
    if not isinstance(result, dict):
        return data_urls

    # —— 来源 1：result["image_data_url"] 已是 data URL ——
    idu = result.get("image_data_url")
    if isinstance(idu, str) and idu.startswith("data:image/"):
        data_urls.append(idu)

    # —— 来源 2 & 3：result["data"] 是 base64 图片 ——
    data_field = result.get("data")
    mime_hint = result.get("content_type") or result.get("mime") or ""
    if isinstance(data_field, str) and len(data_field) > 1000:
        if data_field.startswith("data:image/"):
            data_urls.append(data_field)
        elif "," not in data_field[:50]:
            # 纯 base64（不含 data: 前缀）
            guessed_mime: str | None = None
            if mime_hint and mime_hint.startswith("image/"):
                guessed_mime = mime_hint
            else:
                guessed_mime = _guess_image_mime_from_b64_prefix(data_field[:16])
            if guessed_mime:
                data_urls.append(f"data:{guessed_mime};base64,{data_field}")

    return data_urls


def _extract_image_refs(result: Any) -> list[dict]:
    """从 tool result 里提取图片引用（data URL 或本地文件路径），供前端缩略图渲染。

    返回标准格式列表，最多 8 张，防止撑爆流式行：
      {"type": "data_url", "src": "data:image/png;base64,..."}  —— 图片 <512KB 时直接嵌 data URL
      {"type": "file", "src": "F:/path/to/image.png"}          —— 大图片或非图片扩展名保留路径

    策略：优先转 data_url（前端零依赖直接显示），超过 512KB 的图片走 file 路径让
    前端用 /api/fs/image 按需加载，避免流式行爆炸。
    """
    MAX_EMBED_BYTES = 512 * 1024  # data URL 嵌入上限（512KB 原图 → ~683KB base64，流式行安全阈值）
    refs: list[dict] = []
    if not isinstance(result, dict):
        return refs

    seen: set[str] = set()  # 去重

    def _add(entry: dict) -> None:
        key = entry.get("src", "")
        if not key or key in seen:
            return
        seen.add(key)
        refs.append(entry)

    def _try_file_to_data_url(p: str) -> dict | None:
        """尝试把本地图片文件转成 data_url；文件不存在/非图片/过大 → 返回 None。"""
        try:
            if not os.path.isfile(p):
                return None
            mime = _mime_from_path(p)
            if not mime:
                return None
            if os.path.getsize(p) > MAX_EMBED_BYTES:
                return None  # 大文件保留路径
            with open(p, "rb") as f:
                raw = f.read()
            if not _looks_like_image(raw):
                return None
            import base64 as _b64
            b64 = _b64.b64encode(raw).decode("ascii")
            return {"type": "data_url", "src": f"data:{mime};base64,{b64}"}
        except Exception:
            return None

    # —— 本地文件路径（result["file"] + result["files"] 数组）——
    for p in [result.get("file") or "", *(result.get("files") or [])]:
        p = str(p).strip()
        if not p:
            continue
        url_entry = _try_file_to_data_url(p)
        if url_entry:
            _add(url_entry)
        else:
            # 转不成 data URL → 保留 file 路径（前端走 /api/fs/image）
            _add({"type": "file", "src": p})

    # —— data URL ——
    for url in _extract_data_urls(result):
        if url:
            _add({"type": "data_url", "src": url})

    return refs[:8]


def _build_image_followup(result: Any) -> dict | None:
    """从 tool result 里提取图片，返回一条 user role 的多模态消息。

    用 _extract_data_urls 拿到所有 data URL（覆盖 image_data_url / data 两种字段），
    再加上 result["file"] 路径转成的 data URL，统一组装多模态 content。
    """
    if not isinstance(result, dict):
        return None

    data_urls: list[str] = []

    # —— 本地文件 → data URL ——
    file_path = result.get("file")
    if isinstance(file_path, str) and file_path:
        url = _data_url_from_file(file_path)
        if url:
            data_urls.append(url)

    # —— 统一提取 data URL 字段 ——
    data_urls.extend(_extract_data_urls(result))

    if not data_urls:
        return None

    # 组装多模态 content
    content: list[dict] = [
        {"type": "text", "text": "这是工具返回的结果图片，请基于它回答。"},
    ]
    for url in data_urls[:4]:  # 最多 4 张，防止撑爆
        content.append({"type": "image_url", "image_url": {"url": url, "detail": "low"}})

    return {"role": "user", "content": content, "_img_followup": True}


def _strip_prev_image_followups(messages: list[dict], keep_from_idx: int = 0) -> list[dict]:
    """把 messages 里**之前轮次**追加的图片多模态 followup 替换为简短占位文本。

    修复 keep_last 缺陷：改用 keep_from_idx 明确保留哪一段——index >= keep_from_idx
    的 img_followup 是"当前轮刚追加、LLM 还没看过的"，保留原样；
    index < keep_from_idx 的是之前轮次的，替换为占位文本。

    典型调用方式：
      - 每轮 chat() 之前，keep_from_idx = 上一轮工具执行完后的 messages 长度
        （即 messages 中本轮 tool result / img_followup 起点的 index）。

    额外职责：返回前 pop 掉所有消息里的 `_img_followup` 内部标记。
    这个标记只用于轮次间的识别，不应进入 LLM API payload。
    """
    stripped: list[dict] = []
    for idx, m in enumerate(messages):
        # 先复制并清理标记
        entry = {k: v for k, v in m.items() if k != "_img_followup"}
        if m.get("_img_followup") and idx < keep_from_idx:
            old_content = m.get("content")
            # 统计原 content 里图片数量（占位提示里带上，方便模型回忆"之前有几张图"）
            img_count = 0
            if isinstance(old_content, list):
                for part in old_content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        img_count += 1
            placeholder = (
                f"[上一轮工具返回的 {img_count} 张图片已省略，"
                f"完整内容仅在当轮 LLM 调用时提供过。如需重新查看请重新调用工具。]"
            )
            entry["content"] = placeholder
        stripped.append(entry)
    return stripped


_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _extract_image_paths(result: Any) -> list[str]:
    """从工具结果中提取所有图片文件路径（用于 IMAGE: 流式行传输给前端）。"""
    if not isinstance(result, dict):
        return []
    paths: list[str] = []
    files = result.get("files")
    if isinstance(files, list):
        for f in files:
            if isinstance(f, str) and f.lower().endswith(_IMG_EXTS):
                paths.append(f)
    if not paths:
        f = result.get("file")
        if isinstance(f, str) and f.lower().endswith(_IMG_EXTS):
            paths.append(f)
    return paths


def _truncate_tool_result(result: Any, is_image: bool = False) -> str:
    """把工具执行结果序列化成 JSON，并对大字段做智能截断。

    Args:
        result: 工具原始返回值
        is_image: 调用方已确认这是图片型 result（`_build_image_followup` 能命中）。
                  True 时直接 pop 掉所有 base64/data URL 字段，tool role 消息里
                  只留极简 `[图片]`，完整图已通过多模态 user 消息发送。

    核心策略：
      - 图片型 result：所有 base64/data URL 字段一律 pop（data / image_data_url / _note），
        只留 data: "[图片]"。解释性文字模型自己从多模态消息里能拿到。
      - 非图片但疑似 base64：保留前 BASE64_KEEP_HEAD 字符 + 总长度说明。
      - data 字段（文本/HTML/JSON）：超过 TOOL_RESULT_MAX_CHARS 就截断。
      - 整体兜底：序列化后超过 TOOL_RESULT_MAX_CHARS 再全局截一次。
    """
    if isinstance(result, dict):
        if is_image:
            # —— 图片：pop 掉 base64 data 和 _note ——
            # image_data_url 如果是 data: 开头的大 base64 → pop（太大会撑爆 R: 行）
            # 如果是短 URL（如 /api/fs/image?path=...）→ 保留，前端直接用它渲染缩略图
            _iu = result.get("image_data_url")
            _pop_iu = isinstance(_iu, str) and _iu.startswith("data:")
            result = {
                k: v for k, v in result.items()
                if k not in ("data", "_note") and not (k == "image_data_url" and _pop_iu)
            }
            result["data"] = "[图片]"
        else:
            # —— 非图片：按原策略处理 ——
            data = result.get("data")
            if isinstance(data, str) and len(data) > 4000 and all(
                c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n" for c in data[:200]
            ):
                # 疑似 base64（但不是图片）：只保留前缀 + 总长度说明
                truncated_data = data[:BASE64_KEEP_HEAD] + f"...[base64 截断，原始长度 {len(data)} 字符]"
                result = {**result, "data": truncated_data}
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
    return filter_tools(agent.get("commands", []), agent.get("plugins", []), PLUGIN_REGISTRY, agent.get("skills"))


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
        lines.append(f"- {_encode_tool_name(name)}：{desc or '（无描述）'}")
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
    # 技能 prompt 拼接（渐进式加载）
    # Level 1（发现）：只把 name + description 放进 system prompt，零 token 成本，让模型知道有哪些技能可用。
    # Level 2（激活）：SKILL.md 正文**不**自动拼入 system prompt；模型判断相关时必须用 skill.invoke
    #   显式调用，工具按正文大小返回：一般正文返回全文，较大正文（如 pptx）只返回目录 toc + 预览。
    # Level 3（执行）：大技能用 skill.read_section 按章节标题按需读取；
    #   需要 scripts/ / references/ / assets/ 时再用 file.read 读技能目录内文件。
    # 范围：agent 显式配置了 skills 就用配置的子集，否则回退到全部已注册技能
    # （skill.invoke 是常驻工具，不配置也应能按需调用任意技能）。
    try:
        from skills import SKILL_REGISTRY, load_skills
        load_skills()  # 热重载：用户在对话中新增/修改技能后立即可见
        # 范围：agent 显式配置了 skills 就用配置的子集；
        #   None（旧数据/从未保存过）→ 回退到全部已注册技能索引；
        #   []（用户明确不勾选）→ 不注入任何技能索引（skill.invoke 常驻工具仍可用）
        wanted = agent.get("skills")
        names = sorted(SKILL_REGISTRY.keys()) if wanted is None else [s for s in wanted if SKILL_REGISTRY.get(s)]
        skill_index = []
        for s in names:
            meta = SKILL_REGISTRY.get(s)
            if not meta:
                continue
            label = meta.get("label") or s
            desc = meta.get("description", "")
            skill_index.append(f"- {label}（skill_name={s}）：{desc}" if desc else f"- {label}（skill_name={s}）")
        if skill_index:
            parts.append(
                "\n## 可用技能（索引，不含正文）\n"
                "下列技能**不会**自动加载到你的上下文。任务与某个技能相关时，"
                "用 skill.invoke 并传入其 skill_name 获取技能指令，再按指令执行；"
                "一般大小技能会返回完整正文；较大技能（如 pptx）只返回目录 toc 与开头预览，"
                "需用 skill.read_section 按章节标题按需读取具体章节，不要从预览中猜测未给出的内容。"
                "不要凭猜测使用未加载的技能内容。\n"
                + "\n".join(skill_index)
            )
    except Exception as e:
        print(f"[agent_runtime] WARN 技能索引构建失败：{e}")
    # 工具能力：完整清单 + 主动调用规则（按任务自动选工具，不等用户点名）
    if tools:
        capability = _tool_capability_brief(tools)
        tool_names = [_encode_tool_name(t["function"]["name"]) for t in tools]
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
    prompt = "\n".join(parts)
    # —— 动态替换存储路径占位符 ——
    # agent.system_prompt 中可用 {skills_dir} / {plugins_dir} / {bg_dir} / {storage_root}，
    # 与 store.ROOT 保持同步（用户在设置里切换存储根后自动生效）。
    try:
        _root = str(store.ROOT)
        prompt = prompt.replace("{skills_dir}", str(store.ROOT / "skills"))
        prompt = prompt.replace("{plugins_dir}", str(store.ROOT / "plugins"))
        prompt = prompt.replace("{bg_dir}", str(store.ROOT / "backgrounds"))
        prompt = prompt.replace("{storage_root}", _root)
    except Exception:
        pass
    # 面向模型的工具名统一替换为上游合法名（下划线），与注入的 tools schema 保持一致，
    # 避免模型从示例文本模仿点号名（file.read / cluster.send_message 等）导致上游网关 400。
    for _t in tools or []:
        _fn = _t.get("function") or {}
        _nm = _fn.get("name") or ""
        if _nm:
            _enc = _encode_tool_name(_nm)
            if _enc != _nm:
                prompt = prompt.replace(_nm, _enc)
    return prompt


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
    _stop_event = threading.Event()
    _stop_token = set_stop_event(_stop_event)

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
            import traceback as _tb
            print(f"[agent_runtime] _agent_reply_inner 漏网异常: {e}\n{_tb.format_exc()}", flush=True)
            yield f"E:❌ 运行时异常：{e}\n"
            yield "DONE\n"
    finally:
        _stop_event.set()
        reset_stop_event(_stop_token)
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
    system_prompt_base = _system_prompt(agent, tools, cluster_context, canvas.get("id") if canvas else None, canvas, _wd_probe)
    messages.append({"role": "system", "content": system_prompt_base})
    for m in conv.get("messages", []):
        # a2a 信封只用于前端展示，不进 LLM 历史（未知 role 会被接口拒绝）
        if m.get("role") == "a2a":
            continue
        # 防御：跳过 role 缺失/空/非标准的损坏历史消息（旧版数据 / 序列化异常导致）
        _m_role = m.get("role")
        if not _m_role or _m_role not in ("system", "user", "assistant", "tool"):
            continue
        # tool role / tool_calls 历史也要带上（上下文中的 <continue> 标记一律剥离）
        content = strip_continue(_truncate_history_content(m.get("content", "")))
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
    # 本轮用户消息的引用：快通道在其末尾动态附加 <continue> 续跑提示（仅本次请求的
    # 内存 messages，不写入 conv）；进入慢通道时用它把提示动态剔除。
    user_msg_ref = messages[-1]

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

    # ===== 分层自适应路由：默认快通道 =====
    # 非 linked 模式一律从快通道开始（把 <continue> 续跑提示动态附加到当前用户消息）；
    # 输出检测到 <continue>、意外触发工具调用或上游错误时自动降级/切换慢通道。
    # linked 集群模式涉及 A2A 接力/移交，保持直接走慢通道。
    intent_route = "complex" if mode == "linked" else "simple"

    # —— 快通道：简单问答，单次推理直接返回 ——
    if intent_route == "simple":
        # 快通道：续跑提示动态附加到本轮用户消息末尾（不放进 system prompt；
        # 落库的 user_entry 保持原始输入，历史里永远看不到这段提示）
        user_msg_ref["content"] = user_input + _CONTINUE_HINT
        # 确认日志：在 backend.log 可直接核实提示已动态注入（慢通道/linked 模式无此行）
        print(f"[runtime] 快通道注入续跑标记到用户消息: user_len={len(user_input)}, "
              f"hint_attached={user_msg_ref['content'].endswith(_CONTINUE_HINT)}")
        # 快通道可以选择不注入 tools（模型就不会输出 tool_calls）
        fast_tools = None if FAST_PATH_NO_TOOLS else (tools or None)
        fast_tool_choice = "none" if FAST_PATH_NO_TOOLS else "auto"

        fast_content_parts = []
        fast_reasoning_parts = []
        fast_tool_calls = []
        fast_invalid_calls = []   # 快通道实时检测到的非法工具调用（降级到慢通道纠错）
        _fast_upstream_error = False
        fast_text_scan = _ContinueScanner()
        fast_reason_scan = _ContinueScanner()

        try:
            async for kind, payload in chat(interface, model, messages, stream=True,
                                             tools=fast_tools, tool_choice=fast_tool_choice):
                if kind == "text":
                    _delta = fast_text_scan.feed(payload)
                    if _delta:
                        fast_content_parts.append(_delta)
                        yield f"T:{_esc(_delta)}\n"
                elif kind == "reasoning":
                    _delta = fast_reason_scan.feed(payload)
                    if _delta:
                        fast_reasoning_parts.append(_delta)
                        yield f"RN:{_esc(_delta)}\n"
                elif kind == "tool_call_start":
                    # 名称已确定，参数仍在流式 → 前端立刻显示"正在调用: xxx"
                    yield f"TS:{payload.get('name') or ''}\n"
                elif kind == "tool_args_delta":
                    # 参数增量实时广播 → 前端流式显示正在接收的参数内容（TD: 通道）
                    _acc = payload.get("accumulated", "")
                    if _acc:
                        yield f"TD:{_esc(_acc)}\n"
                elif kind == "tool_call":
                    # 快通道意外遇到 tool_call（模型忽略了 tool_choice=none）→ 降级到慢通道
                    fast_tool_calls.append(payload)
                    yield f"C:{payload['id']}|{payload['name']}|{json.dumps(payload['arguments'], ensure_ascii=False)}\n"
                elif kind == "tool_call_invalid":
                    # 实时检测：工具调用格式错误 → 收集并标记降级到慢通道纠错
                    fast_invalid_calls.append(payload)
                    yield f"RN:⚠️ 实时检测：工具 {payload.get('name') or '?'} 参数非法 —— {payload.get('reason', '')[:200]}\n"
                elif kind == "error":
                    # 快通道上游 400 → 降级到慢通道重试（慢通道 payload 不同：传了 tools）
                    _err_body = payload.get("detail", "")[:200]
                    yield f"RN:⚠️ 快通道上游 HTTP {payload.get('status')}：{_err_body}，尝试降级到慢通道\n"
                    if payload.get("payload_diag"):
                        yield f"RN:【快通道诊断】{payload['payload_diag'][:800]}\n"
                    _fast_upstream_error = True
        except ModelRepetitionError as _rep:
            # 快通道检测到重复循环 → 降级到慢通道重试（慢通道有多轮循环 + 纠错机制）
            yield f"RN:⚠️ 快通道模型输出重复循环（连续重复 {_rep.repeat_count} 句），降级到慢通道重试\n"
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

        fast_content_parts.append(fast_text_scan.close())
        fast_reasoning_parts.append(fast_reason_scan.close())
        fast_text = strip_continue("".join(fast_content_parts))
        assistant_reasoning = strip_continue("".join(fast_reasoning_parts).strip())

        if fast_tool_calls:
            # 降级：把刚收到的 tool_call 当作慢通道第一轮的开始，执行后继续循环
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
                result = await asyncio.to_thread(_execute_tool, tc["name"], args,
                                      conv_id=conversation_id,
                                      tool_call_id=tc["id"],
                                      agent_id=agent.get("id"))
                # 先判断是不是图片：一次调用同时给 truncate 和 followup 用，避免重复 I/O
                img_followup = _build_image_followup(result)
                result_str = _truncate_tool_result(result, is_image=(img_followup is not None))
                tool_msg = {"role": "tool", "tool_call_id": tc["id"], "content": result_str}
                messages.append(tool_msg)
                if img_followup:
                    messages.append(img_followup)
                # 提取图片引用（供前端缩略图渲染，不受 truncate 影响）
                img_refs = _extract_image_refs(result)
                tool_entry = {**tool_msg, "ts": datetime.utcnow().isoformat(),
                              **({"relay": True} if is_relay else {})}
                if img_refs:
                    tool_entry["images"] = img_refs
                conv["messages"].append(tool_entry)
                # ⚠️ JSON 行（R:/I:/C:/A2A:）都不做 _esc——json.dumps 自己转义引号/反斜杠/换行，
                #    直接嵌 f-string 即可；只有纯文本行（T:/RN:）才需要 _esc 防止破坏行分隔。
                yield f"R:{tc['id']}|{result_str}\n"
                if img_refs:
                    yield f"I:{tc['id']}|{json.dumps(img_refs, ensure_ascii=False)}\n"
                tool_history.append({"name": tc["name"], "args": args, "result": result})
            # 进入慢通道循环
        else:
            if _fast_upstream_error:
                # 快通道上游 400 → 直接降级到慢通道循环（不存空 assistant 消息）
                # 慢通道会重新调用 LLM，且带上 tools —— payload 不同，有可能成功
                yield "RN:（快通道上游错误，降级到慢通道重试）\n"
            elif fast_invalid_calls:
                # 快通道检测到非法工具调用 → 降级到慢通道，注入纠错反馈让模型修正
                _last_assistant_text = fast_text
                _fast_assistant_msg: dict = {"role": "assistant", "content": fast_text,
                                             "ts": datetime.utcnow().isoformat()}
                if assistant_reasoning:
                    _fast_assistant_msg["reasoning"] = assistant_reasoning
                messages.append(_fast_assistant_msg)
                _invalid_lines = [
                    f"- 工具 {iv.get('name') or '?'}：{iv.get('reason', '参数非法')}"
                    for iv in fast_invalid_calls
                ]
                _invalid_block = "\n".join(_invalid_lines)
                _fast_correction = (
                    "[实时检测 · 工具调用格式错误]\n"
                    "你上一轮发起的工具调用被实时检测器判定为非法，**已中止执行**，未产生任何副作用。\n"
                    f"问题清单：\n{_invalid_block}\n"
                    "\n正确格式要求：\n"
                    "1. 必须通过 function calling 机制调用工具，**严禁**在正文里手写工具调用；\n"
                    "2. 工具名必须是可用工具列表中的真实名称；\n"
                    "3. 参数必须是合法 JSON 对象，字符串值必须用双引号包裹；\n"
                    "4. **必填参数不可遗漏**（如 action 参数必须明确指定为 enum 中的值）。\n"
                    "\n请立即用正确格式重新发起调用，不要重复相同的错误。"
                )
                messages.append({"role": "user", "content": _fast_correction})
                yield "RN:（快通道检测到非法工具调用，降级到慢通道修正）\n"
            elif fast_text_scan.found or fast_reason_scan.found:
                # 模型输出了 <continue> → 任务未结束：清掉前端已流出的快通道内容，
                # 不入库、不 return，直接落入下方慢通道循环继续处理
                yield "RESET\n"
                yield "RN:（检测到任务未完成标记，自动切换到慢通道继续处理）\n"
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
    # 慢通道不携带续跑提示：把快通道附加到用户消息上的提示动态剔除
    # （linked 直连慢通道时 content 本就等于 user_input，剔除是幂等的）
    user_msg_ref["content"] = user_input
    verify_retry_count = 0  # 验证器未通过时的修正轮数
    # 图片省略边界：本轮 tool 执行起点的 messages index。
    # chat 前 strip 时，index < _tool_round_start 的 img_followup → 替换为占位
    # （已被之前 LLM 看过）；>= _tool_round_start 的保留完整（当前轮刚追加，本轮 LLM 还没看过）。
    # 初始化为 0：进入 for 循环时 messages 里所有 img_followup（linked / 快通道降级追加的）
    # 都还没被 LLM 看过，第一轮 chat 前全部保留。
    _tool_round_start = 0
    _text_history: list[str] = []  # 记录每轮 assistant 纯文本签名，用于循环检测

    for round_idx in range(max_tool_rounds):
        is_last_round = (round_idx == max_tool_rounds - 1)
        tool_choice = "auto"
        if is_last_round and tools:
            tool_choice = "none"  # 强制让模型输出文字（但验证器会兜底）

        # 调 LLM
        assistant_content_parts = []
        assistant_reasoning_parts_round = []
        assistant_tool_calls = []
        round_text_scan = _ContinueScanner()
        round_reason_scan = _ContinueScanner()
        # 实时检测：本轮中收集到的非法工具调用（用于流式结束后注入纠错反馈）
        _round_invalid_calls: list[dict] = []
        _upstream_error = False  # 上游返回非 200（如 400）时置位，跳过本轮后续处理

        # —— 每轮 LLM 调用前：把之前轮次追加的图片多模态 followup 替换为占位文本 ——
        # 完整 base64 data URL 只在图片产生的当轮让 LLM 看到即可，后续轮次继续携带
        # 会让 context 被几万字节的 data URL 撑爆。替换后保留对话链条结构，但 token 开销
        # 降到几乎为零。最近一轮的图片 followup 保留（本轮 LLM 还没回过，必须让它看到）。
        messages = _strip_prev_image_followups(messages, keep_from_idx=_tool_round_start)

        _gen = chat(interface, model, messages, stream=True, tools=tools or None, tool_choice=tool_choice)
        _garbage_hits = 0        # 流式重复垃圾命中次数
        _garbage_buf = ""        # 滚动文本窗口（用于重复字符检测）
        _garbage_aborted = False # 是否因流式垃圾中止本轮
        try:
            async for kind, payload in _gen:
                if kind == "text":
                    _delta = round_text_scan.feed(payload)
                    if _delta:
                        assistant_content_parts.append(_delta)
                        _garbage_buf = (_garbage_buf + _delta)[-400:]
                        yield f"T:{_esc(_delta)}\n"
                        # 重复字符窗口检测：清洗后仍透传的 `::::`/`((((` 等累积到 30+ 重复串 → 判定退化
                        if _GARBAGE_REPEAT_RE.search(_garbage_buf):
                            _garbage_hits += 1
                            if _garbage_hits >= 3:
                                _garbage_aborted = True
                                break
                elif kind == "reasoning":
                    _delta = round_reason_scan.feed(payload)
                    if _delta:
                        assistant_reasoning_parts_round.append(_delta)
                        yield f"RN:{_esc(_delta)}\n"
                elif kind == "tool_call_start":
                    # 名称首次确定即广播（参数还在流）→ 画布气泡/聊天区立刻显示"正在调用: xxx"
                    yield f"TS:{payload.get('name') or ''}\n"
                elif kind == "tool_args_delta":
                    # 参数增量实时广播 → 前端流式显示正在接收的参数内容（TD: 通道）
                    _acc = payload.get("accumulated", "")
                    if _acc:
                        yield f"TD:{_esc(_acc)}\n"
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
                    _err_msg = f"❌ 上游返回 HTTP {payload.get('status')}：{_err_body[:200]}"
                    yield f"E:{_esc(_err_msg)}\n"
                    if _diag:
                        yield f"RN:【诊断】{_diag[:1500]}\n"
                    _upstream_error = True
        except ModelRepetitionError as _rep:
            # 流式输出重复循环（模型复读同一/近似段落）→ 中止本轮，注入纠错反馈，进入下一轮
            try:
                await _gen.aclose()  # 关闭上游流
            except Exception:
                pass
            assistant_content_parts.append(round_text_scan.close())
            assistant_text = strip_continue("".join(assistant_content_parts))
            assistant_msg = {"role": "assistant", "content": assistant_text, "ts": datetime.utcnow().isoformat()}
            if is_relay:
                stored = {**assistant_msg, "agent_id": agent.get("id"), "relay": True}
            else:
                stored = {**assistant_msg, "agent_id": agent.get("id")}
            conv["messages"].append(stored)
            messages.append(assistant_msg)
            # 注入纠错提示：告诉模型已检测到复读循环，立即停止输出
            rep_correction = (
                "[系统检测 · 流式输出重复循环]\n"
                "你本轮的流式输出中检测到连续重复/近似段落，已被系统中止。"
                "**立即行动**：停止复读输出，不要再输出相同或近似的内容。\n"
                "当前任务仍未完成，请直接进入下一阶段——执行我们需要你做的实际工作，不要继续规划或重复之前的表述。"
            )
            messages.append({"role": "user", "content": rep_correction})
            _text_history.append(assistant_text[:800].strip())
            yield f"RN:⚠️ 模型流式输出重复循环（连续重复 {_rep.repeat_count} 句），已中止本轮并注入纠错反馈。\n"
            logger.warning("[agent_runtime] ModelRepetitionError round=%d: %s | repeated=%r",
                           round_idx, _rep, _rep.repeated_text[:120])
            # 注入纠错后进入下一轮处理
            continue
        except ModelDegenerateError as _de:
            # 模型流式输出退化（连续纯内部 token）→ 中止本轮，保留已收到的有效内容，注入纠错
            yield f"RN:⚠️ 模型流式输出退化（{_de}），已主动中止本轮。\n"
            logger.warning("[agent_runtime] ModelDegenerateError round=%d: %s", round_idx, _de)
        except Exception as e:
            # LLM 调用失败（网关不通/超时等）— 输出错误 + 落库 + DONE，防止生成器中断导致前端卡死
            logger.exception("[agent_runtime] LLM 调用失败 round=%d model=%s", round_idx + 1, model)
            err_msg = f"❌ 第 {round_idx + 1} 轮 LLM 调用失败：{type(e).__name__}: {e}"
            yield f"E:{_esc(err_msg)}\n"
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

        # 流式重复垃圾窗口检测触发 → 中止了本轮 LLM 流；保留已收到的有效内容，注入纠错后进入下一轮
        if _garbage_aborted:
            try:
                await _gen.aclose()  # break 后显式关闭上游流，释放 httpx 连接
            except Exception:
                pass
            assistant_content_parts.append(round_text_scan.close())
            assistant_text = strip_continue("".join(assistant_content_parts))
            assistant_msg = {"role": "assistant", "content": assistant_text, "ts": datetime.utcnow().isoformat()}
            if is_relay:
                stored = {**assistant_msg, "agent_id": agent.get("id"), "relay": True}
            else:
                stored = {**assistant_msg, "agent_id": agent.get("id")}
            conv["messages"].append(stored)
            messages.append(assistant_msg)
            _garbage_correction = (
                "[系统检测 · 流式输出退化]\n"
                "你上一轮的流式输出中检测到大量重复/无意义字符（连续内部 token 或垃圾重复），已被系统中止。\n"
                "**立即行动**：请用工具执行实质操作——写代码（file.write）、运行脚本（cmd.run）。"
                "不要重复规划，不要输出相同内容，直接进入执行阶段。"
            )
            messages.append({"role": "user", "content": _garbage_correction})
            _text_history.append(assistant_text[:800].strip())
            yield f"RN:（检测到流式输出退化，已中止并注入纠错反馈）\n"
            continue

        assistant_content_parts.append(round_text_scan.close())
        assistant_reasoning_parts_round.append(round_reason_scan.close())
        assistant_text = strip_continue("".join(assistant_content_parts))
        assistant_reasoning = strip_continue("".join(assistant_reasoning_parts_round).strip())
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

        # —— 输出循环检测：纯文本输出 + 与前 2 轮任意一轮高度相似 → 注入"立即执行"纠错反馈 ——
        if not assistant_tool_calls and assistant_text.strip() and round_idx >= 1:
            _curr_sig = assistant_text[:800].strip()
            # 和最近 2 轮签名逐一比较（防止模型在两种相似输出间交替）
            _loop_hit = False
            for _prev_sig in _text_history[-2:]:
                if not _prev_sig or not _curr_sig:
                    continue
                _min_len = min(len(_prev_sig), len(_curr_sig))
                _match = 0
                for i in range(_min_len):
                    if _prev_sig[i] == _curr_sig[i]:
                        _match += 1
                    else:
                        break
                _ratio = _match / max(len(_curr_sig), 1)
                if _ratio > 0.55 and len(_curr_sig) > 80:
                    _loop_hit = True
                    break
            if _loop_hit:
                _loop_correction = (
                    "[系统检测 · 输出循环]\n"
                    "你本轮的回复与之前轮次高度相似，看起来陷入了重复规划/思考的循环。\n"
                    "**立即行动**：不要再规划、不要重复相同的 Outline。请用工具执行实质操作——"
                    "写代码（file.write）、运行脚本（cmd.run）、或调用其他工具产出实际文件。\n"
                    "规划只输出一次就够了，现在直接进入执行阶段。"
                )
                messages.append({"role": "user", "content": _loop_correction})
                yield f"RN:（检测到输出循环，已注入纠错反馈）\n"
                logger.warning(
                    "[agent_runtime] 输出循环被触发 round=%d", round_idx,
                )
        # 记录本轮文本签名供后续轮次比较
        _text_history.append(assistant_text[:800].strip())

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
            # 记录本轮 tool result / img_followup 追加的起点 index。
            # 下一轮 chat 前 strip 时，index >= 此值的 img_followup 要保留
            # （本轮刚追加，本轮 LLM 还没看过）；< 此值的是之前轮次的，替换为占位。
            _tool_round_start = len(messages)
            for tc in assistant_tool_calls:
                fn = tc["function"]
                try:
                    args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
                except Exception:
                    args = {}
                result = await asyncio.to_thread(_execute_tool, fn["name"], args,
                                      conv_id=conversation_id,
                                      tool_call_id=tc["id"],
                                      agent_id=agent.get("id"))
                # 先判断是不是图片：一次调用同时给 truncate 和 followup 用，避免重复 I/O
                img_followup = _build_image_followup(result)
                result_str = _truncate_tool_result(result, is_image=(img_followup is not None))
                tool_msg = {"role": "tool", "tool_call_id": tc["id"], "content": result_str}
                messages.append(tool_msg)
                if img_followup:
                    messages.append(img_followup)
                # 提取图片引用（供前端缩略图渲染，不受 truncate 影响）
                img_refs = _extract_image_refs(result)
                tool_entry = {**tool_msg, "ts": datetime.utcnow().isoformat()}
                if is_relay:
                    tool_entry["relay"] = True
                if img_refs:
                    tool_entry["images"] = img_refs
                conv["messages"].append(tool_entry)
                # ⚠️ JSON 行不做 _esc（见快通道同名行注释）
                yield f"R:{tc['id']}|{result_str}\n"
                if img_refs:
                    yield f"I:{tc['id']}|{json.dumps(img_refs, ensure_ascii=False)}\n"
                tool_history.append({"name": fn["name"], "args": args, "result": result})

            # 执行了工具 = 任务取得实质进展 → 验证重试配额清零。
            # 否则模型每轮工具之间"过渡解说文字"都会触发一次验证器（上一条工具常有
            # 未消化的报错 → deterministic blocked），3 次配额会被无进展地耗光，
            # 导致"强制采纳"把整个运行提前掐死在任务中途。
            verify_retry_count = 0

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
    final_text = strip_continue("".join(final_text_parts))
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
