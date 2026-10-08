from __future__ import annotations

import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Optional

SCENE_W = 1280
SCENE_H = 720
DEFAULT_FPS = 30
MAIN_ID = "__main__"
DEFAULT_SCENE_BG = "#204030"
SCHEMA_VERSION = 4


def new_uuid() -> str:
    return _uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Placement:
    id: str = field(default_factory=new_uuid)
    kind: str = ""  # "scene" | "symbol" | "sound" | "image" | "video" | "shape"
    ref: str = ""
    track: int = 0
    start: float = 0.0
    end: float = 0.0
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    rotation: float = 0.0   # degrees, applied around placement center
    anim_in: str = "none"
    anim_out: str = "none"
    anim_in_dur: float = 0.0
    anim_out_dur: float = 0.0
    anim_scale: float = 0
    anim_tilt_deg: float = 0
    anim_custom: bool = False
    # pop / slide / enter-slide animations only fade when these are on
    anim_in_fade: bool = False
    anim_out_fade: bool = False
    # flashing timing type extras
    timing_double_flash: bool = False       # blink twice at every timing point
    timing_double_flash_delay: float = 0.1  # seconds: length of each blink and of the gap between them
    timing_flip: bool = False               # toggle a flip at every timing point
    timing_flip_axis: str = "horizontal"    # "horizontal" | "vertical"
    timing_type: str = "none"
    timing_offset_x: float = 0.0
    timing_offset_y: float = 0.0
    timing_points: list = field(default_factory=list)
    timing_random_pos: bool = False
    timing_random_x_min: float = 0.0
    timing_random_x_max: float = 0.0
    timing_random_y_min: float = 0.0
    timing_random_y_max: float = 0.0
    timing_random_seed: int = 0
    timing_overrides: dict = field(default_factory=dict)
    media_list: list = field(default_factory=list)
    media_direction: str = "forward"
    layer: int = 0
    pinned: bool = False
    loop: bool = False
    muted: bool = False
    src_offset: float = 0.0
    volume: float = 1.0   # linear gain, used by "sound" and "video" placements
    speed: float = 1.0    # playback rate multiplier, used by "video" placements
    opacity: float = 1.0  # 0.0 (transparent) to 1.0 (fully opaque)
    shape_type: str = "rect"   # "rect" | "ellipse" | "triangle" | "text" | "polygon"
    shape_color: str = "#4488ff"
    shape_border_color: str = ""
    shape_border_width: float = 0.0
    shape_text: str = ""
    shape_font_family: str = ""
    shape_font_size: float = 24.0
    shape_text_auto_fit: bool = False   # if True, stretch text to fill the shape; font_size ignored
    shape_svg: str = "" # SVG data for custom polygons
    # cropping: fractions (0..1) of the placement's own box cropped away from each edge
    crop_l: float = 0.0
    crop_t: float = 0.0
    crop_r: float = 0.0
    crop_b: float = 0.0
    # masking: an alpha shape applied on top of crop.
    # mask_type: "none" | "ellipse" | "rounded_rect" | "image"
    mask_type: str = "none"
    mask_radius: float = 0.2  # rounded_rect corner radius, fraction of min(w, h)
    mask_invert: bool = False
    mask_source: str = ""     # source uuid_ext used when mask_type == "image"
    # position keyframes: [{"t": local_seconds, "x":.., "y":.., "ease": [x1,y1,x2,y2]}, ...]
    # sorted ascending by "t". When present (len >= 1) the placement's rendered
    # x/y comes from keyframe_xy() below instead of the static x/y fields.
    keyframes: list = field(default_factory=list)
    # 3D transform fields
    enable_3d: bool = False
    translate_3d_x: float = 0.0
    translate_3d_y: float = 0.0
    translate_3d_z: float = 0.0
    rotate_3d_x: float = 0.0   # degrees, pitch (tilt forward/back)
    rotate_3d_y: float = 0.0   # degrees, yaw (left/right)
    rotate_3d_z: float = 0.0   # degrees, roll (same as 2D rotation when 3d enabled)
    # chroma key (green screen) - applies to image/video/scene/symbol placements
    chroma_key_enabled: bool = False
    chroma_key_color: str = "#00ff00"  # key color (typically green or blue)
    chroma_key_similarity: float = 0.3  # 0..1, how close a color must be to be keyed out
    chroma_key_smoothness: float = 0.1  # 0..1, edge softness
    # animation ease types: "linear" | "ease_in" | "ease_out" | "ease_in_out" | "custom"
    anim_in_ease: str = "linear"
    anim_out_ease: str = "linear"

    @staticmethod
    def from_dict(d: dict) -> "Placement":
        known = {f for f in Placement.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in known}
        p = Placement(**kwargs)
        if not p.id:
            p.id = new_uuid()
        # projects saved before the fade checkboxes existed always faded these animations
        if "anim_in_fade" not in d:
            p.anim_in_fade = True
        if "anim_out_fade" not in d:
            p.anim_out_fade = True
        p.timing_points = list(d.get("timing_points") or [])
        p.timing_overrides = dict(d.get("timing_overrides") or {})
        p.media_list = list(d.get("media_list") or [])
        p.keyframes = [dict(kf) for kf in (d.get("keyframes") or [])]
        p.keyframes.sort(key=lambda kf: kf.get("t", 0.0))
        return p

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["timing_points"] = list(self.timing_points)
        d["timing_overrides"] = dict(self.timing_overrides)
        d["media_list"] = list(self.media_list)
        d["keyframes"] = [dict(kf) for kf in self.keyframes]
        return d


