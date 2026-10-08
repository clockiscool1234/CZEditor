from __future__ import annotations

import math
import os
import threading
from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPolygonF, QTransform
try:
    from PyQt6.QtSvg import QSvgRenderer as _QSvgRenderer
except ImportError:
    _QSvgRenderer = None

from .asset_types import is_video
from .imaging import load_animated_frames, load_image, prepare_image
from .model import Placement, Project, keyframe_xy, keyframe_transform, ease_value, DEFAULT_EASE
from .video import VideoCache

MAX_RECURSION = 8

ANIM_DEFAULTS = {
    "none": (0.0, 0.0),
    "fade": (0.2, 0.6),
    "pop": (0.2, 0.2),
    "aero": (8.0 / 30.0, 8.0 / 30.0),
    "slide_up": (0.4, 0.4),
    "slide_down": (0.4, 0.4),
    "slide_left": (0.4, 0.4),
    "slide_right": (0.4, 0.4),
    "slide_out_up": (0.4, 0.4),
    "slide_out_down": (0.4, 0.4),
    "slide_out_left": (0.4, 0.4),
    "slide_out_right": (0.4, 0.4),
}
ANIM_SCALE_DEFAULT = 95.0
ANIM_TILT_DEFAULT = 25.0
THREE_D_CAMERA_MULT = 3.0    # camera distance, as a multiple of max(w, h)
THREE_D_MIN_DEPTH_FRAC = 0.05  # camera distance fraction below which a point is treated as behind the camera


class AnimatedImage:
    def __init__(self, frames_durations: list):
        self.frames: list = [f for f, _d in frames_durations]
        self.durations: list = [d for _f, d in frames_durations]
        cum = []
        acc = 0.0
        for d in self.durations:
            acc += d
            cum.append(acc)
        self._cum = cum
        self.total_duration = acc

    def frame_at(self, local_t: float, loop: bool) -> Optional[QImage]:
        if not self.frames:
            return None
        if len(self.frames) == 1 or self.total_duration <= 0:
            return self.frames[0]
        t = max(0.0, local_t)
        if loop:
            t = t % self.total_duration
        elif t >= self.total_duration:
            return self.frames[-1]
        for i, c in enumerate(self._cum):
            if t < c:
                return self.frames[i]
        return self.frames[-1]


