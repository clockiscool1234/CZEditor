from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from .. import thumbnails
from ..asset_types import is_audio, is_video
from ..imaging import load_pixmap
from ..model import Project

_THUMB_SIZE = QSize(72, 56)
_ROLE_UUID = Qt.ItemDataRole.UserRole
_ROLE_FOLDER = Qt.ItemDataRole.UserRole + 1
_NONE_SENTINEL = "\0none\0"


class SourcePickerDialog(QDialog):
    def __init__(
        self,
        project: Project,
        project_dir: Optional[str],
        filter_kind: Optional[str] = None,  # None | "image" | "audio" | "video"
        allow_none: bool = True,
        preselect: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.project = project
        self.project_dir = project_dir
        self.filter_kind = filter_kind
        self.allow_none = allow_none
        self.selected_uuid: Optional[str] = preselect or None
        self._nav_path: list[str] = []  # folder ids, root to current
        self._image_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self._audio_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
        self._video_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        self._folder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)

        label = {
            "image": i18n.tr("dialog.sourcePicker.filterImage"),
            "audio": i18n.tr("dialog.sourcePicker.filterAudio"),
            "video": i18n.tr("dialog.sourcePicker.filterVideo"),
        }.get(filter_kind, i18n.tr("dialog.sourcePicker.filterGeneric"))
        self.setWindowTitle(i18n.tr("dialog.sourcePicker.titleFmt").format(source=label))
        self.setMinimumSize(560, 440)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(i18n.tr("dialog.sourcePicker.searchPlaceholder"))
        self.search_edit.textChanged.connect(self._on_search_changed)

        self.nav_bar = QWidget()
        self.nav_layout = QHBoxLayout(self.nav_bar)
        self.nav_layout.setContentsMargins(0, 0, 0, 0)

        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setIconSize(_THUMB_SIZE)
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(40)
        self._thumb_timer.timeout.connect(self._apply_ready_thumbnails)
        thumbnails.store().ready.connect(self._thumb_timer.start)
        self.list_widget.setGridSize(QSize(110, 96))
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setWordWrap(True)
        self.list_widget.setSpacing(6)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(i18n.tr("dialog.sourcePicker.select"))
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.search_edit)
        layout.addWidget(self.nav_bar)
        layout.addWidget(self.list_widget, 1)
        layout.addWidget(buttons)

        self._render()

    def result_uuid(self) -> Optional[str]:
        return self.selected_uuid

    # render
    def _render_breadcrumb(self):
        while self.nav_layout.count():
            child = self.nav_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        if self._nav_path:
            back_btn = QPushButton(i18n.tr("dialog.sourcePicker.back"))
            back_btn.clicked.connect(self._go_back)
            self.nav_layout.addWidget(back_btn)
        root_btn = QPushButton(i18n.tr("dialog.sourcePicker.root"))
        root_btn.setFlat(True)
        root_btn.clicked.connect(self._go_root)
        self.nav_layout.addWidget(root_btn)
        for i, fid in enumerate(self._nav_path):
            self.nav_layout.addWidget(QLabel(">"))
            folder = self.project.folders.get(fid)
            name = folder.name if folder else "?"
            crumb_btn = QPushButton(name)
            crumb_btn.setFlat(True)
            idx = i
            crumb_btn.clicked.connect(lambda _checked=False, idx=idx: self._go_to(idx))
            self.nav_layout.addWidget(crumb_btn)
        self.nav_layout.addStretch(1)

    def _current_folder_id(self) -> Optional[str]:
        return self._nav_path[-1] if self._nav_path else None

    def _render(self):
        self._render_breadcrumb()
        self.list_widget.clear()
        fid = self._current_folder_id()

        if self.allow_none and not self._nav_path:
            none_item = QListWidgetItem(i18n.tr("dialog.sourcePicker.none"))
            none_item.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton))
            none_item.setData(_ROLE_UUID, _NONE_SENTINEL)
            none_item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            self.list_widget.addItem(none_item)
            if self.selected_uuid is None:
                none_item.setSelected(True)

        for folder in self.project.folders_in("source", fid):
            item = QListWidgetItem(folder.name)
            item.setIcon(self._folder_icon)
            item.setData(_ROLE_FOLDER, folder.id)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            self.list_widget.addItem(item)

        for uuid_ext, name in sorted(self.project.sources.items(), key=lambda kv: kv[1].lower()):
            if self.project.source_folders.get(uuid_ext) != fid:
                continue
            if not self._passes_filter(uuid_ext):
                continue
            self._add_source_item(uuid_ext, name)

        if self.list_widget.count() == 0:
            empty = QListWidgetItem(i18n.tr("dialog.sourcePicker.emptyFolder"))
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(empty)

    def _render_search(self, query: str):
        while self.nav_layout.count():
            child = self.nav_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self.nav_layout.addWidget(QLabel(i18n.tr("dialog.sourcePicker.searchResults")))
        self.nav_layout.addStretch(1)

        self.list_widget.clear()
        q = query.lower()
        matches = [
            (uuid_ext, name)
            for uuid_ext, name in self.project.sources.items()
            if q in name.lower() and self._passes_filter(uuid_ext)
        ]
        matches.sort(key=lambda kv: kv[1].lower())
        for uuid_ext, name in matches:
            self._add_source_item(uuid_ext, name)
        if not matches:
            empty = QListWidgetItem(i18n.tr("dialog.sourcePicker.noMatches"))
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(empty)

    def _passes_filter(self, uuid_ext: str) -> bool:
        if self.filter_kind is None:
            return True
        audio = is_audio(uuid_ext)
        video = is_video(uuid_ext)
        if self.filter_kind == "audio":
            return audio
        if self.filter_kind == "video":
            return video
        return not audio and not video

    def _add_source_item(self, uuid_ext: str, name: str):
        item = QListWidgetItem(name)
        item.setIcon(self._thumbnail_icon(uuid_ext))
        item.setData(_ROLE_UUID, uuid_ext)
        item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.list_widget.addItem(item)
        if uuid_ext == self.selected_uuid:
            item.setSelected(True)

    def _thumbnail_icon(self, uuid_ext: str) -> QIcon:
        if is_audio(uuid_ext):
            return self._audio_icon
        icon = thumbnails.store().source_icon(self.project_dir, uuid_ext, _THUMB_SIZE)
        if icon is not None:
            return icon
        return self._video_icon if is_video(uuid_ext) else self._image_icon

    def _apply_ready_thumbnails(self):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            uuid_ext = item.data(_ROLE_UUID)
            if uuid_ext and uuid_ext != _NONE_SENTINEL:
                item.setIcon(self._thumbnail_icon(uuid_ext))

    # nav
    def _go_root(self):
        self._nav_path = []
        self.search_edit.clear()
        self._render()

    def _go_back(self):
        if self._nav_path:
            self._nav_path.pop()
        self._render()

    def _go_to(self, idx: int):
        self._nav_path = self._nav_path[: idx + 1]
        self._render()

    def _on_search_changed(self, text: str):
        text = text.strip()
        if text:
            self._render_search(text)
        else:
            self._render()

    # selection
    def _on_item_clicked(self, item: QListWidgetItem):
        folder_id = item.data(_ROLE_FOLDER)
        if folder_id:
            return 
        uuid = item.data(_ROLE_UUID)
        if uuid is not None:
            self.selected_uuid = None if uuid == _NONE_SENTINEL else uuid

    def _on_item_double_clicked(self, item: QListWidgetItem):
        folder_id = item.data(_ROLE_FOLDER)
        if folder_id:
            self._nav_path.append(folder_id)
            self._render()
            return
        uuid = item.data(_ROLE_UUID)
        if uuid is not None:
            self.selected_uuid = None if uuid == _NONE_SENTINEL else uuid
            self.accept()

    def _on_accept(self):
        self.accept()