from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import threading
import time
import weakref
from collections import OrderedDict
from typing import Optional

from PyQt6.QtGui import QImage, QPixmap

try:
    import cv2

    CV2_AVAILABLE = True
except Exception:
    cv2 = None
    CV2_AVAILABLE = False

try:
    import numpy as np
except Exception:
    np = None


def _frame_to_pixmap(frame) -> Optional[QPixmap]:
    if frame is None:
        return None
    h, w = frame.shape[:2]
    if w <= 0 or h <= 0:
        return None
    qimg = QImage(frame.data, w, h, frame.strides[0], QImage.Format.Format_BGR888)
    pix = QPixmap.fromImage(qimg.copy())
    return pix if not pix.isNull() else None


def _bgr_to_qimage(frame, size: Optional[tuple]) -> Optional[QImage]:
    if frame is None:
        return None
    h, w = frame.shape[:2]
    if w <= 0 or h <= 0:
        return None
    if size is not None:
        tw, th = size
        if tw < w or th < h:
            tw, th = max(1, min(tw, w)), max(1, min(th, h))
            ratio = max(w / tw, h / th)
            interp = cv2.INTER_AREA if ratio >= 1.5 else cv2.INTER_LINEAR
            frame = cv2.resize(frame, (tw, th), interpolation=interp)
            h, w = th, tw
    bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
    qimg = QImage(bgra.data, w, h, bgra.strides[0], QImage.Format.Format_RGB32)
    return qimg.copy()  # detach from the numpy buffer


def _parse_ffprobe_rate(val) -> float:
    if not val:
        return 0.0
    try:
        if isinstance(val, str) and "/" in val:
            num, den = val.split("/", 1)
            den = float(den)
            return float(num) / den if den else 0.0
        return float(val)
    except Exception:
        return 0.0


