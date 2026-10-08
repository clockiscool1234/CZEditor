from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..model import DEFAULT_FPS, SCENE_H, SCENE_W

def _resolutions():
    return [
        (i18n.tr("dialog.newProject.resHD"), 1280, 720),
        (i18n.tr("dialog.newProject.resFHD"), 1920, 1080),
        ("854 x 480", 854, 480),
        (i18n.tr("dialog.newProject.custom"), None, None),
    ]


class NewProjectDialog(QDialog):
    def __init__(self, parent=None, default_location: str = ""):
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("dialog.newProject.title"))
        self.setMinimumWidth(380)

        self.name_edit = QLineEdit(i18n.tr("dialog.newProject.defaultName"))
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 240)
        self.fps_spin.setValue(DEFAULT_FPS)

        self.res_combo = QComboBox()
        self._res_list = _resolutions()
        for label, _, _ in self._res_list:
            self.res_combo.addItem(label)
        self.res_combo.currentIndexChanged.connect(self._on_res_changed)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(16, 8192)
        self.width_spin.setValue(SCENE_W)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(16, 8192)
        self.height_spin.setValue(SCENE_H)

        self.location_edit = QLineEdit(default_location)
        browse_btn = QPushButton(i18n.tr("dialog.newProject.browse"))
        browse_btn.clicked.connect(self._browse_location)
        loc_row = QWidget()
        loc_layout = QHBoxLayout(loc_row)
        loc_layout.setContentsMargins(0, 0, 0, 0)
        loc_layout.addWidget(self.location_edit)
        loc_layout.addWidget(browse_btn)

        form = QFormLayout()
        form.addRow(i18n.tr("dialog.newProject.labelName"), self.name_edit)
        form.addRow(i18n.tr("dialog.newProject.labelResolution"), self.res_combo)
        form.addRow(i18n.tr("dialog.newProject.labelWidth"), self.width_spin)
        form.addRow(i18n.tr("dialog.newProject.labelHeight"), self.height_spin)
        form.addRow(i18n.tr("dialog.newProject.labelFPS"), self.fps_spin)
        form.addRow(i18n.tr("dialog.newProject.labelLocation"), loc_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._on_res_changed(0)

    def _on_res_changed(self, index: int):
        _, w, h = self._res_list[index]
        custom = w is None
        self.width_spin.setEnabled(custom)
        self.height_spin.setEnabled(custom)
        if not custom:
            self.width_spin.setValue(w)
            self.height_spin.setValue(h)

    def _browse_location(self):
        d = QFileDialog.getExistingDirectory(self, i18n.tr("dialog.newProject.chooseFolder"))
        if d:
            self.location_edit.setText(d)

    def values(self) -> dict:
        return {
            "name": self.name_edit.text().strip() or i18n.tr("dialog.newProject.defaultName"),
            "width": self.width_spin.value(),
            "height": self.height_spin.value(),
            "fps": self.fps_spin.value(),
            "location": self.location_edit.text().strip(),
        }
