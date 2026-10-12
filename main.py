# -*- coding: utf-8 -*-
"""Luyin Studio 录音工作室 —— 程序入口。

用法：
    python main.py              启动图形界面
    python main.py --selftest   无界面自检（打包验证用）
"""
from __future__ import annotations

import os
import sys


def _prepare_paths():
    """把内置的第三方库目录（vendor）加入搜索路径。

    打包成 exe 后，vendor 会随 PyInstaller 解包到 sys._MEIPASS。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (getattr(sys, "_MEIPASS", None), here):
        if not base:
            continue
        vendor = os.path.join(base, "vendor")
        if os.path.isdir(vendor) and vendor not in sys.path:
            sys.path.insert(0, vendor)


_prepare_paths()

import numpy as np  # noqa: E402
from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from luyin import APP_NAME, __version__  # noqa: E402
from luyin import engine  # noqa: E402
from luyin.engine import AudioError, AudioSystem, CaptureEngine, PlaybackEngine  # noqa: E402
from luyin.mainwindow import MainWindow  # noqa: E402


def make_icon() -> QIcon:
    """用代码画一个波形图标，免去外部资源文件。"""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(QColor("#101820"))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor("#37d6a0"), max(1.0, size / 16.0)))
        mid = size / 2.0
        n = 16
        import math
        pts = []
        for i in range(n + 1):
            x = i / n * size
            amp = math.sin(i * 0.9) * math.sin(i * 0.31) * 0.42 * size
            pts.append((x, mid - amp))
        for i in range(len(pts) - 1):
            p.drawLine(int(pts[i][0]), int(pts[i][1]), int(pts[i + 1][0]), int(pts[i + 1][1]))
        p.end()
        icon.addPixmap(pm)
    return icon


def build_app() -> QApplication:
    app = QApplication(sys.argv)
    app.setApplicationName("LuyinStudio")
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("LuyinStudio")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(make_icon())
    app.setStyleSheet("""
        QMainWindow, QWidget { background: #151b21; color: #cfdde6; }
        QMenuBar, QMenu { background: #182028; color: #cfdde6; }
        QMenuBar::item:selected, QMenu::item:selected { background: #2a3a46; }
        QToolBar { background: #182028; border: 0; spacing: 4px; padding: 3px; }
        QToolButton { color: #cfdde6; padding: 4px 8px; border-radius: 4px; }
        QToolButton:hover { background: #26343e; }
        QToolButton:disabled { color: #5c6b76; }
        QGroupBox { border: 1px solid #26343e; border-radius: 5px;
                    margin-top: 10px; padding-top: 8px; font-weight: bold; }
        QGroupBox::title { subcontrol-origin: margin; left: 9px; padding: 0 4px;
                           color: #8fb4c9; }
        QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit {
            background: #0f151a; color: #d8e4ea; border: 1px solid #2a3a46;
            border-radius: 4px; padding: 3px 6px; }
        QComboBox QAbstractItemView { background: #101820; color: #d8e4ea;
            selection-background-color: #2a5b7a; }
        QPushButton { background: #22303a; color: #d8e4ea; border: 1px solid #2f4350;
                      border-radius: 4px; padding: 5px 10px; }
        QPushButton:hover { background: #2c3f4d; }
        QPushButton:disabled { background: #1b2228; color: #56646e; }
        QPushButton:checked { background: #2f6b46; }
        QSlider::groove:horizontal { height: 5px; background: #22303a; border-radius: 2px; }
        QSlider::handle:horizontal { width: 12px; background: #4ba3d8;
                                     margin: -5px 0; border-radius: 6px; }
        QStatusBar { background: #101820; color: #93a7b3; }
        QDockWidget { color: #8fb4c9; titlebar-close-icon: none; }
        QScrollArea { border: 0; }
        QSplitter::handle { background: #22303a; }
        QProgressDialog { background: #151b21; }
    """)
    return app


def selftest() -> int:
    """无界面自检：导入、设备、编解码、窗口构造。"""
    lines = []

    def log(m):
        lines.append(str(m))

    ok = True
    try:
        log("version=%s python=%s" % (__version__, sys.version.split()[0]))
        log("numpy=%s" % np.__version__)
        log("frozen=%s" % getattr(sys, "frozen", False))
        import lameenc
        import miniaudio
        import pyaudiowpatch as pyaudio
        log("lameenc=%s miniaudio=%s pyaudio=%s" % (
            getattr(lameenc, "__version__", "?"), getattr(miniaudio, "version", "?"),
            getattr(pyaudio, "__version__", "?")))
        sys_ = AudioSystem.instance()
        log("inputs=%d outputs=%d default_in=%s default_out=%s" % (
            len(sys_.inputs), len(sys_.outputs),
            sys_.default_input.name if sys_.default_input else None,
            sys_.default_output.name if sys_.default_output else None))
        loop = [d for d in sys_.inputs if d.is_loopback]
        log("loopback_devices=%d" % len(loop))
        cap = CaptureEngine(sys_)
        if loop:
            cap.open(loop[0].index, int(loop[0].rate), 2, 1024, 0.0)
            cap.start_recording()
            import time
            time.sleep(0.7)
            blocks = cap.take_blocks()
            log("captured_frames=%s" % (0 if blocks is None else blocks.shape[0]))
            cap.close()

        # MP3 往返
        rate = 48000
        t = np.arange(rate, dtype=np.float32) / rate
        tone = np.repeat((0.3 * np.sin(2 * np.pi * 440 * t)).reshape(-1, 1), 2, axis=1)
        blob = engine.encode_mp3(tone, rate, bitrate=192, tags={"title": "自检"})
        log("mp3_bytes=%d" % len(blob))
        tmp = os.path.join(os.environ.get("TEMP", "."), "_luyin_selftest.mp3")
        with open(tmp, "wb") as f:
            f.write(blob)
        arr, r2, _ = engine.load_audio(tmp)
        log("mp3_roundtrip frames=%d rate=%s peak=%.3f" % (arr.shape[0], r2, float(abs(arr).max())))
        wav = tmp.replace(".mp3", ".wav")
        engine.save_wav(wav, tone, rate, 24)
        arr2, r3, _ = engine.load_audio(wav)
        log("wav24_roundtrip frames=%d rate=%s" % (arr2.shape[0], r3))
        for p in (tmp, wav):
            try:
                os.remove(p)
            except OSError:
                pass

        # 窗口构造（不显示）
        app = build_app()
        win = MainWindow(sys_, cap, PlaybackEngine(sys_))
        win.resize(1100, 700)
        log("window_ok title=%s devices_in_combo=%d" % (win.windowTitle(),
                                                        win.cmb_input.count()))
        log("RESULT=OK")
    except Exception:
        import traceback
        log("RESULT=FAIL\n" + traceback.format_exc())
        ok = False

    text = "\n".join(lines)
    try:
        base = (os.path.dirname(os.path.abspath(sys.executable))
                if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__)))
        out = os.path.join(base, "_selftest.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    if sys.stdout is not None:      # windowed 模式下没有控制台
        try:
            print(text)
        except Exception:
            pass
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    app = build_app()
    try:
        system = AudioSystem.instance()
    except AudioError as exc:
        QMessageBox.critical(None, "启动失败", str(exc))
        return 2
    capture = CaptureEngine(system)
    player = PlaybackEngine(system)
    win = MainWindow(system, capture, player)
    win.show()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        win.do_open_file(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
