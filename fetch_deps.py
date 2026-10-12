"""fetch_deps.py - 下载并解包运行本程序所需的三个二进制依赖到 vendor/。

本仓库不直接收录这些第三方二进制（详见 README「第三方与许可」），首次运行前执行一次：

    python fetch_deps.py

会从 PyPI（默认清华镜像，可用 --index 换）下载与本机 Python 版本**及平台**匹配的 wheel，
解包到 vendor/，最后真正 import 一次以确认可用。这些都是预编译好的二进制轮子，
不需要任何编译工具。若 vendor/ 被污染或想换版本，用 --clean --force 重来。
"""
import argparse
import io
import os
import platform
import re
import shutil
import sys
import urllib.parse
import urllib.request
import zipfile

for _stream in (sys.stdout, sys.stderr):
    try:    # Windows 控制台与 CI runner 的默认编码不一定是 UTF-8，直接 print 中文会崩
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor")
UA = "pip/24.0"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
PYPI = "https://pypi.org/simple"

# 依赖清单：
#   marks - 解包后必须存在的文件（%(tag)s -> cp312，%(plat)s -> win_amd64）
#   pin   - 本程序验证过的版本；索引里没有该版本时退回最新可用版本
DEPS = {
    "pyaudiowpatch": {"marks": ["pyaudiowpatch/__init__.py"], "pin": "0.2.12.8"},
    "miniaudio": {"marks": ["miniaudio.py", "_miniaudio%(ext)s"], "pin": "1.71"},
    "lameenc": {"marks": ["lameenc.cp%(tag)s-%(plat)s%(ext)s"], "pin": "1.8.4"},
}


def py_tag():
    """当前 Python 的 wheel ABI 标签，如 cp312。"""
    return "cp%d%d" % (sys.version_info[0], sys.version_info[1])


def platform_tag():
    m = platform.machine().lower()
    return "win_amd64" if m in ("amd64", "x86_64") else m


def ext_tag():
    """本机二进制扩展名（Windows 上是 .pyd，其他平台 .so）。"""
    return ".pyd" if os.name == "nt" else ".so"


def marks_of(name):
    return [m % {"tag": py_tag(), "plat": platform_tag(), "ext": ext_tag()}
            for m in DEPS[name]["marks"]]


def fetch(url, timeout=30):
    # 清华 CDN 会对浏览器 UA 返回 403，必须用 pip 的 UA
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def index_urls(name, index):
    """返回 simple 索引页里该包所有 wheel 的绝对 URL（索引里的 href 是相对路径）。"""
    page = fetch("%s/%s/" % (index.rstrip("/"), name)).decode("utf-8", "replace")
    urls = []
    for part in page.split('href="')[1:]:
        href = part.split('"')[0].split("#")[0]
        if href.endswith(".whl"):
            urls.append(urllib.parse.urljoin(index, href))
    return urls


def _ver_key(ver):
    """把版本号变成可比较的元组，如 1.8.4 -> (1, 8, 4)。"""
    out = []
    for part in re.split(r"[._\-+]", ver):
        m = re.match(r"(\d+)", part)
        out.append(int(m.group(1)) if m else -1)
    return tuple(out)


def wheel_version(fname, safe):
    """从文件名里取出包名后面的版本号：lameenc-1.8.4-cp312-...whl -> 1.8.4"""
    return fname[len(safe) + 1:].split("-")[0]


def pick_wheel(urls, name, want=None, tag=None):
    """挑选本机可用的 wheel（必须 ABI 与平台都匹配，否则会装出跑不起来的二进制）。

    优先 want 指定的版本，其次取版本号最高的；找不到返回 None。
    """
    tag = tag or py_tag()
    plat = platform_tag()
    safe = name.replace("-", "_").lower()
    cands = []
    for u in urls:
        fname = os.path.basename(u).lower()
        if not fname.startswith(safe + "-") or not fname.endswith(".whl"):
            continue
        # 平台必须完全对得上：win_amd64 的机器绝不能装 win32 / macosx / manylinux
        if tag not in fname:
            continue
        if plat in fname:
            prio = 0
        elif "py3-none-any" in fname:
            prio = 1
        else:
            continue
        ver = wheel_version(fname, safe)
        cands.append((prio, _ver_key(ver), ver, u))
    if not cands:
        return None
    if want:
        for c in cands:
            if c[2] == want.lower():
                return c[3]
        print("  索引里没有验证过的版本 %s，改用最新可用版本" % want)
    best = max(cands, key=lambda c: (c[1], -c[0]))
    return best[3]


