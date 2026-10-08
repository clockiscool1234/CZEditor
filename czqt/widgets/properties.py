from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QIcon, QPainter, QPixmap, QColor, QPen, QBrush
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..asset_types import is_audio as _is_audio, is_video as _is_video
from ..model import MAIN_ID, Placement, Project, SymbolDef, DEFAULT_EASE
from .source_field import SourceField
from .timeline import BezierEaseDialog
from .. import i18n
from ..imaging import is_possibly_animated
import os

ANIM_TYPES = [
    "none", "fade", "pop", "aero",
    "slide_up", "slide_down", "slide_left", "slide_right",
    "slide_out_up", "slide_out_down", "slide_out_left", "slide_out_right",
]
ANIM_DEFAULTS = {
    "none": (0.0, 0.0),
    "fade": (0.2, 0.6),
    "pop": (0.2, 0.2),
    "aero": (8 / 30, 8 / 30),
    "slide_up": (0.4, 0.4),
    "slide_down": (0.4, 0.4),
    "slide_left": (0.4, 0.4),
    "slide_right": (0.4, 0.4),
    "slide_out_up": (0.4, 0.4),
    "slide_out_down": (0.4, 0.4),
    "slide_out_left": (0.4, 0.4),
    "slide_out_right": (0.4, 0.4),
}
EASE_TYPES = ["linear", "ease_in", "ease_out", "ease_in_out", "custom"]
ANIM_SCALE_DEFAULT = 95
ANIM_TILT_DEFAULT = 25
TIMING_TYPES = ["none", "duplication", "flashing", "switch_media"]
# animations that only fade when their Fade checkbox is on
FADE_OPTIONAL_ANIMS = (
    "pop", "slide_up", "slide_down", "slide_left", "slide_right",
    "slide_out_up", "slide_out_down", "slide_out_left", "slide_out_right",
)

def _dspin(value: float, minv=-100000.0, maxv=100000.0, step=1.0, decimals=2) -> QDoubleSpinBox:
    w = QDoubleSpinBox()
    w.setRange(minv, maxv)
    w.setSingleStep(step)
    w.setDecimals(decimals)
    w.setValue(float(value or 0))
    w.setMaximumWidth(90)
    w.setKeyboardTracking(False)
    return w


def _ispin(value: int, minv=-100000, maxv=100000) -> QSpinBox:
    w = QSpinBox()
    w.setRange(minv, maxv)
    w.setValue(int(value or 0))
    w.setMaximumWidth(90)
    w.setKeyboardTracking(False)
    return w


def _combo(options, value) -> QComboBox:
    w = QComboBox()
    w.addItems(options)
    idx = w.findText(str(value))
    w.setCurrentIndex(idx if idx >= 0 else 0)
    return w


def _anim_combo(value: str) -> QComboBox:
    w = QComboBox()
    for key in ANIM_TYPES:
        w.addItem(i18n.tr("properties.anim." + key), key)
    for i in range(w.count()):
        if w.itemData(i) == value:
            w.setCurrentIndex(i)
            break
    return w


def _check(value: bool, label: str = "") -> QCheckBox:
    w = QCheckBox(label)
    w.setChecked(bool(value))
    return w


def _check_wrap(value: bool, label: str) -> tuple:
    cb = QCheckBox()
    cb.setChecked(bool(value))

    lbl = QLabel(label)
    lbl.setWordWrap(True)
    lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    lbl.mousePressEvent = lambda _ev: cb.setChecked(not cb.isChecked())

    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)
    row.addWidget(cb)
    row.addWidget(lbl, 1)
    return box, cb


def _sep(text: str = "x") -> QLabel:
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    lbl.setFixedWidth(14 if len(text) <= 1 else 22)
    lbl.setStyleSheet("color: #888; font-size: 11px; padding: 0;")
    return lbl


def _hbox(*widgets) -> QWidget:
    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    for w in widgets:
        layout.addWidget(w)
    return box