class ImageCache:
    USE_MIPS = False

    def __init__(self, exact_video: bool = False):
        self._lock = threading.RLock()
        self._gen = 0  # bumped by clear(); loads that started before it don't repopulate
        self._cache: dict[tuple[str, str], Optional[QImage]] = {}
        self._mips: dict[tuple[str, str], list] = {}
        self._anim_cache: dict[tuple[str, str], Optional[AnimatedImage]] = {}
        self._video_cache = VideoCache(exact=exact_video)

    def clear(self, join: bool = False):
        with self._lock:
            self._gen += 1
            self._cache.clear()
            self._mips.clear()
            self._anim_cache.clear()
        self._video_cache.clear(join=join)

    def invalidate(self, project_dir: Optional[str], uuid_ext: str) -> None:
        key = (project_dir, uuid_ext)
        with self._lock:
            self._cache.pop(key, None)
            self._mips.pop(key, None)
            self._anim_cache.pop(key, None)
        self._video_cache.drop(project_dir, uuid_ext)

    def get(self, project_dir: Optional[str], uuid_ext: str) -> Optional[QImage]:
        if not project_dir or not uuid_ext:
            return None
        key = (project_dir, uuid_ext)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            gen = self._gen
        img = load_image(os.path.join(project_dir, uuid_ext))  # decode outside the lock
        with self._lock:
            if gen != self._gen:
                return img
            return self._cache.setdefault(key, img)

    def get_scaled(self, project_dir: Optional[str], uuid_ext: str,
                   size_hint: Optional[tuple]) -> Optional[QImage]:
        img = self.get(project_dir, uuid_ext)
        if img is None or size_hint is None or not self.USE_MIPS:
            return img
        tw, th = size_hint
        if tw <= 0 or th <= 0:
            return img
        w, h = img.width(), img.height()
        k = 0
        while (w >> (k + 1)) >= tw and (h >> (k + 1)) >= th and k < 12:
            k += 1
        if k == 0:
            return img
        key = (project_dir, uuid_ext)
        with self._lock:
            mips = self._mips.setdefault(key, [img])
            while len(mips) <= k:
                prev = mips[-1]
                nxt = prev.scaled(
                    max(1, prev.width() // 2), max(1, prev.height() // 2),
                    Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
                )
                mips.append(prepare_image(nxt))
            return mips[k]

    def _get_animated(self, project_dir: Optional[str], uuid_ext: str) -> Optional[AnimatedImage]:
        if not project_dir or not uuid_ext:
            return None
        key = (project_dir, uuid_ext)
        with self._lock:
            if key in self._anim_cache:
                return self._anim_cache[key]
            gen = self._gen
        frames = load_animated_frames(os.path.join(project_dir, uuid_ext), as_image=True)
        anim = AnimatedImage(frames) if frames else None
        with self._lock:
            if gen != self._gen:
                return anim
            return self._anim_cache.setdefault(key, anim)

    def is_animated(self, project_dir: Optional[str], uuid_ext: str) -> bool:
        return self._get_animated(project_dir, uuid_ext) is not None

    def get_frame(self, project_dir: Optional[str], uuid_ext: str,
                  local_t: float = 0.0, loop: bool = True,
                  size_hint: Optional[tuple] = None) -> Optional[QImage]:
        if is_video(uuid_ext):
            return self._video_cache.get_frame(project_dir, uuid_ext, local_t, loop, size_hint)
        anim = self._get_animated(project_dir, uuid_ext)
        if anim is not None:
            return anim.frame_at(local_t, loop)
        return self.get_scaled(project_dir, uuid_ext, size_hint)


def _hint(w: float, h: float) -> tuple:
    return (max(1, int(round(w))), max(1, int(round(h))))


def symbol_states_from_placements(project: Project, placements: list, t: float) -> dict:
    states = {}

    # symbols
    sym_on = [p for p in placements if p.kind == "symbol" and p.start <= t <= p.end]
    sym_on.sort(key=lambda p: p.start)
    latest_by_class: dict[str, str] = {}
    for p in sym_on:
        edef = project.symbol_defs.get(p.ref)
        if edef:
            latest_by_class[edef.elem_class] = p.id
    for p in sym_on:
        edef = project.symbol_defs.get(p.ref)
        cls = edef.elem_class if edef else None
        states[p.id] = "active" if latest_by_class.get(cls) == p.id else "inactive"

    # scenes with has_inactive + elem_class
    scene_on = [p for p in placements if p.kind == "scene" and p.start <= t <= p.end]
    scene_on.sort(key=lambda p: p.start)
    latest_scene_by_class: dict[str, str] = {}
    for p in scene_on:
        sdef = project.scenes.get(p.ref)
        if sdef and sdef.has_inactive and sdef.elem_class:
            latest_scene_by_class[sdef.elem_class] = p.id
    for p in scene_on:
        sdef = project.scenes.get(p.ref)
        if sdef and sdef.has_inactive and sdef.elem_class:
            states[p.id] = "active" if latest_scene_by_class.get(sdef.elem_class) == p.id else "inactive"
        else:
            states[p.id] = "active"

    return states


# timing


def _imul32(a: int, b: int) -> int:
    return (a * b) & 0xFFFFFFFF


def _mulberry32(seed: int):
    state = seed & 0xFFFFFFFF

    def rng() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        s = state
        t1 = _imul32(s ^ (s >> 15), (1 | s) & 0xFFFFFFFF)
        t2 = ((t1 + _imul32(t1 ^ (t1 >> 7), (61 | t1) & 0xFFFFFFFF)) & 0xFFFFFFFF) ^ t1
        t2 &= 0xFFFFFFFF
        result = (t2 ^ (t2 >> 14)) & 0xFFFFFFFF
        return result / 4294967296.0

    return rng


def _random_pair_for_clone(seed: int, clone_index: int) -> tuple:
    seed = int(seed) & 0xFFFFFFFF
    clone_index = int(clone_index) & 0xFFFFFFFF
    mixed = (seed * 2654435761 + clone_index * 40503) & 0xFFFFFFFF
    rng = _mulberry32(mixed)
    return rng(), rng()


_DUPLICATION_KINDS = {"symbol", "scene", "image", "video", "shape"}


def duplication_echoes(project: Project, scene, t: float) -> list:
    hidden_tracks = set(getattr(scene, "hidden_tracks", []))
    echoes = []
    for p in scene.placements:
        if p.track in hidden_tracks:
            continue
        if (p.timing_type or "none") != "duplication":
            continue
        if p.kind not in _DUPLICATION_KINDS:
            continue
        if not (p.start <= t <= p.end):
            continue
        local_t = t - p.start
        pts = sorted(pt for pt in (p.timing_points or []) if pt <= local_t)
        base_x, base_y = p.x, p.y
        cx, cy, cw, ch = p.x, p.y, p.w, p.h
        cdx = p.timing_offset_x or 0.0
        cdy = p.timing_offset_y or 0.0
        canim_scale, canim_tilt = p.anim_scale, p.anim_tilt_deg
        use_random = bool(p.timing_random_pos)
        rx_min = min(p.timing_random_x_min or 0.0, p.timing_random_x_max or 0.0)
        rx_max = max(p.timing_random_x_min or 0.0, p.timing_random_x_max or 0.0)
        ry_min = min(p.timing_random_y_min or 0.0, p.timing_random_y_max or 0.0)
        ry_max = max(p.timing_random_y_min or 0.0, p.timing_random_y_max or 0.0)
        seed = p.timing_random_seed or 0

        for i, pt in enumerate(pts):
            n = i + 1
            if use_random:
                r0, r1 = _random_pair_for_clone(seed, n)
                # Each clone's randomized position is absolute (relative to the
                # original placement), not accumulated relative to the previous
                # clone's position, so random offsets don't drift/compound.
                def_x = base_x + rx_min + r0 * (rx_max - rx_min)
                def_y = base_y + ry_min + r1 * (ry_max - ry_min)
            else:
                def_x = cx + cdx
                def_y = cy + cdy
            def_w, def_h = cw, ch
            def_scale, def_tilt = canim_scale, canim_tilt

            ov = p.timing_overrides.get(str(n)) or {}

            ex = ov.get("x", def_x)
            ey = ov.get("y", def_y)
            ew = ov.get("w", def_w)
            eh = ov.get("h", def_h)
            eanim_scale = ov.get("anim_scale", def_scale)
            eanim_tilt = ov.get("anim_tilt_deg", def_tilt)

            if ov.get("affects_subsequent", True):
                cx, cy, cw, ch = ex, ey, ew, eh
                canim_scale, canim_tilt = eanim_scale, eanim_tilt
                if "offset_x" in ov: cdx = ov["offset_x"]
                if "offset_y" in ov: cdy = ov["offset_y"]
                if "rx_min" in ov: rx_min = min(ov["rx_min"], ov.get("rx_max", rx_max))
                if "rx_max" in ov: rx_max = max(ov.get("rx_min", rx_min), ov["rx_max"])
                if "ry_min" in ov: ry_min = min(ov["ry_min"], ov.get("ry_max", ry_max))
                if "ry_max" in ov: ry_max = max(ov.get("ry_min", ry_min), ov["ry_max"])
                if "random_pos" in ov: use_random = ov["random_pos"]
            else:
                if not use_random:
                    cx, cy = def_x, def_y
                cw, ch = def_w, def_h
                canim_scale, canim_tilt = def_scale, def_tilt

            echoes.append(Placement(
                id=f"{p.id}::echo{n}", kind=p.kind, ref=p.ref, track=p.track,
                layer=p.layer, start=p.start + pt, end=p.end,
                x=ex, y=ey, w=ew, h=eh,
                anim_in=p.anim_in, anim_out=p.anim_out,
                anim_in_dur=p.anim_in_dur, anim_out_dur=p.anim_out_dur,
                anim_in_fade=p.anim_in_fade, anim_out_fade=p.anim_out_fade,
                anim_scale=eanim_scale, anim_tilt_deg=eanim_tilt,
                anim_in_ease=getattr(p, "anim_in_ease", DEFAULT_EASE),
                anim_out_ease=getattr(p, "anim_out_ease", DEFAULT_EASE),
                src_offset=p.src_offset,
                loop=p.loop,
                speed=getattr(p, "speed", 1.0),
                opacity=getattr(p, "opacity", 1.0),
                rotation=getattr(p, "rotation", 0.0),
                shape_type=getattr(p, "shape_type", "rect"),
                shape_color=getattr(p, "shape_color", "#4488ff"),
                shape_border_color=getattr(p, "shape_border_color", ""),
                shape_border_width=getattr(p, "shape_border_width", 0.0),
                shape_text=getattr(p, "shape_text", ""),
                shape_font_family=getattr(p, "shape_font_family", ""),
                shape_font_size=getattr(p, "shape_font_size", 24.0),
            ))
    return echoes


def switch_media_override(p, t: float) -> Optional[str]:
    if (p.timing_type or "none") != "switch_media":
        return None
    media_list = p.media_list or []
    pts = p.timing_points or []
    if not media_list or not pts:
        return None
    local_t = t - p.start
    crossed = sum(1 for pt in pts if pt <= local_t)
    if crossed == 0:
        return None
    direction = -1 if p.media_direction == "backward" else 1
    raw = direction * (crossed - 1)
    idx = ((raw % len(media_list)) + len(media_list)) % len(media_list)
    return media_list[idx] or None


def _flash_event_mode(p) -> bool:
    return (p.timing_type or "none") == "flashing" and bool(
        getattr(p, "timing_double_flash", False) or getattr(p, "timing_flip", False))


def timing_flash_alpha(p, t: float) -> Optional[float]:
    if (p.timing_type or "none") != "flashing":
        return None
    pts = p.timing_points or []
    if not pts:
        return None
    local_t = t - p.start
    if _flash_event_mode(p):
        if not getattr(p, "timing_double_flash", False):
            return None
        d = max(0.001, float(getattr(p, "timing_double_flash_delay", 0.1) or 0.1))
        for pt in pts:
            dt = local_t - pt
            # hidden for d, visible for d, hidden for d, then visible again
            if 0.0 <= dt < d or 2.0 * d <= dt < 3.0 * d:
                return 0.0
        return None
    crossed = sum(1 for pt in pts if pt <= local_t)
    if crossed == 0:
        return None
    return 0.0 if crossed % 2 == 1 else 1.0


def timing_flip_state(p, t: float) -> tuple:
    if (p.timing_type or "none") != "flashing" or not getattr(p, "timing_flip", False):
        return False, False
    local_t = t - p.start
    crossed = sum(1 for pt in (p.timing_points or []) if pt <= local_t)
    if crossed % 2 == 0:
        return False, False
    if getattr(p, "timing_flip_axis", "horizontal") == "vertical":
        return False, True
    return True, False


# animation
def _scale_from(p) -> float:
    pct = p.anim_scale if p.anim_scale > 0 else ANIM_SCALE_DEFAULT
    return max(0.0, min(1.0, pct / 100.0))


def _tilt_amount(p) -> float:
    return p.anim_tilt_deg if p.anim_tilt_deg != 0 else ANIM_TILT_DEFAULT


def _ease_aero(frac: float) -> float:
    inv = 1.0 - frac
    return 1.0 - inv * inv * inv


def _offscreen_distance(direction: str, box, bounds, fallback: float) -> float:
    if not bounds or not box:
        return fallback
    sw, sh = bounds
    x, y, w, h = box
    dist = {"up": y + h, "down": sh - y, "left": x + w, "right": sw - x}[direction]
    return max(0.0, dist) + 1.0


def anim_progress(p, t: float, bounds=None, box=None) -> tuple:
    base_alpha = getattr(p, "opacity", 1.0)
    alpha, scale, tilt, off_x, off_y = base_alpha, 1.0, 0.0, 0.0, 0.0
    eff_start, eff_end = p.start, p.end
    if (p.timing_type or "none") == "flashing" and p.timing_points and not _flash_event_mode(p):
        pts = p.timing_points
        local_t = t - p.start
        crossed = sum(1 for pt in pts if pt <= local_t)
        if crossed > 0 and crossed % 2 == 0:
            eff_start = p.start + pts[crossed - 1]
        if crossed < len(pts):
            if crossed % 2 == 0:
                eff_end = p.start + pts[crossed]
        else:
            eff_end = p.end
    dur = max(0.0001, eff_end - eff_start)
    ph = p.h if p.h else 500
    pw = p.w if p.w else 500
    fade_in = bool(getattr(p, "anim_in_fade", False))
    fade_out = bool(getattr(p, "anim_out_fade", False))

    anim_in = p.anim_in or "none"
    if anim_in != "none":
        def_in, _def_out = ANIM_DEFAULTS.get(anim_in, (0.0, 0.0))
        in_dur = p.anim_in_dur if p.anim_in_dur > 0 else def_in
        in_dur = max(0.0, min(in_dur, dur))
        if in_dur > 0 and t < eff_start + in_dur:
            frac = max(0.0, min(1.0, (t - eff_start) / in_dur))
            ease = getattr(p, "anim_in_ease", DEFAULT_EASE)
            if not isinstance(ease, (list, tuple)) or len(ease) != 4:
                ease = DEFAULT_EASE
            efrac = ease_value(ease, frac)
            if anim_in == "fade":
                alpha = min(alpha, efrac * alpha)
            elif anim_in == "aero":
                ef = _ease_aero(efrac)
                sf = _scale_from(p)
                alpha = min(alpha, ef * alpha)
                scale = min(scale, sf + (1.0 - sf) * ef)
                tilt = -_tilt_amount(p) * (1.0 - ef)
            else:
                if fade_in and anim_in != "none":
                    alpha = min(alpha, efrac * alpha)
                if anim_in == "pop":
                    sf = _scale_from(p)
                    scale = min(scale, sf + (1.0 - sf) * efrac)
                elif anim_in == "slide_up":
                    off_y = (1.0 - efrac) * ph
                elif anim_in == "slide_down":
                    off_y = -(1.0 - efrac) * ph
                elif anim_in == "slide_left":
                    off_x = (1.0 - efrac) * pw
                elif anim_in == "slide_right":
                    off_x = -(1.0 - efrac) * pw
                # slide_out_* variants: enter from the opposite side (scene boundary)
                elif anim_in == "slide_out_up":
                    off_y = -(1.0 - efrac) * ph * 2  # from above
                elif anim_in == "slide_out_down":
                    off_y = (1.0 - efrac) * ph * 2   # from below
                elif anim_in == "slide_out_left":
                    off_x = -(1.0 - efrac) * pw * 2  # from right
                elif anim_in == "slide_out_right":
                    off_x = (1.0 - efrac) * pw * 2   # from left

    anim_out = p.anim_out or "none"
    if anim_out != "none":
        _def_in, def_out = ANIM_DEFAULTS.get(anim_out, (0.0, 0.0))
        out_dur = p.anim_out_dur if p.anim_out_dur > 0 else def_out
        out_dur = max(0.0, min(out_dur, dur))
        if out_dur > 0 and t > eff_end - out_dur:
            frac = max(0.0, min(1.0, (eff_end - t) / out_dur))
            ease = getattr(p, "anim_out_ease", DEFAULT_EASE)
            if not isinstance(ease, (list, tuple)) or len(ease) != 4:
                ease = DEFAULT_EASE
            efrac = ease_value(ease, frac)
            if anim_out == "fade":
                alpha = min(alpha, efrac * base_alpha)
            elif anim_out == "aero":
                ef = _ease_aero(efrac)
                sf = _scale_from(p)
                alpha = min(alpha, ef * base_alpha)
                scale = min(scale, sf + (1.0 - sf) * ef)
                tilt = _tilt_amount(p) * (1.0 - ef)
            else:
                if fade_out:
                    alpha = min(alpha, efrac * base_alpha)
                inv = 1.0 - efrac
                if anim_out == "pop":
                    sf = _scale_from(p)
                    scale = min(scale, sf + (1.0 - sf) * efrac)
                # slide exits travel until the object (or clone) is fully outside the scene
                elif anim_out == "slide_up":
                    off_y = -inv * _offscreen_distance("up", box, bounds, ph)
                elif anim_out == "slide_down":
                    off_y = inv * _offscreen_distance("down", box, bounds, ph)
                elif anim_out == "slide_left":
                    off_x = -inv * _offscreen_distance("left", box, bounds, pw)
                elif anim_out == "slide_right":
                    off_x = inv * _offscreen_distance("right", box, bounds, pw)
                # slide_out_* variants: exit toward the opposite side
                elif anim_out == "slide_out_up":
                    off_y = inv * _offscreen_distance("down", box, bounds, ph * 2)
                elif anim_out == "slide_out_down":
                    off_y = -inv * _offscreen_distance("up", box, bounds, ph * 2)
                elif anim_out == "slide_out_left":
                    off_x = inv * _offscreen_distance("right", box, bounds, pw * 2)
                elif anim_out == "slide_out_right":
                    off_x = -inv * _offscreen_distance("left", box, bounds, pw * 2)

    return max(0.0, min(1.0, alpha)), max(0.0, scale), tilt, off_x, off_y


def _project_3d_quad(p, bw: float, bh: float) -> Optional[list]:
    pitch = math.radians(getattr(p, "rotate_3d_x", 0.0) or 0.0)
    yaw = math.radians(getattr(p, "rotate_3d_y", 0.0) or 0.0)
    roll = math.radians(getattr(p, "rotate_3d_z", 0.0) or 0.0)
    tx = getattr(p, "translate_3d_x", 0.0) or 0.0
    ty = getattr(p, "translate_3d_y", 0.0) or 0.0
    tz = getattr(p, "translate_3d_z", 0.0) or 0.0

    cx_p, sx_p = math.cos(pitch), math.sin(pitch)
    cy_w, sy_w = math.cos(yaw), math.sin(yaw)
    cz_r, sz_r = math.cos(roll), math.sin(roll)

    cam_dist = max(1.0, max(bw, bh) * THREE_D_CAMERA_MULT)
    min_depth = cam_dist * THREE_D_MIN_DEPTH_FRAC

    corners = []
    for hx, hy in ((-bw / 2.0, -bh / 2.0), (bw / 2.0, -bh / 2.0),
                   (bw / 2.0, bh / 2.0), (-bw / 2.0, bh / 2.0)):
        x, y, z = hx, hy, 0.0
        y, z = y * cx_p - z * sx_p, y * sx_p + z * cx_p
        x, z = x * cy_w + z * sy_w, -x * sy_w + z * cy_w
        x, y = x * cz_r - y * sz_r, x * sz_r + y * cz_r
        x, y, z = x + tx, y + ty, z + tz
        depth = cam_dist - z
        if depth < min_depth:
            return None
        scale = cam_dist / depth
        corners.append((x * scale, y * scale))
    return corners


def _project_tilt_quad(tilt_deg: float, bw: float, bh: float) -> Optional[list]:
    pitch = math.radians(tilt_deg)
    cx_p, sx_p = math.cos(pitch), math.sin(pitch)
    cam_dist = max(1.0, max(bw, bh) * THREE_D_CAMERA_MULT)
    min_depth = cam_dist * THREE_D_MIN_DEPTH_FRAC

    corners = []
    for hx, hy in ((-bw / 2.0, -bh / 2.0), (bw / 2.0, -bh / 2.0),
                   (bw / 2.0, bh / 2.0), (-bw / 2.0, bh / 2.0)):
        x, y, z = hx, hy, 0.0
        y, z = y * cx_p - z * sx_p, y * sx_p + z * cx_p
        depth = cam_dist - z
        if depth < min_depth:
            return None
        scale = cam_dist / depth
        corners.append((x * scale, y * scale))
    return corners


def _warp_quad_and_draw(painter: QPainter, quad: list, lw: int, lh: int,
                         cx: float, cy: float, alpha: float, offscreen: QImage) -> bool:
    src_quad = QPolygonF([QPointF(0, 0), QPointF(lw, 0), QPointF(lw, lh), QPointF(0, lh)])
    dst_quad = QPolygonF([QPointF(cx + dx, cy + dy) for dx, dy in quad])
    transform = QTransform()
    result = QTransform.quadToQuad(src_quad, dst_quad, transform)
    if isinstance(result, tuple):
        ok, transform = result
    else:
        ok = result
    if not ok:
        return False
    painter.setOpacity(max(0.0, min(1.0, alpha)))
    painter.setWorldTransform(transform, True)
    painter.drawImage(QRectF(0, 0, lw, lh), offscreen)
    return True


# drawing

def _draw_missing(painter: QPainter, x: float, y: float, w: float, h: float):
    painter.save()
    painter.fillRect(QRectF(x, y, max(8.0, w), max(8.0, h)), QColor("#3a1414"))
    painter.setPen(QColor("#ffffff"))
    font = painter.font()
    font.setPointSizeF(max(6.0, min(h * 0.3, 11.0)))
    painter.setFont(font)
    painter.drawText(QRectF(x, y, max(8.0, w), max(8.0, h)), Qt.AlignmentFlag.AlignCenter, "Media offline")
    painter.restore()


def _draw_background(project: Project, scene, painter: QPainter, images: ImageCache,
                      project_dir: Optional[str], ox: float, oy: float, ow: float, oh: float,
                      t: float = 0.0):
    bg = scene.background or "#141414"
    if bg == "transparent":
        return  # leave the surface as-is so nested compositing stays clear
    if bg.startswith("img:"):
        uuid_ext = bg[4:]
        pix = images.get_frame(project_dir, uuid_ext, t, loop=True, size_hint=_hint(ow, oh))
        if pix is not None:
            painter.drawImage(QRectF(ox, oy, ow, oh), pix, QRectF(pix.rect()))
            return
        painter.fillRect(QRectF(ox, oy, ow, oh), QColor("#141414"))
    else:
        painter.fillRect(QRectF(ox, oy, ow, oh), QColor(bg))


def _draw_symbol_content(project: Project, p, t: float, states: dict, images: ImageCache,
                          project_dir: Optional[str], painter: QPainter,
                          tx: float, ty: float, tw: float, th: float):
    edef = project.symbol_defs.get(p.ref)
    if not edef:
        return
    state = states.get(p.id, "inactive")
    override = switch_media_override(p, t)
    uuid_ext = override or (edef.active_image if state == "active" else (edef.inactive_image or edef.active_image))
    local_t = (t - p.start) + (p.src_offset or 0.0)
    pix = images.get_frame(project_dir, uuid_ext, local_t, loop=True,
                           size_hint=_hint(tw, th)) if uuid_ext else None
    if pix is not None:
        painter.save()
        if state == "inactive" and edef.dim_inactive and abs(edef.dim_amount - 1.0) > 1e-6:
            painter.setOpacity(max(0.0, min(1.0, edef.dim_amount)))
        painter.drawImage(QRectF(tx, ty, tw, th), pix, QRectF(pix.rect()))
        painter.restore()
    else:
        _draw_missing(painter, tx, ty, tw, th)


def _draw_placement(project: Project, p, t: float, states: dict, images: ImageCache,
                     project_dir: Optional[str], painter: QPainter,
                     tx: float, ty: float, tw: float, th: float, depth: int):
    if p.kind == "scene":
        sdef = project.scenes.get(p.ref)
        state = states.get(p.id, "active")
        inactive = state == "inactive" and sdef is not None and sdef.has_inactive
        ref_to_render = p.ref
        inactive_img = ""
        if inactive:
            if sdef.inactive_image:
                inactive_img = sdef.inactive_image
            elif sdef.inactive_ref:
                ref_to_render = sdef.inactive_ref
        local_t = (t - p.start) + (p.src_offset or 0.0)
        painter.save()
        if inactive and sdef.dim_inactive:
            dim = max(0.0, min(1.0, sdef.dim_amount))
            if abs(dim - 1.0) > 1e-6:
                painter.setOpacity(painter.opacity() * dim)
        if inactive_img:
            pix = images.get_frame(project_dir, inactive_img, local_t, loop=True,
                                   size_hint=_hint(tw, th))
            if pix is not None and not pix.isNull():
                painter.drawImage(QRectF(tx, ty, tw, th), pix, QRectF(pix.rect()))
            else:
                _draw_missing(painter, tx, ty, tw, th)
        else:
            render_scene(project, ref_to_render, local_t, images, project_dir, painter, tx, ty, tw, th, depth + 1)
        painter.restore()
    elif p.kind == "symbol":
        _draw_symbol_content(project, p, t, states, images, project_dir, painter, tx, ty, tw, th)
    elif p.kind == "image":
        if p.ref and project_dir:
            local_t = (t - p.start) + (p.src_offset or 0.0)
            pix = images.get_frame(project_dir, p.ref, local_t, loop=bool(p.loop),
                                   size_hint=_hint(tw, th))
        else:
            pix = None
        if pix is not None and not pix.isNull():
            painter.save()
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.drawImage(QRectF(tx, ty, tw, th), pix, QRectF(pix.rect()))
            painter.restore()
    elif p.kind == "video":
        if p.ref and project_dir:
            speed = p.speed if p.speed and p.speed > 0 else 1.0
            local_t = (p.src_offset or 0.0) + ((t - p.start) * speed)
            pix = images.get_frame(project_dir, p.ref, local_t, loop=bool(p.loop),
                                   size_hint=_hint(tw, th))
        else:
            pix = None
        if pix is not None and not pix.isNull():
            painter.save()
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.drawImage(QRectF(tx, ty, tw, th), pix, QRectF(pix.rect()))
            painter.restore()
        else:
            _draw_missing(painter, tx, ty, tw, th)
    elif p.kind == "shape":
        _draw_shape(p, painter, tx, ty, tw, th)


_svg_tls = threading.local()


def _svg_renderer(svg: str):
    cache = getattr(_svg_tls, "cache", None)
    if cache is None:
        cache = _svg_tls.cache = {}
    r = cache.get(svg)
    if r is None:
        if len(cache) > 64:
            cache.clear()
        r = _QSvgRenderer(svg.encode())
        cache[svg] = r if r.isValid() else False
        r = cache[svg]
    return r or None


def _draw_shape(p, painter: QPainter, tx: float, ty: float, tw: float, th: float):
    fill = QColor(p.shape_color or "#4488ff")
    border_w = max(0.0, p.shape_border_width or 0.0)
    pen = QPen(QColor(p.shape_border_color or "transparent"), border_w) if border_w > 0 else QPen(Qt.PenStyle.NoPen)
    brush = QBrush(fill)
    painter.save()
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    painter.setPen(pen)
    painter.setBrush(brush)
    rect = QRectF(tx, ty, tw, th)

    stype = p.shape_type or "rect"
    if stype == "rect":
        painter.drawRect(rect)
    elif stype == "ellipse":
        painter.drawEllipse(rect)
    elif stype == "triangle":
        pts = QPolygonF([
            QPointF(tx + tw / 2, ty),
            QPointF(tx + tw, ty + th),
            QPointF(tx, ty + th),
        ])
        painter.drawPolygon(pts)
    elif stype == "text":
        painter.setPen(QPen(fill))
        painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        text = p.shape_text or ""
        font = QFont(getattr(p, 'shape_font_family', '') or "")
        auto_fit = getattr(p, 'shape_text_auto_fit', False)
        if auto_fit and text:
            lo, hi = 1.0, max(th, tw) * 1.5
            for _ in range(16):
                mid = (lo + hi) / 2.0
                font.setPointSizeF(mid)
                fm = painter.fontMetrics() if False else __import__('PyQt6.QtGui', fromlist=['QFontMetricsF']).QFontMetricsF(font)
                br = fm.boundingRect(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)
                if br.width() <= tw and br.height() <= th:
                    lo = mid
                else:
                    hi = mid
            font.setPointSizeF(max(1.0, lo))
        else:
            scene_font_pt = max(4.0, p.shape_font_size or 24.0)
            if p.h and p.h > 0 and th > 0:
                render_scale_factor = th / p.h
                font.setPointSizeF(scene_font_pt * render_scale_factor)
            else:
                font.setPointSizeF(scene_font_pt)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)
    elif stype == "polygon" and p.shape_svg and _QSvgRenderer is not None:
        renderer = _svg_renderer(p.shape_svg)
        if renderer is not None:
            renderer.render(painter, rect)
    painter.restore()