def dep_ready(name):
    return all(os.path.exists(os.path.join(VENDOR, m)) for m in marks_of(name))


def unpack(wheel_bytes, name):
    """解包 wheel：先清掉同名包上一版的 dist-info，避免目录里留下版本混乱的残留。"""
    os.makedirs(VENDOR, exist_ok=True)
    prefix = name.replace("-", "_").lower().split("_")[0][:6]
    for entry in os.listdir(VENDOR):
        low = entry.lower()
        if low.endswith(".dist-info") and low.startswith(prefix):
            shutil.rmtree(os.path.join(VENDOR, entry), ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as z:
        z.extractall(VENDOR)


def check_imports():
    """真 import 一次，确认解出来的二进制与纯 Python 部分版本一致、能加载。"""
    if VENDOR not in sys.path:
        sys.path.insert(0, VENDOR)
    ok = True
    for mod in ("pyaudiowpatch", "miniaudio", "lameenc"):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "")
            print("  [可用] %-14s %s" % (mod, ver))
        except Exception as e:
            print("  [不可用] %-14s %s: %s" % (mod, type(e).__name__, e))
            ok = False
    return ok


def main():
    ap = argparse.ArgumentParser(description="下载 vendor/ 里的二进制依赖")
    ap.add_argument("--index", default=MIRROR, help="PyPI 索引地址（默认清华镜像）")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    ap.add_argument("--clean", action="store_true", help="先删掉整个 vendor/ 再下载")
    ap.add_argument("--check", action="store_true", help="只检查 vendor/ 是否可用，不下载")
    args = ap.parse_args()

    print("Python %s / %s %s" % (sys.version.split()[0], platform.system(), platform.machine()))
    print("目标目录：%s" % VENDOR)

    if args.check:
        print()
        ok = check_imports()
        print("\n%s" % ("依赖可用。" if ok else "依赖不可用，请运行：python fetch_deps.py --clean --force"))
        return 0 if ok else 1

    if args.clean and os.path.isdir(VENDOR):
        shutil.rmtree(VENDOR, ignore_errors=True)
        print("已清空 vendor/")
    print("索引：%s\n" % args.index)

    failed = []
    for name in ("pyaudiowpatch", "miniaudio", "lameenc"):
        spec = DEPS[name]
        if dep_ready(name) and not args.force:
            print("[跳过] %s 已就绪" % name)
            continue
        print("[下载] %s ..." % name)
        try:
            urls = index_urls(name, args.index)
        except Exception as e:
            print("  索引访问失败：%s" % e)
            failed.append(name)
            continue
        url = pick_wheel(urls, name, want=spec["pin"])
        if not url:
            print("  没找到匹配的 wheel（需要 %s / %s）" % (py_tag(), platform_tag()))
            failed.append(name)
            continue
        fname = os.path.basename(url)
        got = wheel_version(fname.lower(), name.replace("-", "_").lower())
        if got != spec["pin"]:
            print("  注意：本程序验证过的版本是 %s，本次用的是 %s" % (spec["pin"], got))
        print("  %s" % fname)
        try:
            unpack(fetch(url, timeout=180), name)
            print("  已解包")
        except Exception as e:
            print("  下载/解包失败：%s" % e)
            failed.append(name)

    print("\n检查依赖能否导入：")
    imports_ok = check_imports()

    print()
    if failed or not imports_ok:
        print("依赖未能就绪%s" % ("：" + ", ".join(failed) if failed else ""))
        print("可换官方源重试：python fetch_deps.py --clean --force --index %s" % PYPI)
        return 1
    print("全部依赖就绪，可以运行：python main.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
