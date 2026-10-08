from __future__ import annotations

import array
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtMultimedia import QAudioDecoder, QAudioFormat

try:
    import numpy as np
except Exception:  # numpy is optional; everything below has a pure-Python fallback
    np = None

try:
    import audioop
except ImportError:
    audioop = None

PEAKS_PER_SEC = 100

_BYTES_PER_SAMPLE = {
    QAudioFormat.SampleFormat.UInt8: 1,
    QAudioFormat.SampleFormat.Int16: 2,
    QAudioFormat.SampleFormat.Int32: 4,
    QAudioFormat.SampleFormat.Float: 4,
}
_FULL_SCALE = {1: 128.0, 2: 32768.0, 4: 2147483648.0}
_ARRAY_TYPECODE = {1: "B", 2: "h", 4: "i"}


def _buffer_bytes(buf) -> bytes:
    try:
        n = buf.byteCount()
    except Exception:
        return b""
    if n <= 0:
        return b""
    ptr = None
    for getter in ("constData", "data"):
        try:
            ptr = getattr(buf, getter)()
            break
        except Exception:
            ptr = None
    if ptr is None:
        return b""
    try:
        ptr.setsize(n)
    except Exception:
        pass
    try:
        return bytes(ptr)
    except Exception:
        pass
    try:
        return ptr.asstring(n)
    except Exception:
        return b""


def _peak_of_fragment(frag: bytes, sample_format, bps: int) -> float:
    if not frag:
        return 0.0
    if sample_format == QAudioFormat.SampleFormat.Float:
        try:
            usable = len(frag) - (len(frag) % 4)
            if usable <= 0:
                return 0.0
            arr = array.array("f")
            arr.frombytes(frag[:usable])
            return min(1.0, max((abs(v) for v in arr), default=0.0))
        except Exception:
            return 0.0

    if audioop is not None:
        try:
            data = frag
            if sample_format == QAudioFormat.SampleFormat.UInt8:
                data = audioop.bias(data, 1, -128)
            usable = len(data) - (len(data) % bps)
            if usable <= 0:
                return 0.0
            peak = audioop.max(data[:usable], bps)
            return min(1.0, peak / _FULL_SCALE.get(bps, 32768.0))
        except Exception:
            pass

    try:
        type_code = _ARRAY_TYPECODE.get(bps)
        if not type_code:
            return 0.0
        usable = len(frag) - (len(frag) % bps)
        if usable <= 0:
            return 0.0
        arr = array.array(type_code)
        arr.frombytes(frag[:usable])
        if not arr:
            return 0.0
        if sample_format == QAudioFormat.SampleFormat.UInt8:
            peak = max(abs(v - 128) for v in arr)
        else:
            peak = max(abs(v) for v in arr)
        return min(1.0, peak / _FULL_SCALE.get(bps, 32768.0))
    except Exception:
        return 0.0


_NP_DTYPE = {}
if np is not None:
    _NP_DTYPE = {
        QAudioFormat.SampleFormat.UInt8: (np.uint8, 128.0),
        QAudioFormat.SampleFormat.Int16: (np.dtype("<i2"), 32768.0),
        QAudioFormat.SampleFormat.Int32: (np.dtype("<i4"), 2147483648.0),
        QAudioFormat.SampleFormat.Float: (np.dtype("<f4"), 1.0),
    }


