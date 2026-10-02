"""意图分类器 — 快/慢通道动态路由

在进入多轮迭代循环之前，先用轻量 LLM 调用（或规则）判断任务复杂度：
  - simple  → 快通道：单次推理，直接返回，不走工具循环
  - complex → 慢通道：进入多轮 Tool Call Loop + 独立验证器

设计原则：
  1. 默认 complex（宁多勿少），分类错误导致 simple→complex 只是多花一点 token；
     但 complex→simple 会让需要工具的任务卡住，代价大。
  2. 分类调用必须极其轻量 —— 只看用户当前输入 + 工具清单，不翻完整历史。
  3. 规则优先（关键词匹配、启发式），LLM 分类兜底，减少不必要的 API 开销。
"""
import json
import re
from typing import Any

# —— 关键词规则（命中则直接判定 complex，跳过 LLM 分类）——
# 文件操作
_FILE_KEYWORDS = [
    "文件", "读取", "写入", "创建文件", "编辑文件", "保存文件", "删除文件",
    "file", "read", "write", "delete", "打开", "查看代码", "修改代码",
    "代码", "脚本", "目录", "文件夹", "结构", "路径",
    "新建", "创建", "生成.*文件", "写一个.*脚本", "帮我写.*代码",
]
# Shell / 命令执行
_CMD_KEYWORDS = [
    "运行", "执行", "命令", "终端", "shell", "cmd", "bash", "pip", "npm",
    "安装", "启动服务", "跑一下", "测试一下", "编译", "构建", "部署",
]
# 集群协作 / A2A
_CLUSTER_KEYWORDS = [
    "画布", "编排", "集群", "连线", "节点", "接力", "移交",
    "其他智能体", "问一下", "协商", "讨论", "协作", "call", "handoff",
]
# 多步骤 / 复杂规划
_MULTI_STEP_KEYWORDS = [
    "先.*然后", "首先.*最后", "第一步.*第二步", "分.*步", "流程",
    "帮我.*并且.*", "同时", "并行", "系列", "一套", "完整的",
    "大纲", "结构", "框架", "架构", "方案", "计划", "规划",
    "分析.*数据", "处理.*数据", "整理.*文档", "总结.*内容",
    "调研", "研究", "比较", "评估",
]

# —— fast-path 规则（命中则直接判定 simple）——
# 纯知识问答 / 寒暄 / 简单解释
_SIMPLE_PATTERNS = [
    re.compile(r"^(你好|您好|hi|hello|嗨|在吗|在嘛|早上好|晚上好|谢谢|thanks|thx)[!！\.。~]*$", re.I),
    re.compile(r"^(是什么|什么是|如何|怎么|为什么|怎么回事|有什么用|.*的定义|.*是什么意思)[？?\s]*$"),
    re.compile(r"^解释一?下|介绍一?下|告诉我|说说看|简单说"),
    re.compile(r"^\d+[\+\-\*/]\d+[\+\-\*/\d\s]*[=]?$"),  # 纯算术
    re.compile(r"^true|false|yes|no|对|错|是|不是$", re.I),
]


def _rule_based_classify(user_input: str, tools: list | None) -> str | None:
    """纯规则分类，返回 'simple' / 'complex' / None(无法确定)。
    规则命中时零 LLM 开销。"""
    text = (user_input or "").strip()
    if not text:
        return "simple"

    # 1. fast-path：极短的寒暄 / 纯问答句式
    for pat in _SIMPLE_PATTERNS:
        if pat.match(text):
            return "simple"

    # 2. 如果没有任何工具可用 → 一定走 simple（没有东西可调用）
    if not tools:
        return "simple"

    # 3. 慢通道关键词匹配
    lower = text.lower()
    for kw_list in (_FILE_KEYWORDS, _CMD_KEYWORDS, _CLUSTER_KEYWORDS, _MULTI_STEP_KEYWORDS):
        for kw in kw_list:
            # 支持正则关键字（如 "生成.*文件"）
            if any(c in kw for c in ".*?+"):
                if re.search(kw, text, re.I):
                    return "complex"
            else:
                if kw.lower() in lower:
                    return "complex"

    # 4. 包含工具名的直接 complex
    tool_names = [t["function"]["name"] for t in (tools or [])]
    for tn in tool_names:
        # 把 "file.read" → 检查 "file" 和 "read" 是否都出现，或完整匹配
        parts = tn.split(".")
        if tn.lower() in lower:
            return "complex"
        if len(parts) >= 2 and all(p.lower() in lower for p in parts):
            return "complex"

    # 规则无法确定 → 交给 LLM
    return None


