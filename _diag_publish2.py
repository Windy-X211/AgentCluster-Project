import subprocess, os, json

MARKET = os.path.join(os.path.dirname(__file__), '..', 'market')

def git(*args):
    return subprocess.run(['git', '-C', MARKET, *args],
                          capture_output=True, text=True)

print('=== publish commit 修改了哪些文件 (name-status) ===')
r = git('show', '--name-status', '--format=fuller', '72edde5')
print(r.stdout)

print('\n=== publish commit 的父 commit 里 index.json ===')
r = git('show', '72edde5^:index.json')
parent_idx = json.loads(r.stdout)
print(f'  父 commit index.json: {len(parent_idx.get("items", []))} 条')

print('\n=== publish commit 里 index.json ===')
r = git('show', '72edde5:index.json')
pub_idx = json.loads(r.stdout)
print(f'  publish commit index.json: {len(pub_idx.get("items", []))} 条')

print('\n=== 两次 index.json 完全相同? ===')
print(f'  父 == publish: {parent_idx == pub_idx}')

# 如果相同，那说明 push_market 执行时 index.json 没被加进去
# 让我模拟：先手动写入 index.json 条目，再 git add -A，看看 commit 里有没有
print('\n' + '='*60)
print('=== 模拟 publish 流程，看 index.json 会不会丢 ===')
print('='*60)

# 先 stash 干净（当前 HEAD 的 index.json 是空的，工作区也干净）
# 模拟：publish 写 index.json
now = subprocess.run(['powershell', '-Command', 'Get-Date -Format "yyyy-MM-ddTHH:mm:ss"'],
                     capture_output=True, text=True).stdout.strip()
test_item = {
    "kind": "extension", "id": "test-item", "name": "测试条目",
    "author": "Windy-X211", "dir": "extensions/test-item", "file": "manifest.json",
    "version": "1.0.0", "updated_at": now,
}

# 1. 手动模拟 _save_item 的操作
idx_path = os.path.join(MARKET, 'index.json')
idx = json.load(open(idx_path, encoding='utf-8'))
idx['items'].append(test_item)
idx['updated_at'] = now
with open(idx_path, 'w', encoding='utf-8') as f:
    json.dump(idx, f, ensure_ascii=False, indent=2)

print(f'1. 手动写入后 index.json: {len(idx["items"])} 条')

# 2. 检查 git 能不能看到这个改动
r = git('status', '--short')
print(f'2. git status: {r.stdout.strip() or "(无变更)"}')

r = git('diff', '--stat', 'HEAD')
print(f'3. git diff --stat HEAD: {r.stdout.strip() or "(无 diff)"}')

# 3. git add -A
git('add', '-A')
r = git('status', '--short')
print(f'4. git add -A 后 status: {r.stdout.strip() or "(无变更)"}')

# 4. commit
git('commit', '-m', 'test: verify index.json gets committed')
r = git('show', '--name-status', '--format=oneline', 'HEAD')
print(f'5. 新 commit 包含: {r.stdout.strip()}')

# 5. 检查新 commit 里 index.json
r = git('show', 'HEAD:index.json')
new_idx = json.loads(r.stdout)
print(f'6. 新 commit index.json: {len(new_idx.get("items", []))} 条')
print(f'   包含 test-item: {any(i.get("id") == "test-item" for i in new_idx.get("items", []))}')

# 6. reset 回去
git('reset', '--hard', 'HEAD~1')
print(f'\n7. 已 reset 回 publish commit')

# 清理临时写入
print('\n=== 结论 ===')
if len(new_idx.get("items", [])) == 1:
    print('  ✅ 手动流程 index.json 能正常提交')
    print('  ❌ 说明 push_market 里 git add 之前 index.json 就已经被覆盖掉了')
else:
    print('  ❌ 即使手动 git add 也提交不了 index.json')
