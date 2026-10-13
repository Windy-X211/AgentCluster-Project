"""Trae Work 式工作流模式：Plan / Spec / Goal 三种模式的工具、状态机与提示词。

设计要点：
- 智能体 agent.workflow_mode ∈ {"none","plan","spec","goal"}
- 运行时按模式向工具列表追加 plan.*/spec.*/goal.* 工具，向 system_prompt 追加阶段提示词
- 工作流状态独立存于 AppData/workflows/{conv_id}_{agent_id}.json，随对话隔离、随智能体隔离
  （不写入 conversation.messages，避免与运行时节流落库竞争）
- _execute_tool 拦截 plan.*/spec.*/goal.* 名称，派发到本模块 run_workflow_tool，
  携带 conv_id+agent_id 上下文（这是 commands.run_command 拿不到的）

三种模式的状态机：
  plan : PLANNING → AWAIT_CONFIRM → EXECUTING → DONE
  spec : DRAFTING  → LOCKED        → EXECUTING → DONE（checklist 全部通过）
  goal : SETTING   → RUNNING       → DONE（goal_assess 评分 ≥ 阈值）
"""
import json
from pathlib import Path

from core.store import ROOT, _atomic_write_json

_WF_DIR = Path(ROOT) / "workflows"


def _wf_path(conv_id: int, agent_id: int) -> Path:
    return _WF_DIR / f"{conv_id}_{agent_id}.json"


def init_workflow(mode: str) -> dict:
    """按模式初始化一个空白工作流状态。"""
    base = {"mode": mode, "stage": "_INIT", "history": []}
    if mode == "plan":
        base.update({"plan_text": "", "tasks": [], "done_tasks": [], "stage": "PLANNING"})
    elif mode == "spec":
        base.update({"spec_md": "", "tasks_md": "", "checklist": [],
                     "task_status": {}, "checklist_passed": {}, "stage": "DRAFTING"})
    elif mode == "goal":
        base.update({"objective": "", "success_criteria": [], "threshold": 0.8,
                     "last_score": 0.0, "last_gaps": "", "stage": "SETTING"})
    return base


def load_workflow(conv_id: int, agent_id: int, mode: str) -> dict:
    """读取工作流状态；不存在或损坏则初始化。mode 不匹配也重新初始化。"""
    p = _wf_path(conv_id, agent_id)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("mode") == mode:
                return data
        except Exception:
            pass
    return init_workflow(mode)


def save_workflow(conv_id: int, agent_id: int, wf: dict) -> None:
    _WF_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(_wf_path(conv_id, agent_id), wf)


# ============================================================
# 工具 Schema（OpenAI function calling 格式）
# ============================================================
_PLAN_TOOLS = [
    {"type": "function", "function": {
        "name": "plan.create",
        "description": "制定执行计划并生成有序任务清单。调用后计划进入'待确认'状态，等用户确认后才开始执行。必须在动手前先调用此工具。",
        "parameters": {"type": "object", "properties": {
            "plan_text": {"type": "string", "description": "计划正文：目标、思路、步骤概要"},
            "tasks": {"type": "array", "items": {"type": "string"}, "description": "有序任务清单，每项是一个可执行的小步骤"}
        }, "required": ["plan_text", "tasks"]}
    }},
    {"type": "function", "function": {
        "name": "plan.update",
        "description": "更新计划正文或任务清单（在用户提出修改时使用）。",
        "parameters": {"type": "object", "properties": {
            "plan_text": {"type": "string", "description": "新的计划正文（可选，留空则不改）"},
            "add_tasks": {"type": "array", "items": {"type": "string"}, "description": "追加的任务（可选）"},
            "remove_task": {"type": "string", "description": "要删除的任务文本（可选）"}
        }}
    }},
    {"type": "function", "function": {
        "name": "plan.task_done",
        "description": "标记某个任务已完成。所有任务完成后工作流自动进入 DONE。",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "已完成的任务文本（必须与 plan.create 里的 tasks 完全一致）"}
        }, "required": ["task"]}
    }},
    {"type": "function", "function": {
        "name": "plan.get_status",
        "description": "查询当前计划状态：阶段、总任务数、已完成任务数、待办任务。",
        "parameters": {"type": "object", "properties": {}}
    }},
]

