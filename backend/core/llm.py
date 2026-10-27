"""统一的 LLM 调用 - 兼容 OpenAI 格式接口 + function calling"""
import asyncio, httpx, json, logging
from typing import AsyncGenerator

_logger = logging.getLogger("llm")

# 模型退化阈值：连续 N 个 content chunk 清洗后全部为空（模型只吐 <unk> 等内部 token）
# → 判定流式输出退化，抛异常中止本轮，避免垃圾继续流向前端/污染上下文
_DEGENERATE_EMPTY_LIMIT = 12

# —— 重复输出检测（模型陷入"复读循环"）——
# 算法：维护一个滚动的"最近段落"缓冲队列（按句子切分，最多 _REPETITION_PARAGRAPH_WINDOW 段），
#       每收到一个新句子，就与缓冲区内所有历史句子做规范化相似度比对。
#       若新句子与某历史句子的相似度 >= _REPETITION_SIMILARITY 阈值 → 计一次"重复命中"；
#       当连续命中的句子数 >= _REPETITION_MAX_REPEATS 时 → 判定陷入重复循环。
# 设计取舍：
#   - 在句子粒度上检测，避免把"单词/短语偶然重复"误判为循环；
#   - 用字符 n-gram 重叠率做相似度（对换行、缩进、标点差异鲁棒）；
#   - 只在产出足够内容（累计 >= _REPETITION_MIN_SENTENCES 句）后才启用，避免误伤开头正常内容。
_REPETITION_PARAGRAPH_WINDOW = 12    # 缓冲区保留的历史句子数
_REPETITION_SIMILARITY = 0.85        # 相似度阈值：>= 视为"同一句在复读"
_REPETITION_MAX_REPEATS = 3          # 连续重复命中数：达到即判定循环
_REPETITION_MIN_SENTENCES = 6        # 至少累计多少句子后才开始检测（防开头误伤）
_REPETITION_NGRAM_K = 6              # 句子相似度用的 n-gram 长度


def _sentence_similarity(a: str, b: str) -> float:
    """计算两段文本的字符 n-gram 重叠相似度（0.0 ~ 1.0）。

    用于判断模型新输出的句子是否在重复历史句子。对以下差异鲁棒：
      - 换行/缩进/空白差异（先归一化空白）
      - 大小写差异（统一转小写）
    实现：把两段文本拆成固定长度 n-gram 集合，用 Jaccard 相似度。
    """
    def _ngrams(s: str, k: int):
        return {s[i:i + k] for i in range(max(0, len(s) - k + 1))}

    a2 = " ".join(a.split()).lower()
    b2 = " ".join(b.split()).lower()
    if not a2 or not b2:
        return 0.0
    ka, kb = _ngrams(a2, _REPETITION_NGRAM_K), _ngrams(b2, _REPETITION_NGRAM_K)
    if not ka or not kb:
        return 0.0
    inter = len(ka & kb)
    union = len(ka | kb)
    return inter / union if union else 0.0


class ModelRepetitionError(RuntimeError):
    """模型流式输出陷入重复循环（复读同一/近似段落）时抛出。

    供上层中止本轮 LLM 调用并注入纠错反馈后重试，而不是把无限复读内容落库/污染上下文。
    """

    def __init__(self, message: str, repeated_text: str = "", repeat_count: int = 0):
        super().__init__(message)
        self.repeated_text = repeated_text      # 被重复的那段文本（便于上层展示/纠错）
        self.repeat_count = repeat_count        # 累计重复命中次数


