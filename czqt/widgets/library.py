from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..asset_types import is_audio, is_video
from ..imaging import load_pixmap
from .. import thumbnails
from ..dialogs.new_symbol_dialog import NewSymbolDialog
from ..dnd import MIME_ASSET, decode_asset_mime_list, encode_asset_mime
from ..model import MAIN_ID, Project
from ..project_controller import ProjectController

_DND_DEBUG = bool(os.environ.get("CZEDITOR_DND_DEBUG"))


def _dbg(*args):
    if _DND_DEBUG:
        print("[dnd]", *args, flush=True)


ROLE_KIND = Qt.ItemDataRole.UserRole
ROLE_ID = Qt.ItemDataRole.UserRole + 1

# kinds stored in ROLE_KIND
KIND_CATEGORY = "category"  # root-level Scenes/Symbols/Sources card
KIND_FOLDER = "folder"
KIND_CLASS = "class"
KIND_SCENE = "scene"
KIND_SYMBOL = "symbol"
KIND_SOURCE = "source"

MEDIA_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
              ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".aiff", ".aif",
              ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg", ".wmv", ".flv", ".ogv", ".3gp")

_ICON_RENDER_SIZE = QSize(140, 100)

_AUTOSCROLL_MARGIN = 40
_AUTOSCROLL_SPEED  = 12
_AUTOSCROLL_INTERVAL = 40


class _AssetListWidget(QListWidget):
    folder_drop = pyqtSignal(list, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scroll_dir = 0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(_AUTOSCROLL_INTERVAL)
        self._scroll_timer.timeout.connect(self._do_autoscroll)

    def _do_autoscroll(self):
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + self._scroll_dir * _AUTOSCROLL_SPEED)

    def _update_autoscroll(self, y: int):
        h = self.viewport().height()
        if y < _AUTOSCROLL_MARGIN:
            self._scroll_dir = -1
            if not self._scroll_timer.isActive():
                self._scroll_timer.start()
        elif y > h - _AUTOSCROLL_MARGIN:
            self._scroll_dir = 1
            if not self._scroll_timer.isActive():
                self._scroll_timer.start()
        else:
            self._scroll_dir = 0
            self._scroll_timer.stop()

    def dragLeaveEvent(self, event):
        self._scroll_timer.stop()
        self._scroll_dir = 0
        super().dragLeaveEvent(event)

    def mimeData(self, items):
        assets = []
        for item in items:
            kind = item.data(ROLE_KIND)
            obj_id = item.data(ROLE_ID)
            if kind in (KIND_SCENE, KIND_SYMBOL, KIND_SOURCE, "shape") and obj_id:
                assets.append((kind, obj_id))
        if assets:
            return encode_asset_mime(assets)
        return super().mimeData(items)

    def startDrag(self, supported_actions):
        # Always use CopyAction so Qt doesn't remove our items after an external drop.
        super().startDrag(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event: QDragEnterEvent):
        _dbg("LIST.dragEnter", "mime=", event.mimeData().formats())
        if event.mimeData().hasFormat(MIME_ASSET):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        pos = event.position().toPoint()
        self._update_autoscroll(pos.y())
        target = self._folder_target(pos)
        _dbg("LIST.dragMove", "pos=", pos, "target=", target)
        if target is not None:
            event.setDropAction(Qt.DropAction.MoveAction)
        else:
            event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept(self.visualItemRect(self.itemAt(pos))
                     if self.itemAt(pos) else QRect(pos, QSize(1, 1)))

    def dropEvent(self, event: QDropEvent):
        self._scroll_timer.stop()
        self._scroll_dir = 0
        folder_id = self._folder_target(event.position().toPoint())
        assets = decode_asset_mime_list(event.mimeData()) if folder_id is not None else []
        _dbg("LIST.drop", "folder=", folder_id, "assets=", assets)
        if not assets:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self.folder_drop.emit(assets, folder_id)

    def _folder_target(self, pos) -> Optional[str]:
        item = self.itemAt(pos)
        if item is not None and item.data(ROLE_KIND) == KIND_FOLDER:
            return item.data(ROLE_ID)
        return None


class _AssetTreeWidget(QTreeWidget):
    folder_drop = pyqtSignal(list, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scroll_dir = 0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(_AUTOSCROLL_INTERVAL)
        self._scroll_timer.timeout.connect(self._do_autoscroll)

    def _do_autoscroll(self):
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + self._scroll_dir * _AUTOSCROLL_SPEED)

    def _update_autoscroll(self, y: int):
        h = self.viewport().height()
        if y < _AUTOSCROLL_MARGIN:
            self._scroll_dir = -1
            if not self._scroll_timer.isActive():
                self._scroll_timer.start()
        elif y > h - _AUTOSCROLL_MARGIN:
            self._scroll_dir = 1
            if not self._scroll_timer.isActive():
                self._scroll_timer.start()
        else:
            self._scroll_dir = 0
            self._scroll_timer.stop()

    def dragLeaveEvent(self, event):
        self._scroll_timer.stop()
        self._scroll_dir = 0
        super().dragLeaveEvent(event)

    def mimeData(self, items):
        assets = []
        seen = set()
        for item in items:
            if id(item) in seen:
                continue
            seen.add(id(item))
            kind = item.data(0, ROLE_KIND)
            obj_id = item.data(0, ROLE_ID)
            if kind in (KIND_SCENE, KIND_SYMBOL, KIND_SOURCE) and obj_id:
                assets.append((kind, obj_id))
        if assets:
            return encode_asset_mime(assets)
        return super().mimeData(items)

    def startDrag(self, supported_actions):
        super().startDrag(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event: QDragEnterEvent):
        _dbg("TREE.dragEnter", "mime=", event.mimeData().formats())
        if event.mimeData().hasFormat(MIME_ASSET):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        pos = event.position().toPoint()
        self._update_autoscroll(pos.y())
        target = self._folder_target(pos)
        _dbg("TREE.dragMove", "pos=", pos, "target=", target)
        if target is not None:
            event.setDropAction(Qt.DropAction.MoveAction)
        else:
            event.setDropAction(Qt.DropAction.IgnoreAction)
        item = self.itemAt(pos)
        event.accept(self.visualItemRect(item) if item
                     else QRect(pos, QSize(1, 1)))

    def dropEvent(self, event: QDropEvent):
        self._scroll_timer.stop()
        self._scroll_dir = 0
        folder_id = self._folder_target(event.position().toPoint())
        assets = decode_asset_mime_list(event.mimeData()) if folder_id is not None else []
        _dbg("TREE.drop", "folder=", folder_id, "assets=", assets)
        if not assets:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self.folder_drop.emit(assets, folder_id)

    def _folder_target(self, pos) -> Optional[str]:
        item = self.itemAt(pos)
        if item is not None and item.data(0, ROLE_KIND) == KIND_FOLDER:
            return item.data(0, ROLE_ID)
        return None


