"""统一的本地 JSON 存储 — v2：每域一个文件夹，每条记录一个 json

目录结构：
  AppData/
    settings.json                        ← 全局配置（单文件对象）
    agents/          1.json, 2.json ...  ← 每条智能体一个文件
    canvases/        1.json, 2.json ...  ← 画布（节点+连线）
    conversations/   49.json, 50.json ... ← 每次对话（含 messages）
    model_interfaces/ 1.json ...          ← 模型接口
    skills/                                ← 自定义技能目录（预留）
    plugins/                               ← 自定义插件目录（预留）
    _legacy/                              ← 旧版单文件数组迁移后的归档

迁移是安全的：init_store 时若检测到旧版 flat 文件，会全量读入 → 逐条写入新目录 →
移动旧文件到 _legacy/（永不删除，永不覆盖已迁移数据）。storage_version 写入 settings.json。

**可靠性保证（2026-09 加入；2026-10 fsync 改为可配置）**：
所有写入走「临时文件 + 原子替换」，任何时刻崩溃都不会留下半截 JSON（下次加载会报错并备份）。
fsync 策略由 AGENTCLUSTER_FSYNC 控制（见下方 FSYNC_MODE 注释），默认 async：
写请求立即返回，后台线程补刷到磁盘，断电最多丢最近不到 1s 的写入（该文件整体回退到
旧内容，不会损坏）；设为 sync 可恢复「先落盘再返回」的旧行为（本机 ~45ms/次，偏慢）。
"""
import atexit, json, os, queue, tempfile, threading, glob, time
from pathlib import Path
from copy import deepcopy

ROOT_DEFAULT = Path(__file__).resolve().parent.parent.parent / "AppData"
# 固定位置的自定义存储路径（独立于 ROOT，避免“存储位置存在存储位置里”的套娃）
_BOOTSTRAP_FILE = Path(__file__).resolve().parent.parent / ".runtime" / "storage_location"


def _resolve_root() -> Path:
    """启动时解析存储根：优先读 .runtime/storage_location，无效则回退项目 AppData。"""
    try:
        if _BOOTSTRAP_FILE.exists():
            custom = _BOOTSTRAP_FILE.read_text(encoding="utf-8").strip()
            if custom:
                p = Path(custom)
                if p.exists() and p.is_dir():
                    return p
    except Exception:
        pass
    return ROOT_DEFAULT


ROOT = _resolve_root()
LEGACY_DIR = ROOT / "_legacy"
LOCK = threading.RLock()

# 写盘 fsync 策略（可用环境变量 AGENTCLUSTER_FSYNC 覆盖）：
#   sync  - 旧版行为：每次写盘同步 fsync，数据先落盘再返回（本机实测 ~45ms/次）
#   async - 默认：原子替换照常执行（不会出现半截 JSON），fsync 交给后台线程队列，
#           写请求 ~2ms 返回；后台通常几 ms 内补刷。进程被杀不会丢数据（数据在 OS 缓存），
#           只有断电/系统崩溃才可能丢最近未刷完的写入，且原子替换保证旧文件仍完好。
#   off   - 完全不 fsync
FSYNC_MODE = os.getenv("AGENTCLUSTER_FSYNC", "async").strip().lower()
if FSYNC_MODE not in ("sync", "async", "off"):
    print(f"[store] AGENTCLUSTER_FSYNC={FSYNC_MODE!r} 无法识别，回退为 async")
    FSYNC_MODE = "async"

_sync_q: "queue.Queue" = queue.Queue()
_sync_thread: threading.Thread | None = None
_sync_init_lock = threading.Lock()


