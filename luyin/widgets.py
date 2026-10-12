# -*- coding: utf-8 -*-
"""波形显示、时间标尺、缩略总览与电平表控件。"""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import Qt, QPointF, QRectF, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                           QPen, QPolygonF)
from PySide6.QtWidgets import QSizePolicy, QWidget

from .engine import db_of, fmt_time

BG = QColor("#0b0e11")
LANE_BG = QColor("#11161b")
LANE_BG2 = QColor("#0e1317")
GRID = QColor("#243039")
GRID_STRONG = QColor("#33424d")
TEXT = QColor("#8fa3b0")
TEXT_LIGHT = QColor("#d8e4ea")
CURVE_L = QColor("#37d6a0")
CURVE_R = QColor("#f0a63c")
CURSOR = QColor("#ff4d4d")
SEL_FILL = QColor(80, 150, 255, 46)
SEL_EDGE = QColor("#5b9dff")
OVERVIEW = QColor("#2b6a8f")
VIEWBOX = QColor(255, 255, 255, 40)

TICK_STEPS = [0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1,
              0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]


def _nice_step(span: float, pixels: float, min_px: float = 72.0) -> float:
    if span <= 0 or pixels <= 0:
        return 1.0
    for step in TICK_STEPS:
        if step / span * pixels >= min_px:
            return step
    return TICK_STEPS[-1]


