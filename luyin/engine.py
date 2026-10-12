# -*- coding: utf-8 -*-
"""Luyin Studio 音频引擎层。

职责：
  * 设备枚举（含 WASAPI Loopback = 电脑内录 / 系统声音）
  * 采集（双声道、任意采样率、软件重采样回退、输入增益、电平表）
  * 播放（选区/整段试听、音量、定位）
  * 文档模型（float32 缓冲区 + 峰值缓存，供波形快速绘制）
  * 文件读写（WAV 16/24/32f 自写、MP3 编码 lameenc、MP3/WAV/FLAC/OGG 解码 miniaudio）
"""
from __future__ import annotations

import math
import os
import struct
import threading
import time

import numpy as np

try:
    import pyaudiowpatch as pyaudio
except Exception:  # pragma: no cover - 环境缺失时给出友好提示
    pyaudio = None

try:
    import lameenc
except Exception:  # pragma: no cover
    lameenc = None

try:
    import miniaudio
except Exception:  # pragma: no cover
    miniaudio = None


# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
SAMPLE_RATES = [8000, 11025, 16000, 22050, 32000, 44100, 48000, 64000, 88200, 96000, 192000]
BIT_DEPTHS = [("16 位 PCM", 16), ("24 位 PCM", 24), ("32 位 浮点", 32)]
BUFFER_SIZES = [128, 256, 512, 1024, 2048, 4096, 8192]
MP3_BITRATES = [64, 96, 128, 160, 192, 224, 256, 320]
AUDIO_EXT = ".wav .mp3 .flac .ogg .oga .m4a .aiff .aif"

C_LANE_BG = "#101418"


class AudioError(Exception):
    """面向用户的音频错误。"""


def db_of(x: float) -> float:
    """线性幅度 -> dBFS。"""
    if x <= 1e-9:
        return -120.0
    return 20.0 * math.log10(x)


