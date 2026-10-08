from __future__ import annotations

import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QImage, QImageReader, QPixmap

from .asset_types import is_audio, is_video

_MAX_ICONS = 768


def _load_image_thumb(path: str, size: QSize) -> Optional[QImage]:
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    native = reader.size()
    if native.isValid() and (native.width() > size.width() or native.height() > size.height()):
        reader.setScaledSize(native.scaled(size, Qt.AspectRatioMode.KeepAspectRatio))
    img = reader.read()
    if img.isNull():
        from .imaging import load_image  # webp-via-Pillow fallback etc.
        img = load_image(path) or QImage()
        if img.isNull():
            return None
    if img.width() > size.width() or img.height() > size.height():
        img = img.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return img


def _load_video_thumb(path: str, size: QSize) -> Optional[QImage]:
    from . import video
    if not video.CV2_AVAILABLE:
        return None
    cap = video._open_capture(path)
    if cap is None:
        return None
    try:
        if not cap.isOpened():
            return None
        ok, frame = cap.read()
        if not ok:
            return None
        h, w = frame.shape[:2]
        s = min(size.width() / w, size.height() / h, 1.0)
        return video._bgr_to_qimage(frame, (max(1, int(w * s)), max(1, int(h * s))))
    finally:
        cap.release()


class ThumbnailStore(QObject):
    ready = pyqtSignal()
    _done = pyqtSignal(object, object)  # worker -> GUI thread

    def __init__(self):
        super().__init__()
        self._icons: "OrderedDict[tuple, QIcon]" = OrderedDict()
        self._failed: set = set()
        self._inflight: set = set()
        workers = max(2, min(4, (os.cpu_count() or 2) - 1))
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="czeditor-thumb")
        self._done.connect(self._on_done)

    def get(self, key: tuple) -> Optional[QIcon]:
        icon = self._icons.get(key)
        if icon is not None:
            self._icons.move_to_end(key)
        return icon

    def request(self, key: tuple, loader: Callable[[], Optional[QImage]]):
        if key in self._icons or key in self._failed or key in self._inflight:
            return
        self._inflight.add(key)

        def work():
            try:
                img = loader()
            except Exception:
                img = None
            try:
                self._done.emit(key, img)
            except RuntimeError:
                pass

        try:
            self._pool.submit(work)
        except RuntimeError:
            self._inflight.discard(key)

    def _on_done(self, key, img):
        self._inflight.discard(key)
        if img is None or img.isNull():
            self._failed.add(key)
        else:
            self._icons[key] = QIcon(QPixmap.fromImage(img))
            while len(self._icons) > _MAX_ICONS:
                self._icons.popitem(last=False)
        self.ready.emit()

    def clear(self):
        self._icons.clear()
        self._failed.clear()

    # ready-made lookups
    def source_icon(self, project_dir: Optional[str], uuid_ext: str, size: QSize) -> Optional[QIcon]:
        if not project_dir or not uuid_ext or is_audio(uuid_ext):
            return None
        if "\x00" in uuid_ext:
            return None
        path = os.path.join(project_dir, uuid_ext)
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return None
        key = ("src", path, mtime, size.width(), size.height())
        icon = self.get(key)
        if icon is None:
            video = is_video(uuid_ext)
            self.request(key, (lambda: _load_video_thumb(path, size)) if video
                         else (lambda: _load_image_thumb(path, size)))
        return icon

    def scene_icon(self, project, project_dir: Optional[str], scene_id: str,
                   signature: str, size: QSize) -> Optional[QIcon]:
        if not project_dir:
            return None
        key = ("scene", project_dir, scene_id, signature, size.width(), size.height())
        icon = self.get(key)
        if icon is None:
            def loader():
                from .compositor import ImageCache, render_frame
                w, h = project.scene_size(scene_id)
                scale = min(1.0, size.width() / max(1, w))
                cache = ImageCache()
                try:
                    return render_frame(project, scene_id, 0.0, cache, project_dir,
                                        max(1, int(w * scale)), max(1, int(h * scale)))
                finally:
                    cache.clear()
            self.request(key, loader)
        return icon


_store: Optional[ThumbnailStore] = None


def store() -> ThumbnailStore:
    global _store
    if _store is None:
        _store = ThumbnailStore()
    return _store


def project_signature(project) -> str:
    import hashlib, json
    blob = json.dumps(
        {"s": {k: v.to_dict() for k, v in project.scenes.items()},
         "y": {k: v.to_dict() for k, v in project.symbol_defs.items()},
         "w": [project.width, project.height]},
        sort_keys=True, default=str,
    )
    return hashlib.md5(blob.encode()).hexdigest()
