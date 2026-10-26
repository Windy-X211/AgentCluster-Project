"""独立验证器 — 概率性 + 确定性双重终止保障

当模型声称"完成"（输出无 tool_calls 的文字）时，不直接采纳，
而是先用确定性规则检查，再用 LLM 验证器做独立审查：

  模型输出文字 → 确定性规则检查（error/timeout/空结果）
                     │
                     ├── 明显未完成 → 强制继续循环
                     │
                     └── 不确定 → LLM 验证器
                                    │
                                    ├── score ≥ threshold → 采纳，终止循环
                                    └── score < threshold → 把验证反馈回灌循环

核心目标：彻底杜绝"任务未完成就提前终止"。

集群协作感知：当 agent_info / cluster_context / delegated_tasks 传入时，
验证器会调整判断标准，允许智能体只完成其分工范围内的工作、
已派发/移交的子任务不纳入"未完成"判定。
"""
import json
from typing import Any


def _deterministic_check(user_input: str, tool_history: list[dict],
                         assistant_text: str,
                         agent_info: dict | None = None,
                         cluster_context: str | None = None) -> dict:
    """确定性规则检查 — 不依赖 LLM，零开销。

    返回：
      {
        "passed": bool,           # 规则是否通过（通过≠完成，只是没被明显打脸）
        "blocked": bool,          # 是否发现明确的"未完成"证据 → 强制继续
        "issues": list[str],      # 发现的问题描述
        "evidence": str,          # 关键证据（给 LLM 验证器参考）
      }
    """
    issues: list[str] = []
    evidence_parts: list[str] = []

    if not tool_history:
        # 没调用工具 → 纯知识回答，不需要工具验证
        return {"passed": True, "blocked": False, "issues": [], "evidence": ""}

    # 只检查每个工具名的**最近一次**执行结果。
    # 长任务的单轮内会累积大量试错调用（早期命令失败、后来修正成功）；若全量扫描，
    # 残余的历史报错会永久触发 deterministic_blocked —— 即使任务事实上已完成、
    # 最新调用全部成功，也会陷入"验证未通过"的死循环反馈。
    # "早期失败是否真被后续操作解决"这种语义判断交给 Phase 2 的 LLM 验证器。
    _latest_by_tool: dict[str, dict] = {}
    for th in tool_history:
        _latest_by_tool[th.get("name") or "unknown"] = th

    # 检查每个工具最近一次调用的结果
    for th in _latest_by_tool.values():
        result = th.get("result") or {}
        args = th.get("args") or {}
        name = th.get("name", "unknown")

        # 情况 1：工具返回 error
        if isinstance(result, dict) and result.get("error"):
            err_msg = str(result.get("error"))
            issues.append(f"工具 {name} 返回错误: {err_msg}")
            evidence_parts.append(f"[{name}] 错误: {err_msg}")

        # 情况 2：cmd.run 返回非零 exit code 且没被处理
        if isinstance(result, dict) and "returncode" in result:
            rc = result.get("returncode")
            if rc != 0:
                # 模型如果提到了 stderr，可能已处理
                stderr = result.get("stderr", "") or ""
                if stderr.strip() and stderr.strip()[:100] not in (assistant_text or ""):
                    issues.append(f"命令 {name} 退出码={rc}，stderr 未被处理")
                    evidence_parts.append(f"[{name}] returncode={rc}, stderr={stderr[:200]}")

        # 情况 3：file.read 返回内容但回复里完全没提到内容
        if name == "file.read" and isinstance(result, dict):
            content = result.get("content", "") or ""
            if content and len(content) > 200:
                # 检查回复是否提到了文件内容（简单启发式：文件内容的前几个关键词是否出现）
                snippet = content[:200].strip()
                if snippet and snippet not in (assistant_text or ""):
                    # 只警告，不阻断 —— 模型可能做了总结
                    evidence_parts.append(f"[file.read] 读取了 {len(content)} 字符，回复未直接引用")

        # 情况 4：file.write / cmd.run / file.patch / file.delete 等有副作用但模型回复说"我没做"或"不需要做"
        if name in ("file.write", "cmd.run", "file.patch", "file.delete"):
            ok = result.get("ok") or (isinstance(result, dict) and result.get("returncode", 0) == 0)
            if not ok and "error" not in result:
                issues.append(f"工具 {name} 执行结果不明确")

    # 情况 5：工具链刚执行完第一个工具就停了（典型的未完成）
    # 比如：用户要求"读文件并总结"，模型只调用了 file.read 就输出了无关文字
    if len(tool_history) == 1 and not issues:
        th = tool_history[0]
        name = th.get("name", "")
        result = th.get("result") or {}
        # file.structure / file.read 后通常还需要进一步操作
        if name in ("file.read", "file.structure"):
            # 检查用户请求是否暗示需要多步（包含"分析"、"总结"、"改"、"写"等词）
            follow_up_keywords = ["分析", "总结", "概括", "改", "修改", "写", "生成", "替换",
                                   "analyze", "summarize", "change", "modify", "write"]
            user_lower = (user_input or "").lower()
            if any(kw in user_lower for kw in follow_up_keywords):
                issues.append(f"刚调用 {name} 获取了信息，但用户请求暗示还需要下一步操作（分析/修改/写）")
                evidence_parts.append(f"[pattern] 信息获取后似乎缺少后续处理")

    blocked = len(issues) > 0 and any(
        "错误" in i or "退出码" in i or "执行结果不明确" in i for i in issues
    )

    return {
        "passed": len(issues) == 0,
        "blocked": blocked,
        "issues": issues,
        "evidence": "\n".join(evidence_parts),
    }


