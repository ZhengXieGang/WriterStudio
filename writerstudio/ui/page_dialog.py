"""纸张设置对话框与纸张预设编辑器。

对应需求「增加 A4 纸张预设、增加纸张预设编辑器」。

* :class:`PagePresetEditorDialog` —— 新建/编辑一个用户预设（名称+尺寸+边距）。
* :class:`PageSetupDialog` —— 页面设置主对话框：选预设、切方向、调尺寸/边距、
  显示页边距参考线开关，并可另存/删除用户预设。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.document import PageSpec
from ..page_presets import PagePreset, PagePresetStore
from .style import spin, unify_inputs

_CUSTOM_LABEL = "自定义尺寸"


def _spin(lo: float, hi: float, val: float, decimals: int = 1) -> QDoubleSpinBox:
    return spin(lo, hi, val, suffix=" mm", step=1.0, decimals=decimals)


class PagePresetEditorDialog(QDialog):
    """新建/编辑用户纸张预设。"""

    def __init__(self, store: PagePresetStore,
                 preset: Optional[PagePreset] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.store = store
        self._original = preset
        self.setWindowTitle("纸张预设编辑器")
        self.setMinimumWidth(360)

        root = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QComboBox()
        self.name_edit.setEditable(True)
        for p in store.builtin():
            self.name_edit.addItem(f"{p.name}（{p.width:g}×{p.height:g}）",
                                   p.name)
        self.name_edit.setCurrentIndex(-1)
        self.name_edit.setEditText(preset.name if preset else "")
        self.name_edit.activated.connect(self._on_base_chosen)
        form.addRow("名称", self.name_edit)

        self.width_spin = _spin(10.0, 5000.0, preset.width if preset else 210.0)
        self.height_spin = _spin(10.0, 5000.0, preset.height if preset else 297.0)
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("宽"))
        size_row.addWidget(self.width_spin, 1)
        size_row.addWidget(QLabel("高"))
        size_row.addWidget(self.height_spin, 1)
        form.addRow("尺寸", size_row)

        self.margin_spin = _spin(0.0, 200.0, preset.margin if preset else 10.0)
        form.addRow("页边距", self.margin_spin)

        root.addLayout(form)

        hint = QLabel("提示：尺寸以纵向 (宽≤高) 保存，使用时可在页面设置里切换横向。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        root.addWidget(hint)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)
        unify_inputs(self)

    def _on_base_chosen(self, index: int) -> None:
        name = self.name_edit.itemData(index)
        if not name:
            return
        p = self.store.find(name)
        if p is None:
            return
        self.name_edit.setEditText(name)
        self.width_spin.setValue(p.width)
        self.height_spin.setValue(p.height)
        self.margin_spin.setValue(p.margin)

    def _on_accept(self) -> None:
        if not self.result_preset().name:
            QMessageBox.warning(self, "名称为空", "请填写预设名称。")
            return
        self.accept()

    def result_preset(self) -> PagePreset:
        return PagePreset(
            name=self.name_edit.currentText().strip(),
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            margin=self.margin_spin.value(),
        )


class PageSetupDialog(QDialog):
    """页面设置：纸张预设 + 方向 + 尺寸/边距 + 参考线开关。"""

    def __init__(self, page: PageSpec, store: PagePresetStore,
                 guide_visible: bool = True,
                 parent: Optional[QWidget] = None,
                 on_rotate=None) -> None:
        super().__init__(parent)
        self.store = store
        self._page = page
        self._on_rotate = on_rotate
        self._preset_name = page.preset_name or ""
        self._landscape = page.width > page.height
        self.setWindowTitle("页面设置")
        self.setMinimumWidth(430)

        root = QVBoxLayout(self)

        # ---- 预设 & 方向 ----
        top = QFormLayout()
        self.preset_combo = QComboBox()
        self._reload_presets()
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        top.addRow("纸张预设", self.preset_combo)

        self.orient_combo = QComboBox()
        self.orient_combo.addItem("横向", True)
        self.orient_combo.addItem("纵向", False)
        self.orient_combo.setCurrentIndex(0 if self._landscape else 1)
        self.orient_combo.currentIndexChanged.connect(self._on_orientation)
        top.addRow("方向", self.orient_combo)
        root.addLayout(top)

        # ---- 尺寸 & 边距 ----
        form = QFormLayout()
        self.width_spin = _spin(10.0, 5000.0, page.width)
        self.height_spin = _spin(10.0, 5000.0, page.height)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("宽"))
        srow.addWidget(self.width_spin, 1)
        srow.addWidget(QLabel("高"))
        srow.addWidget(self.height_spin, 1)
        form.addRow("尺寸", srow)
        for s in (self.width_spin, self.height_spin):
            s.valueChanged.connect(self._on_manual_size)

        self.margin_spin = _spin(0.0, 200.0, page.margin)
        form.addRow("页边距", self.margin_spin)

        self.guide_check = QCheckBox("显示页边距参考线")
        self.guide_check.setChecked(bool(guide_visible))
        form.addRow("", self.guide_check)
        root.addLayout(form)

        # ---- 旋转纸张（连同内容） ----
        rot_row = QHBoxLayout()
        rot_row.addWidget(QLabel("旋转纸张"))
        if on_rotate is not None:
            extra = ("\n坐标映射（原点对齐/对调/反转）随页面坐标系自动换算，"
                     "机械原点标注与书写位置保持一致；撤销时一并还原。")
            for label, direction, tip in (
                    ("顺时针 90°", "cw", "纸张与内容一起顺时针旋转 90°" + extra),
                    ("逆时针 90°", "ccw", "纸张与内容一起逆时针旋转 90°" + extra),
                    ("180°", "180", "纸张与内容一起旋转 180°" + extra)):
                b = QPushButton(label)
                b.setToolTip(tip)
                b.clicked.connect(
                    lambda _=False, d=direction: self._rotate(d))
                rot_row.addWidget(b)
        rot_row.addStretch(1)
        root.addLayout(rot_row)

        # ---- 尺寸信息 ----
        self.info = QLabel("—")
        self.info.setStyleSheet("color:#666;")
        root.addWidget(self.info)

        # ---- 预设管理 ----
        manage = QHBoxLayout()
        save_btn = QPushButton("另存为预设…")
        save_btn.clicked.connect(self._save_as_preset)
        del_btn = QPushButton("删除预设")
        del_btn.clicked.connect(self._delete_preset)
        manage.addWidget(save_btn)
        manage.addWidget(del_btn)
        manage.addStretch(1)
        root.addLayout(manage)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)
        self._update_info()
        unify_inputs(self)

    # --------------------------------------------------------------- 预设列表
    def _reload_presets(self) -> None:
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem(_CUSTOM_LABEL, "")
        for p in self.store.builtin():
            self.preset_combo.addItem(
                f"{p.name}  ({p.width:g}×{p.height:g})", p.name)
        users = self.store.user()
        if users:
            self.preset_combo.insertSeparator(self.preset_combo.count())
            for p in users:
                self.preset_combo.addItem(
                    f"{p.name}  ({p.width:g}×{p.height:g})", p.name)
        idx = self.preset_combo.findData(self._preset_name)
        if idx < 0 and self._preset_name:
            # 预设可能已被删除 → 退化为自定义尺寸，避免保留失效名字
            self._preset_name = ""
            idx = 0
        self.preset_combo.setCurrentIndex(max(0, idx))
        self.preset_combo.blockSignals(False)

    def _current_landscape(self) -> bool:
        return bool(self.orient_combo.currentData())

    def _on_preset_changed(self, index: int) -> None:
        name = self.preset_combo.itemData(index) or ""
        self._preset_name = name
        if not name:
            self._update_info()
            return
        p = self.store.find(name)
        if p is None:
            return
        w, h = p.oriented(self._current_landscape())
        self.width_spin.blockSignals(True)
        self.height_spin.blockSignals(True)
        self.width_spin.setValue(w)
        self.height_spin.setValue(h)
        self.margin_spin.setValue(p.margin)
        self.width_spin.blockSignals(False)
        self.height_spin.blockSignals(False)
        self._update_info()

    def _on_orientation(self) -> None:
        landscape = self._current_landscape()
        if self._preset_name:
            self._on_preset_changed(self.preset_combo.currentIndex())
            return
        # 自定义尺寸：交换宽高
        w, h = self.width_spin.value(), self.height_spin.value()
        if (w > h) != landscape:
            self.width_spin.blockSignals(True)
            self.height_spin.blockSignals(True)
            self.width_spin.setValue(h)
            self.height_spin.setValue(w)
            self.width_spin.blockSignals(False)
            self.height_spin.blockSignals(False)
        self._update_info()

    def _on_manual_size(self) -> None:
        # 手动改尺寸即视为自定义
        if self.preset_combo.currentIndex() != 0:
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(0)
            self.preset_combo.blockSignals(False)
            self._preset_name = ""
        self._update_info()

    def _update_info(self) -> None:
        w, h = self.width_spin.value(), self.height_spin.value()
        self.info.setText(
            f"{w:.1f} × {h:.1f} mm   （{w / 25.4:.2f} × {h / 25.4:.2f} 英寸）")

    def _rotate(self, direction: str) -> None:
        """在对话框内就地旋转纸张（会改动文档并立即刷新界面）。"""
        if self._on_rotate is None:
            return
        self._on_rotate(direction)
        # 旋转后页面尺寸已变，刷新控件
        page = self._page
        self.width_spin.blockSignals(True)
        self.height_spin.blockSignals(True)
        self.width_spin.setValue(page.width)
        self.height_spin.setValue(page.height)
        self.width_spin.blockSignals(False)
        self.height_spin.blockSignals(False)
        self._landscape = page.width > page.height
        self.orient_combo.blockSignals(True)
        self.orient_combo.setCurrentIndex(0 if self._landscape else 1)
        self.orient_combo.blockSignals(False)
        self._update_info()

    # --------------------------------------------------------------- 预设管理
    def _save_as_preset(self) -> None:
        init = PagePreset(
            name=self._preset_name or "我的纸张",
            width=min(self.width_spin.value(), self.height_spin.value()),
            height=max(self.width_spin.value(), self.height_spin.value()),
            margin=self.margin_spin.value(),
        )
        dlg = PagePresetEditorDialog(self.store, init, self)
        if dlg.exec() != QDialog.Accepted:
            return
        preset = dlg.result_preset()
        if self.store.is_builtin(preset.name):
            QMessageBox.warning(self, "名称冲突",
                                f"「{preset.name}」是内置预设名，请换一个名称。")
            return
        try:
            self.store.add(preset)
        except ValueError as exc:
            QMessageBox.warning(self, "无法保存预设", str(exc))
            return
        self._preset_name = preset.name
        self._reload_presets()
        self._on_preset_changed(self.preset_combo.currentIndex())

    def _delete_preset(self) -> None:
        name = self.preset_combo.currentData() or ""
        if not name:
            QMessageBox.information(self, "删除预设", "当前是自定义尺寸，无可删除的预设。")
            return
        if self.store.is_builtin(name):
            QMessageBox.information(self, "删除预设", f"「{name}」是内置预设，无法删除。")
            return
        r = QMessageBox.question(self, "删除预设", f"确定删除用户预设「{name}」？")
        if r != QMessageBox.Yes:
            return
        self.store.remove(name)
        self._preset_name = ""
        self._reload_presets()
        self.preset_combo.setCurrentIndex(0)

    # --------------------------------------------------------------- 取值
    def result_width(self) -> float:
        return self.width_spin.value()

    def result_height(self) -> float:
        return self.height_spin.value()

    def result_margin(self) -> float:
        return self.margin_spin.value()

    def result_preset_name(self) -> str:
        return self._preset_name

    def guide_visible(self) -> bool:
        return self.guide_check.isChecked()
