import os
import sys
import shutil

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_modules")

DIR_NAMES = {
    "test", "tests", "__tests__", "__test__", "example", "examples",
    "demo", "demos", "sample", "samples", "docs", "doc",
    "benchmark", "benchmarks", "coverage", ".github", ".vscode",
    ".idea", ".circleci", "test262", ".travis",
}

FILE_PATTERNS = [
    "*.md", "*.markdown", "*.map", ".npmignore", ".eslintrc*",
    ".editorconfig", ".travis.yml", ".babelrc*", "karma.conf.js",
    "jest.config.js", ".nycrc*", ".prettierrc*", "tsconfig.json",
    "tsconfig.*.json", ".appveyor.yml", "appveyor.yml", "*.flow",
    ".flowconfig", ".eslintignore", "*.d.ts.map", "*.js.map",
    "*.d.mts.map", "*.cjs.map", "*.mjs.map",
]

KEEP_NAMES = {"license", "licence", "notice", "authors", "package.json"}


def should_delete_dir(name):
    return name.lower() in DIR_NAMES


def should_delete_file(name, rel_depth_at_least_pkg):
    low = name.lower()
    base = low.split(".")[0]
    if base in KEEP_NAMES or low in KEEP_NAMES:
        return False
    if low.endswith(".d.ts") or low.endswith(".d.mts") or low.endswith(".d.cts"):
        return False
    import fnmatch
    for pat in FILE_PATTERNS:
        if fnmatch.fnmatch(low, pat):
            return True
    return False


def main():
    apply = "--apply" in sys.argv
    to_delete = []
    total_size = 0
    file_count = 0

    for dirpath, dirnames, filenames in os.walk(ROOT, topdown=True):
        rel = os.path.relpath(dirpath, ROOT)
        depth = 0 if rel == "." else rel.count(os.sep) + 1

        kept_dirs = []
        for d in dirnames:
            if depth >= 1 and should_delete_dir(d):
                p = os.path.join(dirpath, d)
                sz = 0
                cnt = 0
                for r, _, fs in os.walk(p):
                    for f in fs:
                        fp = os.path.join(r, f)
                        try:
                            sz += os.path.getsize(fp)
                            cnt += 1
                        except OSError:
                            pass
                to_delete.append((p, sz, cnt))
                total_size += sz
                file_count += cnt
            else:
                kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for f in filenames:
            if should_delete_file(f, depth >= 1):
                p = os.path.join(dirpath, f)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = 0
                to_delete.append((p, sz, 1))
                total_size += sz
                file_count += 1

    print("模式: %s" % ("APPLY" if apply else "DRY-RUN"))
    print("待删除条目: %d 个文件, 约 %.2f MB" % (file_count, total_size / 1024 / 1024))
    top = sorted(to_delete, key=lambda x: -x[1])[:20]
    print("--- 体积最大的 20 项 ---")
    for p, sz, cnt in top:
        print("%8.1f KB  (%d files)  %s" % (sz / 1024, cnt, os.path.relpath(p, ROOT)))

    if apply:
        err = 0
        for p, _, _ in to_delete:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
            except OSError:
                err += 1
        print("已删除, 失败 %d 项" % err)


if __name__ == "__main__":
    main()