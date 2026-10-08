from __future__ import annotations
import os
import wave
from typing import Optional

try:
    import soundfile as sf

    _SOUNDFILE_AVAILABLE = True
except Exception:
    sf = None
    _SOUNDFILE_AVAILABLE = False


def probe_duration_seconds(path: str) -> Optional[float]:
    if not path or not os.path.isfile(path):
        return None
    if _SOUNDFILE_AVAILABLE:
        try:
            info = sf.info(path)
            if info.frames and info.samplerate:
                return info.frames / float(info.samplerate)
        except Exception:
            pass
    if path.lower().endswith(".wav"):
        try:
            with wave.open(path, "rb") as w:
                frames = w.getnframes()
                rate = w.getframerate()
                if frames and rate:
                    return frames / float(rate)
        except Exception:
            pass
    return None
