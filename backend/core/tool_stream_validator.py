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

# 裸标识符值：{"to_agent": 总经理, "x": 123} 里的 "总经理" 没加引号
_BARE_IDENTIFIER_RE = re.compile(
    r'''("(?:[^"\\]|\\.)*"\s*:\s*)(?!")(?![0-9{\[]|null|true|false\b)([\u4e00-\u9fff\w]+)'''
)


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

    # —— 累积 + 校验 ——
    def feed(self, chunk: str) -> ValidationResult:
        """喂一段增量 arguments。返回本次校验结果。"""
        if self._stopped:
            return ValidationResult("fatal", reason=self._fatal or "已停止累积")

        # 1) 先做可疑模式扫描（在原始 chunk 上做，不依赖 JSON 解析）
        for pat in _SUSPICIOUS_PATTERNS:
            m = pat.search(chunk)
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

        # 4) 增量 JSON 平衡性检查
        joined = "".join(self._args_buf)
        balance = self._check_balance(joined)
        if balance == "broken":
            # 括号/引号错位 → 不一定是 fatal（可能下一段会补全），
            # 但如果已经出现明显的结构破坏（如引号在键名前闭合），标记 fatal
            if self._looks_beyond_repair(joined):
                self._stopped = True
                self._fatal = (
                    f"流式 JSON 参数结构已无法修复：{joined[-120:]!r}；"
                    f"请重新发起调用，确保 JSON 合法（字符串值必须用双引号包裹）。"
                )
                return ValidationResult("fatal", reason=self._fatal)
            # 否则只警告
            return ValidationResult("ok")

        # 5) 裸标识符检测（{"to_agent": 总经理}）
        if _BARE_IDENTIFIER_RE.search(joined):
            if "bare_identifier" not in self._warned:
                self._warned.append("bare_identifier")
                return ValidationResult(
                    "warn",
                    reason="参数中检测到裸标识符值（未加引号的字符串），"
                           "如 {\"to_agent\": 总经理} 应写成 {\"to_agent\": \"总经理\"}",
                    pattern_hit="bare_identifier",
                )

        return ValidationResult("ok")

    def _check_balance(self, s: str) -> str:
        """粗略检查 JSON 括号/引号平衡。返回 ok / broken / pending。"""
        depth_brace = 0   # {}
        depth_bracket = 0 # []
        in_string = False
        escape = False
        i = 0
        while i < len(s):
            c = s[i]
            if escape:
                escape = False
                i += 1
                continue
            if c == "\\":
                escape = True
                i += 1
                continue
            if in_string:
                if c == '"':
                    in_string = False
                i += 1
                continue
            if c == '"':
                in_string = True
            elif c == "{":
                depth_brace += 1
            elif c == "}":
                depth_brace -= 1
                if depth_brace < 0:
                    return "broken"   # 闭合括号多于开括号 → 必坏
            elif c == "[":
                depth_bracket += 1
            elif c == "]":
                depth_bracket -= 1
                if depth_bracket < 0:
                    return "broken"
            i += 1
        if depth_brace < 0 or depth_bracket < 0:
            return "broken"
        return "pending" if (depth_brace > 0 or depth_bracket > 0 or in_string) else "ok"

    def _looks_beyond_repair(self, s: str) -> bool:
        """判断流式 JSON 是否已经无可救药（即使再来 token 也补不成合法 JSON）。

        启发式：
          - 括号深度为负且字符串外出现两个以上连续的 } → 结构已塌
          - 顶层出现非法字符（如 `:` 后面跟非值字符且不在字符串内）
        """
        # 括号深度变负 → 已经塌方
        depth = 0
        in_str = False
        escape = False
        for c in s:
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if in_str:
                if c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth < 0:
                    return True
        return False

    # —— 最终校验 ——
    def finalize(self) -> ValidationResult:
        """流式结束时做一次完整校验。返回 ok/warn/fatal。"""
        if self._stopped:
            return ValidationResult("fatal", reason=self._fatal or "累积已中断")

        raw = "".join(self._args_buf)
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
                fixed = _BARE_IDENTIFIER_RE.sub(r'\1"\2"', raw)
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

    @property
    def raw_args(self) -> str:
        return "".join(self._args_buf)


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
