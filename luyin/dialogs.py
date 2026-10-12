# -*- coding: utf-8 -*-
"""导出对话框与帮助窗口。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QSlider, QSpinBox, QTextBrowser, QVBoxLayout)

from .engine import MP3_BITRATES, fmt_bytes, fmt_time

HELP_HTML = """
<h2>Luyin Studio 录音工作室</h2>
<p>为「录制电脑里正在播放的声音」而做的轻量工具，参照 GoldWave 的操作习惯简化。</p>

<h3>一、录音（含电脑内录）</h3>
<ol>
<li>左侧「输入设备」里选择带 <b>[内录]</b> 标记的设备 —— 这是 WASAPI Loopback，
    录下来的是电脑正在播放的声音（视频、音乐、会议、游戏都行）。</li>
<li>不带 [内录] 的是麦克风/线路输入，可以录人声。</li>
<li>设定 <b>采样率 / 声道 / 位深 / 缓冲区</b>，点工具栏 <b>● 录音</b> 开始。</li>
<li>录音中可 <b>⏸ 暂停</b>（暂停期间不写入数据）、<b>■ 停止</b>。</li>
<li>不确定有没有声音？点 <b>试音</b> 只看电平表，不写入文件。</li>
</ol>
<p><b>提示：</b>内录设备通常只支持它自己的原生采样率（常见 48000 Hz）。
若选了别的采样率，程序会自动改用原生采样率采集并<b>实时重采样</b>到目标采样率，
状态栏会显示「重采样」提示。想要最好的音质，请直接使用设备原生采样率。</p>

<h3>二、波形与时间轴</h3>
<ul>
<li>上条是整段文件的缩略总览，蓝色框表示当前视图，可拖动定位。</li>
<li>滚轮 = 以鼠标位置为中心缩放；Shift/Ctrl + 滚轮 = 左右滚动；中键拖动 = 平移。</li>
<li>波形上单击 = 定位播放头；按住拖动 = 选择区间（状态栏显示选区时长）。</li>
<li>双击波形 = 全选；Ctrl+A 全选；Esc 取消选择。</li>
<li>空格 = 播放/暂停；←/→ = 微调光标；Home/End = 跳到开头/结尾。</li>
</ul>

<h3>三、文件操作</h3>
<ul>
<li><b>打开</b>：支持 WAV / MP3 / FLAC / OGG（也可以直接把文件拖进窗口）。</li>
<li><b>保存 WAV</b>：可选 16 位 PCM / 24 位 PCM / 32 位浮点。</li>
<li><b>导出 MP3</b>：可选比特率、编码质量、立体声模式，并写入 ID3 标签（标题/艺术家/专辑/年份）。</li>
<li><b>导出选区</b>：只导出选中区间。</li>
</ul>

<h3>四、编辑</h3>
<p>删除选区、裁剪到选区、静音、标准化、淡入淡出、整体音量增减，均支持 Ctrl+Z 撤销。</p>

<h3>五、快捷键</h3>
<table cellpadding="3">
<tr><td>Ctrl+N</td><td>新建</td><td>Ctrl+O</td><td>打开</td></tr>
<tr><td>Ctrl+S</td><td>保存 WAV</td><td>Ctrl+E</td><td>导出 MP3</td></tr>
<tr><td>F9 / Ctrl+R</td><td>开始录音</td><td>F10</td><td>暂停录音</td></tr>
<tr><td>F11</td><td>停止</td><td>空格</td><td>播放 / 暂停</td></tr>
<tr><td>Ctrl+A</td><td>全选</td><td>Del</td><td>删除选区</td></tr>
<tr><td>Ctrl+Z</td><td>撤销</td><td>Ctrl+T</td><td>裁剪到选区</td></tr>
</table>
"""


class ExportMp3Dialog(QDialog):
    def __init__(self, frames: int, rate: int, parent=None, title="导出 MP3"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self.frames = max(1, frames)
        self.rate = max(1, rate)
        self.seconds = self.frames / float(self.rate)

        form = QFormLayout()
        self.cmb_bitrate = QComboBox()
        for b in MP3_BITRATES:
            self.cmb_bitrate.addItem("%d kbps%s" % (b, "  （推荐）" if b == 192 else ""), b)
        self.cmb_bitrate.setCurrentIndex(MP3_BITRATES.index(192))
        form.addRow("比特率", self.cmb_bitrate)

        qrow = QHBoxLayout()
        self.sld_quality = QSlider(Qt.Horizontal)
        self.sld_quality.setRange(0, 9)
        self.sld_quality.setValue(2)
        self.sld_quality.setInvertedAppearance(True)
        self.lbl_quality = QLabel("2（高质量）")
        self.sld_quality.valueChanged.connect(self._on_quality)
        qrow.addWidget(self.sld_quality, 1)
        qrow.addWidget(self.lbl_quality)
        form.addRow("编码质量", qrow)

        self.cmb_mode = QComboBox()
        for label, val in (("联合立体声（推荐）", "joint"), ("立体声", "stereo"),
                           ("双声道独立", "dual"), ("单声道（体积更小）", "mono")):
            self.cmb_mode.addItem(label, val)
        form.addRow("声道模式", self.cmb_mode)

        tags = QGroupBox("ID3 标签（可留空）")
        tform = QFormLayout(tags)
        self.ed_title = QLineEdit()
        self.ed_artist = QLineEdit()
        self.ed_album = QLineEdit()
        self.ed_year = QLineEdit()
        tform.addRow("标题", self.ed_title)
        tform.addRow("艺术家", self.ed_artist)
        tform.addRow("专辑", self.ed_album)
        tform.addRow("年份", self.ed_year)

        self.lbl_info = QLabel()
        self.lbl_info.setFont(QFont("Consolas", 9))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("选择保存位置…")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(tags)
        lay.addWidget(self.lbl_info)
        lay.addWidget(buttons)
        self.cmb_bitrate.currentIndexChanged.connect(self._update_info)
        self.cmb_mode.currentIndexChanged.connect(self._update_info)
        self._update_info()

    def _on_quality(self, v: int):
        desc = {0: "最高", 1: "很高", 2: "高质量", 3: "较好", 4: "标准"}
        self.lbl_quality.setText("%d（%s）" % (v, desc.get(v, "较快")))
        self._update_info()

    def _update_info(self):
        br = self.cmb_bitrate.currentData()
        mode = self.cmb_mode.currentData()
        factor = 1.0 if mode != "mono" else 0.55
        size = br * 1000 / 8.0 * self.seconds * factor
        self.lbl_info.setText(
            "时长 %s ／ 采样率 %d Hz ／ 预计大小 %s" % (
                fmt_time(self.seconds), self.rate, fmt_bytes(size)))

    def values(self) -> dict:
        return {
            "bitrate": int(self.cmb_bitrate.currentData()),
            "quality": int(self.sld_quality.value()),
            "mode": str(self.cmb_mode.currentData()),
            "tags": {
                "title": self.ed_title.text().strip(),
                "artist": self.ed_artist.text().strip(),
                "album": self.ed_album.text().strip(),
                "year": self.ed_year.text().strip(),
                "encoder": "Luyin Studio",
            },
            "seconds": self.seconds,
        }


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("使用说明 / 快捷键")
        self.resize(720, 640)
        browser = QTextBrowser()
        browser.setHtml(HELP_HTML)
        browser.setOpenExternalLinks(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(browser)
        lay.addWidget(buttons)
