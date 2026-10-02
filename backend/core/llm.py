"""统一的 LLM 调用 - 兼容 OpenAI 格式接口 + function calling"""
import httpx, json, logging
from typing import AsyncGenerator

_logger = logging.getLogger("llm")

_OPENAI_ROLES = {"system", "user", "assistant", "tool"}


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
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                if not isinstance(fn, dict):
                    continue
                args = fn.get("arguments")
                if not isinstance(args, str):
                    # dict/list → 序列化为 JSON 字符串；None → 空对象字符串
                    try:
                        args = json.dumps(args, ensure_ascii=False) if args else "{}"
                    except Exception:
                        args = "{}"
                    fn["arguments"] = args
                fn.setdefault("name", "")
                tc["function"] = fn
                tc.setdefault("id", "")
                tc.setdefault("type", "function")
                cleaned.append(tc)
            if cleaned:
                m["tool_calls"] = cleaned
            else:
                m.pop("tool_calls", None)
        # tool role 消息必须有 tool_call_id
        if role == "tool":
            m.setdefault("tool_call_id", "")


async def chat(interface: dict, model: str, messages: list, stream: bool = False, tools: list | None = None,
               tool_choice: str = "auto") -> AsyncGenerator:
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
    # tools / tool_choice 注入策略（兼容性优先）：
    #   - tool_choice == "none" 时，国产模型（glm/sensenova 等）不接受 "tools + tool_choice:none" 共存，
    #     干脆不传 tools 字段 —— 模型看不到工具就不会发起 tool_calls，等价于强制文字输出，
    #     且对所有 OpenAI 兼容接口都安全。
    #   - 其他 tool_choice 值（auto/required/指定函数）才同时传 tools + tool_choice。
    if tools and tool_choice != "none":
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    elif tool_choice != "none" and tool_choice != "auto":
        # 没传 tools 但指定了非默认 tool_choice（如 required）→ 仍传 tool_choice
        payload["tool_choice"] = tool_choice

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
                    known_tool_names.add(nm)
                    tool_schemas[nm] = fn.get("parameters") or {}

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
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "): continue
                    data = line[6:]
                    if data == "[DONE]": break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    delta = chunk["choices"][0].get("delta", {})

                    # 深度思考模型的 reasoning 通道（不影响普通模型）
                    if delta.get("reasoning_content"):
                        yield ("reasoning", delta["reasoning_content"])

                    # 普通 content 原样透传（工具调用只可能出现在 tool_calls 通道；
                    # 正文里的伪协议串可能是文档内容本身，不做致命拦截，避免误伤）
                    if delta.get("content"):
                        ctext = delta["content"]
                        yield ("text", ctext)

                    # tool_calls 增量
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        entry = tc_buf.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tc.get("id"): entry["id"] = tc["id"]
                        fn = tc.get("function", {})

                        # —— 名称首次确定 ——
                        if fn.get("name"):
                            entry["name"] = fn["name"]
                            # 名称首次确定 → 立即发出 tool_call_start，让前端流式显示"正在调用: xxx"
                            if idx not in tc_announced:
                                tc_announced.add(idx)
                                yield ("tool_call_start", {"id": entry["id"], "name": entry["name"]})

                            # —— 实时校验工具名是否在白名单内 ——
                            # 仅当本轮确实注入了工具（known_tool_names 非空）才做白名单校验：
                            # 快通道（FAST_PATH_NO_TOOLS=True）不注入工具时 known_tool_names 为空，
                            # 此时模型若仍偷偷发出 tool_call，应正常累积以便上层"降级到慢通道"，
                            # 而不是被当成非法调用拦截（否则破坏既有降级机制）。
                            if known_tool_names and entry["name"] not in known_tool_names:
                                # 未知工具名 → fatal：立即广播 tool_call_invalid 并中止该 index 累积
                                reason = (
                                    f"工具名 {entry['name']!r} 不在可用工具列表内；"
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

                            # 创建该 index 的校验器（名称确定时一次性建好）
                            if idx not in tc_validators:
                                tc_validators[idx] = StreamingToolValidator(
                                    name=entry["name"],
                                    schema=tool_schemas.get(entry["name"], {}),
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
                                validator = StreamingToolValidator(
                                    name=entry.get("name") or "",
                                    schema=tool_schemas.get(entry.get("name") or "", {}),
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

                    # finish_reason = tool_calls 时，把完整 tool_calls 吐出去
                    finish = chunk["choices"][0].get("finish_reason")
                    if finish == "tool_calls":
                        # 按 index 排序
                        for idx in sorted(tc_buf.keys()):
                            entry = tc_buf[idx]
                            # 致命错误已经广播过 → 不再 emit tool_call，跳过
                            if idx in tc_invalid:
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
        else:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            yield ("message", data["choices"][0]["message"])


# —— 自动故障转移：连接断开时切换到下一个模型 ——
# 可触发切换的 HTTP 状态码（0 = 由网络异常推导，非真实 HTTP 状态）
_RETRIABLE_STATUS = {0, 408, 409, 429, 500, 502, 503, 504}
# 可触发切换的网络/传输层异常（httpx.TransportError 覆盖 connect/read/write/timeout/protocol 等）


async def chat_with_failover(interface: dict, model: str, messages: list, stream: bool = False,
                             tools: list | None = None, tool_choice: str = "auto") -> AsyncGenerator:
    """带自动故障转移的 chat 包装器。

    机制：
      - 候选模型 = [当前模型] + [interface.models[] 中其余模型]（去重、保持原始顺序）
      - 只有 1 个候选模型 → 同一模型重试 1 次（共 2 次尝试）
      - 有多个候选模型 → 依次尝试；连接断开 / 超时 / 5xx / 429 时切换到下一个
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

    for attempt_idx, try_model in enumerate(attempts):
        is_last = (attempt_idx == len(attempts) - 1)
        started = False   # 是否已向调用方产出过内容
        switched = False  # 是否因可重试错误切到下一候选
        try:
            async for kind, payload in chat(interface, try_model, messages, stream, tools, tool_choice):
                if kind == "error":
                    status = int(payload.get("status") or 0)
                    last_error = payload
                    retriable = (status in _RETRIABLE_STATUS) or status >= 500
                    # 未产出内容 + 可重试 + 还有候选 → 切换模型
                    if retriable and not started and not is_last:
                        _logger.warning("模型 %s 调用失败 (HTTP %s)，切换到下一候选模型", try_model, status)
                        switched = True
                        break
                    # 不可重试 / 已产出内容 / 最后一次 → 透传错误
                    yield (kind, payload)
                    return
                started = True
                yield (kind, payload)
            if not switched:
                return  # 正常完成
        except httpx.HTTPStatusError as e:
            # 非流式路径：raise_for_status() 抛出的 HTTP 错误
            status = e.response.status_code if e.response is not None else 0
            last_error = {"status": status, "detail": str(e)[:300]}
            retriable = (status in _RETRIABLE_STATUS) or status >= 500
            if retriable and not started and not is_last:
                _logger.warning("模型 %s HTTP %s，切换到下一候选模型", try_model, status)
                continue
            raise
        except httpx.TransportError as e:
            # 连接断开 / 超时 / 协议错误等传输层异常
            last_error = {"status": 0, "detail": f"{type(e).__name__}: {e}"}
            if not started and not is_last:
                _logger.warning("模型 %s 连接异常 (%s)，切换到下一候选模型", try_model, type(e).__name__)
                continue
            raise
        # 其他异常（配置错误等）不在此捕获，直接向上抛，避免掩盖问题

    # 所有候选都失败
    _logger.error("全部候选模型均失败（尝试 %d 次）：%s", len(attempts), last_error)
    yield ("error", last_error)
