from __future__ import annotations

import math
import os
from .. import i18n
from dataclasses import replace
from typing import Optional

try:
    import numpy as _np
except Exception:  # optional: the per-pixel painter loops below are the fallback
    _np = None

from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPolygonItem,
    QGraphicsProxyWidget,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ..asset_types import is_audio, is_video
from ..audio_probe import probe_duration_seconds
from ..dnd import decode_asset_mime, decode_asset_mime_list
from ..model import DEFAULT_EASE, DEFAULT_FPS, MAIN_ID, Placement, Project, new_uuid
from ..waveform import WaveformCache, raster_peak_columns

PPS_MIN, PPS_MAX, PPS_DEFAULT = 12, 480, 60
TRACK_H_MIN, TRACK_H_MAX, TRACK_H_DEFAULT = 14, 90, 28
RULER_H = 24
GUTTER_W = 200  # track# + visibility toggle + inline name field
SCRUB_AUTOSCROLL_MS = 30
SCRUB_AUTOSCROLL_MAX_PX = 40
PLAYHEAD_GRAB_PX = 4
NUM_TRACKS_MIN = 6
EDGE_PX = 7
SNAP_PH_PX = 8
CLIP_SNAP_PX = 10  # snap threshold (px) for resizing/moving clips against other clips' edges
MIN_DUR = 0.1

_KIND_COLORS = {
    "scene": QColor("#4d7cc7"),
    "symbol": QColor("#5aa06a"),
    "sound": QColor("#c78a4d"),
    "video": QColor("#9b59b6"),
}

_TIMING_TICK_COLORS = {
    "duplication": QColor("#e5b567"),
    "flashing": QColor("#c96fe0"),
    "switch_media": QColor("#6fd0e0"),
}
_TIMING_TICK_DEFAULT_COLOR = QColor("#e0e0e0")
TIMING_POINT_SNAP_PX = 6.0

KEYFRAME_HIT_PX = 6.0
_KF_COLOR = QColor("#ffd24a")
_KF_EASE_COLOR = QColor("#ff6fd0")