class SentenceDetector:
    """单轮 LLM 调用中的重复句子检测器。

    机制：
      - 维护一个滚动队列，记录最近 _REPETITION_PARAGRAPH_WINDOW 句子。
      - 每收到一个新句子（过滤纯空/纯标点）→ 与缓冲区历史句子做相似度比对。
      - 若相似度 >= _REPETITION_SIMILARITY → 计一次"重复命中"，累加 consecutive_hits。
      - 连续重复命中数 >= _REPETITION_MAX_REPEATS 时 → 触发 ModelRepetitionError。
      - 检测在累计 >= _REPETITION_MIN_SENTENCES 句后才启用（避免误伤开头）。
      - 换行/停顿被视作句子分隔符（按 .!?。\n 等切分）。

    使用：
      detector = SentenceDetector()
      if detector.check(text_chunk) is True:
          raise ModelRepetitionError(...)
      # else:
      #   yield text_chunk...
    """

    def __init__(self):
        self._history: list[str] = []      # 按出现顺序存储的历史句子
        self._consecutive_hits: int = 0    # 连续重复命中数
        self._sentence_count: int = 0      # 累计句子数（仅用于判断是否达到最小阈值）
        self._pending: str = ""            # 跨 chunk 未完成的尾句（等待下一个 chunk 补全）

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """按标点符号切分文本为句子列表（提升对多轮回复的鲁棒性）。

        注意：regex split 带捕获组会把分隔符（. ！？。等）也作为独立片段返回，
        这里必须过滤掉纯标点/空白片段，否则单独的 "。" 会被当成一个"句子"参与
        相似度比对，从而打断连续重复命中计数。
        """
        if not text or not text.strip():
            return []
        # 按句子结束符 (.!?。！？\n) 分割，保留原始大小写（供 repeated_text 展示用），
        # 大小写差异由 _sentence_similarity 内部统一转小写处理
        sentences = _re.split(r'(\n|[.!?。！？]+)', text.strip())
        # 过滤纯标点/空白片段：至少包含一个字母、数字或汉字才视为有意义句子
        sentences = [s.strip() for s in sentences if _re.search(r'[^\s\W_]', s)]
        return sentences

    def check(self, text: str) -> tuple[bool, ModelRepetitionError | None]:
        """检查新句子是否触发重复循环。

        跨 chunk 处理：上一个 chunk 末尾未完成的句子保存在 _pending，
        与本 chunk 拼接后再切分，避免把一句拆成两半误判。
        """
        if not text or not text.strip():
            return False, None

        merged = (self._pending + text).strip()
        parts = self._split_sentences(merged)
        if not parts:
            return False, None

        # 末尾片段可能仍未完成（不以句子结束符结尾）→ 留待下一个 chunk
        last = parts[-1]
        if merged and last and not _re.search(r'[\n.!?。！？]\s*$', merged):
            self._pending = last
            parts = parts[:-1]
        else:
            self._pending = ""

        # 逐个句子检测
        for sentence in parts:
            sentence = sentence.strip()
            if not sentence:
                continue

            self._sentence_count += 1

            # 在累计至少 MIN_SENTENCES 句后才启用检测（避免误伤开头）
            if self._sentence_count < _REPETITION_MIN_SENTENCES:
                self._history.append(sentence)
                continue

            # 与缓冲区历史句子做相似度比对
            repeated = False
            for hist in self._history[-_REPETITION_PARAGRAPH_WINDOW:]:
                if _sentence_similarity(sentence, hist) >= _REPETITION_SIMILARITY:
                    repeated = True
                    break

            if repeated:
                # 与历史句子几乎相同 → 记录被重复的文本（用历史句子作为参考）
                self._consecutive_hits += 1
                if self._consecutive_hits >= _REPETITION_MAX_REPEATS:
                    # 触发重复循环错误
                    return True, ModelRepetitionError(
                        message=f"模型连续输出 {self._consecutive_hits} 个重复/近似句子，判定陷入重复输出循环",
                        repeated_text=sentence,
                        repeat_count=self._consecutive_hits
                    )
            else:
                # 新句子独特 → 重置连续命中计数，并将其加入历史
                self._consecutive_hits = 0
                self._history.append(sentence)
                # 保持缓冲区大小不超过窗口
                if len(self._history) > _REPETITION_PARAGRAPH_WINDOW:
                    self._history.pop(0)

        # 未触发错误
        return False, None


class ModelDegenerateError(RuntimeError):
    """模型流式输出退化为垃圾（连续内部 token / 无意义重复）时抛出。

    供上层中止本轮 LLM 调用并注入纠错反馈后重试，而不是把垃圾内容落库。
    """
    pass

_OPENAI_ROLES = {"system", "user", "assistant", "tool"}

import re as _re

# —— 清洗阶段用的正则（无论 known_names 是否为空都生效）——
_PSEUDO_PROTOCOL_TAG_RE = _re.compile(r"<\|[^|]*\|>")           # <|xxx|> 标签
_COMMENTARY_TAIL_RE = _re.compile(r"commentary\s*:?")          # commentary 带/不带冒号
_GARBAGE_PREFIX_RE = _re.compile(r"^(functions?)\.", _re.IGNORECASE)  # functions. 前缀（OpenAI 包装格式）
_TRAILING_JUNK_RE = _re.compile(r"[^a-zA-Z0-9._-]+$")          # 尾部纯标点/中文等非合法字符
# 模型内部 tokenizer 标记（源头拦截，流式输出就干净）
_MODEL_INTERNAL_TOKEN_RE = _re.compile(
    r"<\s*[|]?\s*(?:unk(?:now)?|endoftext|startoftext|im_start|im_end|user|assistant|system|tool(?:_call(?:_result)?)?|tool_result|eos|bos)\s*[|]?\s*>",
    _re.IGNORECASE,
)

