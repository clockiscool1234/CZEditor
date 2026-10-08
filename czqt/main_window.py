from __future__ import annotations

import json
import os
import time

from PyQt6.QtCore import QByteArray, QEvent, QSettings, Qt, QTimer
from PyQt6.QtGui import QAction, QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QStatusBar,
    QStyle,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

TIMELINE_DEFAULT_H = 200  # px; default (and reset) height of the timeline panel

from . import i18n
from .backend import BackendError, LocalBackend
from .resources import icon_path
from .asset_types import is_audio, is_video
from .dialogs.new_project_dialog import NewProjectDialog
from .dialogs.open_project_dialog import OpenProjectDialog
from .dialogs.preferences_dialog import PreferencesDialog
from .dialogs.project_settings_dialog import ProjectSettingsDialog
from .keybinds import IMPLEMENTED_ACTIONS, KEYBIND_LABELS
from .model import MAIN_ID
from .preferences import Preferences
from .project_controller import ProjectController, SaveCancelled, folder_name_of
from .sound import SoundEngine
from .widgets.library import LibraryPanel, MEDIA_EXTS
from .widgets.preview import PreviewPanel
from .widgets.properties import PropertiesPanel
from .widgets.timeline import TimelinePanel


def _make_sound_engine(parent):
    try:
        from .sound_mixer import MIXER_AVAILABLE, SoundMixer

        if MIXER_AVAILABLE:
            return SoundMixer()
    except Exception as e:
        print(f"[sound] SoundMixer unavailable, using QtMultimedia instead: {e}")
    return SoundEngine(parent)