def _has_crop(p) -> bool:
    return bool(
        max(0.0, getattr(p, "crop_l", 0.0) or 0.0) > 1e-6
        or max(0.0, getattr(p, "crop_t", 0.0) or 0.0) > 1e-6
        or max(0.0, getattr(p, "crop_r", 0.0) or 0.0) > 1e-6
        or max(0.0, getattr(p, "crop_b", 0.0) or 0.0) > 1e-6
    )

def _has_mask(p) -> bool:
    return (getattr(p, "mask_type", "none") or "none") != "none"

def _crop_fractions(p):
    cl = max(0.0, min(0.98, getattr(p, "crop_l", 0.0) or 0.0))
    ct = max(0.0, min(0.98, getattr(p, "crop_t", 0.0) or 0.0))
    cr = max(0.0, min(0.98, getattr(p, "crop_r", 0.0) or 0.0))
    cb = max(0.0, min(0.98, getattr(p, "crop_b", 0.0) or 0.0))
    if cl + cr > 0.98:
        excess = (cl + cr) - 0.98
        cl -= excess / 2.0
        cr -= excess / 2.0
    if ct + cb > 0.98:
        excess = (ct + cb) - 0.98
        ct -= excess / 2.0
        cb -= excess / 2.0
    return cl, ct, cr, cb