def fmt_time(seconds: float, ms: bool = True) -> str:
    """秒 -> hh:mm:ss.mmm"""
    if seconds is None or not math.isfinite(seconds):
        return "--:--:--"
    negative = seconds < 0
    seconds = abs(seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    text = "%02d:%02d:%06.3f" % (h, m, s) if ms else "%02d:%02d:%02d" % (h, m, int(s))
    return ("-" if negative else "") + text


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%.1f GB" % n


# --------------------------------------------------------------------------
# 设备
# --------------------------------------------------------------------------
class AudioDevice:
    __slots__ = ("index", "name", "max_in", "max_out", "rate", "hostapi",
                 "is_loopback", "is_default_in", "is_default_out")

    def __init__(self, index, name, max_in, max_out, rate, hostapi, is_loopback=False):
        self.index = index
        self.name = name
        self.max_in = max_in
        self.max_out = max_out
        self.rate = float(rate)
        self.hostapi = hostapi
        self.is_loopback = is_loopback
        self.is_default_in = False
        self.is_default_out = False

    @property
    def kind(self) -> str:
        if self.is_loopback:
            return "系统声音"
        if self.max_in and self.max_out:
            return "输入输出"
        if self.max_in:
            return "输入"
        return "输出"

    @property
    def label(self) -> str:
        tag = "★ " if (self.is_default_in or self.is_default_out) else ""
        loop = "[内录] " if self.is_loopback else ""
        return "%s%s%s (%s, %.0f Hz)" % (tag, loop, self.name, self.hostapi, self.rate)

    def __repr__(self):  # pragma: no cover
        return "<AudioDevice %d %s>" % (self.index, self.name)


class AudioSystem:
    """PyAudio 实例与设备清单的单例包装。"""

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls) -> "AudioSystem":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = AudioSystem()
        return cls._instance

    def __init__(self):
        if pyaudio is None:
            raise AudioError("未找到音频库 PyAudioWPatch，无法访问声卡。")
        self._pa = pyaudio.PyAudio()
        self.inputs: list[AudioDevice] = []
        self.outputs: list[AudioDevice] = []
        self.all_devices: list[AudioDevice] = []
        self.refresh()

    # -- 枚举 ------------------------------------------------------------
    def refresh(self):
        pa = self._pa
        devices = []
        default_in = default_out = None
        try:
            default_in = pa.get_default_input_device_info()["index"]
        except Exception:
            pass
        try:
            default_out = pa.get_default_output_device_info()["index"]
        except Exception:
            pass
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            host = pa.get_host_api_info_by_index(d.get("hostApi", d.get("hostapi")))["name"]
            dev = AudioDevice(i, d["name"], int(d["maxInputChannels"]),
                              int(d["maxOutputChannels"]), d["defaultSampleRate"],
                              host, bool(d.get("isLoopbackDevice", False)))
            devices.append(dev)
        self.all_devices = devices
        self.inputs = [d for d in devices if d.max_in > 0]
        self.outputs = [d for d in devices if d.max_out > 0]

        # 输入默认值：优先“系统声音”内录（默认输出设备的 Loopback）
        out_name = None
        if default_out is not None:
            try:
                out_name = pa.get_device_info_by_index(default_out)["name"]
            except Exception:
                out_name = None
        preferred = None
        if out_name:
            for d in self.inputs:
                if d.is_loopback and d.name.startswith(out_name):
                    preferred = d
                    break
        if preferred is None:
            for d in self.inputs:
                if d.is_loopback and "WASAPI" in d.hostapi:
                    preferred = d
                    break
        if preferred is None and default_in is not None:
            for d in self.inputs:
                if d.index == default_in:
                    preferred = d
                    break
        self.default_input = preferred or (self.inputs[0] if self.inputs else None)
        if self.default_input:
            self.default_input.is_default_in = True

        # 输出默认值：MME 声音映射器（支持任意采样率，回放最稳）
        preferred_out = None
        for d in self.outputs:
            if "声音映射器" in d.name or "Sound Mapper" in d.name:
                preferred_out = d
                break
        if preferred_out is None and default_out is not None:
            for d in self.outputs:
                if d.index == default_out:
                    preferred_out = d
                    break
        self.default_output = preferred_out or (self.outputs[0] if self.outputs else None)
        if self.default_output:
            self.default_output.is_default_out = True

    def native_rate(self, index: int) -> float:
        for d in self.all_devices:
            if d.index == index:
                return d.rate
        return 44100.0

    def device(self, index: int) -> AudioDevice | None:
        for d in self.all_devices:
            if d.index == index:
                return d
        return None

    def terminate(self):
        try:
            self._pa.terminate()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 软件重采样（设备不支持目标采样率时的回退）
# --------------------------------------------------------------------------
class LinearResampler:
    """流式线性插值重采样，保持块与块之间的相位连续。"""

    def __init__(self, src_rate: float, dst_rate: float, channels: int):
        self.step = float(src_rate) / float(dst_rate)
        self.channels = channels
        self._tail = np.zeros((0, channels), np.float32)
        self._pos = 0.0

    @property
    def active(self) -> bool:
        return abs(self.step - 1.0) > 1e-9

    def reset(self):
        self._tail = np.zeros((0, self.channels), np.float32)
        self._pos = 0.0

    def process(self, block: np.ndarray) -> np.ndarray:
        if not self.active:
            return block
        if self._tail.size:
            data = np.concatenate((self._tail, block), axis=0)
        else:
            data = block
        n = data.shape[0]
        if n < 2:
            self._tail = data.copy()
            return np.zeros((0, self.channels), np.float32)
        avail = (n - 2) - self._pos
        if avail < 0:
            self._tail = data.copy()
            return np.zeros((0, self.channels), np.float32)
        m = int(math.floor(avail / self.step)) + 1
        idx = self._pos + self.step * np.arange(m, dtype=np.float64)
        i0 = idx.astype(np.int64)
        frac = (idx - i0).astype(np.float32).reshape(-1, 1)
        out = data[i0] * (1.0 - frac) + data[i0 + 1] * frac
        keep = int(i0[-1])
        self._tail = data[keep:].copy()
        self._pos = float(idx[-1]) - keep
        return out.astype(np.float32)


