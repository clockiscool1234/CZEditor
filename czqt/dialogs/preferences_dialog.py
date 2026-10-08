from __future__ import annotations

import os

from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr, available_languages
from ..keybinds import DEFAULT_KEYBINDS, IMPLEMENTED_ACTIONS, KEYBIND_GROUPS, KEYBIND_LABELS
from ..preferences import Preferences, default_project_dir


class PreferencesDialog(QDialog):
    def __init__(self, prefs: Preferences, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("dialog.preferences.title"))
        self.setMinimumWidth(420)
        self._reset_requested = False

        tabs = QTabWidget()

        # general
        general = QWidget()
        gform = QFormLayout(general)

        self.autosave_check = QCheckBox(tr("dialog.preferences.autosaveEnabled"))
        self.autosave_check.setChecked(prefs.autosave_enabled)
        gform.addRow(self.autosave_check)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(5, 3600)
        self.interval_spin.setValue(prefs.autosave_interval_sec)
        gform.addRow(tr("dialog.preferences.autosaveInterval"), self.interval_spin)

        dir_row = QWidget()
        dir_l = QHBoxLayout(dir_row)
        dir_l.setContentsMargins(0, 0, 0, 0)
        self.dir_edit = QLineEdit(prefs.default_project_dir or default_project_dir())
        browse_btn = QPushButton(tr("dialog.preferences.browse"))
        browse_btn.clicked.connect(self._browse_dir)
        dir_l.addWidget(self.dir_edit)
        dir_l.addWidget(browse_btn)
        gform.addRow(tr("dialog.preferences.projectFolder"), dir_row)

        bg_row = QWidget()
        bg_l = QHBoxLayout(bg_row)
        bg_l.setContentsMargins(0, 0, 0, 0)
        self.bg_edit = QLineEdit(prefs.default_scene_bg)
        self.bg_edit.setMaximumWidth(90)
        bg_pick = QPushButton(tr("dialog.preferences.colorPickerEllipsis"))
        bg_pick.setFixedWidth(28)
        bg_pick.clicked.connect(self._pick_bg)
        bg_l.addWidget(self.bg_edit)
        bg_l.addWidget(bg_pick)
        bg_l.addStretch(1)
        gform.addRow(tr("dialog.preferences.sceneBg"), bg_row)

        self.language_combo = QComboBox()
        for code, name in available_languages().items():
            self.language_combo.addItem(name, code)
        idx = self.language_combo.findData(prefs.language)
        if idx >= 0:
            self.language_combo.setCurrentIndex(idx)
        else:
            eng_idx = self.language_combo.findData("english")
            if eng_idx >= 0:
                self.language_combo.setCurrentIndex(eng_idx)
        gform.addRow(tr("dialog.preferences.language"), self.language_combo)

        ffmpeg_row = QWidget()
        ffmpeg_l = QHBoxLayout(ffmpeg_row)
        ffmpeg_l.setContentsMargins(0, 0, 0, 0)
        self.ffmpeg_edit = QLineEdit(prefs.ffmpeg_path)
        self.ffmpeg_edit.setPlaceholderText(tr("dialog.preferences.ffmpegPathPlaceholder"))
        ffmpeg_browse_btn = QPushButton(tr("dialog.preferences.browse"))
        ffmpeg_browse_btn.clicked.connect(self._browse_ffmpeg)
        ffmpeg_l.addWidget(self.ffmpeg_edit)
        ffmpeg_l.addWidget(ffmpeg_browse_btn)
        gform.addRow(tr("dialog.preferences.ffmpegPath"), ffmpeg_row)

        tabs.addTab(general, tr("dialog.preferences.tabGeneral"))

        # editing
        editing = QWidget()
        eform = QFormLayout(editing)
        self.snap_check = QCheckBox(tr("dialog.preferences.snapPlayhead"))
        self.snap_check.setChecked(prefs.snap_to_playhead)
        eform.addRow(self.snap_check)

        self.snap_clips_check = QCheckBox(
            tr("dialog.preferences.snapClips")
        )
        self.snap_clips_check.setChecked(prefs.snap_to_clips)
        eform.addRow(self.snap_clips_check)

        note = QLabel(tr("dialog.preferences.snapFramesNote"))
        note.setWordWrap(True)
        eform.addRow(note)

        self.recursive_nesting_check = QCheckBox(
            tr("dialog.preferences.recursiveNesting")
        )
        self.recursive_nesting_check.setChecked(prefs.allow_recursive_scene_nesting)
        self.recursive_nesting_check.toggled.connect(lambda on: self.max_depth_spin.setEnabled(on))
        eform.addRow(self.recursive_nesting_check)

        self.max_depth_spin = QSpinBox()
        self.max_depth_spin.setRange(1, 64)
        self.max_depth_spin.setValue(prefs.max_scene_nesting_depth)
        self.max_depth_spin.setEnabled(prefs.allow_recursive_scene_nesting)
        eform.addRow(tr("dialog.preferences.maxNestingDepth"), self.max_depth_spin)

        self.auto_apply_check = QCheckBox(
            tr("dialog.preferences.autoApplyLong")
        )
        self.auto_apply_check.setChecked(prefs.auto_apply_properties)
        eform.addRow(self.auto_apply_check)

        tabs.addTab(editing, tr("dialog.preferences.tabEditing"))

        # keybinds
        keybinds_page = QWidget()
        kv = QVBoxLayout(keybinds_page)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        kbody = QWidget()
        kform = QFormLayout(kbody)
        self._keybind_edits: dict = {}
        current_keybinds = prefs.keybinds or dict(DEFAULT_KEYBINDS)
        for group_name, action_names in KEYBIND_GROUPS:
            header = QLabel(f"<b>{group_name}</b>")
            kform.addRow(header)
            for action_name in action_names:
                edit = QKeySequenceEdit(QKeySequence(current_keybinds.get(action_name, DEFAULT_KEYBINDS[action_name])))
                edit.setMaximumSequenceLength(1)
                label = KEYBIND_LABELS.get(action_name, action_name)
                if action_name not in IMPLEMENTED_ACTIONS:
                    label += "  (not yet implemented)"
                self._keybind_edits[action_name] = edit
                kform.addRow(label, edit)
        scroll.setWidget(kbody)
        kv.addWidget(scroll)
        scroll.setToolTip(tr("dialog.preferences.hintKeybinds"))
        for _e in self._keybind_edits.values():
            _e.setToolTip(tr("dialog.preferences.hintKeybinds"))
        tabs.addTab(keybinds_page, tr("dialog.preferences.tabKeybinds"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        reset_btn = buttons.addButton(tr("dialog.preferences.resetAll"), QDialogButtonBox.ButtonRole.ResetRole)
        reset_btn.clicked.connect(self._on_reset)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, tr("dialog.preferences.defaultProjectFolder"), self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _browse_ffmpeg(self):
        start = self.ffmpeg_edit.text().strip() or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, tr("dialog.preferences.browseFfmpeg"), start
        )
        if path:
            self.ffmpeg_edit.setText(path)

    def _pick_bg(self):
        from PyQt6.QtGui import QColor
        c = QColorDialog.getColor(QColor(self.bg_edit.text() or "#204030"), self, tr("dialog.preferences.defaultSceneBackground"))
        if c.isValid():
            self.bg_edit.setText(c.name())

    def _on_reset(self):
        if QMessageBox.question(
            self, tr("dialog.preferences.resetTitle"), tr("dialog.preferences.resetConfirm")
        ) == QMessageBox.StandardButton.Yes:
            self._reset_requested = True
            self.accept()

    def was_reset(self) -> bool:
        return self._reset_requested

    def values(self) -> dict:
        keybinds = {}
        for action_name, edit in self._keybind_edits.items():
            seq = edit.keySequence()
            keybinds[action_name] = seq.toString() if not seq.isEmpty() else ""
        return {
            "autosave_enabled": self.autosave_check.isChecked(),
            "autosave_interval_sec": self.interval_spin.value(),
            "default_project_dir": self.dir_edit.text().strip() or default_project_dir(),
            "default_scene_bg": self.bg_edit.text().strip() or "#204030",
            "language": self.language_combo.currentData(),
            "snap_to_playhead": self.snap_check.isChecked(),
            "snap_to_clips": self.snap_clips_check.isChecked(),
            "allow_recursive_scene_nesting": self.recursive_nesting_check.isChecked(),
            "max_scene_nesting_depth": self.max_depth_spin.value(),
            "auto_apply_properties": self.auto_apply_check.isChecked(),
            "ffmpeg_path": self.ffmpeg_edit.text().strip(),
            "keybinds": keybinds,
        }