# position keyframing (motion-path animation with per-segment bezier easing)

DEFAULT_EASE = (0.0, 0.0, 1.0, 1.0)  # linear cubic-bezier control points
KEYFRAME_MERGE_SNAP = 0.05  # seconds; moves within this window update the same key


def _cubic_component(t: float, p1: float, p2: float) -> float:
    mt = 1.0 - t
    return 3.0 * mt * mt * t * p1 + 3.0 * mt * t * t * p2 + t * t * t


def _cubic_component_deriv(t: float, p1: float, p2: float) -> float:
    mt = 1.0 - t
    return 3.0 * mt * mt * p1 + 6.0 * mt * t * (p2 - p1) + 3.0 * t * t * (1.0 - p2)


def ease_value(ease, frac: float) -> float:
    x1, y1, x2, y2 = ease
    frac = max(0.0, min(1.0, frac))
    if abs(x1 - y1) < 1e-6 and abs(x2 - y2) < 1e-6:
        return frac  # symmetric control points == linear
    t = frac
    for _ in range(8):
        dx = _cubic_component_deriv(t, x1, x2)
        if abs(dx) < 1e-6:
            break
        t -= (_cubic_component(t, x1, x2) - frac) / dx
        t = max(0.0, min(1.0, t))
    return _cubic_component(t, y1, y2)


def keyframe_xy(p: "Placement", local_t: float):
    x, y, _w, _h, _r = keyframe_transform(p, local_t)
    return x, y


def keyframe_transform(p: "Placement", local_t: float):
    kfs = p.keyframes
    static_rot = getattr(p, "rotation", 0.0) or 0.0

    def _from_kf(kf):
        return (
            kf.get("x", p.x), kf.get("y", p.y),
            kf.get("w", p.w), kf.get("h", p.h),
            kf.get("rotation", static_rot),
        )

    if not kfs:
        return p.x, p.y, p.w, p.h, static_rot
    if len(kfs) == 1:
        return _from_kf(kfs[0])
    if local_t <= kfs[0].get("t", 0.0):
        return _from_kf(kfs[0])
    if local_t >= kfs[-1].get("t", 0.0):
        return _from_kf(kfs[-1])
    for i in range(len(kfs) - 1):
        a, b = kfs[i], kfs[i + 1]
        at, bt = a.get("t", 0.0), b.get("t", 0.0)
        if at <= local_t <= bt:
            seg = bt - at
            frac = 0.0 if seg <= 1e-9 else (local_t - at) / seg
            eased = ease_value(a.get("ease") or DEFAULT_EASE, frac)
            ax, ay = a.get("x", p.x), a.get("y", p.y)
            bx, by = b.get("x", p.x), b.get("y", p.y)
            aw, ah = a.get("w", p.w), a.get("h", p.h)
            bw, bh = b.get("w", p.w), b.get("h", p.h)
            ar = a.get("rotation", static_rot)
            br = b.get("rotation", static_rot)
            return (
                ax + (bx - ax) * eased,
                ay + (by - ay) * eased,
                aw + (bw - aw) * eased,
                ah + (bh - ah) * eased,
                ar + (br - ar) * eased,
            )
    return _from_kf(kfs[-1])