def _apply_crop(img: QImage, p) -> QImage:
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return img
    cl, ct, cr, cb = _crop_fractions(p)
    sx = int(round(cl * w))
    sy = int(round(ct * h))
    sw = max(1, w - sx - int(round(cr * w)))
    sh = max(1, h - sy - int(round(cb * h)))
    sx = max(0, min(w - 1, sx))
    sy = max(0, min(h - 1, sy))
    sw = max(1, min(w - sx, sw))
    sh = max(1, min(h - sy, sh))
    return img.copy(sx, sy, sw, sh)

def _build_lum_invert_stencil(mask_img: QImage, w: int, h: int) -> QImage:
    img = mask_img.convertToFormat(QImage.Format.Format_ARGB32)
    nbytes = img.sizeInBytes()
    src_ptr = img.constBits()
    src_ptr.setsize(nbytes)

    try:
        import numpy as np
        bpl = img.bytesPerLine()
        arr = np.frombuffer(bytes(src_ptr), dtype=np.uint8).reshape(h, bpl)
        cols = w * 4
        px = arr[:, :cols].reshape(h * w, 4)
        b = px[:, 0].astype(np.int32)
        g = px[:, 1].astype(np.int32)
        r = px[:, 2].astype(np.int32)
        a = px[:, 3].astype(np.int32)
        lum = np.clip((19595 * r + 38470 * g + 7471 * b) >> 16, 0, 255)
        out_a = np.clip((255 - lum) * a // 255, 0, 255).astype(np.uint8)
        out = np.stack([out_a, out_a, out_a, out_a], axis=1).reshape(h * w * 4)
        out_bytes = bytes(out)
        result = QImage(out_bytes, w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied)
        return result.copy()
    except ImportError:
        pass

    src_bytes = bytes(src_ptr)
    out_buf = bytearray(w * h * 4)
    bpl = img.bytesPerLine()
    for row in range(h):
        row_off_src = row * bpl
        row_off_dst = row * w * 4
        for col in range(w):
            s = row_off_src + col * 4
            d = row_off_dst + col * 4
            b = src_bytes[s]
            g = src_bytes[s + 1]
            r = src_bytes[s + 2]
            a = src_bytes[s + 3]
            lum = (19595 * r + 38470 * g + 7471 * b) >> 16   # BT.709
            out_a = (255 - lum) * a // 255
            out_buf[d] = out_a
            out_buf[d + 1] = out_a
            out_buf[d + 2] = out_a
            out_buf[d + 3] = out_a
    result = QImage(bytes(out_buf), w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied)
    return result.copy()


def _mask_image(w: int, h: int, p, images: "ImageCache" = None,
                project_dir: Optional[str] = None) -> Optional[QImage]:
    mtype = getattr(p, "mask_type", "none") or "none"
    if mtype == "none" or w <= 0 or h <= 0:
        return None

    stencil = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)

    if mtype == "image":
        src_id = getattr(p, "mask_source", "") or ""
        mask_img: Optional[QImage] = None
        if src_id and images is not None and project_dir is not None:
            try:
                raw = images.load(project_dir, src_id)
                if raw and not raw.isNull():
                    mask_img = raw.scaled(
                        w, h,
                        Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation
                    ).convertToFormat(QImage.Format.Format_ARGB32)
            except Exception:
                mask_img = None

        stencil.fill(0)
        if mask_img is None:
            return stencil

        stencil = _build_lum_invert_stencil(mask_img, w, h)

    else:
        stencil.fill(0)
        mp = QPainter(stencil)
        mp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        mp.setPen(Qt.PenStyle.NoPen)
        mp.setBrush(QColor(255, 255, 255, 255))
        rect = QRectF(0, 0, w, h)
        if mtype == "ellipse":
            mp.drawEllipse(rect)
        elif mtype == "rounded_rect":
            frac = max(0.0, min(1.0, getattr(p, "mask_radius", 0.2) or 0.0))
            radius = frac * min(w, h) / 2.0
            mp.drawRoundedRect(rect, radius, radius)
        else:
            mp.end()
            return None
        mp.end()

    if getattr(p, "mask_invert", False):
        inv = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        inv.fill(QColor(255, 255, 255, 255))
        ip = QPainter(inv)
        ip.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
        ip.drawImage(0, 0, stencil)
        ip.end()
        stencil = inv
    return stencil


