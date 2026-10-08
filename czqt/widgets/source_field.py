from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QStyle, QWidget

from .. import i18n
from ..asset_types import is_audio, is_video
from ..dialogs.source_picker_dialog import SourcePickerDialog
from ..imaging import load_pixmap
from ..model import Project

_THUMB_SIZE = (48, 36)


class SourceField(QWidget):
    changed = pyqtSignal()

    def __init__(
        self,
        project: Optional[Project],
        project_dir: Optional[str],
        filter_kind: Optional[str],
        current_uuid: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.project = project
        self.project_dir = project_dir
        self.filter_kind = filter_kind
        self.current_uuid = current_uuid or ""

        self.thumb_label = QLabel()
        self.thumb_label.setFixedSize(*_THUMB_SIZE)
        self.thumb_label.setStyleSheet("border: 1px solid #666; background: #111; color: #888;")
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.name_label = QLabel()
        self.name_label.setWordWrap(True)

        choose_btn = QPushButton(i18n.tr("sourceField.choose"))
        choose_btn.clicked.connect(self._choose)
        clear_btn = QPushButton(i18n.tr("sourceField.remove"))
        clear_btn.setFixedWidth(24)
        clear_btn.setToolTip(i18n.tr("sourceField.clear"))
        clear_btn.clicked.connect(self._clear)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.thumb_label)
        layout.addWidget(self.name_label, 1)
        layout.addWidget(choose_btn)
        layout.addWidget(clear_btn)

        self._refresh_display()

    def value(self) -> str:
        return self.current_uuid

    def set_project_dir(self, project_dir: Optional[str]):
        self.project_dir = project_dir
        self._refresh_display()

    def _refresh_display(self):
        if not self.current_uuid:
            self.thumb_label.setPixmap(QPixmap())
            self.thumb_label.setText(i18n.tr("sourceField.empty"))
            self.name_label.setText(i18n.tr("sourceField.none"))
            return
        name = self.project.sources.get(self.current_uuid, self.current_uuid) if self.project else self.current_uuid
        self.name_label.setText(name)
        pix = QPixmap()
        if self.project_dir and not is_audio(self.current_uuid):
            if is_video(self.current_uuid):
                from ..video import load_video_thumbnail
                p = load_video_thumbnail(os.path.join(self.project_dir, self.current_uuid))
            else:
                p = load_pixmap(os.path.join(self.project_dir, self.current_uuid))
            if p is not None and not p.isNull():
                pix = p.scaled(
                    _THUMB_SIZE[0], _THUMB_SIZE[1],
                    Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
                )
        if not pix.isNull():
            self.thumb_label.setPixmap(pix)
            self.thumb_label.setText("")
        elif is_audio(self.current_uuid):
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
            self.thumb_label.setPixmap(icon.pixmap(*_THUMB_SIZE))
        elif is_video(self.current_uuid):
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
            self.thumb_label.setPixmap(icon.pixmap(*_THUMB_SIZE))
        else:
            self.thumb_label.setPixmap(QPixmap())
            self.thumb_label.setText("?")

    def _choose(self):
        dlg = SourcePickerDialog(
            self.project, self.project_dir, self.filter_kind, allow_none=True,
            preselect=self.current_uuid, parent=self,
        )
        if dlg.exec():
            self.current_uuid = dlg.result_uuid() or ""
            self._refresh_display()
            self.changed.emit()

    def _clear(self):
        if self.current_uuid:
            self.current_uuid = ""
            self._refresh_display()
            self.changed.emit()