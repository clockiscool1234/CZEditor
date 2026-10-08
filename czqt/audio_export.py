from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
import wave
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import numpy as np

    AUDIO_MIX_AVAILABLE = True
except Exception:  # numpy is an optional dependency (see requirements.txt)
    np = None  # type: ignore
    AUDIO_MIX_AVAILABLE = False

from .compositor import MAX_RECURSION
from .model import Project


@dataclass
class AudioLeaf:
    ref: str
    global_start: float
    global_end: float
    src_offset: float
    speed: float
    loop: bool
    volume: float


def collect_audio_leaves(project: Project, scene_id: str) -> list:
    leaves: list = []
    _walk(project, scene_id, 0.0, 0, leaves)
    return leaves


def _walk(project: Project, scene_id: str, offset: float, depth: int, out: list) -> None:
    if depth > MAX_RECURSION:
        return
    scene = project.scenes.get(scene_id)
    if not scene:
        return
    hidden_tracks = set(getattr(scene, "hidden_tracks", []))
    for p in scene.placements:
        if p.track in hidden_tracks:
            continue
        if p.kind == "scene":
            child_offset = offset + (p.start - (p.src_offset or 0.0))
            _walk(project, p.ref, child_offset, depth + 1, out)
        elif p.kind in ("sound", "video"):
            if p.muted or not p.ref or p.ref not in project.sources:
                continue
            volume = p.volume if p.volume is not None else 1.0
            if volume <= 0.0:
                continue
            out.append(AudioLeaf(
                ref=p.ref,
                global_start=p.start + offset,
                global_end=p.end + offset,
                src_offset=p.src_offset or 0.0,
                speed=p.speed if p.speed and p.speed > 0 else 1.0,
                loop=bool(p.loop),
                volume=volume,
            ))


def _resample_linear(data, src_rate: float, dst_rate: float):
    if src_rate == dst_rate or data.shape[0] <= 1:
        return data
    n_src = data.shape[0]
    n_dst = max(1, int(round(n_src * dst_rate / src_rate)))
    src_idx = np.linspace(0, n_src - 1, num=n_dst, dtype=np.float64)
    lo = np.floor(src_idx).astype(np.int64)
    hi = np.minimum(lo + 1, n_src - 1)
    frac = (src_idx - lo).astype(np.float32)[:, None]
    return data[lo] * (1.0 - frac) + data[hi] * frac


def _decode_source(ffmpeg_path: str, path: str, samplerate: int, channels: int):
    cmd = [
        ffmpeg_path, "-v", "error",
        "-i", path,
        "-map", "0:a:0?",
        "-f", "f32le", "-acodec", "pcm_f32le",
        "-ar", str(samplerate), "-ac", str(channels),
        "-",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception:
        return None
    if not proc.stdout:
        return None
    arr = np.frombuffer(proc.stdout, dtype=np.float32)
    if arr.size == 0:
        return None
    usable = (arr.size // channels) * channels
    if usable <= 0:
        return None
    return arr[:usable].reshape(-1, channels).copy()


def render_audio_mixdown(
    project: Project,
    project_dir: str,
    scene_id: str,
    duration: float,
    ffmpeg_path: str,
    samplerate: int = 44100,
    channels: int = 2,
    cancelled: Optional[Callable[[], bool]] = None,
):
    if not AUDIO_MIX_AVAILABLE:
        return None
    channels = max(1, channels)
    leaves = collect_audio_leaves(project, scene_id)
    leaves = [l for l in leaves if l.global_end > 0.0 and l.global_start < duration]
    if not leaves:
        return None

    total_frames = max(1, int(round(duration * samplerate)))
    mix = np.zeros((total_frames, channels), dtype=np.float32)

    raw_cache: dict = {}  # ref -> data at speed 1.0

    def _decode_raw(ref: str):
        return _decode_source(ffmpeg_path, os.path.join(project_dir, ref), samplerate, channels)

    refs = sorted({l.ref for l in leaves})
    workers = max(1, min(len(refs), (os.cpu_count() or 2), 8))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="czeditor-mixdown") as pool:
        futures = {ref: pool.submit(_decode_raw, ref) for ref in refs}
        for ref, fut in futures.items():
            if cancelled is not None and cancelled():
                for f in futures.values():
                    f.cancel()
                return None
            try:
                raw_cache[ref] = fut.result()
            except Exception:
                raw_cache[ref] = None

    decoded_cache: dict = {}
    any_audio = False
    for leaf in leaves:
        if cancelled is not None and cancelled():
            return None

        cache_key = (leaf.ref, round(leaf.speed, 6))
        if cache_key in decoded_cache:
            data = decoded_cache[cache_key]
        else:
            data = raw_cache.get(leaf.ref)
            if data is not None and abs(leaf.speed - 1.0) > 1e-6:
                data = _resample_linear(data, samplerate * leaf.speed, samplerate)
                data = np.ascontiguousarray(data, dtype=np.float32)
            decoded_cache[cache_key] = data

        if data is None or data.shape[0] == 0:
            continue

        n = data.shape[0]
        start_sample = int(round(max(0.0, leaf.global_start) * samplerate))
        end_sample = min(total_frames, int(round(leaf.global_end * samplerate)))
        if end_sample <= start_sample:
            continue

        pos = max(0, min(int(round(leaf.src_offset * samplerate)), n - 1))
        out_off = start_sample
        while out_off < end_sample:
            if pos >= n:
                if leaf.loop:
                    pos = 0
                else:
                    break  # matches SoundMixer: an unlooped source just goes quiet once it ends
            take = min(end_sample - out_off, n - pos)
            mix[out_off:out_off + take] += data[pos:pos + take] * leaf.volume
            pos += take
            out_off += take
        any_audio = True

    if not any_audio:
        return None
    np.clip(mix, -1.0, 1.0, out=mix)
    return mix


def write_wav_pcm16(path: str, mix, samplerate: int) -> None:
    channels = mix.shape[1] if mix.ndim > 1 else 1
    clipped = np.clip(mix, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(pcm16.tobytes())