def convert_channels(arr: np.ndarray, src_ch: int, dst_ch: int) -> np.ndarray:
    """声道数转换（1<->2 常用）。"""
    if src_ch == dst_ch:
        return arr
    if src_ch == 1 and dst_ch >= 2:
        return np.repeat(arr, dst_ch, axis=1)
    if src_ch >= 2 and dst_ch == 1:
        return arr.mean(axis=1, keepdims=True).astype(np.float32)
    if src_ch > dst_ch:
        return arr[:, :dst_ch]
    return np.repeat(arr[:, -1:], dst_ch - src_ch, axis=1).astype(np.float32)


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
class CaptureEngine:
    """输入采集。可只开电平表（试音），也可写入录音缓冲。"""

    def __init__(self, system: AudioSystem):
        self.sys = system
        self._pa = system._pa
        self._stream = None
        self._lock = threading.Lock()
        self._blocks: list[np.ndarray] = []
        self._peak = np.zeros(2, np.float32)
        self._rms = np.zeros(2, np.float32)
        self._recording = False
        self._paused = False
        self._gain = 1.0
        self._resampler: LinearResampler | None = None
        self.frames = 0
        self.overflows = 0
        self.req_rate = 48000
        self.req_channels = 2
        self.dev_rate = 48000
        self.dev_channels = 2
        self.device_index = -1

    # -- 状态 ------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._stream is not None

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def seconds(self) -> float:
        return self.frames / float(self.req_rate) if self.req_rate else 0.0

    @property
    def resampling(self) -> bool:
        return self.dev_rate != self.req_rate

    def set_gain_db(self, gain_db: float):
        self._gain = 10.0 ** (float(gain_db) / 20.0)

    # -- 打开/关闭 -------------------------------------------------------
    def open(self, device_index: int, rate: int, channels: int,
             blocksize: int = 1024, gain_db: float = 0.0) -> None:
        if self._stream is not None:
            self.close()
        dev = self.sys.device(device_index)
        if dev is None:
            raise AudioError("无效的输入设备（#%s）。" % device_index)
        native_rate = int(round(dev.rate))
        native_ch = max(1, min(2, dev.max_in))

        attempt: list[tuple[int, int]] = []
        for cand in ((rate, channels), (native_rate, channels),
                     (rate, native_ch), (native_rate, native_ch)):
            if cand not in attempt:
                attempt.append(cand)

        last_err = None
        stream = None
        used = None
        for r, c in attempt:
            try:
                stream = self._pa.open(
                    format=pyaudio.paFloat32, channels=c, rate=r, input=True,
                    input_device_index=device_index, frames_per_buffer=blocksize,
                    stream_callback=self._callback)
                used = (r, c)
                break
            except Exception as exc:  # 尝试下一个组合
                last_err = exc
                stream = None
        if stream is None:
            raise AudioError(
                "无法打开输入设备「%s」：\n%s\n\n建议：把采样率改为 %.0f Hz。" % (
                    dev.name, last_err, dev.rate))

        self.device_index = device_index
        self.req_rate = int(rate)
        self.req_channels = int(channels)
        self.dev_rate, self.dev_channels = used
        self._gain = 10.0 ** (float(gain_db) / 20.0)
        self._resampler = LinearResampler(self.dev_rate, self.req_rate, self.dev_channels) \
            if self.dev_rate != self.req_rate else None
        self.frames = 0
        self.overflows = 0
        self._blocks = []
        with self._lock:
            self._peak[:] = 0.0
            self._rms[:] = 0.0
        self._stream = stream
        self._recording = False
        self._paused = False
        stream.start_stream()

    def close(self):
        if self._stream is not None:
            try:
                self._stream.stop_stream()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._recording = False
        self._paused = False

    # -- 录音控制 --------------------------------------------------------
    def start_recording(self):
        if self._stream is None:
            raise AudioError("采集流尚未打开。")
        self.frames = 0
        self.overflows = 0
        self._blocks = []
        if self._resampler:
            self._resampler.reset()
        self._recording = True
        self._paused = False

    def pause(self):
        if self._recording:
            self._paused = True

    def resume(self):
        if self._recording:
            self._paused = False

    def take_blocks(self) -> np.ndarray | None:
        """取出尚未被 GUI 消费的录音数据。"""
        with self._lock:
            if not self._blocks:
                return None
            blocks, self._blocks = self._blocks, []
        if len(blocks) == 1:
            return blocks[0]
        return np.concatenate(blocks, axis=0)

    def levels(self):
        """返回 ((peakL, peakR), (rmsL, rmsR))，并重置峰值。"""
        with self._lock:
            peak = self._peak.copy()
            rms = self._rms.copy()
            self._peak[:] = 0.0
        return peak, rms

    # -- 回调 ------------------------------------------------------------
    def _callback(self, in_data, frame_count, time_info, status):
        if status:
            self.overflows += 1
        arr = np.frombuffer(in_data, dtype=np.float32)
        if arr.size:
            arr = arr.reshape(-1, self.dev_channels)
            if self._gain != 1.0:
                arr = arr * self._gain
            if self._resampler is not None:
                arr = self._resampler.process(arr)
            if arr.size == 0:
                return (None, pyaudio.paContinue)
            if self.dev_channels != self.req_channels:
                arr = convert_channels(arr, self.dev_channels, self.req_channels)
            # 电平表
            pk = np.abs(arr).max(axis=0)
            rms = np.sqrt(np.mean(np.square(arr, dtype=np.float32), axis=0))
            with self._lock:
                if arr.shape[1] == 1:
                    self._peak[:] = np.maximum(self._peak, np.repeat(pk, 2))
                    self._rms[:] = np.maximum(self._rms * 0.72, np.repeat(rms, 2))
                else:
                    self._peak[:] = np.maximum(self._peak, pk[:2])
                    self._rms[:] = np.maximum(self._rms * 0.72, rms[:2])
            if self._recording and not self._paused:
                with self._lock:
                    self._blocks.append(arr.copy())
                    self.frames += arr.shape[0]
        return (None, pyaudio.paContinue)