def upsert_keyframe(p: "Placement", local_t: float, x: float, y: float,
                    w: Optional[float] = None, h: Optional[float] = None,
                    rotation: Optional[float] = None) -> dict:
    local_t = max(0.0, local_t)
    for kf in p.keyframes:
        if abs(kf.get("t", 0.0) - local_t) <= KEYFRAME_MERGE_SNAP:
            kf["x"] = x
            kf["y"] = y
            if w is not None:
                kf["w"] = w
            if h is not None:
                kf["h"] = h
            if rotation is not None:
                kf["rotation"] = rotation
            return kf
    kf: dict = {"t": local_t, "x": x, "y": y, "ease": list(DEFAULT_EASE)}
    if w is not None:
        kf["w"] = w
    if h is not None:
        kf["h"] = h
    if rotation is not None:
        kf["rotation"] = rotation
    p.keyframes.append(kf)
    p.keyframes.sort(key=lambda k: k.get("t", 0.0))
    return kf


@dataclass
class Folder:
    id: str = field(default_factory=new_uuid)
    name: str = "New Folder"
    category: str = ""  # "source" | "scene" | "symbol"
    parent: Optional[str] = None
    created: int = 0

    @staticmethod
    def from_dict(d: dict) -> "Folder":
        return Folder(
            id=d.get("id") or new_uuid(),
            name=d.get("name") or "New Folder",
            category=d.get("category") or "",
            parent=d.get("parent") or None,
            created=d.get("created") or 0,
        )

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Scene:
    id: str = ""
    name: str = ""
    background: str = "#000000"
    folder_id: str = ""
    inner_res_width: int = 0
    inner_res_height: int = 0
    created: int = 0
    placements: list = field(default_factory=list)
    hidden_tracks: list = field(default_factory=list)  # list of track indices that are hidden
    track_names: dict = field(default_factory=dict)    # {str(track_idx): display_name}
    # inactive state
    elem_class: str = ""
    has_inactive: bool = False
    inactive_ref: str = ""
    inactive_image: str = ""
    dim_inactive: bool = True
    dim_amount: float = 0.5

    @staticmethod
    def from_dict(d: dict) -> "Scene":
        return Scene(
            id=d.get("id"),
            name=d.get("name"),
            background=d.get("background") or "#000000",
            folder_id="" if d.get("id") == MAIN_ID else (d.get("folder_id") or ""),
            inner_res_width=d.get("inner_res_width") or 0,
            inner_res_height=d.get("inner_res_height") or 0,
            created=d.get("created") or 0,
            placements=[Placement.from_dict(pd) for pd in (d.get("placements") or [])],
            hidden_tracks=list(d.get("hidden_tracks") or []),
            track_names=dict(d.get("track_names") or {}),
            elem_class=d.get("elem_class") or "",
            has_inactive=bool(d.get("has_inactive", False)),
            inactive_ref=d.get("inactive_ref") or "",
            inactive_image=d.get("inactive_image") or "",
            dim_inactive=bool(d.get("dim_inactive", True)),
            dim_amount=float(d.get("dim_amount", 0.5)),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "background": self.background,
            "inner_res_width": self.inner_res_width or 0,
            "inner_res_height": self.inner_res_height or 0,
            "folder_id": self.folder_id or "",
            "created": self.created or 0,
            "placements": [p.to_dict() for p in self.placements],
            "hidden_tracks": self.hidden_tracks or [],
            "track_names": self.track_names or {},
            "elem_class": self.elem_class or "",
            "has_inactive": self.has_inactive,
            "inactive_ref": self.inactive_ref or "",
            "inactive_image": self.inactive_image or "",
            "dim_inactive": self.dim_inactive,
            "dim_amount": self.dim_amount,
        }