_SPEC_TOOLS = [
    {"type": "function", "function": {
        "name": "spec.generate",
        "description": "生成规格三件套：spec.md（规格说明）、tasks.md（任务清单）、checklist（验收清单）。生成后进入起草态，可继续 spec.update 修订。",
        "parameters": {"type": "object", "properties": {
            "spec_md": {"type": "string", "description": "规格说明正文（目标、范围、接口、约束）"},
            "tasks_md": {"type": "string", "description": "任务清单正文（Markdown 列表/表格）"},
            "checklist": {"type": "array", "items": {"type": "string"}, "description": "验收清单：每项是一个可独立判定的验收点"}
        }, "required": ["spec_md", "tasks_md", "checklist"]}
    }},
    {"type": "function", "function": {
        "name": "spec.update",
        "description": "修订规格/任务/验收清单（锁定前可自由改）。",
        "parameters": {"type": "object", "properties": {
            "spec_md": {"type": "string", "description": "新的规格正文（可选）"},
            "tasks_md": {"type": "string", "description": "新的任务清单（可选）"},
            "add_checklist": {"type": "array", "items": {"type": "string"}, "description": "追加的验收点（可选）"}
        }}
    }},
    {"type": "function", "function": {
        "name": "spec.lock",
        "description": "锁定规格，进入执行阶段。锁定后规格正文不再修改，开始按 tasks.md 逐项执行。",
        "parameters": {"type": "object", "properties": {}}
    }},
    {"type": "function", "function": {
        "name": "spec.task_mark",
        "description": "标记 tasks.md 中某项任务的状态。",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "任务文本（tasks.md 中的某一项）"},
            "status": {"type": "string", "enum": ["in_progress", "done", "blocked"], "description": "任务状态"}
        }, "required": ["task", "status"]}
    }},
    {"type": "function", "function": {
        "name": "spec.checklist_check",
        "description": "勾选验收清单某一项是否通过。全部通过后工作流自动进入 DONE。",
        "parameters": {"type": "object", "properties": {
            "item": {"type": "string", "description": "验收项文本（与 spec.generate 的 checklist 完全一致）"},
            "passed": {"type": "boolean", "description": "true=通过，false=未通过/回退"}
        }, "required": ["item", "passed"]}
    }},
    {"type": "function", "function": {
        "name": "spec.get_status",
        "description": "查询当前规格状态：阶段、验收通过项数/总数、任务进度。",
        "parameters": {"type": "object", "properties": {}}
    }},
]

_GOAL_TOOLS = [
    {"type": "function", "function": {
        "name": "goal.create",
        "description": "设定目标、成功标准与达成阈值。调用后工作流进入 RUNNING 态，开始迭代推进。",
        "parameters": {"type": "object", "properties": {
            "objective": {"type": "string", "description": "目标正文：要达成什么、为什么、约束"},
            "success_criteria": {"type": "array", "items": {"type": "string"}, "description": "可判定的成功标准列表"},
            "threshold": {"type": "number", "description": "达成度阈值 0-1，默认 0.8。评估分 ≥ 阈值则目标达成"}
        }, "required": ["objective", "success_criteria"]}
    }},
    {"type": "function", "function": {
        "name": "goal.update",
        "description": "修订目标或成功标准（在评估发现方向偏差时使用）。",
        "parameters": {"type": "object", "properties": {
            "objective": {"type": "string", "description": "新的目标正文（可选）"},
            "success_criteria": {"type": "array", "items": {"type": "string"}, "description": "新的成功标准（可选）"},
            "threshold": {"type": "number", "description": "新的阈值（可选）"}
        }}
    }},
    {"type": "function", "function": {
        "name": "goal.status",
        "description": "查询目标当前状态：阶段、最近评估分、剩余缺口、评估历史。",
        "parameters": {"type": "object", "properties": {}}
    }},
]

_TOOLS_BY_MODE = {
    "plan": _PLAN_TOOLS,
    "spec": _SPEC_TOOLS,
    "goal": _GOAL_TOOLS,
}


