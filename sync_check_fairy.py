# -*- coding: utf-8 -*-
"""同步 fairy_ball 拓展（market 源）到运行时已安装目录并做静态校验。

源：market/extensions/fairy_ball（仓库内真源）
目标：%LOCALAPPDATA%/AgentCluster/extensions/fairy_ball（已安装目录）
     与其 _builtin 副本（重装源）—— 两处都同步保持一致
"""
import json
import os
import py_compile
import shutil

SRC = 'market/extensions/fairy_ball'
_EXT_ROOT = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'AgentCluster', 'extensions')
DST = os.path.join(_EXT_ROOT, 'fairy_ball')
BUILTIN = os.path.join(_EXT_ROOT, '_builtin', 'fairy_ball')
FILES = ['fairy.html', 'menu.html', 'backend.py', 'manifest.json', 'snip.html']

for base in (DST, BUILTIN):
    os.makedirs(os.path.join(base, 'assets'), exist_ok=True)
    for f in FILES:
        shutil.copy2(os.path.join(SRC, f), os.path.join(base, f))
    for g in os.listdir(os.path.join(SRC, 'assets')):
        shutil.copy2(os.path.join(SRC, 'assets', g), os.path.join(base, 'assets', g))
    print('synced ->', base)

py_compile.compile(os.path.join(DST, 'backend.py'), doraise=True)
print('backend.py 编译 OK')

for j in [os.path.join(SRC, 'manifest.json')]:
    json.load(open(j, encoding='utf-8'))
print('JSON OK')

for f in FILES:
    same = open(os.path.join(SRC, f), encoding='utf-8').read() == open(os.path.join(DST, f), encoding='utf-8').read()
    print('一致 %-14s: %s' % (f, same))

# —— fairy.html 关键逻辑检查（宝可梦版） ——
fh = open(os.path.join(DST, 'fairy.html'), encoding='utf-8').read()
checks_fairy = [
    ('右键改 IPC openMenu', "fairyAPI.openMenu && fairyAPI.openMenu(e.screenX, e.screenY)" in fh),
    ('storage 联动换装', "window.addEventListener('storage'" in fh and "fairy.skin" in fh),
    ('菜单 DOM 已移除', 'id="menu"' not in fh and 'mview' not in fh),
    ('穿透 hit-test 保留', 'elementFromPoint' in fh and 'setIgnoreMouse' in fh),
    ('宝可梦皮肤体系', 'data-skin="pikachu"' in fh and '/sprite/' in fh and 'SKIN_TPL' not in fh),
    ('旧皮肤已废除', 'stardust' not in fh and 'data-skin="flame"' not in fh and '--c1' not in fh),
    ('缩放层保留', 'data-size="s"' in fh and '.wrap' in fh),
]
for name, ok in checks_fairy:
    print(('✓ ' if ok else '✗ ') + name)

# —— menu.html 关键逻辑检查 ——
mh = open(os.path.join(DST, 'menu.html'), encoding='utf-8').read()
checks_menu = [
    ('三视图', 'data-v="main"' in mh and 'data-v="skin"' in mh and 'data-v="size"' in mh),
    ('写 localStorage', "localStorage.setItem('fairy.skin'" in mh and "localStorage.setItem('fairy.size'" in mh),
    ('尺寸上报', 'fairyAPI.menuSize' in mh),
    ('选完即关', 'fairyAPI.closeMenu' in mh),
    ('动作分发', 'fairyAPI.action' in mh),
    ('宝可梦小图预览', '/sprite/' in mh and '皮卡丘' in mh),
]
for name, ok in checks_menu:
    print(('✓ ' if ok else '✗ ') + name)

bad = [n for n, ok in checks_fairy + checks_menu if not ok]
print('RESULT:', 'FAIL ' + str(bad) if bad else 'ALL PASS')