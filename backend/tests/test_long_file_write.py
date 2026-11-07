"""长文件写入导致后端 failed 的回归测试。

背景（线上故障）：
  智能体用 file.write 写长文时，arguments 分成上千个 chunk 流式到达。
  旧版 StreamingToolValidator.feed() 每块都对「已累积全文」重新 join +
  逐字符重扫 + 跑一个含嵌套量词的正则 → O(n²) 且正则灾难性回溯，
  单次 feed 可阻塞事件循环数百毫秒，累计数十秒 → /api/health 超时 →
  托盘报「后端 FAIL」，上游流同时被拖到超时。

  另外，写完的大文件内容会作为 assistant.tool_calls 原样回灌给 LLM，
  每轮白白烧掉几万~几十万 token，上下文爆掉 → 上游 400 → 整轮失败。

覆盖：
  1. 增量扫描：长参数下 feed() 必须是 O(n)，单次延迟不阻塞事件循环
  2. 语义等价：fatal/warn 判定与旧实现一致（且修掉旧实现的字面量误报）
  3. 参数回灌裁剪：发给 LLM 的副本被裁剪，落库那份保持完整
"""
from __future__ import annotations

import json
import time

import pytest

from core.tool_stream_validator import StreamingToolValidator, fix_bare_identifiers
from core.agent_runtime import _history_llm_entry, _trim_tool_calls_for_llm


def _long_file_args(blocks: int = 300) -> str:
    """构造一份「写长报告」的真实参数：大量中文 + 转义引号 + 换行。"""
    body = ('这是一段很长的中文内容，包含引号 "key": value 和换行以及反斜杠。' * blocks)
    content = body.replace('"', '\\"')
    return '{"path": "report.md", "content": "' + content + '"}'


# ════════════════════════════════════════════════════════════════════
# 1. 性能：长参数下 feed 必须是 O(n)
# ════════════════════════════════════════════════════════════════════

def test_long_args_feed_is_linear_not_quadratic():
    """长参数分块喂入，累计耗时必须远小于旧实现的 O(n²)。"""
    args = _long_file_args(300)
    chunk = 64
    chunks = [args[i:i + chunk] for i in range(0, len(args), chunk)]

    v = StreamingToolValidator(name="file.write", known_tools={"file.write"})
    t0 = time.perf_counter()
    for c in chunks:
        v.feed(c)
    elapsed = time.perf_counter() - t0

    # 旧实现在同一输入上要 ~28 秒（O(n²) + 正则回溯）。
    # 这里留足余量：增量实现应在 1 秒内完成。
    assert elapsed < 1.0, f"长参数 feed 累计耗时 {elapsed:.2f}s，仍是 O(n²) 级别"
    assert v.finalize().level == "ok"


def test_long_args_single_feed_does_not_block_event_loop():
    """单次 feed 延迟必须够低 —— 这是拖垮事件循环的直接原因。"""
    args = _long_file_args(300)
    half = len(args) // 2
    v = StreamingToolValidator(name="file.write", known_tools={"file.write"})
    v.feed(args[:half])          # 先喂掉一半
    t0 = time.perf_counter()
    v.feed(args[half:])         # 再喂另一半，测单次延迟
    dt = time.perf_counter() - t0
    # 旧实现这里要 ~386ms（单次 feed 阻塞事件循环近 0.4 秒）
    assert dt < 0.05, f"单次 feed 延迟 {dt*1000:.0f}ms，仍会阻塞事件循环"


def test_very_long_content_with_many_escaped_quotes():
    """含大量转义引号的长内容（旧实现正则回溯最严重的形态）。"""
    body = '\\"key\\": value ' * 5000
    args = '{"path": "r.md", "content": "' + body + '"}'
    v = StreamingToolValidator(name="file.write", known_tools={"file.write"})
    t0 = time.perf_counter()
    for i in range(0, len(args), 64):
        v.feed(args[i:i + 64])
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"大量转义引号场景耗时 {elapsed:.2f}s"
    assert v.finalize().level == "ok"