# --------------------------------------------------------------------------
# 播放
# --------------------------------------------------------------------------
class PlaybackEngine:
    """回放已录制/已打开的数据，支持定位、选区循环与音量。"""

    def __init__(self, system: AudioSystem):
        self.sys = system
        self._pa = system._pa
        self._stream = None
        self._lock = threading.Lock()
        self._data: np.ndarray | None = None
        self._rate = 48000
        self._pos = 0
        self._end = 0
        self._loop = False
        self._volume = 1.0
        self._playing = False
        self._device = -1
        self._native_ch = 2
        self.finished = False

    # -- 状态 ------------------------------------------------------------
    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def position(self) -> int:
        return self._pos

    @property
    def position_seconds(self) -> float:
        return self._pos / float(self._rate) if self._rate else 0.0

    # -- 控制 ------------------------------------------------------------
    def set_volume(self, v: float):
        self._volume = max(0.0, min(4.0, float(v)))

    def play(self, data: np.ndarray, rate: int, device_index: int,
             start_frame: int = 0, end_frame: int | None = None, loop: bool = False):
        self.stop()
        if data is None or data.shape[0] == 0:
            raise AudioError("没有可播放的音频数据。")
        self._data = data
        self._rate = int(rate)
        self._pos = max(0, min(int(start_frame), data.shape[0] - 1))
        self._end = data.shape[0] if end_frame is None else max(self._pos + 1, min(int(end_frame), data.shape[0]))
        self._loop = bool(loop)
        self.finished = False
        self._native_ch = data.shape[1]

        dev = self.sys.device(device_index)
        candidates = [device_index]
        fallback = self.sys.default_output.index if self.sys.default_output else None
        if fallback is not None and fallback != device_index:
            candidates.append(fallback)

        last_err = None
        for idx in candidates:
            try:
                stream = self._pa.open(
                    format=pyaudio.paFloat32, channels=max(1, min(2, data.shape[1])),
                    rate=self._rate, output=True, output_device_index=idx,
                    frames_per_buffer=1024, stream_callback=self._callback)
                self._stream = stream
                self._device = idx
                break
            except Exception as exc:
                last_err = exc
                self._stream = None
        if self._stream is None:
            name = dev.name if dev else device_index
            raise AudioError("无法打开输出设备「%s」：\n%s" % (name, last_err))
        self._playing = True
        self._stream.start_stream()

    def stop(self):
        stream, self._stream = self._stream, None
        self._playing = False
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def seek(self, frame: int):
        with self._lock:
            self._pos = max(0, min(int(frame), (self._data.shape[0] - 1) if self._data is not None else 0))

    # -- 回调 ------------------------------------------------------------
    def _callback(self, out_data, frame_count, time_info, status):
        data = self._data
        if data is None or not self._playing:
            return (b"\x00" * (frame_count * 2 * 4), pyaudio.paComplete)
        with self._lock:
            pos = self._pos
            end = self._end
            n = min(frame_count, end - pos)
            if n <= 0:
                if self._loop:
                    pos = 0
                    self._pos = 0
                    n = min(frame_count, end)
                else:
                    self._playing = False
                    self.finished = True
                    return (b"\x00" * (frame_count * 2 * 4), pyaudio.paComplete)
            chunk = data[pos:pos + n]
            self._pos = pos + n
        out = np.zeros((frame_count, 2), np.float32)
        if chunk.shape[1] == 1:
            out[:n, 0] = chunk[:, 0]
            out[:n, 1] = chunk[:, 0]
        else:
            out[:n, :] = chunk[:, :2]
        out *= self._volume
        np.clip(out, -1.0, 1.0, out=out)
        return (out.tobytes(), pyaudio.paContinue)