def _clean_tool_name(raw: str, known_names: set[str]) -> str:
    """从模型输出的脏工具名字符串中提取出真正的工具名。

    模型偶发会把 channel 标签、commentary、functions. 前缀、尾部 :: 等内控 token
    混入 tool name 字段，例如 "file.write<|channel|>"、"file.writecommentary"、
    "functions.file.write::"、"cluster.send_messagecommentary:foo"。

    清洗分两阶段：
      阶段 1（始终生效）：正则剥掉已知垃圾模式——不依赖白名单，快通道也有效。
      阶段 2（有白名单时）：在清洗后的字符串里查 known_names 子串，精确捞回工具名。
    """
    if not raw:
        return raw
    s = raw
    s = _PSEUDO_PROTOCOL_TAG_RE.sub("", s)
    s = _GARBAGE_PREFIX_RE.sub("", s, count=1)
    s = _COMMENTARY_TAIL_RE.sub("", s)
    s = _TRAILING_JUNK_RE.sub("", s).strip()
    s = s.strip(":-_")

    # 阶段 2：有白名单 → 子串匹配兜底（比纯正则更精准，能捞回 commentary:foo 里的 foo 部分）
    if known_names:
        if s in known_names:
            return s
        # 按长度降序匹配，优先命中最长候选（避免 "file" 抢了 "file.write"）
        for candidate in sorted(known_names, key=len, reverse=True):
            if candidate and candidate in s:
                return candidate
        # 子串没命中 → 返回阶段 1 的粗洗结果（白名单校验会拦住）

    return s or raw


# —— 工具名规范化：上游网关（DeepSeek 等）要求 function.name 匹配 ^[a-zA-Z0-9_-]+$（不含点号），
#    内部工具名用点号分隔（file.read / cluster.send_message），发往上游前统一替换为下划线，
#    接收模型返回的工具名后再由 chat() 内的 name_decode 映射还原为原名。函数是确定性的，
#    历史消息里的工具名与当前 tools 的编码结果保持一致。
_TOOL_NAME_ILLEGAL_RE = _re.compile(r"[^a-zA-Z0-9_-]")


def _encode_tool_name(name: str) -> str:
    if not name:
        return name
    return _TOOL_NAME_ILLEGAL_RE.sub("_", str(name)).strip("_") or "tool"


def _diag_payload(payload: dict, model: str) -> str:
    """生成 payload 诊断摘要（用于 4xx 时定位非法字段）。

    输出每条消息的 role + content 长度 + 是否有 tool_calls，
    方便对照服务端错误信息（如 Messages[5].Role invalid）定位具体问题条目。
    """
    msgs = payload.get("messages") or []
    lines = []
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            lines.append(f"  [{i}] <non-dict: {type(m).__name__}>")
            continue
        role = m.get("role")
        content = m.get("content")
        tc = m.get("tool_calls")
        tc_id = m.get("tool_call_id")
        seg = f"  [{i}] role={role!r}"
        if isinstance(content, str):
            seg += f" content_len={len(content)}"
        else:
            seg += f" content_type={type(content).__name__}"
        if tc:
            seg += f" tool_calls={[t.get('function',{}).get('name') for t in tc if isinstance(t,dict)]}"
        if tc_id:
            seg += f" tool_call_id={tc_id!r}"
        lines.append(seg)
    tools_info = ""
    if payload.get("tools"):
        tools_info = f" tools={[t.get('function',{}).get('name') for t in payload['tools'] if isinstance(t,dict)]}"
    tc_choice = payload.get("tool_choice", "(default)")
    return f"model={model} stream={payload.get('stream')} tool_choice={tc_choice}{tools_info}\n" + "\n".join(lines)


def _inject_reasoning(payload: dict, model: str, level: str) -> None:
    """根据模型名和推理等级，向 payload 注入对应的推理参数（使用 reasoning_detector 模块）。"""
    from core.reasoning_detector import detect_protocol, inject_reasoning_payload as _inject
    if not level or level == "off" or not model:
        return
    # llm.py 只有 interface dict，没有 base_url 的 protocol 检测上下文，
    # 用 openai-chat 作为默认协议，inject_reasoning_payload 内部会根据模型名做正确分发。
    _inject(payload, model, level, protocol="openai-chat")


