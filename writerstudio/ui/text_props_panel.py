"""文字属性面板：选中单个文本对象时的快捷属性区（字体/字号/字距/行距）。

嵌在手写扰动停靠面板顶部，只在恰好选中一个文本对象时显示。
所有改动都是**覆盖语义**——新值直接覆盖对象对应字段（字体=重写回退链
首位），不与旧值叠加，避免同一字段被多次设置后互相打架。
信号发出后由主窗口经 ``replace_objects(merge_key=...)`` 实时写回并合并
撤销步骤；若画布行内编辑器里有选中的字符，字体改动只作用于选区字符
（写入 ``TextSpec.char_overrides``）——该判断由主窗口完成。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QVBoxLayout,
    QWidget,
)

from ..fonts.builder import TextSpec
from .style import spin, unify_inputs


class TextPropsPanel(QWidget):
    """文本对象的属性区。任一属性变化发出 :attr:`propsChanged`。"""

    propsChanged = Signal()

    def __init__(self, manager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self._loading = False
        self._last_field = ""          # 最近一次被用户改动的字段

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("文字属性")
        form = QFormLayout(box)

        self.font_combo = QComboBox()
        self.font_combo.currentIndexChanged.connect(
            lambda _i: self._on_change("font"))
        form.addRow("字体", self.font_combo)

        self.size_spin = spin(0.5, 500.0, 10.0, " mm")
        self.size_spin.valueChanged.connect(lambda _v: self._on_change("size"))
        form.addRow("字号", self.size_spin)

        self.cs_spin = spin(-20.0, 50.0, 0.0, " mm", step=0.1)
        self.cs_spin.valueChanged.connect(
            lambda _v: self._on_change("char_spacing"))
        form.addRow("字距", self.cs_spin)

        self.ls_spin = spin(0.5, 5.0, 1.4, " ×", step=0.05)
        self.ls_spin.valueChanged.connect(
            lambda _v: self._on_change("line_spacing"))
        form.addRow("行距", self.ls_spin)

        self.fw_spin = spin(0.0, 2000.0, 0.0, " mm", step=5.0)
        self.fw_spin.setToolTip(
            "文本框宽度：大于 0 时按该宽度自动换行（0 = 不限宽）。\n"
            "也可以在画布上直接拖动文本框右缘的手柄调整。")
        self.fw_spin.valueChanged.connect(
            lambda _v: self._on_change("frame_width"))
        form.addRow("文本框宽度", self.fw_spin)

        v.addWidget(box)
        # 与同列的手写扰动面板同规格（高度取全局 FIELD_HEIGHT）
        unify_inputs(self, min_width=88, button_height=30)

    # ------------------------------------------------------------------ 内部
    def _on_change(self, field: str) -> None:
        if self._loading:
            return
        self._last_field = field
        self.propsChanged.emit()

    def _reload_fonts(self, primary: str) -> None:
        """重建字体下拉（置顶优先、隐藏排除），并选中主字体。"""
        self.font_combo.blockSignals(True)
        self.font_combo.clear()
        names = []
        for e in self.manager.visible_entries():
            self.font_combo.addItem(f"{e.display_name}", e.name)
            names.append(e.name)
        if primary not in names:
            # 对象在用的字体（可能已被隐藏）仍要出现在下拉里，避免回填丢失
            entry = self.manager.get_entry(primary)
            if entry is not None:
                self.font_combo.addItem(f"{entry.display_name}", primary)
        idx = self.font_combo.findData(primary)
        self.font_combo.setCurrentIndex(max(0, idx))
        self.font_combo.blockSignals(False)

    # ------------------------------------------------------------------ 接口
    def set_from_spec(self, spec: TextSpec) -> None:
        """按对象的源参数回填（不触发 propsChanged）。"""
        self._loading = True
        primary = spec.font_names[0] if spec.font_names else ""
        self._reload_fonts(primary)
        self.size_spin.setValue(spec.size)
        self.cs_spin.setValue(spec.char_spacing)
        self.ls_spin.setValue(spec.line_spacing)
        self.fw_spin.setValue(float(getattr(spec, "frame_width", 0.0) or 0.0))
        self._loading = False
        self._last_field = ""

    def values(self) -> dict:
        return {"font": self.font_combo.currentData(),
                "size": self.size_spin.value(),
                "char_spacing": self.cs_spin.value(),
                "line_spacing": self.ls_spin.value(),
                "frame_width": self.fw_spin.value()}

    @property
    def last_field(self) -> str:
        """最近一次被用户改动的字段名（font/size/char_spacing/line_spacing）。"""
        return self._last_field
