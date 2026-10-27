"""推理等级检测器 —— 从服务商元数据/协议适配自动检测模型支持的推理档位。

策略优先级：
  1. 服务商 /models 端点返回的能力元数据 → 精确档位
  2. 协议适配器（openai/anthropic/gemini）→ 协议默认参数
  3. 模型名启发式匹配 → 已知推理模型的常见档位
  4. 无法确定 → unknown，不伪造档位
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse
import re


@dataclass
class ReasoningOption:
    label: str
    value: Any


@dataclass
class ReasoningResult:
    status: str          # "confirmed" | "unsupported" | "unknown"
    protocol: str
    options: list[ReasoningOption] = field(default_factory=list)
    parameter: Optional[str] = None      # payload 中的参数路径，如 "reasoning_effort"
    source: str = "unknown"              # "model_metadata" | "protocol_adapter" | "name_heuristic" | "unknown"
    message: str = ""

    @property
    def supported(self) -> bool:
        return self.status == "confirmed" and bool(self.options)


LABELS = {
    "none": "关闭",
    "off": "关闭",
    "minimal": "极速",
    "low": "低",
    "medium": "中",
    "high": "高",
    "xhigh": "极高",
    "max": "最大",
    "minimal_effort": "极速",
}

PROTOCOL_PARAMETERS = {
    "openai-responses": "reasoning.effort",
    "openai-chat": "reasoning_effort",
    "anthropic": "thinking",
    "gemini": "thinking_level",
    "deepseek": "thinking",
    "glm": "thinking",
    "qwen": "enable_thinking",
}


def _get_path(data: dict, path: str) -> Any:
    current: Any = data
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_value(data: dict, paths: list[str]) -> Any:
    for path in paths:
        value = _get_path(data, path)
        if value is not None:
            return value
    return None


def _normalize_levels(raw: Any) -> list[ReasoningOption]:
    if not isinstance(raw, (list, tuple)):
        return []
    options: list[ReasoningOption] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            value = item
            label = LABELS.get(item.lower(), item)
        elif isinstance(item, dict) and "value" in item:
            value = item["value"]
            label = str(item.get("label", LABELS.get(str(value).lower(), value)))
        else:
            continue
        if not isinstance(value, (str, int, float, bool)):
            continue
        key = f"{type(value).__name__}:{value}"
        if key in seen:
            continue
        seen.add(key)
        options.append(ReasoningOption(label=str(label), value=value))
    return options


# ===== 协议识别 =====

def detect_protocol(base_url: str, explicit_type: str | None = None) -> str:
    if explicit_type:
        aliases = {
            "responses": "openai-responses",
            "openai-responses": "openai-responses",
            "chat": "openai-chat",
            "chat-completions": "openai-chat",
            "openai-chat": "openai-chat",
            "anthropic": "anthropic",
            "messages": "anthropic",
            "gemini": "gemini",
            "google": "gemini",
            "deepseek": "deepseek",
            "glm": "glm",
            "qwen": "qwen",
        }
        return aliases.get(explicit_type.lower(), "unknown")

    host = (urlparse(base_url).hostname or "").lower()
    if host == "api.openai.com":
        return "openai-responses"
    if host == "api.anthropic.com":
        return "anthropic"
    if host == "generativelanguage.googleapis.com":
        return "gemini"
    if "deepseek" in host:
        return "deepseek"
    if "bigmodel" in host or "zhipuai" in host or "zhipu" in host:
        return "glm"
    if "dashscope" in host or "qwen" in host or "alibaba" in host:
        return "qwen"
    return "openai-chat"


# ===== 模型名启发式匹配（元数据缺失时的兜底）=====

_NAME_PATTERNS: list[tuple[re.Pattern, str, list[ReasoningOption], str]] = [
    # OpenAI o 系列
    (re.compile(r'^o[1-9]', re.I), "reasoning_effort",
     [ReasoningOption("低", "low"), ReasoningOption("中", "medium"), ReasoningOption("高", "high")],
     "openai-chat"),
    # DeepSeek 推理模型
    (re.compile(r'deepseek.*r1|deepseek.*v3\.[1-9]|deepseek.*reason', re.I), "thinking",
     [ReasoningOption("关闭", "off"), ReasoningOption("低", "low"), ReasoningOption("高", "high"), ReasoningOption("最大", "max")],
     "deepseek"),
    # GLM 推理模型
    (re.compile(r'glm.*z1|glm.*4\.[5-9]|glm.*thinking|glm.*reason', re.I), "thinking",
     [ReasoningOption("关闭", "off"), ReasoningOption("低", "low"), ReasoningOption("高", "high"), ReasoningOption("最大", "max")],
     "glm"),
    # Claude 推理模型
    (re.compile(r'claude.*3\.5|claude.*4|claude.*sonnet|claude.*opus', re.I), "thinking",
     [ReasoningOption("关闭", "off"), ReasoningOption("低", "low"), ReasoningOption("高", "high"), ReasoningOption("最大", "max")],
     "anthropic"),
    # Qwen 推理模型
    (re.compile(r'qwq|qwen.*3', re.I), "enable_thinking",
     [ReasoningOption("关闭", "off"), ReasoningOption("低", "low"), ReasoningOption("高", "high"), ReasoningOption("最大", "max")],
     "qwen"),
]


def _heuristic_detect(model_name: str) -> ReasoningResult | None:
    if not model_name:
        return None
    for pat, param, options, protocol in _NAME_PATTERNS:
        if pat.search(model_name):
            return ReasoningResult(
                status="confirmed",
                protocol=protocol,
                options=options,
                parameter=param,
                source="name_heuristic",
                message="档位来自模型名启发式匹配。",
            )
    return None


# ===== 检测器主类 =====

class ReasoningDetector:
    LEVEL_PATHS = [
        "reasoning_efforts",
        "supported_reasoning_efforts",
        "reasoning.levels",
        "reasoning.supported_levels",
        "capabilities.reasoning.levels",
        "thinking_levels",
        "supported_thinking_levels",
        "thinking.levels",
        "capabilities.thinking.levels",
    ]

    PARAMETER_PATHS = [
        "reasoning_parameter",
        "reasoning.parameter",
        "capabilities.reasoning.parameter",
        "thinking_parameter",
    ]

    def detect(self, model: dict, protocol: str, model_name: str = "") -> ReasoningResult:
        parameter = PROTOCOL_PARAMETERS.get(protocol, PROTOCOL_PARAMETERS.get("openai-chat"))

        # 1. 优先查找明确声明的支持档位
        raw_levels = _first_value(model, self.LEVEL_PATHS)
        if raw_levels is not None:
            options = _normalize_levels(raw_levels)
            if options:
                explicit_parameter = _first_value(model, self.PARAMETER_PATHS)
                return ReasoningResult(
                    status="confirmed",
                    protocol=protocol,
                    options=options,
                    parameter=explicit_parameter if isinstance(explicit_parameter, str) else parameter,
                    source="model_metadata",
                    message="档位来自模型能力元数据。",
                )

        # 2. 明确声明不支持
        reasoning_supported = _first_value(model, [
            "capabilities.reasoning.enabled",
            "capabilities.thinking.enabled",
            "reasoning_supported",
            "supports_reasoning",
        ])
        if reasoning_supported is False:
            return ReasoningResult(
                status="unsupported",
                protocol=protocol,
                message="模型明确声明不支持推理控制。",
            )

        # 3. 模型名启发式匹配
        heuristic = _heuristic_detect(model_name)
        if heuristic:
            return heuristic

        # 4. 无法确定
        return ReasoningResult(
            status="unknown",
            protocol=protocol,
            parameter=parameter,
            message="未发现明确的推理档位元数据，暂不显示推理等级菜单。",
        )


# ===== payload 注入：把推理等级写入请求体 =====

_BUDGET_MAP = {"low": 4096, "high": 16384, "max": 32768}


def inject_reasoning_payload(payload: dict, model: str, level: str, protocol: str = "openai-chat",
                              detected_parameter: str | None = None) -> None:
    """根据检测到的协议/参数，向 payload 注入推理等级参数。

    level: 用户选择的等级值（"off"/"low"/"high"/"max" 或服务商原始值）
    detected_parameter: 检测器返回的参数路径，优先使用
    """
    if not level or level == "off" or not model:
        return

    param = detected_parameter or PROTOCOL_PARAMETERS.get(protocol, "reasoning_effort")
    ml = model.lower()

    # OpenAI reasoning_effort
    if param in ("reasoning_effort", "reasoning.effort"):
        effort_map = {"low": "low", "high": "medium", "max": "high", "medium": "medium"}
        ev = effort_map.get(level, level)
        payload["reasoning_effort"] = ev
        return

    # DeepSeek / GLM thinking
    if param == "thinking" and ("deepseek" in ml or "glm" in ml):
        budget = _BUDGET_MAP.get(level, 4096)
        payload["thinking"] = {"type": "enabled", "budget": budget}
        return

    # Claude thinking
    if param == "thinking" and "claude" in ml:
        budget = _BUDGET_MAP.get(level, 4096)
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        return

    # Qwen enable_thinking
    if param == "enable_thinking":
        budget = _BUDGET_MAP.get(level, 4096)
        payload["enable_thinking"] = True
        payload["thinking_budget"] = budget
        return

    # Gemini thinking_level
    if param == "thinking_level":
        payload["thinking_level"] = level
        return

    # 兜底：通用 reasoning_effort
    effort_map = {"low": "low", "high": "medium", "max": "high"}
    ev = effort_map.get(level)
    if ev:
        payload["reasoning_effort"] = ev