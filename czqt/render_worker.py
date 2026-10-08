from __future__ import annotations

import atexit
import threading
import traceback
import weakref
from typing import Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal


class FrameRenderer(QObject):
    frame_ready = pyqtSignal(int, object)  # (tag, QImage) - delivered on the GUI thread

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._cond = threading.Condition()
        self._pending: Optional[tuple] = None
        self._closed = False
        self._busy = False
        self._thread = threading.Thread(target=self._run, name="czeditor-render", daemon=True)
        self._thread.start()
        _LIVE.add(self)

    def submit(self, tag: int, job: Callable[[], object]):
        with self._cond:
            if self._closed:
                return
            self._pending = (tag, job)  # replaces any job that hasn't started yet
            self._cond.notify()

    def is_idle(self) -> bool:
        with self._cond:
            return self._pending is None and not self._busy

    def _run(self):
        while True:
            with self._cond:
                while self._pending is None and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                tag, job = self._pending
                self._pending = None
                self._busy = True
            img = None
            try:
                img = job()
            except Exception:
                traceback.print_exc()
            finally:
                with self._cond:
                    self._busy = False
            if img is not None and not self._closed:
                try:
                    self.frame_ready.emit(tag, img)
                except RuntimeError:
                    return  # the QObject was destroyed under us (app shutting down)

    def close(self, join: bool = True):
        with self._cond:
            self._closed = True
            self._pending = None
            self._cond.notify_all()
        if join and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)


_LIVE: "weakref.WeakSet" = weakref.WeakSet()


@atexit.register
def _shutdown_all():
    for r in list(_LIVE):
        try:
            r.close(join=True)
        except Exception:
            pass
