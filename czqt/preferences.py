from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from PyQt6.QtCore import QSettings

from .keybinds import DEFAULT_KEYBINDS, merged_keybinds

_ORG = "CZEditor"
_APP = "CZEditor"


def default_project_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Documents", "CZEditor Projects")


@dataclass
class Preferences:
    autosave_enabled: bool = True
    autosave_interval_sec: int = 30
    default_project_dir: str = field(default_factory=default_project_dir)
    default_scene_bg: str = "#204030"
    preview_quality: str = "1"
    snap_to_playhead: bool = True
    snap_to_clips: bool = True
    language: str = "english"
    allow_recursive_scene_nesting: bool = False 
    max_scene_nesting_depth: int = 8
    auto_apply_properties: bool = True
    ffmpeg_path: str = ""
    keybinds: dict = field(default_factory=lambda: dict(DEFAULT_KEYBINDS))

    @staticmethod
    def load() -> "Preferences":
        s = QSettings(_ORG, _APP)
        p = Preferences()
        p.autosave_enabled = s.value("autosave_enabled", p.autosave_enabled, type=bool)
        p.autosave_interval_sec = s.value("autosave_interval_sec", p.autosave_interval_sec, type=int)
        p.default_project_dir = s.value("default_project_dir", p.default_project_dir, type=str) or default_project_dir()
        p.default_scene_bg = s.value("default_scene_bg", p.default_scene_bg, type=str)
        p.preview_quality = s.value("preview_quality", p.preview_quality, type=str)
        p.snap_to_playhead = s.value("snap_to_playhead", p.snap_to_playhead, type=bool)
        p.snap_to_clips = s.value("snap_to_clips", p.snap_to_clips, type=bool)
        p.language = s.value("language", p.language, type=str)
        p.allow_recursive_scene_nesting = s.value(
            "allow_recursive_scene_nesting", p.allow_recursive_scene_nesting, type=bool
        )
        p.max_scene_nesting_depth = s.value("max_scene_nesting_depth", p.max_scene_nesting_depth, type=int)
        p.auto_apply_properties = s.value("auto_apply_properties", p.auto_apply_properties, type=bool)
        p.ffmpeg_path = s.value("ffmpeg_path", p.ffmpeg_path, type=str)
        raw_keybinds = s.value("keybinds", "", type=str)
        overrides = {}
        if raw_keybinds:
            try:
                overrides = json.loads(raw_keybinds)
            except Exception:
                overrides = {}
        p.keybinds = merged_keybinds(overrides)
        return p

    def save(self):
        s = QSettings(_ORG, _APP)
        s.setValue("autosave_enabled", self.autosave_enabled)
        s.setValue("autosave_interval_sec", self.autosave_interval_sec)
        s.setValue("default_project_dir", self.default_project_dir)
        s.setValue("default_scene_bg", self.default_scene_bg)
        s.setValue("preview_quality", self.preview_quality)
        s.setValue("snap_to_playhead", self.snap_to_playhead)
        s.setValue("snap_to_clips", self.snap_to_clips)
        s.setValue("language", self.language)
        s.setValue("allow_recursive_scene_nesting", self.allow_recursive_scene_nesting)
        s.setValue("max_scene_nesting_depth", self.max_scene_nesting_depth)
        s.setValue("auto_apply_properties", self.auto_apply_properties)
        s.setValue("ffmpeg_path", self.ffmpeg_path)
        s.setValue("keybinds", json.dumps(self.keybinds))

    def reset(self):
        default = Preferences()
        self.__dict__.update(default.__dict__)
        self.save()