class BezierCurveEditor(QWidget):
    changed = pyqtSignal()
    PAD = 20
    GSIZE = 200

    def __init__(self, ease, parent=None):
        super().__init__(parent)
        ease = ease or DEFAULT_EASE
        self.p1 = [float(ease[0]), float(ease[1])]
        self.p2 = [float(ease[2]), float(ease[3])]
        self._drag_handle = None
        self.setFixedSize(self.GSIZE + self.PAD * 2, self.GSIZE + self.PAD * 2)
        self.setMouseTracking(True)

    def ease(self) -> list:
        return [self.p1[0], self.p1[1], self.p2[0], self.p2[1]]

    def set_ease(self, ease):
        self.p1 = [float(ease[0]), float(ease[1])]
        self.p2 = [float(ease[2]), float(ease[3])]
        self.update()
        self.changed.emit()

    def _to_widget(self, x: float, y: float) -> QPointF:
        return QPointF(self.PAD + x * self.GSIZE, self.PAD + (1.0 - y) * self.GSIZE)

    def _to_norm(self, pos: QPointF) -> tuple:
        x = (pos.x() - self.PAD) / self.GSIZE
        y = 1.0 - (pos.y() - self.PAD) / self.GSIZE
        return x, y

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#1c1c1c"))
        box = QRectF(self.PAD, self.PAD, self.GSIZE, self.GSIZE)
        painter.setPen(QPen(QColor("#484848"), 1))
        painter.setBrush(QColor("#141414"))
        painter.drawRect(box)
        painter.setPen(QPen(QColor("#2a2a2a"), 1))
        for i in range(1, 4):
            f = i / 4.0
            gx = self.PAD + f * self.GSIZE
            gy = self.PAD + f * self.GSIZE
            painter.drawLine(QPointF(gx, self.PAD), QPointF(gx, self.PAD + self.GSIZE))
            painter.drawLine(QPointF(self.PAD, gy), QPointF(self.PAD + self.GSIZE, gy))
        painter.setPen(QPen(QColor("#3a3a3a"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(self._to_widget(0, 0), self._to_widget(1, 1))
        painter.setPen(QPen(QColor("#4a8bff"), 1))
        painter.drawLine(self._to_widget(0, 0), self._to_widget(*self.p1))
        painter.drawLine(self._to_widget(1, 1), self._to_widget(*self.p2))
        path = QPainterPath(self._to_widget(0, 0))
        path.cubicTo(self._to_widget(*self.p1), self._to_widget(*self.p2), self._to_widget(1, 1))
        painter.setPen(QPen(_KF_EASE_COLOR, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#888"))
        painter.drawEllipse(self._to_widget(0, 0), 4, 4)
        painter.drawEllipse(self._to_widget(1, 1), 4, 4)
        painter.setBrush(QColor("#4a8bff"))
        painter.drawEllipse(self._to_widget(*self.p1), 6, 6)
        painter.drawEllipse(self._to_widget(*self.p2), 6, 6)
        painter.end()

    def _handle_at(self, pos: QPointF):
        for name, pt in (("p1", self.p1), ("p2", self.p2)):
            wp = self._to_widget(*pt)
            if (wp - pos).manhattanLength() <= 12:
                return name
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_handle = self._handle_at(event.position())
        if self._drag_handle:
            self._drag_to(event.position())

    def mouseMoveEvent(self, event):
        if self._drag_handle:
            self._drag_to(event.position())

    def mouseReleaseEvent(self, event):
        self._drag_handle = None

    def _drag_to(self, pos: QPointF):
        x, y = self._to_norm(pos)
        x = max(0.0, min(1.0, x))
        y = max(-0.5, min(1.5, y))
        target = self.p1 if self._drag_handle == "p1" else self.p2
        target[0], target[1] = x, y
        self.update()
        self.changed.emit()


class BezierEaseDialog(QDialog):
    _PRESETS = [
        ("Linear", (0.0, 0.0, 1.0, 1.0)),
        ("Ease", (0.25, 0.1, 0.25, 1.0)),
        ("Ease In", (0.42, 0.0, 1.0, 1.0)),
        ("Ease Out", (0.0, 0.0, 0.58, 1.0)),
        ("Ease In Out", (0.42, 0.0, 0.58, 1.0)),
    ]

    def __init__(self, ease, parent=None):
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("timeline.easeDialogTitle"))
        layout = QVBoxLayout(self)

        self.editor = BezierCurveEditor(ease, self)
        self.editor.setToolTip(i18n.tr("timeline.hintEaseEditor"))
        center_row = QHBoxLayout()
        center_row.addStretch(1)
        center_row.addWidget(self.editor)
        center_row.addStretch(1)
        layout.addLayout(center_row)

        spin_row = QHBoxLayout()
        self.spins = []
        for label in ("P1 x", "P1 y", "P2 x", "P2 y"):
            spin_row.addWidget(QLabel(label))
            sb = QDoubleSpinBox()
            sb.setRange(-2.0, 2.0)
            sb.setSingleStep(0.01)
            sb.setDecimals(3)
            spin_row.addWidget(sb)
            self.spins.append(sb)
        layout.addLayout(spin_row)
        self._sync_spins_from_editor()
        for sb in self.spins:
            sb.valueChanged.connect(self._on_spin_changed)
        self.editor.changed.connect(self._sync_spins_from_editor)

        preset_row = QHBoxLayout()
        for name, vals in self._PRESETS:
            btn = QPushButton(name)
            btn.clicked.connect(lambda _checked=False, v=vals: self.editor.set_ease(list(v)))
            preset_row.addWidget(btn)
        layout.addLayout(preset_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _sync_spins_from_editor(self):
        for sb, v in zip(self.spins, self.editor.ease()):
            sb.blockSignals(True)
            sb.setValue(v)
            sb.blockSignals(False)

    def _on_spin_changed(self, _value):
        self.editor.set_ease([sb.value() for sb in self.spins])

    def result_ease(self) -> list:
        return self.editor.ease()


class PlacementItem(QGraphicsRectItem):
    def __init__(self, placement: Placement, panel: "TimelinePanel"):
        super().__init__()
        self.placement = placement
        self.panel = panel
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)
        self.setAcceptHoverEvents(True)
        color = _KIND_COLORS.get(placement.kind, QColor("#888"))
        self._base_color = color
        self.setBrush(QBrush(color))
        self.setPen(QPen(color.darker(140), 1))

        label = panel.label_for_placement(placement) if panel is not None else (placement.ref or placement.kind)
        self._label_text = str(label)
        self._elided_for: Optional[tuple] = None
        self._elided_text = ""

        self._drag_mode: Optional[str] = None
        self._drag_start_scene_pos: Optional[QPointF] = None
        self._orig_pos = QPointF()
        self._orig_rect = None
        self._group: list = []
        self._group_orig: dict = {}

    # paint
    def _draw_label(self, painter):
        r = self.rect()
        avail = int(r.width()) - 8
        if avail < 6 or not self._label_text:
            return
        painter.save()
        painter.setClipRect(r)  # never draw outside the clip itself
        fm = painter.fontMetrics()
        key = (avail, self._label_text, painter.font().key())
        if self._elided_for != key:
            self._elided_for = key
            self._elided_text = fm.elidedText(self._label_text, Qt.TextElideMode.ElideRight, avail)
        painter.setPen(QColor("#111"))
        baseline = (r.height() + fm.ascent() - fm.descent()) / 2.0
        painter.drawText(QPointF(4, baseline), self._elided_text)
        painter.restore()

    def paint(self, painter, option, widget=None):
        selected = self in self.panel.selected_items
        if selected:
            painter.setPen(QPen(QColor("#ffd24a"), 2))
            painter.setBrush(self.brush())
            painter.drawRect(self.rect())
        else:
            super().paint(painter, option, widget)
        if self.placement.kind in ("sound", "video"):
            painter.save()
            painter.setClipRect(self.rect())
            self._draw_waveform(painter, self.rect(), option)
            painter.restore()
        tp = self.placement.timing_points
        ttype = self.placement.timing_type or "none"
        if self.panel.timeline_mode == "timing" and ttype != "none" and tp:
            r = self.rect()
            clip_dur = self.placement.end - self.placement.start
            if clip_dur > 0:
                painter.save()
                painter.setClipRect(r)
                color = _TIMING_TICK_COLORS.get(ttype, _TIMING_TICK_DEFAULT_COLOR)
                pen = QPen(color)
                pen.setWidth(2)
                painter.setPen(pen)
                for pt in tp:
                    px = r.left() + (pt / clip_dur) * r.width()
                    painter.drawLine(QPointF(px, r.top()), QPointF(px, r.bottom()))
                    painter.setBrush(QBrush(color))
                    painter.drawEllipse(QPointF(px, r.top() + 3), 3, 3)
                painter.restore()
        if self.panel.timeline_mode == "keyframe" and self.placement.keyframes:
            r = self.rect()
            clip_dur = self.placement.end - self.placement.start
            if clip_dur > 0:
                painter.save()
                painter.setClipRect(r)
                kfs = self.placement.keyframes  # kept sorted by t
                mid_y = r.top() + r.height() / 2.0
                screen_pts = [(r.left() + (kf.get("t", 0.0) / clip_dur) * r.width(), kf) for kf in kfs]
                for i in range(len(screen_pts) - 1):
                    xa, kfa = screen_pts[i]
                    xb, _kfb = screen_pts[i + 1]
                    ease = kfa.get("ease") or list(DEFAULT_EASE)
                    is_linear = abs(ease[0] - ease[1]) < 1e-4 and abs(ease[2] - ease[3]) < 1e-4
                    pen = QPen(_KF_COLOR if is_linear else _KF_EASE_COLOR)
                    pen.setWidth(2)
                    if not is_linear:
                        pen.setStyle(Qt.PenStyle.DashLine)
                    painter.setPen(pen)
                    painter.drawLine(QPointF(xa, mid_y), QPointF(xb, mid_y))
                painter.setPen(QPen(QColor("#111"), 1))
                painter.setBrush(QBrush(_KF_COLOR))
                for px, _kf in screen_pts:
                    diamond = QPolygonF([
                        QPointF(px, mid_y - 5), QPointF(px + 5, mid_y),
                        QPointF(px, mid_y + 5), QPointF(px - 5, mid_y),
                    ])
                    painter.drawPolygon(diamond)
                painter.restore()
        self._draw_label(painter)

    def _draw_waveform_fast(self, painter, rect, option, peaks, peaks_per_sec: float):
        p = self.placement
        sound_dur = len(peaks) / peaks_per_sec
        clip_dur = p.end - p.start
        w, h = rect.width(), rect.height()
        if clip_dur <= 0 or sound_dur <= 0 or w <= 0 or h <= 0:
            return
        exp = option.exposedRect if option is not None else rect
        x_lo = max(0, int(math.floor(exp.left())))
        x_hi = min(int(math.ceil(w)), int(math.ceil(exp.right())))
        if x_hi <= x_lo:
            return
        offset = p.src_offset or 0.0
        tc = (_np.arange(x_lo, x_hi, dtype=_np.float64) + 0.5) / w * clip_dur  # clip-local time per pixel column
        if p.loop:
            ts = ((offset % sound_dur) + tc) % sound_dur
            valid = None
        else:
            ts = offset + tc
            valid = ts < sound_dur
        idx = _np.minimum((ts * peaks_per_sec).astype(_np.int64), len(peaks) - 1)
        cols = peaks[idx].astype(_np.float32)
        if valid is not None:
            cols[~valid] = -1.0
        amp = max(1.0, h / 2.0 - 2.0)
        img = raster_peak_columns(cols, int(math.ceil(h)), h / 2.0, amp, QColor(255, 255, 255, 150))
        if img is not None:
            painter.drawImage(QPointF(x_lo, 0), img)

    def _draw_waveform(self, painter, rect, option=None):
        cache = getattr(self.panel, "waveform_cache", None)
        if cache is None:
            return
        result = cache.get(self.panel.project_dir, self.placement.ref)
        if not result:
            return
        peaks, peaks_per_sec = result
        if len(peaks) == 0 or peaks_per_sec <= 0:
            return
        if _np is not None and hasattr(peaks, "dtype"):
            self._draw_waveform_fast(painter, rect, option, peaks, peaks_per_sec)
            return
        p = self.placement
        sound_dur = len(peaks) / peaks_per_sec
        clip_dur = p.end - p.start
        w = rect.width()
        h = rect.height()
        if clip_dur <= 0 or sound_dur <= 0 or w <= 0 or h <= 0:
            return
        mid_y = h / 2.0
        amp = max(1.0, h / 2.0 - 2.0)
        offset = p.src_offset or 0.0

        pen = QPen(QColor(255, 255, 255, 150))
        pen.setWidth(1)
        painter.setPen(pen)

        def draw_segment(seg_x0: float, seg_w: float, start_t: float, seg_dur: float):
            if seg_w <= 0 or seg_dur <= 0:
                return
            start_sample = max(0, int(start_t * peaks_per_sec))
            n_samples = max(0, min(len(peaks) - start_sample, int(round(seg_dur * peaks_per_sec))))
            if n_samples <= 0:
                return
            seg_w_i = max(1, int(round(seg_w)))
            for px in range(seg_w_i):
                idx = start_sample + int((px / seg_w_i) * n_samples)
                if idx >= len(peaks):
                    break
                peak = peaks[idx]
                x = seg_x0 + px
                painter.drawLine(QPointF(x, mid_y - amp * peak), QPointF(x, mid_y + amp * peak))

        if not p.loop:
            avail_dur = max(0.0, sound_dur - offset)
            drawn_dur = min(clip_dur, avail_dur)
            if drawn_dur > 0:
                drawn_w = max(1.0, w * (drawn_dur / clip_dur))
                draw_segment(0.0, drawn_w, offset, drawn_dur)
        else:
            eff_offset = (offset % sound_dur) if sound_dur > 0 else 0.0
            t = 0.0
            first = True
            iterations = 0
            while t < clip_dur - 1e-6 and iterations < 2000:
                start_t = eff_offset if first else 0.0
                avail_dur = max(0.0, sound_dur - start_t)
                seg_dur = min(avail_dur, clip_dur - t)
                if seg_dur <= 1e-6:
                    break
                seg_x0 = w * (t / clip_dur)
                seg_w = max(1.0, w * (seg_dur / clip_dur))
                draw_segment(seg_x0, seg_w, start_t, seg_dur)
                first = False
                t += seg_dur
                iterations += 1

    # hover
    def _edge_at(self, local_x: float) -> Optional[str]:
        r = self.rect()
        if r.width() < EDGE_PX * 3:
            return None
        if local_x <= EDGE_PX:
            return "start"
        if local_x >= r.width() - EDGE_PX:
            return "end"
        return None

    def hoverMoveEvent(self, event):
        if self.panel.timeline_mode == "timing":
            # In "add timing point" mode, clicking a clip adds a timing point
            # rather than dragging/resizing it, so show a plus/cross cursor.
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
            super().hoverMoveEvent(event)
            return
        edge = self._edge_at(event.pos().x())
        if edge:
            self.setCursor(QCursor(Qt.CursorShape.SizeHorCursor))
        else:
            self.setCursor(QCursor(Qt.CursorShape.OpenHandCursor))
        super().hoverMoveEvent(event)

    # drag
    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if shift:
            self.panel.toggle_select_item(self)
            if self not in self.panel.selected_items:
                # just deselected by this shift-click - nothing to drag
                self._drag_mode = None
                event.accept()
                return
        elif self in self.panel.selected_items and len(self.panel.selected_items) > 1:
            self.panel.select_item(self, keep_selection=True)
        else:
            self.panel.select_item(self)
        self._drag_mode = self._edge_at(event.pos().x()) or "move"
        self._drag_start_scene_pos = event.scenePos()
        self._orig_pos = QPointF(self.pos())
        self._orig_rect = self.rect()
        self._group = [it for it in self.panel.selected_items if it is not self]
        self._group_orig = {
            it: (QPointF(it.pos()), QRectF(it.rect()), it.placement.track) for it in self._group
        }
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._drag_mode:
            return
        pps = self.panel.pps
        dx = event.scenePos().x() - self._drag_start_scene_pos.x()
        dy = event.scenePos().y() - self._drag_start_scene_pos.y()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        exclude_ids = {self.placement.id} | {it.placement.id for it in self._group}

        if self._drag_mode == "move":
            new_x = max(GUTTER_W, self._orig_pos.x() + dx)
            width = self._orig_rect.width()
            new_y = self._orig_pos.y() + dy
            track = max(0, round((new_y - RULER_H) / self.panel.track_h))
            new_x = self.panel.snap_move_x(new_x, width, exclude_ids, track)
            new_x = self.panel.quantize_x(new_x)  # always land on a whole-frame boundary
            new_x = max(GUTTER_W, new_x)
            eff_dx = new_x - self._orig_pos.x()
            self.setPos(new_x, RULER_H + track * self.panel.track_h)
            orig_track = max(0, round((self._orig_pos.y() - RULER_H) / self.panel.track_h))
            track_delta = track - orig_track
            for it, (op, _orect, otrack) in self._group_orig.items():
                ix = max(GUTTER_W, op.x() + eff_dx)
                itrack = max(0, otrack + track_delta)
                it.setPos(ix, RULER_H + itrack * self.panel.track_h)

            view = self.scene().views()[0] if self.scene() and self.scene().views() else None
            if view is not None:
                vp = view.viewport()
                cur_item_scene_y = RULER_H + track * self.panel.track_h
                view_pt = view.mapFromScene(0, cur_item_scene_y)
                vy = view_pt.y()
                vh = vp.height()
                margin = max(self.panel.track_h, 30)
                vsb = view.verticalScrollBar()
                if vy < margin + RULER_H:
                    vsb.setValue(vsb.value() - SCRUB_AUTOSCROLL_MAX_PX // 2)
                elif vy > vh - margin:
                    vsb.setValue(vsb.value() + SCRUB_AUTOSCROLL_MAX_PX // 2)

        elif self._drag_mode == "end":
            own_track = self.placement.track
            if ctrl:
                end_x = event.scenePos().x()
                end_x = self.panel.snap_edge_x(end_x, exclude_ids, own_track)
                end_x = self.panel.quantize_x(end_x)
                new_w = max(MIN_DUR * pps, end_x - self._orig_pos.x())
                self.setRect(0, 0, new_w, self._orig_rect.height())
                for it, (op, orect, _otrack) in self._group_orig.items():
                    iw = max(MIN_DUR * pps, end_x - op.x())
                    it.setRect(0, 0, iw, orect.height())
            else:
                end_x = self._orig_pos.x() + self._orig_rect.width() + dx
                end_x = self.panel.snap_edge_x(end_x, exclude_ids, own_track)
                end_x = self.panel.quantize_x(end_x)
                new_w = max(MIN_DUR * pps, end_x - self._orig_pos.x())
                self.setRect(0, 0, new_w, self._orig_rect.height())
                for it, (op, orect, _otrack) in self._group_orig.items():
                    iw = max(MIN_DUR * pps, orect.width() + dx)
                    it.setRect(0, 0, iw, orect.height())

        elif self._drag_mode == "start":
            own_track = self.placement.track
            orig_end_x = self._orig_pos.x() + self._orig_rect.width()
            if ctrl:
                start_x = max(float(GUTTER_W), event.scenePos().x())
                start_x = self.panel.snap_edge_x(start_x, exclude_ids, own_track)
                start_x = self.panel.quantize_x(start_x)
                start_x = min(start_x, orig_end_x - MIN_DUR * pps)
                start_x = max(float(GUTTER_W), start_x)
                new_w = orig_end_x - start_x
                self.setRect(0, 0, new_w, self._orig_rect.height())
                self.setPos(start_x, self._orig_pos.y())
                for it, (op, orect, _otrack) in self._group_orig.items():
                    iend = op.x() + orect.width()
                    iw = max(MIN_DUR * pps, iend - start_x)
                    istart = iend - iw
                    it.setRect(0, 0, iw, orect.height())
                    it.setPos(istart, op.y())
            else:
                start_x = self._orig_pos.x() + dx
                start_x = self.panel.snap_edge_x(start_x, exclude_ids, own_track)
                start_x = self.panel.quantize_x(start_x)
                start_x = min(start_x, orig_end_x - MIN_DUR * pps)
                start_x = max(float(GUTTER_W), start_x)
                new_w = orig_end_x - start_x
                self.setRect(0, 0, new_w, self._orig_rect.height())
                self.setPos(start_x, self._orig_pos.y())
                for it, (op, orect, _otrack) in self._group_orig.items():
                    iw2 = max(MIN_DUR * pps, orect.width() - dx)
                    istart2 = max(float(GUTTER_W), op.x() + orect.width() - iw2)
                    it.setRect(0, 0, iw2, orect.height())
                    it.setPos(istart2, op.y())
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_mode:
            self.panel._commit_group_geometry([self] + self._group, self._drag_mode)
        self._drag_mode = None
        self._group = []
        self._group_orig = {}
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if self.placement.kind == "scene":
            self.panel.request_enter_scene.emit(self.placement.ref)
        event.accept()


class _RulerWaveformItem(QGraphicsItem):
    def __init__(self, peaks, peaks_per_sec: float, x0: float, clip_dur: float,
                 pix_w: float, height: float, src_offset: float, loop: bool,
                 parent: QGraphicsItem):
        super().__init__(parent)
        self._peaks = peaks
        self._peaks_per_sec = peaks_per_sec
        self._clip_dur = clip_dur
        self._width = pix_w
        self._height = height
        self._offset = src_offset
        self._loop = loop
        self._rect = QRectF(0, 0, max(0.0, pix_w), height)
        self.setPos(x0, 0)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)

    def boundingRect(self) -> QRectF:
        return self._rect

    def paint(self, painter, option, widget=None):
        peaks = self._peaks
        if len(peaks) == 0 or self._peaks_per_sec <= 0 or self._clip_dur <= 0 or self._width <= 0:
            return
        sound_dur = len(peaks) / self._peaks_per_sec
        if sound_dur <= 0:
            return
        w, h = self._width, self._height
        mid_y = h / 2.0
        amp = max(1.0, mid_y - 3.0)
        if _np is not None and hasattr(peaks, "dtype"):
            exp = option.exposedRect if option is not None else self._rect
            x_lo = max(0, int(math.floor(exp.left())))
            x_hi = min(int(math.ceil(w)), int(math.ceil(exp.right())))
            if x_hi <= x_lo:
                return
            tc = (_np.arange(x_lo, x_hi, dtype=_np.float64) + 0.5) / w * self._clip_dur
            if self._loop:
                ts = ((self._offset % sound_dur) + tc) % sound_dur
                valid = None
            else:
                ts = self._offset + tc
                valid = ts < sound_dur
            idx = _np.minimum((ts * self._peaks_per_sec).astype(_np.int64), len(peaks) - 1)
            cols = peaks[idx].astype(_np.float32)
            if valid is not None:
                cols[~valid] = -1.0
            img = raster_peak_columns(cols, int(math.ceil(h)), mid_y, amp, QColor(100, 200, 255, 80))
            if img is not None:
                painter.drawImage(QPointF(x_lo, 0), img)
            return
        pen = QPen(QColor(100, 200, 255, 80))
        pen.setWidth(1)
        painter.setPen(pen)
        w_i = int(w)
        for px in range(w_i):
            tc = (px + 0.5) / w * self._clip_dur
            if self._loop:
                ts = ((self._offset % sound_dur) + tc) % sound_dur
            else:
                ts = self._offset + tc
                if ts >= sound_dur:
                    continue
            idx = min(int(ts * self._peaks_per_sec), len(peaks) - 1)
            if idx < 0:
                continue
            peak = peaks[idx]
            painter.drawLine(
                QPointF(px, mid_y - amp * peak),
                QPointF(px, mid_y + amp * peak),
            )


class TimelineView(QGraphicsView):
    def __init__(self, panel: "TimelinePanel"):
        super().__init__(panel)
        self.panel = panel
        self.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        self.setBackgroundBrush(QBrush(QColor("#1b1b1b")))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setAcceptDrops(True)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.horizontalScrollBar().valueChanged.connect(self._on_scrolled)
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)

        self._scrubbing = False
        self._scrub_timer = QTimer(self)
        self._scrub_timer.setInterval(SCRUB_AUTOSCROLL_MS)
        self._scrub_timer.timeout.connect(self._on_scrub_tick)

    def _on_scrolled(self, _value: int):
        self.panel.reposition_chrome()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.panel.reposition_chrome()

    def _drop_time_track(self, pos) -> tuple:
        scene_pos = self.mapToScene(pos)
        t = self.panel.x_to_time(scene_pos.x())
        track = max(0, int((scene_pos.y() - RULER_H) // self.panel.track_h))
        return t, track

    # playhead scrub
    def _begin_scrub(self, pos: QPoint):
        self._scrubbing = True
        self._scrub_timer.start()
        self._scrub_to(pos)

    def _end_scrub(self):
        self._scrubbing = False
        self._scrub_timer.stop()

    def _scrub_to(self, pos: QPoint):
        t = self.panel.x_to_time(self.mapToScene(pos).x())
        if abs(t - self.panel.playhead) > 1e-9:
            self.panel.set_playhead(t)

    def _on_scrub_tick(self):
        if not self._scrubbing:
            self._scrub_timer.stop()
            return
        pos = self.viewport().mapFromGlobal(QCursor.pos())
        vis = self.mapToScene(self.viewport().rect()).boundingRect()
        sx = self.mapToScene(pos).x()
        content_left = vis.left() + GUTTER_W
        if sx > vis.right():
            step = min(sx - vis.right(), SCRUB_AUTOSCROLL_MAX_PX)
            self.ensureVisible(QRectF(vis.right() + step, vis.center().y(), 1, 1), 0, 0)
        elif sx < content_left and vis.left() > 0:
            step = min(content_left - sx, SCRUB_AUTOSCROLL_MAX_PX)
            self.ensureVisible(QRectF(vis.left() - step, vis.center().y(), 1, 1), 0, 0)
        self._scrub_to(pos)

    def dragEnterEvent(self, event: QDragEnterEvent):
        md = event.mimeData()
        if md.hasUrls() or decode_asset_mime(md) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent):
        md = event.mimeData()
        if md.hasUrls() or decode_asset_mime(md) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        md = event.mimeData()
        t, track = self._drop_time_track(event.position().toPoint())
        assets = decode_asset_mime_list(md)
        if len(assets) > 1:
            self._drop_multiple_assets(assets, t, track)
            event.acceptProposedAction()
            return
        if len(assets) == 1:
            kind, obj_id = assets[0]
            if kind == "source":
                self.panel.source_asset_dropped.emit(obj_id, t, track)
            else:
                pw, ph = (self.panel.project.width, self.panel.project.height) if self.panel.project else (0, 0)
                self.panel.create_placement(kind, obj_id, start=t, center=(pw / 2.0, ph / 2.0), track=track)
            event.acceptProposedAction()
            return
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if paths:
                self.panel.file_dropped.emit(paths[0], t, track)
                event.acceptProposedAction()
                return
        event.ignore()

    def _ask_multi_drop_layout(self, count: int) -> Optional[str]:
        box = QMessageBox(self)
        box.setWindowTitle(i18n.tr("timeline.multiDrop.title"))
        box.setText(i18n.tr("timeline.multiDrop.text").format(count=count))
        seq_btn = box.addButton(i18n.tr("timeline.multiDrop.sequential"), QMessageBox.ButtonRole.AcceptRole)
        tracks_btn = box.addButton(i18n.tr("timeline.multiDrop.separateTracks"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(seq_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is seq_btn:
            return "sequential"
        if clicked is tracks_btn:
            return "tracks"
        return None

    def _drop_multiple_assets(self, assets: list, t: float, track: int):
        project = self.panel.project
        if not project or not assets:
            return
        mode = self._ask_multi_drop_layout(len(assets))
        if mode is None:
            return
        pw, ph = project.width, project.height
        cur_t = t
        cur_track = track
        for kind, obj_id in assets:
            if kind == "source":
                src_name = project.sources.get(obj_id, "")
                if is_audio(src_name):
                    place_kind = "sound"
                    center = None
                elif is_video(src_name):
                    place_kind = "video"
                    center = None
                else:
                    place_kind = "image"
                    center = (pw / 2.0, ph / 2.0)
            else:
                place_kind = kind
                center = (pw / 2.0, ph / 2.0)
            this_track = track if mode == "sequential" else cur_track
            placement = self.panel.create_placement(
                place_kind, obj_id, start=cur_t, center=center, track=this_track
            )
            if placement is None:
                continue
            if mode == "sequential":
                cur_t = placement.end
            else:
                cur_track += 1

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            factor = 1.25 if delta > 0 else 0.8
            self.panel.set_pps(self.panel.pps * factor)
            event.accept()
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            step = 4 if delta > 0 else -4
            new_h = max(18, min(120, self.panel.track_h + step))
            if new_h != self.panel.track_h:
                self.panel.track_h = new_h
                self.panel.redraw()
            event.accept()
        elif mods & Qt.KeyboardModifier.AltModifier:
            sb = self.verticalScrollBar()
            sb.setValue(sb.value() - delta // 2)
            event.accept()
        else:
            sb = self.horizontalScrollBar()
            sb.setValue(sb.value() - delta // 2)
            event.accept()

    def _is_playhead_grab(self, pos: QPoint, scene_pos: QPointF) -> bool:
        px = self.panel.time_to_x(self.panel.playhead)
        if abs(scene_pos.x() - px) > PLAYHEAD_GRAB_PX:
            return False
        return not any(isinstance(i, PlacementItem) for i in self.items(pos))

    def mousePressEvent(self, event):
        self.setFocus()
        pos = event.pos()
        scene_pos = self.mapToScene(pos)
        if self.panel.is_ruler_point(scene_pos):
            if event.button() == Qt.MouseButton.LeftButton:
                self._begin_scrub(pos)
            event.accept()
            return
        if self.panel.is_gutter_point(scene_pos):
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._is_playhead_grab(pos, scene_pos):
            self._begin_scrub(pos)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.panel.timeline_mode == "timing":
            p = self.panel.placement_at(scene_pos)
            if p is not None:
                item = self.panel._items.get(p.id)
                if item is not None:
                    self.panel.select_item(item)
                if p.timing_type and p.timing_type != "none":
                    t_click = self.panel.x_to_time(scene_pos.x())
                    self.panel.toggle_timing_point(p, t_click)
            else:
                self.panel.select_item(None)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.panel.timeline_mode == "keyframe":
            p = self.panel.placement_at(scene_pos)
            if p is not None:
                item = self.panel._items.get(p.id)
                if item is not None:
                    self.panel.select_item(item)
                kf_idx = self.panel.keyframe_hit(p, scene_pos)
                if kf_idx is not None:
                    self.panel.jump_to_keyframe(p, kf_idx)
                    event.accept()
                    return
                seg_idx = self.panel.keyframe_segment_hit(p, scene_pos)
                if seg_idx is not None:
                    self.panel.edit_keyframe_ease(p, seg_idx, self)
                    event.accept()
                    return
            else:
                self.panel.select_item(None)
            event.accept()
            return
        item = self.itemAt(pos)
        if not isinstance(item, PlacementItem):
            self.panel.select_item(None)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._scrubbing:
            self._scrub_to(event.pos())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._scrubbing and event.button() == Qt.MouseButton.LeftButton:
            self._end_scrub()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.panel.delete_selected()
        elif event.key() == Qt.Key.Key_S:
            self.panel.split_selected_at_playhead()
        elif event.key() == Qt.Key.Key_T and self.panel.timeline_mode == "timing":
            p = self.panel.selected_placement
            if p is not None and p.timing_type and p.timing_type != "none":
                self.panel.toggle_timing_point(p, self.panel.playhead)
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        pos = event.pos()
        scene_pos = self.mapToScene(pos)
        if self.panel.is_ruler_point(scene_pos):
            self._show_ruler_waveform_menu(event.globalPos())
            return
        if self.panel.is_gutter_point(scene_pos):
            track = max(0, int((scene_pos.y() - RULER_H) // self.panel.track_h))
            self._show_track_menu(event.globalPos(), track)
            return
        if self.panel.timeline_mode == "keyframe":
            p = self.panel.placement_at(scene_pos)
            if p is not None:
                item = self.panel._items.get(p.id)
                if item is not None:
                    self.panel.select_item(item)
                kf_idx = self.panel.keyframe_hit(p, scene_pos)
                if kf_idx is not None:
                    self.panel.remove_keyframe(p, kf_idx)
                    return
                seg_idx = self.panel.keyframe_segment_hit(p, scene_pos)
                if seg_idx is not None:
                    self.panel.edit_keyframe_ease(p, seg_idx, self)
                    return
            return
        p = self.panel.placement_at(scene_pos)
        if p is None:
            return
        item = self.panel._items.get(p.id)
        if item is not None:
            self.panel.select_item(item)
        menu = QMenu(self)
        split_act = QAction(i18n.tr("timeline.menu.splitAtPlayhead"), self)
        split_act.triggered.connect(self.panel.split_selected_at_playhead)
        menu.addAction(split_act)
        if p.kind == "scene":
            enter_act = QAction(i18n.tr("timeline.menu.enterScene"), self)
            enter_act.triggered.connect(lambda: self.panel.request_enter_scene.emit(p.ref))
            menu.addAction(enter_act)
        if p.kind in ("sound", "video"):
            mute_act = QAction(i18n.tr("timeline.menu.unmute") if p.muted else i18n.tr("timeline.menu.mute"), self)
            mute_act.triggered.connect(lambda: self.panel._toggle_mute(p))
            menu.addAction(mute_act)
        if p.timing_type and p.timing_type != "none":
            menu.addSeparator()
            click_t = self.panel.x_to_time(scene_pos.x())
            tp_menu = menu.addMenu(i18n.tr("timeline.menu.timingPoints"))
            add_here_act = QAction(i18n.tr("timeline.menu.addPointHere"), self)
            add_here_act.triggered.connect(lambda: self.panel.add_timing_point_at(p, click_t))
            tp_menu.addAction(add_here_act)
            add_ph_act = QAction(i18n.tr("timeline.menu.addPointAtPlayhead"), self)
            add_ph_act.triggered.connect(lambda: self.panel.add_timing_point_at(p, self.panel.playhead))
            add_ph_act.setEnabled(p.start <= self.panel.playhead <= p.end)
            tp_menu.addAction(add_ph_act)
            tp_menu.addSeparator()
            remove_here_act = QAction(i18n.tr("timeline.menu.removeNearestPoint"), self)
            remove_here_act.triggered.connect(lambda: self.panel.remove_nearest_timing_point(p, click_t))
            remove_here_act.setEnabled(bool(p.timing_points))
            tp_menu.addAction(remove_here_act)
            clear_act = QAction(i18n.tr("timeline.menu.clearAllPoints"), self)
            clear_act.triggered.connect(lambda: self.panel.clear_timing_points(p))
            clear_act.setEnabled(bool(p.timing_points))
            tp_menu.addAction(clear_act)
        menu.addSeparator()
        del_act = QAction(i18n.tr("timeline.menu.delete"), self)
        del_act.triggered.connect(self.panel.delete_selected)
        menu.addAction(del_act)
        menu.exec(event.globalPos())

    def _show_track_menu(self, global_pos, track: int):
        scene = self.panel.current_scene()
        if not scene:
            return
        hidden = list(getattr(scene, "hidden_tracks", []))
        names = dict(getattr(scene, "track_names", {}))
        is_hidden = track in hidden
        
        menu = QMenu(self)
        title = menu.addAction(i18n.tr("timeline.menu.trackTitle").format(track=track, name=names.get(str(track), i18n.tr("timeline.menu.unnamed")))) 
        title.setEnabled(False)
        menu.addSeparator()
        
        vis_act = menu.addAction(i18n.tr("timeline.menu.show") if is_hidden else i18n.tr("timeline.menu.hide"))
        rename_act = menu.addAction(i18n.tr("timeline.menu.rename"))
        menu.addSeparator()
        move_up_act = menu.addAction(i18n.tr("timeline.menu.moveUp"))
        move_up_act.setEnabled(track > 0)
        move_down_act = menu.addAction(i18n.tr("timeline.menu.moveDown"))
        
        chosen = menu.exec(global_pos)
        if chosen is vis_act:
            if is_hidden:
                hidden = [t for t in hidden if t != track]
            else:
                hidden.append(track)
                sel = self.panel.selected_placement
                if sel is not None and sel.track == track:
                    self.panel.select_item(None)
            scene.hidden_tracks = hidden
            self.panel.changed.emit()
            self.panel.redraw()
        elif chosen is rename_act:
            from PyQt6.QtWidgets import QInputDialog
            cur_name = names.get(str(track), i18n.tr("timeline.trackDefaultName").format(track=track))
            new_name, ok = QInputDialog.getText(
                self, i18n.tr("timeline.renameTrack.title"), i18n.tr("timeline.renameTrack.prompt").format(track=track), text=cur_name)
            if ok:
                new_name = new_name.strip()
                if new_name:
                    names[str(track)] = new_name
                else:
                    names.pop(str(track), None)
                scene.track_names = names
                self.panel.changed.emit()
                self.panel.redraw()
        elif chosen is move_up_act:
            sc2 = self.panel.current_scene()
            if sc2:
                self.panel.push_undo_snapshot()
                self.panel.swap_tracks(sc2, track, track - 1)
                self.panel.changed.emit()
                self.panel.redraw()
        elif chosen is move_down_act:
            sc2 = self.panel.current_scene()
            if sc2:
                self.panel.push_undo_snapshot()
                self.panel.swap_tracks(sc2, track, track + 1)
                self.panel.changed.emit()
                self.panel.redraw()

    def _show_ruler_waveform_menu(self, global_pos):
        project = self.panel.project
        if not project:
            return
        audio_sources: dict[str, str] = {}
        for sc in project.scenes.values():
            for pl in sc.placements:
                if pl.kind in ("sound", "video") and pl.ref in project.sources:
                    audio_sources[pl.ref] = project.sources[pl.ref]
        if not audio_sources:
            menu = QMenu(self)
            menu.addAction(i18n.tr("timeline.menu.noAudioSources")).setEnabled(False)
            menu.exec(global_pos)
            return
        menu = QMenu(i18n.tr("timeline.menu.waveformsInRuler"), self)
        menu.addAction(i18n.tr("timeline.menu.waveformsInRuler")).setEnabled(False)
        menu.addSeparator()
        for uuid_ext, name in sorted(audio_sources.items(), key=lambda kv: kv[1].lower()):
            act = QAction(name, self)
            act.setCheckable(True)
            act.setChecked(uuid_ext in self.panel.ruler_waveforms)
            act.toggled.connect(lambda checked, uid=uuid_ext: self._toggle_ruler_waveform(uid, checked))
            menu.addAction(act)
        menu.exec(global_pos)

    def _toggle_ruler_waveform(self, uuid_ext: str, show: bool):
        if show:
            self.panel.ruler_waveforms.add(uuid_ext)
            self.panel.waveform_cache.get(self.panel.project_dir, uuid_ext)
        else:
            self.panel.ruler_waveforms.discard(uuid_ext)
        self.panel.changed.emit()
        self.panel.redraw()


class TrackManagerDialog(QDialog):
    def __init__(self, panel: "TimelinePanel", parent=None):
        super().__init__(parent)
        self.panel = panel
        self.setWindowTitle(i18n.tr("timeline.manageTracks.title"))
        self.setMinimumWidth(380)
        self.setMinimumHeight(320)

        layout = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setToolTip(i18n.tr("timeline.hintManageTracks"))
        self._body = QWidget()
        self._rows_layout = QVBoxLayout(self._body)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.addStretch(1)
        scroll.setWidget(self._body)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._rebuild()

    def _num_tracks(self, scene) -> int:
        max_track = max([p.track for p in scene.placements], default=0)
        return max(NUM_TRACKS_MIN, max_track + 2)

    def _rebuild(self):
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        scene = self.panel.current_scene()
        if not scene:
            return
        num_tracks = self._num_tracks(scene)
        hidden = set(getattr(scene, "hidden_tracks", []) or [])
        names = dict(getattr(scene, "track_names", {}) or {})
        for i in range(num_tracks):
            row = QWidget()
            hl = QHBoxLayout(row)
            hl.setContentsMargins(0, 2, 0, 2)
            hl.addWidget(QLabel(f"{i}:"))

            name_edit = QLineEdit(names.get(str(i), ""))
            name_edit.setPlaceholderText(i18n.tr("timeline.trackDefaultName").format(track=i))
            name_edit.editingFinished.connect(lambda idx=i, e=name_edit: self._rename(idx, e.text()))
            hl.addWidget(name_edit, 1)

            vis_check = QCheckBox(i18n.tr("timeline.manageTracks.visible"))
            vis_check.setChecked(i not in hidden)
            vis_check.toggled.connect(lambda on, idx=i: self._set_hidden(idx, not on))
            hl.addWidget(vis_check)

            _st = QApplication.style()
            up_btn = QPushButton()
            up_btn.setIcon(_st.standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
            up_btn.setIconSize(QSize(12, 12))
            up_btn.setFixedWidth(28)
            up_btn.setToolTip(i18n.tr("timeline.manageTracks.moveUpTip"))
            up_btn.setEnabled(i > 0)
            up_btn.clicked.connect(lambda _checked=False, idx=i: self._move(idx, -1))
            hl.addWidget(up_btn)

            down_btn = QPushButton()
            down_btn.setIcon(_st.standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
            down_btn.setIconSize(QSize(12, 12))
            down_btn.setFixedWidth(28)
            down_btn.setToolTip(i18n.tr("timeline.manageTracks.moveDownTip"))
            down_btn.setEnabled(i < num_tracks - 1)
            down_btn.clicked.connect(lambda _checked=False, idx=i: self._move(idx, 1))
            hl.addWidget(down_btn)

            self._rows_layout.insertWidget(i, row)

    def _rename(self, idx: int, text: str):
        scene = self.panel.current_scene()
        if not scene:
            return
        text = text.strip()
        names = dict(getattr(scene, "track_names", {}) or {})
        if text:
            names[str(idx)] = text
        else:
            names.pop(str(idx), None)
        scene.track_names = names
        self.panel.changed.emit()
        self.panel.redraw()

    def _set_hidden(self, idx: int, hidden_now: bool):
        scene = self.panel.current_scene()
        if not scene:
            return
        hidden = set(getattr(scene, "hidden_tracks", []) or [])
        if hidden_now:
            hidden.add(idx)
            sel = self.panel.selected_placement
            if sel is not None and sel.track == idx:
                self.panel.select_item(None)
        else:
            hidden.discard(idx)
        scene.hidden_tracks = sorted(hidden)
        self.panel.changed.emit()
        self.panel.redraw()

    def _move(self, idx: int, direction: int):
        scene = self.panel.current_scene()
        if not scene:
            return
        other = idx + direction
        if other < 0:
            return
        self.panel.push_undo_snapshot()
        self.panel.swap_tracks(scene, idx, other)
        self.panel.changed.emit()
        self.panel.redraw()
        self._rebuild()


class TimelinePanel(QWidget):
    selection_changed = pyqtSignal(object)
    placement_changed = pyqtSignal(object)
    playhead_changed = pyqtSignal(float)
    request_enter_scene = pyqtSignal(str)
    file_dropped = pyqtSignal(str, float, int)
    source_asset_dropped = pyqtSignal(str, float, int)
    changed = pyqtSignal()  # non-placement scene/track structure changes (hide/show, rename, reorder)

    # labels
    def label_for_placement(self, p: Placement) -> str:
        if not self.project:
            return p.ref or p.kind
        if p.kind == "scene":
            target = self.project.scenes.get(p.ref)
            label = i18n.tr("timeline.label.scene") + (target.name if target else "?")
        elif p.kind == "symbol":
            edef = self.project.symbol_defs.get(p.ref)
            label = i18n.tr("timeline.label.symbol") + (f"{edef.elem_class}#{edef.elem_id}" if edef else "?")
        elif p.kind == "sound":
            src_name = self.project.sources.get(p.ref)
            label = (i18n.tr("timeline.label.muted") if p.muted else i18n.tr("timeline.label.sound")) + (src_name or "?")
        elif p.kind == "video":
            src_name = self.project.sources.get(p.ref)
            muted_pfx = i18n.tr("timeline.label.muted") if p.muted else ""
            speed_sfx = f" {p.speed:.1f}x" if p.speed and abs(p.speed - 1.0) > 0.05 else ""
            label = f"{muted_pfx}{i18n.tr("timeline.label.video")}{src_name or '?'}{speed_sfx}"
        elif p.kind == "image":
            src_name = self.project.sources.get(p.ref)
            label = i18n.tr("timeline.label.image") + (src_name or "?")
        elif p.kind == "shape":
            stype = p.shape_type or "rect"
            if stype == "text":
                label = i18n.tr("timeline.label.text") + (p.shape_text or "").replace("\n", " ")
            else:
                label = f"{i18n.tr("timeline.label.shape")}{stype}"
        else:
            label = p.kind
        if p.layer:
            label += f" L{p.layer}"
        return label

    def effective_duration(self) -> float:
        if not self.project:
            return 8.0
        return max(self.project.duration_of(self.scene_id), 8.0)

    def set_side_mode(self, is_side: bool):
        if is_side == self.side_mode:
            return
        self.side_mode = is_side
        self.view.resetTransform()
        if is_side:
            self.view.rotate(90)
        self.redraw()

    def _text_rotation(self) -> float:
        return -90.0 if self.side_mode else 0.0

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project: Optional[Project] = None
        self.scene_id: str = MAIN_ID
        self.pps = PPS_DEFAULT
        self.track_h = TRACK_H_DEFAULT
        self.playhead = 0.0
        self._items: dict[str, PlacementItem] = {}
        self.selected_item: Optional[PlacementItem] = None
        self.selected_placement: Optional[Placement] = None
        self.selected_items: set = set()
        self.snap_enabled = True
        self.snap_to_clips = True
        self.allow_recursive_nesting = False
        self.max_nesting_depth = 8
        self.side_mode = False
        self._playhead_line: Optional[QGraphicsLineItem] = None
        self._playhead_head: Optional[QGraphicsPolygonItem] = None
        self._ruler_item: Optional[QGraphicsRectItem] = None
        self._header_item: Optional[QGraphicsRectItem] = None
        self._corner_item: Optional[QGraphicsRectItem] = None
        self._chrome_x = 0.0
        self._chrome_y = 0.0
        self.project_dir: Optional[str] = None
        self.waveform_cache = WaveformCache(self)
        self.waveform_cache.peaks_ready.connect(self._on_waveform_ready)
        self._clipboard: Optional[dict] = None
        self._undo_stack: list = []
        self._redo_stack: list = []
        self._MAX_UNDO = 50
        self.timeline_mode = "normal"

        self.scene_label = QLabel(i18n.tr("timeline.mainTimeline"))
        self.scene_label.setStyleSheet("font-weight: 600;")

        zoom_out_btn = QPushButton("-")
        zoom_out_btn.setFixedWidth(28)
        zoom_out_btn.clicked.connect(lambda: self.set_pps(self.pps / 1.25))
        zoom_in_btn = QPushButton("+")
        zoom_in_btn.setFixedWidth(28)
        zoom_in_btn.clicked.connect(lambda: self.set_pps(self.pps * 1.25))

        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(PPS_MIN, PPS_MAX)
        self.zoom_slider.setValue(PPS_DEFAULT)
        self.zoom_slider.setFixedWidth(120)
        self.zoom_slider.valueChanged.connect(self.set_pps)

        split_btn = QPushButton(i18n.tr("timeline.btn.split"))
        split_btn.clicked.connect(self.split_selected_at_playhead)
        delete_btn = QPushButton(i18n.tr("timeline.btn.delete"))
        delete_btn.clicked.connect(self.delete_selected)
        tracks_btn = QPushButton(i18n.tr("timeline.btn.tracks"))
        tracks_btn.setToolTip(i18n.tr("timeline.btn.tracksTip"))
        tracks_btn.clicked.connect(self.open_track_manager)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.scene_label)
        toolbar.addStretch(1)
        toolbar.addWidget(split_btn)
        toolbar.addWidget(delete_btn)
        toolbar.addWidget(tracks_btn)
        toolbar.addSpacing(12)
        toolbar.addWidget(QLabel(i18n.tr("timeline.zoom")))
        toolbar.addWidget(zoom_out_btn)
        toolbar.addWidget(self.zoom_slider)
        toolbar.addWidget(zoom_in_btn)

        self.gscene = QGraphicsScene(self)
        self.view = TimelineView(self)
        self.view.setScene(self.gscene)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(toolbar)
        layout.addWidget(self.view)

    # timeline mode
    def set_timeline_mode(self, mode: str):
        if mode not in ("normal", "timing", "keyframe"):
            return
        self.timeline_mode = mode
        if mode == "timing":
            # "add timing point" mode: show a plus/cross cursor over the view so
            # it's clear clicking a clip will add a timing point there.
            self.view.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        else:
            self.view.unsetCursor()
        self.redraw()

    # time <-> scene x
    def time_to_x(self, t: float) -> float:
        return GUTTER_W + t * self.pps

    def x_to_time(self, x: float) -> float:
        return max(0.0, (x - GUTTER_W) / self.pps)

    # frame snapping
    def frame_duration(self) -> float:
        fps = self.project.fps if (self.project and self.project.fps > 0) else DEFAULT_FPS
        return 1.0 / max(1, fps)

    def snap_time_to_frame(self, t: float) -> float:
        step = self.frame_duration()
        return round(round(t / step) * step, 9)

    def quantize_x(self, x: float) -> float:
        return self.time_to_x(self.snap_time_to_frame(self.x_to_time(x)))

    # clip/playhead magnetic snapping (used while moving/resizing clips) --
    def _snap_candidates(self, exclude_ids: set, own_track: Optional[int]) -> list:
        cands: list = []
        if self.snap_to_clips:
            scene = self.current_scene()
            if scene:
                for p in scene.placements:
                    if p.id in exclude_ids:
                        continue
                    if own_track is not None and p.track == own_track:
                        continue  # only snap to clips on OTHER tracks
                    cands.append((self.time_to_x(p.start), CLIP_SNAP_PX))
                    cands.append((self.time_to_x(p.end), CLIP_SNAP_PX))
        if self.snap_enabled:
            cands.append((self.time_to_x(self.playhead), SNAP_PH_PX))
        return cands

    def snap_edge_x(self, raw_x: float, exclude_ids: set, own_track: Optional[int]) -> float:
        best_x, best_d = raw_x, None
        for cx, thr in self._snap_candidates(exclude_ids, own_track):
            d = abs(cx - raw_x)
            if d <= thr and (best_d is None or d < best_d):
                best_d, best_x = d, cx
        return best_x

    def snap_move_x(self, raw_x: float, width: float, exclude_ids: set, own_track: Optional[int]) -> float:
        start_snapped = self.snap_edge_x(raw_x, exclude_ids, own_track)
        end_snapped = self.snap_edge_x(raw_x + width, exclude_ids, own_track)
        d_start = abs(start_snapped - raw_x)
        d_end = abs(end_snapped - (raw_x + width))
        if d_start <= 0.0 and d_end <= 0.0:
            return raw_x
        if d_end <= 0.0 or (d_start > 0.0 and d_start <= d_end):
            return start_snapped
        return end_snapped - width

    def is_ruler_point(self, scene_pos: QPointF) -> bool:
        return self._ruler_item is not None and self._chrome_y <= scene_pos.y() <= self._chrome_y + RULER_H

    def is_gutter_point(self, scene_pos: QPointF) -> bool:
        return self._header_item is not None and self._chrome_x <= scene_pos.x() <= self._chrome_x + GUTTER_W

    def placement_at(self, scene_pos: QPointF) -> Optional[Placement]:
        scene = self.current_scene()
        if not scene:
            return None
        t = self.x_to_time(scene_pos.x())
        track = max(0, int((scene_pos.y() - RULER_H) // self.track_h))
        candidates = [p for p in scene.placements if p.track == track and p.start <= t <= p.end]
        if not candidates:
            return None
        return candidates[-1]

    @property
    def ruler_waveforms(self) -> set:
        if self.project is None:
            return set()
        return self.project.ruler_waveforms

    # API
    def set_project(self, project: Project):
        self.project = project
        self.waveform_cache.clear()
        self.set_scene(MAIN_ID)

    def set_project_dir(self, project_dir: Optional[str]):
        if project_dir == self.project_dir:
            return
        self.project_dir = project_dir
        self.waveform_cache.clear()
        self.redraw()

    def _on_waveform_ready(self, uuid_ext: str):
        for item in self._items.values():
            if item.placement.kind == "sound" and item.placement.ref == uuid_ext:
                item.update()

    def set_scene(self, scene_id: str):
        if not self.project or scene_id not in self.project.scenes:
            return
        self.scene_id = scene_id
        self.scene_label.setText(self.project.scenes[scene_id].name)
        self.playhead = 0.0
        self.select_item(None)
        self.redraw()
        self.playhead_changed.emit(self.playhead)

    def set_pps(self, value: float):
        self.pps = max(PPS_MIN, min(PPS_MAX, round(value)))
        if self.zoom_slider.value() != self.pps:
            self.zoom_slider.blockSignals(True)
            self.zoom_slider.setValue(self.pps)
            self.zoom_slider.blockSignals(False)
        self.redraw()

    def set_playhead(self, t: float):
        self.playhead = self.snap_time_to_frame(max(0.0, t))
        self._draw_playhead()
        self.playhead_changed.emit(self.playhead)

    def current_scene(self):
        return self.project.scenes.get(self.scene_id) if self.project else None

    # tracks: reordering, hide/show, rename
    def swap_tracks(self, scene, a: int, b: int):
        if scene is None or a == b or a < 0 or b < 0:
            return
        for p in scene.placements:
            if p.track == a:
                p.track = b
            elif p.track == b:
                p.track = a
        hidden = set(getattr(scene, "hidden_tracks", []) or [])
        new_hidden = set()
        for t in hidden:
            if t == a:
                new_hidden.add(b)
            elif t == b:
                new_hidden.add(a)
            else:
                new_hidden.add(t)
        scene.hidden_tracks = sorted(new_hidden)
        names = dict(getattr(scene, "track_names", {}) or {})
        na = names.pop(str(a), None)
        nb = names.pop(str(b), None)
        if na is not None:
            names[str(b)] = na
        if nb is not None:
            names[str(a)] = nb
        scene.track_names = names

    def open_track_manager(self):
        if not self.current_scene():
            return
        dlg = TrackManagerDialog(self, self)
        dlg.exec()

    def _scene_offsets_from_current(self, max_depth: int = 8, max_results: int = 300) -> dict:
        project = self.project
        if not project or not self.scene_id:
            return {}
        result: dict[str, set] = {self.scene_id: {0.0}}
        frontier = [(self.scene_id, 0.0)]
        visited_edges = set()
        depth = 0
        while frontier and depth < max_depth and sum(len(v) for v in result.values()) < max_results:
            next_frontier = []
            for node, off in frontier:
                scene_obj = project.scenes.get(node)
                if scene_obj:
                    for q in scene_obj.placements:
                        if q.kind != "scene" or q.ref not in project.scenes:
                            continue
                        key = (node, "down", q.id)
                        if key in visited_edges:
                            continue
                        visited_edges.add(key)
                        new_off = off - q.start
                        if new_off not in result.setdefault(q.ref, set()):
                            result[q.ref].add(new_off)
                            next_frontier.append((q.ref, new_off))
                for other_id, other_scene in project.scenes.items():
                    for p in other_scene.placements:
                        if p.kind != "scene" or p.ref != node:
                            continue
                        key = (other_id, "up", p.id)
                        if key in visited_edges:
                            continue
                        visited_edges.add(key)
                        new_off = off + p.start
                        if new_off not in result.setdefault(other_id, set()):
                            result[other_id].add(new_off)
                            next_frontier.append((other_id, new_off))
            frontier = next_frontier
            depth += 1
        return result

    # draw
    def redraw(self):
        prev_selected_ids = {it.placement.id for it in self.selected_items}
        self._ruler_item = self._header_item = self._corner_item = None
        self._playhead_line = self._playhead_head = None
        self.gscene.clear()
        self._items.clear()
        if not self.project:
            return
        scene = self.current_scene()
        if not scene:
            return

        duration = self.effective_duration()
        max_track = max([p.track for p in scene.placements], default=0)
        num_tracks = max(NUM_TRACKS_MIN, max_track + 2)
        width = GUTTER_W + duration * self.pps + 200
        height = RULER_H + num_tracks * self.track_h
        self.gscene.setSceneRect(0, 0, width, height)

        # track lanes (the number labels live in the pinned column below)
        hidden_tracks_set = set(getattr(scene, "hidden_tracks", []))
        for i in range(num_tracks):
            y = RULER_H + i * self.track_h
            if i in hidden_tracks_set:
                bg_color = QColor("#191919")  # dimmer for hidden
            else:
                bg_color = QColor("#232323") if i % 2 == 0 else QColor("#1e1e1e")
            lane = self.gscene.addRect(0, y, width, self.track_h, QPen(Qt.PenStyle.NoPen), QBrush(bg_color))
            lane.setZValue(-10)

        # placements
        prev_selected_id = self.selected_placement.id if self.selected_placement else None
        self.selected_item = None
        hidden_tracks = set(getattr(scene, "hidden_tracks", []))
        for placement in scene.placements:
            if placement.track in hidden_tracks:
                continue  # skip hidden tracks
            item = PlacementItem(placement, self)
            x = self.time_to_x(placement.start)
            y = RULER_H + placement.track * self.track_h
            w = max(4.0, (placement.end - placement.start) * self.pps)
            item.setRect(0, 0, w, self.track_h - 2)
            item.setPos(x, y)
            self.gscene.addItem(item)
            self._items[placement.id] = item
            if placement.id == prev_selected_id:
                self.selected_item = item
                self.selected_placement = placement
        self.selected_items = {self._items[pid] for pid in prev_selected_ids if pid in self._items}

        self._build_chrome(width, height, num_tracks, duration)
        self._draw_playhead()
        self.reposition_chrome()

    def _place_text(self, item: QGraphicsSimpleTextItem, x: float, cell_top: float, cell_h: float):
        rot = self._text_rotation()
        item.setRotation(rot)
        if rot:
            item.setPos(x + 2, cell_top + cell_h - 2)
        else:
            item.setPos(x + 2, cell_top + 2)

    def _build_chrome(self, width: float, height: float, num_tracks: int, duration: float):
        no_pen = QPen(Qt.PenStyle.NoPen)

        # ruler
        ruler = QGraphicsRectItem(0, 0, width, RULER_H)
        ruler.setPen(no_pen)
        ruler.setBrush(QBrush(QColor("#262626")))
        ruler.setZValue(60)
        ruler.setAcceptHoverEvents(True)
        ruler.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

        project = self.project
        scene_offsets = self._scene_offsets_from_current() if (self.ruler_waveforms and project) else {}
        for uuid_ext in list(self.ruler_waveforms):
            result = self.waveform_cache.get(self.project_dir, uuid_ext)
            if not result or not project:
                continue
            peaks, peaks_per_sec = result
            if len(peaks) == 0 or peaks_per_sec <= 0:
                continue
            seen: set = set()
            for other_scene_id, other_scene in project.scenes.items():
                offsets = scene_offsets.get(other_scene_id)
                for pl in other_scene.placements:
                    if pl.kind not in ("sound", "video") or pl.ref != uuid_ext:
                        continue
                    clip_dur = pl.end - pl.start
                    if clip_dur <= 0:
                        continue
                    local_offsets = {0.0} if pl.pinned else (offsets or set())
                    for off in local_offsets:
                        local_start = pl.start - off
                        local_end = pl.end - off
                        if local_end <= 0 or local_start >= duration:
                            continue  # entirely outside the visible ruler range
                        dedupe_key = (pl.id, round(local_start, 4))
                        if dedupe_key in seen:
                            continue
                        seen.add(dedupe_key)
                        x0 = self.time_to_x(local_start)
                        pix_w = clip_dur * self.pps
                        wave_item = _RulerWaveformItem(
                            peaks, peaks_per_sec, x0, clip_dur, pix_w, RULER_H,
                            pl.src_offset or 0.0, bool(pl.loop), ruler,
                        )
                        wave_item.setZValue(0)

        fps = (self.project.fps if self.project and self.project.fps > 0 else 30)
        step = self._time_step_for_pps(fps)
        frame_step = 1.0 / fps
        show_frames = step < 1.0
        t = 0.0
        while t <= duration + step:
            x = self.time_to_x(t)
            tick = QGraphicsLineItem(x, 0, x, RULER_H, ruler)
            tick.setPen(QPen(QColor("#555")))
            if show_frames:
                frame_num = round(t * fps)
                label_text = f"f{frame_num}"
            else:
                label_text = _fmt_time(t)
            label = QGraphicsSimpleTextItem(label_text, ruler)
            label.setBrush(QBrush(QColor("#bbb")))
            self._place_text(label, x, 0, RULER_H)
            t += step
            t = round(t / frame_step) * frame_step
        rule = QGraphicsLineItem(0, RULER_H - 0.5, width, RULER_H - 0.5, ruler)
        rule.setPen(QPen(QColor("#111")))
        self.gscene.addItem(ruler)
        self._ruler_item = ruler

        header = QGraphicsRectItem(0, 0, GUTTER_W, height)
        header.setPen(no_pen)
        header.setBrush(QBrush(QColor("#202020")))
        header.setZValue(55)
        header.setAcceptHoverEvents(True)
        header.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        scene = self.current_scene()
        hidden_tracks = set(getattr(scene, "hidden_tracks", [])) if scene else set()
        track_names = dict(getattr(scene, "track_names", {})) if scene else {}

        _style = QApplication.style()
        _icon_eye_on  = _style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView)
        _icon_eye_off = _style.standardIcon(QStyle.StandardPixmap.SP_FileDialogListView)
        _btn_size = max(16, min(self.track_h - 4, 20))
        _icon_sz  = QSize(_btn_size - 4, _btn_size - 4)
        _num_w = 20  # width reserved for the track# label

        for i in range(num_tracks):
            y = RULER_H + i * self.track_h
            is_hidden = i in hidden_tracks

            if is_hidden:
                dim_rect = QGraphicsRectItem(0, y, GUTTER_W, self.track_h, header)
                dim_rect.setPen(no_pen)
                dim_rect.setBrush(QBrush(QColor("#161616")))

            btn_y = y + (self.track_h - _btn_size) / 2

            # track number (always shown, left-most) ---
            lbl_color = QColor("#444") if is_hidden else QColor("#888")
            num_lbl = QGraphicsSimpleTextItem(str(i), header)
            num_lbl.setBrush(QBrush(lbl_color))
            self._place_text(num_lbl, 4, y, self.track_h)

            # eye (visibility) toggle ---
            eye_btn = QPushButton()
            eye_btn.setIcon(_icon_eye_off if is_hidden else _icon_eye_on)
            eye_btn.setIconSize(_icon_sz)
            eye_btn.setFixedSize(_btn_size, _btn_size)
            eye_btn.setFlat(True)
            eye_btn.setToolTip((i18n.tr("timeline.eye.showTip") if is_hidden else i18n.tr("timeline.eye.hideTip")).format(track=i))
            eye_btn.setStyleSheet(
                "QPushButton { background: transparent; border: none; padding: 0; }"
                "QPushButton:hover { background: rgba(255,255,255,30); border-radius: 2px; }"
            )
            eye_btn.clicked.connect(lambda _=False, t=i: self.toggle_track_visibility(t))

            eye_proxy = QGraphicsProxyWidget(header)
            eye_proxy.setWidget(eye_btn)
            eye_proxy.setPos(_num_w, btn_y)
            eye_proxy.setZValue(56)

            # editable track name field ---
            name_x = _num_w + _btn_size + 6
            name_w = max(20, GUTTER_W - name_x - 6)
            name_edit = QLineEdit()
            name_edit.setText(track_names.get(str(i), ""))
            name_edit.setPlaceholderText(i18n.tr("timeline.trackDefaultName").format(track=i))
            name_edit.setFixedSize(int(name_w), _btn_size)
            name_edit.setStyleSheet(
                "QLineEdit { background: rgba(255,255,255,15); color: #ccc; border: 1px solid #3a3a3a; "
                "border-radius: 2px; padding: 0 3px; }"
                "QLineEdit:focus { border: 1px solid #5a5a5a; }"
            )
            name_edit.editingFinished.connect(lambda idx=i, e=name_edit: self._rename_track_inline(idx, e.text()))

            name_proxy = QGraphicsProxyWidget(header)
            name_proxy.setWidget(name_edit)
            name_proxy.setPos(name_x, btn_y)
            name_proxy.setZValue(56)

            sep = QGraphicsLineItem(0, y + self.track_h - 0.5, GUTTER_W, y + self.track_h - 0.5, header)
            sep.setPen(QPen(QColor("#2e2e2e")))

        edge = QGraphicsLineItem(GUTTER_W - 0.5, 0, GUTTER_W - 0.5, height, header)
        edge.setPen(QPen(QColor("#111")))
        self.gscene.addItem(header)
        self._header_item = header

        # corner
        corner = QGraphicsRectItem(0, 0, GUTTER_W, RULER_H)
        corner.setPen(no_pen)
        corner.setBrush(QBrush(QColor("#2b2b2b")))
        corner.setZValue(65)
        corner.setAcceptHoverEvents(True)
        corner.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        self.gscene.addItem(corner)
        self._corner_item = corner

    def reposition_chrome(self):
        if self._ruler_item is None or self._header_item is None or self._corner_item is None:
            return
        vis = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        self._chrome_x = max(0.0, vis.left())
        self._chrome_y = max(0.0, vis.top())
        try:
            self._ruler_item.setPos(0, self._chrome_y)
            self._header_item.setPos(self._chrome_x, 0)
            self._corner_item.setPos(self._chrome_x, self._chrome_y)
            if self._playhead_head is not None:
                self._playhead_head.setPos(self._playhead_head.x(), self._chrome_y)
        except RuntimeError:
            self._ruler_item = self._header_item = self._corner_item = None
            self._playhead_head = None

    def _draw_playhead(self):
        x = self.time_to_x(self.playhead)
        h = self.gscene.sceneRect().height()
        if self._playhead_line is None:
            line = QGraphicsLineItem()
            line.setPen(QPen(QColor("#e04b4b"), 2))
            line.setZValue(50)
            self.gscene.addItem(line)
            self._playhead_line = line
            head = QGraphicsPolygonItem(QPolygonF([
                QPointF(-6, 2), QPointF(6, 2), QPointF(6, 10), QPointF(0, RULER_H - 2), QPointF(-6, 10),
            ]))
            head.setPen(QPen(Qt.PenStyle.NoPen))
            head.setBrush(QBrush(QColor("#e04b4b")))
            head.setZValue(61)
            self.gscene.addItem(head)
            self._playhead_head = head
        self._playhead_line.setLine(x, 0, x, h)
        self._playhead_head.setPos(x, self._chrome_y)

    def _time_step_for_pps(self, fps: float = 30) -> float:
        frame = 1.0 / max(1, fps)
        for n in (1, 2, 5, 10, 15):
            candidate = n * frame
            if candidate * self.pps >= 40:
                return candidate
        for step in (0.25, 0.5, 1, 2, 5, 10, 30, 60):
            if step * self.pps >= 60:
                return step
        return 60.0

    # selection
    def select_item(self, item: Optional[PlacementItem], keep_selection: bool = False):
        if item is None:
            self.selected_items = set()
        elif keep_selection and item in self.selected_items:
            pass
        else:
            self.selected_items = {item}
        self.selected_item = item
        self.selected_placement = item.placement if item else None
        for it in self._items.values():
            it.update()
        self.selection_changed.emit(self.selected_placement)

    def toggle_select_item(self, item: PlacementItem):
        if item in self.selected_items:
            self.selected_items.discard(item)
            if self.selected_item is item:
                self.selected_item = next(iter(self.selected_items), None)
                self.selected_placement = self.selected_item.placement if self.selected_item else None
        else:
            self.selected_items.add(item)
            self.selected_item = item
            self.selected_placement = item.placement
        for it in self._items.values():
            it.update()
        self.selection_changed.emit(self.selected_placement)

    # drag commit
    def _commit_group_geometry(self, items: list, mode: str):
        scene = self.current_scene()
        if not scene:
            self.redraw()
            return
        computed = []
        for item in items:
            p = item.placement
            new_start = self.x_to_time(item.pos().x())
            new_end = new_start + item.rect().width() / self.pps
            new_track = max(0, round((item.pos().y() - RULER_H) / self.track_h))
            if new_end - new_start < MIN_DUR:
                new_end = new_start + MIN_DUR
                
            # bound media duration if looping is disabled
            if p.kind in ("sound", "video") and not getattr(p, "loop", False) and self.project_dir and p.ref:
                media_dur = None
                path = os.path.join(self.project_dir, p.ref)
                if p.kind == "sound":
                    media_dur = probe_duration_seconds(path)
                elif p.kind == "video":
                    try:
                        from ..video import probe_video_duration
                        media_dur = probe_video_duration(path)
                    except Exception:
                        pass
                
                if media_dur and media_dur > 0:
                    speed = getattr(p, "speed", 1.0)
                    speed = speed if speed and speed > 0 else 1.0
                    eff_dur = media_dur / speed
                    if p.kind in ("video", "sound"):
                        max_dur = media_dur / speed
                    else:
                        max_dur = media_dur
                    
                    if new_end - new_start > max_dur:
                        new_end = new_start + max_dur
                        
            computed.append((p, new_start, new_end, new_track))

        moving_ids = {p.id for p, _s, _e, _t in computed}
        for p, new_start, new_end, new_track in computed:
            if self._overlaps_many(scene, moving_ids, new_track, new_start, new_end):
                self.redraw()  # bail out on the whole group - discard uncommitted moves
                return

        self.push_undo_snapshot()
        for p, new_start, new_end, new_track in computed:
            if mode == "start" and p.kind in ("scene", "sound", "video"):
                speed = p.speed if p.speed and p.speed > 0 else 1.0
                delta = (new_start - p.start) * speed
                p.src_offset = max(0.0, (p.src_offset or 0.0) + delta)
            p.start = new_start
            p.end = new_end
            p.track = new_track
        self.placement_changed.emit(self.selected_placement)
        self.redraw()

    @staticmethod
    def _overlaps_many(scene, moving_ids: set, track: int, start: float, end: float) -> bool:
        for other in scene.placements:
            if other.id in moving_ids or other.track != track:
                continue
            if end <= other.start or start >= other.end:
                continue
            return True
        return False

    @staticmethod
    def _overlaps(scene, placement_id: str, track: int, start: float, end: float) -> bool:
        for other in scene.placements:
            if other.id == placement_id or other.track != track:
                continue
            if end <= other.start or start >= other.end:
                continue
            return True
        return False

    def _free_track(self, scene, start: float, end: float) -> int:
        used = {p.track for p in scene.placements if not (p.end <= start or p.start >= end)}
        track = 0
        while track in used:
            track += 1
        return track

    # edit ops
    def push_undo_snapshot(self):
        scene = self.current_scene()
        if not scene:
            return
        snapshot = [p.to_dict() for p in scene.placements]
        self._undo_stack.append((self.scene_id, snapshot))
        if len(self._undo_stack) > self._MAX_UNDO:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        sid, snapshot = self._undo_stack.pop()
        if not self.project or sid not in self.project.scenes:
            return False
        target_scene = self.project.scenes[sid]
        redo_snap = [p.to_dict() for p in target_scene.placements]
        self._redo_stack.append((sid, redo_snap))
        target_scene.placements = [Placement.from_dict(d) for d in snapshot]
        self.select_item(None)
        self.redraw()
        self.placement_changed.emit(None)
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        sid, snapshot = self._redo_stack.pop()
        if not self.project or sid not in self.project.scenes:
            return False
        target_scene = self.project.scenes[sid]
        undo_snap = [p.to_dict() for p in target_scene.placements]
        self._undo_stack.append((sid, undo_snap))
        if len(self._undo_stack) > self._MAX_UNDO:
            self._undo_stack.pop(0)
        target_scene.placements = [Placement.from_dict(d) for d in snapshot]
        self.select_item(None)
        self.redraw()
        self.placement_changed.emit(None)
        return True

    def copy_selected(self) -> bool:
        if not self.selected_placement:
            return False
        self._clipboard = self.selected_placement.to_dict()
        return True

    def cut_selected(self) -> bool:
        if not self.copy_selected():
            return False
        scene = self.current_scene()
        p = self.selected_placement
        if scene and p:
            self.push_undo_snapshot()
            try:
                scene.placements.remove(p)
            except ValueError:
                pass
            self.select_item(None)
            self.redraw()
            self.placement_changed.emit(None)
        return True

    def paste_from_clipboard(self, t: float) -> Optional[Placement]:
        if not self._clipboard:
            return None
        self.push_undo_snapshot()
        d = dict(self._clipboard)
        dur = d.get("end", 0.0) - d.get("start", 0.0)
        d["id"] = new_uuid()
        d["start"] = t
        d["end"] = t + max(MIN_DUR, dur)
        new_p = Placement.from_dict(d)
        scene = self.current_scene()
        if not scene:
            return None
        new_p.track = self._free_track(scene, new_p.start, new_p.end)
        scene.placements.append(new_p)
        self.redraw()
        self.select_item(self._items.get(new_p.id))
        self.placement_changed.emit(new_p)
        return new_p

    def delete_selected(self, _push_snapshot: bool = True):
        scene = self.current_scene()
        if not scene or not self.selected_items:
            return
        to_remove = {it.placement.id: it.placement for it in self.selected_items}
        if _push_snapshot:
            self.push_undo_snapshot()
        scene.placements = [p for p in scene.placements if p.id not in to_remove]
        self.select_item(None)
        self.redraw()
        self.placement_changed.emit(None)

    def split_selected_at_playhead(self):
        scene = self.current_scene()
        p = self.selected_placement
        if not scene or not p:
            return
        t = self.playhead
        if t <= p.start + 0.02 or t >= p.end - 0.02:
            return
        self.push_undo_snapshot()
        new_p = replace(p, id=new_uuid(), start=t, timing_points=[])
        if p.kind in ("scene", "sound", "video"):
            speed = p.speed if p.speed and p.speed > 0 else 1.0
            elapsed = (t - p.start) * (speed if p.kind == "video" else 1.0)
            new_p.src_offset = (p.src_offset or 0.0) + elapsed
        p.end = t
        p.timing_points = [pt for pt in p.timing_points if pt <= (t - p.start)]
        scene.placements.append(new_p)
        self.select_item(None)
        self.redraw()
        self.placement_changed.emit(p)

    # timing points (add/remove, used by click-in-timing-mode and the context menu)
    def toggle_timing_point(self, p: Placement, t_abs: float):
        t_local = t_abs - p.start
        dur = p.end - p.start
        if not (0 <= t_local <= dur):
            return
        snap = TIMING_POINT_SNAP_PX / self.pps
        self.push_undo_snapshot()
        for i, pt in enumerate(p.timing_points):
            if abs(pt - t_local) < snap:
                p.timing_points.pop(i)
                self.placement_changed.emit(p)
                self.redraw()
                return
        p.timing_points.append(t_local)
        p.timing_points.sort()
        self.placement_changed.emit(p)
        self.redraw()

    def add_timing_point_at(self, p: Placement, t_abs: float) -> bool:
        t_local = t_abs - p.start
        dur = p.end - p.start
        if not (0 <= t_local <= dur):
            return False
        snap = TIMING_POINT_SNAP_PX / self.pps
        if any(abs(pt - t_local) < snap for pt in p.timing_points):
            return False
        self.push_undo_snapshot()
        p.timing_points.append(t_local)
        p.timing_points.sort()
        self.placement_changed.emit(p)
        self.redraw()
        return True

    def remove_nearest_timing_point(self, p: Placement, t_abs: float) -> bool:
        if not p.timing_points:
            return False
        t_local = t_abs - p.start
        idx = min(range(len(p.timing_points)), key=lambda i: abs(p.timing_points[i] - t_local))
        self.push_undo_snapshot()
        p.timing_points.pop(idx)
        self.placement_changed.emit(p)
        self.redraw()
        return True

    def clear_timing_points(self, p: Placement) -> bool:
        if not p.timing_points:
            return False
        self.push_undo_snapshot()
        p.timing_points.clear()
        self.placement_changed.emit(p)
        self.redraw()
        return True

    # position keyframes (used by keyframe-mode clicks and the context menu)
    def _kf_screen_x(self, p: Placement, kf: dict) -> float:
        return self.time_to_x(p.start + kf.get("t", 0.0))

    def keyframe_hit(self, p: Placement, scene_pos: QPointF) -> Optional[int]:
        if not p.keyframes:
            return None
        item = self._items.get(p.id)
        if item is None:
            return None
        top = item.pos().y()
        bottom = top + self.track_h - 2
        if not (top - 4 <= scene_pos.y() <= bottom + 4):
            return None
        for i, kf in enumerate(p.keyframes):
            if abs(scene_pos.x() - self._kf_screen_x(p, kf)) <= KEYFRAME_HIT_PX:
                return i
        return None

    def keyframe_segment_hit(self, p: Placement, scene_pos: QPointF) -> Optional[int]:
        if len(p.keyframes) < 2:
            return None
        item = self._items.get(p.id)
        if item is None:
            return None
        top = item.pos().y()
        bottom = top + self.track_h - 2
        if not (top <= scene_pos.y() <= bottom):
            return None
        for i in range(len(p.keyframes) - 1):
            xa = self._kf_screen_x(p, p.keyframes[i])
            xb = self._kf_screen_x(p, p.keyframes[i + 1])
            lo, hi = min(xa, xb), max(xa, xb)
            if lo + KEYFRAME_HIT_PX < scene_pos.x() < hi - KEYFRAME_HIT_PX:
                return i
        return None

    def jump_to_keyframe(self, p: Placement, idx: int):
        if 0 <= idx < len(p.keyframes):
            self.set_playhead(p.start + p.keyframes[idx].get("t", 0.0))

    def remove_keyframe(self, p: Placement, idx: int):
        if not (0 <= idx < len(p.keyframes)):
            return
        self.push_undo_snapshot()
        p.keyframes.pop(idx)
        self.placement_changed.emit(p)
        self.redraw()

    def edit_keyframe_ease(self, p: Placement, seg_idx: int, parent_widget=None):
        if not (0 <= seg_idx < len(p.keyframes) - 1):
            return
        kf = p.keyframes[seg_idx]
        ease = list(kf.get("ease") or DEFAULT_EASE)
        dlg = BezierEaseDialog(ease, parent_widget or self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.push_undo_snapshot()
            kf["ease"] = dlg.result_ease()
            self.placement_changed.emit(p)
            self.redraw()

    def _toggle_mute(self, placement: Placement):
        placement.muted = not placement.muted
        self.redraw()
        self.placement_changed.emit(placement)

    def toggle_track_visibility(self, track: int):
        scene = self.current_scene()
        if not scene:
            return
        hidden = list(getattr(scene, "hidden_tracks", []) or [])
        if track in hidden:
            hidden = [t for t in hidden if t != track]
        else:
            hidden.append(track)
            sel = self.selected_placement
            if sel is not None and sel.track == track:
                self.select_item(None)
        scene.hidden_tracks = sorted(hidden)
        self.changed.emit()
        self.redraw()

    def _rename_track_inline(self, track: int, text: str):
        scene = self.current_scene()
        if not scene:
            return
        names = dict(getattr(scene, "track_names", {}) or {})
        text = text.strip()
        if text:
            if names.get(str(track)) == text:
                return
            names[str(track)] = text
        else:
            if str(track) not in names:
                return
            names.pop(str(track), None)
        scene.track_names = names
        self.changed.emit()
        self.redraw()

    def insert_asset_at_playhead(self, kind: str, ref_id: str) -> Optional[Placement]:
        return self.create_placement(kind, ref_id, start=self.playhead)

    def create_placement(
        self,
        kind: str,
        ref_id: str,
        *,
        start: float,
        center: Optional[tuple] = None,
        top_left: Optional[tuple] = None,
        dur: Optional[float] = None,
        track: Optional[int] = None,
    ) -> Optional[Placement]:
        scene = self.current_scene()
        if not self.project or not scene:
            return None
        if kind == "scene":
            would_cycle = ref_id == self.scene_id or self.project.scene_reaches(ref_id, self.scene_id)
            if would_cycle and not self.allow_recursive_nesting:
                scene_name = self.project.scenes[ref_id].name if ref_id in self.project.scenes else ref_id
                QMessageBox.warning(
                    self, i18n.tr("timeline.recursiveNesting.title"),
                    i18n.tr("timeline.recursiveNesting.message").format(name=scene_name),
                )
                return None

        pw, ph = self.project.width, self.project.height
        anim_in = anim_out = "none"
        anim_in_fade = anim_out_fade = False
        anim_in_dur = anim_out_dur = anim_scale = anim_tilt = 0.0
        default_xy = (40, 40)
        full_frame = False
        if kind == "scene":
            if self.scene_id == MAIN_ID:
                default_box = (0, 0, pw, ph)
                full_frame = True
            else:
                default_box = (60, 60, 240, 180)
                default_xy = (60, 60)
            default_dur = 4.0
        elif kind == "symbol":
            edef = self.project.symbol_defs.get(ref_id)
            bw, bh = (edef.width, edef.height) if edef and edef.width and edef.height else (160, 120)
            default_box = (40, 40, bw, bh)
            default_dur = 2.5
            if edef:
                anim_in, anim_out = edef.anim_in, edef.anim_out
                anim_in_dur, anim_out_dur = edef.anim_in_dur, edef.anim_out_dur
                anim_scale, anim_tilt = edef.anim_scale, edef.anim_tilt_deg
                anim_in_fade, anim_out_fade = edef.anim_in_fade, edef.anim_out_fade
        elif kind == "image":
            default_box = (40, 40, 160, 120)
            default_dur = 2.5
            if self.project_dir:
                try:
                    from PIL import Image as _PILImage
                    img_path = os.path.join(self.project_dir, ref_id)
                    with _PILImage.open(img_path) as _im:
                        iw, ih = _im.size
                    if iw > 0 and ih > 0:
                        default_box = (40, 40, iw, ih)
                except Exception:
                    pass
        elif kind == "video":
            default_box = (0, 0, pw, ph)
            full_frame = True
            probed_dur = None
            if self.project_dir:
                from ..video import probe_video_duration
                path = os.path.join(self.project_dir, ref_id)
                probed_dur = probe_video_duration(path)
                try:
                    import cv2
                    cap = cv2.VideoCapture(path)
                    if cap.isOpened():
                        vw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                        vh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                        if vw > 0 and vh > 0:
                            default_box = (0, 0, vw, vh)
                    cap.release()
                except Exception:
                    pass
            default_dur = probed_dur if probed_dur and probed_dur > 0 else 5.0
        elif kind == "shape":
            default_box = (40, 40, 120, 120)
            default_dur = 2.5
        else:  # sound
            default_box = (40, 40, 50, 50)
            probed = None
            if self.project_dir:
                probed = probe_duration_seconds(os.path.join(self.project_dir, ref_id))
            default_dur = probed if probed and probed > 0 else 3.0

        bw, bh = default_box[2], default_box[3]
        dur = dur if dur is not None else default_dur

        if full_frame:
            x, y = 0, 0
        elif center is not None:
            x, y = center[0] - bw / 2.0, center[1] - bh / 2.0
        elif top_left is not None:
            x, y = top_left
        else:
            x, y = default_xy

        end = start + dur
        if track is None:
            track = self._free_track(scene, start, end)

        placement = Placement(
            id=new_uuid(), kind=kind, ref="" if kind == "shape" else ref_id,
            track=track,
            start=start, end=end,
            x=round(x), y=round(y), w=round(bw), h=round(bh),
            anim_in=anim_in, anim_out=anim_out,
            anim_in_dur=anim_in_dur, anim_out_dur=anim_out_dur,
            anim_scale=anim_scale, anim_tilt_deg=anim_tilt,
            anim_in_fade=anim_in_fade, anim_out_fade=anim_out_fade,
        )
        if kind == "shape":
            placement.shape_type = ref_id
        elif kind == "image":
            # animated GIF/WebP loops by default (static images ignore it)
            placement.loop = True
        self.push_undo_snapshot()
        scene.placements.append(placement)
        self.redraw()
        self.select_item(self._items.get(placement.id))
        self.placement_changed.emit(placement)
        return placement


def _fmt_time(t: float) -> str:
    m = int(t // 60)
    s = t - m * 60
    return f"{m}:{s:04.1f}" if m else f"{s:.1f}s"