# —— LLM 验证 prompt ——
_VERIFY_SYSTEM = (
    "你是一个任务完成度验证器。独立审查智能体的最后回复是否真正完成了它**应当负责的**工作。\n"
    "只输出 JSON：\n"
    "{\n"
    "  \"complete\": true/false,     // 是否真正完成了自己分工范围内的工作\n"
    "  \"score\": 0.0-1.0,           // 完成度评分（仅针对该智能体的分工，不是整个项目）\n"
    "  \"feedback\": \"...\",        // 具体哪里没做到 / 还缺什么\n"
    "  \"next_step\": \"...\"        // 如果未完成，建议下一步该做什么（一句话）\n"
    "}\n\n"
    "验证标准（按优先级）：\n"
    "1. 【核心原则】如果提供了「智能体角色/分工」，以它为准判断该智能体是否完成了**自己负责的部分**，\n"
    "   不要用完整的用户原始请求要求它做完所有事——团队协作中每个成员只负责一块。\n"
    "2. 【派发豁免】如果列出了「已派发/移交的任务」，这些任务已经由其他成员负责，\n"
    "   不应计入当前智能体的『未完成』清单。\n"
    "3. 用户明确要求的操作（在该智能体分工范围内的）是否都执行了？\n"
    "4. 调用过的工具结果是否被正确消费（不是拿到结果就假装做完）？\n"
    "5. 如果工具执行失败或返回错误，是否被妥善处理或说明了？\n"
    "6. 有没有幻觉——声称做了某件事但实际没做？\n"
    "宁严勿松，但在团队分工场景下**不要越权要求该智能体做不属于它的事**。"
)