class _FFmpegCapture:
    """A small cv2.VideoCapture-alike backed by the system `ffmpeg`/`ffprobe`
    binaries (via subprocess pipes of raw BGR24 frames).

    Used as a fallback when `cv2` is importable but its build has no working
    video I/O backend (FFMPEG=NO and GStreamer=NO in
    cv2.getBuildInformation()) - a real situation with minimal distro
    OpenCV packages, e.g. Alpine's `py3-opencv`. Only implements the handful
    of methods `_VideoDecoder` actually calls. Seeking restarts the ffmpeg
    process with `-ss`, so it's noticeably slower than a native backend and
    only approximately frame-accurate - a deliberate, documented trade-off
    for "it plays at all" over performance.
    """

    def __init__(self, path: str):
        self._path = path
        self._proc: Optional[subprocess.Popen] = None
        self._width = 0
        self._height = 0
        self._fps = 0.0
        self._frame_count = 0
        self._frame_bytes = 0
        self._pos = 0
        self._ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not self._ffmpeg or not ffprobe or np is None:
            return
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate",
                 "-show_entries", "format=duration",
                 "-of", "json", path],
                capture_output=True, text=True, timeout=10,
            )
            data = json.loads(proc.stdout or "{}")
            streams = data.get("streams") or [{}]
            s0 = streams[0] if streams else {}
            fmt = data.get("format") or {}
            self._width = int(s0.get("width") or 0)
            self._height = int(s0.get("height") or 0)
            fps = _parse_ffprobe_rate(s0.get("avg_frame_rate")) or _parse_ffprobe_rate(s0.get("r_frame_rate"))
            self._fps = fps if fps > 0 else 30.0
            duration = float(fmt.get("duration") or 0.0)
            self._frame_count = max(0, int(round(duration * self._fps))) if duration > 0 else 0
        except Exception:
            self._width = 0
            self._height = 0
        if self._width > 0 and self._height > 0:
            self._frame_bytes = self._width * self._height * 3
            self._start_process(0.0)

    def _start_process(self, start_time: float):
        self._stop_process()
        cmd = [self._ffmpeg, "-v", "error"]
        if start_time > 0:
            cmd += ["-ss", f"{start_time:.3f}"]
        cmd += ["-i", self._path, "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-vsync", "0", "pipe:1"]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception:
            self._proc = None

    def _stop_process(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass

    def isOpened(self) -> bool:
        return self._width > 0 and self._height > 0 and self._proc is not None

    def get(self, prop):
        if cv2 is None:
            return 0.0
        if prop == cv2.CAP_PROP_FPS:
            return self._fps
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return self._width
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return self._height
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return self._frame_count
        return 0.0

    def set(self, prop, value) -> bool:
        if cv2 is not None and prop == cv2.CAP_PROP_POS_FRAMES:
            target = max(0, int(value))
            start_time = (target / self._fps) if self._fps > 0 else 0.0
            self._start_process(start_time)
            self._pos = target
            return True
        return False

    def grab(self) -> bool:
        ok, _ = self.read()
        return ok

    def read(self):
        if self._proc is None or self._proc.stdout is None or self._frame_bytes <= 0:
            return False, None
        try:
            buf = self._proc.stdout.read(self._frame_bytes)
        except Exception:
            buf = b""
        if not buf or len(buf) < self._frame_bytes:
            return False, None
        frame = np.frombuffer(buf, dtype=np.uint8).reshape((self._height, self._width, 3)).copy()
        self._pos += 1
        return True, frame

    def release(self):
        self._stop_process()


def _open_capture(path: str):
    """Open `path` for frame reading, preferring native cv2.VideoCapture but
    falling back to an `_FFmpegCapture` (system ffmpeg/ffprobe) when cv2's
    build can't open it - see `_FFmpegCapture` for why that happens."""
    cap = None
    if CV2_AVAILABLE:
        try:
            candidate = cv2.VideoCapture(path)
        except Exception:
            candidate = None
        if candidate is not None and candidate.isOpened():
            w = int(candidate.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(candidate.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            if w > 0 and h > 0:
                cap = candidate
            else:
                candidate.release()
        elif candidate is not None:
            candidate.release()
    if cap is None and CV2_AVAILABLE and np is not None and shutil.which("ffmpeg") and shutil.which("ffprobe"):
        fallback = _FFmpegCapture(path)
        if fallback.isOpened():
            print(f"[video] cv2 couldn't open {path!r} - using a system ffmpeg "
                  "subprocess as a (slower) fallback decoder instead.")
            cap = fallback
        else:
            fallback.release()
    return cap


_LIVE_DECODERS: "weakref.WeakSet" = weakref.WeakSet()


@atexit.register
def _close_all_decoders():
    for dec in list(_LIVE_DECODERS):
        try:
            dec.close(join=True)
        except Exception:
            pass


class _VideoInfo:
    __slots__ = ("width", "height", "fps", "frame_count", "duration")

    def __init__(self, width: int, height: int, fps: float, frame_count: int):
        self.width = width
        self.height = height
        self.fps = fps if fps and fps > 0 else 30.0
        self.frame_count = frame_count
        self.duration = (frame_count / self.fps) if (frame_count and self.fps > 0) else 0.0


class _VideoDecoder:
    READAHEAD = 6
    CACHE_FRAMES = 24
    WAIT_S = 0.35  # how long a render will wait for a frame before drawing a stale one

    def __init__(self, path: str, exact: bool = False):
        self.info: Optional[_VideoInfo] = None
        self._cap = None
        # exact=True (offline export): frame_at() blocks until the requested frame is decoded
        # instead of giving up after WAIT_S and returning a nearby stale frame.
        self.exact = bool(exact)
        try:
            cap = _open_capture(path)
        except Exception:
            cap = None
        if cap is not None and cap.isOpened():
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if w > 0 and h > 0:
                self._cap = cap
                self.info = _VideoInfo(w, h, fps, count)
            else:
                cap.release()
        elif cap is not None:
            cap.release()

        self._cond = threading.Condition()
        self._frames: "OrderedDict[int, QImage]" = OrderedDict()
        self._bad: set = set()
        self._size: Optional[tuple] = None
        self._want: Optional[int] = None
        self._prev_want: Optional[int] = None
        self._sequential = False
        self._seq_run = 0  # consecutive forward-stepping requests
        self._last_advance = 0.0
        self._stride = 1.0  # smoothed frames-per-request while playing; read-ahead follows it
        self._loop = True
        self._closed = False
        # decoder-thread-only state
        self._pos = -1  # index of the frame the next cap.read() returns; -1 = unknown
        self._raw: Optional[tuple] = None  # (idx, BGR ndarray): lets a resize re-scale without re-decoding
        self._grab_s = 0.004
        self._seek_s = 0.060

        self._thread: Optional[threading.Thread] = None
        if self.ok:
            self._thread = threading.Thread(
                target=self._run, name=f"czeditor-video-{os.path.basename(path)}", daemon=True
            )
            self._thread.start()
            _LIVE_DECODERS.add(self)

    @property
    def ok(self) -> bool:
        return self._cap is not None and self.info is not None

    # caller side (render thread)
    def frame_at(self, t: float, loop: bool, size: Optional[tuple] = None) -> Optional[QImage]:
        if not self.ok:
            return None
        fps = self.info.fps
        count = self.info.frame_count
        idx = int(max(0.0, t) * fps)
        if count > 0:
            idx = (idx % count) if loop else max(0, min(idx, count - 1))
        else:
            idx = max(0, idx)
        size = self._normalize_size(size)

        with self._cond:
            if self._closed:
                return None
            if size != self._size:
                self._size = size
                self._frames.clear()
                self._bad.clear()
            now = time.monotonic()
            if self._prev_want is not None:
                step = idx - self._prev_want
                if 1 <= step <= 4 and now - self._last_advance < 0.25:
                    self._seq_run += 1
                    self._stride = 0.6 * self._stride + 0.4 * step
                    self._last_advance = now
                elif step != 0:
                    self._seq_run = 0
                    self._last_advance = now if 1 <= step <= 4 else 0.0
            self._sequential = self._seq_run >= 2 and now - self._last_advance < 0.25
            if self.exact:
                self._sequential = True  # export always walks forward: keep reading ahead
                self._stride = max(1.0, self._stride)
            self._prev_want = idx
            self._want = idx
            self._loop = bool(loop)
            frame = self._frames.get(idx)
            if frame is not None:
                self._frames.move_to_end(idx)
            self._cond.notify_all()

            if frame is None and idx not in self._bad:
                deadline = time.monotonic() + (60.0 if self.exact else self.WAIT_S)
                while frame is None and not self._closed and idx not in self._bad:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(min(remaining, 1.0))
                    frame = self._frames.get(idx)
            if frame is None:
                frame = self._nearest_locked(idx)
            return frame

    def _normalize_size(self, size: Optional[tuple]) -> Optional[tuple]:
        if size is None or self.info is None:
            return None
        tw = ((max(1, int(size[0])) + 7) // 8) * 8
        th = ((max(1, int(size[1])) + 7) // 8) * 8
        if tw >= self.info.width and th >= self.info.height:
            return None  # never upscale
        return (min(tw, self.info.width), min(th, self.info.height))

    def _nearest_locked(self, idx: int) -> Optional[QImage]:
        if not self._frames:
            return None
        best = min(self._frames.keys(), key=lambda k: abs(k - idx))
        return self._frames[best]

    # decoder thread
    def _next_target_locked(self) -> Optional[int]:
        want = self._want
        if want is None:
            return None
        if want not in self._frames and want not in self._bad:
            return want
        if self._sequential:
            count = self.info.frame_count
            stride = max(1, min(4, int(round(self._stride))))
            for k in range(1, self.READAHEAD + 1):
                i = want + k * stride
                if count > 0 and i >= count:
                    if not self._loop:
                        break  # a one-shot clip has nothing past its last frame
                    i %= count  # looping playback wraps
                    if i == want:
                        break
                if i not in self._frames and i not in self._bad:
                    return i
        return None

    def _run(self):
        try:
            while True:
                with self._cond:
                    while not self._closed:
                        target = self._next_target_locked()
                        if target is not None:
                            break
                        self._cond.wait()
                    if self._closed:
                        return
                    size = self._size
                frame, ok = self._decode(target, size)
                with self._cond:
                    if self._closed:
                        return
                    if size == self._size:  # size didn't change while we were decoding
                        if ok and frame is not None:
                            self._frames[target] = frame
                            while len(self._frames) > self.CACHE_FRAMES:
                                # drop the entry furthest from where the caller is looking
                                anchor = self._want if self._want is not None else target
                                far = max(self._frames.keys(), key=lambda k: abs(k - anchor))
                                del self._frames[far]
                        else:
                            self._bad.add(target)
                    self._cond.notify_all()
        finally:
            cap, self._cap = self._cap, None
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

    def _decode(self, target: int, size: Optional[tuple]) -> tuple:
        cap = self._cap
        if cap is None:
            return None, False
        try:
            if self._raw is not None and self._raw[0] == target:
                return _bgr_to_qimage(self._raw[1], size), True

            gap = target - self._pos
            if self._pos >= 0 and gap == 0:
                pass
            elif self._pos >= 0 and 0 < gap and gap * self._grab_s <= self._seek_s:
                t0 = time.perf_counter()
                for _ in range(gap):
                    if not cap.grab():
                        self._pos = -1
                        return None, False
                self._grab_s = 0.7 * self._grab_s + 0.3 * ((time.perf_counter() - t0) / gap)
                self._pos = target
            else:
                t0 = time.perf_counter()
                cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                self._seek_s = 0.7 * self._seek_s + 0.3 * (time.perf_counter() - t0)
                self._pos = target

            t0 = time.perf_counter()
            ok, frame = cap.read()
            if not ok or frame is None:
                self._pos = -1
                return None, False
            self._grab_s = 0.9 * self._grab_s + 0.1 * (time.perf_counter() - t0)
            self._pos = target + 1
            self._raw = (target, frame)
            return _bgr_to_qimage(frame, size), True
        except Exception as e:  # a decode hiccup must never kill the thread
            print(f"[video] decode error at frame {target}: {e}")
            self._pos = -1
            return None, False

    def close(self, join: bool = False):
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        if self._thread is None and self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        if join and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)


class VideoCache:
    def __init__(self, exact: bool = False):
        self._exact = bool(exact)
        self._decoders: dict = {}
        self._lock = threading.Lock()

    def clear(self, join: bool = False):
        with self._lock:
            decoders = list(self._decoders.values())
            self._decoders.clear()
        for dec in decoders:
            if dec is not None:
                dec.close(join=join)

    def drop(self, project_dir: Optional[str], uuid_ext: str, join: bool = True) -> None:
        if not project_dir or not uuid_ext:
            return
        key = (project_dir, uuid_ext)
        with self._lock:
            dec = self._decoders.pop(key, None)
        if dec is not None:
            dec.close(join=join)

    def _get_decoder(self, project_dir: Optional[str], uuid_ext: str) -> Optional[_VideoDecoder]:
        if not CV2_AVAILABLE or not project_dir or not uuid_ext:
            return None
        key = (project_dir, uuid_ext)
        with self._lock:
            if key in self._decoders:
                return self._decoders[key]
        path = os.path.join(project_dir, uuid_ext)
        dec = None
        if os.path.isfile(path):
            try:
                candidate = _VideoDecoder(path, exact=self._exact)
                if candidate.ok:
                    dec = candidate
                else:
                    print(
                        f"[video] couldn't open {path!r} with cv2 or a system "
                        "ffmpeg fallback - the OpenCV build in use may be "
                        "missing FFmpeg/video codec support (common with "
                        "distro packages like Alpine's py3-opencv), and "
                        "either `ffmpeg`/`ffprobe` aren't on PATH or they "
                        "couldn't decode this file either. Falling back to a "
                        "'Media offline' placeholder."
                    )
                    dec = None
            except Exception as e:
                print(f"[video] failed to open {path!r}: {e}")
                dec = None
        with self._lock:
            existing = self._decoders.get(key)
            if existing is not None or key in self._decoders:
                if dec is not None:
                    dec.close()  # lost a race with another thread opening the same file
                return existing
            self._decoders[key] = dec
        return dec

    def get_frame(self, project_dir: Optional[str], uuid_ext: str,
                  local_t: float = 0.0, loop: bool = True,
                  size_hint: Optional[tuple] = None) -> Optional[QImage]:
        dec = self._get_decoder(project_dir, uuid_ext)
        if dec is None:
            return None
        return dec.frame_at(local_t, loop, size_hint)

    def duration(self, project_dir: Optional[str], uuid_ext: str) -> Optional[float]:
        dec = self._get_decoder(project_dir, uuid_ext)
        if dec is None or dec.info is None:
            return None
        return dec.info.duration or None


def probe_video_duration(path: str) -> Optional[float]:
    if not CV2_AVAILABLE or not path or not os.path.isfile(path):
        return None
    try:
        cap = _open_capture(path)
    except Exception:
        cap = None
    if cap is None:
        return None
    try:
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        if fps > 0 and count > 0:
            return count / fps
        return None
    except Exception:
        return None
    finally:
        cap.release()


def load_video_thumbnail(path: str) -> Optional[QPixmap]:
    if not CV2_AVAILABLE or not path or not os.path.isfile(path):
        return None
    try:
        cap = _open_capture(path)
    except Exception:
        cap = None
    if cap is None:
        return None
    try:
        if not cap.isOpened():
            return None
        ok, frame = cap.read()
        if not ok:
            return None
        return _frame_to_pixmap(frame)
    except Exception:
        return None
    finally:
        cap.release()