class MainWindow(QMainWindow):
    def __init__(self, backend: LocalBackend):
        super().__init__()
        self.backend = backend
        self.controller = ProjectController(backend, self)

        self.prefs = Preferences.load()
        i18n.init(self.prefs.language)
        self._last_root = self.prefs.default_project_dir
        os.makedirs(self._last_root, exist_ok=True)

        self.setWindowTitle(i18n.tr("app.title"))
        self.setWindowIcon(QIcon(icon_path()))
        self.resize(1280, 820)
        self.setDockNestingEnabled(True)

        self.library_panel = LibraryPanel(self.controller)
        self.timeline_panel = TimelinePanel()
        self.properties_panel = PropertiesPanel()
        self.properties_panel.auto_apply_properties = self.prefs.auto_apply_properties
        self.preview_panel = PreviewPanel()
        self.controller.invalidate_source_cache = (
            lambda uuid_ext: self.preview_panel.images.invalidate(self.controller.project_dir, uuid_ext)
        )

        self._scene_stack: list[str] = [MAIN_ID]
        self.breadcrumb_bar = QWidget()
        self.breadcrumb_bar.setObjectName("breadcrumbBar")
        self.breadcrumb_layout = QHBoxLayout(self.breadcrumb_bar)
        self.breadcrumb_layout.setContentsMargins(10, 4, 10, 4)

        preview_container = QWidget()
        preview_container_layout = QVBoxLayout(preview_container)
        preview_container_layout.setContentsMargins(0, 0, 0, 0)
        preview_container_layout.setSpacing(0)
        preview_container_layout.addWidget(self.breadcrumb_bar)
        preview_container_layout.addWidget(self.preview_panel, 1)

        dock_features = (
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )

        library_dock = QDockWidget(i18n.tr("dock.library"), self)
        library_dock.setObjectName("libraryDock")
        library_dock.setWidget(self.library_panel)
        library_dock.setFeatures(dock_features)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, library_dock)

        preview_dock = QDockWidget(i18n.tr("dock.programMonitor"), self)
        preview_dock.setObjectName("previewDock")
        preview_dock.setWidget(preview_container)
        preview_dock.setFeatures(dock_features)
        self.splitDockWidget(library_dock, preview_dock, Qt.Orientation.Horizontal)

        properties_dock = QDockWidget(i18n.tr("dock.objectProperties"), self)
        properties_dock.setObjectName("propertiesDock")
        properties_dock.setWidget(self.properties_panel)
        properties_dock.setFeatures(dock_features)
        self.splitDockWidget(preview_dock, properties_dock, Qt.Orientation.Horizontal)

        timeline_dock = QDockWidget(i18n.tr("dock.timeline"), self)
        timeline_dock.setObjectName("timelineDock")
        timeline_dock.setWidget(self.timeline_panel)
        timeline_dock.setFeatures(dock_features)

        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.BottomDockWidgetArea)
        self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.BottomDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, timeline_dock)

        self.resizeDocks([library_dock, preview_dock, properties_dock], [230, 780, 260], Qt.Orientation.Horizontal)
        self._timeline_h = self._load_timeline_height()
        self._applying_timeline_h = False
        self._last_win_size = self.size()
        self._timeline_h_timer = QTimer(self)
        self._timeline_h_timer.setSingleShot(True)
        self._timeline_h_timer.setInterval(150)
        self._timeline_h_timer.timeout.connect(self._enforce_timeline_height)
        timeline_dock.installEventFilter(self)
        self.resizeDocks([preview_dock, timeline_dock], [560, self._timeline_h], Qt.Orientation.Vertical)

        self.library_dock = library_dock
        self.preview_dock = preview_dock
        self.properties_dock = properties_dock
        self.timeline_dock = timeline_dock
        timeline_dock.dockLocationChanged.connect(self._on_timeline_dock_area_changed)
        timeline_dock.topLevelChanged.connect(self._on_timeline_dock_area_changed)

        self.setStatusBar(QStatusBar())
        self._autosave_status_label = QLabel()
        self.statusBar().addPermanentWidget(self._autosave_status_label)
        self._build_toolbar()
        self._build_menu()
        QApplication.instance().focusChanged.connect(self._on_global_focus_changed)

        self.library_panel.scene_activated.connect(self._on_library_scene_activated)
        self.library_panel.object_selected.connect(self._on_library_object_selected)
        self.library_panel.project_mutated.connect(self._on_library_mutated)
        self.library_panel.open_project_requested.connect(self._open_project_at)
        self.timeline_panel.selection_changed.connect(self._on_timeline_selection_changed)
        self.timeline_panel.placement_changed.connect(self._on_placement_changed)
        self.timeline_panel.request_enter_scene.connect(self._on_enter_scene)
        self.timeline_panel.playhead_changed.connect(self.preview_panel.set_time)
        self.timeline_panel.playhead_changed.connect(self._on_playhead_changed)
        self.timeline_panel.file_dropped.connect(self._on_file_dropped_on_timeline)
        self.timeline_panel.source_asset_dropped.connect(self._on_source_asset_dropped_on_timeline)
        self.timeline_panel.changed.connect(self._on_timeline_structure_changed)
        self.properties_panel.changed.connect(self._on_properties_changed)
        self.properties_panel.request_enter_scene.connect(self._on_enter_scene)
        self.properties_panel.request_insert.connect(self._on_insert_requested)
        self.properties_panel.paint_mode_changed.connect(self._on_paint_mode_changed)
        self.controller.project_changed.connect(self._on_project_changed)
        self.controller.dirty_changed.connect(self._on_dirty_changed)
        self.controller.newer_version_detected.connect(self._on_newer_version_detected)

        self.preview_panel.play_pause_requested.connect(self._toggle_playback)
        self.preview_panel.stop_requested.connect(self._stop_playback)
        self.preview_panel.scrub_requested.connect(self.timeline_panel.set_playhead)
        self.preview_panel.asset_dropped.connect(self._on_asset_dropped_on_preview)
        self.preview_panel.file_dropped.connect(self._on_file_dropped_on_preview)
        self.preview_panel.selection_changed.connect(self._on_preview_selection_changed)
        self.preview_panel.placement_edited.connect(self._on_preview_placement_edited)

        self._syncing_selection = False

        self._playing = False
        self._play_timer = QTimer(self)
        self._play_timer.setTimerType(Qt.TimerType.PreciseTimer)  # the default coarse timer can fire ~5% late
        self._play_timer.timeout.connect(self._advance_playhead)
        self._play_started_at = 0.0  # time.perf_counter() when the anchor below was set
        self._play_started_playhead = 0.0  # playhead time at that anchor
        self._advancing_internally = False  # True while _advance_playhead itself is seeking
        self.sound_engine = _make_sound_engine(self)

        # preferences: apply what's persisted, wire the rest to actions
        self.timeline_panel.snap_enabled = self.prefs.snap_to_playhead
        self.timeline_panel.snap_to_clips = self.prefs.snap_to_clips
        self.timeline_panel.allow_recursive_nesting = self.prefs.allow_recursive_scene_nesting
        self.timeline_panel.max_nesting_depth = self.prefs.max_scene_nesting_depth
        self.preview_panel.set_quality(self.prefs.preview_quality)
        self.preview_panel.quality_changed.connect(self._on_preview_quality_changed)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._on_autosave_tick)
        self._restart_autosave_timer()

        self._on_project_changed()
        self._restore_layout_from_settings()
        QTimer.singleShot(0, self._apply_timeline_height)

    # menu
    def _reg_action(self, action_name: str, text: str, slot, menu=None) -> QAction:
        action = QAction(text, self)
        if action_name in IMPLEMENTED_ACTIONS:
            action.triggered.connect(slot)
        else:
            action.triggered.connect(lambda: self._not_implemented_action(action_name))
        self._keybind_actions[action_name] = action
        if menu is not None:
            menu.addAction(action)
        return action

    def _not_implemented_action(self, action_name: str):
        label = KEYBIND_LABELS.get(action_name, action_name)
        self.statusBar().showMessage(i18n.tr("status.notImplemented").format(label=label), 4000)

    def _apply_keybinds(self):
        for action_name, action in self._keybind_actions.items():
            seq = self.prefs.keybinds.get(action_name, "")
            action.setShortcut(QKeySequence(seq) if seq else QKeySequence())
        self._on_global_focus_changed(None, QApplication.instance().focusWidget())

    def _on_global_focus_changed(self, _old, new):
        capturing = _is_capturing_input(new)
        for action_name, action in self._keybind_actions.items():
            seq_str = self.prefs.keybinds.get(action_name, "")
            if _shortcut_is_risky(seq_str):
                action.setEnabled(not capturing)

    def _build_menu(self):
        self._keybind_actions: dict = {}
        file_menu = self.menuBar().addMenu(i18n.tr("menu.file"))
        self._reg_action("action_new", i18n.tr("menu.file.new"), self.on_new_project, file_menu)
        self._reg_action("action_open", i18n.tr("menu.file.open"), self.on_open_project, file_menu)
        file_menu.addSeparator()
        self._reg_action("action_save", i18n.tr("menu.file.save"), self.on_save_project, file_menu)
        self._reg_action("action_save_as", i18n.tr("menu.file.saveAs"), self.on_save_project_as, file_menu)
        file_menu.addSeparator()
        self._reg_action("action_project_settings", i18n.tr("menu.file.projectSettings"), self.on_project_settings, file_menu)
        self._reg_action("action_preferences", i18n.tr("menu.file.preferences"), self.on_preferences, file_menu)
        file_menu.addSeparator()
        self._reg_action("action_quit", i18n.tr("menu.file.quit"), self.close, file_menu)

        edit_menu = self.menuBar().addMenu(i18n.tr("menu.edit"))
        self._reg_action("action_undo", i18n.tr("menu.edit.undo"), self._undo, edit_menu)
        self._reg_action("action_redo", i18n.tr("menu.edit.redo"), self._redo, edit_menu)
        edit_menu.addSeparator()
        self._reg_action("action_cut", i18n.tr("menu.edit.cut"), self._cut, edit_menu)
        self._reg_action("action_copy", i18n.tr("menu.edit.copy"), self._copy, edit_menu)
        self._reg_action("action_paste", i18n.tr("menu.edit.paste"), self._paste, edit_menu)
        edit_menu.addSeparator()
        self._reg_action("action_split", i18n.tr("menu.edit.split"), self.timeline_panel.split_selected_at_playhead, edit_menu)
        self._reg_action("action_delete", i18n.tr("menu.edit.delete"), self.timeline_panel.delete_selected, edit_menu)

        insert_menu = self.menuBar().addMenu(i18n.tr("menu.insert"))
        insert_menu.addAction(i18n.tr("menu.insert.newScene"), self.library_panel._new_scene)
        insert_menu.addAction(i18n.tr("menu.insert.newSymbolClass"), self.library_panel._new_class)
        insert_menu.addAction(i18n.tr("menu.insert.newSymbol"), lambda: self.library_panel._new_symbol())

        playback_menu = self.menuBar().addMenu(i18n.tr("menu.playback"))
        self._reg_action("action_play_pause", i18n.tr("menu.playback.playPause"), self._toggle_playback, playback_menu)
        self._reg_action("action_stop", i18n.tr("menu.playback.stop"), self._stop_playback, playback_menu)
        playback_menu.addSeparator()
        self._reg_action("action_next_frame", i18n.tr("menu.playback.nextFrame"), lambda: self._step_playhead(+1, frame=True), playback_menu)
        self._reg_action("action_prev_frame", i18n.tr("menu.playback.prevFrame"), lambda: self._step_playhead(-1, frame=True), playback_menu)
        self._reg_action("action_next_sec", i18n.tr("menu.playback.nextSec"), lambda: self._step_playhead(+1, frame=False), playback_menu)
        self._reg_action("action_prev_sec", i18n.tr("menu.playback.prevSec"), lambda: self._step_playhead(-1, frame=False), playback_menu)

        export_menu = self.menuBar().addMenu(i18n.tr("menu.export"))
        self._reg_action("action_export_video", i18n.tr("menu.export.video"), self._export_video, export_menu)
        self._reg_action("action_export_png", i18n.tr("menu.export.png"), None, export_menu)

        view_menu = self.menuBar().addMenu(i18n.tr("menu.view"))
        view_menu.addAction(self.library_dock.toggleViewAction())
        view_menu.addAction(self.preview_dock.toggleViewAction())
        view_menu.addAction(self.properties_dock.toggleViewAction())
        view_menu.addAction(self.timeline_dock.toggleViewAction())
        toolbar_toggle = self._toolbar.toggleViewAction()
        toolbar_toggle.setText(i18n.tr("menu.view.mainToolbar"))
        view_menu.addAction(toolbar_toggle)
        view_menu.addSeparator()
        self._reg_action("action_normal_mode", i18n.tr("menu.view.normalMode"), lambda: self.preview_panel.set_mode("normal"), view_menu)
        self._reg_action("action_pan_mode", i18n.tr("menu.view.panMode"), lambda: self.preview_panel.set_mode("pan"), view_menu)
        self._reg_action("action_timeline_mode", i18n.tr("menu.view.timingMode"), self._toggle_timing_mode, view_menu)
        self._reg_action("action_crop_mode", i18n.tr("menu.view.cropMode"), lambda: self.preview_panel.set_mode("crop"), view_menu)
        self._reg_action("action_mask_mode", i18n.tr("menu.view.maskMode"), lambda: self.preview_panel.set_mode("mask"), view_menu)
        self._reg_action("action_keyframe_mode", i18n.tr("menu.view.keyframeMode"), self._toggle_keyframe_mode, view_menu)
        view_menu.addSeparator()
        view_menu.addAction(i18n.tr("menu.view.resetLayout"), self._reset_layout)
        view_menu.addAction(i18n.tr("menu.view.exportLayout"), self.on_export_layout)
        view_menu.addAction(i18n.tr("menu.view.importLayout"), self.on_import_layout)

        help_menu = self.menuBar().addMenu(i18n.tr("menu.help"))
        help_menu.addAction(i18n.tr("menu.help.about"), self._show_about)

        self._apply_keybinds()

    def _theme_icon(self, theme_name: str, fallback: QStyle.StandardPixmap) -> QIcon:
        icon = QIcon.fromTheme(theme_name)
        if not icon.isNull():
            return icon
        return self.style().standardIcon(fallback)

    def _build_toolbar(self):
        s = QSettings("CZEditor", "CZEditor")
        self._toolbar_action_defs = [
            ("new",      i18n.tr("toolbar.new"),          "document-new",       QStyle.StandardPixmap.SP_FileIcon,             self.on_new_project),
            ("open",     i18n.tr("toolbar.open"),         "document-open",      QStyle.StandardPixmap.SP_DialogOpenButton,     self.on_open_project),
            ("save",     i18n.tr("toolbar.save"),         "document-save",      QStyle.StandardPixmap.SP_DialogSaveButton,     self.on_save_project),
            ("sep1",     None, None, None, None),
            ("newScene", i18n.tr("toolbar.newScene"),     "video-x-generic",    QStyle.StandardPixmap.SP_FileDialogNewFolder,  self.library_panel._new_scene),
            ("newSymbol",i18n.tr("toolbar.newSymbol"),    "insert-image",       QStyle.StandardPixmap.SP_FileIcon,             lambda: self.library_panel._new_symbol()),
            ("import",   i18n.tr("toolbar.importSource"), "document-import",    QStyle.StandardPixmap.SP_DialogOpenButton,     self.library_panel._import_source),
            ("sep2",     None, None, None, None),
            ("split",    i18n.tr("toolbar.split"),        "edit-cut",           QStyle.StandardPixmap.SP_ArrowRight,           self.timeline_panel.split_selected_at_playhead),
            ("delete",   i18n.tr("toolbar.delete"),       "edit-delete",        QStyle.StandardPixmap.SP_TrashIcon,            self.timeline_panel.delete_selected),
        ]

        default_order = [d[0] for d in self._toolbar_action_defs]
        saved_order_raw = s.value("toolbar_order", "", type=str)
        if saved_order_raw:
            try:
                saved_order = json.loads(saved_order_raw)
                known = {d[0] for d in self._toolbar_action_defs}
                if all(item in known for item in saved_order):
                    order = saved_order
                else:
                    order = default_order
            except Exception:
                order = default_order
        else:
            order = default_order
        self._toolbar_order = order

        style_str = s.value("toolbar_style", "text+icon", type=str)
        self._toolbar_style = style_str

        self._toolbar = QToolBar(i18n.tr("toolbar.mainTitle"), self)
        self._toolbar.setObjectName("mainToolBar")
        self._toolbar.setMovable(True)
        self._toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._toolbar.customContextMenuRequested.connect(self._toolbar_context_menu)
        self.addToolBar(self._toolbar)
        self._apply_toolbar_style()
        self._rebuild_toolbar()

    def _apply_toolbar_style(self):
        style_map = {
            "icon":      Qt.ToolButtonStyle.ToolButtonIconOnly,
            "text":      Qt.ToolButtonStyle.ToolButtonTextOnly,
            "text+icon": Qt.ToolButtonStyle.ToolButtonTextBesideIcon,
        }
        self._toolbar.setToolButtonStyle(style_map.get(self._toolbar_style, Qt.ToolButtonStyle.ToolButtonTextBesideIcon))

    def _rebuild_toolbar(self):
        self._toolbar.clear()
        defs_by_id = {d[0]: d for d in self._toolbar_action_defs}
        for item_id in self._toolbar_order:
            d = defs_by_id.get(item_id)
            if d is None:
                continue
            act_id, label, theme, fallback, slot = d
            if label is None:
                self._toolbar.addSeparator()
            else:
                self._toolbar.addAction(self._theme_icon(theme, fallback), label, slot)

    def _toolbar_context_menu(self, pos):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        style_menu = menu.addMenu(i18n.tr("toolbar.configure.style"))
        for style_id, label_key in [
            ("icon",      "toolbar.configure.styleIconOnly"),
            ("text",      "toolbar.configure.styleTextOnly"),
            ("text+icon", "toolbar.configure.styleTextBesideIcon"),
        ]:
            act = style_menu.addAction(i18n.tr(label_key))
            act.setCheckable(True)
            act.setChecked(self._toolbar_style == style_id)
            act.triggered.connect((lambda sid: lambda: self._set_toolbar_style(sid))(style_id))
        menu.addSeparator()
        configure_act = menu.addAction(i18n.tr("toolbar.configure.title") + "...")
        configure_act.triggered.connect(self._open_toolbar_config)
        menu.addSeparator()
        hide_act = menu.addAction(i18n.tr("menu.view.mainToolbar"))
        hide_act.setCheckable(True)
        hide_act.setChecked(self._toolbar.isVisible())
        hide_act.triggered.connect(self._toolbar.setVisible)
        menu.exec(self._toolbar.mapToGlobal(pos))

    def _set_toolbar_style(self, style_id: str):
        self._toolbar_style = style_id
        self._apply_toolbar_style()
        QSettings("CZEditor", "CZEditor").setValue("toolbar_style", style_id)

    def _open_toolbar_config(self):
        dlg = _ToolbarConfigDialog(self._toolbar_action_defs, self._toolbar_order, self)
        if dlg.exec():
            self._toolbar_order = dlg.result_order()
            self._rebuild_toolbar()
            QSettings("CZEditor", "CZEditor").setValue(
                "toolbar_order", json.dumps(self._toolbar_order)
            )

    # new
    def on_new_project(self):
        if not self._confirm_discard_if_dirty():
            return
        dlg = NewProjectDialog(self, default_location=self._last_root)
        if dlg.exec():
            values = dlg.values()
            folder = _folder_name(values["name"])
            project_dir = os.path.join(values["location"], folder) if values["location"] else None
            if project_dir and not self._confirm_overwrite(project_dir, allow_current=False):
                return
            self.controller.new_project(folder, values["width"], values["height"], values["fps"])
            if project_dir:
                self._last_root = values["location"]
                try:
                    self.controller.save_project(project_dir)
                    self.preview_panel.set_project_dir(project_dir)
                    self.properties_panel.set_project_dir(project_dir)
                    self.timeline_panel.set_project_dir(project_dir)
                    self._update_title()
                    self.statusBar().showMessage(i18n.tr("status.createdAt").format(path=project_dir), 5000)
                except (BackendError, OSError) as e:
                    QMessageBox.warning(self, i18n.tr("dialog.newProject.title"), i18n.tr("dialog.newProject.errWriteFailed").format(error=e))

    def _confirm_overwrite(self, project_dir: str, allow_current: bool = True) -> bool:
        if not os.path.isfile(os.path.join(project_dir, "project.json")):
            return True
        cur = self.controller.project_dir
        if allow_current and cur and os.path.exists(cur) and os.path.samefile(cur, project_dir):
            return True
        resp = QMessageBox.question(
            self, i18n.tr("dialog.saveProjectAs.title"),
            i18n.tr("dialog.saveProjectAs.confirmOverwrite").format(path=project_dir),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return resp == QMessageBox.StandardButton.Yes

    # open
    def on_open_project(self):
        if not self._confirm_discard_if_dirty():
            return
        dlg = OpenProjectDialog(self.backend, self, default_root=self._last_root)
        if dlg.exec():
            project_dir = dlg.chosen_dir()
            if project_dir:
                self._open_project_at(project_dir)

    def _open_project_at(self, project_dir: str):
        if not self._confirm_discard_if_dirty():
            return
        try:
            self.controller.open_project(project_dir)
            self._last_root = os.path.dirname(project_dir) or self._last_root
            self.statusBar().showMessage(i18n.tr("status.openedAt").format(path=project_dir), 5000)
        except (BackendError, OSError, ValueError) as e:
            QMessageBox.critical(self, i18n.tr("dialog.openProject.title"), i18n.tr("dialog.openProject.errFailedOpen").format(error=e))
        except Exception as e:  # malformed project.json, etc.
            QMessageBox.critical(self, i18n.tr("dialog.openProject.title"), i18n.tr("dialog.openProject.errInvalidJson").format(error=e))

    # save
    def on_save_project(self):
        try:
            if self.controller.project_dir:
                self.controller.save_project()
            else:
                self._save_as_flow()
            self.statusBar().showMessage(i18n.tr("status.saved"), 3000)
        except (BackendError, OSError, ValueError) as e:
            QMessageBox.critical(self, i18n.tr("dialog.saveProject.title"), i18n.tr("dialog.saveProject.errFailed").format(error=e))

    def on_save_project_as(self):
        try:
            self._save_as_flow()
        except (BackendError, OSError, ValueError) as e:
            QMessageBox.critical(self, i18n.tr("dialog.saveProject.title"), i18n.tr("dialog.saveProject.errFailed").format(error=e))

    def _save_as_flow(self):
        base = QFileDialog.getExistingDirectory(self, i18n.tr("dialog.saveProjectAs.chooseFolder"), self._last_root)
        if not base:
            return
        cur_dir = self.controller.project_dir
        default = folder_name_of(cur_dir) if cur_dir else _folder_name(self.controller.project.name)
        name, ok = QInputDialog.getText(
            self, i18n.tr("dialog.saveProjectAs.title"), i18n.tr("dialog.saveProjectAs.labelFolderName"), text=default)
        if not ok or not name.strip():
            return
        project_dir = os.path.join(base, _folder_name(name))
        if not self._confirm_overwrite(project_dir):
            return

        dlg = QProgressDialog(i18n.tr("dialog.saveProjectAs.copying"), i18n.tr("dialog.saveProjectAs.cancel"), 0, 100, self)
        dlg.setWindowTitle(i18n.tr("dialog.saveProjectAs.title"))
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(400)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)

        def on_progress(done: int, total: int, filename: str):
            if total > 0:
                dlg.setValue(min(99, int(done * 100 / total)))
            if filename:
                dlg.setLabelText(i18n.tr("dialog.saveProjectAs.copyingFile").format(name=filename))
            QApplication.processEvents()
            if dlg.wasCanceled():
                raise SaveCancelled()

        try:
            self.controller.save_project(project_dir, progress=on_progress)
        except SaveCancelled:
            self.statusBar().showMessage(i18n.tr("status.saveCancelled"), 4000)
            return
        finally:
            dlg.close()
        self.preview_panel.set_project_dir(project_dir)
        self.properties_panel.set_project_dir(project_dir)
        self.timeline_panel.set_project_dir(project_dir)
        self._last_root = base
        self._update_title()
        self.statusBar().showMessage(i18n.tr("status.savedTo").format(path=project_dir), 5000)

    # state
    def _confirm_discard_if_dirty(self) -> bool:
        if not self.controller.is_dirty:
            return True
        resp = QMessageBox.question(
            self,
            i18n.tr("dialog.unsavedChanges.title"),
            i18n.tr("dialog.unsavedChanges.message"),
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        if resp == QMessageBox.StandardButton.Cancel:
            return False
        if resp == QMessageBox.StandardButton.Save:
            self.on_save_project()
            return not self.controller.is_dirty
        return True

    def _on_project_changed(self):
        self.library_panel.set_project(self.controller.project)
        self.timeline_panel.set_project(self.controller.project)
        self.properties_panel.set_project(self.controller.project)
        self.properties_panel.set_project_dir(self.controller.project_dir)
        self.timeline_panel.set_project_dir(self.controller.project_dir)
        self.preview_panel.set_project(self.controller.project, self.controller.project_dir)
        self.preview_panel.set_duration(self.timeline_panel.effective_duration())
        self._scene_stack = [MAIN_ID]
        self._rebuild_breadcrumb_toolbar()
        self._update_title()

    def _on_dirty_changed(self, _dirty: bool):
        self._update_title()

    def _on_newer_version_detected(self, file_version: int):
        from .model import SCHEMA_VERSION
        QMessageBox.warning(
            self,
            i18n.tr("dialog.newerVersion.title"),
            i18n.tr("dialog.newerVersion.message").format(file_version=file_version, supported_version=SCHEMA_VERSION),
        )

    # panel wiring
    def _on_library_scene_activated(self, scene_id: str):
        self._scene_stack = [MAIN_ID] if scene_id == MAIN_ID else [MAIN_ID, scene_id]
        self._activate_scene(scene_id)

    def _on_enter_scene(self, scene_id: str):
        if scene_id in self._scene_stack:
            self._scene_stack = self._scene_stack[: self._scene_stack.index(scene_id) + 1]
        else:
            self._scene_stack.append(scene_id)
        self._activate_scene(scene_id)

    def _jump_to_breadcrumb(self, idx: int):
        self._scene_stack = self._scene_stack[: idx + 1]
        self._activate_scene(self._scene_stack[-1])

    def _activate_scene(self, scene_id: str):
        self.timeline_panel.set_scene(scene_id)
        self.preview_panel.set_project_dir(self.controller.project_dir)
        self.properties_panel.set_project_dir(self.controller.project_dir)
        self.properties_panel.show_scene_settings(scene_id)
        self.preview_panel.set_scene(scene_id)
        self.preview_panel.set_duration(self.timeline_panel.effective_duration())
        self._rebuild_breadcrumb_toolbar()
        self._stop_playback()

    def _rebuild_breadcrumb_toolbar(self):
        while self.breadcrumb_layout.count():
            child = self.breadcrumb_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        project = self.controller.project
        for i, sid in enumerate(self._scene_stack):
            if i > 0:
                sep = QLabel(">")
                sep.setObjectName("breadcrumbSep")
                self.breadcrumb_layout.addWidget(sep)
            scene = project.scenes.get(sid) if project else None
            name = (scene.name if scene else sid) or i18n.tr("breadcrumb.mainTimeline")
            btn = QPushButton(name)
            btn.setObjectName("breadcrumbLabel")
            btn.setFlat(True)
            if i == len(self._scene_stack) - 1:
                font = btn.font()
                font.setBold(True)
                btn.setFont(font)
            btn.clicked.connect(lambda _checked=False, idx=i: self._jump_to_breadcrumb(idx))
            self.breadcrumb_layout.addWidget(btn)
        self.breadcrumb_layout.addStretch(1)

    def _on_library_object_selected(self, kind: str, obj_id: str):
        self.properties_panel.show_asset(kind, obj_id)

    def _on_paint_mode_changed(self, active: bool, section: str):
        if active:
            self.statusBar().showMessage(
                i18n.tr("status.painterActive").format(section=section), 0
            )
        else:
            self.statusBar().clearMessage()

    def _on_timeline_selection_changed(self, placement):
        if self._syncing_selection:
            return
        # format painter intercept
        if self.properties_panel.is_paint_active and placement is not None:
            applied = self.properties_panel.apply_paint(placement)
            if applied:
                self.controller.mark_dirty()
                self.timeline_panel.redraw()
                self.preview_panel.refresh()
                self.properties_panel.show_placement(placement, self.timeline_panel.scene_id)
                return
        self._syncing_selection = True
        try:
            if placement is None:
                self.properties_panel.clear_selection()
                self.preview_panel._select(None)
            else:
                self.properties_panel.show_placement(placement, self.timeline_panel.scene_id)
                self.preview_panel._select(placement.id)
        finally:
            self._syncing_selection = False

    def _on_placement_changed(self, _placement):
        self.controller.mark_dirty()
        self.preview_panel.set_duration(self.timeline_panel.effective_duration())
        self.preview_panel.refresh()
        self.properties_panel.refresh_selection()

    def _on_timeline_structure_changed(self):
        self.controller.mark_dirty()
        self.preview_panel.refresh()

    def _on_properties_changed(self):
        self.controller.mark_dirty()
        self.timeline_panel.redraw()
        self.library_panel.refresh()
        self._rebuild_breadcrumb_toolbar()
        self.preview_panel.set_duration(self.timeline_panel.effective_duration())
        self.preview_panel.refresh()

    def _on_library_mutated(self):
        valid_scenes = self.controller.project.scenes
        self._scene_stack = [sid for sid in self._scene_stack if sid in valid_scenes] or [MAIN_ID]
        if self.timeline_panel.scene_id not in valid_scenes:
            self.timeline_panel.set_scene(self._scene_stack[-1])
            self.properties_panel.show_scene_settings(self._scene_stack[-1])
            self.preview_panel.set_scene(self._scene_stack[-1])
        else:
            self.timeline_panel.redraw()
        self._rebuild_breadcrumb_toolbar()
        self.properties_panel.clear_selection()
        self.preview_panel.refresh()

    def _on_insert_requested(self, kind: str, ref_id: str):
        placement = self.timeline_panel.insert_asset_at_playhead(kind, ref_id)
        self._after_placement_created(placement)

    def _after_placement_created(self, placement):
        if placement is not None:
            self.properties_panel.show_placement(placement, self.timeline_panel.scene_id)
            self.controller.mark_dirty()
            self.preview_panel.set_duration(self.timeline_panel.effective_duration())
            self.preview_panel.refresh()

    def _on_preview_selection_changed(self, placement):
        if self._syncing_selection:
            return
        # format painter intercept
        if self.properties_panel.is_paint_active and placement is not None:
            applied = self.properties_panel.apply_paint(placement)
            if applied:
                self.controller.mark_dirty()
                self.timeline_panel.redraw()
                self.preview_panel.refresh()
                self.properties_panel.show_placement(placement, self.timeline_panel.scene_id)
                return
        self._syncing_selection = True
        try:
            if placement is not None:
                self.properties_panel.show_placement(placement, self.timeline_panel.scene_id)
                item = self.timeline_panel._items.get(placement.id)
                if item is not None:
                    self.timeline_panel.select_item(item)
            else:
                self.properties_panel.show_scene_settings(self.timeline_panel.scene_id)
        finally:
            self._syncing_selection = False

    def _on_preview_placement_edited(self):
        self.controller.mark_dirty()
        self.timeline_panel.redraw()
        p = self.preview_panel._find_active(self.preview_panel.selected_placement_id)
        if p is not None:
            self.properties_panel.show_placement(p, self.timeline_panel.scene_id)

    # drag-and-drop
    def _on_asset_dropped_on_preview(self, kind: str, obj_id: str, scene_x: float, scene_y: float, top_left: bool):
        project = self.controller.project
        if not project:
            return
        ref_id = obj_id
        if kind == "source":
            src_name = project.sources.get(obj_id, "")
            if is_audio(src_name):
                kind = "sound"
            elif is_video(src_name):
                kind = "video"
            else:
                kind = "image"
        placement = self.timeline_panel.create_placement(
            kind, ref_id, start=self.timeline_panel.playhead,
            center=None if top_left else (scene_x, scene_y),
            top_left=(scene_x, scene_y) if top_left else None,
        )
        self._after_placement_created(placement)

    def _on_file_dropped_on_preview(self, path: str, scene_x: float, scene_y: float, top_left: bool):
        self._import_and_place_file(
            path, start=self.timeline_panel.playhead,
            center=None if top_left else (scene_x, scene_y),
            top_left=(scene_x, scene_y) if top_left else None,
        )

    def _on_file_dropped_on_timeline(self, path: str, t: float, track: int):
        project = self.controller.project
        if not project:
            return
        pw, ph = project.width, project.height
        self._import_and_place_file(path, start=t, center=(pw / 2.0, ph / 2.0), track=track)

    def _on_source_asset_dropped_on_timeline(self, obj_id: str, t: float, track: int):
        project = self.controller.project
        if not project:
            return
        center = None
        src_name = project.sources.get(obj_id, "")
        if is_audio(src_name):
            kind, ref_id = "sound", obj_id
        elif is_video(src_name):
            kind, ref_id = "video", obj_id
        else:
            kind, ref_id = "image", obj_id
            center = (project.width / 2.0, project.height / 2.0)
        placement = self.timeline_panel.create_placement(kind, ref_id, start=t, track=track, center=center)
        self._after_placement_created(placement)

    def _import_and_place_file(self, path: str, **placement_kwargs):
        project = self.controller.project
        if not project:
            return None
        if not path.lower().endswith(MEDIA_EXTS):
            QMessageBox.warning(
                self, i18n.tr("dialog.import.title"),
                i18n.tr("dialog.import.errUnsupported").format(file=os.path.basename(path)),
            )
            return None
        try:
            if is_audio(path):
                uuid_ext = self.controller.add_source_file(path)
                kind, ref_id = "sound", uuid_ext
            elif is_video(path):
                uuid_ext = self.controller.add_source_file(path)
                kind, ref_id = "video", uuid_ext
            else:
                uuid_ext = self.controller.add_source_file(path)
                kind, ref_id = "image", uuid_ext
        except ValueError as e:
            QMessageBox.warning(self, i18n.tr("dialog.import.title"), i18n.tr("dialog.import.errFailed").format(file=os.path.basename(path), error=e))
            return None
        self.library_panel.refresh()
        placement = self.timeline_panel.create_placement(kind, ref_id, **placement_kwargs)
        self._after_placement_created(placement)
        return placement

    # playback
    def _toggle_playback(self):
        self._playing = not self._playing
        self.preview_panel.set_playing(self._playing)
        if self._playing:
            fps = max(1, self.controller.project.fps if self.controller.project else 30)
            self._play_started_at = time.perf_counter()
            self._play_started_playhead = self.timeline_panel.playhead
            self._play_timer.start(max(1, 1000 // fps))
        else:
            self._play_timer.stop()
        self._sync_sound()

    def _stop_playback(self):
        self._playing = False
        self._play_timer.stop()
        self.preview_panel.set_playing(False)
        self.timeline_panel.set_playhead(0.0)
        self.sound_engine.stop_all()

    def _advance_playhead(self):
        duration = self.timeline_panel.effective_duration()
        if duration <= 0:
            t = 0.0
        else:
            elapsed = time.perf_counter() - self._play_started_at
            t = self._play_started_playhead + elapsed
            if t >= duration:
                t %= duration
                self._play_started_at = time.perf_counter() - t
                self._play_started_playhead = 0.0
                self.sound_engine.stop_all()
        self._advancing_internally = True
        try:
            self.timeline_panel.set_playhead(t)
        finally:
            self._advancing_internally = False
        self._sync_sound()

    def _on_playhead_changed(self, t: float):
        if self._playing and not self._advancing_internally:
            self._play_started_at = time.perf_counter()
            self._play_started_playhead = t
            self._sync_sound(seek=True)

    def _sync_sound(self, seek: bool = False):
        project = self.controller.project
        if not project:
            self.sound_engine.update(None, None, None, 0.0, self._playing)
            return

        import dataclasses
        from .model import Scene as _Scene

        local_t = self.timeline_panel.playhead
        current_scene_id = self.timeline_panel.scene_id

        stack = list(getattr(self, "_scene_stack", None) or [MAIN_ID])
        if not stack or stack[0] != MAIN_ID:
            stack = [MAIN_ID] + [sid for sid in stack if sid != MAIN_ID]
        if stack[-1] != current_scene_id:
            stack.append(current_scene_id)

        merged_placements = []
        seen_ids = set()  # was `any(...)` over the merged list per placement: O(n^2) on every playback tick
        offset = 0.0
        for i, sid in enumerate(stack):
            scene = project.scenes.get(sid)
            if scene is None:
                continue
            for p in scene.placements:
                if p.id in seen_ids:
                    continue
                shifted = dataclasses.replace(p, start=p.start + offset, end=p.end + offset) if offset else p
                merged_placements.append(shifted)
                seen_ids.add(p.id)
            if i + 1 < len(stack):
                next_sid = stack[i + 1]
                link = next(
                    (p for p in scene.placements if p.kind == "scene" and p.ref == next_sid), None
                )
                if link is not None:
                    offset += link.start - (link.src_offset or 0.0)

        t = local_t + offset

        proxy_scene = _Scene(
            id=MAIN_ID,
            name="_proxy",
            placements=merged_placements,
        )
        method = self.sound_engine.seek if seek else self.sound_engine.update
        method(project, self.controller.project_dir, proxy_scene, t, self._playing)

    # stepping
    def _toggle_timing_mode(self):
        current = self.preview_panel.mode
        new_mode = "timing" if current != "timing" else "normal"
        self.preview_panel.set_mode(new_mode)
        self.timeline_panel.set_timeline_mode(new_mode)
        label = i18n.tr("preview.modeTiming") if new_mode == "timing" else i18n.tr("preview.modeNormal")
        self.statusBar().showMessage(i18n.tr("status.modeChanged").format(mode=label), 2000)

    def _toggle_keyframe_mode(self):
        current = self.preview_panel.mode
        new_mode = "normal" if current == "keyframe" else "keyframe"
        self.preview_panel.set_mode(new_mode)
        self.timeline_panel.set_timeline_mode(new_mode)
        label = i18n.tr("preview.modeKeyframe") if new_mode == "keyframe" else i18n.tr("preview.modeNormal")
        self.statusBar().showMessage(i18n.tr("status.modeChanged").format(mode=label), 2000)

    def _step_playhead(self, direction: int, *, frame: bool):
        project = self.controller.project
        fps = max(1, project.fps if project else 30)
        delta = (1.0 / fps) if frame else 1.0
        duration = self.timeline_panel.effective_duration()
        new_t = max(0.0, min(duration, self.timeline_panel.playhead + direction * delta))
        self._play_started_at = time.perf_counter()
        self._play_started_playhead = new_t
        self.timeline_panel.set_playhead(new_t)
        if not self._playing:
            self.sound_engine.stop_all()

    # undo/redo
    def _undo(self):
        if not self.timeline_panel.undo():
            self.statusBar().showMessage(i18n.tr("status.nothingToUndo"), 2000)
        else:
            self.controller.mark_dirty()
            self.preview_panel.refresh()

    def _redo(self):
        if not self.timeline_panel.redo():
            self.statusBar().showMessage(i18n.tr("status.nothingToRedo"), 2000)
        else:
            self.controller.mark_dirty()
            self.preview_panel.refresh()

    # cut/copy/paste
    def _cut(self):
        if not self.timeline_panel.cut_selected():
            self.statusBar().showMessage(i18n.tr("status.nothingToCut"), 2000)
        else:
            self.controller.mark_dirty()
            self.preview_panel.refresh()

    def _copy(self):
        if not self.timeline_panel.copy_selected():
            self.statusBar().showMessage(i18n.tr("status.nothingToCopy"), 2000)
        else:
            self.statusBar().showMessage(i18n.tr("status.copied"), 1500)

    def _paste(self):
        p = self.timeline_panel.paste_from_clipboard(self.timeline_panel.playhead)
        if p is None:
            p = self._paste_image_from_system_clipboard()
        if p is None:
            self.statusBar().showMessage(i18n.tr("status.clipboardEmpty"), 2000)
        else:
            self.controller.mark_dirty()
            self.preview_panel.set_duration(self.timeline_panel.effective_duration())
            self.preview_panel.refresh()

    def _paste_image_from_system_clipboard(self):
        project = self.controller.project
        if not project:
            return None
        clipboard = QApplication.clipboard()
        md = clipboard.mimeData()
        if md is None or not md.hasImage():
            return None
        image = clipboard.image()
        if image.isNull():
            return None
        import tempfile
        tmp_dir = tempfile.mkdtemp(prefix="czeditor_paste_")
        tmp_path = os.path.join(tmp_dir, "Pasted Image.png")
        if not image.save(tmp_path, "PNG"):
            return None
        try:
            placement = self._import_and_place_file(
                tmp_path,
                start=self.timeline_panel.playhead,
                center=(project.width / 2.0, project.height / 2.0),
            )
        finally:
            try:
                os.remove(tmp_path)
                os.rmdir(tmp_dir)
            except OSError:
                pass
        return placement

    # project settings
    def on_project_settings(self):
        dlg = ProjectSettingsDialog(self.controller.project, self)
        if dlg.exec():
            values = dlg.values()
            p = self.controller.project
            p.width = values["width"]
            p.height = values["height"]
            p.fps = values["fps"]
            p.export_video_bitrate = values["export_video_bitrate"]
            p.export_audio_bitrate = values["export_audio_bitrate"]
            p.export_channels = values["export_channels"]
            self.controller.mark_dirty()
            self.preview_panel.refresh()
            self.library_panel.refresh()
            self._update_title()

    # preferences
    def on_preferences(self):
        dlg = PreferencesDialog(self.prefs, self)
        if dlg.exec():
            if dlg.was_reset():
                self.prefs.reset()
            else:
                for k, v in dlg.values().items():
                    setattr(self.prefs, k, v)
                self.prefs.save()
            self.timeline_panel.snap_enabled = self.prefs.snap_to_playhead
            self.timeline_panel.snap_to_clips = self.prefs.snap_to_clips
            self.timeline_panel.allow_recursive_nesting = self.prefs.allow_recursive_scene_nesting
            self.timeline_panel.max_nesting_depth = self.prefs.max_scene_nesting_depth
            self.preview_panel.set_quality(self.prefs.preview_quality)
            self.properties_panel.auto_apply_properties = self.prefs.auto_apply_properties
            self._apply_keybinds()
            self._restart_autosave_timer()

    def _on_preview_quality_changed(self, key: str):
        self.prefs.preview_quality = key
        self.prefs.save()

    def _restart_autosave_timer(self):
        self._autosave_timer.stop()
        if self.prefs.autosave_enabled:
            self._autosave_timer.start(max(5, self.prefs.autosave_interval_sec) * 1000)
        self._update_autosave_status_label()

    def _update_autosave_status_label(self):
        if self.prefs.autosave_enabled:
            secs = max(5, self.prefs.autosave_interval_sec)
            self._autosave_status_label.setText(i18n.tr("status.autosaveOn").format(secs=secs))
        else:
            self._autosave_status_label.setText(i18n.tr("status.autosaveOff"))

    def _on_autosave_tick(self):
        if not self.controller.is_dirty or not self.controller.project_dir:
            return
        if getattr(self, "_autosave_running", False):
            return  # previous autosave still writing; skip this tick

        self._autosave_running = True
        self._autosave_status_label.setText(i18n.tr("status.autosaving"))

        project_dir = self.controller.project_dir
        try:
            project_dict = self.controller.project.to_dict()
        except Exception:
            self._autosave_running = False
            self._update_autosave_status_label()
            return

        import threading

        def _write():
            success = False
            try:
                import json as _json, os as _os
                contents = _json.dumps(project_dict, indent=2)
                path = _os.path.join(project_dir, "project.json")
                tmp = path + ".autosave.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(contents)
                _os.replace(tmp, path)
                success = True
            except Exception:
                pass
            finally:
                QTimer.singleShot(0, lambda: self._on_autosave_done(success))

        threading.Thread(target=_write, name="czeditor-autosave", daemon=True).start()

    def _on_autosave_done(self, success: bool):
        self._autosave_running = False
        if success:
            self.controller.mark_saved()
            self.statusBar().showMessage(i18n.tr("status.autosaved"), 2000)
        self._update_autosave_status_label()

    def _export_video(self):
        project = self.controller.project
        if project is None:
            QMessageBox.warning(self, i18n.tr("dialog.export.noProject.title"), i18n.tr("dialog.export.noProject.message"))
            return

        project_dir = self.controller.project_dir
        if not project_dir:
            QMessageBox.warning(
                self,
                i18n.tr("dialog.export.notSaved.title"),
                i18n.tr("dialog.export.notSaved.message"),
            )
            return

        scene_id = self.timeline_panel.scene_id
        if scene_id not in project.scenes:
            QMessageBox.warning(self, i18n.tr("dialog.export.noScene.title"), i18n.tr("dialog.export.noScene.message"))
            return

        from .dialogs.video_export_dialog import VideoExportDialog
        dlg = VideoExportDialog(
            project, scene_id, project_dir, parent=self, ffmpeg_path=self.prefs.ffmpeg_path
        )
        dlg.exec()

    def _show_about(self):
        QMessageBox.about(
            self, i18n.tr("dialog.about.title"),
            i18n.tr("dialog.about.text"),
        )

    def _on_timeline_dock_area_changed(self, *_args):
        is_floating = self.timeline_dock.isFloating()
        area = self.dockWidgetArea(self.timeline_dock)
        is_side = (not is_floating) and area in (
            Qt.DockWidgetArea.LeftDockWidgetArea,
            Qt.DockWidgetArea.RightDockWidgetArea,
        )
        self.timeline_panel.set_side_mode(is_side)

    def _reset_layout(self):
        for dock in (self.library_dock, self.preview_dock, self.properties_dock, self.timeline_dock):
            dock.setFloating(False)
            dock.show()
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.library_dock)
        self.splitDockWidget(self.library_dock, self.preview_dock, Qt.Orientation.Horizontal)
        self.splitDockWidget(self.preview_dock, self.properties_dock, Qt.Orientation.Horizontal)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.timeline_dock)
        self.resizeDocks(
            [self.library_dock, self.preview_dock, self.properties_dock], [230, 780, 260], Qt.Orientation.Horizontal
        )
        self._timeline_h = TIMELINE_DEFAULT_H
        QSettings("CZEditor", "CZEditor").remove("layout/timeline_height")
        self._apply_timeline_height()

    # timeline panel height: remembered between sessions and kept when the layout is re-flowed
    def _load_timeline_height(self) -> int:
        try:
            h = int(QSettings("CZEditor", "CZEditor").value("layout/timeline_height", TIMELINE_DEFAULT_H))
        except (TypeError, ValueError):
            h = TIMELINE_DEFAULT_H
        return h if h >= 80 else TIMELINE_DEFAULT_H

    def _timeline_is_bottom_docked(self) -> bool:
        return (not self.timeline_dock.isFloating()
                and self.dockWidgetArea(self.timeline_dock) == Qt.DockWidgetArea.BottomDockWidgetArea)

    def _apply_timeline_height(self):
        if not self._timeline_is_bottom_docked():
            return
        self._applying_timeline_h = True
        try:
            total = max(1, self.preview_dock.height() + self.timeline_dock.height())
            self.resizeDocks([self.preview_dock, self.timeline_dock],
                             [max(1, total - self._timeline_h), self._timeline_h], Qt.Orientation.Vertical)
        finally:
            QTimer.singleShot(0, lambda: setattr(self, "_applying_timeline_h", False))

    def _enforce_timeline_height(self):
        if QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            self._timeline_h_timer.start()   # still dragging; check again once released
            return
        if self._timeline_is_bottom_docked() and abs(self.timeline_dock.height() - self._timeline_h) > 3:
            self._apply_timeline_height()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "timeline_dock", None) and event.type() == QEvent.Type.Resize:
            if not self._applying_timeline_h and self._timeline_is_bottom_docked():
                window_resized = self.size() != self._last_win_size
                dragging = bool(QApplication.mouseButtons() & Qt.MouseButton.LeftButton)
                if dragging and not window_resized and self.timeline_dock.height() >= 80:
                    self._timeline_h = self.timeline_dock.height()   # the user moved the separator
                else:
                    self._timeline_h_timer.start()                   # layout re-flow: restore chosen height
            self._last_win_size = self.size()
        return super().eventFilter(obj, event)

    # layout persistence
    def _layout_blob(self) -> dict:
        return {
            "geometry": bytes(self.saveGeometry()).hex(),
            "state": bytes(self.saveState()).hex(),
        }

    def _apply_layout_blob(self, blob: dict) -> bool:
        try:
            geometry = QByteArray(bytes.fromhex(blob["geometry"]))
            state = QByteArray(bytes.fromhex(blob["state"]))
        except (KeyError, ValueError):
            return False
        ok_g = self.restoreGeometry(geometry)
        ok_s = self.restoreState(state)
        if ok_s:
            self._on_timeline_dock_area_changed()
        return ok_g or ok_s

    def _restore_layout_from_settings(self):
        s = QSettings("CZEditor", "CZEditor")
        geometry = s.value("layout/geometry")
        state = s.value("layout/state")
        if geometry is None or state is None:
            return
        try:
            self._apply_layout_blob({"geometry": bytes(geometry).hex(), "state": bytes(state).hex()})
        except (TypeError, ValueError):
            pass

    def _save_layout_to_settings(self):
        s = QSettings("CZEditor", "CZEditor")
        s.setValue("layout/geometry", self.saveGeometry())
        s.setValue("layout/state", self.saveState())
        s.setValue("layout/timeline_height", int(self._timeline_h))

    def on_export_layout(self):
        path, _ = QFileDialog.getSaveFileName(
            self, i18n.tr("dialog.exportLayout.title"), os.path.join(self._last_root, "czeditor_layout.json"),
            i18n.tr("dialog.exportLayout.filter"),
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._layout_blob(), f)
            self.statusBar().showMessage(i18n.tr("status.layoutExported").format(path=path), 5000)
        except OSError as e:
            QMessageBox.warning(self, i18n.tr("dialog.exportLayout.title"), i18n.tr("dialog.exportLayout.errWrite").format(path=path, error=e))

    def on_import_layout(self):
        path, _ = QFileDialog.getOpenFileName(self, i18n.tr("dialog.importLayout.title"), self._last_root, i18n.tr("dialog.exportLayout.filter"))
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            if not self._apply_layout_blob(blob):
                raise ValueError(i18n.tr("dialog.importLayout.errInvalidFile"))
            self.statusBar().showMessage(i18n.tr("status.layoutImported").format(path=path), 5000)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            QMessageBox.warning(self, i18n.tr("dialog.importLayout.title"), i18n.tr("dialog.importLayout.errLoad").format(path=path, error=e))

    def _update_title(self):
        name = self.controller.project.name
        star = "*" if self.controller.is_dirty else ""
        loc = f" - {self.controller.project_dir}" if self.controller.project_dir else " " + i18n.tr("windowTitle.unsaved")
        self.setWindowTitle(f"{i18n.tr('app.title')} - {name}{star}{loc}")

    def closeEvent(self, event):
        if not self._confirm_discard_if_dirty():
            event.ignore()
            return
        self._play_timer.stop()
        self._autosave_timer.stop()
        self.sound_engine.stop_all()
        if hasattr(self.sound_engine, "close"):
            self.sound_engine.close()
        self.preview_panel.shutdown()
        self._save_layout_to_settings()
        self.backend.stop()
        super().closeEvent(event)