# --------------------------------------------------------------------------
# 文档模型
# --------------------------------------------------------------------------
class PeakCache:
    """按固定块长保存每声道 min/max，用于缩略波形快速绘制。"""

    def __init__(self, channels: int, block: int = 256):
        self.channels = int(channels)
        self.block = int(block)
        self._cap = 8192
        self._mins = np.zeros((self._cap, self.channels), np.float32)
        self._maxs = np.zeros((self._cap, self.channels), np.float32)
        self.count = 0
        self._tail = np.zeros((0, self.channels), np.float32)

    def clear(self):
        self.count = 0
        self._tail = np.zeros((0, self.channels), np.float32)

    def _ensure(self, n: int):
        if n <= self._cap:
            return
        cap = self._cap
        while cap < n:
            cap *= 2
        mins = np.zeros((cap, self.channels), np.float32)
        mins[:self.count] = self._mins[:self.count]
        maxs = np.zeros((cap, self.channels), np.float32)
        maxs[:self.count] = self._maxs[:self.count]
        self._mins, self._maxs, self._cap = mins, maxs, cap

    def feed(self, arr: np.ndarray):
        if arr is None or arr.size == 0:
            return
        if self._tail.size:
            arr = np.concatenate((self._tail, arr), axis=0)
        nb = arr.shape[0] // self.block
        if nb:
            seg = arr[:nb * self.block].reshape(nb, self.block, self.channels)
            self._ensure(self.count + nb)
            self._mins[self.count:self.count + nb] = seg.min(axis=1)
            self._maxs[self.count:self.count + nb] = seg.max(axis=1)
            self.count += nb
        self._tail = arr[nb * self.block:].copy()

    def mins(self, b0: int, b1: int) -> np.ndarray:
        b0 = max(0, min(b0, self.count))
        b1 = max(b0, min(b1, self.count))
        return self._mins[b0:b1]

    def maxs(self, b0: int, b1: int) -> np.ndarray:
        b0 = max(0, min(b0, self.count))
        b1 = max(b0, min(b1, self.count))
        return self._maxs[b0:b1]