def workflow_tools_for(mode: str) -> list:
    """返回指定模式的工具 schema 列表（OpenAI function calling 格式）。"""
    return _TOOLS_BY_MODE.get(mode, [])


# ============================================================
# 提示词前缀：注入到 system_prompt 末尾，告诉模型当前所处工作流阶段
# ============================================================
def workflow_preamble(mode: str, wf: dict) -> str:
    stage = wf.get("stage", "")
    lines = [
        f"\n## 工作流模式（Trae Work 式）",
        f"你当前运行在 `{mode}` 工作流模式，阶段：`{stage}`。",
    ]
    if mode == "plan":
        lines.append(
            "- 阶段流转：PLANNING（制定计划）→ AWAIT_CONFIRM（等用户确认）→ EXECUTING（执行）→ DONE。\n"
            "- 动手前必须先调 `plan.create` 提交计划与任务清单，调完后用一句话请用户确认。\n"
            "- 用户确认后，按任务清单逐项执行；每完成一项调 `plan.task_done` 标记。\n"
            "- 全部任务完成后工作流自动结束；不要在未调 plan.create 前就开干。"
        )
        if stage == "AWAIT_CONFIRM":
            lines.append(f"\n当前计划：\n{wf.get('plan_text','（空）')}\n任务清单：{wf.get('tasks',[])}")
        elif stage == "EXECUTING":
            done = set(wf.get("done_tasks", []))
            todo = [t for t in wf.get("tasks", []) if t not in done]
            lines.append(f"\n待办任务：{todo}")
    elif mode == "spec":
        lines.append(
            "- 阶段流转：DRAFTING（起草三件套）→ LOCKED（锁定规格）→ EXECUTING（按任务执行）→ DONE（验收全过）。\n"
            "- 先调 `spec.generate` 产出 spec/tasks/checklist 三件套；可 `spec.update` 修订。\n"
            "- 调 `spec.lock` 锁定规格后开始执行；逐项调 `spec.task_mark` 推进任务状态。\n"
            "- 每完成一个验收点调 `spec.checklist_check` 勾选；全部通过自动 DONE。"
        )
        if wf.get("checklist"):
            passed = wf.get("checklist_passed", {})
            lines.append(f"\n验收进度：{sum(1 for v in passed.values() if v)}/{len(wf['checklist'])}")
    elif mode == "goal":
        lines.append(
            "- 阶段流转：SETTING（设定目标）→ RUNNING（迭代推进）→ DONE（达成度达标）。\n"
            "- 先调 `goal.create` 设定目标+成功标准+阈值；之后每轮结束时系统会自动评估达成度。\n"
            "- 评估分 ≥ 阈值则目标达成、工作流结束；否则系统会把缺口回灌下一轮让你继续。\n"
            "- 不要在目标未设定（SETTING 阶段）时就开干，先 goal.create。"
        )
        if stage == "RUNNING":
            lines.append(f"\n目标：{wf.get('objective','（空）')}")
            lines.append(f"成功标准：{wf.get('success_criteria',[])}")
            if wf.get("last_score"):
                lines.append(f"最近评估分：{wf.get('last_score')}，缺口：{wf.get('last_gaps','无')}")
    return "\n".join(lines)


# ============================================================
# 阶段门控：在每轮 agent_reply 开头根据 user_input 推动阶段
# ============================================================
_CONFIRM_KEYWORDS = {"确认", "同意", "可以", "好的", "ok", "OK", "Okay", "开始", "执行",
                     "没问题", "继续", "yes", "Yes", "YES", "go", "Go", "GO", "确认执行", "开干"}


def _user_confirms(text: str) -> bool:
    """用户消息是否表示对计划的确认。"""
    if not text:
        return False
    t = text.strip()
    if any(k in t for k in _CONFIRM_KEYWORDS):
        return True
    # 极短回复（≤6 字符）且无否定词 → 视为确认
    if len(t) <= 6 and not any(n in t for n in ["不", "改", "等", "no", "No"]):
        return True
    return False