def _ruler_text(t: float, step: float) -> str:
    if step >= 1.0:
        return "%d:%02d:%02d" % (int(t // 3600), int(t % 3600 // 60), int(t % 60))
    if step >= 0.1:
        return "%d:%02d.%d" % (int(t // 60), int(t % 60), int(round((t % 1) * 10)) % 10)
    if step >= 0.01:
        return "%d:%02d.%02d" % (int(t // 60), int(t % 60), int(round((t % 1) * 100)) % 100)
    return "%d:%02d.%03d" % (int(t // 60), int(t % 60), int(round((t % 1) * 1000)) % 1000)


def _reduce_cols(mn: np.ndarray, mx: np.ndarray, ncols: int):
    """把 (min,max) 序列压缩成 ncols 列，返回列级 (min,max)。"""
    n = mn.shape[0]
    if n <= 0 or ncols <= 0:
        return None
    if n == 1:
        return (np.full(ncols, float(mn[0, 0]), np.float32),
                np.full(ncols, float(mx[0, 0]), np.float32))
    bounds = np.linspace(0, n, ncols + 1).astype(np.int64)
    starts = np.minimum(bounds[:-1], n - 1)
    return (np.minimum.reduceat(mn[:, 0], starts),
            np.maximum.reduceat(mx[:, 0], starts))


class WaveformView(QWidget):
    """立体声波形视图：总览条 + 时间标尺 + 多声道波形 + 光标/选区。"""

    cursorChanged = Signal(float)
    selectionChanged = Signal(float, float)
    viewChanged = Signal(float, float)
    playRequested = Signal()

    OVERVIEW_H = 28
    RULER_H = 22
    GUTTER = 48

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc = None
        self.view_start = 0.0
        self.view_span = 8.0
        self.cursor = 0.0
        self.sel_start = 0.0
        self.sel_end = 0.0
        self.follow = False
        self._drag = None
        self._drag_anchor = 0.0
        self._drag_view0 = 0.0
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAutoFillBackground(False)

    # -- 数据 ------------------------------------------------------------
    def set_doc(self, doc):
        self.doc = doc
        self.cursor = 0.0
        self.sel_start = self.sel_end = 0.0
        self.view_start = 0.0
        self.view_span = max(0.5, min(8.0, doc.seconds)) if doc and doc.seconds else 8.0
        self.update()

    def duration(self) -> float:
        return self.doc.seconds if self.doc is not None else 0.0

    # -- 视图控制 --------------------------------------------------------
    def set_view(self, start: float, span: float):
        span = max(1e-4, float(span))
        start = max(0.0, float(start))
        self.view_start, self.view_span = start, span
        self.viewChanged.emit(start, span)
        self.update()

    def zoom(self, factor: float, anchor: float | None = None):
        if self.doc is None:
            return
        if anchor is None:
            anchor = self.view_start + self.view_span / 2.0
        rel = (anchor - self.view_start) / self.view_span
        span = self.view_span / factor
        span = max(1.0 / max(1, self.doc.rate) * 16, min(span, max(self.duration(), 1.0) * 1.2 + 1.0))
        start = anchor - rel * span
        self.set_view(max(0.0, start), span)

    def fit(self):
        if self.doc is None:
            return
        self.set_view(0.0, max(self.duration(), 1e-3))

    def follow_end(self):
        if self.doc is None:
            return
        self.view_start = max(0.0, self.duration() - self.view_span)
        self.update()

    def ensure_visible(self, t: float):
        if t < self.view_start or t > self.view_start + self.view_span:
            self.set_view(max(0.0, t - self.view_span / 2.0), self.view_span)

    # -- 坐标换算 --------------------------------------------------------
    def plot_rect(self) -> QRectF:
        top = self.OVERVIEW_H + self.RULER_H
        return QRectF(self.GUTTER, top, max(1, self.width() - self.GUTTER - 1),
                      max(1, self.height() - top - 1))

    def x_to_time(self, x: float) -> float:
        r = self.plot_rect()
        return self.view_start + (x - r.left()) / r.width() * self.view_span

    def time_to_x(self, t: float) -> float:
        r = self.plot_rect()
        return r.left() + (t - self.view_start) / self.view_span * r.width()

    def lane_rect(self, ch: int, lanes: int) -> QRectF:
        r = self.plot_rect()
        h = r.height() / lanes
        return QRectF(r.left(), r.top() + ch * h, r.width(), h - 1)

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        p.setRenderHint(QPainter.Antialiasing, False)
        self._draw_overview(p)
        self._draw_ruler(p)
        if self.doc is None or self.doc.frames == 0:
            p.setPen(QPen(QColor("#4d5c66")))
            f = QFont("Microsoft YaHei UI", 11)
            p.setFont(f)
            p.drawText(self.plot_rect(), Qt.AlignCenter,
                       "没有音频数据\n\n点击工具栏「● 录音」录制电脑内声音，或「打开」载入音频文件")
            p.end()
            return
        lanes = self.doc.channels
        for ch in range(lanes):
            self._draw_lane(p, ch, lanes)
        self._draw_cursor(p, lanes)
        p.end()

    def _draw_overview(self, p: QPainter):
        rect = QRectF(self.GUTTER, 1, max(1, self.width() - self.GUTTER - 1), self.OVERVIEW_H - 2)
        p.fillRect(rect, QColor("#0d1216"))
        p.setPen(QPen(QColor("#1d262d")))
        p.drawRect(rect.adjusted(0, 0, -1, -1))
        if self.doc is None or self.doc.frames == 0:
            return
        cols = int(rect.width())
        total = self.doc.frames
        cache = self.doc.peaks
        if cache.count:
            b0, b1 = 0, cache.count
            mn, mx = _reduce_cols(cache.mins(b0, b1), cache.maxs(b0, b1), cols)
        else:
            mn = mx = None
        if mn is None:
            return
        mid = rect.center().y()
        half = rect.height() / 2.0 - 1
        poly = QPolygonF()
        for i in range(cols):
            x = rect.left() + i
            poly.append(QPointF(x, mid - float(mx[i]) * half))
        for i in range(cols - 1, -1, -1):
            x = rect.left() + i
            poly.append(QPointF(x, mid - float(mn[i]) * half))
        p.setBrush(QBrush(OVERVIEW))
        p.setPen(Qt.NoPen)
        p.drawPolygon(poly)

        # 当前视图范围
        dur = self.duration()
        if dur > 0:
            vx0 = rect.left() + self.view_start / dur * rect.width()
            vx1 = rect.left() + min(dur, self.view_start + self.view_span) / dur * rect.width()
            p.setBrush(QBrush(QColor(255, 255, 255, 34)))
            p.setPen(QPen(QColor("#9fd0ff"), 1))
            p.drawRect(QRectF(vx0, rect.top(), max(2.0, vx1 - vx0), rect.height()))
        # 选区
        if self.sel_end > self.sel_start:
            sx0 = rect.left() + self.sel_start / dur * rect.width()
            sx1 = rect.left() + self.sel_end / dur * rect.width()
            p.fillRect(QRectF(sx0, rect.top(), max(1.0, sx1 - sx0), rect.height()), SEL_EDGE.darker(160))

    def _draw_ruler(self, p: QPainter):
        r = self.plot_rect()
        rect = QRectF(self.GUTTER, self.OVERVIEW_H, r.width(), self.RULER_H)
        p.fillRect(QRectF(0, self.OVERVIEW_H, self.width(), self.RULER_H), QColor("#0d1216"))
        p.setFont(QFont("Consolas", 8))
        step = _nice_step(self.view_span, rect.width())
        t0 = math.floor(self.view_start / step) * step
        t = t0
        end = self.view_start + self.view_span
        while t <= end + step:
            x = self.time_to_x(t)
            if x >= rect.left() - 1:
                p.setPen(QPen(QColor("#3d4c57")))
                p.drawLine(QPointF(x, self.OVERVIEW_H + self.RULER_H - 6),
                           QPointF(x, self.OVERVIEW_H + self.RULER_H))
                p.setPen(QPen(TEXT))
                p.drawText(QPointF(x + 3, self.OVERVIEW_H + 13), _ruler_text(max(0.0, t), step))
            t += step
        p.setPen(QPen(QColor("#243039")))
        p.drawLine(QPointF(0, self.OVERVIEW_H + self.RULER_H - 1),
                   QPointF(self.width(), self.OVERVIEW_H + self.RULER_H - 1))

    def _draw_lane(self, p: QPainter, ch: int, lanes: int):
        rect = self.lane_rect(ch, lanes)
        p.fillRect(rect, LANE_BG if ch % 2 == 0 else LANE_BG2)
        mid = rect.center().y()
        half = rect.height() / 2.0 - 3

        # 纵向网格
        step = _nice_step(self.view_span, rect.width())
        t = math.floor(self.view_start / step) * step
        end = self.view_start + self.view_span
        p.setPen(QPen(GRID))
        while t <= end:
            x = self.time_to_x(t)
            if x >= rect.left():
                p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            t += step

        # 横向 dB 刻度
        p.setFont(QFont("Consolas", 7))
        for val, label in ((1.0, "0dB"), (0.5, "-6"), (0.25, "-12"), (0.1, "-20"), (0.0, "")):
            for sign in ((1,) if val == 0.0 else (1, -1)):
                y = mid - sign * val * half
                p.setPen(QPen(GRID_STRONG if val == 0.0 else GRID))
                p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
                if val and sign == 1:
                    p.setPen(QPen(TEXT))
                    p.drawText(QPointF(3, y + 3), label)
        p.setPen(QPen(TEXT_LIGHT))
        p.setFont(QFont("Microsoft YaHei UI", 8, QFont.Bold))
        name = "左 L" if (lanes > 1 and ch == 0) else ("右 R" if lanes > 1 else "单声道")
        p.drawText(QPointF(5, rect.top() + 12), name)

        # 波形
        doc = self.doc
        s0 = int(self.view_start * doc.rate)
        s1 = int((self.view_start + self.view_span) * doc.rate)
        s0 = max(0, min(s0, doc.frames))
        s1 = max(s0 + 1, min(s1, doc.frames))
        cols = max(1, int(rect.width()))
        cache = doc.peaks
        B = cache.block
        if (s1 - s0) >= cols * B and cache.count > 0:
            b0 = s0 // B
            b1 = min(cache.count, (s1 + B - 1) // B)
            mn = cache.mins(b0, b1)[:, ch:ch + 1]
            mx = cache.maxs(b0, b1)[:, ch:ch + 1]
        else:
            seg = doc.data[s0:s1, ch:ch + 1]
            mn = mx = seg
        cols_data = _reduce_cols(mn, mx, cols)
        if cols_data is None:
            return
        cmn, cmx = cols_data
        cmn = np.clip(cmn, -1.0, 1.0)
        cmx = np.clip(cmx, -1.0, 1.0)
        poly = QPolygonF()
        left = rect.left()
        for i in range(cols):
            poly.append(QPointF(left + i, mid - float(cmx[i]) * half))
        for i in range(cols - 1, -1, -1):
            poly.append(QPointF(left + i, mid - float(cmn[i]) * half))
        color = CURVE_L if (lanes == 1 or ch == 0) else CURVE_R
        grad = QLinearGradient(0, rect.top(), 0, rect.bottom())
        grad.setColorAt(0.0, color.lighter(120))
        grad.setColorAt(0.5, color)
        grad.setColorAt(1.0, color.darker(130))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(color.darker(150), 1))
        p.drawPolygon(poly)
        p.setBrush(Qt.NoBrush)

        # 选区
        if self.sel_end > self.sel_start:
            x0 = self.time_to_x(self.sel_start)
            x1 = self.time_to_x(self.sel_end)
            p.fillRect(QRectF(x0, rect.top(), max(1.0, x1 - x0), rect.height()), SEL_FILL)
            p.setPen(QPen(SEL_EDGE, 1))
            p.drawLine(QPointF(x0, rect.top()), QPointF(x0, rect.bottom()))
            p.drawLine(QPointF(x1, rect.top()), QPointF(x1, rect.bottom()))

        p.setPen(QPen(QColor("#1c262d")))
        p.drawRect(rect.adjusted(0, 0, -1, -1))

    def _draw_cursor(self, p: QPainter, lanes: int):
        r = self.plot_rect()
        x = self.time_to_x(self.cursor)
        if r.left() - 1 <= x <= r.right() + 1:
            p.setPen(QPen(CURSOR, 1))
            p.drawLine(QPointF(x, self.OVERVIEW_H), QPointF(x, r.bottom()))
            p.setBrush(QBrush(CURSOR))
            p.setPen(Qt.NoPen)
            p.drawPolygon(QPolygonF([QPointF(x - 4, self.OVERVIEW_H),
                                     QPointF(x + 4, self.OVERVIEW_H),
                                     QPointF(x, self.OVERVIEW_H + 6)]))

    # -- 交互 ------------------------------------------------------------
    def _lanes(self) -> int:
        return self.doc.channels if self.doc else 2

    def mousePressEvent(self, event):
        if self.doc is None:
            return
        pos = event.position()
        r = self.plot_rect()
        if pos.y() < self.OVERVIEW_H:
            self._drag = "overview"
            self._jump_overview(pos.x())
            return
        if pos.y() < self.OVERVIEW_H + self.RULER_H:
            t = max(0.0, min(self.x_to_time(pos.x()), self.duration()))
            self.set_cursor(t)
            self._drag = "cursor"
            return
        if event.button() == Qt.MiddleButton:
            self._drag = "pan"
            self._drag_anchor = pos.x()
            self._drag_view0 = self.view_start
            return
        if pos.x() < self.GUTTER:
            return
        t = max(0.0, min(self.x_to_time(pos.x()), self.duration()))
        if event.modifiers() & Qt.ShiftModifier and self.sel_end > self.sel_start:
            self.sel_end = t
            if self.sel_end < self.sel_start:
                self.sel_start, self.sel_end = self.sel_end, self.sel_start
            self.selectionChanged.emit(self.sel_start, self.sel_end)
        else:
            self.set_cursor(t)
            self._drag = "select"
            self.sel_start = self.sel_end = t
        self.update()

    def _jump_overview(self, x: float):
        rect_left = self.GUTTER
        rect_w = max(1, self.width() - self.GUTTER - 1)
        dur = max(self.duration(), 1e-6)
        t = (x - rect_left) / rect_w * dur
        self.set_view(max(0.0, t - self.view_span / 2.0), self.view_span)

    def mouseMoveEvent(self, event):
        if self.doc is None:
            return
        pos = event.position()
        if self._drag == "pan":
            dx = pos.x() - self._drag_anchor
            self.set_view(max(0.0, self._drag_view0 - dx / self.plot_rect().width() * self.view_span),
                          self.view_span)
            return
        if self._drag == "overview":
            self._jump_overview(pos.x())
            return
        if self._drag == "select":
            t = max(0.0, min(self.x_to_time(pos.x()), self.duration()))
            self.sel_end = t
            self.selectionChanged.emit(min(self.sel_start, self.sel_end),
                                       max(self.sel_start, self.sel_end))
            self.update()
            return
        if self._drag == "cursor":
            t = max(0.0, min(self.x_to_time(pos.x()), self.duration()))
            self.set_cursor(t)
            return
        # 悬停提示：显示鼠标处时间
        if self.rect().contains(pos.toPoint()):
            self.setToolTip("时间 %s" % fmt_time(self.x_to_time(pos.x())))

    def mouseReleaseEvent(self, event):
        if self._drag == "select":
            if abs(self.sel_end - self.sel_start) < 1e-5:
                self.sel_start = self.sel_end = 0.0
                self.selectionChanged.emit(0.0, 0.0)
        self._drag = None
        self.update()

    def mouseDoubleClickEvent(self, event):
        if self.doc is not None and event.position().y() > self.OVERVIEW_H + self.RULER_H:
            self.sel_start = 0.0
            self.sel_end = self.duration()
            self.selectionChanged.emit(self.sel_start, self.sel_end)
            self.update()

    def wheelEvent(self, event):
        if self.doc is None:
            return
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.ControlModifier or event.modifiers() & Qt.ShiftModifier:
            self.set_view(max(0.0, self.view_start - delta / 120.0 * self.view_span * 0.15),
                          self.view_span)
        else:
            self.zoom(1.25 if delta > 0 else 1 / 1.25, self.x_to_time(event.position().x()))

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Left:
            self.set_cursor(max(0.0, self.cursor - self.view_span * 0.02))
        elif key == Qt.Key_Right:
            self.set_cursor(min(self.duration(), self.cursor + self.view_span * 0.02))
        elif key == Qt.Key_Home:
            self.set_cursor(0.0)
        elif key == Qt.Key_End:
            self.set_cursor(self.duration())
        elif key == Qt.Key_Space:
            self.playRequested.emit()
        else:
            super().keyPressEvent(event)

    # -- 光标 ------------------------------------------------------------
    def set_cursor(self, t: float, notify: bool = True):
        self.cursor = max(0.0, min(float(t), max(self.duration(), 0.0)))
        if notify:
            self.cursorChanged.emit(self.cursor)
        self.update()

    def set_selection(self, s: float, e: float, notify: bool = True):
        self.sel_start, self.sel_end = max(0.0, s), max(0.0, e)
        if notify:
            self.selectionChanged.emit(self.sel_start, self.sel_end)
        self.update()

    def selection(self):
        if self.sel_end > self.sel_start:
            return self.sel_start, self.sel_end
        return None


class LevelMeter(QWidget):
    """纵向 dB 电平表（带峰值保持与削波指示）。"""

    DB_MIN = -60.0
    DB_MAX = 0.0

    def __init__(self, label="L", color="#37d6a0", parent=None):
        super().__init__(parent)
        self.label = label
        self.color = QColor(color)
        self.peak = 0.0
        self.rms = 0.0
        self.hold = 0.0
        self.clip = False
        self.setMinimumWidth(46)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setToolTip("点击可清除削波指示")

    def set_level(self, peak: float, rms: float):
        self.peak = max(0.0, float(peak))
        self.rms = max(0.0, float(rms))
        self.hold = max(self.hold - 0.02, self.peak)
        if self.peak >= 0.999:
            self.clip = True
        self.update()

    def reset(self):
        self.peak = self.rms = self.hold = 0.0
        self.update()

    def clear_clip(self):
        self.clip = False
        self.update()

    def mousePressEvent(self, event):
        self.clear_clip()

    def _db_y(self, db: float, rect: QRectF) -> float:
        db = max(self.DB_MIN, min(self.DB_MAX, db))
        return rect.bottom() - (db - self.DB_MIN) / (self.DB_MAX - self.DB_MIN) * rect.height()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0d1216"))
        rect = QRectF(14, 16, max(10, self.width() - 22), self.height() - 34)
        p.setPen(QPen(QColor("#1d262d")))
        p.drawRect(rect)
        for db in (0, -6, -12, -20, -30, -40, -50, -60):
            y = self._db_y(db, rect)
            p.setPen(QPen(QColor("#1a232a")))
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            p.setPen(QPen(TEXT))
            p.setFont(QFont("Consolas", 6))
            p.drawText(QPointF(0, y + 3), "%d" % db)

        def bar(value, width_ratio, color, alpha=255):
            if value <= 1e-6:
                return
            db = db_of(value)
            y = self._db_y(db, rect)
            c = QColor(color)
            c.setAlpha(alpha)
            p.setBrush(QBrush(c))
            p.setPen(Qt.NoPen)
            p.drawRect(QRectF(rect.left() + 1, y, (rect.width() - 2) * width_ratio,
                              rect.bottom() - y))

        bar(self.rms, 0.45, self.color)
        bar(self.peak, 1.0, self.color.lighter(115))
        if self.hold > 1e-6:
            y = self._db_y(db_of(self.hold), rect)
            p.setPen(QPen(QColor("#ffffff"), 1))
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        # 刻度标签
        p.setPen(QPen(TEXT_LIGHT))
        p.setFont(QFont("Consolas", 7))
        p.drawText(QRectF(0, 2, self.width(), 12), Qt.AlignCenter, self.label)
        txt = "-∞" if self.hold <= 1e-6 else "%.1f" % db_of(self.hold)
        p.setPen(QPen(QColor("#ff5b5b") if self.clip else TEXT))
        p.setFont(QFont("Consolas", 7))
        p.drawText(QRectF(0, self.height() - 13, self.width(), 12), Qt.AlignCenter,
                   "CLIP" if self.clip else txt)
        p.end()
