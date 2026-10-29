# -*- coding: utf-8 -*-
"""主窗口：采样参数、录音、波形、文件与编辑操作。"""
from __future__ import annotations

import os
import time
from collections import deque
from datetime import datetime

import numpy as np
from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDockWidget,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QMainWindow,
                               QMessageBox, QProgressDialog, QPushButton,
                               QScrollArea, QSlider, QStyle, QVBoxLayout, QWidget)

from . import APP_NAME, __version__
from . import engine
from .dialogs import ExportMp3Dialog, HelpDialog
from .engine import (AUDIO_EXT, BIT_DEPTHS, BUFFER_SIZES, SAMPLE_RATES, AudioDoc,
                     AudioError, CaptureEngine, PlaybackEngine, fmt_bytes, fmt_time)
from .widgets import LevelMeter, WaveformView

AUDIO_FILTER = ("音频文件 (*.wav *.mp3 *.flac *.ogg *.oga);;"
                "WAV (*.wav);;MP3 (*.mp3);;所有文件 (*.*)")


class MainWindow(QMainWindow):
    def __init__(self, system, capture: CaptureEngine, player: PlaybackEngine):
        super().__init__()
        self.sys = system
        self.capture = capture
        self.player = player
        self.settings = QSettings("LuyinStudio", "LuyinStudio")
        self.doc = AudioDoc(2, 48000)
        self._undo: deque = deque(maxlen=4)
        self._rec_state = None          # None | 'rec' | 'pause'
        self._monitoring = False
        self._painted_s = 0.0           # 上次重绘波形时的文档时长（限帧用）
        self._play_state = "stopped"    # stopped | playing | paused
        self._pause_pos = 0
        self._play_from = 0.0
        self._last_dir = self.settings.value("lastDir", os.path.expanduser("~"), str)
        self._suggest_name = "录音"

        self.setWindowTitle(APP_NAME)
        self.resize(1280, 760)
        self.setAcceptDrops(True)
        self.setMinimumSize(940, 560)

        self.view = WaveformView(self)
        self.view.cursorChanged.connect(self._on_cursor)
        self.view.selectionChanged.connect(self._on_selection)
        self.view.playRequested.connect(self.do_play)

        self._build_actions()
        self._build_menu()
        self._build_toolbar()
        self._build_panel()
        self._build_meters()
        self._build_status()
        self._load_settings()
        self._refresh_devices()
        self.view.set_doc(self.doc)
        self._update_title()
        self._update_status()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    # ==================================================================
    # 界面构造
    # ==================================================================
    def _build_actions(self):
        st = self.style()
        self.act_new = QAction(st.standardIcon(QStyle.SP_FileIcon), "新建(&N)", self)
        self.act_new.setShortcut(QKeySequence.New)
        self.act_new.triggered.connect(self.do_new_doc)

        self.act_open = QAction(st.standardIcon(QStyle.SP_DialogOpenButton), "打开(&O)…", self)
        self.act_open.setShortcut(QKeySequence.Open)
        self.act_open.triggered.connect(self.do_open_file)

        self.act_save_wav = QAction(st.standardIcon(QStyle.SP_DialogSaveButton), "保存 WAV(&S)…", self)
        self.act_save_wav.setShortcut(QKeySequence.Save)
        self.act_save_wav.triggered.connect(self.do_save_wav)

        self.act_export_mp3 = QAction("导出 MP3(&E)…", self)
        self.act_export_mp3.setShortcut("Ctrl+E")
        self.act_export_mp3.triggered.connect(lambda: self.do_export_mp3(False))

        self.act_export_sel = QAction("仅导出选区为 MP3…", self)
        self.act_export_sel.setShortcut("Ctrl+Shift+E")
        self.act_export_sel.triggered.connect(lambda: self.do_export_mp3(True))

        self.act_close = QAction("关闭文件", self)
        self.act_close.triggered.connect(self.do_new_doc)
        self.act_quit = QAction("退出(&Q)", self)
        self.act_quit.setShortcut("Ctrl+Q")
        self.act_quit.triggered.connect(self.close)

        self.act_undo = QAction("撤销(&U)", self)
        self.act_undo.setShortcut(QKeySequence.Undo)
        self.act_undo.triggered.connect(self.do_undo)

        self.act_select_all = QAction("全选(&A)", self)
        self.act_select_all.setShortcut(QKeySequence.SelectAll)
        self.act_select_all.triggered.connect(self.do_select_all)

        self.act_unselect = QAction("取消选择", self)
        self.act_unselect.setShortcut("Esc")
        self.act_unselect.triggered.connect(self.do_unselect)

        self.act_delete = QAction("删除选区", self)
        self.act_delete.setShortcut(QKeySequence.Delete)
        self.act_delete.triggered.connect(self.do_delete)

        self.act_crop = QAction("裁剪到选区(&T)", self)
        self.act_crop.setShortcut("Ctrl+T")
        self.act_crop.triggered.connect(self.do_crop)

        self.act_mute = QAction("静音选区", self)
        self.act_mute.setShortcut("Ctrl+M")
        self.act_mute.triggered.connect(self.do_mute)

        self.act_normalize = QAction("标准化（最大化）", self)
        self.act_normalize.triggered.connect(self.do_normalize)

        self.act_fade_in = QAction("淡入", self)
        self.act_fade_in.triggered.connect(lambda: self.do_fade(True))
        self.act_fade_out = QAction("淡出", self)
        self.act_fade_out.triggered.connect(lambda: self.do_fade(False))

        self.act_gain = QAction("音量调整…", self)
        self.act_gain.triggered.connect(self.do_gain)

        self.act_record = QAction("● 录音", self)
        self.act_record.setShortcut("F9")
        self.act_record.triggered.connect(self.do_record)

        self.act_pause_rec = QAction("⏸ 暂停录音", self)
        self.act_pause_rec.setShortcut("F10")
        self.act_pause_rec.triggered.connect(self.do_pause_rec)
        self.act_pause_rec.setEnabled(False)

        self.act_stop = QAction(st.standardIcon(QStyle.SP_MediaStop), "■ 停止", self)
        self.act_stop.setShortcut("F11")
        self.act_stop.triggered.connect(self.do_stop)

        self.act_play = QAction(st.standardIcon(QStyle.SP_MediaPlay), "▶ 播放 / 暂停", self)
        self.act_play.setShortcut("Space")
        self.act_play.setShortcutContext(Qt.ApplicationShortcut)
        self.act_play.triggered.connect(self.do_play)

        self.act_play_start = QAction(st.standardIcon(QStyle.SP_MediaSkipBackward),
                                      "从头播放", self)
        self.act_play_start.triggered.connect(self.do_play_from_start)

        self.act_loop = QAction("循环播放", self)
        self.act_loop.setCheckable(True)
        self.act_loop.toggled.connect(self._on_loop)

        self.act_monitor = QAction("试音（只看电平，不录音）", self)
        self.act_monitor.setCheckable(True)
        self.act_monitor.toggled.connect(self.do_monitor_toggle)

        self.act_rec_device = QAction("重新扫描音频设备", self)
        self.act_rec_device.setShortcut("F5")
        self.act_rec_device.triggered.connect(self._refresh_devices)

        self.act_zoom_in = QAction("放大", self)
        self.act_zoom_in.setShortcut(QKeySequence.ZoomIn)
        self.act_zoom_in.triggered.connect(lambda: self.view.zoom(1.5))
        self.act_zoom_out = QAction("缩小", self)
        self.act_zoom_out.setShortcut(QKeySequence.ZoomOut)
        self.act_zoom_out.triggered.connect(lambda: self.view.zoom(1 / 1.5))
        self.act_fit = QAction("适应窗口", self)
        self.act_fit.setShortcut("Ctrl+0")
        self.act_fit.triggered.connect(lambda: self.view.fit())
        self.act_show_all = QAction("显示整个文件", self)
        self.act_show_all.triggered.connect(lambda: self.view.fit())
        self.act_go_start = QAction("跳到开头", self)
        self.act_go_start.setShortcut("Home")
        self.act_go_start.triggered.connect(lambda: self.view.set_cursor(0.0))
        self.act_go_end = QAction("跳到结尾", self)
        self.act_go_end.setShortcut("End")
        self.act_go_end.triggered.connect(lambda: self.view.set_cursor(self.doc.seconds))

        self.act_help = QAction("使用说明 / 快捷键", self)
        self.act_help.setShortcut("F1")
        self.act_help.triggered.connect(lambda: HelpDialog(self).exec())
        self.act_about = QAction("关于", self)
        self.act_about.triggered.connect(self._about)

    def _build_menu(self):
        mb = self.menuBar()
        m = mb.addMenu("文件(&F)")
        for a in (self.act_new, self.act_open, self.act_close):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_save_wav)
        m.addAction(self.act_export_mp3)
        m.addAction(self.act_export_sel)
        m.addSeparator()
        m.addAction(self.act_quit)

        m = mb.addMenu("编辑(&E)")
        m.addAction(self.act_undo)
        m.addSeparator()
        for a in (self.act_select_all, self.act_unselect, self.act_delete, self.act_crop,
                  self.act_mute):
            m.addAction(a)
        m.addSeparator()
        for a in (self.act_normalize, self.act_fade_in, self.act_fade_out, self.act_gain):
            m.addAction(a)

        m = mb.addMenu("录音(&R)")
        m.addAction(self.act_record)
        m.addAction(self.act_pause_rec)
        m.addAction(self.act_stop)
        m.addSeparator()
        m.addAction(self.act_monitor)
        m.addAction(self.act_rec_device)

        m = mb.addMenu("播放(&P)")
        m.addAction(self.act_play)
        m.addAction(self.act_play_start)
        m.addAction(self.act_stop)
        m.addAction(self.act_loop)

        m = mb.addMenu("视图(&V)")
        for a in (self.act_zoom_in, self.act_zoom_out, self.act_fit, self.act_show_all):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_go_start)
        m.addAction(self.act_go_end)

        m = mb.addMenu("帮助(&H)")
        m.addAction(self.act_help)
        m.addAction(self.act_about)

    def _build_toolbar(self):
        tb = self.addToolBar("主工具栏")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.addAction(self.act_new)
        tb.addAction(self.act_open)
        tb.addAction(self.act_save_wav)
        tb.addAction(self.act_export_mp3)
        tb.addSeparator()
        tb.addAction(self.act_record)
        tb.addAction(self.act_pause_rec)
        tb.addAction(self.act_stop)
        tb.addSeparator()
        tb.addAction(self.act_play)
        tb.addAction(self.act_play_start)
        tb.addAction(self.act_loop)
        tb.addSeparator()
        tb.addAction(self.act_zoom_out)
        tb.addAction(self.act_zoom_in)
        tb.addAction(self.act_fit)
        tb.addSeparator()
        tb.addAction(self.act_undo)
        return tb

    def _build_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)

        # ---- 采样参数 ----
        box = QGroupBox("采样参数")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight)
        self.cmb_input = QComboBox()
        self.cmb_input.setMinimumWidth(150)
        self.cmb_input.currentIndexChanged.connect(self._on_device_changed)
        row = QHBoxLayout()
        row.addWidget(self.cmb_input, 1)
        btn = QPushButton("⟳")
        btn.setFixedWidth(30)
        btn.setToolTip("重新扫描音频设备 (F5)")
        btn.clicked.connect(self._refresh_devices)
        row.addWidget(btn)
        form.addRow("输入设备", row)

        self.lbl_dev_hint = QLabel("—")
        self.lbl_dev_hint.setWordWrap(True)
        self.lbl_dev_hint.setStyleSheet("color:#7f8f9a; font-size:11px;")
        form.addRow("", self.lbl_dev_hint)

        self.cmb_rate = QComboBox()
        self.cmb_rate.currentIndexChanged.connect(self._update_param_info)
        form.addRow("采样率", self.cmb_rate)

        self.cmb_channels = QComboBox()
        self.cmb_channels.addItem("立体声（双声道）", 2)
        self.cmb_channels.addItem("单声道", 1)
        self.cmb_channels.currentIndexChanged.connect(self._update_param_info)
        form.addRow("声道", self.cmb_channels)

        self.cmb_depth = QComboBox()
        for label, val in BIT_DEPTHS:
            self.cmb_depth.addItem(label, val)
        self.cmb_depth.currentIndexChanged.connect(self._update_param_info)
        form.addRow("位深（导出）", self.cmb_depth)

        self.cmb_buffer = QComboBox()
        for val in BUFFER_SIZES:
            self.cmb_buffer.addItem("%d 帧" % val, val)
        self.cmb_buffer.setCurrentIndex(BUFFER_SIZES.index(1024))
        self.cmb_buffer.currentIndexChanged.connect(self._update_param_info)
        form.addRow("缓冲区", self.cmb_buffer)

        gain_row = QHBoxLayout()
        self.spin_gain = QDoubleSpinBox()
        self.spin_gain.setRange(-24.0, 24.0)
        self.spin_gain.setSingleStep(0.5)
        self.spin_gain.setDecimals(1)
        self.spin_gain.setSuffix(" dB")
        self.spin_gain.valueChanged.connect(self._update_param_info)
        btn = QPushButton("归零")
        btn.setFixedWidth(50)
        btn.clicked.connect(lambda: self.spin_gain.setValue(0.0))
        gain_row.addWidget(self.spin_gain, 1)
        gain_row.addWidget(btn)
        form.addRow("输入增益", gain_row)

        self.lbl_params = QLabel()
        self.lbl_params.setStyleSheet(
            "color:#9fb3c0; font-family:Consolas; font-size:11px; "
            "background:#0d1216; border:1px solid #1d262d; padding:6px;")
        self.lbl_params.setWordWrap(True)
        form.addRow(self.lbl_params)
        lay.addWidget(box)

        # ---- 录音控制 ----
        box2 = QGroupBox("录音控制")
        v = QVBoxLayout(box2)
        self.lbl_rec_time = QLabel("00:00:00.000")
        self.lbl_rec_time.setAlignment(Qt.AlignCenter)
        self.lbl_rec_time.setStyleSheet(
            "font-family:Consolas; font-size:26px; color:#e8f4ff; "
            "background:#0d1216; border:1px solid #1d262d; padding:6px;")
        v.addWidget(self.lbl_rec_time)
        self.lbl_rec_info = QLabel("就绪")
        self.lbl_rec_info.setAlignment(Qt.AlignCenter)
        self.lbl_rec_info.setStyleSheet("color:#7f8f9a; font-size:11px;")
        v.addWidget(self.lbl_rec_info)

        row = QHBoxLayout()
        self.btn_record = QPushButton("● 开始录音")
        self.btn_record.setStyleSheet(
            "QPushButton{background:#8b1f1f;color:#fff;font-weight:bold;padding:7px;}"
            "QPushButton:hover{background:#a82626;}"
            "QPushButton:disabled{background:#3a3a3a;color:#888;}")
        self.btn_record.clicked.connect(self.do_record)
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self.do_pause_rec)
        self.btn_stop = QPushButton("■ 停止")
        self.btn_stop.clicked.connect(self.do_stop)
        row.addWidget(self.btn_record, 2)
        row.addWidget(self.btn_pause, 1)
        row.addWidget(self.btn_stop, 1)
        v.addLayout(row)

        self.btn_monitor = QPushButton("试音（只看电平，不写入）")
        self.btn_monitor.setCheckable(True)
        self.btn_monitor.toggled.connect(self.do_monitor_toggle)
        v.addWidget(self.btn_monitor)
        lay.addWidget(box2)

        # ---- 播放 ----
        box3 = QGroupBox("播放")
        form3 = QFormLayout(box3)
        self.cmb_output = QComboBox()
        form3.addRow("输出设备", self.cmb_output)
        vrow = QHBoxLayout()
        self.sld_volume = QSlider(Qt.Horizontal)
        self.sld_volume.setRange(0, 200)
        self.sld_volume.setValue(100)
        self.lbl_volume = QLabel("100%")
        self.lbl_volume.setFixedWidth(42)
        self.sld_volume.valueChanged.connect(self._on_volume)
        vrow.addWidget(self.sld_volume, 1)
        vrow.addWidget(self.lbl_volume)
        form3.addRow("音量", vrow)
        self.chk_loop = QCheckBox("循环播放")
        self.chk_loop.toggled.connect(self.act_loop.setChecked)
        self.act_loop.toggled.connect(self.chk_loop.setChecked)
        form3.addRow(self.chk_loop)
        self.lbl_play_hint = QLabel("有选区时只播放选区，无选区时从光标处播放到结尾。")
        self.lbl_play_hint.setWordWrap(True)
        self.lbl_play_hint.setStyleSheet("color:#7f8f9a; font-size:11px;")
        form3.addRow(self.lbl_play_hint)
        lay.addWidget(box3)
        lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(360)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(scroll)
        h.addWidget(self.view, 1)
        self.setCentralWidget(central)

    def _build_meters(self):
        dock = QDockWidget("电平表 (dBFS)", self)
        dock.setAllowedAreas(Qt.RightDockWidgetArea | Qt.LeftDockWidgetArea)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(6, 6, 6, 6)
        h.setSpacing(8)
        self.meter_l = LevelMeter("L", "#37d6a0")
        self.meter_r = LevelMeter("R", "#f0a63c")
        h.addWidget(self.meter_l)
        h.addWidget(self.meter_r)
        dock.setWidget(w)
        dock.setMinimumWidth(130)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self.dock_meters = dock

    def _build_status(self):
        sb = self.statusBar()
        self.lbl_st1 = QLabel("—")
        self.lbl_st2 = QLabel("—")
        self.lbl_st3 = QLabel("—")
        self.lbl_st4 = QLabel("—")
        for w in (self.lbl_st1, self.lbl_st2, self.lbl_st3, self.lbl_st4):
            w.setStyleSheet("color:#a8bcc8; font-family:Consolas; font-size:11px; padding:0 8px;")
            sb.addPermanentWidget(w)
        self.lbl_msg = QLabel("就绪")
        sb.addWidget(self.lbl_msg, 1)

    # ==================================================================
    # 设备与参数
    # ==================================================================
    def _refresh_devices(self):
        try:
            self.sys.refresh()
        except Exception as exc:
            QMessageBox.warning(self, "设备扫描失败", str(exc))
            return
        cur_in = self.cmb_input.currentData()
        self.cmb_input.blockSignals(True)
        self.cmb_input.clear()
        for d in self.sys.inputs:
            self.cmb_input.addItem(d.label, d.index)
        self.cmb_input.blockSignals(False)
        if cur_in is None and getattr(self, "_want_input", None) is not None:
            try:
                cur_in = int(self._want_input)
            except (TypeError, ValueError):
                cur_in = None
        target = cur_in if any(d.index == cur_in for d in self.sys.inputs) else None
        if target is None and self.sys.default_input:
            target = self.sys.default_input.index
        idx = self.cmb_input.findData(target)
        self._on_device_changed()
        self.cmb_input.setCurrentIndex(max(0, idx))
        if getattr(self, "_want_rate", None) is not None:
            try:
                want = int(self._want_rate)
            except (TypeError, ValueError):
                want = None
            if want is not None:
                i = self.cmb_rate.findData(want)
                if i >= 0:
                    self.cmb_rate.setCurrentIndex(i)
            self._want_rate = None

        cur_out = self.cmb_output.currentData()
        self.cmb_output.blockSignals(True)
        self.cmb_output.clear()
        for d in self.sys.outputs:
            self.cmb_output.addItem(d.label, d.index)
        self.cmb_output.blockSignals(False)
        if cur_out is None and getattr(self, "_want_output", None) is not None:
            try:
                cur_out = int(self._want_output)
            except (TypeError, ValueError):
                cur_out = None
        target = cur_out if any(d.index == cur_out for d in self.sys.outputs) else None
        if target is None and self.sys.default_output:
            target = self.sys.default_output.index
        idx = self.cmb_output.findData(target)
        self.cmb_output.setCurrentIndex(max(0, idx))
        self._on_device_changed()
        self._set_msg("已扫描到 %d 个输入设备 / %d 个输出设备（F5 可重新扫描）"
                      % (len(self.sys.inputs), len(self.sys.outputs)))

    def _current_device(self):
        idx = self.cmb_input.currentData()
        return self.sys.device(idx) if idx is not None else None

    def _on_device_changed(self):
        dev = self._current_device()
        if dev is None:
            return
        native = int(round(dev.rate))
        rates = list(SAMPLE_RATES)
        if native not in rates:
            rates.append(native)
        rates.sort()
        keep = self.cmb_rate.currentData()
        self.cmb_rate.blockSignals(True)
        self.cmb_rate.clear()
        for r in rates:
            tag = "  ← 设备原生" if r == native else ""
            self.cmb_rate.addItem("%d Hz%s" % (r, tag), r)
        self.cmb_rate.blockSignals(False)
        want = keep if keep in rates else native
        self.cmb_rate.setCurrentIndex(rates.index(want))
        if dev.is_loopback:
            self.lbl_dev_hint.setText(
                "内录设备：录制电脑正在播放的声音。原生 %.0f Hz / %d 声道。"
                % (dev.rate, dev.max_in))
        else:
            self.lbl_dev_hint.setText(
                "输入设备（麦克风/线路）。原生 %.0f Hz / 最多 %d 声道。"
                % (dev.rate, dev.max_in))
        self._update_param_info()

    def _params(self) -> dict:
        return {
            "device": self.cmb_input.currentData(),
            "rate": int(self.cmb_rate.currentData() or 48000),
            "channels": int(self.cmb_channels.currentData() or 2),
            "depth": int(self.cmb_depth.currentData() or 16),
            "buffer": int(self.cmb_buffer.currentData() or 1024),
            "gain": float(self.spin_gain.value()),
        }

    def _update_param_info(self):
        p = self._params()
        dev = self._current_device()
        per_sec = p["rate"] * p["channels"] * (p["depth"] / 8.0)
        kbps = p["rate"] * p["channels"] * p["depth"] / 1000.0
        latency = p["buffer"] / float(p["rate"]) * 1000.0
        native = int(round(dev.rate)) if dev else p["rate"]
        note = ""
        if dev is not None and p["rate"] != native:
            note = "\n⚠ 设备原生 %d Hz，将实时重采样到 %d Hz" % (native, p["rate"])
        self.lbl_params.setText(
            "采集格式 : 32 位浮点 → 导出 %d 位\n"
            "数据速率 : %.0f kbps（%d Hz × %d 声道 × %d bit）\n"
            "WAV 大小 : %s / 分钟\n"
            "缓冲区   : %.1f ms%s" % (
                p["depth"], kbps, p["rate"], p["channels"], p["depth"],
                fmt_bytes(per_sec * 60), latency, note))

    def _on_volume(self, v):
        self.lbl_volume.setText("%d%%" % v)
        self.player.set_volume(v / 100.0)

    def _on_loop(self, on):
        self.chk_loop.setChecked(bool(on))

    # ==================================================================
    # 录音
    # ==================================================================
    def do_record(self):
        if self._rec_state:
            self.do_stop()
            return
        if self.doc.frames and self.doc.dirty:
            if not self._confirm_discard("开始新的录音会丢弃当前未保存的内容，继续吗？"):
                return
        p = self._params()
        if p["device"] is None:
            QMessageBox.warning(self, "没有输入设备", "没有可用的输入设备，请检查系统声音设置。")
            return
        self._stop_playback()
        reuse = (self._monitoring and self.capture.is_open
                 and self.capture.device_index == p["device"]
                 and self.capture.req_rate == p["rate"]
                 and self.capture.req_channels == p["channels"])
        try:
            if not reuse:
                if self._monitoring:
                    self.capture.close()
                    self._monitoring = False
                    self.btn_monitor.setChecked(False)
                    self.btn_monitor.setText("试音（只看电平，不写入）")
                self.capture.open(p["device"], p["rate"], p["channels"],
                                  p["buffer"], p["gain"])
            else:
                self.capture.set_gain_db(p["gain"])
            self.capture.start_recording()
        except AudioError as exc:
            QMessageBox.critical(self, "无法开始录音", str(exc))
            return
        self.doc = AudioDoc(p["channels"], self.capture.req_rate)
        self.view.set_doc(self.doc)
        self.view.view_span = 10.0
        self.view.follow = True
        self._rec_state = "rec"
        self._set_ui_recording(True)
        self._set_msg("● 正在录音……  设备：%s" % self._current_device().name)
        if self.capture.resampling:
            self._set_msg("● 正在录音（设备 %.0f Hz → 已重采样到 %d Hz）"
                          % (self.capture.dev_rate, self.capture.req_rate))

    def do_pause_rec(self):
        if self._rec_state == "rec":
            self.capture.pause()
            self._rec_state = "pause"
            self.btn_pause.setText("▶ 继续")
            self.act_pause_rec.setText("▶ 继续录音")
            self.lbl_rec_info.setText("已暂停（不写入数据）")
            self._set_msg("录音已暂停")
        elif self._rec_state == "pause":
            self.capture.resume()
            self._rec_state = "rec"
            self.btn_pause.setText("⏸ 暂停")
            self.act_pause_rec.setText("⏸ 暂停录音")
            self.lbl_rec_info.setText("正在录音")
            self._set_msg("继续录音")
        self._set_ui_recording(True)

    def do_stop(self):
        if self._rec_state:
            wall = self.capture.wall_seconds
            blocks = self.capture.take_blocks()
            if blocks is not None:
                self.doc.append(blocks)
            self.capture.close()
            self._rec_state = None
            self.btn_pause.setText("⏸ 暂停")
            self.act_pause_rec.setText("⏸ 暂停录音")
            self.lbl_rec_info.setText("已停止")
            self.lbl_rec_info.setStyleSheet("")
            self.doc.recorded = True
            self.doc.dirty = True
            self.doc.path = None
            self._suggest_name = "录音_" + datetime.now().strftime("%Y%m%d_%H%M%S")
            self.view.follow = False
            self.view.fit()
            self._set_ui_recording(False)
            self._set_msg("录音完成：%s，共 %s。可「保存 WAV」或「导出 MP3」。"
                          % (fmt_time(self.doc.seconds, False), fmt_time(self.doc.seconds)))
            self.meter_l.reset()
            self.meter_r.reset()
            self._update_title()
            self._report_rec_lag(wall)
        elif self._play_state != "stopped":
            self._stop_playback()
        elif self._monitoring:
            self.do_monitor_toggle(False)

    def _report_rec_lag(self, wall: float):
        """录音结束后核对时间轴：驱动丢帧时明确告诉用户，避免导出后才发现偏快。"""
        if wall <= 1.0:
            return
        short = wall - self.doc.seconds
        if short <= max(0.30, wall * 0.005):
            return
        pct = short / wall * 100.0
        QMessageBox.warning(
            self, "录音时间轴偏短",
            "本次录音：真实时间 %s，实际采到 %s，少了 %.2f 秒（%.1f%%）。\n\n"
            "这是声卡驱动丢帧造成的，导出的文件会比实际快、音调偏高。\n"
            "可以尝试：\n"
            "  · 把「缓冲区」调大（例如 2048 / 4096）\n"
            "  · 关闭占用 CPU 的程序，或改用另一个内录设备\n"
            "  · 重新录一遍\n\n"
            "已有的这遍仍然可以保存/导出，只是时长偏短。"
            % (fmt_time(wall), fmt_time(self.doc.seconds), short, pct))
        self._set_msg("⚠ 本次录音少了 %.2f 秒（%.1f%%）：驱动丢帧，建议增大缓冲区后重录。"
                      % (short, pct))

    def do_monitor_toggle(self, on: bool):
        on = bool(on)
        if on == self._monitoring:
            self.btn_monitor.setChecked(on)
            self.act_monitor.setChecked(on)
            return
        if on:
            if self._rec_state:
                self.btn_monitor.setChecked(False)
                self.act_monitor.setChecked(False)
                return
            p = self._params()
            if p["device"] is None:
                self.btn_monitor.setChecked(False)
                return
            try:
                self.capture.open(p["device"], p["rate"], p["channels"],
                                  p["buffer"], p["gain"])
            except AudioError as exc:
                QMessageBox.critical(self, "无法打开输入设备", str(exc))
                self.btn_monitor.setChecked(False)
                self.act_monitor.setChecked(False)
                return
            self._monitoring = True
            self.btn_monitor.setText("■ 停止试音")
            self._set_msg("试音中：观察右侧电平表，确认有信号后再开始录音")
        else:
            if not self._rec_state:
                self.capture.close()
            self._monitoring = False
            self.btn_monitor.setText("试音（只看电平，不写入）")
            self.meter_l.reset()
            self.meter_r.reset()
            self._set_msg("已停止试音")
        self.btn_monitor.setChecked(on)
        self.act_monitor.setChecked(on)

    def _set_ui_recording(self, recording: bool):
        self.btn_record.setText("● 录音中…" if recording else "● 开始录音")
        self.btn_pause.setEnabled(recording)
        self.act_pause_rec.setEnabled(recording)
        self.act_record.setEnabled(not recording)
        for w in (self.cmb_input, self.cmb_rate, self.cmb_channels, self.cmb_buffer,
                  self.cmb_depth, self.spin_gain, self.btn_monitor):
            w.setEnabled(not recording)

    # ==================================================================
    # 播放
    # ==================================================================
    def do_play(self):
        if self._play_state == "playing":
            self._pause_pos = self.player.position
            self.player.stop()
            self._play_state = "paused"
            self.act_play.setText("▶ 继续播放")
            self._set_msg("播放已暂停")
            return
        rate = self.doc.rate
        if self.doc.frames == 0:
            return
        loop = self.act_loop.isChecked()
        sel = self.view.selection()
        start = self._pause_pos if self._play_state == "paused" else None
        if start is not None and (start >= self.doc.frames or start < 0):
            start = None
        if start is None:
            if sel:
                start = int(sel[0] * rate)
            else:
                start = int(self.view.cursor * rate)
        end = None
        if sel and self._play_state != "paused":
            end = int(sel[1] * rate)
        try:
            self.player.play(self.doc.data, rate, self.cmb_output.currentData(),
                             start_frame=start, end_frame=end, loop=loop)
        except AudioError as exc:
            QMessageBox.critical(self, "无法播放", str(exc))
            return
        self._play_state = "playing"
        self._play_from = start / float(rate)
        self.act_play.setText("⏸ 暂停播放")
        self._set_msg("正在播放%s" % ("（循环）" if loop else ""))

    def do_play_from_start(self):
        self.view.set_cursor(0.0)
        self._pause_pos = 0
        self._play_state = "stopped"
        self.do_play()

    def _stop_playback(self, move_cursor=False):
        if self._play_state != "stopped":
            self.player.stop()
            self._play_state = "stopped"
            self.act_play.setText("▶ 播放 / 暂停")
            if move_cursor:
                self.view.set_cursor(self._play_from, notify=True)
            self._set_msg("播放已停止")

    # ==================================================================
    # 周期刷新
    # ==================================================================
    def _tick(self):
        if self._rec_state:
            blocks = self.capture.take_blocks()
            if blocks is not None:
                self.doc.append(blocks)
            self.lbl_rec_time.setText(fmt_time(self.doc.seconds))
            self._update_rec_info()
            # 波形限帧：录音时时长每隔 80ms 才重绘一次（约 12fps）。
            # 波形重绘每次要持有 GIL 若干毫秒，画得太勤会把音频读取线程
            # 饿死，PortAudio 环形缓冲一溢出就静默丢帧——录到的比实际短、
            # 导出后像快放。计时器/电平表仍然按 30fps 刷。
            if self.doc.seconds - self._painted_s >= 0.08 or self.doc.seconds < 0.1:
                self._painted_s = self.doc.seconds
                if self.view.follow:
                    self.view.follow_end()
                else:
                    self.view.update()
            self._update_status()
        if self._rec_state or self._monitoring:
            peaks, rmss = self.capture.levels()
            self.meter_l.set_level(float(peaks[0]), float(rmss[0]))
            self.meter_r.set_level(float(peaks[1]), float(rmss[1]))

        if self._play_state == "playing":
            pos = self.player.position_seconds
            self.view.set_cursor(pos, notify=False)
            self.lbl_st4.setText("光标 %s" % fmt_time(pos))
            self.view.update()
            if not self.player.playing:
                self._play_state = "stopped"
                self.act_play.setText("▶ 播放 / 暂停")
                self.view.set_cursor(self._play_from, notify=True)
                self._set_msg("播放结束")

    def _update_rec_info(self):
        """录音时把「采到的时长」和「真实经过的时间」摆在一起，丢帧立刻可见。"""
        if self._rec_state != "rec":
            self.lbl_rec_info.setText("已暂停")
            self.lbl_rec_info.setStyleSheet("")
            return
        wall = self.capture.wall_seconds
        drift = self.capture.drift
        if wall > 1.0 and drift > max(0.30, wall * 0.005):
            pct = drift / wall * 100.0
            self.lbl_rec_info.setText(
                "⚠ 丢帧：已录 %s / 实际 %s（少 %.1f%%）"
                % (fmt_time(self.doc.seconds), fmt_time(wall), pct))
            self.lbl_rec_info.setStyleSheet("color:#ffb020;")
        else:
            self.lbl_rec_info.setText("正在录音…")
            self.lbl_rec_info.setStyleSheet("")

    # ==================================================================
    # 文件
    # ==================================================================
    def _confirm_discard(self, text="当前内容尚未保存，确定放弃吗？") -> bool:
        r = QMessageBox.question(self, "确认", text,
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return r == QMessageBox.Yes

    def do_new_doc(self):
        if self._rec_state:
            self.do_stop()
        self._stop_playback()
        if self.doc.frames and self.doc.dirty:
            if not self._confirm_discard():
                return
        self._undo.clear()
        self.doc = AudioDoc(2, self._params()["rate"])
        self.view.set_doc(self.doc)
        self._update_title()
        self._update_status()
        self._set_msg("新建空白文档")

    def do_open_file(self, path: str | None = None):
        if self._rec_state:
            self.do_stop()
        self._stop_playback()
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "打开音频文件", self._last_dir,
                                                  AUDIO_FILTER)
        if not path:
            return
        if self.doc.frames and self.doc.dirty and not self._confirm_discard():
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            data, rate, depth = engine.load_audio(path)
        except AudioError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "打开失败", str(exc))
            return
        QApplication.restoreOverrideCursor()
        if data is None or data.shape[0] == 0:
            QMessageBox.warning(self, "打开失败", "文件里没有音频数据。")
            return
        self._undo.clear()
        self.doc = AudioDoc(data.shape[1], rate)
        self.doc.set_data(data, rate, path)
        self.view.set_doc(self.doc)
        self.view.fit()
        self._last_dir = os.path.dirname(path)
        self.settings.setValue("lastDir", self._last_dir)
        self._suggest_name = os.path.splitext(os.path.basename(path))[0]
        self._update_title()
        self._update_status()
        self._set_msg("已载入 %s（%d 帧，%s）" % (os.path.basename(path), data.shape[0],
                                                fmt_time(self.doc.seconds)))

    def do_save_wav(self):
        if self.doc.frames == 0:
            QMessageBox.information(self, "没有数据", "还没有可保存的音频。")
            return
        p = self._params()
        depth = p["depth"]
        default = os.path.join(self._last_dir, self._suggest_name + ".wav")
        path, _ = QFileDialog.getSaveFileName(self, "保存为 WAV（%d 位）" % depth,
                                              default, "WAV 文件 (*.wav)")
        if not path:
            return
        if not path.lower().endswith(".wav"):
            path += ".wav"
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            size = engine.save_wav(path, self.doc.data, self.doc.rate, depth)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        QApplication.restoreOverrideCursor()
        self.doc.path = path
        self.doc.dirty = False
        self._last_dir = os.path.dirname(path)
        self.settings.setValue("lastDir", self._last_dir)
        self._update_title()
        self._set_msg("已保存 %s（%d 位，%s）" % (os.path.basename(path), depth, fmt_bytes(size)))

    def do_export_mp3(self, selection_only: bool):
        data = self.doc.data
        if data.shape[0] == 0:
            QMessageBox.information(self, "没有数据", "还没有可导出的音频。")
            return
        if selection_only:
            sel = self._sel_samples()
            if sel is None:
                QMessageBox.information(self, "没有选区", "请先在波形上拖选一段区间。")
                return
            data = data[sel[0]:sel[1]]
        dlg = ExportMp3Dialog(data.shape[0], self.doc.rate, self,
                              "导出 MP3" + ("（选区）" if selection_only else ""))
        if dlg.exec() != ExportMp3Dialog.Accepted:
            return
        v = dlg.values()
        default = os.path.join(self._last_dir, self._suggest_name + ".mp3")
        path, _ = QFileDialog.getSaveFileName(self, "导出 MP3", default, "MP3 文件 (*.mp3)")
        if not path:
            return
        if not path.lower().endswith(".mp3"):
            path += ".mp3"
        if self._encode_mp3_with_progress(data, self.doc.rate, v, path):
            self._last_dir = os.path.dirname(path)
            self.settings.setValue("lastDir", self._last_dir)
            self._set_msg("已导出 %s（%d kbps，%s）"
                          % (os.path.basename(path), v["bitrate"],
                             fmt_bytes(os.path.getsize(path))))

    def _encode_mp3_with_progress(self, data, rate, values, path) -> bool:
        try:
            enc = engine.Mp3StreamEncoder(rate, data.shape[1],
                                          bitrate=values["bitrate"],
                                          quality=values["quality"],
                                          mode=values["mode"])
        except AudioError as exc:
            QMessageBox.critical(self, "无法导出", str(exc))
            return False
        total = data.shape[0]
        step = max(1, int(rate * 5))
        dlg = QProgressDialog("正在编码 MP3…", "取消", 0, max(1, total), self)
        dlg.setWindowTitle("导出 MP3")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        cancelled = False
        try:
            with open(path, "wb") as f:
                head = engine._id3v23(values.get("tags") or {})
                if head:
                    f.write(head)
                for i in range(0, total, step):
                    blob = enc.encode(data[i:i + step])
                    if blob:
                        f.write(blob)
                    dlg.setValue(min(i + step, total))
                    QApplication.processEvents()
                    if dlg.wasCanceled():
                        blob = enc.flush()
                        if blob:
                            f.write(blob)
                        f.flush()
                        cancelled = True
                        break
                blob = enc.flush()
                if blob:
                    f.write(blob)
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return False
        finally:
            dlg.close()
        if cancelled:
            try:
                os.remove(path)
            except OSError:
                pass
            self._set_msg("已取消导出")
            return False
        return True

    # ==================================================================
    # 编辑
    # ==================================================================
    def _sel_samples(self):
        sel = self.view.selection()
        if sel is None:
            return None
        i0 = max(0, int(sel[0] * self.doc.rate))
        i1 = min(self.doc.frames, int(round(sel[1] * self.doc.rate)))
        if i1 - i0 < 1:
            return None
        return i0, i1

    def _snapshot(self):
        nbytes = self.doc.frames * self.doc.channels * 4
        if nbytes > 150 * 1024 * 1024:
            self._undo.clear()
            return
        self._undo.append((self.doc.data.copy(), self.doc.rate, self.doc.path))

    def _apply(self, arr, sel_after=None):
        self.doc.set_data(arr, rate=self.doc.rate, path=self.doc.path)
        self.doc.dirty = True
        if sel_after:
            self.view.set_selection(sel_after[0] / self.doc.rate,
                                    sel_after[1] / self.doc.rate, notify=True)
        else:
            self.view.set_selection(0.0, 0.0, notify=True)
        self.view.update()
        self._update_status()
        self._update_title()

    def do_undo(self):
        if not self._undo:
            self._set_msg("没有可撤销的操作")
            return
        data, rate, path = self._undo.pop()
        self.doc.set_data(data, rate=rate, path=path)
        self.doc.dirty = True
        self.view.set_selection(0.0, 0.0, notify=True)
        self.view.update()
        self._update_status()
        self._set_msg("已撤销")

    def do_select_all(self):
        if self.doc.frames:
            self.view.set_selection(0.0, self.doc.seconds)
            self._set_msg("已全选：%s" % fmt_time(self.doc.seconds))

    def do_unselect(self):
        self.view.set_selection(0.0, 0.0)

    def do_delete(self):
        sel = self._sel_samples()
        if sel is None:
            self._set_msg("请先选择要删除的区间")
            return
        i0, i1 = sel
        self._snapshot()
        d = self.doc.data
        arr = np.concatenate((d[:i0], d[i1:]), axis=0) if d.shape[0] > i1 else d[:i0].copy()
        self._apply(arr, (i0, i0))
        self._set_msg("已删除 %s" % fmt_time((i1 - i0) / self.doc.rate))

    def do_crop(self):
        sel = self._sel_samples()
        if sel is None:
            self._set_msg("请先选择要保留的区间")
            return
        self._snapshot()
        arr = self.doc.data[sel[0]:sel[1]].copy()
        self._apply(arr, (0, arr.shape[0]))
        self.view.fit()
        self._set_msg("已裁剪为 %s" % fmt_time(self.doc.seconds))

    def do_mute(self):
        sel = self._sel_samples()
        if sel is None:
            self._set_msg("请先选择要静音的区间")
            return
        self._snapshot()
        arr = self.doc.data.copy()
        arr[sel[0]:sel[1]] = 0.0
        self._apply(arr, sel)
        self._set_msg("已静音选区")

    def do_normalize(self):
        if self.doc.frames == 0:
            return
        peak = self.doc.peak_abs()
        if peak < 1e-6:
            self._set_msg("音频是静音，无法标准化")
            return
        self._snapshot()
        arr = self.doc.data * (0.99 / peak)
        np.clip(arr, -1.0, 1.0, out=arr)
        self._apply(arr, (0, 0))
        self._set_msg("已标准化（原峰值 %.1f dB → -0.1 dB）" % engine.db_of(peak))

    def do_fade(self, fade_in: bool):
        if self.doc.frames == 0:
            return
        sel = self._sel_samples()
        if sel is None:
            n = min(self.doc.frames, int(self.doc.rate * 1.0))
            sel = (0, n) if fade_in else (self.doc.frames - n, self.doc.frames)
        i0, i1 = sel
        if i1 - i0 < 2:
            return
        self._snapshot()
        arr = self.doc.data.copy()
        ramp = np.linspace(0.0, 1.0, i1 - i0, dtype=np.float32)
        if not fade_in:
            ramp = ramp[::-1]
        arr[i0:i1] = arr[i0:i1] * ramp.reshape(-1, 1)
        self._apply(arr, sel)
        self._set_msg("已%s（%s）" % ("淡入" if fade_in else "淡出",
                                     fmt_time((i1 - i0) / self.doc.rate)))

    def do_gain(self):
        if self.doc.frames == 0:
            return
        db, ok = QInputDialog.getDouble(self, "音量调整", "增益 (dB)：", 0.0, -60.0, 60.0, 1)
        if not ok or abs(db) < 1e-6:
            return
        self._snapshot()
        arr = self.doc.data * (10.0 ** (db / 20.0))
        np.clip(arr, -1.0, 1.0, out=arr)
        self._apply(arr, (0, 0))
        self._set_msg("已调整音量 %+.1f dB" % db)

    # ==================================================================
    # 事件与状态
    # ==================================================================
    def _on_cursor(self, t):
        self.lbl_st4.setText("光标 %s" % fmt_time(t))

    def _on_selection(self, s, e):
        if e > s:
            self._set_msg("选区 %s → %s（时长 %s）"
                          % (fmt_time(s), fmt_time(e), fmt_time(e - s)))
        self._update_status()

    def _update_status(self):
        p = self._params()
        self.lbl_st1.setText("时长 %s" % fmt_time(self.doc.seconds))
        self.lbl_st2.setText("%d 帧 / %d Hz / %d 声道" % (self.doc.frames, self.doc.rate,
                                                          self.doc.channels))
        size = self.doc.frames * self.doc.channels * (p["depth"] / 8.0)
        self.lbl_st3.setText("WAV 约 %s" % fmt_bytes(size))
        sel = self.view.selection()
        if sel:
            self.lbl_st4.setText("选区 %s" % fmt_time(sel[1] - sel[0]))
        else:
            self.lbl_st4.setText("光标 %s" % fmt_time(self.view.cursor))

    def _update_title(self):
        name = os.path.basename(self.doc.path) if self.doc.path else (
            "未命名" if self.doc.frames else "空白")
        star = " *" if self.doc.dirty else ""
        self.setWindowTitle("%s%s — %s" % (name, star, APP_NAME))

    def _set_msg(self, text):
        self.lbl_msg.setText(text)

    def _about(self):
        QMessageBox.about(
            self, "关于",
            "<h3>%s</h3><p>版本 %s</p>"
            "<p>面向「录制电脑内声音」的录音 / 编辑 / 导出工具。<br>"
            "双声道 · WASAPI 内录 · 实时波形与电平表 · WAV/MP3 输出。</p>"
            "<p style='color:#888'>音频后端：PortAudio/PyAudioWPatch　"
            "MP3：LAME (lameenc)　解码：miniaudio　界面：PySide6</p>"
            % (APP_NAME, __version__))

    # -- 拖放 -----------------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith(tuple(AUDIO_EXT.split())):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith(tuple(AUDIO_EXT.split())):
                self.do_open_file(p)
                break

    # -- 设置 -----------------------------------------------------------
    def _save_settings(self):
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("lastDir", self._last_dir)
        s.setValue("input", self.cmb_input.currentData())
        s.setValue("output", self.cmb_output.currentData())
        s.setValue("rate", self.cmb_rate.currentData())
        s.setValue("channels", self.cmb_channels.currentData())
        s.setValue("depth", self.cmb_depth.currentData())
        s.setValue("buffer", self.cmb_buffer.currentData())
        s.setValue("gain", self.spin_gain.value())
        s.setValue("volume", self.sld_volume.value())

    def _load_settings(self):
        s = self.settings
        geo = s.value("geometry")
        if geo:
            try:
                self.restoreGeometry(geo)
            except Exception:
                pass
        try:
            vol = int(s.value("volume", 100))
        except (TypeError, ValueError):
            vol = 100
        self.sld_volume.setValue(max(0, min(200, vol)))
        self.player.set_volume(vol / 100.0)
        for combo, key, default in ((self.cmb_channels, "channels", 2),
                                    (self.cmb_depth, "depth", 16),
                                    (self.cmb_buffer, "buffer", 1024)):
            try:
                val = int(s.value(key, default))
            except (TypeError, ValueError):
                val = default
            idx = combo.findData(val)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
        try:
            g = float(s.value("gain", 0.0))
        except (TypeError, ValueError):
            g = 0.0
        self.spin_gain.setValue(max(-24.0, min(24.0, g)))
        self._want_input = s.value("input", None)
        self._want_output = s.value("output", None)
        self._want_rate = s.value("rate", None)

    def closeEvent(self, event):
        if self._rec_state:
            if not self._confirm_discard("正在录音，确定要退出并放弃本次录音吗？"):
                event.ignore()
                return
            self.do_stop()
        if self.doc.frames and self.doc.dirty:
            if not self._confirm_discard("当前内容尚未保存，确定退出吗？"):
                event.ignore()
                return
        self._stop_playback()
        self._save_settings()
        try:
            self.capture.close()
            self.sys.terminate()
        except Exception:
            pass
        event.accept()