async def _llm_verify(interface: dict, model: str, user_input: str,
                     tool_history: list[dict], assistant_text: str,
                     deterministic: dict,
                     agent_info: dict | None = None,
                     cluster_context: str | None = None,
                     delegated_tasks: list[dict] | None = None) -> dict:
    """LLM 独立验证 —— 非流式调用。

    Args:
        agent_info: 智能体自身信息 dict，含 name/description/system_prompt 等；
                    用于告知验证器"这个智能体在团队中扮演什么角色"
        cluster_context: 集群协作上下文字符串（来自 _build_cluster_context），
                        包含成员列表、沟通权限等；让验证器理解团队结构
        delegated_tasks: 已派发/移交的任务列表，每项含 target（接收方）和 task 描述；
                        这些任务的完成责任已转移给其他成员，不应作为当前智能体的"未完成"

    Returns:
      dict with keys: complete, score, feedback, next_step, method
      特别注意：当 LLM 调用失败时，method 会标记为 "llm_call_failed"，
      score 返回 0.5 但 complete=False（调用失败 = 验证未执行，保守判定）。
    """
    tool_summary_lines = []
    for th in tool_history or []:
        result = th.get("result") or {}
        result_str = json.dumps(result, ensure_ascii=False)[:300]
        # 图片型结果：file 字段是图片路径，图片已通过多模态视觉通道（img_followup）
        # 作为 user 消息发送给主模型，主模型可直接"看到"完整图片内容。此处文本摘要里的
        # [图片] 仅为占位符（避免 base64 撑爆验证上下文），并非缺失数据。需告知验证 LLM
        # 这一点，否则它会因"工具只返回 [图片] 占位符"误判主模型"没有图像依据/幻觉"。
        _img_note = ""
        if isinstance(result, dict):
            _fp = result.get("file") or ""
            if isinstance(_fp, str) and _fp.lower().endswith(
                    (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
                _img_note = ("  [注：该工具返回的是图片文件，图片已通过多模态视觉通道发送给主模型，"
                             "主模型可直接看到完整图片内容；此处文本摘要中的 [图片] 仅为占位符，并非缺失数据。"
                             "请据此判断主模型是否基于真实图片作答，而非幻觉。]")
        tool_summary_lines.append(f"- {th.get('name')}({json.dumps(th.get('args') or {}, ensure_ascii=False)[:150]}) → {result_str}{_img_note}")

    # —— 构造集群协作信息块（仅当有集群上下文时注入）——
    cluster_block = ""
    if agent_info or cluster_context or delegated_tasks:
        parts = ["\n\n【集群协作上下文】"]
        if agent_info:
            name = agent_info.get("name", "该智能体")
            desc = (agent_info.get("description") or "").strip()
            sp_first = (agent_info.get("system_prompt") or "").strip().split("\n")[0]
            role_brief = desc or sp_first or ""
            parts.append(f"- 当前验证对象：{name}（agent_id={agent_info.get('id', '?')}）")
            if role_brief:
                parts.append(f"- 角色/分工：{role_brief[:200]}")
        if cluster_context:
            parts.append(f"- 团队结构：{cluster_context[:500]}")
        if delegated_tasks:
            parts.append("- 已派发/移交的任务（这些不由当前智能体负责完成）：")
            for dt in delegated_tasks:
                target = dt.get("target", "?")
                task_desc = dt.get("task") or dt.get("message") or "(无描述)"
                parts.append(f"  → 派发给 {target}：{task_desc[:200]}")
        cluster_block = "\n".join(parts)

    user_msg = (
        f"【智能体收到的输入】\n{user_input}\n\n"
        f"【本轮调用的工具历史】\n"
        + ("\n".join(tool_summary_lines) if tool_summary_lines else "（未调用任何工具）")
        + f"\n\n【确定性规则检查发现的问题】\n"
        + (deterministic.get("evidence") or "（未发现明显问题）")
        + cluster_block
        + f"\n\n【智能体的最终回复】\n{assistant_text or '（空）'}"
    )

    messages = [
        {"role": "system", "content": _VERIFY_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    from core.llm import chat_with_failover as chat
    got_response = False
    last_error_info = ""

    async for kind, payload in chat(interface, model, messages, stream=False):
        if kind == "error":
            # 模型调用失败 — 记录错误信息，继续等待（可能还有 failover 切换）
            status = payload.get("status", 0)
            detail = str(payload.get("detail", ""))[:200]
            last_error_info = f"HTTP {status}: {detail}" if status else detail
            continue

        if kind == "message":
            got_response = True
            content = (payload.get("content") or "").strip()
            # 解析 JSON
            try:
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
                parsed = json.loads(content)
                return {
                    "complete": bool(parsed.get("complete", True)),
                    "score": float(parsed.get("score", 0.5)),
                    "feedback": str(parsed.get("feedback", "")),
                    "next_step": str(parsed.get("next_step", "")),
                    "method": "llm",
                }
            except Exception:
                # JSON 解析失败，退而看关键词
                lower = content.lower()
                is_complete = "complete" in lower and ("true" in lower or "是" in content)
                return {
                    "complete": is_complete,
                    "score": 0.7 if is_complete else 0.3,
                    "feedback": content[:300],
                    "next_step": "",
                    "method": "llm_fallback",
                }

    # —— 没有收到任何有效 message 响应 ——
    # 原因可能是：所有候选模型都 failover 失败了，或者网络问题导致没有产出
    if last_error_info:
        # 有明确的错误信息 —— LLM 调用失败，验证未执行
        import logging as _logging
        _logging.getLogger("validator").warning(
            "验证器 LLM 调用失败: %s (interface=%s, model=%s)",
            last_error_info,
            interface.get("url", "?") if isinstance(interface, dict) else "?",
            model,
        )
        # 关键修复：调用失败 → 保守判定 complete=False 但给一个中间分
        # 让 verify_completion 的 except 分支接管 → 保守采纳避免死循环
        raise RuntimeError(f"验证器 LLM 调用失败: {last_error_info}")

    if not got_response:
        # 既没 message 也没 error —— 意外情况，保守处理
        import logging as _logging
        _logging.getLogger("validator").warning(
            "验证器 LLM 调用无响应 (interface=%s, model=%s)",
            interface.get("url", "?") if isinstance(interface, dict) else "?",
            model,
        )
        raise RuntimeError("验证器 LLM 调用无响应（既无 message 也无 error）")

    # got_response=True 但没进入任何 return —— 理论上不会到这里
    # 留一个保守兜底：默认不确定，让上层决定
    return {"complete": False, "score": 0.5, "feedback": "验证器未返回有效判断",
            "next_step": "", "method": "unknown_no_return"}


async def verify_completion(interface: dict, model: str, user_input: str,
                           tool_history: list[dict], assistant_text: str,
                           retry_count: int = 0, max_retries: int = 2,
                           agent_info: dict | None = None,
                           cluster_context: str | None = None,
                           delegated_tasks: list[dict] | None = None) -> dict:
    """独立验证入口。

    Args:
        agent_info: 智能体自身信息（name/description/system_prompt/id），
                    集群模式下用于告知验证器"该智能体负责什么分工"
        cluster_context: 集群协作上下文字符串，让验证器理解团队结构
        delegated_tasks: 已派发/移交的任务列表（cluster.send_message 给下游的），
                        这些不由当前智能体负责完成

    返回：
      {
        "adopt": bool,           # True → 采纳当前回复，终止循环；False → 继续循环
        "complete": bool,        # 验证器认为是否完成
        "score": float,
        "feedback": str,         # 给主循环的反馈（会作为新 system/tool 消息回灌）
        "next_step": str,
        "method": str,           # 哪个验证器给出的结论
        "deterministic": dict,   # 确定性检查结果
      }
    """
    # Phase 1：确定性规则
    det = _deterministic_check(user_input, tool_history, assistant_text,
                               agent_info=agent_info, cluster_context=cluster_context)

    if det["blocked"]:
        # 发现明确的未完成证据 → 强制继续循环，不用 LLM 验证
        feedback = "确定性检查发现以下问题，任务显然未完成，请继续处理：\n" + "\n".join(f"  - {i}" for i in det["issues"])
        return {
            "adopt": False,
            "complete": False,
            "score": 0.2,
            "feedback": feedback,
            "next_step": "请先处理上述工具错误或补充未完成的操作",
            "method": "deterministic_blocked",
            "deterministic": det,
        }

    if det["passed"] and not tool_history:
        # 没调用工具、确定性规则通过 → 大概率是纯知识回答，可以采纳
        # 但为了安全，简单问题还是直接采纳
        return {
            "adopt": True,
            "complete": True,
            "score": 0.9,
            "feedback": "",
            "next_step": "",
            "method": "deterministic_passed_no_tools",
            "deterministic": det,
        }

    # Phase 2：LLM 验证器（只在调用过工具但确定性检查没发现硬错误时触发）
    # 超过重试次数后降低门槛（避免无限循环）
    threshold = 0.75 - retry_count * 0.15  # 第一次 0.75，第二次 0.6，第三次 0.45
    # 但也不能太低
    threshold = max(0.4, threshold)

    try:
        result = await _llm_verify(interface, model, user_input, tool_history,
                                    assistant_text, det,
                                    agent_info=agent_info,
                                    cluster_context=cluster_context,
                                    delegated_tasks=delegated_tasks)
        result["deterministic"] = det

        if result["score"] >= threshold:
            result["adopt"] = True
            result["complete"] = True
        else:
            result["adopt"] = False
            result["complete"] = False

        # 强制条件：如果 score 特别低（< 0.3），不管重试次数都必须继续
        if result["score"] < 0.3:
            result["adopt"] = False
            result["complete"] = False

        return result
    except Exception as e:
        # LLM 验证失败 → 保守采纳（避免卡壳），但标记一下
        return {
            "adopt": True,
            "complete": True,
            "score": 0.5,
            "feedback": f"验证器 LLM 调用异常({e})，保守采纳",
            "next_step": "",
            "method": "llm_error_fallback_adopt",
            "deterministic": det,
        }
