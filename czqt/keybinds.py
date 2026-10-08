from __future__ import annotations

from . import i18n

DEFAULT_KEYBINDS = {
    # File
    "action_new": "Ctrl+N",
    "action_open": "Ctrl+O",
    "action_save": "Ctrl+S",
    "action_save_as": "Ctrl+Shift+S",
    "action_project_settings": "Ctrl+P",
    "action_preferences": "Ctrl+Shift+P",
    "action_quit": "Ctrl+Q",
    # Playback
    "action_play_pause": "Space",
    "action_stop": "Ctrl+Space",
    "action_next_frame": ".",
    "action_prev_frame": ",",
    "action_next_sec": "Shift+.",
    "action_prev_sec": "Shift+,",
    # Export
    "action_export_video": "Ctrl+E",
    "action_export_png": "Ctrl+Shift+E",
    # Edit
    "action_undo": "Ctrl+Z",
    "action_redo": "Ctrl+Shift+Z",
    "action_cut": "Ctrl+X",
    "action_copy": "Ctrl+C",
    "action_paste": "Ctrl+V",
    "action_delete": "Delete",
    "action_split": "S",
    # Timeline / Preview modes
    "action_timeline_mode": "T",
    "action_normal_mode": "N",
    "action_pan_mode": "P",
    "action_crop_mode": "C",
    "action_mask_mode": "M",
    "action_keyframe_mode": "K",
}

class _KeybindGroups:
    def __iter__(self):
        return iter([
            (i18n.tr("keybind.groupFile"), ["action_new", "action_open", "action_save", "action_save_as",
                      "action_project_settings", "action_preferences", "action_quit"]),
            (i18n.tr("keybind.groupPlayback"), ["action_play_pause", "action_stop", "action_next_frame",
                          "action_prev_frame", "action_next_sec", "action_prev_sec"]),
            (i18n.tr("keybind.groupExport"), ["action_export_video", "action_export_png"]),
            (i18n.tr("keybind.groupEdit"), ["action_undo", "action_redo", "action_cut", "action_copy",
                      "action_paste", "action_delete", "action_split"]),
            (i18n.tr("keybind.groupTimelinePreviewModes"),
             ["action_timeline_mode", "action_normal_mode", "action_pan_mode", "action_crop_mode",
              "action_mask_mode", "action_keyframe_mode"]),
        ])


KEYBIND_GROUPS = _KeybindGroups()


class _KeybindLabels:
    _KEYS = {
        "action_new": "keybind.newProject",
        "action_open": "keybind.openProject",
        "action_save": "keybind.saveProject",
        "action_save_as": "keybind.saveProjectAs",
        "action_project_settings": "keybind.projectSettings",
        "action_preferences": "keybind.preferences",
        "action_quit": "keybind.quit",
        "action_play_pause": "keybind.playPause",
        "action_stop": "keybind.stop",
        "action_next_frame": "keybind.nextFrame",
        "action_prev_frame": "keybind.prevFrame",
        "action_next_sec": "keybind.nextSec",
        "action_prev_sec": "keybind.prevSec",
        "action_export_video": "keybind.exportVideo",
        "action_export_png": "keybind.exportPng",
        "action_undo": "keybind.undo",
        "action_redo": "keybind.redo",
        "action_cut": "keybind.cut",
        "action_copy": "keybind.copy",
        "action_paste": "keybind.paste",
        "action_delete": "keybind.deleteSelected",
        "action_split": "keybind.splitAtPlayhead",
        "action_timeline_mode": "keybind.toggleTimingMode",
        "action_normal_mode": "keybind.previewNormalMode",
        "action_pan_mode": "keybind.previewPanMode",
        "action_crop_mode": "keybind.previewCropMode",
        "action_mask_mode": "keybind.previewMaskMode",
        "action_keyframe_mode": "keybind.previewKeyframeMode",
    }

    def get(self, action_name, default=None):
        key = self._KEYS.get(action_name)
        return i18n.tr(key) if key else default

    def __getitem__(self, action_name):
        return i18n.tr(self._KEYS[action_name])

    def __contains__(self, action_name):
        return action_name in self._KEYS


KEYBIND_LABELS = _KeybindLabels()

IMPLEMENTED_ACTIONS = {
    "action_new", "action_open", "action_save", "action_save_as",
    "action_project_settings", "action_preferences", "action_quit",
    "action_play_pause", "action_stop",
    "action_next_frame", "action_prev_frame",
    "action_next_sec", "action_prev_sec",
    "action_export_video",
    "action_undo", "action_redo",
    "action_cut", "action_copy", "action_paste",
    "action_delete", "action_split",
    "action_normal_mode", "action_pan_mode",
    "action_crop_mode", "action_mask_mode",
    "action_keyframe_mode",
    "action_timeline_mode",
}


def merged_keybinds(overrides: dict) -> dict:
    merged = dict(DEFAULT_KEYBINDS)
    if overrides:
        merged.update({k: v for k, v in overrides.items() if k in DEFAULT_KEYBINDS})
    return merged