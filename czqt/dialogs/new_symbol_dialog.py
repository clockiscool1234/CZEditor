from __future__ import annotations

import os
import re
from typing import Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..model import Project
from ..widgets.source_field import SourceField

ANIM_TYPES = ["none", "fade", "pop", "aero"]
ANIM_DEFAULTS = {
    "none": (0.0, 0.0),
    "fade": (0.2, 0.6),
    "pop": (0.2, 0.2),
    "aero": (8 / 30, 8 / 30),
}


def _slugify_id(name: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    return out or "sym1"


class NewSymbolDialog(QDialog):
    def __init__(self, project: Project, project_dir: Optional[str] = None, initial_class: str = "", parent=None):
        super().__init__(parent)
        self.project = project
        self.project_dir = project_dir
        self.setWindowTitle(i18n.tr("dialog.newSymbol.title"))
        self.setMinimumWidth(420)

        self.name_edit = QLineEdit(i18n.tr("dialog.newSymbol.defaultName"))
        self.class_combo = QComboBox()
        self.class_combo.setEditable(True)
        self.class_combo.addItems(project.classes)
        if initial_class:
            self.class_combo.setCurrentText(initial_class)
        self.id_edit = QLineEdit(_slugify_id(self.name_edit.text()))
        self._id_touched = False
        self.id_edit.textEdited.connect(lambda _: setattr(self, "_id_touched", True))
        self.name_edit.textEdited.connect(self._sync_id_from_name)

        self.active_field = SourceField(project, project_dir, "image")
        self.inactive_field = SourceField(project, project_dir, "image")
        self.active_field.changed.connect(self._sync_size_from_active_image)

        self.w_spin = QDoubleSpinBox()
        self.w_spin.setRange(0, 100000)
        self.h_spin = QDoubleSpinBox()
        self.h_spin.setRange(0, 100000)
        self._size_touched = False
        self.w_spin.valueChanged.connect(self._mark_size_touched)
        self.h_spin.valueChanged.connect(self._mark_size_touched)
        size_row = QWidget()
        size_layout = QHBoxLayout(size_row)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.addWidget(self.w_spin)
        size_layout.addWidget(QLabel("x"))
        size_layout.addWidget(self.h_spin)

        self.dim_check = QCheckBox(i18n.tr("dialog.newSymbol.dimInactive"))
        self.dim_amount = QDoubleSpinBox()
        self.dim_amount.setRange(0, 1)
        self.dim_amount.setSingleStep(0.05)
        self.dim_amount.setValue(0.5)

        self.in_type = QComboBox()
        self.in_type.addItems(ANIM_TYPES)
        self.in_dur = QDoubleSpinBox()
        self.in_dur.setSingleStep(0.05)
        self.in_dur.setRange(0, 60)
        self.out_type = QComboBox()
        self.out_type.addItems(ANIM_TYPES)
        self.out_dur = QDoubleSpinBox()
        self.out_dur.setSingleStep(0.05)
        self.out_dur.setRange(0, 60)
        self.in_type.currentTextChanged.connect(
            lambda t: self.in_dur.setValue(ANIM_DEFAULTS[t][0])
        )
        self.out_type.currentTextChanged.connect(
            lambda t: self.out_dur.setValue(ANIM_DEFAULTS[t][1])
        )

        form = QFormLayout()
        form.addRow(i18n.tr("dialog.newSymbol.labelName"), self.name_edit)
        form.addRow(i18n.tr("dialog.newSymbol.labelClass"), self.class_combo)
        form.addRow(i18n.tr("dialog.newSymbol.labelID"), self.id_edit)
        form.addRow(i18n.tr("dialog.newSymbol.labelActiveImage"), self.active_field)
        form.addRow(i18n.tr("dialog.newSymbol.labelInactiveImage"), self.inactive_field)
        _tip_last_row(form, i18n.tr("dialog.newSymbol.hintInactive"))
        form.addRow(i18n.tr("dialog.newSymbol.labelSize"), size_row)
        form.addRow("", self.dim_check)
        form.addRow(i18n.tr("dialog.newSymbol.labelBrightness"), self.dim_amount)
        form.addRow(i18n.tr("dialog.newSymbol.labelIn"), _hbox(self.in_type, self.in_dur))
        form.addRow(i18n.tr("dialog.newSymbol.labelOut"), _hbox(self.out_type, self.out_dur))
        _tip_last_row(form, i18n.tr("dialog.newSymbol.hintAnimation"), 2)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _sync_id_from_name(self, _text: str):
        if not self._id_touched:
            self.id_edit.setText(_slugify_id(self.name_edit.text()))

    def _mark_size_touched(self, _value: float):
        self._size_touched = True

    def _sync_size_from_active_image(self):
        if self._size_touched:
            return
        uuid = self.active_field.value()
        if not uuid or not self.project_dir:
            return
        try:
            from PIL import Image as _PILImage
            with _PILImage.open(os.path.join(self.project_dir, uuid)) as im:
                iw, ih = im.size
        except Exception:
            return
        if iw <= 0 or ih <= 0:
            return
        self.w_spin.blockSignals(True)
        self.h_spin.blockSignals(True)
        self.w_spin.setValue(iw)
        self.h_spin.setValue(ih)
        self.w_spin.blockSignals(False)
        self.h_spin.blockSignals(False)

    def _on_accept(self):
        if not self.active_field.value():
            QMessageBox.warning(self, i18n.tr("dialog.newSymbol.title"), i18n.tr("dialog.newSymbol.warnActiveRequired"))
            return
        elem_id = self.id_edit.text().strip() or "sym1"
        existing_ids = {s.elem_id for s in self.project.symbol_defs.values()}
        if elem_id in existing_ids:
            QMessageBox.warning(
                self, i18n.tr("dialog.newSymbol.title"),
                i18n.tr("dialog.newSymbol.warnIdUsed").format(id=elem_id),
            )
            return
        self.accept()

    def values(self) -> dict:
        return {
            "name": self.name_edit.text().strip() or i18n.tr("dialog.newSymbol.defaultName"),
            "elem_class": self.class_combo.currentText().strip() or "generic",
            "elem_id": self.id_edit.text().strip() or "sym1",
            "active_image": self.active_field.value() or "",
            "inactive_image": self.inactive_field.value() or "",
            "width": self.w_spin.value(),
            "height": self.h_spin.value(),
            "dim_inactive": self.dim_check.isChecked(),
            "dim_amount": max(0.0, self.dim_amount.value() or 0.5),
            "anim_in": self.in_type.currentText(),
            "anim_out": self.out_type.currentText(),
            "anim_in_dur": max(0.0, self.in_dur.value()),
            "anim_out_dur": max(0.0, self.out_dur.value()),
        }


def _hbox(*widgets) -> QWidget:
    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        layout.addWidget(w)
    return box


def _tip_last_row(form: QFormLayout, text: str, count: int = 1) -> None:
    """Show `text` as a hover tooltip on the last `count` rows instead of a visible hint label."""
    for r in range(max(0, form.rowCount() - count), form.rowCount()):
        for role in (QFormLayout.ItemRole.LabelRole, QFormLayout.ItemRole.FieldRole):
            item = form.itemAt(r, role)
            w = item.widget() if item is not None else None
            if w is not None:
                w.setToolTip(text)
                for child in w.findChildren(QWidget):
                    if not child.toolTip():
                        child.setToolTip(text)
