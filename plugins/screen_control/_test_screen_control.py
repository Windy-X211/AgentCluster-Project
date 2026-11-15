"""screen_control 自测 —— 单元级参数映射 + 真机只读链路（不点击/不输入/不改系统）"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tool

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (("  | " + str(detail)[:300]) if detail and not cond else ""))


def cli_available():
    return tool._find_cli() is not None


# ============ 单元级：纯逻辑 ============
check("ref regex ok", tool._REF_RE.match("@s8f3k2p9:e8"))
check("ref regex reject bare", not tool._REF_RE.match("@e8"))
check("ref regex reject junk", not tool._REF_RE.match("s8f3k2p9:e8"))

shrink_big = tool._shrink({"tree": {"role": "window", "children": [{"i": "x" * 300000}]}}, )
check("shrink compresses big tree", isinstance(shrink_big, dict) and shrink_big.get("tree_omitted") is True)
shrink_small = tool._shrink({"ref_count": 3})
check("shrink keeps small data", shrink_small == {"ref_count": 3})

check("zh hint stale", tool._zh_hint("STALE_REF") is not None)
check("zh hint unknown", tool._zh_hint("WHATVER") is None)

r = tool.run()
check("missing action rejected", r.get("ok") is False and r["error"]["code"] == "MISSING_ACTION")
r = tool.run(action="nope")
check("unknown action rejected", r.get("ok") is False and r["error"]["code"] == "UNKNOWN_ACTION" and "setup" in r["known_actions"])

r = tool.run(action="click")
check("click missing ref", r["error"]["code"] == "MISSING_REF")
r = tool.run(action="click", ref="@e1")
check("click bare ref rejected", r["error"]["code"] == "BAD_REF")
r = tool.run(action="refAction", ref="@s1:e1", subAction="destroy")
check("refAction bad subaction", r["error"]["code"] == "BAD_SUBACTION")
r = tool.run(action="refAction", ref="@s1:e1", subAction="select")
check("refAction select needs value", r["error"]["code"] == "MISSING_VALUE")
r = tool.run(action="windowOp", subAction="explode")
check("windowOp bad subaction", r["error"]["code"] == "BAD_SUBACTION")
r = tool.run(action="windowOp", subAction="resize", width=800)
check("windowOp resize missing height", r["error"]["code"] == "MISSING_SIZE")
r = tool.run(action="notifications", subAction="dismiss", index=1)
check("dismiss without app blocked", r["error"]["code"] == "MISSING_APP")
r = tool.run(action="batch", commands="x" * 40000)
check("batch too large rejected", r["error"]["code"] == "BATCH_TOO_LARGE")
r = tool.run(action="physical", subAction="click")
check("physical click missing xy", r["error"]["code"] == "MISSING_XY")
r = tool.run(action="wait")
check("wait missing target", r["error"]["code"] == "MISSING_WAIT_TARGET")

# CLI 缺失路径
_orig_find = tool._find_cli
tool._find_cli = lambda: None
r = tool.run(action="status")
check("cli missing -> friendly error", r["error"]["code"] == "CLI_NOT_INSTALLED" and "setup" in r.get("hint", ""))
tool._find_cli = _orig_find

# ============ 单元级：argv 映射（mock _run_cli） ============
captured = []
_orig_run_cli = tool._run_cli


def fake_cli(args, timeout_ms=None, input_text=None):
    captured.append((list(args), timeout_ms, input_text))
    return {"ok": True, "command": args[0], "data": {}}


tool._run_cli = fake_cli

tool.run(action="snapshot", app="记事本", interactiveOnly=True, compact=True,
         surface="taskbar", rootRef="@s1:e2", timeoutMs=5000)
argv = captured[-1][0]
check("snapshot argv contains flags",
      "snapshot" == argv[0] and "--app=记事本" in argv and "--interactive-only" in argv
      and "--compact" in argv and "--surface=taskbar" in argv and "--root=@s1:e2" in argv
      and "--timeout-ms=5000" in argv, argv)

tool.run(action="find", text="设置", role="button", resultMode="first")
argv = captured[-1][0]
check("find first mode", "--first" in argv and "--text=设置" in argv and "--role=button" in argv, argv)

tool.run(action="find", resultMode="nth", nth=2)
check("find nth mode", "--nth=2" in captured[-1][0], captured[-1][0])

tool.run(action="find", resultMode="all")
check("find all mode", "--limit=0" in captured[-1][0], captured[-1][0])

tool.run(action="find", resultMode="count")
check("find count mode", "--count" in captured[-1][0], captured[-1][0])

tool.run(action="readElement", ref="@s1:e1", property="checked")
argv = captured[-1][0]
check("readElement is for states", argv[:2] == ["is", "@s1:e1"] and "--property=checked" in argv, argv)

tool.run(action="readElement", ref="@s1:e1")
argv = captured[-1][0]
check("readElement get default text", argv[:2] == ["get", "@s1:e1"] and "--property=text" in argv, argv)

tool.run(action="click", ref="@s1:e1", clickType="right")
check("click right maps", captured[-1][0][0] == "right-click" and captured[-1][0][1] == "@s1:e1", captured[-1][0])

tool.run(action="click", ref="@s1:e1", clickType="double")
check("click double maps", captured[-1][0][0] == "double-click", captured[-1][0])

tool.run(action="click", ref="@s1:e1")
check("click default single", captured[-1][0][0] == "click", captured[-1][0])

tool.run(action="type", ref="@s1:e1", text="你好 world")
argv = captured[-1][0]
check("type inline text", argv[:2] == ["type", "@s1:e1"] and argv[-1] == "你好 world", argv)

long_text = "长" * 8000
tool.run(action="type", ref="@s1:e1", text=long_text)
argv, _, inp = captured[-1]
check("type long text via stdin", "--stdin" in argv and inp == long_text, argv)

tool.run(action="setValue", ref="@s1:e1", value="v1")
check("setValue argv", captured[-1][0][:2] == ["set-value", "@s1:e1"] and captured[-1][0][-1] == "v1", captured[-1][0])

tool.run(action="refAction", ref="@s1:e1", subAction="toggle")
check("refAction toggle", captured[-1][0][0] == "toggle", captured[-1][0])

tool.run(action="refAction", ref="@s1:e1", subAction="select", value="B")
argv = captured[-1][0]
check("refAction select argv", argv[0] == "select" and argv[1] == "@s1:e1" and argv[-1] == "B", argv)

tool.run(action="pressKey", key="ctrl+s", app="记事本", headed=True)
argv = captured[-1][0]
check("pressKey argv", argv[0] == "press" and argv[1] == "ctrl+s" and "--app=记事本" in argv and "--headed" in argv, argv)

tool.run(action="scroll", ref="@s1:e1", direction="up", amount=5)
argv = captured[-1][0]
check("scroll argv", argv[0] == "scroll" and "--direction=up" in argv and "--amount=5" in argv, argv)

tool.run(action="launch", app="notepad", arg=["-a", "C:/x.txt"], activate=True)
argv = captured[-1][0]
check("launch argv", argv[0] == "launch" and argv[1] == "notepad" and "--activate" in argv
      and "--arg=-a" in argv and "--arg=C:/x.txt" in argv, argv)

tool.run(action="closeApp", app="notepad", force=True)
check("closeApp force", captured[-1][0] == ["close-app", "notepad", "--force"], captured[-1][0])

tool.run(action="windowOp", subAction="move", x=-100, y=50, windowId="w-1")
argv = captured[-1][0]
check("windowOp move negative eq", argv == ["move-window", "--x=-100", "--y=50", "--window-id=w-1"], argv)

tool.run(action="windowOp", subAction="focus", title="设置")
check("windowOp focus title", "--title=设置" in captured[-1][0], captured[-1][0])

tool.run(action="windowOp", subAction="minimize", app="x")
check("windowOp minimize", captured[-1][0][0] == "minimize", captured[-1][0])

tool.run(action="physical", subAction="click", xy="100,-200", button="right")
argv = captured[-1][0]
check("physical click auto headed", argv[0] == "mouse-click" and "--xy=100,-200" in argv
      and "--button=right" in argv and "--headed" in argv, argv)

tool.run(action="wait", ms=1500)
check("wait ms positional", captured[-1][0] == ["wait", "1500"], captured[-1][0])

tool.run(action="wait", ref="@s1:e1", predicate="value", value="done", timeoutMs=8000)
argv = captured[-1][0]
check("wait element argv", "--element=@s1:e1" in argv and "--predicate=value" in argv
      and "--value=done" in argv and "--wait-timeout=8000" in argv, argv)

tool.run(action="wait", title="记事本", timeoutMs=9000)
argv = captured[-1][0]
check("wait window argv", "--window=记事本" in argv and "--wait-timeout=9000" in argv, argv)

tool.run(action="batch", commands=[{"command": "snapshot", "args": ["--app", "x"]}])
argv, _, _ = captured[-1]
check("batch json payload", argv[0] == "batch" and json.loads(argv[1])[0]["command"] == "snapshot"
      and "--timeout-ms=60000" in argv, argv)

tool._run_cli = _orig_run_cli
tool.run(action="getGuide", guide="desktop")

# ============ 真机只读链路 ============
if cli_available():
    print("--- 真机只读（CLI: %s）---" % tool._find_cli())
    r = tool.run(action="status")
    check("status ok", r.get("ok") is True and r.get("data", {}).get("platform") == "windows", r)
    r = tool.run(action="version")
    check("version 1.x", r.get("ok") and str(r["data"].get("version", "")).startswith("1."), r)
    r = tool.run(action="listWindows")
    wins = r.get("data") or []
    check("listWindows ok", r.get("ok") is True and isinstance(wins, list), r)
    target = next((w for w in wins if w.get("accessible") and not w.get("minimized")), None)
    if target:
        app_name = target["app_name"]
        print("--- 目标窗口: %s | %s" % (app_name, target.get("title", "")))
        r = tool.run(action="snapshot", app=app_name, interactiveOnly=True, compact=True, timeoutMs=15000)
        data = r.get("data") or {}
        check("snapshot ok", r.get("ok") is True and data.get("snapshot_id"), json.dumps(r)[:300])

        def collect_refs(node, out):
            rid = node.get("ref_id")
            if rid:
                out.append(rid)
            for ch in node.get("children") or []:
                collect_refs(ch, out)
            return out

        refs = collect_refs(data.get("tree") or {}, [])
        check("snapshot has refs", len(refs) > 0, "ref_count=%s" % data.get("ref_count"))
        if refs:
            r2 = tool.run(action="readElement", ref=refs[0], property="role")
            check("readElement role", r2.get("ok") is True, json.dumps(r2)[:300])
            r3 = tool.run(action="readElement", ref=refs[0], property="visible")
            d3 = (r3.get("data") or {})
            check("readElement visible bool", r3.get("ok") is True
                  and d3.get("result") is True and d3.get("property") == "visible", json.dumps(r3)[:300])
        r = tool.run(action="find", app=app_name, role="button", resultMode="first", timeoutMs=15000)
        check("find role=button", r.get("ok") is True, json.dumps(r)[:300])
        r = tool.run(action="screenshot", app=app_name)
        shot = (r.get("data") or {})
        path = shot.get("saved_to") or shot.get("path") or shot.get("file")
        check("screenshot saved", r.get("ok") is True and path and Path(str(path)).is_file(), json.dumps(r)[:300])
    else:
        print("跳过窗口级真机用例：无可访问的可见窗口")
    r = tool.run(action="readElement", ref="@deadbeefdeadbeef:e1")
    check("stale ref surfaces", r.get("ok") is False and r["error"]["code"] in
          ("STALE_REF", "SNAPSHOT_NOT_FOUND"), json.dumps(r)[:300])
    r = tool.run(action="getGuide")
    check("getGuide returns content", r.get("ok") is True and (r.get("text") or r.get("data")), json.dumps(r)[:200])
    r = tool.run(action="listApps")
    check("listApps ok", r.get("ok") is True, json.dumps(r)[:200])
else:
    print("跳过真机链路：CLI 未安装")

print("\n=== %d passed, %d failed ===" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)