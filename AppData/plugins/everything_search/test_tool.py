# -*- coding: utf-8 -*-
"""everything_search 插件单元测试：mock everything.client，验证 run() 各分支逻辑。"""
import sys
import types
import os
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "tool.py")

# 用一个真实存在的文件冒充「应用」，方便 open 分支通过 isfile 校验
REAL_EXE = sys.executable


class FakeResult:
    def __init__(self, name, parent, size, is_folder=False, ext=""):
        self.name = name
        self.parent_path = parent + "\\"
        self.full_path = os.path.join(parent, name)
        self.size = size
        self.is_folder = is_folder
        self.extension = ext
        self.date_modified = 0
        self.date_created = 0
        self.date_accessed = 0
        self.attributes = 0x20

    @property
    def modified_time(self):
        from datetime import datetime
        return datetime(2024, 1, 1, 12, 0, 0)


class FakeClient:
    def __init__(self, results):
        self._r = results
        self.searches = []

    def connect(self, version):
        self.version = version

    def search(self, text, **kw):
        self.searches.append((text, kw))
        # 极简匹配：取 name: 后的第一段关键词做子串匹配
        key = text.split("name:")[-1].split(" ")[0].lower()
        matched = [r for r in self._r if key in r.name.lower()]
        return matched, len(matched)


def install_mock(results):
    mod = types.ModuleType("everything")
    client_mod = types.ModuleType("everything.client")
    client_mod.EverythingClient = lambda *a, **k: FakeClient(results)
    sys.modules["everything"] = mod
    sys.modules["everything.client"] = client_mod


exe_dir, exe_name = os.path.split(REAL_EXE)
results = [
    FakeResult(exe_name, exe_dir, 300_000_000, ext="exe"),      # 冒充 vscode
    FakeResult("settings.yaml", r"C:\Users\me\Projects", 2048, ext="yaml"),
    FakeResult("notes.md", r"D:\Docs", 5120, ext="md"),
]
install_mock(results)

spec = importlib.util.spec_from_file_location("everything_search_tool", PLUGIN)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

# 绕过真实 DLL 探测 + 重置客户端缓存 + 屏蔽真实启动
tool._find_dll = lambda: r"C:\__fake__\Everything3_x64.dll"
tool._CLIENT = None
tool.os.startfile = lambda p: None

ok = 0
fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("PASS: " + label)
    else:
        fail += 1
        print("FAIL: " + label + ("  << " + str(extra) if extra else ""))


# 1) search 基本路径
r = tool.run(action="search", query=exe_name.split(".")[0], name_only=True, ext="exe", limit=5)
check("search ok", r.get("ok") is True, r)
check("search count==1", r.get("count") == 1, r)
check("search path correct",
      r.get("results") and r["results"][0]["path"].lower() == REAL_EXE.lower(), r)
check("search query_used has name/ext",
      "name:" in r.get("query_used", "") and "ext:exe" in r.get("query_used", ""), r.get("query_used"))

# 2) open 分支（指向真实存在的 exe，isfile 通过）
r2 = tool.run(action="open", query=exe_name)
check("open ok", r2.get("ok") is True, r2)
check("open action", r2.get("action") == "open", r2)
check("open path", (r2.get("opened") or "").lower() == REAL_EXE.lower(), r2)

# 3) 过滤参数 -> 查询语法翻译
r3 = tool.run(action="search", query="", ext="md", path="D:\\*",
              size_min=4096, size_max=65536,
              mdate_min="2024-01-01", mdate_max="2024-12-31", hidden=1)
q = r3.get("query_used", "")
check("query ext", "ext:md" in q, q)
check("query path", 'path:"D:\\"' in q, q)
check("query size", "size:>=4096" in q and "size:<=65536" in q, q)
check("query date", "dm:>=2024-01-01" in q and "dm:<=2024-12-31" in q, q)
check("query hidden=1", "attrib:H" in q, q)

# 4) hidden 默认排除隐藏
r4 = tool.run(action="search", query="x", ext="txt")
check("query hidden default excludes", "!attrib:H" in r4.get("query_used", ""), r4.get("query_used"))

# 5) size 传 0 视为不限（不应出现 size: 条件）
r5 = tool.run(action="search", query="x", ext="txt", size_min=0, size_max=0)
check("size 0 => no size filter", "size:" not in r5.get("query_used", ""), r5.get("query_used"))

# 6) 错误分支
check("empty query error", tool.run(action="search").get("ok") is False)
check("bad action error", tool.run(action="bad").get("ok") is False)
check("open no query error", tool.run(action="open").get("ok") is False)
r_notfound = tool.run(action="open", query="definitely_not_exist_xyz")
check("open not-found error", r_notfound.get("ok") is False, r_notfound)

# 7) 分页
r7 = tool.run(action="search", query="x", ext="txt", limit=1, offset=1)
check("pagination returns dict", r7.get("ok") is True, r7)

print("\nRESULT: {} passed, {} failed".format(ok, fail))
sys.exit(1 if fail else 0)