from __future__ import annotations

import multiprocessing as mp
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from multiprocessing import shared_memory
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..audio_export import AUDIO_MIX_AVAILABLE, render_audio_mixdown, write_wav_pcm16
from ..export_worker import run_export_worker
from ..model import Project

DEFAULT_AUDIO_SAMPLERATE = 44100
QUEUE_GET_TIMEOUT_S = 30


def _find_ffmpeg(configured_path: Optional[str] = None) -> Optional[str]:
    if configured_path:
        configured_path = configured_path.strip()
        if configured_path:
            if os.path.isdir(configured_path):
                # user pointed at a folder rather than the executable itself
                for name in ("ffmpeg.exe", "ffmpeg"):
                    candidate = os.path.join(configured_path, name)
                    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                        return candidate
            elif os.path.isfile(configured_path) and os.access(configured_path, os.X_OK):
                return configured_path
            # configured but invalid/not found - don't silently fall back
            return None
    return shutil.which("ffmpeg")


def _cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))  # respects cgroup/taskset limits on Linux
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def _default_worker_count() -> int:
    # leave one core for the GUI/coordinator thread (which feeds ffmpeg's stdin);
    # ffmpeg's own encoder threads soak up whatever the renderers don't use.
    n = _cpu_count()
    return max(1, n - 1) if n > 2 else n


RING_BUDGET_BYTES = 384 * 1024 * 1024  # cap on shared-memory frame buffers
MAX_RING_SLOTS = 64


