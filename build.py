# -*- coding: utf-8 -*-
"""打包脚本：生成图标 + 用 PyInstaller 打出单文件 exe。

    python build.py            生成图标并打包
    python build.py --icon     只生成图标
    python build.py --verify   只验证 dist 里的 exe
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor"))
sys.path.insert(0, HERE)

ICON = os.path.join(HERE, "app.ico")
DIST = os.path.join(HERE, "dist")
SIZES = [16, 24, 32, 48, 64, 128, 256]


def _png_bytes(rgba) -> bytes:
    """把 (h,w,4) uint8 数组编码成 PNG（纯 Python，不依赖 Qt/PIL）。"""
    import zlib

    h, w = rgba.shape[:2]
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgba[y].tobytes()

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _icon_rgba(size: int):
    """画一个圆角深色底 + 绿色波形 + 红色录音点的图标。"""
    import math

    import numpy as np

    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    cx = cy = (size - 1) / 2.0
    half = size / 2.0
    radius = size * 0.20
    qx = np.abs(xx - cx) - (half - radius - size * 0.03)
    qy = np.abs(yy - cy) - (half - radius - size * 0.03)
    dist = (np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2)
            + np.minimum(np.maximum(qx, qy), 0) - radius)
    inside = np.clip(0.5 - dist, 0.0, 1.0)          # 抗锯齿覆盖度

    # 背景渐变
    t = (yy / max(1.0, size - 1)).reshape(size, size, 1)
    top = np.array([0x21, 0x33, 0x40], np.float64)
    bot = np.array([0x0a, 0x10, 0x15], np.float64)
    rgb = (top * (1 - t) + bot * t)

    # 波形
    mid = size * 0.52
    amp = size * 0.30
    thick = max(1.3, size / 16.0)
    x0, x1 = size * 0.13, size * 0.87
    wmask = np.zeros((size, size), np.float64)
    for x in range(size):
        if x < x0 - thick or x > x1 + thick:
            continue
        xn = min(1.0, max(0.0, (x - x0) / max(1e-6, (x1 - x0))))
        env = math.sin(math.pi * xn) ** 0.7
        yw = mid - amp * env * math.sin(2 * math.pi * 3.5 * xn)
        d = np.abs(yy[:, x] - yw)
        wmask[:, x] = np.clip((thick / 2.0 + 0.5) - d, 0.0, 1.0)
    green = np.array([0x37, 0xd6, 0xa0], np.float64)
    rgb = rgb * (1 - wmask.reshape(size, size, 1)) + green * wmask.reshape(size, size, 1)

    # 录音红点
    rad = size * 0.115
    dcx, dcy = size * 0.785, size * 0.215
    d = np.sqrt((xx - dcx) ** 2 + (yy - dcy) ** 2)
    dmask = np.clip((rad + 0.5) - d, 0.0, 1.0)
    red = np.array([0xff, 0x4d, 0x4d], np.float64)
    rgb = rgb * (1 - dmask.reshape(size, size, 1)) + red * dmask.reshape(size, size, 1)

    out = np.zeros((size, size, 4), np.uint8)
    out[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    out[:, :, 3] = (inside * 255).astype(np.uint8)
    return out


def build_icon() -> str:
    pngs = [_png_bytes(_icon_rgba(s)) for s in SIZES]

    # 组装 ICO（Vista+ 支持 PNG 负载）
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = b""
    for size, data in zip(SIZES, pngs):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    with open(ICON, "wb") as f:
        f.write(header + entries + b"".join(pngs))
    print("icon -> %s (%d bytes)" % (ICON, os.path.getsize(ICON)))
    return ICON


RUNNER_SRC = '''# -*- coding: utf-8 -*-
"""PyInstaller 启动器（由 build.py 自动生成）。

PyInstaller 6 会为“隔离执行”创建带管道的子进程；在某些受限环境里管道会被拒绝。
这里在管道创建失败时自动回退为进程内执行（结果等价，只是少了隔离）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "vendor"))


def patch_isolated():
    try:
        from PyInstaller import isolated as iso_pkg
        from PyInstaller.isolated import _parent
    except Exception:
        return
    orig_call = _parent.call

    def clear_qt_singleton():
        """隔离调用本该在干净进程里跑；进程内回退时先清掉上一次残留的
        QCoreApplication，否则 Qt 会抛 “Please destroy the QCoreApplication
        singleton before creating a new QCoreApplication instance”。"""
        try:
            from PySide6.QtCore import QCoreApplication
        except Exception:
            return
        inst = QCoreApplication.instance()
        if inst is None:
            return
        try:
            import shiboken6
            shiboken6.delete(inst)
        except Exception:
            pass

    def safe_call(function, *args, **kwargs):
        try:
            return orig_call(function, *args, **kwargs)
        except (PermissionError, OSError) as exc:
            print("[luyin] isolated pipe unavailable (%s); running %s in-process"
                  % (exc, getattr(function, "__name__", function)), flush=True)
            clear_qt_singleton()
            return function(*args, **kwargs)

    _parent.call = safe_call
    if hasattr(iso_pkg, "call"):
        iso_pkg.call = safe_call

    real_python = getattr(_parent, "Python", None)
    if real_python is not None:
        class FallbackPython:
            def __init__(self, *a, **k):
                self._inner = None

            def __enter__(self):
                try:
                    inner = real_python()
                    inner.__enter__()
                    self._inner = inner
                    return inner
                except (PermissionError, OSError) as exc:
                    print("[luyin] isolated Python unavailable (%s); in-process" % exc,
                          flush=True)
                    self._inner = None
                    return self

            def call(self, function, *args, **kwargs):
                clear_qt_singleton()
                return function(*args, **kwargs)

            def __exit__(self, *exc):
                if self._inner is not None:
                    return self._inner.__exit__(*exc)
                return False

        _parent.Python = FallbackPython
        iso_pkg.Python = FallbackPython