def _apply_mask(img: QImage, p, images=None, project_dir=None) -> QImage:
    stencil = _mask_image(img.width(), img.height(), p, images, project_dir)
    if stencil is None:
        return img
    out = QImage(img)
    op = QPainter(out)
    op.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    op.drawImage(0, 0, stencil)
    op.end()
    return out


def _apply_chroma_key(img: QImage, p) -> QImage:
    if not getattr(p, "chroma_key_enabled", False):
        return img
    color = QColor(getattr(p, "chroma_key_color", "#00ff00"))
    sim = getattr(p, "chroma_key_similarity", 0.3)
    smooth = getattr(p, "chroma_key_smoothness", 0.1)
    
    kr, kg, kb = color.red(), color.green(), color.blue()
    w, h = img.width(), img.height()
    out = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    ptr = out.bits()
    ptr.setsize(out.sizeInBytes())
    
    try:
        import numpy as np
        bpl = out.bytesPerLine()
        arr = np.frombuffer(bytes(ptr), dtype=np.uint8).copy().reshape(h, bpl)
        cols = w * 4
        px = arr[:, :cols].reshape(h * w, 4)
        b, g, r, a = px[:,0], px[:,1], px[:,2], px[:,3]
        
        dr = r.astype(np.float32) - kr
        dg = g.astype(np.float32) - kg
        db = b.astype(np.float32) - kb
        dist = np.sqrt(dr*dr + dg*dg + db*db) / 441.67
        
        s = max(0.001, sim)
        sm = max(0.001, smooth)
        
        alpha_mult = (dist - s) / sm
        alpha_mult = np.clip(alpha_mult, 0.0, 1.0)
        
        px[:, 0] = (b * alpha_mult).astype(np.uint8)
        px[:, 1] = (g * alpha_mult).astype(np.uint8)
        px[:, 2] = (r * alpha_mult).astype(np.uint8)
        px[:, 3] = (a * alpha_mult).astype(np.uint8)
        
        res = QImage(bytes(arr), w, h, bpl, QImage.Format.Format_ARGB32_Premultiplied)
        return res.copy()
    except ImportError:
        pass
    return img