class PropertiesPanel(QWidget):
    changed = pyqtSignal()
    request_enter_scene = pyqtSignal(str)
    request_insert = pyqtSignal(str, str)
    paint_mode_changed = pyqtSignal(bool, str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.project: Optional[Project] = None
        self.project_dir: Optional[str] = None
        self._settings_scene_id = MAIN_ID
        self._selection = None
        self._auto_apply = True
        self._built = False
        self._paint_buffer = None  # dict: {"section": str, "apply": callable(Placement)}
        self._paint_mode = False
        self._painter_btns: list = []   # all live painter buttons; cleared on rebuild

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()
        outer.addWidget(self._tabs)

        # Scene Settings tab
        scene_scroll = QScrollArea()
        scene_scroll.setWidgetResizable(True)
        self._scene_scroll_content = QWidget()
        scene_scroll.setWidget(self._scene_scroll_content)
        self._scene_content_layout = QVBoxLayout(self._scene_scroll_content)
        self._scene_content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.scene_settings_box = QGroupBox(i18n.tr("properties.tabSceneSettings"))
        self._scene_content_layout.addWidget(self.scene_settings_box)
        self._scene_content_layout.addStretch(1)
        self._tabs.addTab(scene_scroll, i18n.tr("properties.tabSceneSettings"))

        # Object Properties tab
        obj_scroll = QScrollArea()
        obj_scroll.setWidgetResizable(True)
        self._obj_scroll_content = QWidget()
        obj_scroll.setWidget(self._obj_scroll_content)
        self._obj_content_layout = QVBoxLayout(self._obj_scroll_content)
        self._obj_content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.selection_box = QGroupBox(i18n.tr("properties.tabObjectProperties"))
        self._obj_content_layout.addWidget(self.selection_box)
        self._obj_content_layout.addStretch(1)
        self._tabs.addTab(obj_scroll, i18n.tr("properties.tabObjectProperties"))

        self._rebuild_scene_settings()
        self._rebuild_selection()
        self._built = True

    # auto-apply
    @property
    def auto_apply_properties(self) -> bool:
        return self._auto_apply

    @auto_apply_properties.setter
    def auto_apply_properties(self, value: bool):
        value = bool(value)
        changed = value != self._auto_apply
        self._auto_apply = value
        if changed and self._built:
            self._rebuild_scene_settings()
            self._rebuild_selection()

    def _wire(self, apply_fn, *widgets, live_text: bool = False):
        if not self._auto_apply:
            return
        for w in widgets:
            if isinstance(w, QFontComboBox):
                w.currentFontChanged.connect(lambda _f: apply_fn())
            elif isinstance(w, QComboBox):
                w.currentIndexChanged.connect(lambda _i: apply_fn())
            elif isinstance(w, QAbstractSpinBox):
                w.valueChanged.connect(lambda _v: apply_fn())
            elif isinstance(w, QCheckBox):
                w.toggled.connect(lambda _b: apply_fn())
            elif isinstance(w, QLineEdit):
                # names/ids apply when you commit them; only free text (shape text) is live
                (w.textChanged if live_text else w.editingFinished).connect(lambda *_a: apply_fn())
            elif isinstance(w, SourceField):
                w.changed.connect(apply_fn)

    def _apply_button(self, layout, text: str) -> Optional[QPushButton]:
        if self._auto_apply:
            return None
        btn = QPushButton(text)
        layout.addWidget(btn)
        return btn

    # format painter

    @property
    def is_paint_active(self) -> bool:
        return self._paint_mode

    def _activate_painter(self, section: str, apply_fn, source_btn: QPushButton):
        for b in self._painter_btns:
            if b is not source_btn:
                b.blockSignals(True)
                b.setChecked(False)
                b.blockSignals(False)
        self._paint_buffer = {"section": section, "apply": apply_fn}
        self._paint_mode = True
        self.paint_mode_changed.emit(True, section)

    def _deactivate_painter(self):
        for b in self._painter_btns:
            b.blockSignals(True)
            b.setChecked(False)
            b.blockSignals(False)
        self._paint_buffer = None
        self._paint_mode = False
        self.paint_mode_changed.emit(False, "")

    def apply_paint(self, placement: Placement) -> bool:
        if not self._paint_buffer:
            return False
        try:
            self._paint_buffer["apply"](placement)
        finally:
            self._deactivate_painter()
        self.changed.emit()
        return True

    def _find_placement(self, placement_id: str) -> Optional[Placement]:
        if not self.project:
            return None
        for scene in self.project.scenes.values():
            for pl in scene.placements:
                if pl.id == placement_id:
                    return pl
        return None

    def refresh_selection(self):
        if not self._selection:
            return
        if self._selection[0] == "placement":
            fresh = self._find_placement(self._selection[1].id)
            if fresh is None:
                self._selection = None
            else:
                self._selection = ("placement", fresh)
        self._rebuild_selection()

    # API
    def set_project(self, project: Project):
        self.project = project
        self._settings_scene_id = MAIN_ID
        self._selection = None
        self._rebuild_scene_settings()

    def set_project_dir(self, project_dir):
        self.project_dir = project_dir
        self._rebuild_scene_settings()
        self._rebuild_selection()

    def show_scene_settings(self, scene_id: str):
        self._settings_scene_id = scene_id
        self._rebuild_scene_settings()

    def show_placement(self, placement: Optional[Placement], scene_id: str):
        if placement is None:
            self.clear_selection()
            return
        self._selection = ("placement", placement)
        self._rebuild_selection()
        self._tabs.setCurrentIndex(1)

    def show_asset(self, kind: str, obj_id: str):
        self._selection = ("asset", kind, obj_id)
        self._rebuild_selection()
        self._tabs.setCurrentIndex(1)

    def clear_selection(self):
        self._selection = None
        self._rebuild_selection()
        self._tabs.setCurrentIndex(0)

    # scene settings
    def _rebuild_scene_settings(self):
        box = self.scene_settings_box
        _clear_group(box)
        layout = QFormLayout(box)
        if not self.project:
            return
        scene = self.project.scenes.get(self._settings_scene_id)
        if not scene:
            return
        layout.addRow(QLabel(i18n.tr("properties.editingScene").format(name=scene.name)))

        is_img = (scene.background or "").startswith("img:")
        is_transp = (scene.background or "") == "transparent"
        if is_transp:
            bg_type = "Transparent"
        elif is_img:
            bg_type = "Image"
        else:
            bg_type = "Color"
        type_combo = QComboBox()
        for _key, _label in (("Color", i18n.tr("properties.typeColor")), ("Image", i18n.tr("properties.typeImage")), ("Transparent", i18n.tr("properties.typeTransparent"))):
            type_combo.addItem(_label, _key)
        type_combo.setCurrentIndex(max(0, type_combo.findData(bg_type)))
        layout.addRow(i18n.tr("properties.labelType"), type_combo)

        color_row = QWidget()
        color_layout = QHBoxLayout(color_row)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_btn = QPushButton()
        color_btn.setFixedWidth(60)
        current_color = "#000000" if (is_img or is_transp) else (scene.background or "#000000")
        color_btn.setStyleSheet(f"background-color: {current_color};")
        color_state = {"color": current_color}

        def apply_scene():
            t = type_combo.currentData()
            if t == "Image":
                # auto mode: until an image is picked keep the current background
                if img_field.value() or not self._auto_apply:
                    scene.background = "img:" + (img_field.value() or "")
            elif t == "Transparent":
                scene.background = "transparent"
            else:
                scene.background = color_state["color"]
            scene.inner_res_width = inner_w.value()
            scene.inner_res_height = inner_h.value()
            self.changed.emit()

        def pick_color():
            from PyQt6.QtGui import QColor
            c = QColorDialog.getColor(QColor(color_state["color"]), self)
            if c.isValid():
                color_state["color"] = c.name()
                color_btn.setStyleSheet(f"background-color: {c.name()};")
                if self._auto_apply:
                    apply_scene()

        color_btn.clicked.connect(pick_color)
        color_layout.addWidget(color_btn)

        img_field = SourceField(self.project, self.project_dir, "image", scene.background[4:] if is_img else "")

        def sync_bg_row():
            t = type_combo.currentData()
            color_btn.setVisible(t == "Color")
            img_field.setVisible(t == "Image")

        color_layout.addWidget(img_field)
        layout.addRow(i18n.tr("properties.labelBackground"), color_row)
        type_combo.currentIndexChanged.connect(sync_bg_row)
        if self._auto_apply:
            type_combo.currentIndexChanged.connect(lambda _i: apply_scene())
        sync_bg_row()

        inner_w = _ispin(scene.inner_res_width, 0, 100000)
        inner_h = _ispin(scene.inner_res_height, 0, 100000)
        inner_w.setSpecialValueText("proj")
        inner_h.setSpecialValueText("proj")
        layout.addRow(i18n.tr("properties.labelInnerRes"), _hbox(inner_w, _sep(), inner_h))
        _tip_rows(layout, i18n.tr("properties.hintInnerRes"))


        self._wire(apply_scene, type_combo, img_field, inner_w, inner_h)
        apply_btn = self._apply_button(layout, i18n.tr("scene.applySettings"))
        if apply_btn:
            apply_btn.clicked.connect(apply_scene)

        enter_btn = QPushButton(i18n.tr("properties.enterScene"))
        enter_btn.clicked.connect(lambda: self.request_enter_scene.emit(scene.id))
        layout.addRow(enter_btn)

    # selection
    def _rebuild_selection(self):
        self._painter_btns = []
        box = self.selection_box
        _clear_group(box)
        layout = QVBoxLayout(box)
        if not self.project or not self._selection:
            layout.addWidget(QLabel(i18n.tr("properties.nothingSelected")))
            return

        kind0 = self._selection[0]
        if kind0 == "placement":
            placement = self._selection[1]
            if placement.kind == "symbol":
                self._build_symbol(layout, self.project.symbol_defs.get(placement.ref), placement)
            elif placement.kind == "sound":
                self._build_sound(layout, placement)
            elif placement.kind == "video":
                self._build_video_placement(layout, placement)
            elif placement.kind == "scene":
                self._build_scene_asset(layout, self.project.scenes.get(placement.ref), placement)
            elif placement.kind == "image":
                self._build_image_placement(layout, placement)
            elif placement.kind == "shape":
                self._build_shape_placement(layout, placement)
        elif kind0 == "asset":
            _, akind, obj_id = self._selection
            if akind == "symbol":
                self._build_symbol(layout, self.project.symbol_defs.get(obj_id), None)
            elif akind == "scene":
                self._build_scene_asset(layout, self.project.scenes.get(obj_id), None)
            elif akind == "source":
                self._build_source(layout, obj_id)
        elif kind0 == "source":
            self._build_source(layout, self._selection[1])

    # SYMBOL
    def _build_symbol(self, layout: QVBoxLayout, edef: Optional[SymbolDef], placement: Optional[Placement]):
        if edef is None:
            layout.addWidget(QLabel(i18n.tr("properties.notFound.symbol")))
            return
        form = QFormLayout()
        layout.addLayout(form)
        form.addRow(_section(i18n.tr("properties.sectionSymbol")))
        name_edit = QLineEdit(edef.name)
        form.addRow(i18n.tr("properties.labelName"), name_edit)
        cls_edit = QLineEdit(edef.elem_class)
        form.addRow(i18n.tr("properties.labelClass"), cls_edit)
        id_edit = QLineEdit(edef.elem_id)
        form.addRow(i18n.tr("properties.labelID"), id_edit)

        active_field = SourceField(self.project, self.project_dir, "image", edef.active_image)
        form.addRow(i18n.tr("properties.labelActiveImg"), active_field)
        inactive_field = SourceField(self.project, self.project_dir, "image", edef.inactive_image)
        form.addRow(i18n.tr("properties.labelInactiveImg"), inactive_field)

        w_spin = _dspin(edef.width, 0, 100000, 1, 0)
        h_spin = _dspin(edef.height, 0, 100000, 1, 0)
        form.addRow(i18n.tr("properties.labelDefaultWH"), _hbox(w_spin, _sep(), h_spin))

        dim_check = _check(edef.dim_inactive, i18n.tr("properties.dimInactivePlacements"))
        form.addRow("", dim_check)
        dim_amt = _dspin(edef.dim_amount, 0, 1, 0.05, 2)
        form.addRow(i18n.tr("properties.labelBrightness"), dim_amt)

        anim_getter = None
        if not placement:
            anim_getter = self._build_anim_block(
                form, edef.anim_in, edef.anim_out, edef.anim_in_dur, edef.anim_out_dur,
                edef.anim_scale, edef.anim_tilt_deg,
                on_change=(lambda: save()) if self._auto_apply else None,
                in_fade=edef.anim_in_fade, out_fade=edef.anim_out_fade,
            )

        def save():
            edef.name = name_edit.text() or edef.name
            edef.elem_class = cls_edit.text() or "generic"
            new_elem_id = id_edit.text() or "sym1"
            if new_elem_id != edef.elem_id:
                collision = any(
                    s is not edef and s.elem_id == new_elem_id
                    for s in self.project.symbol_defs.values()
                )
                if collision:
                    QMessageBox.warning(
                        self, i18n.tr("properties.symbolIdTitle"),
                        i18n.tr("properties.symbolIdUsed").format(id=new_elem_id),
                    )
                    id_edit.setText(edef.elem_id)
                    new_elem_id = edef.elem_id
            edef.elem_id = new_elem_id
            edef.active_image = active_field.value()
            edef.inactive_image = inactive_field.value()
            edef.width = w_spin.value()
            edef.height = h_spin.value()
            edef.dim_inactive = dim_check.isChecked()
            edef.dim_amount = dim_amt.value()
            if anim_getter and not placement:
                anim = anim_getter()
                edef.anim_in, edef.anim_out = anim["in"], anim["out"]
                edef.anim_in_dur, edef.anim_out_dur = anim["in_dur"], anim["out_dur"]
                edef.anim_scale, edef.anim_tilt_deg = anim["scale"], anim["tilt"]
                edef.anim_in_fade, edef.anim_out_fade = anim["in_fade"], anim["out_fade"]
                for scene in self.project.scenes.values():
                    for pl in scene.placements:
                        if pl.kind == "symbol" and pl.ref == edef.id and not pl.anim_custom:
                            pl.anim_in, pl.anim_out = edef.anim_in, edef.anim_out
                            pl.anim_in_dur, pl.anim_out_dur = edef.anim_in_dur, edef.anim_out_dur
                            pl.anim_scale, pl.anim_tilt_deg = edef.anim_scale, edef.anim_tilt_deg
                            pl.anim_in_fade, pl.anim_out_fade = edef.anim_in_fade, edef.anim_out_fade
            if edef.elem_class and edef.elem_class not in self.project.classes:
                self.project.classes.append(edef.elem_class)
            self.changed.emit()

        self._wire(save, name_edit, cls_edit, id_edit, active_field, inactive_field,
                   w_spin, h_spin, dim_check, dim_amt)
        save_btn = self._apply_button(layout, i18n.tr("properties.saveChanges"))
        if save_btn:
            save_btn.clicked.connect(save)

        if placement:
            self._build_placement_geometry(layout, placement)
        else:
            insert_btn = QPushButton(i18n.tr("properties.insertAtPlayhead"))
            insert_btn.clicked.connect(lambda: self.request_insert.emit("symbol", edef.id))
            layout.addWidget(insert_btn)

    # SOUND
    def _build_sound(self, layout: QVBoxLayout, placement: Placement):
        form = QFormLayout()
        layout.addLayout(form)
        form.addRow(_section(i18n.tr("properties.sectionSound")))
        ref_field = SourceField(self.project, self.project_dir, "audio", placement.ref)
        form.addRow(i18n.tr("properties.labelSourceFile"), ref_field)

        save_btn = self._apply_button(layout, i18n.tr("properties.saveChanges"))

        self._build_placement_geometry(layout, placement)

        pin_check = _check(placement.pinned, i18n.tr("properties.pinnedToTimeline"))
        mute_check = _check(placement.muted, i18n.tr("properties.muted"))
        _loop_wrap, loop_check = _check_wrap(placement.loop, i18n.tr("properties.loopRepeat"))
        layout.addWidget(pin_check)
        layout.addWidget(mute_check)
        layout.addWidget(_loop_wrap)

        def save_sound():
            placement.ref = ref_field.value() or placement.ref
            placement.pinned = pin_check.isChecked()
            placement.muted = mute_check.isChecked()
            placement.loop = loop_check.isChecked()
            self.changed.emit()

        self._wire(save_sound, ref_field, pin_check, mute_check, loop_check)
        if save_btn:
            save_btn.clicked.connect(save_sound)

    # IMAGE
    def _build_image_placement(self, layout: QVBoxLayout, placement: Placement):
        form = QFormLayout()
        layout.addLayout(form)
        form.addRow(_section(i18n.tr("properties.sectionImagePlacement")))
        ref_field = SourceField(self.project, self.project_dir, "image", placement.ref)
        form.addRow(i18n.tr("properties.labelSourceFile"), ref_field)

        _loop_wrap, loop_check = _check_wrap(placement.loop, i18n.tr("properties.loopAnimation"))
        layout.addWidget(_loop_wrap)
        _tip(_loop_wrap, i18n.tr("properties.hintLoopAnimation"))

        def _sync_loop_visibility():
            _loop_wrap.setVisible(is_possibly_animated(ref_field.value()))

        ref_field.changed.connect(_sync_loop_visibility)
        _sync_loop_visibility()

        def save_image():
            placement.ref = ref_field.value() or ""
            placement.loop = loop_check.isChecked()
            self.changed.emit()

        self._wire(save_image, ref_field, loop_check)
        save_btn = self._apply_button(layout, i18n.tr("properties.updateSource"))
        if save_btn:
            save_btn.clicked.connect(save_image)

        self._build_placement_geometry(layout, placement)

    # VIDEO
    def _build_video_placement(self, layout: QVBoxLayout, placement: Placement):
        form = QFormLayout()
        layout.addLayout(form)
        form.addRow(_section(i18n.tr("properties.sectionVideoPlacement")))

        ref_field = SourceField(self.project, self.project_dir, "video", placement.ref)
        form.addRow(i18n.tr("properties.labelSourceFile"), ref_field)

        # volume
        volume_spin = _dspin(
            getattr(placement, "volume", 1.0) or 1.0,
            minv=0.0, maxv=4.0, step=0.05, decimals=2,
        )
        form.addRow(i18n.tr("properties.labelVolume"), volume_spin)

        # playback speed
        speed_spin = _dspin(
            getattr(placement, "speed", 1.0) or 1.0,
            minv=0.05, maxv=16.0, step=0.05, decimals=2,
        )
        form.addRow(i18n.tr("properties.labelSpeed"), speed_spin)
        _tip_rows(form, i18n.tr("properties.hintSpeed"))

        mute_check = _check(placement.muted, i18n.tr("properties.muted"))
        _loop_wrap, loop_check = _check_wrap(placement.loop, i18n.tr("properties.loopRepeat"))
        layout.addWidget(mute_check)
        layout.addWidget(_loop_wrap)

        def save_video():
            placement.ref = ref_field.value() or ""
            placement.volume = max(0.0, volume_spin.value())
            placement.speed = max(0.05, speed_spin.value())
            placement.muted = mute_check.isChecked()
            placement.loop = loop_check.isChecked()
            self.changed.emit()

        self._wire(save_video, ref_field, volume_spin, speed_spin, mute_check, loop_check)
        av_btn = self._apply_button(layout, i18n.tr("properties.applyVideoSettings"))
        if av_btn:
            av_btn.clicked.connect(save_video)

        self._build_placement_geometry(layout, placement)
    def _build_shape_placement(self, layout: QVBoxLayout, placement: Placement):
        form = QFormLayout()
        layout.addLayout(form)
        form.addRow(_section(i18n.tr("properties.sectionShape")))

        stype_combo = _combo(["rect", "ellipse", "triangle", "text", "polygon"], placement.shape_type or "rect")
        form.addRow(i18n.tr("properties.labelShapeType"), stype_combo)

        fill_btn = QPushButton()
        fill_btn.setFixedWidth(60)
        fill_color = {"c": placement.shape_color or "#4488ff"}
        fill_btn.setStyleSheet(f"background-color: {fill_color['c']};")
        border_w_spin = _dspin(placement.shape_border_width or 0.0, 0, 100, 1, 1)
        text_edit = QLineEdit(placement.shape_text or "")
        font_combo = QFontComboBox()
        font_combo.setCurrentFont(font_combo.currentFont() if not getattr(placement, 'shape_font_family', '') else __import__('PyQt6.QtGui', fromlist=['QFont']).QFont(placement.shape_font_family))
        font_size = _dspin(placement.shape_font_size or 24.0, 4, 1000, 1, 1)

        def pick_fill():
            from PyQt6.QtGui import QColor
            c = QColorDialog.getColor(QColor(fill_color["c"]), self)
            if c.isValid():
                fill_color["c"] = c.name()
                fill_btn.setStyleSheet(f"background-color: {c.name()};")
                if self._auto_apply:
                    apply_shape()
        fill_btn.clicked.connect(pick_fill)
        form.addRow(i18n.tr("properties.labelFillColor"), fill_btn)

        border_btn = QPushButton()
        border_btn.setFixedWidth(60)
        border_color = {"c": placement.shape_border_color or "transparent"}
        border_btn.setStyleSheet(f"background-color: {border_color['c']};")
        def pick_border():
            from PyQt6.QtGui import QColor
            c = QColorDialog.getColor(QColor(border_color["c"]) if border_color["c"] != "transparent" else Qt.GlobalColor.transparent, self)
            if c.isValid():
                border_color["c"] = c.name()
                border_btn.setStyleSheet(f"background-color: {c.name()};")
                if self._auto_apply:
                    apply_shape()
        border_btn.clicked.connect(pick_border)
        form.addRow(i18n.tr("properties.labelBorder"), _hbox(border_btn, _sep(i18n.tr("properties.sepW")), border_w_spin))

        _auto_fit_wrap, auto_fit_check = _check_wrap(
            getattr(placement, 'shape_text_auto_fit', False), i18n.tr("properties.autoFitText"))

        font_row = _labeled_row(i18n.tr("properties.labelFontSize"), _hbox(font_combo, font_size))
        text_row = _labeled_row(i18n.tr("properties.labelText"), text_edit)
        form.addRow(text_row[0], text_row[1])
        form.addRow("", _auto_fit_wrap)
        form.addRow(font_row[0], font_row[1])

        def sync_font_enabled():
            font_size.setEnabled(not auto_fit_check.isChecked())
            font_combo.setEnabled(not auto_fit_check.isChecked())

        auto_fit_check.toggled.connect(lambda _: sync_font_enabled())

        def sync_visibility():
            is_text = stype_combo.currentText() == "text"
            text_row[0].setVisible(is_text)
            text_row[1].setVisible(is_text)
            _auto_fit_wrap.setVisible(is_text)
            font_row[0].setVisible(is_text)
            font_row[1].setVisible(is_text)
            if is_text:
                sync_font_enabled()
        stype_combo.currentIndexChanged.connect(sync_visibility)
        sync_visibility()

        def apply_shape():
            placement.shape_type = stype_combo.currentText()
            placement.shape_color = fill_color["c"]
            placement.shape_border_color = border_color["c"] if border_color["c"] != "transparent" else ""
            placement.shape_border_width = border_w_spin.value()
            placement.shape_text = text_edit.text()
            placement.shape_font_family = font_combo.currentFont().family()
            placement.shape_font_size = font_size.value()
            placement.shape_text_auto_fit = auto_fit_check.isChecked()
            self.changed.emit()

        self._wire(apply_shape, stype_combo, border_w_spin, text_edit, font_combo, font_size, auto_fit_check, live_text=True)
        save_btn = self._apply_button(layout, i18n.tr("properties.updateShape"))
        if save_btn:
            save_btn.clicked.connect(apply_shape)

        self._build_placement_geometry(layout, placement)

    # SCENE
    def _build_scene_asset(self, layout: QVBoxLayout, scene, placement: Optional[Placement]):
        if scene is None:
            layout.addWidget(QLabel(i18n.tr("properties.notFound.scene")))
            return
        form = QFormLayout()
        layout.addLayout(form)
        form.addRow(_section(i18n.tr("properties.sectionScene")))
        name_edit = QLineEdit(scene.name)
        form.addRow(i18n.tr("properties.labelName"), name_edit)

        # inactive state: applies whenever this scene is placed inside another scene
        form.addRow(_section(i18n.tr("properties.sectionInactiveState")))
        _has_inactive_wrap, has_inactive_check = _check_wrap(
            getattr(scene, "has_inactive", False), i18n.tr("properties.enableInactiveState"))
        form.addRow(_has_inactive_wrap)

        class_edit = QLineEdit(getattr(scene, "elem_class", "") or "")
        class_edit.setPlaceholderText(i18n.tr("properties.inactiveClassPlaceholder"))
        class_row = _labeled_row(i18n.tr("properties.labelClass"), class_edit)
        form.addRow(class_row[0], class_row[1])

        cur_image = getattr(scene, "inactive_image", "") or ""
        cur_ref = getattr(scene, "inactive_ref", "") or ""
        mode_combo = QComboBox()
        mode_combo.addItem(i18n.tr("properties.inactiveModeDim"), "dim")
        mode_combo.addItem(i18n.tr("properties.inactiveModeScene"), "scene")
        mode_combo.addItem(i18n.tr("properties.inactiveModeImage"), "image")
        mode_combo.setCurrentIndex(1 if (cur_ref and not cur_image) else (2 if cur_image else 0))
        mode_row = _labeled_row(i18n.tr("properties.labelInactiveShows"), mode_combo)
        form.addRow(mode_row[0], mode_row[1])

        inactive_scene_combo = QComboBox()
        for sid, sc in (self.project.scenes.items() if self.project else []):
            if sid == scene.id or sid == MAIN_ID:
                continue
            inactive_scene_combo.addItem(sc.name or sid, sid)
        for i in range(inactive_scene_combo.count()):
            if inactive_scene_combo.itemData(i) == cur_ref:
                inactive_scene_combo.setCurrentIndex(i)
                break
        inactive_ref_row = _labeled_row(i18n.tr("properties.labelInactiveScene"), inactive_scene_combo)
        form.addRow(inactive_ref_row[0], inactive_ref_row[1])

        inactive_img_field = SourceField(self.project, self.project_dir, "image", cur_image)
        inactive_img_row = _labeled_row(i18n.tr("properties.labelInactiveImg"), inactive_img_field)
        form.addRow(inactive_img_row[0], inactive_img_row[1])

        dim_check = _check(getattr(scene, "dim_inactive", True), i18n.tr("properties.dimWhenInactive"))
        dim_row = _labeled_row(i18n.tr("properties.labelDim"), dim_check)
        form.addRow(dim_row[0], dim_row[1])
        dim_spin = _dspin(getattr(scene, "dim_amount", 0.5), 0.0, 1.0, 0.05, 2)
        dim_spin_row = _labeled_row(i18n.tr("properties.labelDimAmount"), dim_spin)
        form.addRow(dim_spin_row[0], dim_spin_row[1])
        _inactive_tip = i18n.tr("properties.hintInactiveState")
        _tip(_has_inactive_wrap, _inactive_tip)
        _tip(class_row[0], _inactive_tip)
        _tip(class_row[1], _inactive_tip)

        def _sync_inactive_visibility():
            enabled = has_inactive_check.isChecked()
            mode = mode_combo.currentData()
            for w in (class_row[0], class_row[1], mode_row[0], mode_row[1],
                      dim_row[0], dim_row[1], dim_spin_row[0], dim_spin_row[1]):
                w.setVisible(enabled)
            for w in inactive_ref_row:
                w.setVisible(enabled and mode == "scene")
            for w in inactive_img_row:
                w.setVisible(enabled and mode == "image")

        has_inactive_check.stateChanged.connect(lambda _: _sync_inactive_visibility())
        mode_combo.currentIndexChanged.connect(lambda _i: _sync_inactive_visibility())
        _sync_inactive_visibility()

        def save():
            scene.name = name_edit.text() or scene.name
            scene.has_inactive = has_inactive_check.isChecked()
            scene.elem_class = class_edit.text().strip()
            mode = mode_combo.currentData()
            scene.inactive_ref = (inactive_scene_combo.currentData() or "") if mode == "scene" else ""
            scene.inactive_image = inactive_img_field.value() if mode == "image" else ""
            scene.dim_inactive = dim_check.isChecked()
            scene.dim_amount = dim_spin.value()
            self.changed.emit()

        self._wire(save, name_edit, has_inactive_check, class_edit, mode_combo,
                   inactive_scene_combo, inactive_img_field, dim_check, dim_spin)
        save_btn = self._apply_button(layout, i18n.tr("properties.saveChanges"))
        if save_btn:
            save_btn.clicked.connect(save)

        enter_btn = QPushButton(i18n.tr("properties.enterScene"))
        enter_btn.clicked.connect(lambda: self.request_enter_scene.emit(scene.id))
        layout.addWidget(enter_btn)

        if placement:
            self._build_placement_geometry(layout, placement)
        else:
            can_place = self._settings_scene_id != MAIN_ID and scene.id != self._settings_scene_id
            insert_btn = QPushButton(i18n.tr("properties.insertAtPlayhead"))
            insert_btn.setEnabled(can_place)
            insert_btn.clicked.connect(lambda: self.request_insert.emit("scene", scene.id))
            layout.addWidget(insert_btn)

    # SOURCE
    def _build_source(self, layout: QVBoxLayout, uuid: str):
        form = QFormLayout()
        layout.addLayout(form)
        form.addRow(_section(i18n.tr("properties.sectionSource")))
        name = self.project.sources.get(uuid, "(unknown)")
        form.addRow(i18n.tr("properties.labelName"), QLabel(name))
        is_audio = _is_audio(name)
        is_video = _is_video(name)
        type_label = i18n.tr("properties.typeAudio") if is_audio else (i18n.tr("properties.typeVideo") if is_video else i18n.tr("properties.typeImage"))
        form.addRow(i18n.tr("properties.labelType"), QLabel(type_label))

        if is_audio:
            insert_btn = QPushButton(i18n.tr("properties.insertAsSoundAtPlayhead"))
            insert_btn.clicked.connect(lambda: self.request_insert.emit("sound", uuid))
            layout.addWidget(insert_btn)
        elif is_video:
            insert_btn = QPushButton(i18n.tr("properties.insertAsVideoAtPlayhead"))
            insert_btn.clicked.connect(lambda: self.request_insert.emit("video", uuid))
            layout.addWidget(insert_btn)

        _tip_rows(form, i18n.tr("properties.hintSourceManage"), 2)

    # placement geometry
    def _build_placement_geometry(self, layout: QVBoxLayout, p: Placement):
        is_sound_only = p.kind == "sound"   # only pure audio has no position/size
        form = QFormLayout()
        layout.addLayout(form)
        _place_section_label = i18n.tr("properties.sectionPlacement")
        _place_section_row, _place_painter_btn = _painter_section_row(_place_section_label)
        self._painter_btns.append(_place_painter_btn)
        form.addRow(_place_section_row)
        track_spin = _ispin(p.track, 0, 999)
        start_spin = _dspin(p.start, 0, 100000, 0.1, 2)
        end_spin = _dspin(p.end, 0, 100000, 0.1, 2)
        form.addRow(i18n.tr("properties.labelTrack"), track_spin)
        form.addRow(i18n.tr("properties.labelStart"), start_spin)
        form.addRow(i18n.tr("properties.labelEnd"), end_spin)

        offset_spin = None
        if p.kind in ("scene", "sound", "image", "video"):
            offset_spin = _dspin(p.src_offset, 0, 100000, 0.1, 2)
            form.addRow(i18n.tr("properties.labelContentOffset"), offset_spin)
            if p.kind == "image":
                _tip_rows(form, i18n.tr("properties.hintContentOffsetImage"))
            elif p.kind == "video":
                _tip_rows(form, i18n.tr("properties.hintContentOffsetVideo"))
            else:
                _tip_rows(form, i18n.tr("properties.hintContentOffsetGenericFmt").format(kind=p.kind))

        x_spin = y_spin = w_spin = h_spin = None
        anim_getter = None
        timing_getter = None
        rotation_spin = None
        if not is_sound_only:
            x_spin = _dspin(p.x, -100000, 100000, 1, 0)
            y_spin = _dspin(p.y, -100000, 100000, 1, 0)
            w_spin = _dspin(p.w, 0, 100000, 1, 0)
            h_spin = _dspin(p.h, 0, 100000, 1, 0)
            rotation_spin = _dspin(getattr(p, "rotation", 0.0), -360, 360, 1, 1)
            opacity_spin = _dspin(getattr(p, "opacity", 1.0), 0.0, 1.0, 0.05, 2)
            form.addRow(i18n.tr("properties.labelX"), x_spin)
            form.addRow(i18n.tr("properties.labelY"), y_spin)
            form.addRow(i18n.tr("properties.labelW"), w_spin)
            form.addRow(i18n.tr("properties.labelH"), h_spin)
            form.addRow(i18n.tr("properties.labelRotation"), rotation_spin)
            form.addRow(i18n.tr("properties.labelOpacity"), opacity_spin)
            
            # 3D
            _3d_section_row, _3d_painter_btn = _painter_section_row(i18n.tr("properties.section3D"))
            self._painter_btns.append(_3d_painter_btn)
            form.addRow(_3d_section_row)
            enable_3d_check = _check(getattr(p, "enable_3d", False))
            form.addRow(i18n.tr("properties.label3DEnable"), enable_3d_check)
            tx_spin = _dspin(getattr(p, "translate_3d_x", 0.0), -100000, 100000, 1, 1)
            ty_spin = _dspin(getattr(p, "translate_3d_y", 0.0), -100000, 100000, 1, 1)
            tz_spin = _dspin(getattr(p, "translate_3d_z", 0.0), -100000, 100000, 1, 1)
            rx_spin = _dspin(getattr(p, "rotate_3d_x", 0.0), -360, 360, 1, 1)
            ry_spin = _dspin(getattr(p, "rotate_3d_y", 0.0), -360, 360, 1, 1)
            rz_spin = _dspin(getattr(p, "rotate_3d_z", 0.0), -360, 360, 1, 1)
            form.addRow(i18n.tr("properties.label3DTranslateXY"), _hbox(tx_spin, ty_spin))
            form.addRow(i18n.tr("properties.label3DTranslateZ"), tz_spin)
            form.addRow(i18n.tr("properties.label3DRotateXY"), _hbox(rx_spin, ry_spin))
            form.addRow(i18n.tr("properties.label3DRotateZ"), rz_spin)
            
            # Chroma Key
            if p.kind in ("image", "video", "scene", "symbol"):
                _ck_section_row, _ck_painter_btn = _painter_section_row(i18n.tr("properties.sectionChromaKey"))
                self._painter_btns.append(_ck_painter_btn)
                form.addRow(_ck_section_row)
                ck_enable_check = _check(getattr(p, "chroma_key_enabled", False))
                form.addRow(i18n.tr("properties.labelChromaEnable"), ck_enable_check)
                ck_color_edit = QLineEdit(getattr(p, "chroma_key_color", "#00ff00"))
                ck_color_btn = QPushButton(i18n.tr("properties.pick"))
                def _pick_ck_color():
                    from PyQt6.QtWidgets import QColorDialog
                    from PyQt6.QtGui import QColor
                    c = QColorDialog.getColor(QColor(ck_color_edit.text()), form.parentWidget(), i18n.tr("properties.pickChromaTitle"))
                    if c.isValid():
                        ck_color_edit.setText(c.name())
                        if self._auto_apply: apply_geo()
                ck_color_btn.clicked.connect(_pick_ck_color)
                form.addRow(i18n.tr("properties.labelChromaKeyColor"), _hbox(ck_color_edit, ck_color_btn))
                ck_sim_spin = _dspin(getattr(p, "chroma_key_similarity", 0.3), 0.0, 1.0, 0.05, 2)
                ck_smooth_spin = _dspin(getattr(p, "chroma_key_smoothness", 0.1), 0.0, 1.0, 0.05, 2)
                form.addRow(i18n.tr("properties.labelChromaSimilarity"), ck_sim_spin)
                form.addRow(i18n.tr("properties.labelChromaSmoothness"), ck_smooth_spin)

        anim_getter = None
        timing_getter = None
        crop_getter = None

        spins = {"track": track_spin, "start": start_spin, "end": end_spin}
        if offset_spin is not None:
            spins["offset"] = offset_spin
        if not is_sound_only:
            spins.update(x=x_spin, y=y_spin, w=w_spin, h=h_spin, opacity=opacity_spin)
            if rotation_spin is not None:
                spins["rotation"] = rotation_spin
            spins.update(tx=tx_spin, ty=ty_spin, tz=tz_spin, rx=rx_spin, ry=ry_spin, rz=rz_spin)
            if p.kind in ("image", "video", "scene", "symbol"):
                spins.update(ck_sim=ck_sim_spin, ck_smooth=ck_smooth_spin)
        shown = {k: sp.value() for k, sp in spins.items()}

        def touched(key: str) -> bool:
            return key in spins and abs(spins[key].value() - shown[key]) > 1e-9

        def show(key: str, value: float):
            sp = spins.get(key)
            if sp is not None:
                sp.blockSignals(True)
                sp.setValue(value)
                sp.blockSignals(False)
                shown[key] = sp.value()

        def apply_geo():
            new_start = start_spin.value() if touched("start") else p.start
            new_end = end_spin.value() if touched("end") else p.end
            slide = touched("start") and not touched("end") and new_start >= p.end
            if slide:
                new_end = new_start + (p.end - p.start)
                
            # bound media duration if looping is disabled
            if p.kind in ("sound", "video") and not getattr(p, "loop", False) and self.project_dir and p.ref:
                from ..audio_probe import probe_duration_seconds
                media_dur = None
                path = os.path.join(self.project_dir, p.ref)
                if p.kind == "sound":
                    media_dur = probe_duration_seconds(path)
                elif p.kind == "video":
                    try:
                        from ..video import probe_video_duration
                        media_dur = probe_video_duration(path)
                    except Exception:
                        pass
                
                if media_dur and media_dur > 0:
                    speed = getattr(p, "speed", 1.0)
                    speed = speed if speed and speed > 0 else 1.0
                    max_dur = media_dur / speed
                    if new_end - new_start > max_dur:
                        new_end = new_start + max_dur

            if new_end <= new_start:
                show("start", p.start)
                show("end", p.end)
                return
            old_anim = (p.anim_in, p.anim_out, p.anim_in_dur, p.anim_out_dur, p.anim_scale, p.anim_tilt_deg,
                        p.anim_in_fade, p.anim_out_fade)
            if touched("track"):
                p.track = track_spin.value()
            if touched("start") or slide:
                p.start = new_start
            if touched("end") or slide:
                p.end = new_end
            if offset_spin is not None and touched("offset"):
                p.src_offset = max(0.0, offset_spin.value())
            if not is_sound_only:
                if touched("x"):
                    p.x = round(x_spin.value())
                if touched("y"):
                    p.y = round(y_spin.value())
                if touched("w"):
                    p.w = max(10, round(w_spin.value()))
                if touched("h"):
                    p.h = max(10, round(h_spin.value()))
                if rotation_spin is not None and touched("rotation"):
                    p.rotation = rotation_spin.value()
                if touched("opacity"):
                    p.opacity = opacity_spin.value()
                    
                p.enable_3d = enable_3d_check.isChecked()
                p.translate_3d_x = tx_spin.value()
                p.translate_3d_y = ty_spin.value()
                p.translate_3d_z = tz_spin.value()
                p.rotate_3d_x = rx_spin.value()
                p.rotate_3d_y = ry_spin.value()
                p.rotate_3d_z = rz_spin.value()
                
                if p.kind in ("image", "video", "scene", "symbol"):
                    p.chroma_key_enabled = ck_enable_check.isChecked()
                    p.chroma_key_color = ck_color_edit.text()
                    p.chroma_key_similarity = ck_sim_spin.value()
                    p.chroma_key_smoothness = ck_smooth_spin.value()
                    
                if anim_getter:
                    anim = anim_getter()
                    p.anim_in, p.anim_out = anim["in"], anim["out"]
                    p.anim_in_dur, p.anim_out_dur = anim["in_dur"], anim["out_dur"]
                    p.anim_scale, p.anim_tilt_deg = anim["scale"], anim["tilt"]
                    p.anim_in_ease, p.anim_out_ease = anim["in_ease"], anim["out_ease"]
                    p.anim_in_fade, p.anim_out_fade = anim["in_fade"], anim["out_fade"]
                if crop_getter:
                    crop = crop_getter()
                    p.crop_l, p.crop_t, p.crop_r, p.crop_b = crop["l"], crop["t"], crop["r"], crop["b"]
                    p.mask_type = crop["mask_type"]
                    p.mask_radius = crop["mask_radius"]
                    p.mask_invert = crop["mask_invert"]
                if timing_getter:
                    timing = timing_getter()
                    p.timing_type = timing["type"]
                    p.timing_offset_x, p.timing_offset_y = timing["off_x"], timing["off_y"]
                    p.layer = timing["layer"]
                    p.timing_random_pos = timing["random_pos"]
                    p.timing_random_x_min, p.timing_random_x_max = timing["rx_min"], timing["rx_max"]
                    p.timing_random_y_min, p.timing_random_y_max = timing["ry_min"], timing["ry_max"]
                    p.timing_random_seed = timing["seed"]
                    p.timing_double_flash = timing["double_flash"]
                    p.timing_double_flash_delay = timing["double_flash_delay"]
                    p.timing_flip = timing["flip"]
                    p.timing_flip_axis = timing["flip_axis"]
                    new_dur = p.end - p.start
                    p.timing_points = [pt for pt in p.timing_points if pt <= new_dur]
                    new_anim = (p.anim_in, p.anim_out, p.anim_in_dur, p.anim_out_dur, p.anim_scale, p.anim_tilt_deg,
                                p.anim_in_fade, p.anim_out_fade)
                    if new_anim != old_anim:
                        p.anim_custom = True
            for key in list(spins):
                if touched(key):
                    shown[key] = spins[key].value()
            show("start", p.start)
            show("end", p.end)
            if not is_sound_only:
                show("w", p.w)
                show("h", p.h)
            self.changed.emit()

        # format-painter wiring for inline geometry sections
        def _on_place_painter_clicked(checked, _b=_place_painter_btn):
            if not checked:
                self._deactivate_painter()
                return
            data = {
                "x": round(x_spin.value()) if x_spin else p.x,
                "y": round(y_spin.value()) if y_spin else p.y,
                "w": max(10, round(w_spin.value())) if w_spin else p.w,
                "h": max(10, round(h_spin.value())) if h_spin else p.h,
                "rotation": rotation_spin.value() if rotation_spin else getattr(p, "rotation", 0.0),
                "opacity": opacity_spin.value() if not is_sound_only else getattr(p, "opacity", 1.0),
            }
            def _apply(tgt: Placement, _d=data):
                tgt.x = _d["x"]; tgt.y = _d["y"]
                tgt.w = _d["w"]; tgt.h = _d["h"]
                tgt.rotation = _d["rotation"]
                tgt.opacity = _d["opacity"]
            self._activate_painter(_place_section_label, _apply, _b)

        _place_painter_btn.clicked.connect(_on_place_painter_clicked)

        if not is_sound_only:
            def _on_3d_painter_clicked(checked, _b=_3d_painter_btn):
                if not checked:
                    self._deactivate_painter()
                    return
                data = {
                    "enable_3d": enable_3d_check.isChecked(),
                    "tx": tx_spin.value(), "ty": ty_spin.value(), "tz": tz_spin.value(),
                    "rx": rx_spin.value(), "ry": ry_spin.value(), "rz": rz_spin.value(),
                }
                def _apply(tgt: Placement, _d=data):
                    tgt.enable_3d = _d["enable_3d"]
                    tgt.translate_3d_x = _d["tx"]; tgt.translate_3d_y = _d["ty"]; tgt.translate_3d_z = _d["tz"]
                    tgt.rotate_3d_x = _d["rx"]; tgt.rotate_3d_y = _d["ry"]; tgt.rotate_3d_z = _d["rz"]
                self._activate_painter(i18n.tr("properties.section3D"), _apply, _b)

            _3d_painter_btn.clicked.connect(_on_3d_painter_clicked)

            if p.kind in ("image", "video", "scene", "symbol"):
                def _on_ck_painter_clicked(checked, _b=_ck_painter_btn):
                    if not checked:
                        self._deactivate_painter()
                        return
                    data = {
                        "enabled": ck_enable_check.isChecked(),
                        "color": ck_color_edit.text(),
                        "similarity": ck_sim_spin.value(),
                        "smoothness": ck_smooth_spin.value(),
                    }
                    def _apply(tgt: Placement, _d=data):
                        tgt.chroma_key_enabled = _d["enabled"]
                        tgt.chroma_key_color = _d["color"]
                        tgt.chroma_key_similarity = _d["similarity"]
                        tgt.chroma_key_smoothness = _d["smoothness"]
                    self._activate_painter(i18n.tr("properties.sectionChromaKey"), _apply, _b)

                _ck_painter_btn.clicked.connect(_on_ck_painter_clicked)

        if not is_sound_only:
            cb = apply_geo if self._auto_apply else None
            anim_getter = self._build_anim_block(form, p.anim_in, p.anim_out, p.anim_in_dur, p.anim_out_dur,
                                                  p.anim_scale, p.anim_tilt_deg, getattr(p, "anim_in_ease", DEFAULT_EASE), getattr(p, "anim_out_ease", DEFAULT_EASE), cb,
                                                  in_fade=p.anim_in_fade, out_fade=p.anim_out_fade)
            timing_getter = self._build_timing_block(form, p, cb)
            if p.kind in ("image", "video", "symbol", "scene", "shape"):
                crop_getter = self._build_crop_mask_block(form, p, cb)

        if p.kind == "scene":
            enter_btn = QPushButton(i18n.tr("properties.enterScene"))
            enter_btn.clicked.connect(lambda: self.request_enter_scene.emit(p.ref))
            layout.addWidget(enter_btn)

        apply_btn = self._apply_button(layout, i18n.tr("properties.applyPlacement"))
        if apply_btn:
            apply_btn.clicked.connect(apply_geo)
            
        widgets_to_wire = list(spins.values())
        if not is_sound_only:
            widgets_to_wire.append(enable_3d_check)
            if p.kind in ("image", "video", "scene", "symbol"):
                widgets_to_wire.append(ck_enable_check)
                widgets_to_wire.append(ck_color_edit)
        self._wire(apply_geo, *widgets_to_wire)

    # ANIMATION
    def _build_anim_block(self, form: QFormLayout, anim_in, anim_out, in_dur, out_dur, scale, tilt, in_ease=DEFAULT_EASE, out_ease=DEFAULT_EASE, on_change=None,
                          *, in_fade=False, out_fade=False):
        _anim_section_label = i18n.tr("properties.sectionAnimation")
        _anim_section_row, _anim_painter_btn = _painter_section_row(_anim_section_label)
        self._painter_btns.append(_anim_painter_btn)
        form.addRow(_anim_section_row)
        anim_in = anim_in if anim_in in ANIM_TYPES else "none"
        anim_out = anim_out if anim_out in ANIM_TYPES else "none"
        in_default, _ = ANIM_DEFAULTS[anim_in]
        _, out_default = ANIM_DEFAULTS[anim_out]

        in_combo = _anim_combo(anim_in)
        in_dur_spin = _dspin(in_dur if in_dur > 0 else in_default, 0, 100, 0.05, 2)
        
        in_ease_val = list(in_ease) if isinstance(in_ease, (list, tuple)) else list(DEFAULT_EASE)
        out_ease_val = list(out_ease) if isinstance(out_ease, (list, tuple)) else list(DEFAULT_EASE)
        
        in_ease_btn = QPushButton(i18n.tr("properties.editEase"))
        out_ease_btn = QPushButton(i18n.tr("properties.editEase"))
        
        def _edit_in_ease():
            d = BezierEaseDialog(tuple(in_ease_val), form.parentWidget())
            if d.exec():
                in_ease_val[:] = d.result_ease()
                if on_change: on_change()
        def _edit_out_ease():
            d = BezierEaseDialog(tuple(out_ease_val), form.parentWidget())
            if d.exec():
                out_ease_val[:] = d.result_ease()
                if on_change: on_change()
                
        in_ease_btn.clicked.connect(_edit_in_ease)
        out_ease_btn.clicked.connect(_edit_out_ease)
        
        out_combo = _anim_combo(anim_out)
        out_dur_spin = _dspin(out_dur if out_dur > 0 else out_default, 0, 100, 0.05, 2)
        
        in_ease_row = _labeled_row(i18n.tr("properties.labelEaseIn"), in_ease_btn)
        out_ease_row = _labeled_row(i18n.tr("properties.labelEaseOut"), out_ease_btn)
        
        in_fade_wrap, in_fade_check = _check_wrap(in_fade, i18n.tr("properties.animFadeCheck"))
        out_fade_wrap, out_fade_check = _check_wrap(out_fade, i18n.tr("properties.animFadeCheck"))
        in_fade_row = _labeled_row(i18n.tr("properties.labelFadeIn"), in_fade_wrap)
        out_fade_row = _labeled_row(i18n.tr("properties.labelFadeOut"), out_fade_wrap)

        form.addRow(i18n.tr("properties.labelAnimIn"), _hbox(in_combo, in_dur_spin))
        form.addRow(in_ease_row[0], in_ease_row[1])
        form.addRow(in_fade_row[0], in_fade_row[1])
        form.addRow(i18n.tr("properties.labelAnimOut"), _hbox(out_combo, out_dur_spin))
        form.addRow(out_ease_row[0], out_ease_row[1])
        form.addRow(out_fade_row[0], out_fade_row[1])
        _tip_rows(form, i18n.tr("properties.hintAnimDuration"), 6)
        _tip(in_fade_wrap, i18n.tr("properties.hintAnimFade"))
        _tip(out_fade_wrap, i18n.tr("properties.hintAnimFade"))

        scale_spin = _dspin(scale if scale > 0 else ANIM_SCALE_DEFAULT, 1, 999, 1, 0)
        scale_row = _labeled_row(i18n.tr("properties.labelAnimScale"), scale_spin)
        form.addRow(scale_row[0], scale_row[1])
        tilt_spin = _dspin(tilt if tilt != 0 else ANIM_TILT_DEFAULT, -180, 180, 1, 0)
        tilt_row = _labeled_row(i18n.tr("properties.labelAnimTilt"), tilt_spin)
        form.addRow(tilt_row[0], tilt_row[1])

        def update_visibility():
            uses_scale = in_combo.currentData() in ("pop", "aero") or out_combo.currentData() in ("pop", "aero")
            uses_tilt = in_combo.currentData() == "aero" or out_combo.currentData() == "aero"
            
            in_ease_visible = in_combo.currentData() not in ("none", "aero")
            out_ease_visible = out_combo.currentData() not in ("none", "aero")
            
            in_ease_row[0].setVisible(in_ease_visible)
            in_ease_row[1].setVisible(in_ease_visible)
            out_ease_row[0].setVisible(out_ease_visible)
            out_ease_row[1].setVisible(out_ease_visible)
            
            in_fade_visible = in_combo.currentData() in FADE_OPTIONAL_ANIMS
            out_fade_visible = out_combo.currentData() in FADE_OPTIONAL_ANIMS
            in_fade_row[0].setVisible(in_fade_visible)
            in_fade_row[1].setVisible(in_fade_visible)
            out_fade_row[0].setVisible(out_fade_visible)
            out_fade_row[1].setVisible(out_fade_visible)

            scale_row[0].setVisible(uses_scale)
            scale_row[1].setVisible(uses_scale)
            tilt_row[0].setVisible(uses_tilt)
            tilt_row[1].setVisible(uses_tilt)

        def on_in_change():
            d, _ = ANIM_DEFAULTS[in_combo.currentData() or "none"]
            in_dur_spin.setValue(d)
            update_visibility()
            if on_change: on_change()

        def on_out_change():
            _, d = ANIM_DEFAULTS[out_combo.currentData() or "none"]
            out_dur_spin.setValue(d)
            update_visibility()
            if on_change: on_change()

        in_combo.currentIndexChanged.connect(lambda _i: on_in_change())
        out_combo.currentIndexChanged.connect(lambda _i: on_out_change())
        if on_change:
            in_dur_spin.valueChanged.connect(lambda _v: on_change())
            out_dur_spin.valueChanged.connect(lambda _v: on_change())
            scale_spin.valueChanged.connect(lambda _v: on_change())
            tilt_spin.valueChanged.connect(lambda _v: on_change())
            in_fade_check.toggled.connect(lambda _b: on_change())
            out_fade_check.toggled.connect(lambda _b: on_change())
        update_visibility()

        def getter():
            return {
                "in": in_combo.currentData() or "none",
                "out": out_combo.currentData() or "none",
                "in_dur": max(0.0, in_dur_spin.value()),
                "out_dur": max(0.0, out_dur_spin.value()),
                "in_ease": tuple(in_ease_val),
                "out_ease": tuple(out_ease_val),
                "scale": max(0, min(100, scale_spin.value())),
                "tilt": tilt_spin.value(),
                "in_fade": in_fade_check.isChecked(),
                "out_fade": out_fade_check.isChecked(),
            }

        # format-painter wiring
        def _apply_anim(tgt: Placement, d):
            tgt.anim_in = d["in"]
            tgt.anim_out = d["out"]
            tgt.anim_in_dur = d["in_dur"]
            tgt.anim_out_dur = d["out_dur"]
            tgt.anim_scale = d["scale"]
            tgt.anim_tilt_deg = d["tilt"]
            tgt.anim_in_ease = d["in_ease"]
            tgt.anim_out_ease = d["out_ease"]
            tgt.anim_in_fade = d["in_fade"]
            tgt.anim_out_fade = d["out_fade"]

        def _on_anim_painter_clicked(checked, _g=getter, _lbl=_anim_section_label, _b=_anim_painter_btn):
            if checked:
                data = _g()
                self._activate_painter(_lbl, lambda tgt, _d=data: _apply_anim(tgt, _d), _b)
            else:
                self._deactivate_painter()

        _anim_painter_btn.clicked.connect(_on_anim_painter_clicked)

        return getter

    # TIMING
    def _build_timing_block(self, form: QFormLayout, p: Placement, on_change=None):
        _timing_section_label = i18n.tr("properties.sectionTiming")
        _timing_section_row, _timing_painter_btn = _painter_section_row(_timing_section_label)
        self._painter_btns.append(_timing_painter_btn)
        form.addRow(_timing_section_row)
        ttype = p.timing_type if p.timing_type in TIMING_TYPES else "none"
        type_combo = _combo(TIMING_TYPES, ttype)
        form.addRow(i18n.tr("properties.labelTimingType"), type_combo)

        off_x = _dspin(p.timing_offset_x, -100000, 100000, 1, 1)
        off_y = _dspin(p.timing_offset_y, -100000, 100000, 1, 1)
        off_row = _labeled_row(i18n.tr("properties.labelOffsetXY"), _hbox(off_x, off_y))
        form.addRow(off_row[0], off_row[1])

        random_check = _check(p.timing_random_pos)
        random_row = _labeled_row(i18n.tr("properties.labelRandomPositions"), random_check)
        form.addRow(random_row[0], random_row[1])

        rx_min = _dspin(p.timing_random_x_min, -100000, 100000, 1, 0)
        rx_max = _dspin(p.timing_random_x_max, -100000, 100000, 1, 0)
        rx_row = _labeled_row(i18n.tr("properties.labelRandomXMinMax"), _hbox(rx_min, rx_max))
        form.addRow(rx_row[0], rx_row[1])
        ry_min = _dspin(p.timing_random_y_min, -100000, 100000, 1, 0)
        ry_max = _dspin(p.timing_random_y_max, -100000, 100000, 1, 0)
        ry_row = _labeled_row(i18n.tr("properties.labelRandomYMinMax"), _hbox(ry_min, ry_max))
        form.addRow(ry_row[0], ry_row[1])
        seed_spin = _ispin(p.timing_random_seed, 0, 999999999)
        seed_row = _labeled_row(i18n.tr("properties.labelRandomSeed"), seed_spin)
        form.addRow(seed_row[0], seed_row[1])

        dflash_wrap, dflash_check = _check_wrap(p.timing_double_flash, i18n.tr("properties.flashDouble"))
        dflash_row = _labeled_row(i18n.tr("properties.labelFlashDouble"), dflash_wrap)
        form.addRow(dflash_row[0], dflash_row[1])
        dflash_delay = _dspin(p.timing_double_flash_delay, 0.001, 5.0, 0.01, 2)
        dflash_delay_row = _labeled_row(i18n.tr("properties.labelFlashDelay"), dflash_delay)
        form.addRow(dflash_delay_row[0], dflash_delay_row[1])
        flip_wrap, flip_check = _check_wrap(p.timing_flip, i18n.tr("properties.flipOnPoint"))
        flip_row = _labeled_row(i18n.tr("properties.labelFlip"), flip_wrap)
        form.addRow(flip_row[0], flip_row[1])
        flip_axis = _combo(["horizontal", "vertical"],
                           p.timing_flip_axis if p.timing_flip_axis in ("horizontal", "vertical") else "horizontal")
        flip_axis_row = _labeled_row(i18n.tr("properties.labelFlipAxis"), flip_axis)
        form.addRow(flip_axis_row[0], flip_axis_row[1])
        for _w in (dflash_row[0], dflash_row[1], dflash_delay_row[0], dflash_delay_row[1]):
            _tip(_w, i18n.tr("properties.hintFlashDouble"))
        for _w in (flip_row[0], flip_row[1], flip_axis_row[0], flip_axis_row[1]):
            _tip(_w, i18n.tr("properties.hintFlip"))

        layer_spin = _ispin(p.layer, -999, 999)
        form.addRow(i18n.tr("properties.labelLayer"), layer_spin)
        _tip_rows(form, i18n.tr("properties.hintLayerStacking"))

        media_rows: list[SourceField] = []
        media_container = QWidget()
        media_layout = QVBoxLayout(media_container)
        media_layout.setContentsMargins(0, 0, 0, 0)
        media_wrap_row = None
        if p.kind == "symbol":
            form.addRow(media_container)
            _tip(media_container, i18n.tr("properties.hintMediaSequence"))

            def add_media_row(uuid: str):
                field = SourceField(self.project, self.project_dir, "image", uuid)
                remove_btn = QPushButton("X")
                remove_btn.setFixedWidth(24)
                row = _hbox(field, remove_btn)
                media_layout.addWidget(row)
                media_rows.append(field)

                def remove():
                    media_rows.remove(field)
                    row.hide()
                    row.setParent(None)
                    row.deleteLater()

                remove_btn.clicked.connect(remove)
                if on_change:
                    field.changed.connect(on_change)
                    remove_btn.clicked.connect(lambda: on_change())

            for uuid in (p.media_list or []):
                add_media_row(uuid)
            add_btn = QPushButton(i18n.tr("properties.addMedia"))
            add_btn.clicked.connect(lambda: add_media_row(""))
            form.addRow(add_btn)
            dir_combo = _combo(["forward", "backward"], p.media_direction or "forward")
            media_wrap_row = _labeled_row(i18n.tr("properties.labelDirection"), dir_combo)
            form.addRow(media_wrap_row[0], media_wrap_row[1])
        else:
            dir_combo = None

        def update_visibility():
            t = type_combo.currentText()
            random_on = random_check.isChecked()
            off_row[0].setVisible(t == "duplication" and not random_on)
            off_row[1].setVisible(t == "duplication" and not random_on)
            random_row[0].setVisible(t == "duplication")
            random_row[1].setVisible(t == "duplication")
            rx_row[0].setVisible(t == "duplication" and random_on)
            rx_row[1].setVisible(t == "duplication" and random_on)
            ry_row[0].setVisible(t == "duplication" and random_on)
            ry_row[1].setVisible(t == "duplication" and random_on)
            seed_row[0].setVisible(t == "duplication" and random_on)
            seed_row[1].setVisible(t == "duplication" and random_on)
            flashing = t == "flashing"
            for w in dflash_row:
                w.setVisible(flashing)
            for w in dflash_delay_row:
                w.setVisible(flashing and dflash_check.isChecked())
            for w in flip_row:
                w.setVisible(flashing)
            for w in flip_axis_row:
                w.setVisible(flashing and flip_check.isChecked())
            media_container.setVisible(t == "switch_media")
            if media_wrap_row:
                media_wrap_row[0].setVisible(t == "switch_media")
                media_wrap_row[1].setVisible(t == "switch_media")

        type_combo.currentIndexChanged.connect(lambda _i: update_visibility())
        random_check.stateChanged.connect(lambda _v: update_visibility())
        dflash_check.stateChanged.connect(lambda _v: update_visibility())
        flip_check.stateChanged.connect(lambda _v: update_visibility())
        if on_change:
            type_combo.currentIndexChanged.connect(lambda _i: on_change())
            random_check.toggled.connect(lambda _b: on_change())
            off_x.valueChanged.connect(lambda _v: on_change())
            off_y.valueChanged.connect(lambda _v: on_change())
            rx_min.valueChanged.connect(lambda _v: on_change())
            rx_max.valueChanged.connect(lambda _v: on_change())
            ry_min.valueChanged.connect(lambda _v: on_change())
            ry_max.valueChanged.connect(lambda _v: on_change())
            seed_spin.valueChanged.connect(lambda _v: on_change())
            layer_spin.valueChanged.connect(lambda _v: on_change())
            dflash_check.toggled.connect(lambda _b: on_change())
            dflash_delay.valueChanged.connect(lambda _v: on_change())
            flip_check.toggled.connect(lambda _b: on_change())
            flip_axis.currentIndexChanged.connect(lambda _i: on_change())
            if dir_combo:
                dir_combo.currentIndexChanged.connect(lambda _i: on_change())
        update_visibility()

        n = len(p.timing_points)
        _tip(_timing_section_row, i18n.tr("properties.hintTimingPointsFmt").format(
            count=n, plural="s" if n != 1 else ""))

        def getter():
            return {
                "type": type_combo.currentText(),
                "off_x": off_x.value(),
                "off_y": off_y.value(),
                "layer": layer_spin.value(),
                "random_pos": random_check.isChecked(),
                "rx_min": rx_min.value(),
                "rx_max": rx_max.value(),
                "ry_min": ry_min.value(),
                "ry_max": ry_max.value(),
                "seed": seed_spin.value(),
                "double_flash": dflash_check.isChecked(),
                "double_flash_delay": dflash_delay.value(),
                "flip": flip_check.isChecked(),
                "flip_axis": flip_axis.currentText(),
            }

        def apply_media():
            if p.kind == "symbol":
                p.media_list = [f.value() for f in media_rows if f.value()]
                p.media_direction = dir_combo.currentText() if dir_combo else "forward"

        def combined_getter():
            result = getter()
            apply_media()
            return result

        # format-painter wiring
        def _apply_timing(tgt: Placement, d):
            tgt.timing_type = d["type"]
            tgt.timing_offset_x = d["off_x"]
            tgt.timing_offset_y = d["off_y"]
            tgt.layer = d["layer"]
            tgt.timing_random_pos = d["random_pos"]
            tgt.timing_random_x_min = d["rx_min"]
            tgt.timing_random_x_max = d["rx_max"]
            tgt.timing_random_y_min = d["ry_min"]
            tgt.timing_random_y_max = d["ry_max"]
            tgt.timing_random_seed = d["seed"]
            tgt.timing_double_flash = d["double_flash"]
            tgt.timing_double_flash_delay = d["double_flash_delay"]
            tgt.timing_flip = d["flip"]
            tgt.timing_flip_axis = d["flip_axis"]

        def _on_timing_painter_clicked(checked, _g=getter, _lbl=_timing_section_label, _b=_timing_painter_btn):
            if checked:
                data = _g()
                self._activate_painter(_lbl, lambda tgt, _d=data: _apply_timing(tgt, _d), _b)
            else:
                self._deactivate_painter()

        _timing_painter_btn.clicked.connect(_on_timing_painter_clicked)

        return combined_getter

    # CROP & MASK
    def _build_crop_mask_block(self, form: QFormLayout, p: Placement, on_change=None):
        _crop_section_label = i18n.tr("properties.sectionCropMask")
        _crop_section_row, _crop_painter_btn = _painter_section_row(_crop_section_label)
        self._painter_btns.append(_crop_painter_btn)
        form.addRow(_crop_section_row)

        l_spin = _dspin((p.crop_l or 0.0) * 100, 0, 98, 1, 0)
        t_spin = _dspin((p.crop_t or 0.0) * 100, 0, 98, 1, 0)
        r_spin = _dspin((p.crop_r or 0.0) * 100, 0, 98, 1, 0)
        b_spin = _dspin((p.crop_b or 0.0) * 100, 0, 98, 1, 0)
        for s in (l_spin, t_spin, r_spin, b_spin):
            s.setSuffix("%")
        form.addRow(i18n.tr("properties.labelCropLR"), _hbox(_sep(i18n.tr("properties.cropL")), l_spin, _sep(i18n.tr("properties.cropR")), r_spin))
        form.addRow(i18n.tr("properties.labelCropTB"), _hbox(_sep(i18n.tr("properties.cropT")), t_spin, _sep(i18n.tr("properties.cropB")), b_spin))
        _tip_rows(form, i18n.tr("properties.hintCropMask"), 2)

        mask_combo = _combo(["none", "ellipse", "rounded_rect"], p.mask_type or "none")
        form.addRow(i18n.tr("properties.labelMaskType"), mask_combo)
        radius_spin = _dspin((p.mask_radius if p.mask_radius else 0.2) * 100, 0, 100, 1, 0)
        radius_spin.setSuffix("%")
        radius_row = _labeled_row(i18n.tr("properties.labelMaskRadius"), radius_spin)
        form.addRow(radius_row[0], radius_row[1])
        invert_check = _check(p.mask_invert, i18n.tr("properties.labelMaskInvert"))
        form.addRow("", invert_check)

        reset_btn = QPushButton(i18n.tr("properties.resetCrop"))

        def do_reset():
            for s in (l_spin, t_spin, r_spin, b_spin):
                s.blockSignals(True)
                s.setValue(0)
                s.blockSignals(False)
            if on_change:
                on_change()
        reset_btn.clicked.connect(do_reset)
        form.addRow(reset_btn)

        def update_visibility():
            has_mask = mask_combo.currentText() != "none"
            is_rounded = mask_combo.currentText() == "rounded_rect"
            radius_row[0].setVisible(is_rounded)
            radius_row[1].setVisible(is_rounded)
            invert_check.setVisible(has_mask)
        mask_combo.currentIndexChanged.connect(lambda _i: update_visibility())
        update_visibility()

        if on_change:
            for s in (l_spin, t_spin, r_spin, b_spin, radius_spin):
                s.valueChanged.connect(lambda _v: on_change())
            mask_combo.currentIndexChanged.connect(lambda _i: on_change())
            invert_check.toggled.connect(lambda _b: on_change())

        def getter():
            l = max(0.0, min(0.98, l_spin.value() / 100.0))
            t = max(0.0, min(0.98, t_spin.value() / 100.0))
            r = max(0.0, min(0.98, r_spin.value() / 100.0))
            b = max(0.0, min(0.98, b_spin.value() / 100.0))
            return {
                "l": l, "t": t, "r": r, "b": b,
                "mask_type": mask_combo.currentText(),
                "mask_radius": max(0.0, min(1.0, radius_spin.value() / 100.0)),
                "mask_invert": invert_check.isChecked(),
            }

        # format-painter wiring
        def _apply_crop(tgt: Placement, d):
            tgt.crop_l = d["l"]
            tgt.crop_t = d["t"]
            tgt.crop_r = d["r"]
            tgt.crop_b = d["b"]
            tgt.mask_type = d["mask_type"]
            tgt.mask_radius = d["mask_radius"]
            tgt.mask_invert = d["mask_invert"]

        def _on_crop_painter_clicked(checked, _g=getter, _lbl=_crop_section_label, _b=_crop_painter_btn):
            if checked:
                data = _g()
                self._activate_painter(_lbl, lambda tgt, _d=data: _apply_crop(tgt, _d), _b)
            else:
                self._deactivate_painter()

        _crop_painter_btn.clicked.connect(_on_crop_painter_clicked)

        return getter


_FORMAT_PAINTER_ICON: Optional[QIcon] = None


def _format_painter_icon() -> QIcon:
    global _FORMAT_PAINTER_ICON
    if _FORMAT_PAINTER_ICON is not None:
        return _FORMAT_PAINTER_ICON
    img = QPixmap(20, 20)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # Handle
    handle_pen = QPen(QColor("#caa36b"), 2.4)
    handle_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(handle_pen)
    p.drawLine(13, 3, 17, 7)
    # Ferrule
    ferrule_pen = QPen(QColor("#b0b4bb"), 3.2)
    ferrule_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(ferrule_pen)
    p.drawLine(10, 6, 14, 10)
    # Brush head/bristles
    p.setBrush(QBrush(QColor("#2060cc")))
    p.setPen(QPen(QColor("#ffffff"), 1))
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPolygonF
    bristles = QPolygonF([
        QPointF(11.5, 9.5), QPointF(15.5, 13.5),
        QPointF(7.0, 18.0), QPointF(3.0, 17.0),
        QPointF(2.0, 14.0),
    ])
    p.drawPolygon(bristles)
    p.end()
    _FORMAT_PAINTER_ICON = QIcon(img)
    return _FORMAT_PAINTER_ICON


def _painter_section_row(title: str) -> tuple:
    container = QWidget()
    hl = QHBoxLayout(container)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.setSpacing(4)
    lbl = _section(title)
    hl.addWidget(lbl, stretch=1)
    btn = QPushButton()
    btn.setIcon(_format_painter_icon())
    btn.setIconSize(QSize(16, 16))
    btn.setFixedSize(24, 24)
    btn.setCheckable(True)
    btn.setFlat(True)
    btn.setToolTip(i18n.tr("properties.painterTip").format(title=title))
    btn.setStyleSheet(
        "QPushButton { border: none; padding: 1px; font-size: 14px; } "
        "QPushButton:checked { background-color: #2060cc; border-radius: 4px; } "
        "QPushButton:hover:!checked { background-color: rgba(255,255,255,0.12); border-radius: 4px; }"
    )
    hl.addWidget(btn)
    return container, btn


def _section(title: str) -> QLabel:
    label = QLabel(title)
    label.setStyleSheet("font-weight: 600; color: #9ac; margin-top: 6px;")
    return label


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #888; font-size: 11px;")
    return label


def _tip(widget: QWidget, text: str) -> None:
    if widget is None or not text:
        return
    widget.setToolTip(text)
    for child in widget.findChildren(QWidget):
        if not child.toolTip():
            child.setToolTip(text)


def _tip_rows(form: QFormLayout, text: str, count: int = 1) -> None:
    for r in range(max(0, form.rowCount() - count), form.rowCount()):
        for role in (QFormLayout.ItemRole.LabelRole, QFormLayout.ItemRole.FieldRole,
                     QFormLayout.ItemRole.SpanningRole):
            item = form.itemAt(r, role)
            if item is not None and item.widget() is not None:
                _tip(item.widget(), text)


def _labeled_row(label_text: str, widget: QWidget):
    return QLabel(label_text), widget


def _clear_group(box: QGroupBox):
    old_layout = box.layout()
    if old_layout is not None:
        while old_layout.count():
            item = old_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
            sub = item.layout()
            if sub is not None:
                _clear_layout(sub)
        QWidget().setLayout(old_layout)


def _clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        sub = item.layout()
        if sub is not None:
            _clear_layout(sub)