def stage_gate(mode: str, wf: dict, user_input: str, conv_id: int, agent_id: int) -> str:
    """在 agent_reply 开头调用：根据 user_input 推动阶段机，返回给前端的提示文本。

    目前仅 plan 模式有显式门控（AWAIT_CONFIRM → EXECUTING）。
    spec/goal 的阶段推进由工具调用驱动，无需在此处理。
    """
    if mode != "plan":
        return ""
    stage = wf.get("stage")
    if stage != "AWAIT_CONFIRM":
        return ""
    if _user_confirms(user_input):
        wf["stage"] = "EXECUTING"
        save_workflow(conv_id, agent_id, wf)
        return "✓ 计划已确认，进入执行阶段。\n"
    # 用户没确认 → 视为修改请求，回到 PLANNING 让模型 revise
    wf["stage"] = "PLANNING"
    save_workflow(conv_id, agent_id, wf)
    return "（用户提出修改意见，回到计划阶段，请按反馈修订计划后重新 plan.create）\n"


# ============================================================
# 工具分发：plan.*/spec.*/goal.* → 对应处理函数
# ============================================================
def run_workflow_tool(name: str, args: dict, conv_id: int, agent_id: int) -> dict:
    """工作流工具统一入口。从 store 载入 wf → 执行 → 落盘 → 返回结果 dict。"""
    mode = name.split(".", 1)[0]
    wf = load_workflow(conv_id, agent_id, mode)
    handler = _HANDLERS.get(name)
    if not handler:
        return {"error": f"未知工作流工具：{name}"}
    try:
        result = handler(wf, args or {})
    except Exception as e:
        return {"error": f"{name} 执行异常：{e}"}
    save_workflow(conv_id, agent_id, wf)
    return result


def _plan_create(wf: dict, args: dict) -> dict:
    wf["plan_text"] = args.get("plan_text", "").strip()
    wf["tasks"] = [t for t in (args.get("tasks") or []) if str(t).strip()]
    wf["done_tasks"] = []
    wf["stage"] = "AWAIT_CONFIRM"
    return {"ok": True, "stage": wf["stage"],
            "tasks_count": len(wf["tasks"]),
            "hint": "计划已生成，等待用户确认。请用一句话请用户确认后再执行。"}


def _plan_update(wf: dict, args: dict) -> dict:
    if args.get("plan_text"):
        wf["plan_text"] = args["plan_text"].strip()
    add_t = args.get("add_tasks") or []
    if add_t:
        wf["tasks"] = (wf.get("tasks") or []) + [str(t) for t in add_t if str(t).strip()]
    rm = args.get("remove_task")
    if rm:
        wf["tasks"] = [t for t in (wf.get("tasks") or []) if t != rm]
        wf["done_tasks"] = [t for t in (wf.get("done_tasks") or []) if t != rm]
    return {"ok": True, "stage": wf.get("stage"), "tasks": wf.get("tasks")}


def _plan_task_done(wf: dict, args: dict) -> dict:
    task = args.get("task", "").strip()
    if not task:
        return {"error": "task 参数不能为空"}
    done = set(wf.get("done_tasks") or [])
    if task not in (wf.get("tasks") or []):
        # 容错：任务文本不完全匹配 → 尝试子串匹配
        cand = [t for t in wf.get("tasks", []) if task in t or t in task]
        if cand:
            task = cand[0]
        else:
            return {"error": f"任务 '{task}' 不在任务清单里"}
    done.add(task)
    wf["done_tasks"] = list(done)
    remaining = [t for t in wf.get("tasks", []) if t not in done]
    if not remaining and wf.get("stage") == "EXECUTING":
        wf["stage"] = "DONE"
    return {"ok": True, "stage": wf.get("stage"),
            "done_count": len(done), "total": len(wf.get("tasks") or []),
            "remaining": remaining}


def _plan_get_status(wf: dict, args: dict) -> dict:
    done = set(wf.get("done_tasks") or [])
    return {"stage": wf.get("stage"), "plan_text": wf.get("plan_text", ""),
            "tasks": wf.get("tasks", []),
            "done_tasks": list(done),
            "remaining": [t for t in wf.get("tasks", []) if t not in done]}