class LibraryPanel(QWidget):
    scene_activated = pyqtSignal(str)
    object_selected = pyqtSignal(str, str)
    project_mutated = pyqtSignal()
    open_project_requested = pyqtSignal(str)

    def __init__(self, controller: ProjectController, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.controller = controller
        self.project: Optional[Project] = None
        self._nav_category: Optional[str] = None
        self._nav_path: list[tuple[str, str]] = []
        self._view_mode = "icon"
        self.setAcceptDrops(True)

        style = self.style()
        self._thumb_signature: Optional[str] = None
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(40)
        self._thumb_timer.timeout.connect(self._apply_ready_thumbnails)
        thumbnails.store().ready.connect(self._on_thumbnails_ready)
        self._folder_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self._generic_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self._audio_icon = style.standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
        self._video_icon = style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        self._up_icon = style.standardIcon(QStyle.StandardPixmap.SP_ArrowBack)

        # toolbar: import + view-mode toggle + icon-size slider
        import_btn = QPushButton(i18n.tr("library.importButton"))
        import_btn.clicked.connect(self._import_source)
        self.icon_view_btn = QPushButton(i18n.tr("library.viewIcons"))
        self.icon_view_btn.setCheckable(True)
        self.icon_view_btn.setChecked(True)
        self.icon_view_btn.clicked.connect(lambda: self._set_view_mode("icon"))
        self.detail_view_btn = QPushButton(i18n.tr("library.viewDetails"))
        self.detail_view_btn.setCheckable(True)
        self.detail_view_btn.clicked.connect(lambda: self._set_view_mode("detail"))
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(40, 140)
        self.size_slider.setValue(72)
        self.size_slider.setFixedWidth(80)
        self.size_slider.valueChanged.connect(self._apply_icon_size)
        toolbar_row = QHBoxLayout()
        toolbar_row.addWidget(import_btn)
        toolbar_row.addStretch(1)
        toolbar_row.addWidget(self.icon_view_btn)
        toolbar_row.addWidget(self.detail_view_btn)
        toolbar_row.addWidget(self.size_slider)

        # breadcrumb (Back button + clickable path), inside this panel
        self.back_btn = QPushButton()
        self.back_btn.setIcon(self._up_icon)
        self.back_btn.setFixedWidth(28)
        self.back_btn.setToolTip(i18n.tr("library.back"))
        self.back_btn.clicked.connect(self._go_back)
        self.crumb_bar = QWidget()
        self.crumb_layout = QHBoxLayout(self.crumb_bar)
        self.crumb_layout.setContentsMargins(0, 0, 0, 0)
        crumb_row = QHBoxLayout()
        crumb_row.addWidget(self.back_btn)
        crumb_row.addWidget(self.crumb_bar, 1)

        # search + sort (unchanged from before)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(i18n.tr("library.search"))
        self.search_edit.textChanged.connect(lambda _t: self._render())
        self.sort_combo = QComboBox()
        self.sort_combo.addItems([i18n.tr("library.sortAZ"), i18n.tr("library.sortZA")])
        self.sort_combo.currentIndexChanged.connect(self._render)
        search_row = QHBoxLayout()
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.sort_combo)

        # icon view
        self.icon_list = _AssetListWidget()
        self.icon_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.icon_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.icon_list.setMovement(QListWidget.Movement.Static)
        self.icon_list.setWordWrap(True)
        self.icon_list.setSpacing(6)
        self.icon_list.setDragEnabled(True)
        self.icon_list.setAcceptDrops(True)
        self.icon_list.viewport().setAcceptDrops(True)
        self.icon_list.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.icon_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.icon_list.setDropIndicatorShown(True)
        self.icon_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.icon_list.itemClicked.connect(self._on_item_clicked)
        self.icon_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.icon_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.icon_list.customContextMenuRequested.connect(lambda pos: self._on_context_menu(self.icon_list, pos))
        self.icon_list.folder_drop.connect(self._on_folder_drop)

        self.detail_tree = _AssetTreeWidget()
        self.detail_tree.setHeaderLabels([i18n.tr("library.headerName"), i18n.tr("library.headerType")])
        self.detail_tree.setRootIsDecorated(False)
        self.detail_tree.setDragEnabled(True)
        self.detail_tree.setAcceptDrops(True)
        self.detail_tree.viewport().setAcceptDrops(True)
        self.detail_tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.detail_tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.detail_tree.setDropIndicatorShown(True)
        self.detail_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.detail_tree.itemClicked.connect(self._on_item_clicked)
        self.detail_tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.detail_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.detail_tree.customContextMenuRequested.connect(lambda pos: self._on_context_menu(self.detail_tree, pos))
        self.detail_tree.folder_drop.connect(self._on_folder_drop)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.icon_list)
        self.stack.addWidget(self.detail_tree)

        library_page = QWidget()
        lib_layout = QVBoxLayout(library_page)
        lib_layout.setContentsMargins(4, 4, 4, 4)
        lib_layout.addLayout(toolbar_row)
        lib_layout.addLayout(crumb_row)
        lib_layout.addLayout(search_row)
        lib_layout.addWidget(self.stack, 1)

        shapes_page = QWidget()
        shapes_layout = QVBoxLayout(shapes_page)
        shapes_list = _AssetListWidget()
        shapes_list.setViewMode(QListWidget.ViewMode.IconMode)
        shapes_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        shapes_list.setMovement(QListWidget.Movement.Static)
        shapes_list.setSpacing(6)
        shapes_list.setDragEnabled(True)
        shapes_list.setIconSize(QSize(64, 64))
        shapes_list.setGridSize(QSize(84, 84))
        
        from PyQt6.QtGui import QColor, QPainter, QBrush, QPen, QPolygonF
        from PyQt6.QtCore import QPointF, QRectF
        for shape, label in [("rect", "Rectangle"), ("ellipse", "Ellipse"), ("triangle", "Triangle"), ("text", "Text")]:
            item = QListWidgetItem(label)
            item.setData(ROLE_KIND, "shape")
            item.setData(ROLE_ID, shape)
            
            img = QPixmap(64, 64)
            img.fill(Qt.GlobalColor.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(QBrush(QColor("#4488ff")))
            p.setPen(QPen(QColor("#ffffff"), 2))
            rect = QRectF(8, 8, 48, 48)
            if shape == "rect":
                p.drawRect(rect)
            elif shape == "ellipse":
                p.drawEllipse(rect)
            elif shape == "triangle":
                pts = QPolygonF([QPointF(32, 8), QPointF(56, 56), QPointF(8, 56)])
                p.drawPolygon(pts)
            elif shape == "text":
                font = p.font()
                font.setPixelSize(36)
                font.setBold(True)
                p.setFont(font)
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "T")
            p.end()
            
            item.setIcon(QIcon(img))
            shapes_list.addItem(item)
            
        shapes_layout.addWidget(shapes_list)

        self.tabs = QTabWidget()
        self.tabs.addTab(library_page, i18n.tr("library.tabLibrary"))
        self.tabs.addTab(shapes_page, i18n.tr("library.tabShapes"))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)

        self._apply_icon_size(self.size_slider.value())

    # state
    def set_project(self, project: Project):
        self.project = project
        self._nav_category = None
        self._nav_path = []
        self.refresh()

    def refresh(self):
        self._validate_nav()
        self._render()

    def _validate_nav(self):
        if not self.project or self._nav_category is None:
            return
        clean: list[tuple[str, str]] = []
        for kind, ident in self._nav_path:
            if kind == "folder" and ident not in self.project.folders:
                break
            if kind == "class" and ident not in self.project.classes:
                break
            clean.append((kind, ident))
        self._nav_path = clean

    # view mode
    def _set_view_mode(self, mode: str):
        self._view_mode = mode
        self.icon_view_btn.setChecked(mode == "icon")
        self.detail_view_btn.setChecked(mode == "detail")
        self.size_slider.setVisible(mode == "icon")
        self.stack.setCurrentWidget(self.icon_list if mode == "icon" else self.detail_tree)

    def _apply_icon_size(self, px: int):
        self.icon_list.setIconSize(QSize(px, int(px * 0.75)))
        self.icon_list.setGridSize(QSize(px + 40, px + 46))

    # breadcrumb
    def _rebuild_breadcrumb(self):
        while self.crumb_layout.count():
            child = self.crumb_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self.back_btn.setEnabled(self._nav_category is not None)

        root_btn = QPushButton(i18n.tr("library.root"))
        root_btn.setFlat(True)
        root_btn.clicked.connect(self._go_root)
        self.crumb_layout.addWidget(root_btn)

        if self._nav_category is not None:
            self.crumb_layout.addWidget(QLabel(">"))
            cat_btn = QPushButton(_CATEGORY_LABELS[self._nav_category])
            cat_btn.setFlat(True)
            cat_btn.clicked.connect(self._go_category_root)
            self.crumb_layout.addWidget(cat_btn)

        for i, (kind, ident) in enumerate(self._nav_path):
            self.crumb_layout.addWidget(QLabel(">"))
            name = ident if kind == "class" else (self.project.folders.get(ident).name if self.project.folders.get(ident) else "?")
            btn = QPushButton(name)
            btn.setFlat(True)
            idx = i
            btn.clicked.connect(lambda _checked=False, idx=idx: self._go_to(idx))
            self.crumb_layout.addWidget(btn)
        self.crumb_layout.addStretch(1)

    def _go_root(self):
        self._nav_category = None
        self._nav_path = []
        self._render()

    def _go_category_root(self):
        self._nav_path = []
        self._render()

    def _go_to(self, idx: int):
        self._nav_path = self._nav_path[: idx + 1]
        self._render()

    def _go_back(self):
        if self._nav_path:
            self._nav_path.pop()
        else:
            self._nav_category = None
        self._render()

    # entries
    def _current_folder_id(self) -> Optional[str]:
        for kind, ident in reversed(self._nav_path):
            if kind == "folder":
                return ident
        return None

    def _current_class(self) -> Optional[str]:
        if self._nav_path and self._nav_path[-1][0] == "class":
            return self._nav_path[-1][1]
        return None

    def _entries(self) -> list[dict]:
        p = self.project
        rev = self.sort_combo.currentIndex() == 1
        out: list[dict] = []

        if self._nav_category is None:
            for cat in ("scene", "symbol", "source"):
                out.append({"type": "category", "kind": KIND_CATEGORY, "id": cat, "label": _CATEGORY_LABELS[cat]})
            return out

        cat = self._nav_category
        cls = self._current_class()
        fid = self._current_folder_id()

        if cls is None:
            for folder in p.folders_in(cat, fid):
                out.append({"type": "folder", "kind": KIND_FOLDER, "id": folder.id, "label": folder.name})

        # the Main Timeline is the project root not a library asset
        if cat == "scene":
            for sid, scene in sorted(p.scenes.items(), key=lambda kv: kv[1].name.lower(), reverse=rev):
                if sid == MAIN_ID or (scene.folder_id or None) != fid:
                    continue
                out.append({"type": "leaf", "kind": KIND_SCENE, "id": sid, "label": scene.name})

        elif cat == "symbol":
            if cls is not None:
                for sym_id, sym in sorted(p.symbol_defs.items(), key=lambda kv: kv[1].name.lower(), reverse=rev):
                    if not sym.folder_id and sym.elem_class == cls:
                        out.append({"type": "leaf", "kind": KIND_SYMBOL, "id": sym_id, "label": sym.name})
            elif fid is not None:
                for sym_id, sym in sorted(p.symbol_defs.items(), key=lambda kv: kv[1].name.lower(), reverse=rev):
                    if sym.folder_id == fid:
                        out.append({"type": "leaf", "kind": KIND_SYMBOL, "id": sym_id, "label": sym.name})
            else:
                by_class: dict[str, int] = {}
                for sym in p.symbol_defs.values():
                    if not sym.folder_id:
                        by_class[sym.elem_class or "generic"] = by_class.get(sym.elem_class or "generic", 0) + 1
                for c in sorted(set(p.classes) | set(by_class.keys()), key=str.lower, reverse=rev):
                    out.append({"type": "class", "kind": KIND_CLASS, "id": c, "label": c, "count": by_class.get(c, 0)})

        elif cat == "source":
            for uuid_ext, name in sorted(p.sources.items(), key=lambda kv: kv[1].lower(), reverse=rev):
                if (p.source_folders.get(uuid_ext) or None) != fid:
                    continue
                out.append({"type": "leaf", "kind": KIND_SOURCE, "id": uuid_ext, "label": name})

        return out

    def _search_entries(self, query: str) -> list[dict]:
        p = self.project
        q = query.lower()
        out: list[dict] = []
        for sid, scene in p.scenes.items():
            if sid == MAIN_ID:
                continue
            if q in (scene.name or "").lower():
                out.append({"type": "leaf", "kind": KIND_SCENE, "id": sid, "label": scene.name})
        for sym_id, sym in p.symbol_defs.items():
            if q in sym.name.lower():
                out.append({"type": "leaf", "kind": KIND_SYMBOL, "id": sym_id, "label": sym.name})
        for uuid_ext, name in p.sources.items():
            if q in name.lower():
                out.append({"type": "leaf", "kind": KIND_SOURCE, "id": uuid_ext, "label": name})
        out.sort(key=lambda e: e["label"].lower())
        return out

    # render
    def _render(self):
        if not self.project:
            self.icon_list.clear()
            self.detail_tree.clear()
            return
        self._rebuild_breadcrumb()
        self._thumb_signature = None
        query = self.search_edit.text().strip()
        entries = self._search_entries(query) if query else self._entries()

        self.icon_list.clear()
        self.detail_tree.clear()
        for entry in entries:
            icon = self._icon_for(entry)
            label = entry["label"]
            if entry["type"] == "class" and entry.get("count"):
                label = f'{label} ({entry["count"]})'

            is_folder = entry["kind"] == KIND_FOLDER

            item = QListWidgetItem(label)
            item.setIcon(icon)
            item.setData(ROLE_KIND, entry["kind"])
            item.setData(ROLE_ID, entry["id"])
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            if is_folder:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDropEnabled)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            self.icon_list.addItem(item)

            titem = QTreeWidgetItem([label, _TYPE_LABELS.get(entry["kind"], entry["kind"])])
            titem.setIcon(0, icon)
            titem.setData(0, ROLE_KIND, entry["kind"])
            titem.setData(0, ROLE_ID, entry["id"])
            if is_folder:
                titem.setFlags(titem.flags() | Qt.ItemFlag.ItemIsDropEnabled)
            else:
                titem.setFlags(titem.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            self.detail_tree.addTopLevelItem(titem)

        if not entries:
            empty_text = "No matches." if query else "This folder is empty."
            empty = QListWidgetItem(empty_text)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.icon_list.addItem(empty)

    def _icon_for(self, entry: dict) -> QIcon:
        kind = entry["kind"]
        if kind in (KIND_FOLDER, KIND_CATEGORY, KIND_CLASS):
            return self._folder_icon
        if kind == KIND_SOURCE:
            return self._thumbnail_or(entry["id"], self._generic_icon)
        if kind == KIND_SYMBOL:
            sym = self.project.symbol_defs.get(entry["id"])
            uuid_ext = (sym.active_image or sym.inactive_image) if sym else ""
            return self._thumbnail_or(uuid_ext, self._generic_icon) if uuid_ext else self._generic_icon
        if kind == KIND_SCENE:
            return self._scene_thumbnail(entry["id"]) or self._generic_icon
        return self._generic_icon

    def _thumbnail_or(self, uuid_ext: str, fallback: QIcon) -> QIcon:
        if not uuid_ext:
            return fallback
        if is_audio(uuid_ext):
            return self._audio_icon
        icon = thumbnails.store().source_icon(self.controller.project_dir, uuid_ext, _ICON_RENDER_SIZE)
        if icon is not None:
            return icon
        return self._video_icon if is_video(uuid_ext) else fallback

    def _scene_thumbnail(self, scene_id: str) -> Optional[QIcon]:
        project_dir = self.controller.project_dir
        if not project_dir or not self.project:
            return None
        if self._thumb_signature is None:
            self._thumb_signature = thumbnails.project_signature(self.project)
        return thumbnails.store().scene_icon(
            self.project, project_dir, scene_id, self._thumb_signature, _ICON_RENDER_SIZE)

    def _on_thumbnails_ready(self):
        if not self._thumb_timer.isActive():
            self._thumb_timer.start()

    def _apply_ready_thumbnails(self):
        if not self.project:
            return
        for i in range(self.icon_list.count()):
            item = self.icon_list.item(i)
            kind, obj_id = item.data(ROLE_KIND), item.data(ROLE_ID)
            if kind in (KIND_SCENE, KIND_SYMBOL, KIND_SOURCE) and obj_id:
                item.setIcon(self._icon_for({"kind": kind, "id": obj_id}))
        for i in range(self.detail_tree.topLevelItemCount()):
            item = self.detail_tree.topLevelItem(i)
            kind, obj_id = item.data(0, ROLE_KIND), item.data(0, ROLE_ID)
            if kind in (KIND_SCENE, KIND_SYMBOL, KIND_SOURCE) and obj_id:
                item.setIcon(0, self._icon_for({"kind": kind, "id": obj_id}))

    # selection / nav
    def _entry_at(self, widget, pos_or_item):
        if isinstance(pos_or_item, (QListWidgetItem, QTreeWidgetItem)):
            item = pos_or_item
        else:
            item = widget.itemAt(pos_or_item)
        if item is None:
            return None, None
        col = 0
        kind = item.data(ROLE_KIND) if isinstance(item, QListWidgetItem) else item.data(col, ROLE_KIND)
        obj_id = item.data(ROLE_ID) if isinstance(item, QListWidgetItem) else item.data(col, ROLE_ID)
        return kind, obj_id

    def _selected_entries(self, widget) -> list:
        out = []
        for item in widget.selectedItems():
            kind, obj_id = self._entry_at(widget, item)
            if kind in (KIND_SCENE, KIND_SYMBOL, KIND_SOURCE) and obj_id:
                out.append((kind, obj_id))
        return out

    def _on_item_clicked(self, item):
        kind, obj_id = self._entry_at(None, item)
        if kind in (KIND_SCENE, KIND_SYMBOL, KIND_SOURCE) and obj_id:
            self.object_selected.emit(kind, obj_id)

    def _on_item_double_clicked(self, item):
        kind, obj_id = self._entry_at(None, item)
        if self.search_edit.text().strip():
            if kind == KIND_SCENE and obj_id:
                self.scene_activated.emit(obj_id)
            return
        if kind == KIND_CATEGORY:
            self._nav_category = obj_id
            self._nav_path = []
            self._render()
        elif kind == KIND_FOLDER:
            self._nav_path.append(("folder", obj_id))
            self._render()
        elif kind == KIND_CLASS:
            self._nav_path.append(("class", obj_id))
            self._render()
        elif kind == KIND_SCENE and obj_id:
            self.scene_activated.emit(obj_id)

    # mutate + notify
    def _after_mutation(self):
        self.refresh()
        self.project_mutated.emit()

    # context menu
    def _on_context_menu(self, widget, pos):
        if not self.project:
            return
        kind, obj_id = self._entry_at(widget, pos)
        menu = QMenu(self)
        cur_cat = self._nav_category

        selected = self._selected_entries(widget)
        if len(selected) > 1 and kind in (KIND_SCENE, KIND_SYMBOL, KIND_SOURCE) and (kind, obj_id) in selected:
            self._build_multi_select_menu(menu, selected)
            if not menu.isEmpty():
                menu.exec(widget.viewport().mapToGlobal(pos))
            return

        if kind is None:
            if cur_cat is None:
                self._add_import_action(menu)
                menu.addAction(i18n.tr("library.menu.newScene"), self._new_scene)
                menu.addAction(i18n.tr("library.menu.newSymbolClass"), self._new_class)
                menu.addSeparator()
                new_folder_menu = menu.addMenu(i18n.tr("library.menu.newFolderSub"))
                new_folder_menu.addAction(i18n.tr("library.menu.sourceFolder"), lambda: self._new_folder("source", None))
                new_folder_menu.addAction(i18n.tr("library.menu.sceneFolder"), lambda: self._new_folder("scene", None))
                new_folder_menu.addAction(i18n.tr("library.menu.symbolFolder"), lambda: self._new_folder("symbol", None))
            else:
                self._add_category_new_actions(menu, cur_cat)
        elif kind == KIND_CATEGORY:
            self._add_category_new_actions(menu, obj_id)
        elif kind == KIND_SOURCE:
            self._add_import_action(menu)
            menu.addAction(i18n.tr("library.menu.newFolder"), lambda: self._new_folder("source", self._current_folder_id()))
            menu.addSeparator()
            if is_video(obj_id):
                menu.addAction(i18n.tr("library.menu.insertVideo"), lambda: self.controller.main_window.timeline_panel.insert_asset_at_playhead("video", obj_id))
            elif is_audio(obj_id):
                menu.addAction(i18n.tr("library.menu.insertSound"), lambda: self.controller.main_window.timeline_panel.insert_asset_at_playhead("sound", obj_id))
            else:
                menu.addAction(i18n.tr("library.menu.insertImage"), lambda: self.controller.main_window.timeline_panel.insert_asset_at_playhead("image", obj_id))
            menu.addSeparator()
            menu.addAction(i18n.tr("library.menu.rename"), lambda: self._rename_source(obj_id))
            menu.addAction(i18n.tr("library.menu.replaceFile"), lambda: self._replace_source(obj_id))
            self._add_move_to_folder_menu(menu, "source", obj_id)
            menu.addSeparator()
            menu.addAction(i18n.tr("library.menu.delete"), lambda: self._delete_asset("source", obj_id))
        elif kind == KIND_SCENE:
            menu.addAction(i18n.tr("library.menu.newScene"), self._new_scene)
            menu.addAction(i18n.tr("library.menu.newFolder"), lambda: self._new_folder("scene", self._current_folder_id()))
            menu.addAction(i18n.tr("library.menu.importScene"), self._import_scene)
            menu.addSeparator()
            menu.addAction(i18n.tr("library.menu.exportScene"), lambda: self._export_scene(obj_id))
            if obj_id != MAIN_ID:
                menu.addSeparator()
                menu.addAction(i18n.tr("library.menu.renameScene"), lambda: self._rename_scene(obj_id))
                self._add_move_to_folder_menu(menu, "scene", obj_id)
                menu.addSeparator()
                menu.addAction(i18n.tr("library.menu.deleteScene"), lambda: self._delete_asset("scene", obj_id))
        elif kind == KIND_CLASS:
            menu.addAction(i18n.tr("library.menu.newSymbol"), lambda: self._new_symbol(obj_id))
            menu.addAction(i18n.tr("library.menu.newClass"), self._new_class)
            menu.addAction(i18n.tr("library.menu.newFolder"), lambda: self._new_folder("symbol", None))
            menu.addAction(i18n.tr("library.menu.importSymbol"), self._import_symbol)
            menu.addSeparator()
            menu.addAction(i18n.tr("library.menu.deleteClass"), lambda: self._delete_class(obj_id))
        elif kind == KIND_SYMBOL:
            sym = self.project.symbol_defs.get(obj_id)
            cls = sym.elem_class if sym else None
            menu.addAction(i18n.tr("library.menu.newSymbol"), lambda: self._new_symbol(cls))
            menu.addAction(i18n.tr("library.menu.newFolder"), lambda: self._new_folder("symbol", self._current_folder_id()))
            menu.addAction(i18n.tr("library.menu.importSymbol"), self._import_symbol)
            self._add_move_to_folder_menu(menu, "symbol", obj_id)
            menu.addSeparator()
            menu.addAction(i18n.tr("library.menu.exportSymbol"), lambda: self._export_symbol(obj_id))
            menu.addSeparator()
            menu.addAction(i18n.tr("library.menu.deleteSymbol"), lambda: self._delete_asset("symbol", obj_id))
        elif kind == KIND_FOLDER:
            folder = self.project.folders.get(obj_id)
            if folder:
                menu.addAction(i18n.tr("library.menu.newFolder"), lambda: self._new_folder(folder.category, obj_id))
                menu.addSeparator()
                menu.addAction(i18n.tr("library.menu.renameFolder"), lambda: self._rename_folder(obj_id))
                self._add_move_folder_menu(menu, obj_id)
                menu.addSeparator()
                menu.addAction(i18n.tr("library.menu.deleteFolder"), lambda: self._delete_folder(obj_id))

        if not menu.isEmpty():
            menu.exec(widget.viewport().mapToGlobal(pos))

    def _add_category_new_actions(self, menu: QMenu, category: str):
        fid = self._current_folder_id()
        if category == "scene":
            menu.addAction(i18n.tr("library.menu.newScene"), self._new_scene)
            menu.addAction(i18n.tr("library.menu.newFolder"), lambda: self._new_folder("scene", fid))
            menu.addAction(i18n.tr("library.menu.importScene"), self._import_scene)
        elif category == "symbol":
            menu.addAction(i18n.tr("library.menu.newSymbolClass"), self._new_class)
            if self.project.classes:
                menu.addAction(i18n.tr("library.menu.newSymbolEllipsis"), lambda: self._new_symbol())
            menu.addAction(i18n.tr("library.menu.newFolder"), lambda: self._new_folder("symbol", fid))
            menu.addAction(i18n.tr("library.menu.importSymbol"), self._import_symbol)
        elif category == "source":
            self._add_import_action(menu)
            menu.addAction(i18n.tr("library.menu.newFolder"), lambda: self._new_folder("source", fid))

    def _add_import_action(self, menu: QMenu):
        menu.addAction(i18n.tr("library.menu.importSource"), self._import_source)

    def _build_multi_select_menu(self, menu: QMenu, selected: list):
        categories = {kind for kind, _ in selected}
        if len(categories) == 1:
            category = next(iter(categories))
            ids = [obj_id for _, obj_id in selected if not (category == KIND_SCENE and obj_id == MAIN_ID)]
            if ids:
                self._add_move_to_folder_menu(menu, category, ids, label=i18n.tr("library.menu.moveNToFolder").format(count=len(ids)))
                menu.addSeparator()
        deletable = [(k, i) for k, i in selected if not (k == KIND_SCENE and i == MAIN_ID)]
        if deletable:
            menu.addAction(i18n.tr("library.menu.deleteN").format(count=len(deletable)), lambda: self._delete_assets(deletable))

    def _add_move_to_folder_menu(self, menu: QMenu, category: str, real_id, label: str = None):
        label = label or i18n.tr("library.menu.moveToFolder")
        ids = real_id if isinstance(real_id, list) else [real_id]
        if not ids:
            return
        current = self.controller.item_folder(category, ids[0]) if len(ids) == 1 else None
        sub = menu.addMenu(label)
        root_action = sub.addAction(i18n.tr("library.menu.root"))
        root_action.setCheckable(True)
        root_action.setChecked(len(ids) == 1 and not current)
        root_action.triggered.connect(lambda: self._move_items(category, ids, None))
        flat = self._flatten_folders(category)
        if flat:
            sub.addSeparator()
        for folder, depth in flat:
            action = sub.addAction("\u00a0\u00a0" * depth + folder.name)
            action.setCheckable(True)
            action.setChecked(len(ids) == 1 and current == folder.id)
            action.triggered.connect(
                lambda _checked=False, fid=folder.id: self._move_items(category, ids, fid)
            )
        sub.addSeparator()
        sub.addAction(i18n.tr("library.menu.newFolder"), lambda: self._new_folder(category, None))

    def _move_items(self, category: str, real_ids: list, folder_id: Optional[str]):
        for real_id in real_ids:
            self.controller.move_item_to_folder(category, real_id, folder_id)
        self._after_mutation()

    def _on_folder_drop(self, assets: list, folder_id: str):
        if not assets or not folder_id:
            return
        for kind, obj_id in assets:
            if kind in (KIND_SCENE, KIND_SYMBOL, KIND_SOURCE):
                self.controller.move_item_to_folder(kind, obj_id, folder_id)
        self._after_mutation()

    def _move_item(self, category: str, real_id: str, folder_id: Optional[str]):
        self.controller.move_item_to_folder(category, real_id, folder_id)
        self._after_mutation()

    def _add_move_folder_menu(self, menu: QMenu, folder_id: str):
        f = self.project.folders.get(folder_id)
        if not f:
            return
        sub = menu.addMenu(i18n.tr("library.menu.moveToFolder"))
        root_action = sub.addAction(i18n.tr("library.menu.root"))
        root_action.setCheckable(True)
        root_action.setChecked(not f.parent)
        root_action.triggered.connect(lambda: self._move_folder(folder_id, None))
        candidates = [
            (folder, depth) for folder, depth in self._flatten_folders(f.category)
            if folder.id != folder_id and not self.project.would_create_folder_cycle(folder_id, folder.id)
        ]
        if candidates:
            sub.addSeparator()
        for folder, depth in candidates:
            action = sub.addAction("\u00a0\u00a0" * depth + folder.name)
            action.setCheckable(True)
            action.setChecked(f.parent == folder.id)
            action.triggered.connect(lambda _checked=False, fid=folder.id: self._move_folder(folder_id, fid))

    def _flatten_folders(self, category: str, parent_id: Optional[str] = None, depth: int = 0) -> list:
        out = []
        for folder in self.project.folders_in(category, parent_id):
            out.append((folder, depth))
            out.extend(self._flatten_folders(category, folder.id, depth + 1))
        return out

    def _move_folder(self, folder_id: str, new_parent: Optional[str]):
        f = self.project.folders.get(folder_id)
        if not f or f.parent == new_parent:
            return
        f.parent = new_parent
        self.controller.mark_dirty()
        self._after_mutation()

    # actions
    def _new_scene(self):
        name, ok = QInputDialog.getText(self, i18n.tr("library.newScene.title"), i18n.tr("library.newScene.prompt"), text=i18n.tr("library.newScene.default"))
        if ok and name.strip():
            fid = self._current_folder_id() if self._nav_category == "scene" else None
            new_id = self.controller.insert_new_scene(name.strip(), folder_id=fid)
            self._after_mutation()

    def _rename_scene(self, scene_id: str):
        scene = self.project.scenes.get(scene_id)
        if not scene:
            return
        name, ok = QInputDialog.getText(self, i18n.tr("library.renameScene.title"), i18n.tr("library.renameScene.prompt"), text=scene.name)
        if ok and name.strip():
            new_name = name.strip()
            if new_name != scene.name:
                existing = {
                    s.name for sid, s in self.project.scenes.items()
                    if sid != scene_id and (s.folder_id or "") == (scene.folder_id or "")
                }
                new_name = self.controller._unique_name(new_name, existing)
            scene.name = new_name
            self.controller.mark_dirty()
            self._after_mutation()


    def _new_class(self):
        name, ok = QInputDialog.getText(self, i18n.tr("library.newClass.title"), i18n.tr("library.newClass.prompt"))
        if ok and name.strip():
            self.controller.create_class(name.strip())
            self._after_mutation()

    def _delete_class(self, cls: str):
        members = [s for s in self.project.symbol_defs.values() if s.elem_class == cls]
        msg = i18n.tr("library.deleteClass.confirm").format(name=cls)
        if members:
            msg += "\n\n" + i18n.tr("library.deleteClass.members").format(count=len(members))
        if QMessageBox.question(self, i18n.tr("app.title"), msg) == QMessageBox.StandardButton.Yes:
            self.controller.delete_class(cls)
            self._after_mutation()

    def _new_symbol(self, preselect_class: Optional[str] = None):
        if not self.project.classes:
            QMessageBox.information(self, i18n.tr("library.newSymbol.title"), i18n.tr("library.newSymbol.noClasses"))
            return
        default_class = preselect_class or self._current_class() or self.project.classes[0]
        dlg = NewSymbolDialog(self.project, self.controller.project_dir, initial_class=default_class, parent=self)
        if dlg.exec():
            fid = self._current_folder_id() if self._nav_category == "symbol" else None
            new_id = self.controller.insert_new_symbol(dlg.values(), folder_id=fid)
            self._after_mutation()
            self.object_selected.emit(KIND_SYMBOL, new_id)

    def _new_folder(self, category: str, parent_id: Optional[str]):
        name, ok = QInputDialog.getText(self, i18n.tr("library.newFolder.title"), i18n.tr("library.newFolder.prompt"), text=i18n.tr("library.newFolder.default"))
        if ok and name.strip():
            self.controller.create_folder(category, parent_id, name.strip())
            self._after_mutation()

    def _rename_folder(self, folder_id: str):
        f = self.project.folders.get(folder_id)
        if not f:
            return
        name, ok = QInputDialog.getText(self, i18n.tr("library.renameFolder.title"), i18n.tr("library.renameFolder.prompt"), text=f.name)
        if ok and name.strip():
            self.controller.rename_folder(folder_id, name.strip())
            self._after_mutation()

    def _delete_folder(self, folder_id: str):
        f = self.project.folders.get(folder_id)
        if not f:
            return
        folder_ids = self.project.folder_and_descendants(folder_id)
        items = self.controller.items_in_folders(f.category, folder_ids)
        msg = i18n.tr("library.deleteFolder.confirm").format(name=f.name)
        if items:
            msg += "\n\n" + i18n.tr("library.deleteFolder.items").format(count=len(items))
        if QMessageBox.question(self, i18n.tr("app.title"), msg) == QMessageBox.StandardButton.Yes:
            self.controller.delete_folder(folder_id)
            self._after_mutation()

    def _delete_asset(self, kind: str, obj_id: str):
        if QMessageBox.question(
            self, i18n.tr("app.title"), i18n.tr("library.deleteAsset.confirm")
        ) == QMessageBox.StandardButton.Yes:
            try:
                self.controller.delete_asset(kind, obj_id)
            except ValueError as e:
                QMessageBox.warning(self, i18n.tr("app.title"), str(e))
                return
            self._after_mutation()

    def _delete_assets(self, entries: list):
        if not entries:
            return
        n = len(entries)
        if QMessageBox.question(
            self, i18n.tr("app.title"),
            i18n.tr("library.deleteAssets.confirm").format(count=n)
        ) != QMessageBox.StandardButton.Yes:
            return
        errors = []
        for kind, obj_id in entries:
            try:
                self.controller.delete_asset(kind, obj_id)
            except ValueError as e:
                errors.append(str(e))
        if errors:
            QMessageBox.warning(self, i18n.tr("app.title"), "\n".join(errors))
        self._after_mutation()

    def _rename_source(self, uuid: str):
        cur = self.project.sources.get(uuid, "")
        name, ok = QInputDialog.getText(self, i18n.tr("library.renameSource.title"), i18n.tr("library.renameSource.prompt"), text=cur)
        if ok and name.strip():
            self.controller.rename_source(uuid, name.strip())
            self._after_mutation()

    # external D&D
    def _view_folder_target(self, pos):
        view = self.stack.currentWidget()
        if view is None or not hasattr(view, "_folder_target"):
            return None, None
        local = view.viewport().mapFrom(self, pos)
        if not view.viewport().rect().contains(local):
            return view, None
        return view, view._folder_target(local)

    def dragEnterEvent(self, event: QDragEnterEvent):
        _dbg("PANEL.dragEnter", "mime=", event.mimeData().formats())
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        elif event.mimeData().hasFormat(MIME_ASSET):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        if event.mimeData().hasFormat(MIME_ASSET):
            _, folder_id = self._view_folder_target(event.position().toPoint())
            _dbg("PANEL.dragMove", "target=", folder_id)
            event.setDropAction(Qt.DropAction.MoveAction if folder_id
                                else Qt.DropAction.IgnoreAction)
            event.accept()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasFormat(MIME_ASSET):
            _, folder_id = self._view_folder_target(event.position().toPoint())
            assets = decode_asset_mime_list(event.mimeData()) if folder_id else []
            _dbg("PANEL.drop", "folder=", folder_id, "assets=", assets)
            if not assets:
                event.ignore()
                return
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            self._on_folder_drop(assets, folder_id)
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self._import_dropped_paths(paths)

    def _import_dropped_paths(self, paths: list):
        for path in paths:
            if os.path.isdir(path) and os.path.isfile(os.path.join(path, "project.json")):
                self.open_project_requested.emit(path)
                return
        if not self.project:
            return
        parent_folder_id = self._current_folder_id() if self._nav_category == "source" else None
        imported_any = False
        for path in paths:
            if os.path.isdir(path):
                imported_any = self._import_folder_recursive(path, parent_folder_id) or imported_any
            elif os.path.isfile(path) and path.lower().endswith(MEDIA_EXTS):
                imported_any = self._import_single_file(path, parent_folder_id) or imported_any
        if imported_any:
            self._after_mutation()

    def _import_folder_recursive(self, dir_path: str, parent_folder_id: Optional[str]) -> bool:
        folder_id = self.controller.create_folder("source", parent_folder_id, os.path.basename(dir_path.rstrip(os.sep)))
        imported_any = False
        try:
            names = sorted(os.listdir(dir_path))
        except OSError:
            return False
        for name in names:
            full = os.path.join(dir_path, name)
            if os.path.isdir(full):
                imported_any = self._import_folder_recursive(full, folder_id) or imported_any
            elif os.path.isfile(full) and full.lower().endswith(MEDIA_EXTS):
                imported_any = self._import_single_file(full, folder_id) or imported_any
        return imported_any

    def _import_single_file(self, path: str, folder_id: Optional[str]) -> bool:
        try:
            self.controller.add_source_file(path, folder_id=folder_id)
        except ValueError as e:
            QMessageBox.warning(self, i18n.tr("library.import.title"), i18n.tr("library.import.failed").format(file=os.path.basename(path), error=e))
            return False
        return True

    def _import_source(self):
        pattern = "Media files (*" + " *".join(MEDIA_EXTS) + ")"
        paths, _ = QFileDialog.getOpenFileNames(self, i18n.tr("library.importSources.title"), "", pattern)
        if not paths:
            return
        fid = self._current_folder_id() if self._nav_category == "source" else None
        imported = []
        for path in paths:
            try:
                imported.append(self.controller.add_source_file(path, folder_id=fid))
            except ValueError as e:
                QMessageBox.warning(self, i18n.tr("library.importSource.title"), str(e))
                break
        if imported:
            self._after_mutation()

    def _replace_source(self, uuid: str):
        path, _ = QFileDialog.getOpenFileName(self, i18n.tr("library.replaceFile.title"))
        if not path:
            return

        def done(err):
            if err is not None:
                QMessageBox.warning(self, i18n.tr("library.replaceFile.title"), str(err))
                return
            self._after_mutation()

        try:
            self.controller.replace_source_file(uuid, path, on_done=done)
        except ValueError as e:
            QMessageBox.warning(self, i18n.tr("library.replaceFile.title"), str(e))

    # scene / symbol export & import
    def _safe_filename(self, name: str) -> str:
        keep = "".join(c if (c.isalnum() or c in " ._-") else "_" for c in (name or "untitled"))
        return keep.strip() or "untitled"

    def _export_scene(self, scene_id: str):
        scene = self.project.scenes.get(scene_id)
        if not scene:
            return
        default = self._safe_filename(scene.name) + ".czescn"
        path, _ = QFileDialog.getSaveFileName(self, i18n.tr("library.exportScene.title"), default, i18n.tr("library.filter.scene"))
        if not path:
            return
        if not path.lower().endswith(".czescn"):
            path += ".czescn"
        try:
            self.controller.export_scene(scene_id, path)
        except ValueError as e:
            QMessageBox.warning(self, i18n.tr("library.exportScene.title"), str(e))

    def _export_symbol(self, symbol_id: str):
        sym = self.project.symbol_defs.get(symbol_id)
        if not sym:
            return
        default = self._safe_filename(sym.name) + ".czesym"
        path, _ = QFileDialog.getSaveFileName(self, i18n.tr("library.exportSymbol.title"), default, i18n.tr("library.filter.symbol"))
        if not path:
            return
        if not path.lower().endswith(".czesym"):
            path += ".czesym"
        try:
            self.controller.export_symbol(symbol_id, path)
        except ValueError as e:
            QMessageBox.warning(self, i18n.tr("library.exportSymbol.title"), str(e))

    def _import_scene(self):
        path, _ = QFileDialog.getOpenFileName(self, i18n.tr("library.importSceneDlg.title"), "", i18n.tr("library.filter.scene"))
        if not path:
            return
        try:
            new_id = self.controller.import_scene(path)
        except ValueError as e:
            QMessageBox.warning(self, i18n.tr("library.importSceneDlg.title"), str(e))
            return
        self._after_mutation()
        if new_id:
            self.object_selected.emit(KIND_SCENE, new_id)

    def _import_symbol(self):
        path, _ = QFileDialog.getOpenFileName(self, i18n.tr("library.importSymbolDlg.title"), "", i18n.tr("library.filter.symbol"))
        if not path:
            return
        try:
            new_id = self.controller.import_symbol(path)
        except ValueError as e:
            QMessageBox.warning(self, i18n.tr("library.importSymbolDlg.title"), str(e))
            return
        self._after_mutation()
        if new_id:
            self.object_selected.emit(KIND_SYMBOL, new_id)


class _LazyLabels:
    def __init__(self, prefix, keys):
        self._prefix, self._keys = prefix, keys
    def __getitem__(self, k):
        return i18n.tr(f"{self._prefix}.{self._keys[k]}")
    def get(self, k, default=None):
        return self[k] if k in self._keys else default


_CATEGORY_LABELS = _LazyLabels("library.category", {"scene": "scenes", "symbol": "symbols", "source": "sources"})
_TYPE_LABELS = _LazyLabels("library.type", {
    KIND_FOLDER: "folder", KIND_CLASS: "class", KIND_SCENE: "scene", KIND_SYMBOL: "symbol", KIND_SOURCE: "source",
})