def _fsync_path(p: Path) -> None:
    """对已写完的文件补一次 fsync。后台线程调用，失败直接放弃（不影响主流程）。

    Windows 必须用 CreateFileW(FILE_SHARE_READ|WRITE|DELETE) 打开再 FlushFileBuffers：
    os.open() 的句柄不带 FILE_SHARE_DELETE，后台刷盘期间主线程对同一文件
    os.replace / unlink 会报 WinError 32（文件被占用），必须避免。
    POSIX 上用只读句柄即可 fsync，且读句柄不会阻塞 rename/unlink。"""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            # argtypes/restype 必须显式设置：不设置时 int 会按 32 位 C int 传递，
            # 64 位句柄被截断 → FlushFileBuffers 失败、CloseHandle 泄漏句柄
            CreateFileW = k32.CreateFileW
            CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            CreateFileW.restype = wintypes.HANDLE
            FlushFileBuffers = k32.FlushFileBuffers
            FlushFileBuffers.argtypes = [wintypes.HANDLE]
            FlushFileBuffers.restype = wintypes.BOOL
            CloseHandle = k32.CloseHandle
            CloseHandle.argtypes = [wintypes.HANDLE]
            CloseHandle.restype = wintypes.BOOL
            GENERIC_WRITE = 0x40000000
            SHARE_ALL = 0x07          # READ | WRITE | DELETE
            OPEN_EXISTING = 3
            h = CreateFileW(str(p), GENERIC_WRITE, SHARE_ALL, None, OPEN_EXISTING, 0, None)
            if not h or h == ctypes.c_void_p(-1).value:
                return                # 文件已被删 / 被独占占用 → 跳过这次补刷
            try:
                FlushFileBuffers(h)
            finally:
                CloseHandle(h)
        else:
            fd = os.open(str(p), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except Exception:
        pass


def _sync_worker_loop() -> None:
    while True:
        p = _sync_q.get()
        if p is None:
            return
        _fsync_path(p)


def _enqueue_fsync(p: Path) -> None:
    global _sync_thread
    with _sync_init_lock:
        if _sync_thread is None:
            _sync_thread = threading.Thread(target=_sync_worker_loop, name="store-fsync", daemon=True)
            _sync_thread.start()
            atexit.register(_drain_sync_queue)
    _sync_q.put(p)


def _drain_sync_queue() -> None:
    """进程退出前把队列里没刷完的文件刷掉（FIFO，正常几百 ms 内完成）。"""
    t = _sync_thread
    if t is None:
        return
    try:
        _sync_q.put(None)
        t.join(timeout=5)
    except Exception:
        pass

# 记录型域 → 目录下每条一个 id.json
RECORD_DOMAINS = ["agents", "canvases", "conversations", "model_interfaces", "rules", "param_presets"]
# 单文件域 → 整个域一个 json
SINGLE_DOMAINS = ["settings"]
# 预留的文件夹（用户可在这里放自定义 skills / plugins）
EXTRA_DIRS = ["skills", "plugins"]

DEFAULT_SETTINGS = {
    "storage_version": 2,
    "default_model_interface": None,
    "default_commands": ["file.read", "file.write", "file.delete", "file.structure", "cmd.run"],
    "default_skills": [],
    "default_plugins": [],
    "max_tool_rounds": 50,
    # 独立对话模式（无画布）下的默认输出目录；为空则回退到进程 CWD
    "default_workdir": "",
}


# ==============================================================
# 基础路径工具 + 原子写
# ==============================================================
def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _file_for(domain: str, record_id: int) -> Path:
    return ROOT / domain / f"{record_id}.json"


def _atomic_write_json(path: Path, payload) -> None:
    """原子写 JSON —— 任何时刻崩溃都不会留下半截文件。

    流程：同目录写 .tmp → 关闭 → os.replace（原子替换）。
    fsync 按 FSYNC_MODE 处理：sync = 关闭前同步 fsync（旧行为，写请求阻塞到落盘）；
    async = 替换后丢给后台线程补刷（写请求 ~2ms 返回）；off = 不 fsync。
    Windows 上 os.replace 会覆盖目标；跨盘失败会回退到 shutil.move + unlink。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    # 临时文件放在同目录，保证 os.replace 原子
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            if FSYNC_MODE == "sync":
                os.fsync(f.fileno())
        # 父目录 fsync（某些 OS 要求）
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass
        try:
            os.replace(tmp_path, path)
        except OSError:
            # 跨盘 / 文件被占用兜底
            import shutil
            shutil.move(tmp_path, path)
        if FSYNC_MODE == "async":
            _enqueue_fsync(path)
    except Exception:
        # 失败也要清理 tmp，防止目录堆积
        try: os.unlink(tmp_path)
        except Exception: pass
        raise


def _glob_record_files(domain: str) -> list[Path]:
    """返回域文件夹内所有数字命名的 json 文件，按 id 升序"""
    folder = ROOT / domain
    if not folder.exists():
        return []
    files = []
    for fp in folder.glob("*.json"):
        stem = fp.stem
        if stem.isdigit():
            files.append((int(stem), fp))
    files.sort(key=lambda x: x[0])
    return [fp for _, fp in files]


# ==============================================================
# 初始化 + 自动迁移
# ==============================================================
def init_store() -> None:
    with LOCK:
        print(f"[store] fsync mode = {FSYNC_MODE} (AGENTCLUSTER_FSYNC)")
        # 顶层目录
        ROOT.mkdir(parents=True, exist_ok=True)
        LEGACY_DIR.mkdir(parents=True, exist_ok=True)

        # 记录型域 → 创建文件夹
        for d in RECORD_DOMAINS:
            (ROOT / d).mkdir(parents=True, exist_ok=True)

        # 预留目录
        for d in EXTRA_DIRS:
            (ROOT / d).mkdir(parents=True, exist_ok=True)

        # settings.json：没有就建一个
        settings_file = ROOT / "settings.json"
        if not settings_file.exists():
            _atomic_write_json(settings_file, DEFAULT_SETTINGS)

        # 迁移检查
        _migrate_if_needed()


def _migrate_if_needed() -> None:
    """旧版单文件数组 → 新版目录 per-id 一条"""
    settings_file = ROOT / "settings.json"
    try:
        current = json.loads(settings_file.read_text(encoding="utf-8"))
    except Exception:
        current = {}

    version = current.get("storage_version", 1)
    if version >= 2:
        return  # 已是新版

    migrated_any = False
    for domain in RECORD_DOMAINS:
        old_file = ROOT / f"{domain}.json"
        if not old_file.exists():
            continue
        try:
            items = json.loads(old_file.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                continue
        except Exception:
            continue

        target_folder = ROOT / domain
        target_folder.mkdir(parents=True, exist_ok=True)
        already_ids = {p.stem for p in target_folder.glob("*.json")}
        for item in items:
            rid = item.get("id")
            if rid is None:
                continue
            # 已存在则不覆盖（幂等）
            if str(rid) in already_ids:
                continue
            target = target_folder / f"{rid}.json"
            _atomic_write_json(target, item)
        # 归档旧文件（移动到 _legacy，重命名避免冲突）
        dest = LEGACY_DIR / f"{domain}_v1_flat.json"
        # 如果 dest 已存在（多次启动），加时间戳后缀
        if dest.exists():
            import time
            dest = LEGACY_DIR / f"{domain}_v1_flat.{int(time.time())}.json"
        old_file.rename(dest)
        migrated_any = True

    # 处理 messages.json（旧版有、新版不再需要，也归档）
    old_msgs = ROOT / "messages.json"
    if old_msgs.exists():
        dest = LEGACY_DIR / "messages_v1_flat.json"
        if dest.exists():
            import time
            dest = LEGACY_DIR / f"messages_v1_flat.{int(time.time())}.json"
        old_msgs.rename(dest)
        migrated_any = True

    # 升级版本号
    current["storage_version"] = 2
    _atomic_write_json(settings_file, current)
    print(f"[store] storage migrated to v2: {migrated_any} domains archived to _legacy/")


# ==============================================================
# 读/写 API（签名保持不变，向后兼容）
# ==============================================================
def load(name: str):
    """加载整个域：settings → dict；agents/canvases/... → list[record]

    坏文件容错：解析失败的 json 重命名为 .corrupt-<ts>.bak 并跳过，
    防止一次崩溃留下的半截 JSON 污染整个域。"""
    with LOCK:
        if name == "settings":
            fp = ROOT / "settings.json"
            if not fp.exists():
                _atomic_write_json(fp, DEFAULT_SETTINGS)
            try:
                return json.loads(fp.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[store] WARN settings.json 损坏，重命名为 .bak 并用默认值重建: {e}")
                _backup_bad(fp)
                _atomic_write_json(fp, DEFAULT_SETTINGS)
                return deepcopy(DEFAULT_SETTINGS)

        if name in RECORD_DOMAINS:
            result = []
            for fp in _glob_record_files(name):
                try:
                    result.append(json.loads(fp.read_text(encoding="utf-8")))
                except Exception as e:
                    print(f"[store] WARN 读取 {fp} 失败：{e}，备份并跳过")
                    _backup_bad(fp)
            return result

        # 兼容：messages / 其他未知域（单文件模式，若不存在则返回空）
        fp = ROOT / f"{name}.json"
        if not fp.exists():
            return []
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[store] WARN {name}.json 损坏: {e}，备份并返回空")
            _backup_bad(fp)
            return []


def _backup_bad(path: Path) -> None:
    """把损坏文件挪到同目录 .corrupt-<ts>.bak 保留现场，不阻塞后续启动"""
    try:
        ts = int(time.time())
        bak = path.with_name(f"{path.name}.corrupt-{ts}.bak")
        path.rename(bak)
    except Exception:
        try: path.unlink(missing_ok=True)
        except Exception: pass


def save(name: str, data) -> None:
    """保存整个域（幂等）。
    - settings: 写一个 json
    - 记录型域：清空文件夹 + 逐条写入（慎用，一般用 upsert）
    """
    with LOCK:
        if name == "settings":
            fp = ROOT / "settings.json"
            fp.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(fp, data)
            return

        if name in RECORD_DOMAINS:
            folder = ROOT / name
            folder.mkdir(parents=True, exist_ok=True)
            # 先收集所有 id
            new_ids = set()
            if isinstance(data, list):
                for item in data:
                    rid = item.get("id")
                    if rid is None:
                        continue
                    _atomic_write_json(folder / f"{rid}.json", item)
                    new_ids.add(str(rid))
            # 删除不在新集合中的旧文件（安全：只删数字 id 的）
            for fp in folder.glob("*.json"):
                if fp.stem.isdigit() and fp.stem not in new_ids:
                    fp.unlink(missing_ok=True)
            return

        # 兼容单文件域
        fp = ROOT / f"{name}.json"
        _atomic_write_json(fp, data)


def upsert(name: str, item, key: str = "id"):
    """按 key 插入或更新一条记录。返回写入后的 item。"""
    with LOCK:
        if name in RECORD_DOMAINS:
            rid = item.get(key)
            if rid is None:
                raise ValueError(f"item['{key}'] is required for upsert on {name}")
            folder = ROOT / name
            folder.mkdir(parents=True, exist_ok=True)
            fp = folder / f"{rid}.json"
            _atomic_write_json(fp, item)
            return item

        # settings 合并写
        if name == "settings":
            cur = load("settings")
            if isinstance(item, dict):
                cur.update(item)
            else:
                cur = item
            save("settings", cur)
            return cur

        # 其他单文件域（兼容）
        items = load(name) or []
        found = False
        for i, x in enumerate(items):
            if x.get(key) == item.get(key):
                items[i] = item
                found = True
                break
        if not found:
            items.append(item)
        save(name, items)
        return item


def delete_by(name: str, value, key: str = "id") -> bool:
    """按 key 删除一条。返回 True 表示确实删了东西。"""
    with LOCK:
        if name in RECORD_DOMAINS:
            # key 通常是 id
            rid = value if key == "id" else None
            if rid is None:
                # 其他 key：要遍历文件找出匹配 id
                for fp in _glob_record_files(name):
                    try:
                        obj = json.loads(fp.read_text(encoding="utf-8"))
                        if obj.get(key) == value:
                            fp.unlink(missing_ok=True)
                            return True
                    except Exception:
                        continue
                return False
            fp = ROOT / name / f"{rid}.json"
            if fp.exists():
                fp.unlink()
                return True
            return False

        if name == "settings":
            cur = load("settings")
            if isinstance(cur, dict) and key in cur:
                cur.pop(key, None)
                save("settings", cur)
                return True
            return False

        # 其他单文件域（兼容）
        items = load(name) or []
        new = [x for x in items if x.get(key) != value]
        if len(new) == len(items):
            return False
        save(name, new)
        return True


def get_by(name: str, value, key: str = "id"):
    """按 key 查一条，返回 deepcopy。找不到返回 None。"""
    with LOCK:
        if name in RECORD_DOMAINS:
            if key == "id":
                fp = ROOT / name / f"{value}.json"
                if not fp.exists():
                    return None
                try:
                    return deepcopy(json.loads(fp.read_text(encoding="utf-8")))
                except Exception:
                    return None
            # 其他 key：遍历
            for fp in _glob_record_files(name):
                try:
                    obj = json.loads(fp.read_text(encoding="utf-8"))
                    if obj.get(key) == value:
                        return deepcopy(obj)
                except Exception:
                    continue
            return None

        if name == "settings":
            cur = load("settings")
            if isinstance(cur, dict):
                val = cur.get(key)
                return deepcopy(val) if val is not None else None
            return None

        # 其他单文件域（兼容）
        items = load(name) or []
        for x in items:
            if x.get(key) == value:
                return deepcopy(x)
        return None


def next_id(name: str) -> int:
    """下一个可用 id（最大值 + 1）。"""
    with LOCK:
        if name in RECORD_DOMAINS:
            max_id = 0
            for fp in _glob_record_files(name):
                try:
                    rid = int(fp.stem)
                    if rid > max_id:
                        max_id = rid
                except ValueError:
                    continue
            return max_id + 1

        # 单文件域兜底
        items = load(name) or []
        return max((x.get("id", 0) for x in items), default=0) + 1


# ==============================================================
# 列表接口的序列化缓存（2026-10 加入）
# ==============================================================
# 列表接口每次都要读+解析全部记录（实测 45 条 / 3.2MB ≈ 90ms），再交给 FastAPI
# 序列化（≈40ms）。这里按文件缓存 (mtime_ns, size) → (排序键, JSON 字节)：stat 校验
# 通过就直接复用（只 stat、不读盘、不解析），外部手工改文件也会因 mtime 变化自动失效。
_LIST_PAYLOAD_CACHE: dict[str, tuple[int, int, bytes, object]] = {}


def load_list_payloads(name: str, key_fn):
    """记录型域的列表快速读取，返回 [(sort_key, payload_bytes), ...]（按 id 升序）。

    payload_bytes 与 FastAPI JSONResponse 的序列化完全等价（紧凑分隔符、
    ensure_ascii=False、allow_nan=False），调用方直接拼成 JSON 数组返回即可。
    坏文件处理与 load() 一致：备份并跳过。
    """
    if name not in RECORD_DOMAINS:
        raise ValueError(f"load_list_payloads 只支持记录型域: {name}")
    with LOCK:
        entries = []
        seen: set = set()
        for fp in _glob_record_files(name):
            seen.add(fp.name)
            try:
                st = fp.stat()
            except OSError:
                continue
            hit = _LIST_PAYLOAD_CACHE.get(fp.name)
            if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
                entries.append((hit[3], hit[2]))
                continue
            try:
                obj = json.loads(fp.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[store] WARN 读取 {fp} 失败：{e}，备份并跳过")
                _backup_bad(fp)
                _LIST_PAYLOAD_CACHE.pop(fp.name, None)
                continue
            payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            _LIST_PAYLOAD_CACHE[fp.name] = (st.st_mtime_ns, st.st_size, payload, key_fn(obj))
            entries.append((key_fn(obj), payload))
        # 清掉已被删除记录的缓存
        for gone in set(_LIST_PAYLOAD_CACHE) - seen:
            _LIST_PAYLOAD_CACHE.pop(gone, None)
        return entries


# ==============================================================
# 辅助：给前端返回路径（用于 Settings 里"打开文件夹"）
# ==============================================================
def get_store_paths() -> dict:
    """返回所有数据位置的绝对路径。"""
    with LOCK:
        paths = {
            "root": str(ROOT),
            "settings_file": str(ROOT / "settings.json"),
            "agents_dir": str(ROOT / "agents"),
            "canvases_dir": str(ROOT / "canvases"),
            "conversations_dir": str(ROOT / "conversations"),
            "model_interfaces_dir": str(ROOT / "model_interfaces"),
            "skills_dir": str(ROOT / "skills"),
            "plugins_dir": str(ROOT / "plugins"),
            "param_presets_dir": str(ROOT / "param_presets"),
            "legacy_dir": str(LEGACY_DIR),
        }
        # 每个域的统计
        paths["stats"] = {}
        for d in RECORD_DOMAINS:
            paths["stats"][d] = len(_glob_record_files(d))
        # 自定义技能/插件：按子目录实际加载的文件数统计
        paths["stats"]["skills"] = _count_skill_dirs()
        paths["stats"]["plugins"] = _count_plugin_dirs()
        return paths


def set_root(new_path: str) -> dict:
    """切换存储根目录到新位置（不迁移旧数据），并让后续读写都走新位置。

    新位置会补建目录结构与 settings.json；若已有数据则原样读取。
    返回 get_store_paths() 的最新结果，供前端立即刷新展示。"""
    global ROOT, LEGACY_DIR
    p = Path(new_path).expanduser().resolve()
    if p == ROOT.resolve():
        return get_store_paths()
    with LOCK:
        p.mkdir(parents=True, exist_ok=True)
        if not p.is_dir():
            raise ValueError(f"不是有效目录：{new_path}")
        _BOOTSTRAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BOOTSTRAP_FILE.write_text(str(p), encoding="utf-8")
        ROOT = p
        LEGACY_DIR = ROOT / "_legacy"
        _LIST_PAYLOAD_CACHE.clear()
        init_store()
    return get_store_paths()


def _count_skill_dirs() -> int:
    """统计已生效的自定义技能数量（AppData/skills/*/SKILL.md）。"""
    folder = ROOT / "skills"
    if not folder.exists():
        return 0
    return sum(1 for sub in folder.iterdir()
               if sub.is_dir() and not sub.name.startswith(("_", ".")) and (sub / "SKILL.md").is_file())


def _count_plugin_dirs() -> int:
    """统计已生效的自定义插件数量（AppData/plugins/*/manifest.yaml）。"""
    folder = ROOT / "plugins"
    if not folder.exists():
        return 0
    return sum(1 for sub in folder.iterdir()
               if sub.is_dir() and not sub.name.startswith(("_", ".")) and (sub / "manifest.yaml").is_file())


def _list_explorer_hwnds() -> list:
    """枚举当前所有可见的资源管理器窗口句柄（CabinetWClass / ExploreWClass）。"""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    # argtypes/restype 必须显式设置：不设置时 HWND 会按 32 位 C int 传递，64 位指针被截断
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int

    hwnds: list = []

    @EnumWindowsProc
    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            if user32.GetClassNameW(hwnd, buf, 256):
                if buf.value in ("CabinetWClass", "ExploreWClass"):
                    hwnds.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return hwnds


def _raise_explorer_to_foreground(before: list) -> None:
    """把打开目录后新出现的资源管理器窗口拉到最前并置顶。

    Windows 有前台锁定：后台进程直接 SetForegroundWindow 会被拒绝，
    新窗口只会闪烁在任务栏。先模拟一次 Alt 按键重置空闲计时器，再置顶即可生效。"""
    if os.name != "nt":
        return
    import ctypes
    import time
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

    new_hwnd = None
    for _ in range(15):
        time.sleep(0.1)
        candidates = [h for h in _list_explorer_hwnds() if h not in before]
        if candidates:
            new_hwnd = candidates[-1]
            break

    if new_hwnd is None:
        existing = _list_explorer_hwnds()
        if not existing:
            return
        new_hwnd = existing[-1]

    VK_MENU = 0x12
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    SW_RESTORE = 9
    if user32.IsIconic(new_hwnd):
        user32.ShowWindow(new_hwnd, SW_RESTORE)
    user32.SetForegroundWindow(new_hwnd)


def open_folder(path: str) -> bool:
    """在资源管理器里打开指定目录；Windows 下尝试把窗口拉到最前。"""
    p = Path(path)
    if not p.exists():
        return False
    try:
        if os.name == "nt":
            before = _list_explorer_hwnds()
            os.startfile(str(p))  # type: ignore[attr-defined]
            _raise_explorer_to_foreground(before)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", str(p)])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(p)])
        return True
    except Exception as e:
        print(f"[store] open_folder 失败：{e}")
        return False


def pick_folder(initial: str = "") -> str:
    """弹出系统文件夹选择对话框，返回所选绝对路径（取消则返回空串）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        selected = filedialog.askdirectory(
            initialdir=initial or None, title="选择数据存储位置")
        root.destroy()
        return selected or ""
    except Exception as e:
        print(f"[store] pick_folder 失败：{e}")
        return ""


import sys