# ════════════════════════════════════════════════════════════════════
# 2. 语义等价：判定结果不能因重构而改变
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("chunk_size", [1, 3, 7, 64, 100000])
@pytest.mark.parametrize("args,name,expect", [
    ('{"path": "a.md", "content": "hello"}', "file.write", "ok"),
    ('{"to_agent": 总经理, "x": 1}', "cluster.send_message", "warn"),
    ('{"flag": true, "z": null}', "file.write", "ok"),
    ('{"n": 123}', "file.write", "ok"),
    ('{"list": [1, 2]}', "file.write", "ok"),
    ('{"a": 1}}', "file.write", "fatal"),      # 多余的 }
    ('{"a": "unclosed', "file.write", "fatal"),  # 字符串未闭合 → finalize 失败
    ('{"content": "<|channel|>commentary: hi"}', "file.write", "fatal"),
])
def test_validator_verdicts(args, name, expect, chunk_size):
    """分块切分不应改变最终判定结论。"""
    v = StreamingToolValidator(name=name, known_tools={name})
    for i in range(0, len(args), chunk_size):
        v.feed(args[i:i + chunk_size])
    assert v.finalize().level == expect


def test_literal_prefix_not_mistaken_for_bare_identifier():
    """true/false/null 的流式半截前缀不能被误判成裸标识符（旧实现的 bug）。

    旧实现用正则扫全文，`{"flag": t`（true 还没传完）就会被判成裸标识符并
    回灌纠错反馈，让模型以为自己写错了。新实现必须等值词终止才判定。
    """
    for literal in ("true", "false", "null"):
        v = StreamingToolValidator(name="file.write", known_tools={"file.write"})
        full = '{"flag": ' + literal + '}'
        # 逐字符喂入，全程不得出现 warn
        for i in range(0, len(full), 1):
            res = v.feed(full[i:i + 1])
            assert not res.is_warn, f"{literal} 的半截前缀被误判为裸标识符"
        assert v.finalize().level == "ok", f"{literal} 被误判为裸标识符"


def test_bare_identifier_still_detected():
    """真正的裸标识符仍要检出（这是给模型的纠错信号，不能丢）。"""
    v = StreamingToolValidator(name="cluster.send_message", known_tools={"cluster.send_message"})
    args = '{"to_agent": 总经理, "x": 1}'
    warned = False
    for i in range(0, len(args), 1):
        res = v.feed(args[i:i + 1])
        if res.is_warn:
            warned = True
    assert warned, "裸标识符未被检出"
    # 该参数本身不是合法 JSON，但修复后能解析 → 结论是 warn（可自动修复），不是 fatal
    assert v.finalize().level == "warn"


def test_bare_identifier_inside_string_value_not_flagged():
    """字符串值内部的 "key": value 不是裸标识符（内容里的普通文本）。"""
    v = StreamingToolValidator(name="file.write", known_tools={"file.write"})
    args = '{"path": "a.md", "content": "he said \\"hi\\": ok"}'
    for i in range(0, len(args), 5):
        v.feed(args[i:i + 5])
    assert v.finalize().level == "ok"


def test_unknown_tool_name_is_fatal():
    v = StreamingToolValidator(name="file.evil", known_tools={"file.write"})
    res = v.feed('{"a": 1}')
    assert res.is_fatal
    assert "不在可用工具列表" in res.reason


def test_pseudo_protocol_is_fatal():
    v = StreamingToolValidator(name="file.write", known_tools={"file.write"})
    res = v.feed('{"content": "<|channel|>commentary: hi"}')
    assert res.is_fatal
    assert "伪协议" in res.reason


def test_fix_bare_identifiers_roundtrip():
    """修复函数能把裸标识符补回引号，且不破坏正常 JSON。"""
    fixed = fix_bare_identifiers('{"to_agent": 总经理, "x": 123}')
    assert json.loads(fixed) == {"to_agent": "总经理", "x": 123}
    # 已经是合法 JSON 的不应被改动语义
    ok = '{"a": "b", "c": 1, "d": true}'
    assert json.loads(fix_bare_identifiers(ok)) == json.loads(ok)


def test_fix_bare_identifiers_is_linear():
    """修复函数不能有正则回溯（长内容曾要 36 秒）。"""
    body = 'x' * 200000
    raw = '{"content": "' + body + '", "k": v}'
    t0 = time.perf_counter()
    fix_bare_identifiers(raw)
    assert time.perf_counter() - t0 < 1.0


# ════════════════════════════════════════════════════════════════════
# 3. tool_call 参数回灌裁剪
# ════════════════════════════════════════════════════════════════════