def _is_capturing_input(w) -> bool:
    if w is None:
        return False
    if isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QKeySequenceEdit)):
        return True
    if isinstance(w, QComboBox) and w.isEditable():
        return True
    return False


def _shortcut_is_risky(seq_str: str) -> bool:
    if not seq_str:
        return False
    return "Ctrl+" not in seq_str and "Meta+" not in seq_str


class _ToolbarConfigDialog(QDialog):
    def __init__(self, action_defs, current_order, parent=None):
        super().__init__(parent)
        self.setWindowTitle(i18n.tr("toolbar.configure.title"))
        self.resize(500, 400)
        
        self.action_defs = action_defs
        self.defs_by_id = {d[0]: d for d in action_defs}
        
        layout = QHBoxLayout()
        
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel(i18n.tr("toolbar.configure.available")))
        self.list_available = QListWidget()
        left_layout.addWidget(self.list_available)
        layout.addLayout(left_layout)
        
        btn_layout = QVBoxLayout()
        btn_layout.addStretch()
        self.btn_add = QPushButton(i18n.tr("toolbar.configure.add"))
        self.btn_remove = QPushButton(i18n.tr("toolbar.configure.remove"))
        btn_layout.addWidget(self.btn_add)
        btn_layout.addWidget(self.btn_remove)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        right_layout = QVBoxLayout()
        right_layout.addWidget(QLabel(i18n.tr("toolbar.configure.current")))
        self.list_current = QListWidget()
        right_layout.addWidget(self.list_current)

        updown_layout = QHBoxLayout()
        self.btn_up = QPushButton(i18n.tr("toolbar.configure.moveUp"))
        self.btn_down = QPushButton(i18n.tr("toolbar.configure.moveDown"))
        updown_layout.addWidget(self.btn_up)
        updown_layout.addWidget(self.btn_down)
        right_layout.addLayout(updown_layout)
        layout.addLayout(right_layout)
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        
        main_vbox = QVBoxLayout(self)
        main_vbox.addLayout(layout)
        main_vbox.addWidget(buttons)
        
        self._populate(current_order)
        
        self.btn_add.clicked.connect(self._add_item)
        self.btn_remove.clicked.connect(self._remove_item)
        self.btn_up.clicked.connect(self._move_up)
        self.btn_down.clicked.connect(self._move_down)
        self.list_available.itemDoubleClicked.connect(self._add_item)
        self.list_current.itemDoubleClicked.connect(self._remove_item)
        
    def _item_text(self, item_id: str) -> str:
        d = self.defs_by_id.get(item_id)
        if d is None:
            return item_id
        return d[1] if d[1] else i18n.tr("toolbar.configure.separator")

    def _populate(self, current_order):
        for item_id in current_order:
            item = QListWidgetItem(self._item_text(item_id))
            item.setData(Qt.ItemDataRole.UserRole, item_id)
            self.list_current.addItem(item)
            
        for d in self.action_defs:
            item_id = d[0]
            if item_id.startswith("sep") or item_id not in current_order:
                item = QListWidgetItem(self._item_text(item_id))
                item.setData(Qt.ItemDataRole.UserRole, item_id)
                self.list_available.addItem(item)
                
    def _add_item(self):
        row = self.list_available.currentRow()
        if row < 0: return
        item = self.list_available.takeItem(row)
        item_id = item.data(Qt.ItemDataRole.UserRole)
        self.list_current.addItem(item)
        
        if item_id.startswith("sep"):
            new_item = QListWidgetItem(self._item_text(item_id))
            new_item.setData(Qt.ItemDataRole.UserRole, item_id)
            self.list_available.addItem(new_item)

    def _remove_item(self):
        row = self.list_current.currentRow()
        if row < 0: return
        item = self.list_current.takeItem(row)
        item_id = item.data(Qt.ItemDataRole.UserRole)
        if not item_id.startswith("sep"):
            self.list_available.addItem(item)
            
    def _move_up(self):
        row = self.list_current.currentRow()
        if row <= 0: return
        item = self.list_current.takeItem(row)
        self.list_current.insertItem(row - 1, item)
        self.list_current.setCurrentRow(row - 1)
        
    def _move_down(self):
        row = self.list_current.currentRow()
        if row < 0 or row >= self.list_current.count() - 1: return
        item = self.list_current.takeItem(row)
        self.list_current.insertItem(row + 1, item)
        self.list_current.setCurrentRow(row + 1)
        
    def result_order(self) -> list[str]:
        return [self.list_current.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list_current.count())]


_INVALID_FOLDER_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(32)}
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def _folder_name(name: str) -> str:
    out = "".join(c for c in name if c not in _INVALID_FOLDER_CHARS).strip().rstrip(". ")
    if out.upper() in _RESERVED_NAMES:
        out += "_"
    return out or "untitled_project"