class _ExportThread(QThread):
    progress = pyqtSignal(int, int)
    status_message = pyqtSignal(str)
    finished_export = pyqtSignal(bool, str)

    def __init__(
        self,
        project: Project,
        scene_id: str,
        project_dir: Optional[str],
        output_path: str,
        fps: float,
        width: int,
        height: int,
        codec: str,   # "h264" | "vp9"
        crf: int,
        ffmpeg_path: str,
        audio_bitrate: str = "192k",
        audio_channels: int = 2,
        workers: Optional[int] = None,
    ):
        super().__init__()
        self._project = project
        self._scene_id = scene_id
        self._project_dir = project_dir
        self._output_path = output_path
        self._fps = fps
        self._width = width
        self._height = height
        self._codec = codec
        self._crf = crf
        self._ffmpeg = ffmpeg_path
        self._audio_bitrate = audio_bitrate or "192k"
        self._audio_channels = max(1, audio_channels or 2)
        self._workers = max(1, workers or _default_worker_count())
        self._cancelled = False
        self._mp_cancel_event = None  # set once run() has an mp context to build one from

    def cancel(self) -> None:
        self._cancelled = True
        if self._mp_cancel_event is not None:
            self._mp_cancel_event.set()

    def run(self) -> None:
        proc = None
        worker_procs: list = []
        result_queue = None
        task_queue = None
        shm = None
        shm_buf = None
        wav_path = None
        try:
            duration = self._project.duration_of(self._scene_id)
            total_frames = max(1, int(round(duration * self._fps)))

            w = self._width if self._width % 2 == 0 else self._width - 1
            h = self._height if self._height % 2 == 0 else self._height - 1

            num_workers = max(1, min(self._workers, total_frames))
            ctx = mp.get_context("spawn")  # never "fork": Qt does not support forking a live process
            self._mp_cancel_event = ctx.Event()
            if self._cancelled:
                self._mp_cancel_event.set()

            # ring of shared-memory frame slots; slot = frame_index % n_slots
            slot_bytes = w * h * 3
            n_slots = max(num_workers * 2, min(MAX_RING_SLOTS, RING_BUDGET_BYTES // max(1, slot_bytes)))
            n_slots = max(2, min(n_slots, total_frames))
            shm = shared_memory.SharedMemory(create=True, size=n_slots * slot_bytes)
            shm_buf = shm.buf
            cursor = ctx.Value("q", 0, lock=False)  # next frame index the coordinator will write
            result_queue = ctx.Queue()  # only carries frame indices
            task_queue = ctx.Queue()
            blocks = self._block_ranges(total_frames, num_workers)
            for b in blocks:
                task_queue.put(b)
            for _ in range(num_workers):
                task_queue.put(None)  # one stop sentinel per worker

            self.status_message.emit(i18n.tr("dialog.videoExport.startingWorkers").format(count=num_workers))
            for _ in range(num_workers):
                p = ctx.Process(
                    target=run_export_worker,
                    args=(self._project, self._scene_id, self._project_dir, w, h, self._fps,
                          shm.name, n_slots, slot_bytes, task_queue, result_queue,
                          cursor, self._mp_cancel_event, num_workers > 1),
                    daemon=True,
                )
                p.start()
                worker_procs.append(p)

            wav_path = self._build_audio_track(duration)

            cmd = self._build_ffmpeg_cmd(w, h, wav_path)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            # ffmpeg's stderr is only read after the pipe closes; drain it now so a chatty
            # encoder can't fill the pipe and stall the whole export.
            stderr_chunks: list = []
            stderr_thread = threading.Thread(
                target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
            stderr_thread.start()

            done: set = set()
            next_write = 0
            last_status = 0.0
            while next_write < total_frames:
                if self._cancelled:
                    break
                if next_write not in done:
                    try:
                        done.add(result_queue.get(timeout=1.0))
                    except queue.Empty:
                        bad = [p for p in worker_procs if p.exitcode not in (None, 0)]
                        if bad or not any(p.is_alive() for p in worker_procs):
                            raise RuntimeError(
                                i18n.tr("dialog.videoExport.workersExited")
                            )
                    # drain whatever else is already waiting without blocking
                    while True:
                        try:
                            done.add(result_queue.get_nowait())
                        except queue.Empty:
                            break
                    continue

                done.discard(next_write)
                off = (next_write % n_slots) * slot_bytes
                view = shm_buf[off:off + slot_bytes]
                try:
                    proc.stdin.write(view)
                except BrokenPipeError:
                    break
                finally:
                    view.release()

                next_write += 1
                cursor.value = next_write  # frees the slot for the workers (after the write)
                self.progress.emit(next_write, total_frames)
                now = time.monotonic()
                if now - last_status > 0.1:
                    last_status = now
                    self.status_message.emit(
                        i18n.tr("dialog.videoExport.rendering").format(
                            frame=next_write, total=total_frames,
                            seconds="%.2f" % (next_write / self._fps), count=num_workers)
                    )

            proc.stdin.close()

            if self._cancelled:
                proc.kill()
                proc.wait()
                try:
                    os.remove(self._output_path)
                except OSError:
                    pass
                self.finished_export.emit(False, i18n.tr("dialog.videoExport.cancelled"))
                return

            proc.wait()
            stderr_thread.join(timeout=5.0)
            stderr_data = b"".join(stderr_chunks)

            if proc.returncode != 0:
                msg = stderr_data.decode("utf-8", "replace").strip()
                snippet = msg[-800:] if len(msg) > 800 else msg
                self.finished_export.emit(
                    False, i18n.tr("dialog.videoExport.ffmpegExited").format(code=proc.returncode, output=snippet)
                )
            else:
                self.finished_export.emit(True, self._output_path)

        except Exception as exc:
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass
            self.finished_export.emit(False, str(exc))

        finally:
            if self._mp_cancel_event is not None:
                self._mp_cancel_event.set()
            for p in worker_procs:
                p.join(timeout=3.0)
                if p.is_alive():
                    p.terminate()
            for q in (result_queue, task_queue):
                if q is not None:
                    try:
                        q.close()
                        q.cancel_join_thread()
                    except Exception:
                        pass
            if shm is not None:
                try:
                    if shm_buf is not None:
                        shm_buf.release()
                    shm.close()
                    shm.unlink()
                except Exception:
                    pass
            if wav_path and os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except OSError:
                    pass

    @staticmethod
    def _block_ranges(total_frames: int, num_workers: int) -> list:
        """Split [0, total_frames) into small contiguous blocks handed out dynamically.

        Small enough to balance load (~8 blocks per worker), large enough that video
        decoders read mostly sequentially within a block.
        """
        size = max(4, min(32, -(-total_frames // (num_workers * 8))))
        return [(a, min(total_frames, a + size)) for a in range(0, total_frames, size)]

    def _build_audio_track(self, duration: float) -> Optional[str]:
        if not self._project_dir:
            return None
        if not AUDIO_MIX_AVAILABLE:
            self.status_message.emit(
                "numpy not installed - exporting video without audio "
                "(pip install numpy soundfile to enable it)"
            )
            return None

        self.status_message.emit(i18n.tr("dialog.videoExport.mixingAudio"))
        try:
            mix = render_audio_mixdown(
                self._project, self._project_dir, self._scene_id, duration,
                self._ffmpeg, samplerate=DEFAULT_AUDIO_SAMPLERATE,
                channels=self._audio_channels,
                cancelled=lambda: self._cancelled,
            )
        except Exception as e:
            print(f"[export] audio mixdown failed: {e}")
            return None
        if mix is None or self._cancelled:
            return None

        fd, path = tempfile.mkstemp(prefix="czeditor-export-audio-", suffix=".wav")
        os.close(fd)
        try:
            write_wav_pcm16(path, mix, DEFAULT_AUDIO_SAMPLERATE)
        except Exception as e:
            print(f"[export] failed writing audio mixdown: {e}")
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        return path

    def _build_ffmpeg_cmd(self, w: int, h: int, wav_path: Optional[str]) -> list:
        if self._codec == "h264":
            enc_args = [
                "-c:v", "libx264",
                "-crf", str(self._crf),
                "-preset", "fast",
                "-pix_fmt", "yuv420p",
            ]
            audio_codec = "aac"
        else:  # vp9
            enc_args = [
                "-c:v", "libvpx-vp9",
                "-crf", str(self._crf),
                "-b:v", "0",
                "-pix_fmt", "yuv420p",
            ]
            audio_codec = "libopus"

        cmd = [
            self._ffmpeg, "-y",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{w}x{h}",
            "-r", str(self._fps),
            "-i", "pipe:0",
        ]
        if wav_path:
            cmd += ["-i", wav_path]

        cmd += ["-map", "0:v:0"]
        if wav_path:
            cmd += [
                "-map", "1:a:0",
                "-c:a", audio_codec,
                "-b:a", self._audio_bitrate,
                "-ac", str(self._audio_channels),
            ]
        cmd += enc_args
        if wav_path:
            cmd += ["-shortest"]
        cmd += [self._output_path]
        return cmd

class VideoExportDialog(QDialog):
    def __init__(
        self,
        project: Project,
        scene_id: str,
        project_dir: Optional[str],
        parent: Optional[QWidget] = None,
        ffmpeg_path: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._scene_id = scene_id
        self._project_dir = project_dir
        self._configured_ffmpeg_path = ffmpeg_path
        self._thread: Optional[_ExportThread] = None

        self.setWindowTitle(i18n.tr("dialog.videoExport.title"))
        self.setMinimumWidth(500)

        root = QVBoxLayout(self)

        # settings
        settings_box = QGroupBox(i18n.tr("dialog.videoExport.settings"))
        form = QFormLayout(settings_box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        # output path
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText(i18n.tr("dialog.videoExport.choosePlaceholder"))
        browse_btn = QPushButton(i18n.tr("dialog.videoExport.browse"))
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse)
        path_layout.addWidget(self._path_edit, 1)
        path_layout.addWidget(browse_btn)
        form.addRow(i18n.tr("dialog.videoExport.outputFile"), path_row)

        # format
        self._codec_combo = QComboBox()
        self._codec_combo.addItem(i18n.tr("dialog.videoExport.codecMp4"), "h264")
        self._codec_combo.addItem(i18n.tr("dialog.videoExport.codecWebm"), "vp9")
        self._codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        form.addRow(i18n.tr("dialog.videoExport.format"), self._codec_combo)

        # quality
        self._crf_spin = QSpinBox()
        self._crf_spin.setRange(0, 51)
        self._crf_spin.setValue(23)
        self._crf_spin.setToolTip(
            i18n.tr("dialog.videoExport.crfTip")
        )
        form.addRow(i18n.tr("dialog.videoExport.quality"), self._crf_spin)

        # render workers (multicore export)
        self._workers_spin = QSpinBox()
        cpu_count = _cpu_count()
        self._workers_spin.setRange(1, max(1, cpu_count * 2))
        self._workers_spin.setValue(_default_worker_count())
        self._workers_spin.setToolTip(
            i18n.tr("dialog.videoExport.workersTip").format(count=cpu_count)
        )
        form.addRow(i18n.tr("dialog.videoExport.workers"), self._workers_spin)

        # project-derived read-only info
        scene_w, scene_h = project.scene_size(scene_id)
        self._export_w, self._export_h = scene_w, scene_h
        self._export_fps = float(project.fps)
        self._audio_bitrate = project.export_audio_bitrate
        self._audio_channels = project.export_channels

        duration = project.duration_of(scene_id)
        total_frames = max(1, int(round(duration * self._export_fps)))

        form.addRow(i18n.tr("dialog.videoExport.resolution"), QLabel(i18n.tr("dialog.videoExport.resolutionValue").format(w=scene_w, h=scene_h)))
        form.addRow(i18n.tr("dialog.videoExport.frameRate"), QLabel(i18n.tr("dialog.videoExport.frameRateValue").format(fps=project.fps)))
        form.addRow(
            i18n.tr("dialog.videoExport.duration"),
            QLabel(i18n.tr("dialog.videoExport.durationValue").format(seconds="%.2f" % duration, frames=total_frames)),
        )
        audio_note = (
            i18n.tr("dialog.videoExport.audioValue").format(bitrate=self._audio_bitrate, channels=self._audio_channels)
            if AUDIO_MIX_AVAILABLE
            else i18n.tr("dialog.videoExport.audioUnavailable")
        )
        form.addRow(i18n.tr("dialog.videoExport.audio"), QLabel(audio_note))

        root.addWidget(settings_box)

        # progress
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        root.addWidget(self._progress)

        self._status_label = QLabel("")
        self._status_label.setVisible(False)
        root.addWidget(self._status_label)

        # buttons
        btn_row = QHBoxLayout()
        self._export_btn = QPushButton(i18n.tr("dialog.videoExport.export"))
        self._export_btn.setDefault(True)
        self._export_btn.clicked.connect(self._start_export)
        self._close_btn = QPushButton(i18n.tr("dialog.videoExport.close"))
        self._close_btn.clicked.connect(self._on_close_clicked)
        btn_row.addStretch()
        btn_row.addWidget(self._export_btn)
        btn_row.addWidget(self._close_btn)
        root.addLayout(btn_row)

        # default output path
        import time
        import platform
        from pathlib import Path
        
        proj_name = project.name or "Untitled"
        time_str = time.strftime("%Y%m%d-%H%M%S")
        is_windows = platform.system() == "Windows"
        
        if is_windows:
            video_dir = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Videos", "CZEditor")
        else:
            video_dir = os.path.join(os.path.expanduser("~"), "Videos", "CZEditor")
            
        os.makedirs(video_dir, exist_ok=True)
        default_file = f"{proj_name}-{time_str}.mp4"
        self._path_edit.setText(os.path.join(video_dir, default_file))

    # slots

    def _on_codec_changed(self) -> None:
        codec = self._codec_combo.currentData()
        path = self._path_edit.text()
        if codec == "h264":
            self._crf_spin.setRange(0, 51)
            self._crf_spin.setValue(23)
            if path.endswith(".webm"):
                self._path_edit.setText(path[:-5] + ".mp4")
        else:
            self._crf_spin.setRange(0, 63)
            self._crf_spin.setValue(33)
            if path.endswith(".mp4"):
                self._path_edit.setText(path[:-4] + ".webm")

    def _browse(self) -> None:
        codec = self._codec_combo.currentData()
        if codec == "h264":
            filter_str = i18n.tr("dialog.videoExport.filterMp4")
            default_ext = ".mp4"
        else:
            filter_str = i18n.tr("dialog.videoExport.filterWebm")
            default_ext = ".webm"

        start = self._path_edit.text() or (
            self._project_dir or os.path.expanduser("~")
        )
        path, _ = QFileDialog.getSaveFileName(
            self, i18n.tr("dialog.videoExport.title"), start, filter_str
        )
        if path:
            if not (path.endswith(".mp4") or path.endswith(".webm")):
                path += default_ext
            self._path_edit.setText(path)

    def _start_export(self) -> None:
        output_path = self._path_edit.text().strip()
        if not output_path:
            QMessageBox.warning(
                self, i18n.tr("dialog.videoExport.noPath.title"), i18n.tr("dialog.videoExport.noPath.message")
            )
            return

        ffmpeg = _find_ffmpeg(self._configured_ffmpeg_path)
        if not ffmpeg:
            if self._configured_ffmpeg_path and self._configured_ffmpeg_path.strip():
                QMessageBox.critical(
                    self,
                    i18n.tr("dialog.videoExport.ffmpegMissing.title"),
                    i18n.tr("dialog.videoExport.ffmpegBadPath").format(path=self._configured_ffmpeg_path),
                )
            else:
                QMessageBox.critical(
                    self,
                    i18n.tr("dialog.videoExport.ffmpegMissing.title"),
                    i18n.tr("dialog.videoExport.ffmpegNotInPath"),
                )
            return

        self._set_exporting(True)

        self._thread = _ExportThread(
            project=self._project,
            scene_id=self._scene_id,
            project_dir=self._project_dir,
            output_path=output_path,
            fps=self._export_fps,
            width=self._export_w,
            height=self._export_h,
            codec=self._codec_combo.currentData(),
            crf=self._crf_spin.value(),
            ffmpeg_path=ffmpeg,
            audio_bitrate=self._audio_bitrate,
            audio_channels=self._audio_channels,
            workers=self._workers_spin.value(),
        )
        self._thread.progress.connect(self._on_progress)
        self._thread.status_message.connect(self._on_status)
        self._thread.finished_export.connect(self._on_finished)
        self._thread.start()

    def _on_progress(self, current: int, total: int) -> None:
        self._progress.setMaximum(total)
        self._progress.setValue(current)

    def _on_status(self, msg: str) -> None:
        self._status_label.setText(msg)

    def _on_finished(self, success: bool, message: str) -> None:
        self._thread = None
        self._set_exporting(False)

        if success:
            QMessageBox.information(
                self,
                i18n.tr("dialog.videoExport.done.title"),
                i18n.tr("dialog.videoExport.done.message").format(path=message),
            )
            self.accept()
        else:
            if message != i18n.tr("dialog.videoExport.cancelled"):
                QMessageBox.warning(self, i18n.tr("dialog.videoExport.failed"), message)

    def _on_close_clicked(self) -> None:
        if self._thread is not None:
            # A render is running - treat this as a cancel request.
            self._thread.cancel()
            self._close_btn.setEnabled(False)
            self._status_label.setText(i18n.tr("dialog.videoExport.cancelling"))
        else:
            self.reject()

    # helpers

    def _set_exporting(self, exporting: bool) -> None:
        self._export_btn.setEnabled(not exporting)
        self._progress.setVisible(exporting)
        self._status_label.setVisible(exporting)
        if exporting:
            self._progress.setValue(0)
            self._close_btn.setText(i18n.tr("dialog.videoExport.cancel"))
            self._close_btn.setEnabled(True)
        else:
            self._close_btn.setText(i18n.tr("dialog.videoExport.close"))
            self._close_btn.setEnabled(True)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._thread is not None:
            self._thread.cancel()
            self._thread.wait(3000)
        super().closeEvent(event)
