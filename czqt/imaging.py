from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtGui import QImage, QPixmap

try:
    from PIL import Image as _PILImage
    from PIL import ImageSequence as _PILImageSequence

    PIL_AVAILABLE = True
except Exception:
    _PILImage = None
    _PILImageSequence = None
    PIL_AVAILABLE = False

ANIMATED_EXTS = (".gif", ".webp")

MIN_FRAME_SECONDS = 0.02


def load_pixmap(path: str) -> Optional[QPixmap]:
    if not path or not os.path.isfile(path):
        return None
    pix = QPixmap(path)
    if not pix.isNull():
        return pix
    if path.lower().endswith(".webp"):
        image = _load_webp_via_pillow(path)
        if image is not None:
            pix = QPixmap.fromImage(image)
            if not pix.isNull():
                return pix
    return None


def prepare_image(img: QImage) -> QImage:
    if img.isNull():
        return img
    if img.hasAlphaChannel():
        if img.format() != QImage.Format.Format_ARGB32_Premultiplied:
            return img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        return img
    if img.format() != QImage.Format.Format_RGB32:
        return img.convertToFormat(QImage.Format.Format_RGB32)
    return img


def load_image(path: str) -> Optional[QImage]:
    if not path or not os.path.isfile(path):
        return None
    img = QImage(path)
    if img.isNull() and path.lower().endswith(".webp"):
        img = _load_webp_via_pillow(path) or QImage()
    if img is None or img.isNull():
        return None
    return prepare_image(img)


def _load_webp_via_pillow(path: str) -> Optional[QImage]:
    if not PIL_AVAILABLE:
        return None
    try:
        with _PILImage.open(path) as im:
            im = im.convert("RGBA")
            w, h = im.size
            if w <= 0 or h <= 0:
                return None
            data = im.tobytes("raw", "RGBA")
            qimg = QImage(data, w, h, w * 4, QImage.Format.Format_RGBA8888)
            return qimg.copy()
    except Exception as e:
        print(f"[imaging] Pillow failed to decode {path!r}: {e}")
        return None


def is_possibly_animated(path: str) -> bool:
    return (path or "").lower().endswith(ANIMATED_EXTS)


def load_animated_frames(path: str, as_image: bool = False) -> Optional[list]:
    if not PIL_AVAILABLE or not is_possibly_animated(path):
        return None
    if not path or not os.path.isfile(path):
        return None
    try:
        with _PILImage.open(path) as im:
            if not getattr(im, "is_animated", False) or getattr(im, "n_frames", 1) <= 1:
                return None
            frames: list = []
            for frame in _PILImageSequence.Iterator(im):
                rgba = frame.convert("RGBA")
                w, h = rgba.size
                if w <= 0 or h <= 0:
                    continue
                data = rgba.tobytes("raw", "RGBA")
                qimg = QImage(data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
                if as_image:
                    pix = prepare_image(qimg)
                else:
                    pix = QPixmap.fromImage(qimg)
                if pix.isNull():
                    continue
                dur_ms = frame.info.get("duration", 100) or 100
                frames.append((pix, max(MIN_FRAME_SECONDS, dur_ms / 1000.0)))
            return frames if len(frames) > 1 else None
    except Exception as e:
        print(f"[imaging] Pillow failed to decode animated frames for {path!r}: {e}")
        return None