def _sanitize_payload(payload: dict) -> None:
    """原地清洗 payload，修正对国产模型（glm/sensenova 等）常见的不兼容字段。

    修复清单（按命中概率降序）：
      1. tool parameters 里的空 required: [] 数组 → 删除该字段
         （部分国产模型严格校验，不接受空数组）
      2. tool_calls[].function.arguments 必须是字符串
         （OpenAI 规范要求字符串；历史存储时偶尔会出现 dict）
      3. tool_calls[].function.arguments 若是 JSON 对象字符串，保持原样；
         若是裸 dict，序列化为 JSON 字符串
      4. tool role 消息必须有 tool_call_id 字段（否则模型无法匹配）
    """
    # —— tools schema 清洗 ——
    tools = payload.get("tools")
    if isinstance(tools, list):
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            if not isinstance(fn, dict):
                continue
            params = fn.get("parameters")
            if isinstance(params, dict):
                req = params.get("required")
                if req is None or (isinstance(req, list) and len(req) == 0):
                    params.pop("required", None)

    # —— messages 清洗 ——
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    # Sensenova/glm 等只接受 developer/user/assistant/system/tool/function/root；
    # 我们统一规范化到 OpenAI 标准四角色（system/user/assistant/tool），所有模型都接受。
    # 旧规范映射：developer → system，function → tool。
    _ROLE_NORMALIZE = {"developer": "system", "function": "tool"}
    # 上一段 assistant.tool_calls 中保留下来（合法）的 tool_call id 集合，
    # 用于把「引用了被丢弃 tool_call」的 tool 消息降级为 user，避免悬空引用被网关 400。
    pending_tool_ids: set[str] = set()
    for idx in range(len(messages)):
        m = messages[idx]
        if not isinstance(m, dict):
            # 非 dict 的消息条目直接丢弃（用占位 user 消息保持索引连续，避免 tool 消息错位）
            messages[idx] = {"role": "user", "content": str(m) if m else ""}
            continue
        role = m.get("role")
        if not role or not isinstance(role, str):
            # role 缺失/空/非字符串 → 修正为 user（保留 content，避免 400）
            m["role"] = "user"
            role = "user"
        elif role in _ROLE_NORMALIZE:
            m["role"] = _ROLE_NORMALIZE[role]
            role = m["role"]
        elif role not in _OPENAI_ROLES:
            # 其他未知 role（root 等罕见值）→ 降级为 user，保留 content
            m["role"] = "user"
            role = "user"
        # assistant.tool_calls 清洗
        if role == "assistant" and isinstance(m.get("tool_calls"), list):
            cleaned = []
            for j, tc in enumerate(m["tool_calls"]):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                if not isinstance(fn, dict):
                    continue
                name = fn.get("name")
                if not name or not str(name).strip():
                    # 空/缺工具名 → 无效 tool_call：直接丢弃。
                    # 不能补空串发出去（上游会以 "function/name cannot be empty" 400 拒绝），
                    # 也不能靠 setdefault 硬撑 —— 该调用本就没有可执行目标。
                    continue
                args = fn.get("arguments")
                if not isinstance(args, str):
                    # dict/list → 序列化为 JSON 字符串；None → 空对象字符串
                    try:
                        args = json.dumps(args, ensure_ascii=False) if args else "{}"
                    except Exception:
                        args = "{}"
                fn["name"] = _encode_tool_name(str(name).strip())
                fn["arguments"] = args
                tc["function"] = fn
                tc["type"] = "function"
                if not tc.get("id"):
                    # 缺 id → 合成一个（部分网关要求非空 id；对应的 tool 消息若无匹配 id 会被降级）
                    tc["id"] = f"call_{idx}_{j}"
                cleaned.append(tc)
            if cleaned:
                m["tool_calls"] = cleaned
                pending_tool_ids = {tc.get("id", "") for tc in cleaned}
            else:
                m.pop("tool_calls", None)
                pending_tool_ids = set()
            continue
        # tool role 消息：必须匹配到上一条 assistant 的合法 tool_call 才能保留；
        # 否则（引用了被丢弃的空名调用 / 无主消息）降级为 user，避免网关拒绝。
        if role == "tool":
            tc_id = m.get("tool_call_id") or ""
            if tc_id in pending_tool_ids:
                # 正常匹配。tool_call_id 已由 runtime 在落库时填充，这里留空串或原值均可。
                continue
            content = m.get("content")
            m.clear()
            m["role"] = "user"
            m["content"] = content if content is not None else ""
            continue


