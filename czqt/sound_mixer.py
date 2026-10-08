from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

try:
    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    MIXER_AVAILABLE = True
except Exception:  # ImportError, or a native lib failing to load
    np = None  # type: ignore
    sd = None  # type: ignore
    sf = None  # type: ignore
    MIXER_AVAILABLE = False

from .model import Project, Scene

BLOCK_SIZE = 2048  # larger block = more buffer headroom against ALSA underruns
_PENDING = object()  # sentinel: this source is still being decoded on a worker thread


class _DecodedSource:
    __slots__ = ("data",)

    def __init__(self, data):
        self.data = data

class _Voice:
    __slots__ = ("placement_id", "source", "pos", "loop", "volume")

    def __init__(self, placement_id: str, source: _DecodedSource, start_frame: int, loop: bool, volume: float = 1.0):
        self.placement_id = placement_id
        self.source = source
        self.pos = start_frame
        self.loop = loop
        self.volume = volume


def _resample_linear(data, src_rate: int, dst_rate: int):
    if src_rate == dst_rate or data.shape[0] <= 1:
        return data
    n_src = data.shape[0]
    n_dst = max(1, int(round(n_src * dst_rate / src_rate)))
    src_idx = np.linspace(0, n_src - 1, num=n_dst, dtype=np.float64)
    lo = np.floor(src_idx).astype(np.int64)
    hi = np.minimum(lo + 1, n_src - 1)
    frac = (src_idx - lo).astype(np.float32)[:, None]
    return data[lo] * (1.0 - frac) + data[hi] * frac


