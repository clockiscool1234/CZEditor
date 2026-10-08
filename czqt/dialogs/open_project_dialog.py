from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..backend import BackendError, LocalBackend

_THUMB_SIZE = QSize(96, 72)


class OpenProjectDialog(QDialog):
    def __init__(self, backend: LocalBackend, parent=None, default_root: str = ""):
        super().__init__(parent)
        self.backend = backend
        self.setWindowTitle(i18n.tr("dialog.openProject.title"))
        self.setMinimumSize(560, 420)
        self._chosen_dir: Optional[str] = None

        self.root_edit = QLineEdit(default_root)
        browse_btn = QPushButton(i18n.tr("dialog.browse"))
        browse_btn.clicked.connect(self._browse_root)
        root_row = QWidget()
        root_layout = QHBoxLayout(root_row)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(QLabel(i18n.tr("dialog.openProject.labelLibrary")))
        root_layout.addWidget(self.root_edit)
        root_layout.addWidget(browse_btn)

        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setIconSize(_THUMB_SIZE)
        self.list_widget.setGridSize(QSize(140, 118))
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setWordWrap(True)
        self.list_widget.setSpacing(6)
        self.list_widget.itemDoubleClicked.connect(self._on_double_click)

        self._placeholder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)

        other_btn = QPushButton(i18n.tr("dialog.openProject.openOther"))
        other_btn.clicked.connect(self._open_other_folder)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Open).clicked.connect(self._accept_selection)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(root_row)
        layout.addWidget(QLabel(i18n.tr("dialog.openProject.labelProjects")))
        layout.addWidget(self.list_widget)
        layout.addWidget(other_btn)
        layout.addWidget(buttons)

        self.root_edit.editingFinished.connect(self._refresh)
        if default_root:
            self._refresh()

    def chosen_dir(self) -> Optional[str]:
        return self._chosen_dir

    def _browse_root(self):
        d = QFileDialog.getExistingDirectory(self, i18n.tr("dialog.openProject.chooseLibrary"))
        if d:
            self.root_edit.setText(d)
            self._refresh()

    def _refresh(self):
        root = self.root_edit.text().strip()
        self.list_widget.clear()
        if not root or not os.path.isdir(root):
            return
        try:
            names = self.backend.find_project_dirs(root)
        except BackendError as e:
            QMessageBox.warning(self, i18n.tr("dialog.openProject.title"), str(e))
            return
        for name in names:
            project_dir = os.path.join(root, name)
            item = QListWidgetItem(name)
            item.setData(1000, project_dir)
            item.setIcon(self._thumbnail_icon(project_dir))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            self.list_widget.addItem(item)

    def _thumbnail_icon(self, project_dir: str) -> QIcon:
        thumb_path = os.path.join(project_dir, "thumbnail.png")
        pix = QPixmap(thumb_path)
        if pix.isNull():
            return self._placeholder_icon
        pix = pix.scaled(
            _THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        return QIcon(pix)

    def _on_double_click(self, item: QListWidgetItem):
        self._chosen_dir = item.data(1000)
        self.accept()

    def _accept_selection(self):
        item = self.list_widget.currentItem()
        if not item:
            QMessageBox.information(self, i18n.tr("dialog.openProject.title"), i18n.tr("dialog.openProject.warnSelect"))
            return
        self._chosen_dir = item.data(1000)
        self.accept()

    def _open_other_folder(self):
        d = QFileDialog.getExistingDirectory(self, i18n.tr("dialog.openProject.chooseFolder"))
        if not d:
            return
        if not os.path.isfile(os.path.join(d, "project.json")):
            resp = QMessageBox.question(
                self,
                i18n.tr("dialog.openProject.title"),
                i18n.tr("dialog.openProject.warnNoProject"),
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
        self._chosen_dir = d
        self.accept()