patch_isolated()

import PyInstaller.__main__ as pyi_main  # noqa: E402

pyi_main.run(sys.argv[1:])
'''


def _qt_search_dirs() -> list:
    """conda 版 PySide6 的 Qt6*.dll / shiboken 运行库在 Library\\bin 里，
    而它并不一定在 PATH 上；PyInstaller 解析二进制依赖时需要能找到它们。"""
    dirs = []
    prefixes = [sys.prefix, getattr(sys, "base_prefix", sys.prefix)]
    for prefix in prefixes:
        for cand in (os.path.join(prefix, "Library", "bin"),
                     os.path.join(prefix, "Lib", "site-packages", "shiboken6"),
                     os.path.join(prefix, "Lib", "site-packages", "PySide6")):
            if os.path.isdir(cand) and cand not in dirs:
                dirs.append(cand)
    return dirs


def build_exe(console: bool = False, name: str = "LuyinStudio", onefile: bool = True) -> int:
    env = dict(os.environ)
    tmp = os.path.join(HERE, "_tmp")
    os.makedirs(tmp, exist_ok=True)
    env["TEMP"] = tmp
    env["TMP"] = tmp
    env["PYTHONIOENCODING"] = "utf-8"
    qt_dirs = _qt_search_dirs()
    if qt_dirs:
        env["PATH"] = os.pathsep.join(qt_dirs + [env.get("PATH", "")])
        print("Qt 搜索路径: %s" % " ; ".join(qt_dirs))
    args = [
        "--noconfirm", "--clean",
        "--onefile" if onefile else "--onedir",
        "--console" if console else "--windowed",
        "--name", name,
        "--icon", ICON,
        "--paths", os.path.join(HERE, "vendor"),
        "--add-data", "%s%svendor" % (os.path.join(HERE, "vendor"), os.pathsep),
        "--hidden-import", "pyaudiowpatch",
        "--hidden-import", "lameenc",
        "--hidden-import", "miniaudio",
        "--hidden-import", "numpy",
        "--hidden-import", "cffi",
        "--hidden-import", "_cffi_backend",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PyQt6",
        "--exclude-module", "IPython",
        "--exclude-module", "pandas",
        "--exclude-module", "PIL",
        "--distpath", DIST,
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", os.path.join(HERE, "build"),
        os.path.join(HERE, "main.py"),
    ]
    runner = os.path.join(HERE, "build", "_pyi_runner.py")
    os.makedirs(os.path.dirname(runner), exist_ok=True)
    with open(runner, "w", encoding="utf-8") as f:
        f.write(RUNNER_SRC)
    log = os.path.join(HERE, "_build.log")
    with open(log, "w", encoding="utf-8", errors="replace") as f:
        f.write("CMD: python %s %s\n\n" % (runner, " ".join(args)))
        f.flush()
        t0 = time.time()
        proc = subprocess.run([sys.executable, runner] + args, cwd=HERE, env=env,
                              stdout=f, stderr=subprocess.STDOUT)
        f.write("\nEXIT=%d  用时 %.1f 秒\n" % (proc.returncode, time.time() - t0))
    print("pyinstaller exit=%d (log: %s)" % (proc.returncode, log))
    return proc.returncode


def verify(exe_name: str = "LuyinStudio.exe", label: str = "exe") -> int:
    exe = os.path.join(DIST, exe_name)
    if not os.path.isfile(exe):
        print("缺少 %s" % exe)
        return 1
    print("%s 大小 %.1f MB" % (label, os.path.getsize(exe) / 1024 / 1024))
    candidates = [os.path.join(DIST, "_selftest.txt"), os.path.join(HERE, "_selftest.txt")]
    for p in candidates:
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    env = dict(os.environ)
    tmp = os.path.join(HERE, "_tmp")
    os.makedirs(tmp, exist_ok=True)
    env["TEMP"] = tmp
    env["TMP"] = tmp
    t0 = time.time()
    proc = subprocess.run([exe, "--selftest"], cwd=HERE, env=env, timeout=900)
    text = None
    for p in candidates:
        if os.path.exists(p):
            text = open(p, encoding="utf-8").read()
            break
    print("%s 自检 exit=%d 用时 %.1f 秒\n%s"
          % (label, proc.returncode, time.time() - t0, text or "(没有自检输出)"))
    return 0 if (proc.returncode == 0 and text and "RESULT=OK" in text) else 1


def build_debug(onefile: bool = True) -> int:
    """打一个带控制台的调试版，用来定位 exe 启动失败原因。"""
    name = "LuyinStudioDebug" if onefile else "LuyinStudioDir"
    code = build_exe(console=True, name=name, onefile=onefile)
    if code != 0:
        return code
    exe = os.path.join(DIST, name + ".exe") if onefile else os.path.join(DIST, name, name + ".exe")
    log = os.path.join(HERE, "_debug_run.log")
    env = dict(os.environ)
    env["TEMP"] = os.path.join(HERE, "_tmp")
    env["TMP"] = env["TEMP"]
    with open(log, "w", encoding="utf-8", errors="replace") as f:
        proc = subprocess.run([exe, "--selftest"], cwd=HERE, env=env, stdout=f,
                              stderr=subprocess.STDOUT, timeout=900)
        f.write("\nEXIT=%d\n" % proc.returncode)
    print(open(log, encoding="utf-8", errors="replace").read()[-6000:])
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--verify":
        return verify()
    build_icon()
    if mode == "--icon":
        return 0
    if mode == "--debug":
        return build_debug(True)
    if mode == "--debug-dir":
        return build_debug(False)
    code = build_exe()
    if code != 0:
        return code
    return verify()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