# —— LLM 分类 prompt（极短，低 token 开销）——
_CLASSIFY_SYSTEM = (
    "你是一个任务分类器。根据用户输入判断任务类型：\n"
    "- simple：纯知识问答、闲聊、简单解释、单轮即可回答，不需要执行操作或调用工具。\n"
    "- complex：需要多步骤、需要工具操作（读写文件、运行命令、调用其他智能体）、需要规划或分析。\n"
    "只输出 JSON，格式：{\"route\": \"simple\" 或 \"complex\", \"confidence\": 0-1 浮点数}"
)


async def _llm_classify(interface: dict, model: str, user_input: str,
                       tools: list | None, cluster_mode: bool = False) -> dict:
    """LLM 分类 —— 非流式，极低 token 开销（约 100-200 tokens）。"""
    tool_names = [t["function"]["name"] for t in (tools or [])]
    tool_hint = f"可用工具: {', '.join(tool_names)}" if tool_names else "当前没有可用工具"
    if cluster_mode:
        tool_hint += "；这是集群对话模式（可能需要与其他智能体协作）"

    messages = [
        {"role": "system", "content": _CLASSIFY_SYSTEM},
        {"role": "user", "content": f"{tool_hint}\n\n用户输入：{user_input}"},
    ]

    # 非流式调用 —— 走 chat() 的非流式分支
    from core.llm import chat_with_failover as chat
    async for kind, payload in chat(interface, model, messages, stream=False):
        if kind == "message":
            content = (payload.get("content") or "").strip()
            # 尝试解析 JSON
            try:
                # 可能有 markdown code fence
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
                parsed = json.loads(content)
                route = parsed.get("route", "complex")
                confidence = float(parsed.get("confidence", 0.5))
                return {"route": route, "confidence": confidence, "method": "llm"}
            except Exception:
                pass
            # 解析失败，退而求其次看关键词
            lower = content.lower()
            if "simple" in lower and "complex" not in lower:
                return {"route": "simple", "confidence": 0.6, "method": "llm_fallback"}
            return {"route": "complex", "confidence": 0.5, "method": "llm_fallback"}

    return {"route": "complex", "confidence": 0.3, "method": "timeout_default"}


async def classify_intent(interface: dict, model: str, user_input: str,
                         tools: list | None, cluster_mode: bool = False) -> dict:
    """主入口：规则优先 → LLM 兜底 → 默认 complex（安全优先）。

    返回 dict：
      {
        "route": "simple" | "complex",
        "confidence": float,        # 0-1，越高越确定
        "method": "rule" | "llm" | "rule_fastpath" | "default",
        "reason": str,               # 简短说明
      }
    """
    # Phase 1：规则分类（零开销）
    rule_result = _rule_based_classify(user_input, tools)
    if rule_result is not None:
        if rule_result == "simple":
            return {"route": "simple", "confidence": 0.9, "method": "rule_fastpath",
                    "reason": "规则快速匹配：纯问答/寒暄句式"}
        else:
            return {"route": "complex", "confidence": 0.85, "method": "rule",
                    "reason": "规则命中：需要工具或多步骤"}

    # Phase 2：LLM 分类（兜底）
    try:
        result = await _llm_classify(interface, model, user_input, tools, cluster_mode)
        # 低置信度 → 默认 complex
        if result["route"] == "simple" and result["confidence"] < 0.6:
            result["route"] = "complex"
            result["confidence"] = 0.5
            result["reason"] = "LLM 置信度不足，默认 complex（安全优先）"
        return result
    except Exception as e:
        # LLM 调用失败 → 默认 complex
        return {"route": "complex", "confidence": 0.2, "method": "default",
                "reason": f"LLM 分类失败({e})，默认 complex 保安全"}
