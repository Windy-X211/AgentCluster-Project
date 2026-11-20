"""智能体正文自带「[名字]」前缀的回归测试。

背景：集群（linked）模式回灌历史时，assistant 消息统一被标注成 `[发言者名] 正文`。
模型把自己带前缀的历史当成输出格式，正文开头自带 `[悠悠]`；落库后又被回灌，越叠越多
（记录里出现过 `[通用] [通用] …`）。未绑画布的独立会话 1257 条消息里 0 例，可作对照。

这里锁住三条防线：
  1) 历史标注只加给“别人的发言”，自己的历史不加前缀；
  2) 落库/定稿前剥掉开头的自名标签（含叠层）；
  3) 流式输出开头实时剥离（含 chunk 被切断的情况）。
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.agent_runtime import (  # noqa: E402
    _NamePrefixScanner,
    _history_llm_entry,
    strip_self_name_prefix,
)


def _stream(name, chunks):
    sc = _NamePrefixScanner(name)
    return "".join(sc.feed(c) for c in chunks) + sc.close()


# ===== 1) 落库/定稿剥离 =====

def test_strip_self_name_prefix_basic():
    assert strip_self_name_prefix("[悠悠] 你好", "悠悠") == "你好"
    assert strip_self_name_prefix("【悠悠】：正文", "悠悠") == "正文"
    assert strip_self_name_prefix("（悠悠）正文", "悠悠") == "正文"


def test_strip_self_name_prefix_repeated_layers():
    assert strip_self_name_prefix("[通用] [通用] 害羞表情已生效", "通用") == "害羞表情已生效"
    assert strip_self_name_prefix("  [悠悠]  \n[悠悠] 双层", "悠悠") == "双层"


def test_strip_self_name_prefix_keeps_normal_content():
    # 别人的标签 / 正文中间的标签 / 其他名字都不能动
    assert strip_self_name_prefix("[重要] 别删我", "悠悠") == "[重要] 别删我"
    assert strip_self_name_prefix("[阿明] 我来接手", "悠悠") == "[阿明] 我来接手"
    assert strip_self_name_prefix("普通正文 [悠悠] 中间", "悠悠") == "普通正文 [悠悠] 中间"


def test_strip_self_name_prefix_empty_name_is_noop():
    assert strip_self_name_prefix("[悠悠] 你好", "") == "[悠悠] 你好"
    assert strip_self_name_prefix(None, "悠悠") is None


# ===== 2) 历史回灌标注 =====

def _conv_msg(agent_id, content, role="assistant"):
    return {"role": role, "agent_id": agent_id, "content": content, "ts": "2026-01-01T00:00:00"}


def test_history_entry_does_not_prefix_self():
    names = {11: "悠悠", 22: "阿明"}
    entry = _history_llm_entry(_conv_msg(11, "直接给结论"), "linked", names, 11)
    assert entry["content"] == "直接给结论"


def test_history_entry_strips_dirty_self_prefix():
    """存量脏数据：自己的历史里已经带着自名标签 → 回灌时剥干净，绝不变成双层。"""
    names = {11: "悠悠"}
    entry = _history_llm_entry(_conv_msg(11, "[悠悠] 我这边完事了"), "linked", names, 11)
    assert entry["content"] == "我这边完事了"


def test_history_entry_prefixes_other_member():
    names = {11: "悠悠", 22: "阿明"}
    entry = _history_llm_entry(_conv_msg(22, "我来接手"), "linked", names, 11)
    assert entry["content"] == "[阿明] 我来接手"
    # 别人之前的脏前缀先剥掉，再标一层，避免 `[阿明] [阿明]`
    entry = _history_llm_entry(_conv_msg(22, "[阿明] [阿明] 我来了"), "linked", names, 11)
    assert entry["content"] == "[阿明] 我来了"


def test_history_entry_independent_mode_untouched():
    names = {11: "悠悠"}
    entry = _history_llm_entry(_conv_msg(11, "[悠悠] 正文"), "independent", names, 11)
    assert entry["content"] == "[悠悠] 正文"


# ===== 3) 流式实时剥离 =====

def test_scanner_strips_leading_prefix():
    assert _stream("悠悠", ["[悠悠] 你好"]) == "你好"


def test_scanner_handles_split_chunks():
    assert _stream("悠悠", ["[悠", "悠] 你好"]) == "你好"
    assert _stream("悠悠", ["[悠悠]", " 你好"]) == "你好"


def test_scanner_strips_stacked_prefix():
    assert _stream("通用", ["[通用] [通用] 害羞"]) == "害羞"


def test_scanner_passes_normal_text_untouched():
    assert _stream("悠悠", ["[重要] 别删"]) == "[重要] 别删"
    assert _stream("悠悠", ["【提示】正文"]) == "【提示】正文"
    assert _stream("悠悠", ["\n\n## 标题"]) == "\n\n## 标题"
    assert _stream("悠悠", ["普通", "正文"]) == "普通正文"


def test_scanner_flushes_unclosed_prefix():
    # 流结束时标签还没闭合 → 原样吐出，不能吞正文
    assert _stream("悠悠", ["[悠悠"]) == "[悠悠"