def _compute_peaks_np(raw: bytes, sample_format, sample_rate: int, channels: int):
    spec = _NP_DTYPE.get(sample_format)
    if spec is None:
        return None
    dtype, full_scale = spec
    channels = max(1, channels)
    itemsize = np.dtype(dtype).itemsize
    n_frames = len(raw) // (itemsize * channels)
    if n_frames <= 0:
        return None
    data = np.frombuffer(raw, dtype=dtype, count=n_frames * channels)
    chunk_frames = max(1, sample_rate // PEAKS_PER_SEC)
    row = chunk_frames * channels
    n_full = n_frames // chunk_frames
    out = np.empty(n_full + (1 if n_frames % chunk_frames else 0), dtype=np.float32)
    BLOCK = 2048  # chunks per pass: keeps the float temporary small
    for b0 in range(0, n_full, BLOCK):
        b1 = min(n_full, b0 + BLOCK)
        blk = data[b0 * row:b1 * row].reshape(b1 - b0, row).astype(np.float32)
        if dtype == np.uint8:
            blk -= 128.0
        np.abs(blk, out=blk)
        out[b0:b1] = blk.max(axis=1)
    if n_frames % chunk_frames:
        tail = data[n_full * row:].astype(np.float32)
        if dtype == np.uint8:
            tail -= 128.0
        out[n_full] = np.abs(tail).max()
    out /= full_scale
    np.minimum(out, 1.0, out=out)
    return out, sample_rate / chunk_frames


def _compute_peaks(raw: bytes, sample_format, sample_rate: int, channels: int):
    if not raw or sample_format is None or sample_rate <= 0:
        return None
    if np is not None:
        try:
            res = _compute_peaks_np(raw, sample_format, sample_rate, channels)
            if res is not None:
                return res
        except Exception:
            pass  # fall through to the pure-Python path
    bps = _BYTES_PER_SAMPLE.get(sample_format)
    if not bps:
        return None
    channels = max(1, channels)
    frame_bytes = bps * channels
    n_frames = len(raw) // frame_bytes
    if n_frames <= 0:
        return None
    chunk_frames = max(1, sample_rate // PEAKS_PER_SEC)
    peaks = []
    for i in range(0, n_frames, chunk_frames):
        j = min(n_frames, i + chunk_frames)
        start = i * frame_bytes
        end = j * frame_bytes
        peaks.append(_peak_of_fragment(raw[start:end], sample_format, bps))
    if not peaks:
        return None
    return peaks, sample_rate / chunk_frames


def raster_peak_columns(cols, height: int, mid_y: float, amp: float, color: QColor) -> Optional[QImage]:
    if np is None or len(cols) == 0 or height <= 0:
        return None
    wv = len(cols)
    rows = (np.arange(height, dtype=np.float32) + 0.5)[:, None]
    half = (amp * np.clip(cols, 0.0, 1.0))[None, :] + 0.5  # +0.5: silence still draws a 1px baseline
    mask = (np.abs(rows - mid_y) <= half) & (cols[None, :] >= 0.0)
    a = color.alpha()
    pm = (color.blue() * a // 255, color.green() * a // 255, color.red() * a // 255, a)  # BGRA, premultiplied
    buf = np.zeros((height, wv, 4), dtype=np.uint8)
    buf[mask] = pm
    return QImage(buf.data, wv, height, wv * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()


class WaveformCache(QObject):
    peaks_ready = pyqtSignal(str)
    _peaks_computed = pyqtSignal(object, str, object)  # worker thread -> GUI thread

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._cache: dict = {}
        self._decoders: dict = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="czeditor-peaks")
        self._peaks_computed.connect(self._finish)

    def clear(self):
        for decoder in list(self._decoders.values()):
            try:
                decoder.stop()
            except Exception:
                pass
            try:
                decoder.deleteLater()
            except Exception:
                pass
        self._decoders.clear()
        self._cache.clear()

    def get(self, project_dir: Optional[str], uuid_ext: Optional[str]):
        if not project_dir or not uuid_ext:
            return None
        key = (project_dir, uuid_ext)
        if key not in self._cache:
            self._cache[key] = "pending"
            self._start_decode(key, project_dir, uuid_ext)
            return None
        cached = self._cache[key]
        return None if cached == "pending" else cached

    def _start_decode(self, key, project_dir: str, uuid_ext: str):
        path = os.path.join(project_dir, uuid_ext)
        if not os.path.isfile(path):
            self._finish(key, uuid_ext, None)
            return
        try:
            decoder = QAudioDecoder(self)
            decoder.setSource(QUrl.fromLocalFile(path))
        except Exception:
            self._finish(key, uuid_ext, None)
            return

        state = {"raw": bytearray(), "sample_rate": 44100, "channels": 1, "sample_format": None}

        def on_buffer_ready():
            try:
                if not decoder.bufferAvailable():
                    return
                buf = decoder.read()
            except Exception:
                return
            if buf is None:
                return
            try:
                fmt = buf.format()
                state["sample_rate"] = fmt.sampleRate() or state["sample_rate"]
                state["channels"] = max(1, fmt.channelCount())
                state["sample_format"] = fmt.sampleFormat()
            except Exception:
                pass
            data = _buffer_bytes(buf)
            if data:
                state["raw"].extend(data)

        def on_finished():
            raw = bytes(state["raw"])
            fmt, sr, ch = state["sample_format"], state["sample_rate"], state["channels"]
            state["raw"] = bytearray()  # the decoder is done; drop the big buffer

            def work():
                try:
                    res = _compute_peaks(raw, fmt, sr, ch)
                except Exception:
                    res = None
                try:
                    self._peaks_computed.emit(key, uuid_ext, res)
                except RuntimeError:
                    pass  # cache destroyed during shutdown

            try:
                self._pool.submit(work)
            except RuntimeError:  # pool already shut down
                pass

        def on_error(*_args):
            self._finish(key, uuid_ext, None)

        decoder.bufferReady.connect(on_buffer_ready)
        decoder.finished.connect(on_finished)
        connected = False
        for signal_name in ("errorOccurred", "error"):
            try:
                getattr(decoder, signal_name).connect(on_error)
                connected = True
                break
            except Exception:
                continue
        if not connected:
            pass  # best-effort; a stuck "pending" just means no waveform is drawn

        self._decoders[key] = decoder
        try:
            decoder.start()
        except Exception:
            self._finish(key, uuid_ext, None)

    def _finish(self, key, uuid_ext: str, result):
        self._cache[key] = result
        decoder = self._decoders.pop(key, None)
        if decoder is not None:
            try:
                decoder.deleteLater()
            except Exception:
                pass
        self.peaks_ready.emit(uuid_ext)