class SoundMixer:
    def __init__(self, samplerate: int = 44100, channels: int = 2):
        if not MIXER_AVAILABLE:
            raise RuntimeError(
                "SoundMixer needs the 'numpy', 'sounddevice' and 'soundfile' "
                "packages installed (pip install numpy sounddevice soundfile)."
            )
        self.samplerate = samplerate
        self.channels = channels
        self._voices: dict[str, _Voice] = {}
        self._lock = threading.Lock()
        self._decode_cache: dict[tuple, Optional[_DecodedSource]] = {}
        self._decode_lock = threading.Lock()
        self._futures: dict = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="czeditor-audio")
        self._closed = False
        try:
            self._stream = sd.OutputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                blocksize=BLOCK_SIZE,
                dtype="float32",
                latency="high",
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception:
            self._stream = sd.OutputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="float32",
                latency="high",
                callback=self._audio_callback,
            )
            self._stream.start()

    # audio thread
    def _audio_callback(self, outdata, frames, time_info, status):
        with self._lock:
            voices = list(self._voices.values())

        if not voices:
            outdata.fill(0.0)
            return

        mix = np.zeros((frames, self.channels), dtype=np.float32)
        finished = []
        for voice in voices:
            data = voice.source.data
            n = data.shape[0]
            pos = voice.pos
            remaining = frames
            out_off = 0
            while remaining > 0 and n > 0:
                if pos >= n:
                    if voice.loop:
                        pos = 0
                    else:
                        break
                take = min(remaining, n - pos)
                mix[out_off:out_off + take] += data[pos:pos + take] * voice.volume
                pos += take
                out_off += take
                remaining -= take
            voice.pos = pos
            if not voice.loop and (n == 0 or voice.pos >= n):
                finished.append(voice.placement_id)

        np.clip(mix, -1.0, 1.0, out=mix)
        outdata[:] = mix

        if finished:
            with self._lock:
                for pid in finished:
                    self._voices.pop(pid, None)

    # main thread
    def _decode(self, project_dir: str, uuid_ext: str, speed: float = 1.0) -> Optional[_DecodedSource]:
        speed = speed if speed and speed > 0 else 1.0
        cache_key = (uuid_ext, speed)
        with self._decode_lock:
            if cache_key in self._decode_cache:
                return self._decode_cache[cache_key]
            base_source = self._decode_cache.get((uuid_ext, 1.0))
        path = os.path.join(project_dir, uuid_ext)
        result = None
        try:
            if base_source is not None and abs(speed - 1.0) < 1e-6:
                result = base_source
            else:
                data, sr = sf.read(path, dtype="float32", always_2d=True)
                if data.shape[1] != self.channels:
                    if data.shape[1] == 1:
                        data = np.repeat(data, self.channels, axis=1)
                    else:
                        data = data[:, : self.channels]
                effective_sr = int(sr * speed)
                data = _resample_linear(data, effective_sr, self.samplerate)
                result = _DecodedSource(np.ascontiguousarray(data, dtype=np.float32))
        except Exception as e:
            print(f"[sound_mixer] couldn't decode {uuid_ext!r} (speed={speed}): {e}")
            result = None
        with self._decode_lock:
            self._decode_cache[cache_key] = result
        return result

    def _get_decoded(self, project_dir: str, uuid_ext: str, speed: float):
        speed = speed if speed and speed > 0 else 1.0
        key = (uuid_ext, speed)
        with self._decode_lock:
            if key in self._decode_cache:
                return self._decode_cache[key]
            if key not in self._futures:
                def job():
                    try:
                        self._decode(project_dir, uuid_ext, speed)
                    finally:
                        with self._decode_lock:
                            self._futures.pop(key, None)
                try:
                    self._futures[key] = self._pool.submit(job)
                except RuntimeError:  # closing
                    return None
        return _PENDING

    def stop_all(self):
        with self._lock:
            self._voices.clear()

    def _collect_active_audio(self, project: Project, scene_id: str, t: float, depth: int = 0) -> list[tuple[str, str, float, float, bool, float]]:
        if depth > 10:
            return []
        scene = project.scenes.get(scene_id)
        if not scene:
            return []
            
        active = []
        hidden_tracks = set(getattr(scene, "hidden_tracks", []))
        for p in scene.placements:
            if p.track in hidden_tracks:
                continue
            if not (p.start <= t <= p.end):
                continue
            
            if p.kind == "scene":
                local_t = (t - p.start) + (p.src_offset or 0.0)
                nested = self._collect_active_audio(project, p.ref, local_t, depth + 1)
                for (vid, ref, speed, offset, loop, vol) in nested:
                    active.append((f"{p.id}_{vid}", ref, speed, offset, loop, vol))
                    
            elif p.kind in ("sound", "video"):
                if p.muted or p.ref not in project.sources:
                    continue
                speed = p.speed if p.speed and p.speed > 0 else 1.0
                offset_s = max(0.0, (p.src_offset or 0.0) + (t - p.start) * speed)
                vol = p.volume if p.volume is not None else 1.0
                active.append((p.id, p.ref, speed, offset_s, bool(p.loop), vol))
                
        return active

    def update(
        self,
        project: Optional[Project],
        project_dir: Optional[str],
        scene: Optional[Scene],
        t: float,
        playing: bool,
    ):
        if self._closed:
            return
        if not playing or not scene or not project or not project_dir:
            if self._voices:
                self.stop_all()
            return

        active_leaves = self._collect_active_audio(project, scene.id, t)
        for (vid, ref, speed, offset, loop, vol) in active_leaves:
            self._get_decoded(project_dir, ref, speed)

        active_ids = {vid for (vid, ref, speed, offset, loop, vol) in active_leaves}

        with self._lock:
            for pid in list(self._voices.keys()):
                if pid not in active_ids:
                    del self._voices[pid]
        existing = set(self._voices.keys())

        for (vid, ref, speed, offset_s, loop, vol) in active_leaves:
            if vid in existing:
                continue
            source = self._get_decoded(project_dir, ref, speed)
            if source is None or source is _PENDING:
                continue  # failed, or still decoding: picked up on a later tick, at the then-correct offset
            start_frame = int(offset_s * self.samplerate)
            voice = _Voice(vid, source, start_frame, loop, vol)
            with self._lock:
                self._voices[vid] = voice

    def seek(
        self,
        project: Optional[Project],
        project_dir: Optional[str],
        scene: Optional[Scene],
        t: float,
        playing: bool,
    ):
        if self._closed:
            return
        if not playing or not scene or not project or not project_dir:
            if self._voices:
                self.stop_all()
            return

        active_leaves = self._collect_active_audio(project, scene.id, t)
        for (vid, ref, speed, offset, loop, vol) in active_leaves:
            self._get_decoded(project_dir, ref, speed)

        active_ids = {vid for (vid, ref, speed, offset, loop, vol) in active_leaves}

        with self._lock:
            for pid in list(self._voices.keys()):
                if pid not in active_ids:
                    del self._voices[pid]

        for (vid, ref, speed, offset_s, loop, vol) in active_leaves:
            start_frame = int(offset_s * self.samplerate)
            with self._lock:
                voice = self._voices.get(vid)
            if voice is not None:
                voice.pos = start_frame
                voice.loop = loop
                voice.volume = vol
                continue
            source = self._get_decoded(project_dir, ref, speed)
            if source is None or source is _PENDING:
                continue  # failed, or still decoding: picked up on a later tick
            new_voice = _Voice(vid, source, start_frame, loop, vol)
            with self._lock:
                self._voices[vid] = new_voice

    def close(self):
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            self.stop_all()
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass
