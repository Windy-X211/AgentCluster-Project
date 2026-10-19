import ast
import pathlib
import sys

stdlib = set(sys.stdlib_module_names)

mods = {}
files = list(pathlib.Path('backend').rglob('*.py')) + list(pathlib.Path('.').glob('*.py'))
# 排除 vendored 目录
files = [p for p in files if 'mcp_vendor' not in str(p)]
for p in files:
    try:
        tree = ast.parse(p.read_text(encoding='utf-8', errors='ignore'))
    except Exception:
        continue
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                top = a.name.split('.')[0]
                if top not in stdlib:
                    mods.setdefault(top, set()).add(str(p))
        elif isinstance(n, ast.ImportFrom) and n.module:
            top = n.module.split('.')[0]
            if top not in stdlib:
                mods.setdefault(top, set()).add(str(p))

local = {'core', 'api', 'plugins', 'skills', 'utils', 'mcp_vendor', 'plugins_debug'}
print("=== 第三方库 -> 使用文件 ===")
for m in sorted(mods):
    if m in local:
        continue
    files_list = '; '.join(sorted(mods[m]))
    print(f"{m}: {files_list[:300]}")