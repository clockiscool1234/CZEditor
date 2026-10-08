from __future__ import annotations

import math
from typing import Optional

from PyQt6.QtCore import QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..compositor import ImageCache, render_frame
from ..dnd import decode_asset_mime
from ..model import MAIN_ID, Project, keyframe_xy, keyframe_transform, upsert_keyframe
from ..render_worker import FrameRenderer

ZOOM_PRESETS = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0]
HANDLE_SIZE = 8.0
MIN_PLACEMENT_SIZE = 4.0
_SNAP_DIST = 8  # scene-pixel threshold below which a snap fires
CROPPABLE_KINDS = {"image", "video", "symbol", "scene", "shape"}

# colours used in crop / mask modes
_CROP_COLOUR   = QColor("#28e0ff")
_MASK_COLOUR   = QColor("#ff9f28")
_GHOST_COLOUR  = QColor(255, 255, 255, 45)   # full-source extent overlay
_CROP_HANDLE_C = QColor("#28e0ff")
_MASK_HANDLE_C = QColor("#ff9f28")


def _snap_placement(x: float, y: float, w: float, h: float,
                    pw: float, ph: float) -> tuple:
    snap_cols = [0.0, pw / 4, pw / 2, pw * 3 / 4, pw]
    snap_rows = [0.0, ph / 4, ph / 2, ph * 3 / 4, ph]

    placement_xs = [x, x + w / 2, x + w] # left, centre, right
    placement_ys = [y, y + h / 2, y + h] # top,  centre, bottom

    snaps: list = []

    # x axis
    best_dx = None
    best_col = None
    best_pidx = None
    for pi, px in enumerate(placement_xs):
        for col in snap_cols:
            d = abs(px - col)
            if d < _SNAP_DIST and (best_dx is None or d < best_dx):
                best_dx = d
                best_col = col
                best_pidx = pi
    if best_col is not None:
        # shift x so that placement_xs[best_pidx] == best_col
        offsets = [0.0, w / 2, w]
        x = best_col - offsets[best_pidx]
        snaps.append(("x", best_col))

    # y axis
    best_dy = None
    best_row = None
    best_qidx = None
    for qi, qy in enumerate(placement_ys):
        for row in snap_rows:
            d = abs(qy - row)
            if d < _SNAP_DIST and (best_dy is None or d < best_dy):
                best_dy = d
                best_row = row
                best_qidx = qi
    if best_row is not None:
        offsets = [0.0, h / 2, h]
        y = best_row - offsets[best_qidx]
        snaps.append(("y", best_row))

    return x, y, snaps