def _apply_crop_mask(img: QImage, p, images=None, project_dir=None) -> QImage:
    if getattr(p, "chroma_key_enabled", False):
        img = _apply_chroma_key(img, p)
    if _has_crop(p):
        img = _apply_crop(img, p)
    if _has_mask(p):
        img = _apply_mask(img, p, images, project_dir)
    return img


def _draw_animated_placement(project: Project, p, t: float, states: dict, images: ImageCache,
                              project_dir: Optional[str], painter: QPainter,
                              bx: float, by: float, bw: float, bh: float, depth: int,
                              alpha: float, scale: float, tilt: float):
    lw = max(1, round(bw))
    lh = max(1, round(bh))

    if _has_crop(p):
        cl, ct, cr, cb = _crop_fractions(p)
        vis_w = max(0.02, 1.0 - cl - cr)
        vis_h = max(0.02, 1.0 - ct - cb)
        full_lw = max(1, round(lw / vis_w))
        full_lh = max(1, round(lh / vis_h))
        offscreen = QImage(full_lw, full_lh, QImage.Format.Format_ARGB32_Premultiplied)
        offscreen.fill(0)
        off_painter = QPainter(offscreen)
        off_painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        _draw_placement(project, p, t, states, images, project_dir,
                        off_painter, 0, 0, full_lw, full_lh, depth)
        off_painter.end()
        offscreen = _apply_crop_mask(offscreen, p, images, project_dir)
    else:
        offscreen = QImage(lw, lh, QImage.Format.Format_ARGB32_Premultiplied)
        offscreen.fill(0)
        off_painter = QPainter(offscreen)
        off_painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        _draw_placement(project, p, t, states, images, project_dir,
                        off_painter, 0, 0, lw, lh, depth)
        off_painter.end()
        if _has_mask(p):
            offscreen = _apply_mask(offscreen, p, images, project_dir)
        if getattr(p, "chroma_key_enabled", False):
            offscreen = _apply_chroma_key(offscreen, p)

    if scale < 0.998:
        dw = max(1, round(lw * scale))
        dh = max(1, round(lh * scale))
        paste_x = bx + (bw - dw) / 2.0
        paste_y = by + (bh - dh) / 2.0
    else:
        dw, dh = float(lw), float(lh)
        paste_x, paste_y = bx, by

    painter.save()
    if getattr(p, "enable_3d", False):
        quad = _project_3d_quad(p, dw, dh)
        if quad is None:
            painter.restore()
            return
        cx = paste_x + dw / 2.0
        cy = paste_y + dh / 2.0
        _warp_quad_and_draw(painter, quad, lw, lh, cx, cy, alpha, offscreen)
    elif abs(tilt) >= 0.01:
        quad = _project_tilt_quad(tilt, dw, dh)
        if quad is not None:
            cx = paste_x + dw / 2.0
            cy = paste_y + dh / 2.0
            _warp_quad_and_draw(painter, quad, lw, lh, cx, cy, alpha, offscreen)
    else:
        painter.setOpacity(max(0.0, min(1.0, alpha)))
        painter.drawImage(QRectF(paste_x, paste_y, dw, dh), offscreen)
    painter.restore()