class AudioDoc:
    """一段音频：float32 [frames, channels]，带增量峰值缓存。"""

    def __init__(self, channels: int = 2, rate: int = 48000):
        self.channels = int(channels)
        self.rate = int(rate)
        self._cap = 1 << 16
        self._buf = np.zeros((self._cap, self.channels), np.float32)
        self._n = 0
        self.peaks = PeakCache(self.channels, 256)
        self.path: str | None = None
        self.dirty = False
        self.recorded = False

    # -- 数据 ------------------------------------------------------------
    @property
    def data(self) -> np.ndarray:
        return self._buf[:self._n]

    @property
    def frames(self) -> int:
        return self._n

    @property
    def seconds(self) -> float:
        return self._n / float(self.rate) if self.rate else 0.0

    def clear(self):
        self._n = 0
        self.peaks.clear()
        self.path = None
        self.dirty = False
        self.recorded = False

    def append(self, arr: np.ndarray):
        if arr is None or arr.size == 0:
            return
        if arr.shape[1] != self.channels:
            arr = convert_channels(arr, arr.shape[1], self.channels)
        need = self._n + arr.shape[0]
        if need > self._cap:
            cap = self._cap
            while cap < need:
                cap *= 2
            buf = np.zeros((cap, self.channels), np.float32)
            buf[:self._n] = self._buf[:self._n]
            self._buf, self._cap = buf, cap
        self._buf[self._n:need] = arr
        self._n = need
        self.peaks.feed(arr)
        self.dirty = True

    def set_data(self, arr: np.ndarray, rate: int | None = None, path: str | None = None):
        if rate:
            self.rate = int(rate)
        self.channels = arr.shape[1]
        self._cap = max(1 << 16, arr.shape[0])
        self._buf = np.zeros((self._cap, self.channels), np.float32)
        self._buf[:arr.shape[0]] = arr
        self._n = arr.shape[0]
        self.peaks = PeakCache(self.channels, 256)
        self.peaks.feed(arr)
        self.path = path
        self.dirty = False

    def replace(self, arr: np.ndarray):
        """编辑后整体替换（保持 rate）。"""
        self.set_data(arr, rate=self.rate, path=self.path)

    # -- 便捷属性 --------------------------------------------------------
    @property
    def duration(self) -> float:
        return self.seconds

    def peak_abs(self) -> float:
        if self._n == 0:
            return 0.0
        return float(np.abs(self.data).max())

    def read(self, sample: int) -> np.ndarray | None:
        if 0 <= sample < self._n:
            return self.data[sample]
        return None


