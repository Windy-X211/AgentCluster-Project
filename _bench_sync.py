"""实测：镜像 race 下载 index.json 的速度。"""
import sys, os, time, json
from pathlib import Path
from urllib.request import urlopen, Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))
from core import market_sync

cfg = market_sync.get_config()
url = market_sync.raw_index_url()
print(f'目标: {url}')
print(f'镜像数: {len(cfg["mirrors"])}')
print()

candidates = market_sync.mirror_candidates(url, cfg["mirrors"])
print(f'候选地址 ({len(candidates)}):')
for i, c in enumerate(candidates):
    print(f'  [{i}] {c}')
print()

# 直接对每个候选测速（不 race，逐个试）
print('=== 逐个测速（每个 30s 超时）===')
for i, c in enumerate(candidates):
    t0 = time.monotonic()
    try:
        req = Request(c, headers={'User-Agent': market_sync._USER_AGENT})
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
        dt = time.monotonic() - t0
        size = len(data)
        # 检查是不是 index.json（看是否是合法 JSON 且有 items 字段）
        try:
            j = json.loads(data)
            items_count = len(j.get('items', [])) if isinstance(j, dict) else -1
            print(f'  [{i}] ✅ {dt:6.2f}s  {size:>8} bytes  items={items_count}  ← {c[:60]}')
        except:
            print(f'  [{i}] ⚠️  {dt:6.2f}s  {size:>8} bytes  非 JSON/无效  ← {c[:60]}')
    except Exception as e:
        dt = time.monotonic() - t0
        print(f'  [{i}] ❌ {dt:6.2f}s  {type(e).__name__}: {str(e)[:60]}  ← {c[:60]}')
print()

# 测当前 race 总耗时
print('=== 并发 race 实测 ===')
t0 = time.monotonic()
try:
    import tempfile
    tmpdir = Path(tempfile.mkdtemp())
    dest = tmpdir / 'index.json'
    result = market_sync.download_file(url, dest, mirrors=cfg["mirrors"], timeout=120)
    dt = time.monotonic() - t0
    print(f'  race 完成: {dt:.2f}s  winner={result.get("mirror")}  size={result.get("size")}')
except Exception as e:
    dt = time.monotonic() - t0
    print(f'  race 失败: {dt:.2f}s  {e}')
