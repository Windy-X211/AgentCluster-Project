"""文本化工具调用（伪协议块）剥离与转换的单元测试。

覆盖上游把 function calling 序列化进正文的两种真实格式：
  变体 A：<||DSML||tool_calls> / <||DSML||invoke name="...">（无空格）
  变体 B：<||DSML|| calls>   / <||DSML|| invoke name="...">（特殊 token 后带空格）
重点验证：整块剥离、跨 chunk 拆分、未闭合块丢弃、正常正文不误伤。
说明：token 用拼接构造，避免源文件被上层链路改写成另一种形态。
"""
from __future__ import annotations

from core.llm import (
    PseudoToolBlockFilter,
    _parse_pseudo_tool_calls,
    _pseudo_tool_events,
)

_V = "｜"
_T = "<" + _V * 2 + "DSML" + _V * 2
_C = "</" + _V * 2 + "DSML" + _V * 2

BLOCK_A = f"""{_T}tool_calls>
{_T}invoke name="file_write">
{_T}parameter name="path" string="true">兵部呈-军务方略.md{_C}parameter>
{_T}parameter name="content" string="true"># 标题

第一段。{_C}parameter>
{_C}invoke>
{_C}tool_calls>"""

BLOCK_B = f"""{_T} calls>
{_T} invoke name="desktop_control">
{_T} parameter name="action" string="true">hotkey{_C} parameter>
{_T} parameter name="keys" string="false">["ctrl", "end"]{_C} parameter>
{_C} invoke>
{_C} calls>"""

KNOWN = {"file_write", "desktop_control"}
DECODE = {"file_write": "file.write", "desktop_control": "desktop_control"}


def _collect(text: str, chunk_size: int = 0) -> tuple[str, list[str]]:
    """按指定粒度喂入，返回 (正文, 完整伪块列表)。chunk_size=0 表示一次性喂入。"""
    filt = PseudoToolBlockFilter()
    out, blocks = "", []
    if chunk_size <= 0:
        o, b = filt.feed(text)
        out += o
        blocks += b
    else:
        for i in range(0, len(text), chunk_size):
            o, b = filt.feed(text[i:i + chunk_size])
            out += o
            blocks += b
    out += filt.flush()
    return out, blocks


class TestStripAndParse:
    def test_variant_a_block_is_stripped_and_parsed(self):
        text = "正文开头。\n\n" + BLOCK_A + "\n\n正文结尾。"
        body, blocks = _collect(text)
        assert body == "正文开头。\n\n\n\n正文结尾。"
        assert len(blocks) == 1
        calls = _parse_pseudo_tool_calls(blocks[0], KNOWN, DECODE)
        assert len(calls) == 1
        assert calls[0]["name"] == "file.write"
        assert calls[0]["encoded"] == "file_write"
        assert calls[0]["arguments"]["path"] == "兵部呈-军务方略.md"
        assert calls[0]["arguments"]["content"] == "# 标题\n\n第一段。"

    def test_variant_b_with_spaces_is_stripped_and_parsed(self):
        body, blocks = _collect("执行。\n" + BLOCK_B)
        assert body.strip() == "执行。"
        calls = _parse_pseudo_tool_calls(blocks[0], KNOWN, DECODE)
        assert calls[0]["name"] == "desktop_control"
        assert calls[0]["arguments"] == {"action": "hotkey", "keys": ["ctrl", "end"]}

    def test_cross_chunk_one_char_at_a_time(self):
        text = "前" + BLOCK_B + "后"
        body, blocks = _collect(text, chunk_size=1)
        assert body == "前后"
        assert len(blocks) == 1
        assert _parse_pseudo_tool_calls(blocks[0], KNOWN, DECODE)[0]["name"] == "desktop_control"

    def test_cross_chunk_mid_token(self):
        text = "前" + BLOCK_B + "后"
        cut = text.index(_T + " calls>") + 3
        filt = PseudoToolBlockFilter()
        out1, b1 = filt.feed(text[:cut])
        out2, b2 = filt.feed(text[cut:])
        tail = filt.flush()
        assert out1 + out2 + tail == "前后"
        assert len(b1 + b2) == 1

    def test_unclosed_block_is_discarded(self):
        filt = PseudoToolBlockFilter()
        out, blocks = filt.feed("前文 " + _T + 'tool_calls>\n' + _T + 'invoke name="file_write">\n')
        tail = filt.flush()
        assert out + tail == "前文 "
        assert blocks == []


class TestNoFalsePositive:
    def test_normal_markdown_and_html_untouched(self):
        text = "# 标题\n\n<div class=\"x\">hello</div>\n\n`a < b` 和 `c > d`\n\n```python\nprint(1)\n```"
        body, blocks = _collect(text, chunk_size=7)
        assert body == text
        assert blocks == []

    def test_angle_bracket_chunks_are_flushed(self):
        filt = PseudoToolBlockFilter()
        out1, _ = filt.feed("a <")
        out2, _ = filt.feed("b> c")
        tail = filt.flush()
        assert out1 + out2 + tail == "a <b> c"

    def test_stray_token_removed_on_flush(self):
        filt = PseudoToolBlockFilter()
        out, _ = filt.feed("正文" + _T)
        tail = filt.flush()
        assert out + tail == "正文"


class TestEvents:
    def test_whitelisted_tool_emits_tool_call_events(self):
        _, blocks = _collect(BLOCK_B)
        events = _pseudo_tool_events(blocks, KNOWN, DECODE, set())
        kinds = [k for k, _ in events]
        assert kinds == ["tool_call_start", "tool_args_delta", "tool_call"]
        assert events[-1][1]["name"] == "desktop_control"
        assert events[-1][1]["arguments"]["keys"] == ["ctrl", "end"]

    def test_unknown_tool_is_stripped_but_not_executed(self):
        _, blocks = _collect(BLOCK_B)
        events = _pseudo_tool_events(blocks, {"file_write"}, DECODE, set())
        assert events == []

    def test_no_tools_injected_means_strip_only(self):
        _, blocks = _collect(BLOCK_B)
        assert _pseudo_tool_events(blocks, set(), DECODE, set()) == []

    def test_duplicate_calls_deduplicated(self):
        _, blocks = _collect(BLOCK_B + "\n" + BLOCK_B)
        events = _pseudo_tool_events(blocks, KNOWN, DECODE, set())
        assert len([k for k, _ in events if k == "tool_call"]) == 1