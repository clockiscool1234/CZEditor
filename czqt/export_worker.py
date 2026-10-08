from __future__ import annotations

import os
import time

_W_PROJECT = None
_W_SCENE_ID = None
_W_PROJECT_DIR = None
_W_WIDTH = 0
_W_HEIGHT = 0
_W_IMAGES = None
_W_APP = None  # kept alive for the process's lifetime; never read again


def init_worker(project, scene_id, project_dir, width: int, height: int,
                single_threaded_libs: bool = True) -> None:
    global _W_PROJECT, _W_SCENE_ID, _W_PROJECT_DIR, _W_WIDTH, _W_HEIGHT, _W_IMAGES, _W_APP

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if single_threaded_libs:
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS"):
            os.environ.setdefault(var, "1")

    from PyQt6.QtGui import QGuiApplication

    if QGuiApplication.instance() is None:
        _W_APP = QGuiApplication(["czeditor-export-worker"])

    if single_threaded_libs:
        try:
            import cv2
            cv2.setNumThreads(1)
        except Exception:
            pass

    from .compositor import ImageCache

    _W_PROJECT = project
    _W_SCENE_ID = scene_id
    _W_PROJECT_DIR = project_dir
    _W_WIDTH = width
    _W_HEIGHT = height
    # exact_video: block for the right frame instead of drawing a stale one
    _W_IMAGES = ImageCache(exact_video=True)


def _attach_shm(name: str):
    from multiprocessing import shared_memory

    try:
        return shared_memory.SharedMemory(name=name, track=False)  # py3.13+
    except TypeError:
        return shared_memory.SharedMemory(name=name)


def _copy_frame_into(img_rgb, dst, width: int, height: int) -> None:
    bpl = img_rgb.bytesPerLine()
    row = width * 3
    ptr = img_rgb.constBits()
    ptr.setsize(img_rgb.sizeInBytes())
    src = memoryview(ptr).cast("B")
    if bpl == row:
        dst[: row * height] = src[: row * height]
    else:
        for y in range(height):
            dst[y * row:(y + 1) * row] = src[y * bpl:y * bpl + row]


def _render_blocks(fps: float, shm_name: str, n_slots: int, slot_bytes: int,
                   task_queue, result_queue, cursor, cancel_event) -> None:
    from PyQt6.QtGui import QImage

    from .compositor import render_frame

    shm = _attach_shm(shm_name)
    buf = shm.buf
    try:
        while not cancel_event.is_set():
            block = task_queue.get()
            if block is None:
                break
            start, end = block
            for idx in range(start, end):
                # back-pressure: wait until this frame's ring slot has been consumed
                while idx >= cursor.value + n_slots:
                    if cancel_event.is_set():
                        return
                    time.sleep(0.002)
                if cancel_event.is_set():
                    return
                img = render_frame(_W_PROJECT, _W_SCENE_ID, idx / fps, _W_IMAGES,
                                   _W_PROJECT_DIR, _W_WIDTH, _W_HEIGHT)
                if img.width() != _W_WIDTH or img.height() != _W_HEIGHT:
                    img = img.scaled(_W_WIDTH, _W_HEIGHT)
                img_rgb = img.convertToFormat(QImage.Format.Format_RGB888)
                off = (idx % n_slots) * slot_bytes
                slot = buf[off:off + slot_bytes]
                try:
                    _copy_frame_into(img_rgb, slot, _W_WIDTH, _W_HEIGHT)
                finally:
                    slot.release()
                result_queue.put(idx)
    finally:
        buf.release()
        shm.close()
        if _W_IMAGES is not None:
            try:
                _W_IMAGES.clear(join=False)
            except Exception:
                pass


def run_export_worker(project, scene_id, project_dir, width, height, fps: float,
                      shm_name: str, n_slots: int, slot_bytes: int,
                      task_queue, result_queue, cursor, cancel_event,
                      single_threaded_libs: bool = True) -> None:
    try:
        init_worker(project, scene_id, project_dir, width, height, single_threaded_libs)
        _render_blocks(fps, shm_name, n_slots, slot_bytes,
                       task_queue, result_queue, cursor, cancel_event)
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)  # non-zero exit lets the coordinator fail fast instead of hanging