def test_tool_call_args_trimmed_for_llm_only():
    """发给 LLM 的副本被裁剪；落库/前端那份必须保持完整。"""
    big = "长内容" * 30000
    args = json.dumps({"path": "report.md", "content": big}, ensure_ascii=False)
    stored = [{"id": "c1", "type": "function",
               "function": {"name": "file.write", "arguments": args}}]
    msg = {"role": "assistant", "content": "", "tool_calls": stored}

    entry = _history_llm_entry(msg, "serial", {})

    sent = entry["tool_calls"][0]["function"]["arguments"]
    assert len(sent) < len(args) / 10, "发给 LLM 的副本没有裁剪"
    parsed = json.loads(sent)                       # 裁剪后必须是合法 JSON
    assert parsed["path"] == "report.md"            # 短值保留
    assert parsed["content"].startswith("长内容")    # 长值保留前缀
    # 落库那份完全没动
    assert stored[0]["function"]["arguments"] == args
    assert len(msg["tool_calls"][0]["function"]["arguments"]) == len(args)


def test_short_tool_call_args_untouched():
    """正常长度的参数不应被裁剪。"""
    args = json.dumps({"path": "a.md", "content": "短内容"}, ensure_ascii=False)
    tcs = [{"id": "c1", "type": "function",
             "function": {"name": "file.write", "arguments": args}}]
    out = _trim_tool_calls_for_llm(tcs)
    assert out[0]["function"]["arguments"] == args


def test_tool_call_id_preserved_for_pairing():
    """裁剪不得破坏 tool_call / tool 结果的配对（上游按 id 校验）。"""
    args = json.dumps({"content": "x" * 50000}, ensure_ascii=False)
    tcs = [{"id": "call_abc", "type": "function",
             "function": {"name": "file.write", "arguments": args}}]
    out = _trim_tool_calls_for_llm(tcs)
    assert out[0]["id"] == "call_abc"
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "file.write"


def test_agent_runtime_uses_linear_bare_identifier_fix():
    """agent_runtime 的本地修复函数必须走线性实现，不能带正则回溯副本。

    这条路径在写长文件时必然触发（JSON 解析失败 → 回退修复参数），
    旧正则版在 12KB 参数上就要 323ms，足以卡死事件循环。
    """
    import inspect
    from core import agent_runtime as ar

    src = inspect.getsource(ar._execute_tool)
    assert "pattern = r'''" not in src, "agent_runtime 里仍有旧正则版裸标识符修复"
    assert "re.sub" not in src, "agent_runtime 里仍有正则替换（可能灾难性回溯）"

    # 行为等价性：修复结果与公共线性实现一致
    from core.tool_stream_validator import fix_bare_identifiers as linear_fix
    raw = '{"to_agent": 总经理, "x": 123}'
    assert json.loads(linear_fix(raw)) == {"to_agent": "总经理", "x": 123}


def test_long_content_argument_parsed_quickly():
    """超长参数走「解析失败 → 裸标识符修复」回退路径时，不能卡住。"""
    from core.tool_stream_validator import fix_bare_identifiers

    # 合法 JSON 里塞 200KB 正文（真实写长文件形态：引号/换行/反斜杠齐全）
    unit = '正文含引号 "k": v、换行\n与反斜杠\\ 。'
    args = json.dumps({"path": "r.md", "content": unit * 4000}, ensure_ascii=False)
    assert len(args) > 100000

    t0 = time.perf_counter()
    fix_bare_identifiers(args)
    assert time.perf_counter() - t0 < 0.5, "长参数修复耗时过高（正则回溯回归）"


def test_invalid_arguments_json_left_alone():
    """参数不是合法 JSON 时原样返回（宁可不裁剪，也不给上游传非法 JSON）。"""
    raw = "{not json at all"
    out = _trim_tool_calls_for_llm([
        {"id": "c1", "function": {"name": "x", "arguments": raw}}
    ])
    assert out[0]["function"]["arguments"] == raw


def test_history_entry_keeps_tool_call_id():
    """tool 消息的 tool_call_id 必须原样保留。"""
    msg = {"role": "tool", "content": "ok", "tool_call_id": "call_abc"}
    entry = _history_llm_entry(msg, "serial", {})
    assert entry["tool_call_id"] == "call_abc"