class _CanvasWidget(QWidget):
    def __init__(self, panel: "PreviewPanel"):
        super().__init__()
        self.panel = panel
        self.setAutoFillBackground(False)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._drag: Optional[dict] = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        image = self.panel.current_image
        if image is None or image.isNull():
            painter.end()
            return
        rs = max(0.001, self.panel.render_scale)
        scale = self.panel.effective_scale() / rs
        w = image.width() * scale
        h = image.height() * scale
        x = (self.width() - w) / 2 + self.panel._pan_x
        y = (self.height() - h) / 2 + self.panel._pan_y
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(QRect(int(x), int(y), int(w), int(h)), image)

        if self.panel.mode in ("normal", "timing", "keyframe") and self.panel.selected_placement_id:
            p = self.panel._find_active(self.panel.selected_placement_id)
            if p is not None:
                eff_x, eff_y = self.panel.effective_xy(p)
                rect = self.panel._scene_rect_to_screen(eff_x, eff_y, p.w, p.h)
                if rect is not None:
                    rot = getattr(p, 'rotation', 0.0) or 0.0
                    cx, cy = rect.center().x(), rect.center().y()

                    painter.save()
                    painter.translate(cx, cy)
                    painter.rotate(rot)
                    painter.translate(-cx, -cy)

                    painter.setPen(QPen(QColor("#ffd24a"), 2))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(rect)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor("#ffd24a"))

                    handles = self._handle_rects(rect)
                    for corner, hrect in handles.items():
                        if corner == "rot":
                            painter.setBrush(QColor("#4a8bff"))
                            painter.drawEllipse(hrect)
                            painter.setPen(QPen(QColor("#4a8bff"), 2))
                            n_rect = handles["n"]
                            painter.drawLine(
                                QPointF(n_rect.center().x(), n_rect.top()),
                                QPointF(hrect.center().x(), hrect.bottom())
                            )
                            painter.setPen(Qt.PenStyle.NoPen)
                            painter.setBrush(QColor("#ffd24a"))
                        else:
                            painter.drawRect(hrect)

                    painter.restore()

                if self.panel.mode == "keyframe" and p.keyframes:
                    pts = []
                    for kf in p.keyframes:
                        kr = self.panel._scene_rect_to_screen(
                            kf.get("x", p.x), kf.get("y", p.y), p.w, p.h)
                        if kr is not None:
                            pts.append(kr.center())
                    if pts:
                        painter.save()
                        pen = QPen(QColor("#ffd24a"), 1, Qt.PenStyle.DashLine)
                        painter.setPen(pen)
                        for i in range(len(pts) - 1):
                            painter.drawLine(pts[i], pts[i + 1])
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(QColor("#ffd24a"))
                        for pt in pts:
                            painter.drawEllipse(pt, 4, 4)
                        painter.restore()

        if self.panel.mode in ("normal", "timing", "keyframe") and len(self.panel.selected_ids) > 1:
            for pid in self.panel.selected_ids:
                if pid == self.panel.selected_placement_id:
                    continue
                gp = self.panel._find_active(pid)
                if gp is None:
                    continue
                geff_x, geff_y = self.panel.effective_xy(gp)
                grect = self.panel._scene_rect_to_screen(geff_x, geff_y, gp.w, gp.h)
                if grect is None:
                    continue
                grot = getattr(gp, "rotation", 0.0) or 0.0
                painter.save()
                gcx, gcy = grect.center().x(), grect.center().y()
                painter.translate(gcx, gcy)
                painter.rotate(grot)
                painter.translate(-gcx, -gcy)
                painter.setPen(QPen(QColor("#ffd24a"), 2, Qt.PenStyle.DashLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(grect)
                painter.restore()

        if self._drag and self._drag.get("kind") == "move":
            snaps = self._drag.get("snaps") or []
            if snaps and self.panel.project:
                p_sel = self.panel._find_active(self.panel.selected_placement_id)
                if p_sel is not None:
                    pw, ph = self.panel.project.width, self.panel.project.height
                    guide_pen_v = QPen(QColor("#00d4ff"), 1, Qt.PenStyle.DashLine)
                    guide_pen_h = QPen(QColor("#00d4ff"), 1, Qt.PenStyle.DashLine)
                    dot_brush = QColor("#00d4ff")
                    painter.save()
                    for axis, scene_coord in snaps:
                        if axis == "x":
                            scr = self.panel._scene_rect_to_screen(scene_coord, 0, 0, ph)
                            if scr is not None:
                                sx = scr.x()
                                painter.setPen(guide_pen_v)
                                painter.drawLine(QPointF(sx, 0), QPointF(sx, self.height()))
                                painter.setBrush(dot_brush)
                                painter.setPen(Qt.PenStyle.NoPen)
                                painter.drawEllipse(QPointF(sx, self.height() / 2), 3, 3)
                        else:
                            scr = self.panel._scene_rect_to_screen(0, scene_coord, pw, 0)
                            if scr is not None:
                                sy = scr.y()
                                painter.setPen(guide_pen_h)
                                painter.drawLine(QPointF(0, sy), QPointF(self.width(), sy))
                                painter.setBrush(dot_brush)
                                painter.setPen(Qt.PenStyle.NoPen)
                                painter.drawEllipse(QPointF(self.width() / 2, sy), 3, 3)
                    painter.restore()

        # crop mode overlay 
        if self.panel.mode == "crop" and self.panel.selected_placement_id:
            p = self.panel._find_active(self.panel.selected_placement_id)
            if p is not None and p.kind in CROPPABLE_KINDS:
                rect = self.panel._scene_rect_to_screen(p.x, p.y, p.w, p.h)
                if rect is not None:
                    rot = getattr(p, 'rotation', 0.0) or 0.0
                    cx, cy = rect.center().x(), rect.center().y()
                    painter.save()
                    painter.translate(cx, cy)
                    painter.rotate(rot)
                    painter.translate(-cx, -cy)

                    # ghost: full uncropped extent
                    full_rect = self._full_crop_rect_screen(p)
                    if full_rect is not None:
                        ghost_fill = QColor(255, 255, 255, 30)
                        painter.setPen(QPen(QColor(255, 255, 255, 80), 1, Qt.PenStyle.DashLine))
                        painter.setBrush(ghost_fill)
                        painter.drawRect(full_rect)

                    # visible (cropped) box outline
                    painter.setPen(QPen(_CROP_COLOUR, 2))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(rect)

                    # edge + corner handles (cyan)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(_CROP_HANDLE_C)
                    for hrect in self._crop_mode_handles(rect).values():
                        painter.drawRect(hrect)

                    painter.restore()

        # mask mode overlay 
        if self.panel.mode == "mask" and self.panel.selected_placement_id:
            p = self.panel._find_active(self.panel.selected_placement_id)
            if p is not None:
                rect = self.panel._scene_rect_to_screen(p.x, p.y, p.w, p.h)
                if rect is not None:
                    rot = getattr(p, 'rotation', 0.0) or 0.0
                    cx, cy = rect.center().x(), rect.center().y()
                    painter.save()
                    painter.translate(cx, cy)
                    painter.rotate(rot)
                    painter.translate(-cx, -cy)

                    painter.setPen(QPen(_MASK_COLOUR, 2, Qt.PenStyle.DashLine))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(rect)

                    mtype = getattr(p, "mask_type", "none") or "none"
                    if mtype not in ("none", "image"):
                        mask_fill = QColor(_MASK_COLOUR)
                        mask_fill.setAlpha(55)
                        painter.setBrush(mask_fill)
                        painter.setPen(QPen(_MASK_COLOUR, 2))
                        if mtype == "ellipse":
                            painter.drawEllipse(rect)
                        elif mtype == "rounded_rect":
                            frac = getattr(p, "mask_radius", 0.2) or 0.2
                            r = frac * min(rect.width(), rect.height()) / 2.0
                            painter.drawRoundedRect(rect, r, r)
                    elif mtype == "image":
                        painter.setPen(QPen(_MASK_COLOUR, 2))
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.drawRect(rect.adjusted(4, 4, -4, -4))

                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(_MASK_HANDLE_C)
                    for hrect in self._crop_mode_handles(rect).values():
                        painter.drawRect(hrect)

                    painter.restore()

        painter.end()

    # normal-mode hit-test
    def _handle_rects(self, rect: QRectF) -> dict:
        s = HANDLE_SIZE
        mid_x = rect.center().x()
        mid_y = rect.center().y()
        return {
            "nw": QRectF(rect.left() - s / 2, rect.top() - s / 2, s, s),
            "n":  QRectF(mid_x - s / 2,        rect.top() - s / 2, s, s),
            "ne": QRectF(rect.right() - s / 2,  rect.top() - s / 2, s, s),
            "e":  QRectF(rect.right() - s / 2,  mid_y - s / 2, s, s),
            "se": QRectF(rect.right() - s / 2,  rect.bottom() - s / 2, s, s),
            "s":  QRectF(mid_x - s / 2,        rect.bottom() - s / 2, s, s),
            "sw": QRectF(rect.left() - s / 2,  rect.bottom() - s / 2, s, s),
            "w":  QRectF(rect.left() - s / 2,  mid_y - s / 2, s, s),
            "rot": QRectF(mid_x - s / 2,       rect.top() - s * 2.5, s, s),
        }

    def _crop_mode_handles(self, rect: QRectF) -> dict:
        s = HANDLE_SIZE
        mid_x = rect.center().x()
        mid_y = rect.center().y()
        return {
            "nw": QRectF(rect.left()  - s/2, rect.top()    - s/2, s, s),
            "n":  QRectF(mid_x        - s/2, rect.top()    - s/2, s, s),
            "ne": QRectF(rect.right() - s/2, rect.top()    - s/2, s, s),
            "e":  QRectF(rect.right() - s/2, mid_y         - s/2, s, s),
            "se": QRectF(rect.right() - s/2, rect.bottom() - s/2, s, s),
            "s":  QRectF(mid_x        - s/2, rect.bottom() - s/2, s, s),
            "sw": QRectF(rect.left()  - s/2, rect.bottom() - s/2, s, s),
            "w":  QRectF(rect.left()  - s/2, mid_y         - s/2, s, s),
        }

    def _full_crop_rect_screen(self, p) -> Optional[QRectF]:
        cl = max(0.0, getattr(p, "crop_l", 0.0) or 0.0)
        ct = max(0.0, getattr(p, "crop_t", 0.0) or 0.0)
        cr = max(0.0, getattr(p, "crop_r", 0.0) or 0.0)
        cb = max(0.0, getattr(p, "crop_b", 0.0) or 0.0)
        vis_w = max(0.02, 1.0 - cl - cr)
        vis_h = max(0.02, 1.0 - ct - cb)
        full_w = p.w / vis_w
        full_h = p.h / vis_h
        full_x = p.x - cl * full_w
        full_y = p.y - ct * full_h
        return self.panel._scene_rect_to_screen(full_x, full_y, full_w, full_h)

    def _rotate_point(self, px: float, py: float, cx: float, cy: float, angle_deg: float):
        rad = math.radians(-angle_deg)
        dx, dy = px - cx, py - cy
        rx = dx * math.cos(rad) - dy * math.sin(rad)
        ry = dx * math.sin(rad) + dy * math.cos(rad)
        return cx + rx, cy + ry

    def _resize_anchor_origin(self, x0: float, y0: float, w0: float, h0: float, rot0: float,
                               new_w: float, new_h: float, corner: str):
        frac_x = 1.0 if "w" in corner else (0.0 if "e" in corner else 0.5)
        frac_y = 1.0 if "n" in corner else (0.0 if "s" in corner else 0.5)

        cx0, cy0 = x0 + w0 / 2.0, y0 + h0 / 2.0
        ax0, ay0 = (frac_x - 0.5) * w0, (frac_y - 0.5) * h0
        ax1, ay1 = (frac_x - 0.5) * new_w, (frac_y - 0.5) * new_h

        if rot0:
            rad = math.radians(rot0)
            cos_r, sin_r = math.cos(rad), math.sin(rad)
            anchor_wx = cx0 + ax0 * cos_r - ay0 * sin_r
            anchor_wy = cy0 + ax0 * sin_r + ay0 * cos_r
            new_cx = anchor_wx - (ax1 * cos_r - ay1 * sin_r)
            new_cy = anchor_wy - (ax1 * sin_r + ay1 * cos_r)
        else:
            new_cx = cx0 + ax0 - ax1
            new_cy = cy0 + ay0 - ay1

        return new_cx - new_w / 2.0, new_cy - new_h / 2.0

    def _hit_test(self, pos):
        panel = self.panel
        mode = panel.mode

        # crop mode hit-test 
        if mode == "crop" and panel.selected_placement_id:
            p = panel._find_active(panel.selected_placement_id)
            if p is not None and p.kind in CROPPABLE_KINDS:
                rect = panel._scene_rect_to_screen(p.x, p.y, p.w, p.h)
                if rect is not None:
                    rot = getattr(p, 'rotation', 0.0) or 0.0
                    cx, cy = rect.center().x(), rect.center().y()
                    lx, ly = self._rotate_point(pos.x(), pos.y(), cx, cy, rot)
                    lpos = QPointF(lx, ly)
                    for corner, hrect in self._crop_mode_handles(rect).items():
                        if hrect.contains(lpos):
                            return "crop_mode", corner
                    if rect.contains(lpos):
                        return "move", p
            for p in reversed(panel._active_placements()):
                if p.id == panel.selected_placement_id:
                    continue
                rect = panel._scene_rect_to_screen(p.x, p.y, p.w, p.h)
                if rect is not None and rect.contains(pos):
                    return "move", p
            return None, None

        # mask mode hit-test 
        if mode == "mask" and panel.selected_placement_id:
            p = panel._find_active(panel.selected_placement_id)
            if p is not None:
                rect = panel._scene_rect_to_screen(p.x, p.y, p.w, p.h)
                if rect is not None:
                    rot = getattr(p, 'rotation', 0.0) or 0.0
                    cx, cy = rect.center().x(), rect.center().y()
                    lx, ly = self._rotate_point(pos.x(), pos.y(), cx, cy, rot)
                    lpos = QPointF(lx, ly)
                    for corner, hrect in self._crop_mode_handles(rect).items():
                        if hrect.contains(lpos):
                            return "mask_mode", corner
                    if rect.contains(lpos):
                        return "move", p
            for p in reversed(panel._active_placements()):
                if p.id == panel.selected_placement_id:
                    continue
                rect = panel._scene_rect_to_screen(p.x, p.y, p.w, p.h)
                if rect is not None and rect.contains(pos):
                    return "move", p
            return None, None

        # normal / timing / keyframe mode hit-test
        if panel.selected_placement_id:
            p = panel._find_active(panel.selected_placement_id)
            if p is not None:
                eff_x, eff_y = panel.effective_xy(p)
                rect = panel._scene_rect_to_screen(eff_x, eff_y, p.w, p.h)
                if rect is not None:
                    rot = getattr(p, 'rotation', 0.0) or 0.0
                    cx, cy = rect.center().x(), rect.center().y()
                    lx, ly = self._rotate_point(pos.x(), pos.y(), cx, cy, rot)
                    lpos = QPointF(lx, ly)
                    for corner, hrect in self._handle_rects(rect).items():
                        if hrect.contains(lpos):
                            return "handle", corner
                    if rect.contains(lpos):
                        return "move", p
        for p in reversed(panel._active_placements()):
            eff_x, eff_y = panel.effective_xy(p)
            rect = panel._scene_rect_to_screen(eff_x, eff_y, p.w, p.h)
            if rect is None:
                continue
            rot = getattr(p, 'rotation', 0.0) or 0.0
            if rot:
                cx, cy = rect.center().x(), rect.center().y()
                lx, ly = self._rotate_point(pos.x(), pos.y(), cx, cy, rot)
                lpos = QPointF(lx, ly)
                if rect.contains(lpos):
                    return "move", p
            else:
                if rect.contains(pos):
                    return "move", p
        return None, None

    # mouse
    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        panel = self.panel
        pos = event.position()
        if panel.mode == "pan":
            self._drag = {"kind": "pan", "start": pos, "pan0": (panel._pan_x, panel._pan_y)}
            return
        if panel.mode not in ("normal", "timing", "crop", "mask", "keyframe"):
            return
        kind, payload = self._hit_test(pos)

        if kind == "crop_mode":
            p = panel._find_active(panel.selected_placement_id)
            if p is not None:
                cl = max(0.0, getattr(p, "crop_l", 0.0) or 0.0)
                ct = max(0.0, getattr(p, "crop_t", 0.0) or 0.0)
                cr = max(0.0, getattr(p, "crop_r", 0.0) or 0.0)
                cb = max(0.0, getattr(p, "crop_b", 0.0) or 0.0)
                vis_w = max(0.02, 1.0 - cl - cr)
                vis_h = max(0.02, 1.0 - ct - cb)
                full_w = p.w / vis_w
                full_h = p.h / vis_h
                full_x = p.x - cl * full_w
                full_y = p.y - ct * full_h
                self._drag = {
                    "kind": "crop_mode",
                    "corner": payload,
                    "start": pos,
                    "box0": (p.x, p.y, p.w, p.h),
                    "full_box": (full_x, full_y, full_w, full_h),
                    "crop0": (cl, ct, cr, cb),
                    "rot0": (getattr(p, "rotation", 0.0) or 0.0),
                    "placement": p,
                }
            return

        if kind == "mask_mode":
            p = panel._find_active(panel.selected_placement_id)
            if p is not None:
                self._drag = {
                    "kind": "mask_mode",
                    "corner": payload,
                    "start": pos,
                    "box0": (p.x, p.y, p.w, p.h),
                    "rot0": (getattr(p, "rotation", 0.0) or 0.0),
                    "placement": p,
                }
            return

        if kind == "handle":
            p = panel._find_active(panel.selected_placement_id)
            if p is not None:
                group_box0 = {}
                for pid in panel.selected_ids:
                    if pid == p.id:
                        continue
                    gp = panel._find_active(pid)
                    if gp is not None:
                        group_box0[pid] = (gp.x, gp.y, gp.w, gp.h, getattr(gp, "rotation", 0.0) or 0.0)
                self._drag = {"kind": "resize", "corner": payload, "start": pos,
                              "box0": (p.x, p.y, p.w, p.h), "rot0": (getattr(p, 'rotation', 0.0) or 0.0),
                              "placement": p, "group_box0": group_box0}
            return
        if kind == "move":
            p = payload
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if shift:
                panel._select(p.id, toggle=True)
                if p.id not in panel.selected_ids:
                    return  # this click just removed it from the selection
            elif p.id in panel.selected_ids and len(panel.selected_ids) > 1:
                panel._select(p.id, keep_selection=True)
            else:
                panel._select(p.id)
            group_box0 = {}
            for pid in panel.selected_ids:
                if pid == p.id:
                    continue
                gp = panel._find_active(pid)
                if gp is not None:
                    group_box0[pid] = panel.effective_xy(gp)
            self._drag = {"kind": "move", "start": pos, "box0": panel.effective_xy(p), "placement": p,
                          "group_box0": group_box0}
            return
        panel._select(None)
        self.setFocus()

    def mouseMoveEvent(self, event):
        if not self._drag:
            return
        panel = self.panel
        pos = event.position()
        dx_screen = pos.x() - self._drag["start"].x()
        dy_screen = pos.y() - self._drag["start"].y()

        if self._drag["kind"] == "pan":
            panel._pan_x = self._drag["pan0"][0] + dx_screen
            panel._pan_y = self._drag["pan0"][1] + dy_screen
            panel._clamp_pan()
            self.update()
            return

        scale = panel.effective_scale()
        if scale <= 0:
            return
        dx = dx_screen / scale
        dy = dy_screen / scale

        if self._drag["kind"] == "move":
            p = self._drag["placement"]
            x0, y0 = self._drag["box0"]
            new_x = x0 + dx
            new_y = y0 + dy

            if panel.project:
                new_x, new_y, snaps = _snap_placement(new_x, new_y, p.w, p.h, panel.project.width, panel.project.height)
                self._drag["snaps"] = snaps
            else:
                self._drag["snaps"] = []

            is_echo = "::echo" in p.id
            use_kf = (not is_echo) and (panel.mode == "keyframe" or bool(p.keyframes))
            if use_kf:
                upsert_keyframe(p, panel.time - p.start, new_x, new_y)
            else:
                p.x = new_x
                p.y = new_y

            group_box0 = self._drag.get("group_box0") or {}
            if group_box0:
                eff_dx, eff_dy = new_x - x0, new_y - y0
                for pid, (gx0, gy0) in group_box0.items():
                    gp = panel._find_active(pid)
                    if gp is not None:
                        ngx, ngy = gx0 + eff_dx, gy0 + eff_dy
                        g_is_echo = "::echo" in gp.id
                        if (not g_is_echo) and (panel.mode == "keyframe" or bool(gp.keyframes)):
                            upsert_keyframe(gp, panel.time - gp.start, ngx, ngy)
                        else:
                            gp.x = ngx
                            gp.y = ngy

            if is_echo and panel.project:
                main_id, clone_idx = p.id.split("::echo")
                scene = panel.project.scenes.get(panel.scene_id)
                if scene:
                    main_p = next((mp for mp in scene.placements if mp.id == main_id), None)
                    if main_p:
                        ov = main_p.timing_overrides.get(clone_idx) or {}
                        ov["x"] = p.x
                        ov["y"] = p.y
                        main_p.timing_overrides[clone_idx] = ov

            panel.refresh()
            return

        if self._drag["kind"] == "crop_mode":
            p = self._drag["placement"]
            corner = self._drag["corner"]
            rot0 = self._drag.get("rot0", 0.0) or 0.0
            full_x, full_y, full_w, full_h = self._drag["full_box"]
            x0, y0, w0, h0 = self._drag["box0"]
            cl0, ct0, cr0, cb0 = self._drag["crop0"]

            ldx, ldy = (dx, dy) if not rot0 else self._rotate_point(dx, dy, 0.0, 0.0, rot0)

            new_x, new_y = x0, y0
            new_w, new_h = w0, h0

            if "w" in corner:
                new_x = min(x0 + ldx, x0 + w0 - MIN_PLACEMENT_SIZE)
                new_w = w0 - (new_x - x0)
            elif "e" in corner:
                new_w = max(MIN_PLACEMENT_SIZE, w0 + ldx)
            if "n" in corner:
                new_y = min(y0 + ldy, y0 + h0 - MIN_PLACEMENT_SIZE)
                new_h = h0 - (new_y - y0)
            elif "s" in corner:
                new_h = max(MIN_PLACEMENT_SIZE, h0 + ldy)

            full_right  = full_x + full_w
            full_bottom = full_y + full_h
            if new_x < full_x:
                new_w -= full_x - new_x
                new_x = full_x
            if new_y < full_y:
                new_h -= full_y - new_y
                new_y = full_y
            if new_x + new_w > full_right:
                new_w = full_right - new_x
            if new_y + new_h > full_bottom:
                new_h = full_bottom - new_y
            new_w = max(MIN_PLACEMENT_SIZE, new_w)
            new_h = max(MIN_PLACEMENT_SIZE, new_h)

            p.crop_l = max(0.0, (new_x - full_x) / full_w)
            p.crop_t = max(0.0, (new_y - full_y) / full_h)
            p.crop_r = max(0.0, (full_right  - (new_x + new_w)) / full_w)
            p.crop_b = max(0.0, (full_bottom - (new_y + new_h)) / full_h)
            p.x, p.y, p.w, p.h = new_x, new_y, new_w, new_h
            panel.refresh()
            return

        if self._drag["kind"] == "mask_mode":
            p = self._drag["placement"]
            x0, y0, w0, h0 = self._drag["box0"]
            corner = self._drag["corner"]
            rot0 = self._drag.get("rot0", 0.0) or 0.0
            ldx, ldy = (dx, dy) if not rot0 else self._rotate_point(dx, dy, 0.0, 0.0, rot0)
            mtype = getattr(p, "mask_type", "none") or "none"
            if mtype == "rounded_rect" and corner in ("nw", "ne", "sw", "se"):
                cur_r = getattr(p, "mask_radius", 0.2) or 0.2
                delta = (-ldx - ldy) / (max(1.0, min(w0, h0)) * 2.0)
                p.mask_radius = max(0.0, min(1.0, cur_r + delta))
                panel.refresh()
            return

        if self._drag["kind"] == "resize":
            p = self._drag["placement"]
            x0, y0, w0, h0 = self._drag["box0"]
            corner = self._drag["corner"]
            rot0 = self._drag.get("rot0", 0.0) or 0.0

            if corner == "rot":
                rect = panel._scene_rect_to_screen(p.x, p.y, p.w, p.h)
                if rect is not None:
                    scx = rect.center().x()
                    scy = rect.center().y()
                    ang = math.degrees(math.atan2(pos.y() - scy, pos.x() - scx))
                    rot = (ang + 90) % 360
                    if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                        rot = round(rot / 45.0) * 45.0
                    rot_delta = rot - rot0
                    use_kf = panel.mode == "keyframe" or bool(p.keyframes)
                    if use_kf:
                        kx, ky, kw, kh, _ = keyframe_transform(p, panel.time - p.start) if p.keyframes else (p.x, p.y, p.w, p.h, 0.0)
                        upsert_keyframe(p, panel.time - p.start, kx, ky, w=kw, h=kh, rotation=rot)
                    else:
                        p.rotation = rot
                    for pid, (_gx0, _gy0, _gw0, _gh0, grot0) in (self._drag.get("group_box0") or {}).items():
                        gp = panel._find_active(pid)
                        if gp is not None:
                            new_grot = (grot0 + rot_delta) % 360
                            if use_kf and (panel.mode == "keyframe" or bool(gp.keyframes)):
                                gkx, gky, gkw, gkh, _ = keyframe_transform(gp, panel.time - gp.start) if gp.keyframes else (gp.x, gp.y, gp.w, gp.h, 0.0)
                                upsert_keyframe(gp, panel.time - gp.start, gkx, gky, w=gkw, h=gkh, rotation=new_grot)
                            else:
                                gp.rotation = new_grot
                panel.refresh()
                return

            rot0 = self._drag.get("rot0", 0.0) or 0.0
            if rot0:
                dx, dy = self._rotate_point(dx, dy, 0.0, 0.0, rot0)

            new_w, new_h = w0, h0

            if "w" in corner:
                new_w = w0 - dx
            elif "e" in corner:
                new_w = w0 + dx

            if "n" in corner:
                new_h = h0 - dy
            elif "s" in corner:
                new_h = h0 + dy

            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and w0 > 0 and h0 > 0:
                aspect = w0 / h0
                if corner in ("nw", "ne", "sw", "se"):
                    if abs(new_w - w0) > abs(new_h - h0):
                        new_h = new_w / aspect
                    else:
                        new_w = new_h * aspect
                elif corner in ("n", "s"):
                    new_w = new_h * aspect
                elif corner in ("e", "w"):
                    new_h = new_w / aspect

            new_x, new_y = self._resize_anchor_origin(x0, y0, w0, h0, rot0, new_w, new_h, corner)

            is_echo = "::echo" in p.id
            use_kf_resize = (not is_echo) and (panel.mode == "keyframe" or bool(p.keyframes))
            if use_kf_resize:
                kf_x = new_x if new_w >= MIN_PLACEMENT_SIZE else p.x
                kf_y = new_y if new_h >= MIN_PLACEMENT_SIZE else p.y
                kf_w = new_w if new_w >= MIN_PLACEMENT_SIZE else p.w
                kf_h = new_h if new_h >= MIN_PLACEMENT_SIZE else p.h
                _, _, _, _, cur_rot = keyframe_transform(p, panel.time - p.start) if p.keyframes else (0, 0, 0, 0, getattr(p, "rotation", 0.0))
                upsert_keyframe(p, panel.time - p.start, kf_x, kf_y, w=kf_w, h=kf_h, rotation=cur_rot)
            else:
                if new_w >= MIN_PLACEMENT_SIZE:
                    p.x, p.w = new_x, new_w
                if new_h >= MIN_PLACEMENT_SIZE:
                    p.y, p.h = new_y, new_h

            group_box0 = self._drag.get("group_box0") or {}
            if group_box0:
                delta_x, delta_y = new_x - x0, new_y - y0
                delta_w, delta_h = new_w - w0, new_h - h0
                for pid, (gx0, gy0, gw0, gh0, _grot0) in group_box0.items():
                    gp = panel._find_active(pid)
                    if gp is None:
                        continue
                    gw = gw0 + delta_w
                    gh = gh0 + delta_h
                    if gw >= MIN_PLACEMENT_SIZE:
                        gp.x, gp.w = gx0 + delta_x, gw
                    if gh >= MIN_PLACEMENT_SIZE:
                        gp.y, gp.h = gy0 + delta_y, gh

            if "::echo" in p.id and panel.project:
                main_id, clone_idx = p.id.split("::echo")
                scene = panel.project.scenes.get(panel.scene_id)
                if scene:
                    main_p = next((mp for mp in scene.placements if mp.id == main_id), None)
                    if main_p:
                        ov = main_p.timing_overrides.get(clone_idx) or {}
                        ov["x"] = p.x
                        ov["y"] = p.y
                        ov["w"] = p.w
                        ov["h"] = p.h
                        main_p.timing_overrides[clone_idx] = ov

            panel.refresh()
            return

    def mouseReleaseEvent(self, event):
        if self._drag and self._drag["kind"] in ("move", "resize", "crop_mode", "mask_mode"):
            self.panel.placement_edited.emit()
        self._drag = None
        self.update()  # clear snap guides

    def keyPressEvent(self, event):
        panel = self.panel
        key = event.key()
        if key == Qt.Key.Key_C and not event.modifiers():
            panel.set_mode("crop")
            return
        if key == Qt.Key.Key_M and not event.modifiers():
            panel.set_mode("mask")
            return
        if panel.mode not in ("normal", "timing", "crop", "mask", "keyframe") or not panel.selected_placement_id:
            super().keyPressEvent(event)
            return
        p = panel._find_active(panel.selected_placement_id)
        if p is None:
            super().keyPressEvent(event)
            return
        step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        ddx, ddy = 0, 0
        if key == Qt.Key.Key_Left:
            ddx = -step
        elif key == Qt.Key.Key_Right:
            ddx = step
        elif key == Qt.Key.Key_Up:
            ddy = -step
        elif key == Qt.Key.Key_Down:
            ddy = step
        else:
            super().keyPressEvent(event)
            return
        for pid in panel.selected_ids:
            gp = panel._find_active(pid)
            if gp is not None:
                gx, gy = panel.effective_xy(gp)
                ngx, ngy = gx + ddx, gy + ddy
                if "::echo" not in gp.id and (panel.mode == "keyframe" or bool(gp.keyframes)):
                    upsert_keyframe(gp, panel.time - gp.start, ngx, ngy)
                else:
                    gp.x = ngx
                    gp.y = ngy
        panel.placement_edited.emit()
        panel.refresh()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        panel = self.panel
        if panel.mode not in ("normal", "timing"):
            return
        kind, payload = self._hit_test(event.position())
        if kind != "move":
            return
        p = payload
        if getattr(p, 'shape_type', '') != 'text':
            return
        rect = panel._scene_rect_to_screen(p.x, p.y, p.w, p.h)
        if rect is None:
            return
        panel._select(p.id)
        editor = QLineEdit(p.shape_text or "", self)
        editor.setGeometry(int(rect.x()), int(rect.y()), int(rect.width()), max(28, int(rect.height())))
        editor.setStyleSheet("background:#1e1e1e; color:#fff; border:1px solid #ffd24a; font-size:14px;")
        editor.show()
        editor.setFocus()
        editor.selectAll()

        def commit():
            p.shape_text = editor.text()
            editor.deleteLater()
            panel.placement_edited.emit()
            panel.refresh()

        def cancel():
            editor.deleteLater()
            panel.refresh()

        editor.returnPressed.connect(commit)
        editor.editingFinished.connect(lambda: commit() if editor.isVisible() else None)
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, editor.setFocus)

    def wheelEvent(self, event):
        panel = self.panel
        delta = event.angleDelta().y()
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if delta > 0 else (1.0 / 1.15)
            cur = panel.effective_scale()
            new_scale = max(ZOOM_PRESETS[0], min(ZOOM_PRESETS[-1], cur * factor))
            pos = event.position()
            if panel.current_image and not panel.current_image.isNull():
                old_s = panel.effective_scale()
                panel._zoom_mode = new_scale
                new_s = panel.effective_scale()
                if old_s > 0:
                    ratio = new_s / old_s
                    cx = self.width() / 2 + panel._pan_x
                    cy = self.height() / 2 + panel._pan_y
                    panel._pan_x = pos.x() - (pos.x() - panel._pan_x) * ratio
                    panel._pan_y = pos.y() - (pos.y() - panel._pan_y) * ratio
            else:
                panel._zoom_mode = new_scale
            panel._clamp_pan()
            panel._update_zoom_combo()
            self.update()
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            panel._pan_x += delta / 3.0
            panel._clamp_pan()
            self.update()
        else:
            panel._pan_y += delta / 3.0
            panel._clamp_pan()
            self.update()
        event.accept()

    def _draggable_payload(self, event) -> bool:
        md = event.mimeData()
        return md.hasUrls() or decode_asset_mime(md) is not None

    def dragEnterEvent(self, event: QDragEnterEvent):
        if self._draggable_payload(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent):
        if self._draggable_payload(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        scene_pos = self.panel._screen_to_scene(event.position())
        if scene_pos is None:
            event.ignore()
            return
        top_left = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        md = event.mimeData()
        asset = decode_asset_mime(md)
        if asset is not None:
            kind, obj_id = asset
            self.panel.asset_dropped.emit(kind, obj_id, scene_pos[0], scene_pos[1], top_left)
            event.acceptProposedAction()
            return
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if paths:
                self.panel.file_dropped.emit(paths[0], scene_pos[0], scene_pos[1], top_left)
                event.acceptProposedAction()
                return
        event.ignore()


class PreviewPanel(QWidget):
    play_pause_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    scrub_requested = pyqtSignal(float)
    asset_dropped = pyqtSignal(str, str, float, float, bool)
    file_dropped = pyqtSignal(str, float, float, bool)
    selection_changed = pyqtSignal(object)
    placement_edited = pyqtSignal()
    frame_rendered = pyqtSignal(int)
    quality_changed = pyqtSignal(str)

    QUALITY_PRESETS = (("1", 1.0), ("1/2", 0.5), ("1/4", 0.25), ("1/8", 0.125))

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project: Optional[Project] = None
        self.project_dir: Optional[str] = None
        self.scene_id: str = MAIN_ID
        self.time: float = 0.0
        self.current_image = None
        self.images = ImageCache()
        self.async_render = True
        self._renderer = FrameRenderer(self)
        self._renderer.frame_ready.connect(self._on_frame_ready)
        self._req_seq = 0      # number of the newest render request
        self._applied_seq = 0  # request number of the frame currently displayed
        self._zoom_mode = "fit"  # "fit" or a float
        self.render_scale = 1.0  # Preferences > General > "Program Monitor render scale"
        self.mode = "normal"  # "normal" (select/move/resize) or "pan"
        self.selected_placement_id: Optional[str] = None
        self.selected_ids: set = set()
        self._pan_x = 0.0
        self._pan_y = 0.0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        controls = QWidget()
        controls.setObjectName("panelHeader")
        controls_l = QHBoxLayout(controls)
        controls_l.setContentsMargins(8, 4, 8, 4)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(i18n.tr("preview.modeNormal"), "normal")
        self.mode_combo.addItem(i18n.tr("preview.modePan"), "pan")
        self.mode_combo.addItem(i18n.tr("preview.modeTiming"), "timing")
        self.mode_combo.addItem(i18n.tr("preview.modeCrop"), "crop")
        self.mode_combo.addItem(i18n.tr("preview.modeMask"), "mask")
        self.mode_combo.addItem(i18n.tr("preview.modeKeyframes"), "keyframe")
        self.mode_combo.setCurrentIndex(0)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_selected)
        controls_l.addWidget(self.mode_combo)

        controls_l.addStretch(1)

        self.quality_combo = QComboBox()
        for label, scale in self.QUALITY_PRESETS:
            self.quality_combo.addItem(label, label)
        self.quality_combo.setCurrentIndex(0)
        self.quality_combo.setToolTip(i18n.tr("preview.quality"))
        self.quality_combo.activated.connect(self._on_quality_activated)
        controls_l.addWidget(self.quality_combo)

        zoom_out = QPushButton("-")
        zoom_out.setFixedWidth(28)
        zoom_out.clicked.connect(self._zoom_out)
        controls_l.addWidget(zoom_out)

        self.zoom_combo = QComboBox()
        self.zoom_combo.setEditable(True)
        self.zoom_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.zoom_combo.addItem(i18n.tr("preview.zoomFit"), "fit")
        for z in ZOOM_PRESETS:
            self.zoom_combo.addItem(f"{int(z * 100)}%", z)
        self.zoom_combo.setCurrentIndex(0)
        self.zoom_combo.activated.connect(self._on_zoom_activated)
        self.zoom_combo.lineEdit().returnPressed.connect(self._on_zoom_text_entered)
        controls_l.addWidget(self.zoom_combo)

        zoom_in = QPushButton("+")
        zoom_in.setFixedWidth(28)
        zoom_in.clicked.connect(self._zoom_in)
        controls_l.addWidget(zoom_in)

        fit_btn = QPushButton(i18n.tr("preview.zoomFit"))
        fit_btn.clicked.connect(self._zoom_fit)
        controls_l.addWidget(fit_btn)

        outer.addWidget(controls)

        self.canvas = _CanvasWidget(self)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.canvas.setMinimumHeight(120)
        outer.addWidget(self.canvas, 1)

        transport = QWidget()
        transport_l = QHBoxLayout(transport)
        transport_l.setContentsMargins(8, 4, 8, 6)
        self.play_btn = QPushButton()
        self.play_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_btn.setFixedWidth(30)
        self.play_btn.setToolTip(i18n.tr("preview.playPause"))
        self.play_btn.clicked.connect(self.play_pause_requested.emit)
        transport_l.addWidget(self.play_btn)
        self.stop_btn = QPushButton()
        self.stop_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_btn.setFixedWidth(30)
        self.stop_btn.setToolTip(i18n.tr("preview.stop"))
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        transport_l.addWidget(self.stop_btn)
        self.time_label = QLabel("0.00s")
        self.time_label.setFixedWidth(56)
        transport_l.addWidget(self.time_label)
        self.scrub = QSlider(Qt.Orientation.Horizontal)
        self.scrub.setRange(0, 800)  # 0.01s units, rescaled in set_duration()
        self.scrub.sliderMoved.connect(self._on_scrub_moved)
        transport_l.addWidget(self.scrub, 1)
        outer.addWidget(transport)

        self._scrubbing_internally = False
        self.set_duration(8.0)

    # external state
    def set_project(self, project: Optional[Project], project_dir: Optional[str]):
        self.project = project
        self.project_dir = project_dir
        self.images.clear()
        self.scene_id = MAIN_ID
        self.set_time(0.0, sync=True)

    def set_project_dir(self, project_dir: Optional[str]):
        if project_dir != self.project_dir:
            self.images.clear()
        self.project_dir = project_dir
        self.refresh()

    def set_scene(self, scene_id: str):
        self.scene_id = scene_id
        self._select(None)
        self.set_time(0.0, sync=True)

    def set_playing(self, playing: bool):
        icon = QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        self.play_btn.setIcon(self.style().standardIcon(icon))

    def set_duration(self, duration: float):
        self.scrub.blockSignals(True)
        self.scrub.setRange(0, max(1, int(duration * 100)))
        self.scrub.blockSignals(False)

    def set_time(self, t: float, sync: bool = False):
        self.time = max(0.0, t)
        self._scrubbing_internally = True
        self.scrub.setValue(int(round(self.time * 100)))
        self._scrubbing_internally = False
        self.time_label.setText(f"{self.time:.2f}s")
        if self.selected_placement_id and self._find_active(self.selected_placement_id) is None:
            self._select(None)
        self.refresh(sync=sync)

    def set_render_scale(self, scale: float):
        self.render_scale = max(0.1, min(1.0, scale))
        self.refresh(sync=True)  # image size changes: keep hit-testing in step

    def _on_quality_activated(self, index: int):
        key = self.quality_combo.itemData(index)
        scale = dict(self.QUALITY_PRESETS).get(key, 1.0)
        self.set_render_scale(scale)
        self.quality_changed.emit(key)

    def set_quality(self, key: str):
        idx = self.quality_combo.findData(key)
        if idx < 0:
            idx = 0
        self.quality_combo.blockSignals(True)
        self.quality_combo.setCurrentIndex(idx)
        self.quality_combo.blockSignals(False)
        scale = dict(self.QUALITY_PRESETS).get(self.quality_combo.itemData(idx), 1.0)
        self.set_render_scale(scale)

    def refresh(self, sync: bool = False):
        if not self.project:
            self._req_seq += 1
            self._applied_seq = self._req_seq
            self.current_image = None
            self.canvas.update()
            return
        width, height = self.project.scene_size(self.scene_id)
        rw = max(1, int(width * self.render_scale))
        rh = max(1, int(height * self.render_scale))
        project, scene_id, t = self.project, self.scene_id, self.time
        images, project_dir = self.images, self.project_dir
        self._req_seq += 1
        tag = self._req_seq
        if sync or not self.async_render:
            self.current_image = render_frame(project, scene_id, t, images, project_dir, rw, rh)
            self._applied_seq = tag  # any older in-flight async frame is now stale
            self.canvas.update()
            self.frame_rendered.emit(tag)
        else:
            self._renderer.submit(tag, lambda: render_frame(project, scene_id, t, images, project_dir, rw, rh))

    def _on_frame_ready(self, tag: int, image):
        if tag < self._applied_seq:
            return
        self._applied_seq = tag
        self.current_image = image
        self.canvas.update()
        self.frame_rendered.emit(tag)

    def shutdown(self):
        self._renderer.close(join=True)
        self.images.clear(join=True)

    # transport
    def _on_scrub_moved(self, value: int):
        self.scrub_requested.emit(value / 100.0)

    # zoom
    def _screen_to_scene(self, pos) -> Optional[tuple]:
        image = self.current_image
        if image is None or image.isNull() or not self.project:
            return None
        rs = max(0.001, self.render_scale)
        display_scale = self.effective_scale() / rs
        if display_scale <= 0:
            return None
        w = image.width() * display_scale
        h = image.height() * display_scale
        x0 = (self.canvas.width() - w) / 2.0 + self._pan_x
        y0 = (self.canvas.height() - h) / 2.0 + self._pan_y
        img_x = (pos.x() - x0) / display_scale
        img_y = (pos.y() - y0) / display_scale
        return img_x / rs, img_y / rs

    def _scene_rect_to_screen(self, x: float, y: float, w: float, h: float) -> Optional[QRectF]:
        image = self.current_image
        if image is None or image.isNull():
            return None
        rs = max(0.001, self.render_scale)
        display_scale = self.effective_scale() / rs
        if display_scale <= 0:
            return None
        iw = image.width() * display_scale
        ih = image.height() * display_scale
        x0 = (self.canvas.width() - iw) / 2.0 + self._pan_x
        y0 = (self.canvas.height() - ih) / 2.0 + self._pan_y
        proj_scale = self.effective_scale()
        return QRectF(x0 + x * proj_scale, y0 + y * proj_scale, w * proj_scale, h * proj_scale)

    def _active_placements(self) -> list:
        if not self.project:
            return []
        scene = self.project.scenes.get(self.scene_id)
        if not scene:
            return []
        on = [p for p in scene.placements if p.kind in ("scene", "symbol", "image", "video", "shape") and p.start <= self.time <= p.end]
        if self.mode == "timing":
            from ..compositor import duplication_echoes
            on.extend(duplication_echoes(self.project, scene, self.time))
        on.sort(key=lambda p: (p.layer or 0, p.start))
        return on

    def _find_active(self, placement_id: Optional[str]):
        if not placement_id:
            return None
        return next((p for p in self._active_placements() if p.id == placement_id), None)

    def effective_xy(self, p) -> tuple:
        if getattr(p, "keyframes", None) and "::echo" not in p.id:
            return keyframe_xy(p, self.time - p.start)
        return p.x, p.y

    def _select(self, placement_id: Optional[str], toggle: bool = False, keep_selection: bool = False):
        if toggle and placement_id:
            if placement_id in self.selected_ids:
                self.selected_ids.discard(placement_id)
                if self.selected_placement_id == placement_id:
                    self.selected_placement_id = next(iter(self.selected_ids), None)
            else:
                self.selected_ids.add(placement_id)
                self.selected_placement_id = placement_id
        elif placement_id is None:
            self.selected_ids = set()
            self.selected_placement_id = None
        elif keep_selection and placement_id in self.selected_ids:
            self.selected_placement_id = placement_id
        else:
            self.selected_ids = {placement_id}
            self.selected_placement_id = placement_id
        self.selection_changed.emit(self._find_active(self.selected_placement_id))
        self.canvas.update()

    def set_mode(self, mode: str):
        if mode not in ("normal", "pan", "timing", "crop", "mask", "keyframe"):
            return
        self.mode = mode
        idx = self.mode_combo.findData(mode)
        if idx >= 0:
            self.mode_combo.setCurrentIndex(idx)
        if mode == "pan":
            self.canvas.setCursor(Qt.CursorShape.OpenHandCursor)
        elif mode == "crop":
            self.canvas.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == "mask":
            self.canvas.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.canvas.setCursor(Qt.CursorShape.ArrowCursor)
        if mode not in ("normal", "timing", "crop", "mask", "keyframe"):
            self._select(None)
        else:
            self.canvas.update()

    def _on_mode_selected(self, _index: int):
        self.set_mode(self.mode_combo.currentData())

    def effective_scale(self) -> float:
        if self._zoom_mode == "fit":
            return self._fit_scale()
        return float(self._zoom_mode)

    def _fit_scale(self) -> float:
        if not self.current_image or self.current_image.isNull():
            return 1.0
        rs = max(0.001, self.render_scale)
        iw = self.current_image.width() / rs
        ih = self.current_image.height() / rs
        if iw <= 0 or ih <= 0:
            return 1.0
        cw, ch = max(1, self.canvas.width()), max(1, self.canvas.height())
        return max(0.02, min(cw / iw, ch / ih))

    def reset_pan(self):
        self._pan_x = 0.0
        self._pan_y = 0.0

    _PAN_MARGIN_PX = 80.0

    def _clamp_pan(self):
        image = self.current_image
        cw, ch = self.canvas.width(), self.canvas.height()
        if image is None or image.isNull() or cw <= 0 or ch <= 0:
            return
        rs = max(0.001, self.render_scale)
        scale = self.effective_scale() / rs
        w = image.width() * scale
        h = image.height() * scale
        margin = self._PAN_MARGIN_PX
        max_pan_x = max(0.0, (w + cw) / 2.0 - margin)
        max_pan_y = max(0.0, (h + ch) / 2.0 - margin)
        self._pan_x = max(-max_pan_x, min(max_pan_x, self._pan_x))
        self._pan_y = max(-max_pan_y, min(max_pan_y, self._pan_y))

    def _zoom_fit(self):
        self._zoom_mode = "fit"
        self.reset_pan()
        self._update_zoom_combo()
        self.canvas.update()

    def _set_zoom(self, value: float):
        self._zoom_mode = value
        self._update_zoom_combo()
        self.canvas.update()

    def _zoom_in(self):
        cur = self.effective_scale()
        nxt = next((z for z in ZOOM_PRESETS if z > cur + 1e-6), ZOOM_PRESETS[-1])
        self._set_zoom(nxt)

    def _zoom_out(self):
        cur = self.effective_scale()
        nxt = next((z for z in reversed(ZOOM_PRESETS) if z < cur - 1e-6), ZOOM_PRESETS[0])
        self._set_zoom(nxt)

    def _on_zoom_activated(self, index: int):
        if index < 0:
            return
        data = self.zoom_combo.itemData(index)
        if data == "fit":
            self._zoom_mode = "fit"
            self.reset_pan()
        elif data is not None:
            self._zoom_mode = float(data)
        self._update_zoom_combo()
        self.canvas.update()

    def _on_zoom_text_entered(self):
        text = self.zoom_combo.lineEdit().text().strip()
        if not text:
            self._update_zoom_combo()
            return
        fit_label = i18n.tr("preview.zoomFit")
        if text.lower() in ("fit", fit_label.lower()):
            self._zoom_mode = "fit"
            self.reset_pan()
        else:
            try:
                pct = float(text.rstrip("%").strip())
                self._zoom_mode = max(0.01, min(8.0, pct / 100.0))
            except ValueError:
                pass
        self._update_zoom_combo()
        self.canvas.update()
        self.zoom_combo.lineEdit().clearFocus()

    def _update_zoom_combo(self):
        self.zoom_combo.blockSignals(True)
        try:
            if self._zoom_mode == "fit":
                idx = self.zoom_combo.findData("fit")
                if idx >= 0:
                    self.zoom_combo.setCurrentIndex(idx)
            else:
                zoom_f = float(self._zoom_mode)
                match = -1
                for i in range(self.zoom_combo.count()):
                    d = self.zoom_combo.itemData(i)
                    if isinstance(d, float) and abs(d - zoom_f) < 1e-9:
                        match = i
                        break
                if match >= 0:
                    self.zoom_combo.setCurrentIndex(match)
                else:
                    pct_text = f"{round(zoom_f * 100)}%"
                    self.zoom_combo.setCurrentIndex(-1)
                    self.zoom_combo.lineEdit().setText(pct_text)
        finally:
            self.zoom_combo.blockSignals(False)

    def sizeHint(self) -> QSize:
        return QSize(640, 400)