async def chat(interface: dict, model: str, messages: list, stream: bool = False, tools: list | None = None,
               tool_choice: str = "auto", reasoning_level: str = "off") -> AsyncGenerator:
    """调用 LLM，支持流式/非流式。
    流式输出：yield ("text", str) | ("tool_call", {id, name, arguments}) | ("tool_result", {tool_call_id, content}) （仅 LLM 返回的 tool_call）
    非流式输出：yield ("message", dict) — 完整 assistant message（含可能的 tool_calls）

    流式新增的实时检测信号：
      ("tool_call_invalid", {"id", "name", "raw_args", "reason", "pattern_hit"})
        —— 流式累积过程中检测到致命错误（伪协议片段 / 未知工具名 / JSON 结构破坏），
           上层应停止该 tool_call 并注入纠错消息回灌循环。
      ("tool_call_warn", {"id", "name", "warnings"})
        —— 非致命警告（裸标识符等），仍会继续累积，最终 tool_call 会带 warnings 字段。
    """
    url = interface.get("url", "").rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {interface.get('api_key','')}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "stream": stream}
    # 工具名规范化反向映射（上游合法名 → 内部原名），流式接收 tool_call 时据此还原
    name_decode: dict[str, str] = {}
    # tools / tool_choice 注入策略（兼容性优先）：
    #   - tool_choice == "none" 时，国产模型（glm/sensenova 等）不接受 "tools + tool_choice:none" 共存，
    #     干脆不传 tools 字段 —— 模型看不到工具就不会发起 tool_calls，等价于强制文字输出，
    #     且对所有 OpenAI 兼容接口都安全。
    #   - 其他 tool_choice 值（auto/required/指定函数）才同时传 tools + tool_choice。
    if tools and tool_choice != "none":
        # 工具名规范化：内部名（可能带点号 file.read）→ 上游合法名（file_read），并记录反向映射
        _encoded_tools: list = []
        for _t in tools:
            if not isinstance(_t, dict):
                _encoded_tools.append(_t)
                continue
            _fn = _t.get("function") or {}
            if not isinstance(_fn, dict):
                _encoded_tools.append(_t)
                continue
            _nm = _fn.get("name")
            if _nm:
                _orig = str(_nm)
                _enc = _encode_tool_name(_orig)
                name_decode[_enc] = _orig
                _fn = {**_fn, "name": _enc}
            _encoded_tools.append({"type": _t.get("type", "function"), "function": _fn})
        payload["tools"] = _encoded_tools
        payload["tool_choice"] = tool_choice
    elif tool_choice != "none" and tool_choice != "auto":
        # 没传 tools 但指定了非默认 tool_choice（如 required）→ 仍传 tool_choice
        payload["tool_choice"] = tool_choice

    # —— 推理等级注入：根据模型名向 payload 添加 reasoning_effort / thinking 等参数 ——
    _inject_reasoning(payload, model, reasoning_level)

    # —— payload 兼容性清洗：修正对国产模型（glm/sensenova 等）常见的不兼容字段 ——
    _sanitize_payload(payload)

    async with httpx.AsyncClient(timeout=180) as client:
        if stream:
            # 流式解析 tool_calls（增量拼接）
            # 引入实时校验器：按 index 维护 StreamingToolValidator
            from core.tool_stream_validator import StreamingToolValidator
            # 工具白名单 + schema 索引（便于实时校验工具名 + 参数）
            known_tool_names: set[str] = set()
            tool_schemas: dict[str, dict] = {}
            for t in tools or []:
                fn = t.get("function") or {}
                nm = fn.get("name") or ""
                if nm:
                    _enc = _encode_tool_name(nm)
                    known_tool_names.add(_enc)
                    tool_schemas[_enc] = fn.get("parameters") or {}

            tc_buf: dict[int, dict] = {}        # index -> {id, name, args}
            tc_announced: set[int] = set()      # 已发出 tool_call_start 的 index
            tc_validators: dict[int, StreamingToolValidator] = {}
            tc_invalid: dict[int, dict] = {}    # index -> 致命错误信息（已广播）
            tc_warnings: dict[int, list[str]] = {}
            tc_aborted: set[int] = set()        # 已中止累积的 index（致命错误后停止喂 chunk）

            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                # —— 非 2xx 立即报错并记录诊断信息（含 messages role 序列），便于定位非法字段 ——
                if resp.status_code != 200:
                    _err_body = ""
                    try:
                        _err_body = (await resp.aread()).decode("utf-8", "replace")[:800]
                    except Exception:
                        pass
                    _logger.error(
                        "LLM upstream HTTP %s from %s | %s\n%s",
                        resp.status_code, model, _err_body, _diag_payload(payload, model),
                    )
                    yield ("error", {
                        "status": resp.status_code,
                        "detail": _err_body,
                        "payload_diag": _diag_payload(payload, model),
                    })
                    return
                _empty_streak = 0              # 每次调用重置退化检测
                _rep_detector = SentenceDetector()  # 初始化重复检测器
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "): continue
                    data = line[6:]
                    if data == "[DONE]": break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    delta = chunk["choices"][0].get("delta", {})

                    # 深度思考模型的 reasoning 通道 — 同样过滤模型内部 token
                    if delta.get("reasoning_content"):
                        _rtext = _MODEL_INTERNAL_TOKEN_RE.sub("", delta["reasoning_content"])
                        if _rtext:
                            yield ("reasoning", _rtext)

                    # 普通 content：先清洗模型内部 token，再透传
                    # （工具调用只可能出现在 tool_calls 通道；
                    #   正文里的伪协议串可能是文档内容本身，不做致命拦截，避免误伤）
                    if delta.get("content"):
                        ctext = _MODEL_INTERNAL_TOKEN_RE.sub("", delta["content"])
                        if ctext:  # 清洗后还有内容才 yield，避免空 chunk 白白浪费
                            # —— 重复检测（在 yield 前检查，避免重复内容下发到前端）——
                            trigger, rep_error = _rep_detector.check(ctext)
                            if trigger:
                                # 触发重复循环错误 → 立即中止本轮，后续内容不再 yield
                                raise rep_error
                            _empty_streak = 0
                            yield ("text", ctext)
                        else:
                            # 本 chunk 全为内部 token → 连续空计数；超过阈值判定模型退化
                            _empty_streak += 1
                            if _empty_streak >= _DEGENERATE_EMPTY_LIMIT:
                                raise ModelDegenerateError(
                                    f"模型连续输出 {_empty_streak} 个纯内部 token（<unk> 等），判定为退化，已中止本轮"
                                )

                    # tool_calls 增量
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        entry = tc_buf.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tc.get("id"): entry["id"] = tc["id"]
                        fn = tc.get("function", {})

                        # —— 名称首次确定 ——
                        if fn.get("name"):
                            raw_name = fn["name"]
                            # 清洗伪协议片段（<|channel|>commentary: 等）；白名单是上游合法名（下划线）
                            cleaned = _clean_tool_name(raw_name, known_tool_names)
                            # 还原为内部原名（可能带点号 file.read）供上层执行与展示
                            original = name_decode.get(cleaned, cleaned)
                            entry["name"] = original
                            # 名称首次确定 → 立即发出 tool_call_start，让前端流式显示"正在调用: xxx"
                            if idx not in tc_announced:
                                tc_announced.add(idx)
                                yield ("tool_call_start", {"id": entry["id"], "name": entry["name"]})

                            # —— 实时校验工具名是否在白名单内（白名单为上游合法名）——
                            # 仅当本轮确实注入了工具（known_tool_names 非空）才做白名单校验：
                            # 快通道（FAST_PATH_NO_TOOLS=True）不注入工具时 known_tool_names 为空，
                            # 此时模型若仍偷偷发出 tool_call，应正常累积以便上层"降级到慢通道"，
                            # 而不是被当成非法调用拦截（否则破坏既有降级机制）。
                            if known_tool_names and cleaned not in known_tool_names:
                                # 未知工具名 → fatal：立即广播 tool_call_invalid 并中止该 index 累积
                                reason = (
                                    f"工具名 {cleaned!r} 不在可用工具列表内；"
                                    f"可用工具：{', '.join(sorted(known_tool_names))[:200]}。"
                                    f"请只调用上述工具，不要臆造。"
                                )
                                tc_invalid[idx] = {
                                    "id": entry["id"], "name": entry["name"],
                                    "raw_args": "", "reason": reason,
                                    "pattern_hit": "unknown_tool_name",
                                }
                                tc_aborted.add(idx)
                                yield ("tool_call_invalid", tc_invalid[idx])
                                continue

                            # 创建该 index 的校验器（名称确定为上游合法名时一次性建好）
                            if idx not in tc_validators:
                                tc_validators[idx] = StreamingToolValidator(
                                    name=cleaned,
                                    schema=tool_schemas.get(cleaned, {}),
                                    known_tools=known_tool_names,
                                )

                        # —— 增量 arguments ——
                        if fn.get("arguments"):
                            arg_chunk = fn["arguments"]

                            # 已中止 → 不再累积，丢弃后续 chunk
                            if idx in tc_aborted:
                                continue

                            # 取/建校验器（极端情况：name 还没到就开始来 args，先建一个临时校验器）
                            validator = tc_validators.get(idx)
                            if validator is None:
                                _enc_name = _encode_tool_name(entry.get("name") or "")
                                validator = StreamingToolValidator(
                                    name=_enc_name,
                                    schema=tool_schemas.get(_enc_name, {}),
                                    known_tools=known_tool_names,
                                )
                                tc_validators[idx] = validator

                            # 实时喂 chunk 校验
                            res = validator.feed(arg_chunk)
                            if res.is_fatal:
                                # —— 致命错误：立即停止累积、广播 invalid 信号 ——
                                entry["args"] = validator.raw_args
                                tc_invalid[idx] = {
                                    "id": entry["id"], "name": entry["name"],
                                    "raw_args": validator.raw_args,
                                    "reason": res.reason,
                                    "pattern_hit": res.pattern_hit,
                                }
                                tc_aborted.add(idx)
                                yield ("tool_call_invalid", tc_invalid[idx])
                                continue
                            elif res.is_warn:
                                tc_warnings.setdefault(idx, []).append(res.reason)

                            entry["args"] += arg_chunk
                            # 参数增量实时广播 → 前端可流式显示正在接收的参数内容
                            yield ("tool_args_delta", {"id": entry["id"], "name": entry.get("name") or "", "delta": arg_chunk, "accumulated": entry["args"]})

                    # finish_reason = tool_calls 时，把完整 tool_calls 吐出去
                    finish = chunk["choices"][0].get("finish_reason")
                    if finish == "tool_calls":
                        # 按 index 排序
                        for idx in sorted(tc_buf.keys()):
                            entry = tc_buf[idx]
                            # 致命错误已经广播过 → 不再 emit tool_call，跳过
                            if idx in tc_invalid:
                                continue
                            # 流式结束仍未确定工具名 → 视为非法调用（避免把空名 tool_call 入库，
                            # 否则下一轮请求会被上游以 "function/name cannot be empty" 拒绝）
                            if not entry.get("name"):
                                tc_invalid[idx] = {
                                    "id": entry["id"], "name": "",
                                    "raw_args": entry.get("args") or "",
                                    "reason": "工具调用缺少函数名（function.name 为空），已拦截并跳过执行",
                                    "pattern_hit": "empty_tool_name",
                                }
                                yield ("tool_call_invalid", tc_invalid[idx])
                                continue
                            args_str = entry.get("args") or "{}"
                            validator = tc_validators.get(idx)
                            # 最终校验（JSON 闭合性 / 必填 / 类型）
                            final_res = validator.finalize() if validator else None
                            if final_res and final_res.is_fatal:
                                tc_invalid[idx] = {
                                    "id": entry["id"], "name": entry["name"],
                                    "raw_args": args_str,
                                    "reason": final_res.reason,
                                    "pattern_hit": final_res.pattern_hit,
                                }
                                yield ("tool_call_invalid", tc_invalid[idx])
                                continue
                            try:
                                args = json.loads(args_str)
                            except Exception:
                                # finalize 没拦住的兜底
                                tc_invalid[idx] = {
                                    "id": entry["id"], "name": entry["name"],
                                    "raw_args": args_str,
                                    "reason": f"工具参数 JSON 解析失败：{args_str[-200:]!r}",
                                    "pattern_hit": "json_parse_final",
                                }
                                yield ("tool_call_invalid", tc_invalid[idx])
                                continue
                            payload_out: dict = {"id": entry["id"], "name": entry["name"], "arguments": args}
                            if tc_warnings.get(idx):
                                payload_out["warnings"] = tc_warnings[idx]
                            yield ("tool_call", payload_out)
                        tc_buf = {}
                        tc_announced = set()
                        tc_validators = {}
                        tc_invalid = {}
                        tc_warnings = {}
                        tc_aborted = set()
            # —— 流截断检测：已 announce tool_call_start 但流结束时未 emit tool_call ——
            # 上游连接中断（RST）时 aiter_lines 可能正常结束（EOF）而非抛 ReadError，
            # 此时 finish_reason 非 "tool_calls"，已 announce 的 tool_call 不会走到 emit，
            # 若不处理则前端收到 TS:（正在调用）但永远等不到 C:（参数）和 R:（结果）→ 悬空
            for _idx in sorted(tc_announced):
                if _idx in tc_invalid:
                    continue
                _entry = tc_buf.get(_idx, {})
                if _entry.get("name"):
                    _logger.warning(
                        "LLM 流式中断：tool_call name=%s id=%s 已开始但参数未完整接收"
                        "（上游连接断开，已收 args %d 字符），上报 error 让上层终止而非悬空",
                        _entry["name"], _entry.get("id"), len(_entry.get("args", "")),
                    )
                    yield ("error", {
                        "status": 0,
                        "detail": f"上游流式输出中断：工具调用 {_entry['name']} 的参数未接收完整"
                                  f"（已收 {len(_entry.get('args', ''))} 字符），本轮已中止，请重试",
                    })
                    break
        else:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            yield ("message", data["choices"][0]["message"])