def _spec_generate(wf: dict, args: dict) -> dict:
    wf["spec_md"] = args.get("spec_md", "").strip()
    wf["tasks_md"] = args.get("tasks_md", "").strip()
    wf["checklist"] = [c for c in (args.get("checklist") or []) if str(c).strip()]
    wf["task_status"] = {}
    wf["checklist_passed"] = {c: False for c in wf["checklist"]}
    wf["stage"] = "DRAFTING"
    return {"ok": True, "stage": wf["stage"],
            "checklist_count": len(wf["checklist"]),
            "hint": "三件套已生成。可 spec.update 修订，或 spec.lock 锁定后开始执行。"}


def _spec_update(wf: dict, args: dict) -> dict:
    if args.get("spec_md"):
        wf["spec_md"] = args["spec_md"].strip()
    if args.get("tasks_md"):
        wf["tasks_md"] = args["tasks_md"].strip()
    add_c = args.get("add_checklist") or []
    if add_c:
        for c in add_c:
            c = str(c).strip()
            if c and c not in (wf.get("checklist") or []):
                wf.setdefault("checklist", []).append(c)
                wf.setdefault("checklist_passed", {})[c] = False
    return {"ok": True, "checklist_count": len(wf.get("checklist") or [])}


def _spec_lock(wf: dict, args: dict) -> dict:
    if not wf.get("spec_md"):
        return {"error": "spec.md 为空，请先 spec.generate"}
    wf["stage"] = "EXECUTING"
    return {"ok": True, "stage": wf["stage"], "hint": "规格已锁定，开始按 tasks.md 执行。"}


def _spec_task_mark(wf: dict, args: dict) -> dict:
    task = args.get("task", "").strip()
    status = args.get("status", "in_progress")
    if not task:
        return {"error": "task 参数不能为空"}
    wf.setdefault("task_status", {})[task] = status
    return {"ok": True, "task": task, "status": status}


def _spec_checklist_check(wf: dict, args: dict) -> dict:
    item = args.get("item", "").strip()
    passed = bool(args.get("passed", False))
    if not item:
        return {"error": "item 参数不能为空"}
    cl = wf.get("checklist") or []
    if item not in cl:
        cand = [c for c in cl if item in c or c in item]
        if cand:
            item = cand[0]
        else:
            return {"error": f"验收项 '{item}' 不在 checklist 里"}
    wf.setdefault("checklist_passed", {})[item] = passed
    cp = wf.get("checklist_passed", {})
    all_passed = all(cp.get(c, False) for c in cl)
    if all_passed and cl and wf.get("stage") == "EXECUTING":
        wf["stage"] = "DONE"
    passed_count = sum(1 for v in cp.values() if v)
    return {"ok": True, "item": item, "passed": passed,
            "passed_count": passed_count, "total": len(cl), "stage": wf.get("stage")}


def _spec_get_status(wf: dict, args: dict) -> dict:
    cp = wf.get("checklist_passed", {})
    cl = wf.get("checklist", [])
    return {"stage": wf.get("stage"),
            "spec_md": wf.get("spec_md", ""),
            "tasks_md": wf.get("tasks_md", ""),
            "checklist": cl,
            "checklist_passed": cp,
            "task_status": wf.get("task_status", {})}


def _goal_create(wf: dict, args: dict) -> dict:
    wf["objective"] = args.get("objective", "").strip()
    wf["success_criteria"] = [c for c in (args.get("success_criteria") or []) if str(c).strip()]
    thr = args.get("threshold")
    if thr is not None:
        try:
            wf["threshold"] = max(0.0, min(1.0, float(thr)))
        except Exception:
            pass
    wf["stage"] = "RUNNING"
    return {"ok": True, "stage": wf["stage"],
            "objective": wf["objective"],
            "criteria_count": len(wf["success_criteria"]),
            "threshold": wf["threshold"],
            "hint": "目标已设定，开始推进。每轮结束时系统会自动评估达成度。"}


def _goal_update(wf: dict, args: dict) -> dict:
    if args.get("objective"):
        wf["objective"] = args["objective"].strip()
    sc = args.get("success_criteria")
    if sc:
        wf["success_criteria"] = [c for c in sc if str(c).strip()]
    thr = args.get("threshold")
    if thr is not None:
        try:
            wf["threshold"] = max(0.0, min(1.0, float(thr)))
        except Exception:
            pass
    return {"ok": True, "objective": wf["objective"], "threshold": wf["threshold"]}