@dataclass
class SymbolDef:
    id: str = field(default_factory=new_uuid)
    name: str = "Symbol"
    elem_class: str = "generic"
    elem_id: str = "sym1"
    active_image: str = ""
    inactive_image: str = ""
    width: float = 0.0
    height: float = 0.0
    dim_inactive: bool = False
    dim_amount: float = 0.5
    anim_in: str = "none"
    anim_out: str = "none"
    anim_in_dur: float = 0.0
    anim_out_dur: float = 0.0
    anim_scale: float = 0
    anim_tilt_deg: float = 0
    anim_in_fade: bool = False
    anim_out_fade: bool = False
    folder_id: str = ""
    created: int = 0

    @staticmethod
    def from_dict(d: dict) -> "SymbolDef":
        known = {f for f in SymbolDef.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in known}
        s = SymbolDef(**kwargs)
        if not s.id:
            s.id = new_uuid()
        if "anim_in_fade" not in d:
            s.anim_in_fade = True
        if "anim_out_fade" not in d:
            s.anim_out_fade = True
        return s

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Project:
    schema_version: int = SCHEMA_VERSION
    name: str = "Untitled Project"
    fps: int = DEFAULT_FPS
    width: int = SCENE_W
    height: int = SCENE_H
    export_video_bitrate: str = "8000k"
    export_audio_bitrate: str = "192k"
    export_channels: int = 2
    default_bg: str = DEFAULT_SCENE_BG
    scenes: dict = field(default_factory=dict) # id -> Scene
    symbol_defs: dict = field(default_factory=dict) # id -> SymbolDef
    classes: list = field(default_factory=list)
    sources: dict = field(default_factory=dict) # uuid(+ext) -> original filename
    source_created: dict = field(default_factory=dict) # uuid -> ms epoch
    folders: dict = field(default_factory=dict) # id -> Folder
    source_folders: dict = field(default_factory=dict) # uuid -> folder id
    track_hidden: dict = field(default_factory=dict)  # scene_id -> set of hidden track indices
    track_order: dict = field(default_factory=dict)   # scene_id -> list of track indices (reordering)
    ruler_waveforms: set = field(default_factory=set) # source uuid(+ext) values shown in the time ruler

    def __post_init__(self):
        if MAIN_ID not in self.scenes:
            self.scenes[MAIN_ID] = Scene(id=MAIN_ID, name="Main Timeline", background="#141414")

    # LIBRARY folders
    def folders_in(self, category: str, parent_id: Optional[str]) -> list:
        pid = parent_id or None
        out = [
            f for f in self.folders.values()
            if f.category == category and (f.parent or None) == pid
        ]
        out.sort(key=lambda f: f.name.lower())
        return out

    def folder_and_descendants(self, folder_id: str) -> list:
        out = [folder_id]
        grew = True
        while grew:
            grew = False
            for fid, f in self.folders.items():
                if fid not in out and f.parent in out:
                    out.append(fid)
                    grew = True
        return out

    def would_create_folder_cycle(self, fid: str, target_parent_id: Optional[str]) -> bool:
        cur = target_parent_id or None
        while cur:
            if cur == fid:
                return True
            f = self.folders.get(cur)
            cur = f.parent if f else None
        return False

    def duration_of(self, scene_id: str) -> float:
        sc = self.scenes.get(scene_id)
        if not sc or not sc.placements:
            return 8.0
        return max(p.end for p in sc.placements)

    def scene_size(self, scene_id: str):
        sc = self.scenes.get(scene_id)
        if sc and sc.inner_res_width > 0 and sc.inner_res_height > 0:
            return sc.inner_res_width, sc.inner_res_height
        return self.width, self.height

    def ensure_class(self, name: str):
        if name and name not in self.classes:
            self.classes.append(name)

    def scene_reaches(self, from_scene_id: str, target_scene_id: str, _visited: Optional[set] = None) -> bool:
        visited = _visited if _visited is not None else set()
        if from_scene_id in visited:
            return False
        visited.add(from_scene_id)
        scene = self.scenes.get(from_scene_id)
        if not scene:
            return False
        for p in scene.placements:
            if p.kind == "scene":
                if p.ref == target_scene_id:
                    return True
                if self.scene_reaches(p.ref, target_scene_id, visited):
                    return True
        return False

    # (de)serialization
    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version or SCHEMA_VERSION,
            "name": self.name,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "export_video_bitrate": self.export_video_bitrate,
            "export_audio_bitrate": self.export_audio_bitrate,
            "export_channels": self.export_channels,
            "default_bg": self.default_bg,
            "scenes": {sid: s.to_dict() for sid, s in self.scenes.items()},
            "symbol_defs": {k: s.to_dict() for k, s in self.symbol_defs.items()},
            "classes": list(self.classes),
            "sources": dict(self.sources),
            "sourceCreated": dict(self.source_created),
            "folders": {k: f.to_dict() for k, f in self.folders.items()},
            "source_folders": dict(self.source_folders),
            "track_hidden": {sid: list(v) for sid, v in self.track_hidden.items()},
            "track_order": {sid: list(v) for sid, v in self.track_order.items()},
            "ruler_waveforms": sorted(self.ruler_waveforms),
        }

    @staticmethod
    def from_dict(d: dict) -> "Project":
        p = Project(
            schema_version=d.get("schema_version") or 1,
            name=d.get("name") or "Untitled Project",
            fps=d.get("fps") or DEFAULT_FPS,
            width=d.get("width") or SCENE_W,
            height=d.get("height") or SCENE_H,
            export_video_bitrate=d.get("export_video_bitrate") or "8000k",
            export_audio_bitrate=d.get("export_audio_bitrate") or "192k",
            export_channels=d.get("export_channels") if isinstance(d.get("export_channels"), int) else 2,
            default_bg=d.get("default_bg") or DEFAULT_SCENE_BG,
            scenes={},
        )
        for k, sd in (d.get("symbol_defs") or {}).items():
            p.symbol_defs[k] = SymbolDef.from_dict(sd)
        for sid, sd in (d.get("scenes") or {}).items():
            p.scenes[sid] = Scene.from_dict(sd)
        if MAIN_ID not in p.scenes:
            p.scenes[MAIN_ID] = Scene(id=MAIN_ID, name="Main Timeline", background="#141414")
        p.classes = list(d.get("classes") or [])
        p.sources = dict(d.get("sources") or {})
        p.source_created = dict(d.get("sourceCreated") or {})
        p.ruler_waveforms = {u for u in (d.get("ruler_waveforms") or []) if u in p.sources}

        if "scene_folders" in d:
            for sid, fid in d["scene_folders"].items():
                if sid in p.scenes:
                    p.scenes[sid].folder_id = fid
                    
        for u in list(p.source_created):
            if u not in p.sources:
                del p.source_created[u]
        for k, fd in (d.get("folders") or {}).items():
            p.folders[k] = Folder.from_dict(fd)
        p.source_folders = dict(d.get("source_folders") or {})
        for u in list(p.source_folders):
            if u not in p.sources or p.source_folders[u] not in p.folders:
                del p.source_folders[u]
        for sd in p.symbol_defs.values():
            if sd.folder_id and sd.folder_id not in p.folders:
                sd.folder_id = ""
        for sc in p.scenes.values():
            if sc.folder_id and sc.folder_id not in p.folders:
                sc.folder_id = ""
        for f in p.folders.values():
            if f.parent and (f.parent not in p.folders or p.folders[f.parent].category != f.category):
                f.parent = None
        for sd in p.symbol_defs.values():
            p.ensure_class(sd.elem_class)
        for sid, vh in (d.get("track_hidden") or {}).items():
            p.track_hidden[sid] = list(vh)
        for sid, vo in (d.get("track_order") or {}).items():
            p.track_order[sid] = list(vo)
        return p

    @staticmethod
    def new_default() -> "Project":
        p = Project()
        return p
