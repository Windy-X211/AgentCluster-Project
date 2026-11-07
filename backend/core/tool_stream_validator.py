"""流式工具调用参数的实时检测机制

在 LLM 流式输出 tool_calls.arguments 的过程中，对每一段增量 token 做实时校验：
  - JSON 结构是否逐步平衡（括号 / 引号 / 转义）
  - 是否出现可疑的伪协议片段（<|channel|> / commentary: / <|startofcontent|> / 裸标识符等）
  - 工具名是否在可用工具白名单内
  - JSON 闭合后做必填参数 + 类型粗校验

一旦发现「致命错误」，立即返回 fatal 信号，让 llm.py 中断流式累积、
让 agent_runtime.py 注入纠错消息回灌循环，模型可基于反馈自我修正。

返回等级：
  ok    —— 当前片段通过，继续累积
  warn  —— 发现可疑迹象但不致命（继续累积，最终结果会带 warning）
  fatal —— 致命错误，必须立即停止累积并纠错

核心目标：杜绝 "把错误的流式片段补完" —— 错了就停，停了就纠。

【性能约束 —— 为什么必须增量扫描】
本模块处在 LLM 流式回调的热路径上（llm.py: chat() → validator.feed()），
且 feed() 是在 asyncio 事件循环里同步执行的。写长文件（file.write 的 content
动辄几十~几百 KB）时，上游会把 arguments 切成成百上千个 chunk 逐块下发。

若每次 feed 都对「已累积的全部参数」重新 join + 重新全量扫描，就是 O(n²)：
  · "".join(args_buf)                —— 每块都重建整个字符串
  · _check_balance(joined)           —— 每块都逐字符重扫一遍（纯 Python 循环）
  · _BARE_IDENTIFIER_RE.search(joined) —— 每块都重扫；该正则的键侧
    "(?:[^"\\\\]|\\\\.)*" 含嵌套量词，在含大量转义引号的正文上会灾难性回溯
    （实测 141KB 参数单次 search 要 35 秒）

实测：一份 12KB 的长文件参数，现状全量重扫累计 22 秒 —— 事件循环被占死，
/api/health 超时 → 托盘误报「后端 FAIL」，上游流同时被拖到超时。
（现场证据：backend/diag/stall-*.log 的主线程栈停在 tool_stream_validator.feed()）

因此本模块改为**增量扫描**：跨 chunk 维护结构状态机（括号深度 / 字符串态 /
转义位 / 值位置），每块只看新到的字符，整体 O(n)。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# —— 可疑伪协议片段：模型一旦把通道标签当成正文/参数输出，基本就是格式错乱 ——
_SUSPICIOUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p)
    for p in (
        r"<\|channel\|>",
        r"<\|startofcontent\|>",
        r"<\|endofcontent\|>",
        r"<\|startofspeech\|>",
        r"<\|endofspeech\|>",
        r"\bcommentary\s*:",      # file.write<|channel|>commentary: {...}
        r"\bfunctions\.\w+\s*::", # functions.file.write:0:: 这类伪标签
        r"<\|[^|]*\|>",            # 任意 <|xxx|> 形式的伪标签
    )
]

# 裸标识符值：{"to_agent": 总经理} —— 引号缺失的值。
# 只判定「值」侧（键侧由扫描器的字符串态天然保证），且匹配一段完整的词，
# 语义与旧正则的值侧完全一致：
#   不以 " 开头、不以数字/{/[ 开头、不是 null/true 字面量、
#   "false" 需带词边界（falsex 仍算裸标识符），且整段由词字符组成。
_BARE_VALUE_RE = re.compile(
    r'''(?!")(?![0-9{\[]|null|true|false\b)([\u4e00-\u9fff\w]+)'''
)

# 伪协议扫描的重叠窗口大小：标记可能被切在两块之间（如 "<|chan" + "nel|>"），
# 保留上一块尾部这么多字符与本块一起扫。固定大小 → 整体仍是 O(n)。
_SCAN_OVERLAP = 64


@dataclass
class ValidationResult:
    """单次校验结果。"""

    level: str            # "ok" | "warn" | "fatal"
    reason: str = ""      # 致命/警告原因（人类可读，会回灌给模型）
    pattern_hit: str = "" # 命中的可疑模式（供调试）
    warnings: list[str] = field(default_factory=list)

    @property
    def is_fatal(self) -> bool:
        return self.level == "fatal"

    @property
    def is_warn(self) -> bool:
        return self.level == "warn"


def _is_bare_value_word(word: str) -> bool:
    """判断一个词是否是「引号缺失的值」（裸标识符）。

    与旧正则的值侧判定保持一致：
      - 不以数字 / { / [ 开头
      - 不是 null / true 字面量；false 需带词边界（falsex 仍算裸标识符）
      - 整段由词字符（含汉字）组成
    """
    if not word:
        return False
    return _BARE_VALUE_RE.match(word) is not None


class _ArgScanner:
    """增量扫描工具参数 JSON：跨 chunk 保留状态，每块只处理新字符。

    维护的状态：
      - depth_brace / depth_bracket：当前括号深度
      - min_depth_brace：花括号深度出现过的最小值（<0 说明出现多余的 '}'，
        结构已不可修复 —— 对应旧实现的 _looks_beyond_repair）
      - min_depth_bracket：方括号深度出现过的最小值（<0 只是括号不匹配，
        旧实现对此不判 fatal）
      - in_string / escape：字符串态与转义位。转义位必须跨 chunk 保留，
        因为 "\\\"" 的转义引号可能正好被切在两块之间
      - 裸标识符检测：last_was_string / after_colon / value_text
    """

    def __init__(self) -> None:
        self.depth_brace = 0
        self.depth_bracket = 0
        self.min_depth_brace = 0
        self.min_depth_bracket = 0
        self.in_string = False
        self.escape = False
        # —— 裸标识符检测状态 ——
        self.last_was_string = False  # 上一个结构性字符是刚闭合的字符串（可能是键）
        self.after_colon = False      # 已确认「键 : 」→ 下一个是值
        self.value_text = ""          # 正在累积的值文本
        self.in_value = False
        self.bare_value = False       # 已确认存在裸标识符

    # —— 结构扫描 ——
    def feed(self, text: str) -> None:
        for c in text:
            if self.escape:
                self.escape = False
                continue
            if c == "\\":
                self.escape = True
                continue
            # —— 裸标识符检测：值文本累积 ——
            # 必须排在引号/括号处理之前：任何非词字符都终止「值」，
            # 此时才能判定它是 true/false/null 字面量还是裸标识符。
            if self.in_value:
                if c.isalnum() or c == "_":
                    self.value_text += c
                    if len(self.value_text) > 16:
                        # 太长不可能是 null/true/false → 立刻判定为裸标识符
                        if _is_bare_value_word(self.value_text):
                            self.bare_value = True
                        self.value_text = ""
                        self.in_value = False
                    continue
                # 到达分隔符 → 判定
                if _is_bare_value_word(self.value_text):
                    self.bare_value = True
                self.value_text = ""
                self.in_value = False
                # 继续用当前分隔符走下面的逻辑
            if self.in_string:
                if c == '"':
                    self.in_string = False
                    self.last_was_string = True
                continue
            if c == '"':
                self.in_string = True
                self.last_was_string = False
                continue
            if c == "{":
                self.depth_brace += 1
            elif c == "}":
                self.depth_brace -= 1
                if self.depth_brace < self.min_depth_brace:
                    self.min_depth_brace = self.depth_brace
            elif c == "[":
                self.depth_bracket += 1
            elif c == "]":
                self.depth_bracket -= 1
                if self.depth_bracket < self.min_depth_bracket:
                    self.min_depth_bracket = self.depth_bracket
            if c == ":":
                if self.last_was_string:
                    self.after_colon = True
                self.last_was_string = False
                continue
            if c.isspace():
                continue
            if self.after_colon:
                self.after_colon = False
                if c.isalpha() or "\u4e00" <= c <= "\u9fff":
                    # 值的开头是字母/汉字 → 可能是裸标识符，也可能是 true/false/null
                    self.in_value = True
                    self.value_text = c
                    continue
                # 数字 / { / [ / " 等 → 合法值，不是裸标识符
                self.last_was_string = False
                continue
            self.last_was_string = False

    @property
    def brace_broken(self) -> bool:
        """花括号结构已不可修复（多余的 '}'）—— 对应旧 _looks_beyond_repair。"""
        return self.min_depth_brace < 0

    @property
    def bracket_broken(self) -> bool:
        """方括号结构不匹配（旧实现不因此判 fatal，只是不再检查裸标识符）。"""
        return self.min_depth_bracket < 0

    @property
    def pending(self) -> bool:
        """结构尚未闭合（还有未关的括号或未闭合的字符串）。"""
        return self.depth_brace > 0 or self.depth_bracket > 0 or self.in_string


class StreamingToolValidator:
    """单个 tool_call 流式累积器 + 校验器。

    用法：
        v = StreamingToolValidator(name="file.write", schema={...})
        for chunk in argument_chunks:
            res = v.feed(chunk)
            if res.is_fatal:
                # 立即停止累积，纠错
                break
        final = v.finalize()  # JSON 闭合后做完整校验
    """

    def __init__(self, name: str | None = None, schema: dict | None = None,
                 known_tools: set[str] | None = None):
        self.name = name or ""
        self.schema = schema or {}
        self.known_tools = known_tools or set()
        self._args_buf: list[str] = []
        self._warned: list[str] = []
        self._fatal: str = ""
        self._stopped = False
        # 增量扫描器（跨 chunk 保留状态）
        self._scanner = _ArgScanner()
        # 伪协议扫描的重叠窗口：标记可能被切在两块之间（如 "<|chan" + "nel|>")，
        # 保留上一块尾部若干字符与本块一起扫，避免漏检。窗口固定大小 → 仍是 O(n)。
        self._scan_tail = ""

    # —— 累积 + 校验 ——
    def feed(self, chunk: str) -> ValidationResult:
        """喂一段增量 arguments。返回本次校验结果。"""
        if self._stopped:
            return ValidationResult("fatal", reason=self._fatal or "已停止累积")

        # 1) 先做可疑模式扫描（带上上一块的尾部，避免标记被切在块边界时漏检）
        scan_text = self._scan_tail + chunk
        for pat in _SUSPICIOUS_PATTERNS:
            m = pat.search(scan_text)
            if m:
                self._stopped = True
                self._fatal = (
                    f"流式参数中检测到非法伪协议片段：{m.group(0)!r}；"
                    "正确格式应为：工具名 + 纯 JSON 参数（如 cmd.run: {'command': '...'}），"
                    "不要在参数里夹带 <|channel|> / commentary: 等通道标签。"
                )
                return ValidationResult(
                    "fatal", reason=self._fatal, pattern_hit=m.group(0)
                )
        self._scan_tail = scan_text[-_SCAN_OVERLAP:]

        # 2) 工具名校验（名称一旦确定就检查）
        if self.name and self.known_tools and self.name not in self.known_tools:
            # 不在白名单 → fatal（避免模型瞎编工具名）
            self._stopped = True
            self._fatal = (
                f"工具名 {self.name!r} 不在可用工具列表内；"
                f"可用工具：{', '.join(sorted(self.known_tools))[:200]}。"
                f"请只调用上述工具，不要臆造。"
            )
            return ValidationResult("fatal", reason=self._fatal)

        # 3) 累积到 buffer
        self._args_buf.append(chunk)

        # 4) 增量 JSON 平衡性检查（O(chunk)，状态跨块保留）
        self._scanner.feed(chunk)
        if self._scanner.brace_broken:
            # 花括号深度变负 → 结构已无法修复
            self._stopped = True
            self._fatal = (
                f"流式 JSON 参数结构已无法修复：{self._tail(120)!r}；"
                f"请重新发起调用，确保 JSON 合法（字符串值必须用双引号包裹）。"
            )
            return ValidationResult("fatal", reason=self._fatal)
        if self._scanner.bracket_broken:
            # 方括号不匹配：结构仍可继续（旧实现同样不判 fatal），
            # 但这一轮不再检查裸标识符
            return ValidationResult("ok")

        # 5) 裸标识符检测（增量：只看新到的字符，O(chunk)）
        if self._scanner.bare_value and "bare_identifier" not in self._warned:
            self._warned.append("bare_identifier")
            return ValidationResult(
                "warn",
                reason="参数中检测到裸标识符值（未加引号的字符串），"
                       "如 {\"to_agent\": 总经理} 应写成 {\"to_agent\": \"总经理\"}",
                pattern_hit="bare_identifier",
            )

        return ValidationResult("ok")

    @property
    def raw_args(self) -> str:
        return "".join(self._args_buf)

    def _tail(self, n: int) -> str:
        """取累积参数末尾 n 个字符（用于错误提示），只扫尾部不重扫全文。"""
        if not self._args_buf:
            return ""
        tail = ""
        for part in reversed(self._args_buf):
            tail = part + tail
            if len(tail) >= n:
                break
        return tail[-n:]

    # —— 最终校验 ——
    def finalize(self) -> ValidationResult:
        """流式结束时做一次完整校验。返回 ok/warn/fatal。"""
        if self._stopped:
            return ValidationResult("fatal", reason=self._fatal or "累积已中断")

        raw = self.raw_args
        if not raw.strip():
            # 空参数
            if self.schema and self.schema.get("required"):
                return ValidationResult(
                    "fatal",
                    reason=f"工具 {self.name} 缺少必填参数：{self.schema['required']}",
                )
            return ValidationResult("ok")

        # 尝试 JSON 解析
        try:
            args = json.loads(raw)
        except json.JSONDecodeError as e:
            # 尝试裸标识符修复
            try:
                fixed = fix_bare_identifiers(raw)
                args = json.loads(fixed)
            except Exception:
                return ValidationResult(
                    "fatal",
                    reason=(
                        f"工具参数 JSON 解析失败：{e.msg}（位置 {e.pos}）；"
                        f"原始片段末尾：{raw[-200:]!r}；"
                        f"请确保参数是合法 JSON，字符串值用双引号包裹。"
                    ),
                )

        # 类型 / 必填校验
        props = (self.schema or {}).get("properties") or {}
        required = set((self.schema or {}).get("required") or [])

        if isinstance(args, dict):
            # 必填缺失
            missing = [k for k in required if k not in args]
            if missing:
                return ValidationResult(
                    "fatal",
                    reason=f"工具 {self.name} 缺少必填参数：{missing}",
                )
            # 类型粗校验
            for k, v in args.items():
                if k in props:
                    expected = props[k].get("type")
                    if expected and not _type_ok(v, expected):
                        return ValidationResult(
                            "fatal",
                            reason=f"参数 {k} 期望类型 {expected}，实际 {type(v).__name__}",
                        )

        # 警告汇总
        if self._warned:
            return ValidationResult(
                "warn",
                reason=";".join(self._warned),
                pattern_hit=",".join(self._warned),
            )

        return ValidationResult("ok")


def fix_bare_identifiers(raw: str) -> str:
    """把 JSON 里的裸标识符值修复成字符串。

    例：{"to_agent": 总经理, "x": 123} → {"to_agent": "总经理", "x": 123}
    策略：匹配 "key": 后面紧跟的、非引号非数字非 true/false/null 的标识符，加引号。

    实现说明：旧实现用正则一次 sub 整个字符串，键侧 "(?:[^"\\\\]|\\\\.)*"
    含嵌套量词，在长正文上会灾难性回溯（141KB 参数实测要 36 秒）。
    这里改成单遍线性扫描，只在「引号闭合的键 : 值」的位置判定，整体 O(n)。
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    in_string = False
    escape = False
    last_was_string = False
    after_colon = False
    while i < n:
        c = raw[i]
        if escape:
            escape = False
            out.append(c)
            i += 1
            continue
        if c == "\\":
            escape = True
            out.append(c)
            i += 1
            continue
        if in_string:
            out.append(c)
            if c == '"':
                in_string = False
                last_was_string = True
            i += 1
            continue
        if c == '"':
            in_string = True
            last_was_string = False
            out.append(c)
            i += 1
            continue
        if after_colon:
            if c.isspace():
                out.append(c)
                i += 1
                continue
            after_colon = False
            if c.isalpha() or "\u4e00" <= c <= "\u9fff":
                # 收集整个词再判定（要区分 true/false/null 等字面量）
                j = i
                while j < n and (raw[j].isalnum() or raw[j] == "_"):
                    j += 1
                word = raw[i:j]
                if _is_bare_value_word(word):
                    out.append('"' + word + '"')
                else:
                    out.append(word)
                i = j
                continue
            out.append(c)
            i += 1
            continue
        if c == ":":
            if last_was_string:
                after_colon = True
            last_was_string = False
            out.append(c)
            i += 1
            continue
        if c.isspace():
            out.append(c)
            i += 1
            continue
        last_was_string = False
        out.append(c)
        i += 1
    return "".join(out)


def _type_ok(value: Any, expected: str) -> bool:
    """粗略的类型匹配（OpenAI schema 类型 → Python 类型）。"""
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True  # 未知类型不校验


# —— 便捷函数：针对单个 chunk 做一次性可疑模式扫描（用于 content 通道同样适用）——
def scan_for_pseudo_protocol(text: str) -> tuple[bool, str]:
    """扫描文本是否包含伪协议片段，返回 (命中, 命中片段)。"""
    for pat in _SUSPICIOUS_PATTERNS:
        m = pat.search(text)
        if m:
            return True, m.group(0)
    return False, ""