# --------------------------------------------------------------------------
# 文件读写
# --------------------------------------------------------------------------
def _to_int16_bytes(data: np.ndarray) -> bytes:
    return (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def save_wav(path: str, data: np.ndarray, rate: int, bit_depth: int = 16):
    """自写 WAV（支持 16/24 位 PCM 与 32 位浮点）。"""
    ch = data.shape[1]
    if bit_depth == 16:
        payload = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        fmt_tag, bits, block_align = 1, 16, ch * 2
    elif bit_depth == 24:
        ints = (np.clip(data, -1.0, 1.0) * 8388607.0).astype("<i4")
        payload = ints.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
        fmt_tag, bits, block_align = 1, 24, ch * 3
    elif bit_depth == 32:
        payload = data.astype("<f4").tobytes()
        fmt_tag, bits, block_align = 3, 32, ch * 4
    else:
        raise AudioError("不支持的位深：%s" % bit_depth)

    byte_rate = rate * block_align
    header = b"RIFF" + struct.pack("<I", 36 + len(payload)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, fmt_tag, ch, rate, byte_rate,
                                    block_align, bits)
    header += b"data" + struct.pack("<I", len(payload))
    with open(path, "wb") as f:
        f.write(header)
        f.write(payload)
    return os.path.getsize(path)


def _id3v23(tags: dict) -> bytes:
    """生成 UTF-16 编码的 ID3v2.3 标签（兼容中文）。"""
    frames = b""

    def text_frame(fid: str, value: str):
        if not value:
            return b""
        body = b"\x01" + "\ufeff".encode("utf-16-be") + value.encode("utf-16-be")
        return fid.encode("ascii") + struct.pack(">I", len(body)) + b"\x00\x00" + body

    for fid, key in (("TIT2", "title"), ("TPE1", "artist"), ("TALB", "album"),
                     ("TYER", "year"), ("TCON", "genre"), ("TENC", "encoder"),
                     ("TSSE", "encoder")):
        frames += text_frame(fid, (tags or {}).get(key, "") or "")
    if not frames:
        return b""
    size = len(frames)
    # synchsafe
    b1 = (size >> 21) & 0x7F
    b2 = (size >> 14) & 0x7F
    b3 = (size >> 7) & 0x7F
    b4 = size & 0x7F
    return b"ID3\x03\x00\x00" + bytes([b1, b2, b3, b4]) + frames


class Mp3StreamEncoder:
    """分块 MP3 编码器，便于长文件导出时显示进度。"""

    def __init__(self, rate: int, channels: int, bitrate: int = 192,
                 quality: int = 2, mode: str = "joint"):
        if lameenc is None:
            raise AudioError("未找到 MP3 编码库 lameenc。")
        self.mode = mode
        self.channels = 1 if (mode == "mono" and channels > 1) else int(channels)
        self.encoder = lameenc.Encoder()
        self.encoder.set_bit_rate(int(bitrate))
        self.encoder.set_in_sample_rate(int(rate))
        self.encoder.set_channels(self.channels)
        self.encoder.set_quality(int(quality))
        self.encoder.silence()

    def encode(self, data: np.ndarray) -> bytes:
        if self.mode == "mono" and data.shape[1] > 1:
            data = data.mean(axis=1, keepdims=True)
        return bytes(self.encoder.encode(_to_int16_bytes(data)))

    def flush(self) -> bytes:
        return bytes(self.encoder.flush())


def encode_mp3(data: np.ndarray, rate: int, bitrate: int = 192, quality: int = 2,
               mode: str = "joint", tags: dict | None = None) -> bytes:
    """float32 [-1,1] -> MP3 字节流。"""
    enc = Mp3StreamEncoder(rate, data.shape[1], bitrate=bitrate, quality=quality, mode=mode)
    out = enc.encode(data) + enc.flush()
    if tags:
        out = _id3v23(tags) + out
    return out


def save_mp3(path: str, data: np.ndarray, rate: int, bitrate: int = 192,
             quality: int = 2, mode: str = "joint", tags: dict | None = None) -> int:
    blob = encode_mp3(data, rate, bitrate=bitrate, quality=quality, mode=mode, tags=tags)
    with open(path, "wb") as f:
        f.write(blob)
    return len(blob)


def load_audio(path: str) -> tuple[np.ndarray, int, int]:
    """读取音频文件 -> (float32 [frames, channels], rate, bit_depth)。"""
    if miniaudio is not None:
        try:
            info = miniaudio.get_file_info(path)
            dec = miniaudio.decode_file(
                path, output_format=miniaudio.SampleFormat.FLOAT32,
                nchannels=max(1, int(info.nchannels)),
                sample_rate=int(info.sample_rate))
            arr = np.frombuffer(dec.samples, dtype=np.float32)
            ch = max(1, int(dec.nchannels))
            arr = arr.reshape(-1, ch).copy()
            np.clip(arr, -1.0, 1.0, out=arr)
            return arr, int(dec.sample_rate), 32
        except Exception as exc:
            raise AudioError("无法解码文件：\n%s\n（%s）" % (path, exc))
    # 兜底：仅 WAV
    import wave
    with wave.open(path, "rb") as w:
        ch = w.getnchannels()
        rate = w.getframerate()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 1:
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        ints = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8)
                | (b[:, 2].astype(np.int32) << 16))
        ints = np.where(ints >= 1 << 23, ints - (1 << 24), ints)
        arr = ints.astype(np.float32) / 8388608.0
    else:
        arr = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    return arr.reshape(-1, ch).copy(), rate, width * 8
