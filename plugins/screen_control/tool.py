"""screen_control 插件 —— 屏幕控制（基于 vercel-labs/agent-desktop）

通过操作系统无障碍 API（Windows UI Automation / macOS AX / Linux AT-SPI）以语义方式
读取窗口控件树并精准操作：不需要截图与坐标估算，默认不移动鼠标指针、不抢焦点。
与「桌面控制」(desktop_control，像素级/录屏) 互补。

依赖外部 CLI：agent-desktop（Rust，经 npm 分发，ref/快照存储于 ~/.agent-desktop，
跨进程持续有效）。首次使用可执行 action=setup 自动安装。

核心工作流：snapshot 取 ref → click/type/... 作用于 ref → 再 snapshot 验证。
ref 形如 @<snapshot_id>:eN，必须完整携带快照 id；UI 变化后 CLI 返回 STALE_REF /
AMBIGUOUS_TARGET，此时需重新 snapshot 取新 ref，绝不盲目重试。
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

_NPM_SPEC = "agent-desktop"
_REF_RE = re.compile(r"^@[\w-]+:[\w-]+$")
_XY_RE = re.compile(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?$")
_IS_PROPERTIES = {"visible", "enabled", "checked", "focused", "expanded", "selected"}
_BATCH_MAX_CHARS = 30000
_MAX_DATA_CHARS = 250000
_TIMEOUT_FLOOR_S = 30
_TIMEOUT_CAP_S = 240
_LONG_TEXT_STDIN = 7000

_HINTS = {
    "STALE_REF": "ref 已失效（界面发生变化）：请重新 snapshot 并使用新返回的 ref",
    "AMBIGUOUS_TARGET": "匹配到多个候选：重新 snapshot 缩小范围，或用 windowId 指定窗口；同名孪生元素无法用 ref 区分，只能用 physical 坐标点击",
    "SNAPSHOT_NOT_FOUND": "快照不存在或已被清理：请重新 snapshot",
    "APP_NOT_FOUND": "未找到应用：先用 action=listApps 查看运行中的应用名，名称需精确匹配",
    "WINDOW_NOT_FOUND": "窗口不存在：先用 action=listWindows 查询窗口 id",
    "PERM_DENIED": "系统权限不足：执行 action=status 查看权限状态",
    "POLICY_DENIED": "无头模式禁止物理输入：传 headed=true 重试",
    "TIMEOUT": "等待超时：查看 error.details.kind；仅当 error.disposition.retry=safe 时才可重试",
    "ACTION_NOT_SUPPORTED": "目标元素不支持该动作：查看 snapshot 返回的 available_actions 换动作",
    "INVALID_ARGS": "参数无效：ref 必须是完整 @<snapshot_id>:eN 形式",
    "APP_UNRESPONSIVE": "应用无响应：稍候再试",
    "ELEMENT_NOT_FOUND": "未找到匹配元素：检查 role/text 拼写是否正确",
}


def _detect_root() -> Path:
    here = Path(__file__).resolve().parent
    node = here
    for _ in range(6):
        if (node / "backend").is_dir() or (node / ".git").exists():
            return node
        if node.parent == node:
            break
        node = node.parent
    return here.parent.parent


def _find_cli():
    exe = shutil.which("agent-desktop")
    if exe:
        return exe
    appdata = os.environ.get("APPDATA")
    if appdata:
        for name in ("agent-desktop.cmd", "agent-desktop.exe", "agent-desktop"):
            cand = os.path.join(appdata, "npm", name)
            if os.path.isfile(cand):
                return cand
    return None


def _find_npm():
    exe = shutil.which("npm")
    if exe:
        return exe
    appdata = os.environ.get("APPDATA")
    if appdata:
        cand = os.path.join(appdata, "npm", "npm.cmd")
        if os.path.isfile(cand):
            return cand
    return None


def _timeout_s(timeout_ms) -> float:
    ms = 30000.0
    try:
        if timeout_ms is not None and float(timeout_ms) > 0:
            ms = float(timeout_ms)
    except (TypeError, ValueError):
        pass
    return max(_TIMEOUT_FLOOR_S, min(_TIMEOUT_CAP_S, ms / 1000.0 + 20))


def _shrink(data):
    if not isinstance(data, (dict, list)):
        return data
    try:
        size = len(json.dumps(data, ensure_ascii=False))
    except (TypeError, ValueError):
        return data
    if size <= _MAX_DATA_CHARS:
        return data
    if isinstance(data, dict) and isinstance(data.get("tree"), dict):
        kept = {k: v for k, v in data.items() if k != "tree"}
        kept["tree_omitted"] = True
        kept["note"] = ("控件树过大（%d 字符）已省略；请用 interactiveOnly=true、compact=true、"
                        "rootRef 钻取局部，或提高 maxDepth 前先 skeleton 观察" % size)
        return kept
    return {"omitted": True, "size": size,
            "note": "结果过大已省略", "keys": list(data)[:50] if isinstance(data, dict) else None}


def _zh_hint(code):
    if not code:
        return None
    return _HINTS.get(str(code).upper())


def _run_cli(args, timeout_ms=None, input_text=None):
    exe = _find_cli()
    if not exe:
        return {"ok": False,
                "error": {"code": "CLI_NOT_INSTALLED",
                          "message": "未找到 agent-desktop CLI"},
                "hint": "执行 action=setup 可自动安装（npm install -g agent-desktop --allow-scripts=agent-desktop）"}
    argv = [exe] + [str(a) for a in args]
    timeout_s = _timeout_s(timeout_ms)
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout_s,
                              encoding="utf-8", errors="replace",
                              input=input_text if input_text is not None else None)
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "error": {"code": "PLUGIN_TIMEOUT",
                          "message": "CLI 执行超过 %.0fs 被终止" % timeout_s},
                "hint": "可调大 timeoutMs 重试，或改用 action=wait 等待 UI 就绪后再操作"}
    out = (proc.stdout or "").strip()
    env = None
    if out:
        try:
            env = json.loads(out)
        except ValueError:
            idx = out.find("{")
            if idx >= 0:
                try:
                    env = json.loads(out[idx:])
                except ValueError:
                    env = None
    if not isinstance(env, dict):
        return {"ok": False,
                "error": {"code": "CLI_BAD_OUTPUT", "message": "CLI 未返回 JSON envelope"},
                "stdout": out[:2000], "stderr": (proc.stderr or "").strip()[:2000]}
    if env.get("ok"):
        return {"ok": True, "command": env.get("command"), "data": _shrink(env.get("data"))}
    err = env.get("error") or {}
    result = {"ok": False, "command": env.get("command"), "error": err}
    hint = _zh_hint(err.get("code"))
    if hint:
        result["hint"] = hint
    return result


def _run_cli_text(args, timeout_ms=None):
    exe = _find_cli()
    if not exe:
        return {"ok": False,
                "error": {"code": "CLI_NOT_INSTALLED", "message": "未找到 agent-desktop CLI"},
                "hint": "执行 action=setup 可自动安装"}
    try:
        proc = subprocess.run([exe] + [str(a) for a in args], capture_output=True,
                              timeout=_timeout_s(timeout_ms), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "PLUGIN_TIMEOUT", "message": "CLI 执行超时"}}
    out = (proc.stdout or "").strip()
    try:
        env = json.loads(out)
        if isinstance(env, dict) and "ok" in env:
            if env.get("ok"):
                return {"ok": True, "command": env.get("command"), "data": _shrink(env.get("data"))}
            return {"ok": False, "command": env.get("command"), "error": env.get("error") or {}}
    except ValueError:
        pass
    return {"ok": proc.returncode == 0, "command": args[0], "text": out[:100000]}


def _opt(argv, flag, value):
    if value is None or value == "" or value is False:
        return
    argv.append("%s=%s" % (flag, value))


def _bool(argv, flag, value):
    if value is True:
        argv.append(flag)


def _require_ref(kw):
    ref = str(kw.get("ref") or "").strip()
    if not ref:
        return None, {"ok": False,
                      "error": {"code": "MISSING_REF", "message": "缺少 ref 参数"},
                      "hint": "先 action=snapshot 获取元素 ref（形如 @<snapshot_id>:eN）"}
    if not _REF_RE.match(ref):
        return None, {"ok": False,
                      "error": {"code": "BAD_REF",
                                "message": "ref 格式应为 @<snapshot_id>:eN，收到: %s" % ref},
                      "hint": "ref 必须来自最近一次 snapshot 的返回"}
    return ref, None


def _require_str(kw, key, code):
    v = str(kw.get(key) or "").strip()
    if not v:
        return None, {"ok": False, "error": {"code": code, "message": "缺少 %s 参数" % key}}
    return v, None


def _action_tail(kw):
    argv = []
    _bool(argv, "--headed", kw.get("headed"))
    _opt(argv, "--wait-for", kw.get("waitFor"))
    _opt(argv, "--timeout-ms", kw.get("timeoutMs"))
    return argv


def _snapshot(kw):
    argv = ["snapshot"]
    _opt(argv, "--app", kw.get("app"))
    _opt(argv, "--window-id", kw.get("windowId"))
    _opt(argv, "--surface", kw.get("surface"))
    _opt(argv, "--root", kw.get("rootRef"))
    _bool(argv, "--interactive-only", kw.get("interactiveOnly"))
    _bool(argv, "--compact", kw.get("compact"))
    _opt(argv, "--wait-for", kw.get("waitFor"))
    _opt(argv, "--timeout-ms", kw.get("timeoutMs"))
    return _run_cli(argv, kw.get("timeoutMs"))


def _find(kw):
    argv = ["find"]
    _opt(argv, "--app", kw.get("app"))
    _opt(argv, "--window-id", kw.get("windowId"))
    _opt(argv, "--surface", kw.get("surface"))
    _opt(argv, "--root", kw.get("rootRef"))
    _opt(argv, "--role", kw.get("role"))
    _opt(argv, "--text", kw.get("text"))
    mode = str(kw.get("resultMode") or "").strip().lower()
    if mode == "first":
        argv.append("--first")
    elif mode == "last":
        argv.append("--last")
    elif mode == "count":
        argv.append("--count")
    elif mode == "nth":
        _opt(argv, "--nth", kw.get("nth") if kw.get("nth") is not None else 0)
    elif mode == "all":
        argv.append("--limit=0")
    _opt(argv, "--timeout-ms", kw.get("timeoutMs"))
    return _run_cli(argv, kw.get("timeoutMs"))


def _read_element(kw):
    ref, err = _require_ref(kw)
    if err:
        return err
    prop = str(kw.get("property") or "text").strip().lower()
    if prop in _IS_PROPERTIES:
        argv = ["is", ref, "--property=%s" % prop]
    else:
        argv = ["get", ref, "--property=%s" % prop]
    argv += _action_tail(kw)
    return _run_cli(argv, kw.get("timeoutMs"))


def _click(kw):
    ref, err = _require_ref(kw)
    if err:
        return err
    cmd = {"single": "click", "double": "double-click",
           "triple": "triple-click", "right": "right-click"}.get(
        str(kw.get("clickType") or "single").strip().lower(), "click")
    argv = [cmd, ref] + _action_tail(kw)
    return _run_cli(argv, kw.get("timeoutMs"))


def _type(kw):
    ref, err = _require_ref(kw)
    if err:
        return err
    text, err = _require_str(kw, "text", "MISSING_TEXT")
    if err:
        return err
    argv = ["type", ref] + _action_tail(kw)
    if len(text) > _LONG_TEXT_STDIN:
        argv.append("--stdin")
        return _run_cli(argv, kw.get("timeoutMs"), input_text=text)
    argv.append(text)
    return _run_cli(argv, kw.get("timeoutMs"))


def _set_value(kw):
    ref, err = _require_ref(kw)
    if err:
        return err
    value, err = _require_str(kw, "value", "MISSING_VALUE")
    if err:
        return err
    argv = ["set-value", ref] + _action_tail(kw)
    if len(value) > _LONG_TEXT_STDIN:
        argv.append("--stdin")
        return _run_cli(argv, kw.get("timeoutMs"), input_text=value)
    argv.append(value)
    return _run_cli(argv, kw.get("timeoutMs"))


_REF_ACTIONS = {
    "clear": "clear", "focus": "focus", "toggle": "toggle", "check": "check",
    "uncheck": "uncheck", "expand": "expand", "collapse": "collapse", "select": "select",
}


def _ref_action(kw):
    ref, err = _require_ref(kw)
    if err:
        return err
    sub, err = _require_str(kw, "subAction", "MISSING_SUBACTION")
    if err:
        return err
    cmd = _REF_ACTIONS.get(sub.lower())
    if not cmd:
        return {"ok": False,
                "error": {"code": "BAD_SUBACTION",
                          "message": "refAction 的 subAction 仅支持: %s" % ", ".join(sorted(_REF_ACTIONS))}}
    argv = [cmd, ref] + _action_tail(kw)
    if cmd == "select":
        value, err = _require_str(kw, "value", "MISSING_VALUE")
        if err:
            return err
        argv.append(value)
    return _run_cli(argv, kw.get("timeoutMs"))


def _press_key(kw):
    key, err = _require_str(kw, "key", "MISSING_KEY")
    if err:
        return err
    argv = ["press", key]
    _opt(argv, "--app", kw.get("app"))
    _opt(argv, "--window-id", kw.get("windowId"))
    argv += _action_tail(kw)
    return _run_cli(argv, kw.get("timeoutMs"))


def _scroll(kw):
    ref, err = _require_ref(kw)
    if err:
        return err
    argv = ["scroll", ref]
    _opt(argv, "--direction", kw.get("direction") or "down")
    _opt(argv, "--amount", kw.get("amount"))
    argv += _action_tail(kw)
    return _run_cli(argv, kw.get("timeoutMs"))


def _launch(kw):
    app, err = _require_str(kw, "app", "MISSING_APP")
    if err:
        return err
    argv = ["launch", app]
    _bool(argv, "--activate", kw.get("activate"))
    for a in (kw.get("arg") or []):
        _opt(argv, "--arg", a)
    _opt(argv, "--timeout", kw.get("timeoutMs"))
    return _run_cli(argv, kw.get("timeoutMs"))


def _close_app(kw):
    app, err = _require_str(kw, "app", "MISSING_APP")
    if err:
        return err
    argv = ["close-app", app]
    _bool(argv, "--force", kw.get("force"))
    return _run_cli(argv, kw.get("timeoutMs"))


def _list_apps(kw):
    argv = ["list-apps"]
    _opt(argv, "--app", kw.get("app"))
    return _run_cli(argv)


def _list_windows(kw):
    argv = ["list-windows"]
    _opt(argv, "--app", kw.get("app"))
    return _run_cli(argv)


_WINDOW_OPS = {"focus", "resize", "move", "minimize", "maximize", "restore"}


def _window_op(kw):
    sub, err = _require_str(kw, "subAction", "MISSING_SUBACTION")
    if err:
        return err
    sub = sub.lower()
    if sub not in _WINDOW_OPS:
        return {"ok": False,
                "error": {"code": "BAD_SUBACTION",
                          "message": "windowOp 的 subAction 仅支持: %s" % ", ".join(sorted(_WINDOW_OPS))}}
    if sub == "focus":
        argv = ["focus-window"]
        _opt(argv, "--app", kw.get("app"))
        _opt(argv, "--window-id", kw.get("windowId"))
        _opt(argv, "--title", kw.get("title"))
        return _run_cli(argv)
    if sub == "resize":
        if kw.get("width") is None or kw.get("height") is None:
            return {"ok": False, "error": {"code": "MISSING_SIZE",
                    "message": "resize 需要 width 与 height 参数"}}
        argv = ["resize-window", "--width=%s" % kw["width"], "--height=%s" % kw["height"]]
    elif sub == "move":
        if kw.get("x") is None or kw.get("y") is None:
            return {"ok": False, "error": {"code": "MISSING_XY",
                    "message": "move 需要 x 与 y 参数"}}
        argv = ["move-window", "--x=%s" % kw["x"], "--y=%s" % kw["y"]]
    else:
        argv = [sub]
    _opt(argv, "--app", kw.get("app"))
    _opt(argv, "--window-id", kw.get("windowId"))
    return _run_cli(argv)


_CLIP_OPS = {"get", "set", "clear"}


def _clipboard(kw):
    sub, err = _require_str(kw, "subAction", "MISSING_SUBACTION")
    if err:
        return err
    sub = sub.lower()
    if sub not in _CLIP_OPS:
        return {"ok": False,
                "error": {"code": "BAD_SUBACTION",
                          "message": "clipboard 的 subAction 仅支持: get/set/clear"}}
    if sub == "get":
        return _run_cli(["clipboard-get"])
    if sub == "clear":
        return _run_cli(["clipboard-clear"])
    text, err = _require_str(kw, "text", "MISSING_TEXT")
    if err:
        return err
    if len(text) > _LONG_TEXT_STDIN:
        return _run_cli(["clipboard-set", "--stdin"], input_text=text)
    return _run_cli(["clipboard-set", text])


def _notifications(kw):
    sub, err = _require_str(kw, "subAction", "MISSING_SUBACTION")
    if err:
        return err
    sub = sub.lower()
    if sub == "list":
        argv = ["list-notifications"]
        _opt(argv, "--app", kw.get("app"))
        _opt(argv, "--text", kw.get("text"))
        return _run_cli(argv)
    if sub == "dismissall":
        argv = ["dismiss-all-notifications"]
        _opt(argv, "--app", kw.get("app"))
        return _run_cli(argv)
    if sub in ("dismiss", "action"):
        idx = kw.get("index")
        if idx is None:
            return {"ok": False, "error": {"code": "MISSING_INDEX",
                    "message": "dismiss/action 需要 index 参数（list-notifications 返回的 1 起始序号）"}}
        if sub == "dismiss":
            app, err = _require_str(kw, "app", "MISSING_APP")
            if err:
                return {"ok": False,
                        "error": {"code": "MISSING_APP",
                                  "message": "dismiss 必须携带 app 参数以校验通知归属，防止误删其他应用的通知"},
                        "hint": "先 action=notifications(list) 查看通知及其所属应用"}
            return _run_cli(["dismiss-notification", str(idx), "--app=%s" % app])
        action_name, err = _require_str(kw, "actionName", "MISSING_ACTION_NAME")
        if err:
            return err
        return _run_cli(["notification-action", str(idx), action_name])
    return {"ok": False,
            "error": {"code": "BAD_SUBACTION",
                      "message": "notifications 的 subAction 仅支持: list/dismiss/dismissAll/action"}}


def _physical(kw):
    sub, err = _require_str(kw, "subAction", "MISSING_SUBACTION")
    if err:
        return err
    sub = sub.lower()
    argv = None
    if sub == "hover":
        ref = str(kw.get("ref") or "").strip()
        xy = str(kw.get("xy") or "").strip()
        if ref:
            argv = ["hover", ref]
        elif xy and _XY_RE.match(xy):
            argv = ["hover", "--xy=%s" % xy]
        else:
            return {"ok": False, "error": {"code": "MISSING_TARGET",
                    "message": "hover 需要 ref 或 xy（格式 x,y）"}}
    elif sub == "move":
        xy = str(kw.get("xy") or "").strip()
        if not xy or not _XY_RE.match(xy):
            return {"ok": False, "error": {"code": "MISSING_XY",
                    "message": "move 需要 xy 参数（格式 x,y，屏幕绝对坐标）"}}
        argv = ["mouse-move", "--xy=%s" % xy]
    elif sub == "click":
        xy = str(kw.get("xy") or "").strip()
        if not xy or not _XY_RE.match(xy):
            return {"ok": False, "error": {"code": "MISSING_XY",
                    "message": "click 需要 xy 参数（格式 x,y，屏幕绝对坐标）"}}
        argv = ["mouse-click", "--xy=%s" % xy, "--button=%s" % (kw.get("button") or "left")]
    else:
        return {"ok": False,
                "error": {"code": "BAD_SUBACTION",
                          "message": "physical 的 subAction 仅支持: hover/move/click（拖拽等走 action=batch）"}}
    _bool(argv, "--headed", True)
    _opt(argv, "--wait-for", kw.get("waitFor"))
    return _run_cli(argv, kw.get("timeoutMs"))


def _wait(kw):
    ms = kw.get("ms")
    ref = str(kw.get("ref") or "").strip()
    title = str(kw.get("title") or "").strip()
    text = str(kw.get("text") or "").strip()
    timeout_ms = kw.get("timeoutMs")
    if ms is not None:
        try:
            return _run_cli(["wait", str(int(float(ms)))])
        except (TypeError, ValueError):
            return {"ok": False, "error": {"code": "BAD_MS", "message": "ms 必须是数字（毫秒）"}}
    if ref:
        if not _REF_RE.match(ref):
            return {"ok": False, "error": {"code": "BAD_REF",
                    "message": "ref 格式应为 @<snapshot_id>:eN，收到: %s" % ref}}
        argv = ["wait", "--element=%s" % ref]
        _opt(argv, "--predicate", kw.get("predicate"))
        _opt(argv, "--value", kw.get("value"))
        _opt(argv, "--wait-timeout", timeout_ms)
        return _run_cli(argv, timeout_ms)
    if title:
        argv = ["wait", "--window=%s" % title]
        _opt(argv, "--wait-timeout", timeout_ms)
        return _run_cli(argv, timeout_ms)
    if text:
        argv = ["wait", "--text=%s" % text]
        _opt(argv, "--app", kw.get("app"))
        _opt(argv, "--wait-timeout", timeout_ms)
        return _run_cli(argv, timeout_ms)
    return {"ok": False,
            "error": {"code": "MISSING_WAIT_TARGET",
                      "message": "wait 需要 ms（固定等待）/ ref（等元素）/ title（等窗口）/ text（等文本）之一"}}


def _batch(kw):
    cmds = kw.get("commands")
    if cmds is None:
        return {"ok": False,
                "error": {"code": "MISSING_COMMANDS",
                          "message": "batch 需要 commands 参数：JSON 数组，每项形如 {\"command\": \"snapshot\", \"args\": [\"--app\", \"NOTEPAD\"]}"}}
    payload = cmds.strip() if isinstance(cmds, str) else json.dumps(cmds, ensure_ascii=False)
    if len(payload) > _BATCH_MAX_CHARS:
        return {"ok": False,
                "error": {"code": "BATCH_TOO_LARGE",
                          "message": "commands 超过 %d 字符上限（Windows 命令行长度限制）" % _BATCH_MAX_CHARS}}
    timeout_ms = kw.get("timeoutMs")
    argv = ["batch", payload]
    if timeout_ms:
        _opt(argv, "--timeout-ms", timeout_ms)
    else:
        argv.append("--timeout-ms=60000")
    return _run_cli(argv, max(float(timeout_ms or 60000) + 20000, timeout_ms or 0) or None)


def _get_guide(kw):
    guide = str(kw.get("guide") or "desktop").strip().lower()
    if guide not in ("desktop", "platform"):
        return {"ok": False, "error": {"code": "BAD_GUIDE",
                "message": "guide 仅支持 desktop（快照/ref 循环规范）或 platform（当前系统差异）"}}
    return _run_cli_text(["skills", "get", guide])


def _surfaces(kw):
    sub = str(kw.get("subAction") or "list").strip().lower()
    if sub == "open":
        surface, err = _require_str(kw, "surface", "MISSING_SURFACE")
        if err:
            return err
        return _run_cli(["open-system-surface", "--surface=%s" % surface])
    argv = ["list-surfaces"]
    _opt(argv, "--app", kw.get("app"))
    return _run_cli(argv)


def _screenshot(kw):
    out_path = str(kw.get("outPath") or "").strip()
    if not out_path:
        out_dir = _detect_root() / "outputs" / "screen_control"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / ("shot_%s.png" % datetime.now().strftime("%Y%m%d_%H%M%S")))
    argv = ["screenshot", out_path]
    _opt(argv, "--app", kw.get("app"))
    _opt(argv, "--window-id", kw.get("windowId"))
    _opt(argv, "--wait-for", kw.get("waitFor"))
    res = _run_cli(argv, kw.get("timeoutMs"))
    if isinstance(res.get("data"), dict):
        res["data"].setdefault("saved_to", out_path)
    else:
        res["data"] = {"saved_to": out_path, "cli_data": res.get("data")}
    return res


def _status(kw):
    return _run_cli(["status"])


def _version(kw):
    return _run_cli(["version"])


def _setup(kw):
    if _find_cli():
        v = _run_cli(["version"])
        if v.get("ok"):
            return {"ok": True, "already_installed": True, "data": v.get("data")}
    npm = _find_npm()
    if not npm:
        return {"ok": False,
                "error": {"code": "NPM_NOT_FOUND", "message": "未找到 npm（需要 Node.js 环境）"},
                "hint": "安装 Node.js 后重试，或手动执行 npm install -g agent-desktop --allow-scripts=agent-desktop"}
    try:
        proc = subprocess.run(
            [npm, "install", "-g", _NPM_SPEC, "--allow-scripts=agent-desktop"],
            capture_output=True, timeout=300, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "INSTALL_TIMEOUT", "message": "npm 安装超过 300s 被终止"}}
    tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-3000:]
    if proc.returncode != 0:
        return {"ok": False, "error": {"code": "INSTALL_FAILED", "message": "npm 安装失败"},
                "output": tail}
    v = _run_cli(["version"])
    return {"ok": bool(v.get("ok")), "install_output": tail,
            "version": v.get("data"), "version_error": v.get("error")}


_HANDLERS = {
    "setup": _setup,
    "status": _status,
    "version": _version,
    "snapshot": _snapshot,
    "find": _find,
    "readElement": _read_element,
    "screenshot": _screenshot,
    "click": _click,
    "type": _type,
    "setValue": _set_value,
    "refAction": _ref_action,
    "pressKey": _press_key,
    "scroll": _scroll,
    "launch": _launch,
    "closeApp": _close_app,
    "listApps": _list_apps,
    "listWindows": _list_windows,
    "windowOp": _window_op,
    "clipboard": _clipboard,
    "notifications": _notifications,
    "physical": _physical,
    "wait": _wait,
    "batch": _batch,
    "getGuide": _get_guide,
    "surfaces": _surfaces,
}


def run(**kwargs):
    action = str(kwargs.get("action") or "").strip()
    if not action:
        return {"ok": False,
                "error": {"code": "MISSING_ACTION",
                          "message": "缺少 action 参数，可选值: %s" % ", ".join(sorted(_HANDLERS))}}
    handler = _HANDLERS.get(action)
    if handler is None:
        return {"ok": False,
                "error": {"code": "UNKNOWN_ACTION", "message": "未知 action: %s" % action},
                "known_actions": sorted(_HANDLERS)}
    try:
        return handler(kwargs)
    except Exception as e:
        return {"ok": False,
                "error": {"code": "PLUGIN_ERROR", "message": "%s: %s" % (type(e).__name__, e)}}