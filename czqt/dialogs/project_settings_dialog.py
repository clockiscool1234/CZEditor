from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
)

from .. import i18n
from ..model import Project


class ProjectSettingsDialog(QDialog):
    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("dialog.projectSettings.title"))
        self.setMinimumWidth(380)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(16, 8192)
        self.width_spin.setValue(project.width)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(16, 8192)
        self.height_spin.setValue(project.height)

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 240)
        self.fps_spin.setValue(project.fps)

        self.vbitrate_edit = QLineEdit(project.export_video_bitrate or "8000k")
        self.abitrate_edit = QLineEdit(project.export_audio_bitrate or "192k")
        self.channels_combo = QComboBox()
        self.channels_combo.addItems([i18n.tr("dialog.projectSettings.mono"), i18n.tr("dialog.projectSettings.stereo")])
        self.channels_combo.setCurrentIndex(0 if project.export_channels == 1 else 1)

        form = QFormLayout()
        form.addRow(QLabel(i18n.tr("dialog.projectSettings.labelVideo")))
        form.addRow(i18n.tr("dialog.projectSettings.labelWidth"), self.width_spin)
        form.addRow(i18n.tr("dialog.projectSettings.labelHeight"), self.height_spin)
        form.addRow(i18n.tr("dialog.projectSettings.labelFPS"), self.fps_spin)
        for _w in (self.width_spin, self.height_spin, self.fps_spin):
            _w.setToolTip(i18n.tr("dialog.projectSettings.hintVideo"))
        form.addRow(QLabel(i18n.tr("dialog.projectSettings.labelExport")))
        form.addRow(i18n.tr("dialog.projectSettings.labelVideoBitrate"), self.vbitrate_edit)
        form.addRow(i18n.tr("dialog.projectSettings.labelAudioBitrate"), self.abitrate_edit)
        form.addRow(i18n.tr("dialog.projectSettings.labelChannels"), self.channels_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_accept(self):
        if self.width_spin.value() < 16 or self.height_spin.value() < 16:
            QMessageBox.warning(self, i18n.tr("dialog.projectSettings.title"), i18n.tr("dialog.projectSettings.warnSmallRes"))
            return
        self.accept()

    def values(self) -> dict:
        return {
            "width": self.width_spin.value(),
            "height": self.height_spin.value(),
            "fps": self.fps_spin.value(),
            "export_video_bitrate": self.vbitrate_edit.text().strip() or "8000k",
            "export_audio_bitrate": self.abitrate_edit.text().strip() or "192k",
            "export_channels": 1 if self.channels_combo.currentIndex() == 0 else 2,
        }