def render_scene(project: Project, scene_id: str, t: float, images: ImageCache,
                  project_dir: Optional[str], painter: QPainter,
                  ox: float, oy: float, ow: float, oh: float, depth: int = 0):
    if depth > MAX_RECURSION:
        return
    scene = project.scenes.get(scene_id)
    if not scene:
        return

    painter.save()
    painter.setClipRect(QRectF(ox, oy, ow, oh))

    _draw_background(project, scene, painter, images, project_dir, ox, oy, ow, oh, t)

    scene_w, scene_h = project.scene_size(scene_id)
    sx = ow / max(1, scene_w)
    sy = oh / max(1, scene_h)

    hidden_tracks = set(getattr(scene, "hidden_tracks", []))
    on = [p for p in scene.placements if p.start <= t <= p.end and p.track not in hidden_tracks]
    on = on + duplication_echoes(project, scene, t)
    on.sort(key=lambda p: (p.layer or 0, p.start))
    states = symbol_states_from_placements(project, on, t)

    for p in on:
        if p.keyframes and "::echo" not in p.id:
            eff_x, eff_y, eff_w, eff_h, eff_rot = keyframe_transform(p, t - p.start)
        else:
            eff_x, eff_y = p.x, p.y
            eff_w, eff_h = p.w, p.h
            eff_h = p.h
            eff_rot = getattr(p, "rotation", 0.0) or 0.0
            
        alpha, scale, tilt, off_x, off_y = anim_progress(
            p, t, (scene_w, scene_h), (eff_x, eff_y, eff_w, eff_h))

        has_3d = getattr(p, "enable_3d", False)
        has_chroma = getattr(p, "chroma_key_enabled", False)

        bx = ox + eff_x * sx + off_x * sx
        by = oy + eff_y * sy + off_y * sy
        bw = eff_w * sx
        bh = eff_h * sy

        rot = 0.0 if has_3d else eff_rot
        has_rot = abs(rot) >= 0.01
        flip_h, flip_v = timing_flip_state(p, t)
        if has_rot or flip_h or flip_v:
            cx, cy = bx + bw / 2.0, by + bh / 2.0
            painter.save()
            painter.translate(cx, cy)
            if has_rot:
                painter.rotate(rot)
            if flip_h or flip_v:
                painter.scale(-1.0 if flip_h else 1.0, -1.0 if flip_v else 1.0)
            painter.translate(-cx, -cy)
        transformed = has_rot or flip_h or flip_v

        flash = timing_flash_alpha(p, t)
        if flash is not None:
            alpha *= flash
        if alpha <= 0.002:
            if transformed:
                painter.restore()
            continue

        if alpha >= 0.998 and scale >= 0.998 and abs(tilt) < 0.01 and not _has_crop(p) and not _has_mask(p) and not has_3d and not has_chroma:
            _draw_placement(project, p, t, states, images, project_dir, painter, bx, by, bw, bh, depth)
        else:
            _draw_animated_placement(project, p, t, states, images, project_dir, painter,
                                      bx, by, bw, bh, depth, alpha, scale, tilt)

        if transformed:
            painter.restore()

    painter.restore()


def render_frame(project: Project, scene_id: str, t: float, images: ImageCache,
                  project_dir: Optional[str], width: int, height: int,
                  transparent: bool = False) -> QImage:
    scene = project.scenes.get(scene_id)
    use_alpha = transparent or bool(scene and (scene.background or "") == "transparent")
    if use_alpha:
        img = QImage(max(1, width), max(1, height), QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
    else:
        img = QImage(max(1, width), max(1, height), QImage.Format.Format_RGB32)
        img.fill(QColor("#141414"))
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    render_scene(project, scene_id, t, images, project_dir, painter, 0, 0, width, height, 0)
    painter.end()
    return img