def _goal_status(wf: dict, args: dict) -> dict:
    return {"stage": wf.get("stage"), "objective": wf.get("objective", ""),
            "success_criteria": wf.get("success_criteria", []),
            "threshold": wf.get("threshold", 0.8),
            "last_score": wf.get("last_score", 0.0),
            "last_gaps": wf.get("last_gaps", ""),
            "history": wf.get("history", [])[-5:]}


_HANDLERS = {
    "plan.create": _plan_create,
    "plan.update": _plan_update,
    "plan.task_done": _plan_task_done,
    "plan.get_status": _plan_get_status,
    "spec.generate": _spec_generate,
    "spec.update": _spec_update,
    "spec.lock": _spec_lock,
    "spec.task_mark": _spec_task_mark,
    "spec.checklist_check": _spec_checklist_check,
    "spec.get_status": _spec_get_status,
    "goal.create": _goal_create,
    "goal.update": _goal_update,
    "goal.status": _goal_status,
}


# ============================================================
# Goal 模式：达成度评估器（运行时每轮结束时调用）
# ============================================================
_GOAL_ASSESS_SYSTEM = (
    "你是目标达成度评估器。给定目标、成功标准、本轮工具历史与智能体回复，"
    "评估当前对目标的达成程度。只输出 JSON：\n"
    "{\"score\": 0-1 浮点, \"gaps\": \"尚未达成的标准/缺口（简洁列表）\", "
    "\"next_step\": \"下一步建议\", \"done\": true/false}\n"
    "评分规则：每条成功标准达成约得 1/N 分，全部达成 score=1.0；done=true 当且仅当 score≥阈值。"
)


async def goal_assess(interface: dict, model: str, wf: dict,
                      tool_history: list, assistant_text: str) -> dict:
    """Goal 模式自评：返回 {score, gaps, next_step, done, adopt}。

    复用 validator 的 LLM 调用模式（chat_with_failover 非流式 + JSON 解析兜底）。
    """
    criteria = wf.get("success_criteria") or []
    threshold = float(wf.get("threshold") or 0.8)
    tool_lines = []
    for th in tool_history or []:
        r = th.get("result") or {}
        tool_lines.append(f"- {th.get('name')}({json.dumps(th.get('args') or {}, ensure_ascii=False)[:120]}) → {json.dumps(r, ensure_ascii=False)[:200]}")
    user_msg = (
        f"【目标】\n{wf.get('objective','')}\n\n"
        f"【成功标准（每条达成约得 1/{len(criteria)} 分）】\n"
        + ("\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria)) if criteria else "（未设定标准）")
        + f"\n\n【阈值】{threshold}\n\n"
        f"【本轮工具历史】\n" + ("\n".join(tool_lines) if tool_lines else "（未调用工具）")
        + f"\n\n【智能体本轮回复】\n{assistant_text or '（空）'}"
    )
    messages = [{"role": "system", "content": _GOAL_ASSESS_SYSTEM},
                {"role": "user", "content": user_msg}]
    from core.llm import chat_with_failover as chat
    last_err = ""
    async for kind, payload in chat(interface, model, messages, stream=False):
        if kind == "error":
            last_err = f"HTTP {payload.get('status','?')}: {str(payload.get('detail',''))[:200]}"
            continue
        if kind == "message":
            content = (payload.get("content") or "").strip()
            try:
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
                parsed = json.loads(content)
            except Exception:
                parsed = {"score": 0.4, "gaps": content[:200], "next_step": "", "done": False}
            score = float(parsed.get("score", 0.4))
            gaps = str(parsed.get("gaps", ""))
            done = score >= threshold
            wf["last_score"] = score
            wf["last_gaps"] = gaps
            wf.setdefault("history", []).append({"score": score, "gaps": gaps})
            if done:
                wf["stage"] = "DONE"
            return {"score": score, "gaps": gaps,
                    "next_step": str(parsed.get("next_step", "")),
                    "done": done, "adopt": done}
    # LLM 调用失败 → 保守不采纳，让主循环再给一次机会
    return {"score": 0.3, "gaps": f"评估器调用失败：{last_err}", "next_step": "", "done": False, "adopt": False}