# —— 自动故障转移：连接断开时切换到下一个模型 ——
# 可触发切换的 HTTP 状态码（0 = 由网络异常推导，非真实 HTTP 状态）
_RETRIABLE_STATUS = {0, 408, 409, 429, 500, 502, 503, 504}
# 429 RPM 限流退避秒数：RPM 按分钟窗口计，立即重试必然再 429，
# 需要等待跨越至少一个完整窗口（10+20+40=70s），耗尽后仍未恢复才切换候选或透传错误
_429_BACKOFFS = [10, 20, 40]
# 可触发切换的网络/传输层异常（httpx.TransportError 覆盖 connect/read/write/timeout/protocol 等）


async def chat_with_failover(interface: dict, model: str, messages: list, stream: bool = False,
                             tools: list | None = None, tool_choice: str = "auto",
                             reasoning_level: str = "off") -> AsyncGenerator:
    """带自动故障转移的 chat 包装器。

    机制：
      - 候选模型 = [当前模型] + [interface.models[] 中其余模型]（去重、保持原始顺序）
      - 只有 1 个候选模型 → 同一模型重试 1 次（共 2 次尝试）
      - 有多个候选模型 → 依次尝试；连接断开 / 超时 / 5xx 时切换到下一个
      - 429（RPM 限流）特殊处理：先退避等待（10s/20s/40s）再重试同一模型，
        退避耗尽仍未恢复才切换到下一候选；RPM 是按分钟窗口计的，立即重试必然再 429
      - 流式安全：一旦已向调用方 yield 过内容，则锁定该模型不再中途切换
        （中途切换会导致前端收到重复/交错内容），此时直接透传错误
      - 4xx（除 408/409/429）视为请求本身非法 → 不切换，直接透传（换模型也会失败）
      - 全部候选失败 → yield ("error", last_error) 收尾
    """
    primary = model or (interface or {}).get("default_model") or "gpt-4o-mini"
    models_list = (interface or {}).get("models") or []
    candidates: list[str] = [primary]
    for m in models_list:
        if m and m not in candidates:
            candidates.append(m)

    # 单模型 → 重试 1 次；多模型 → 每个试一次
    attempts = [primary, primary] if len(candidates) == 1 else candidates

    last_error: dict = {"status": 0, "detail": "无可用模型"}

    # 429 RPM 限流退避计数：跨候选共享，防止单模型场景下退避次数被重置导致无限重试
    _429_retries_left = list(_429_BACKOFFS)

    for attempt_idx, try_model in enumerate(attempts):
        is_last = (attempt_idx == len(attempts) - 1)
        while True:  # 429 退避后重试同一模型；其他可重试错误切到下一候选
            started = False    # 是否已向调用方产出过内容
            _err_kind = None   # async for 内收到的错误（None=无错误）
            _err_payload = None
            try:
                async for kind, payload in chat(interface, try_model, messages, stream, tools, tool_choice, reasoning_level):
                    if kind == "error":
                        _err_kind = kind
                        _err_payload = payload
                        break  # 跳出 async for，循环后统一处理
                    started = True
                    yield (kind, payload)
                if _err_kind is None:
                    return  # async for 正常结束 = 本轮成功完成
                # —— 有错误 → 分类处理 ——
                status = int(_err_payload.get("status") or 0)
                last_error = _err_payload
                # 已产出内容 → 无法重试/切换（前端已收到部分内容），直接透传
                if started:
                    yield (_err_kind, _err_payload)
                    return
                if status == 429 and _429_retries_left:
                    # 限流：等待 RPM 窗口恢复后重试同一模型（立即重试必然再 429）
                    wait = _429_retries_left.pop(0)
                    _logger.warning("模型 %s 调用失败 (HTTP 429)，%.0f 秒后重试同一模型", try_model, wait)
                    await asyncio.sleep(wait)
                    continue  # 内层 while：退避后重试同一模型
                retriable = (status in _RETRIABLE_STATUS) or status >= 500
                if retriable and not is_last:
                    _logger.warning("模型 %s 调用失败 (HTTP %s)，切换到下一候选模型", try_model, status)
                    break  # 退出内层 while → 外层 for 取下一候选
                # 不可重试 / 429 退避耗尽 / 最后一次 → 透传错误
                yield (_err_kind, _err_payload)
                return
            except httpx.HTTPStatusError as e:
                # 非流式路径：raise_for_status() 抛出的 HTTP 错误
                status = e.response.status_code if e.response is not None else 0
                last_error = {"status": status, "detail": str(e)[:300]}
                if status == 429 and _429_retries_left and not started:
                    wait = _429_retries_left.pop(0)
                    _logger.warning("模型 %s 限流 (HTTP 429)，%.0f 秒后重试同一模型", try_model, wait)
                    await asyncio.sleep(wait)
                    continue  # 内层 while：重试同一模型
                retriable = (status in _RETRIABLE_STATUS) or status >= 500
                if retriable and not started and not is_last:
                    _logger.warning("模型 %s HTTP %s，切换到下一候选模型", try_model, status)
                    break  # 退出内层 while → 外层 for 取下一候选
                raise
            except httpx.TransportError as e:
                # 连接断开 / 超时 / 协议错误等传输层异常
                last_error = {"status": 0, "detail": f"{type(e).__name__}: {e}"}
                if not started and not is_last:
                    _logger.warning("模型 %s 连接异常 (%s)，切换到下一候选模型", try_model, type(e).__name__)
                    break  # 退出内层 while → 外层 for 取下一候选
                raise

    # 所有候选都失败
    _logger.error("全部候选模型均失败（尝试 %d 次）：%s", len(attempts), last_error)
    yield ("error", last_error)
