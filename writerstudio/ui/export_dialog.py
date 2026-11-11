"""导出选项对话框：PNG 分辨率与透明背景。

PNG 的 DPI 决定位图像素尺寸（页面物理尺寸固定不变），对话框实时显示
换算结果，避免用户在不清楚「300 DPI 到底多大」的情况下导出一张
几百 MB 的图。SVG 是矢量图，不需要 DPI，只保留透明背景选项。
"""

from __future__ import annotations

from ..i18n import tr, trf
from .style import set_role

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

#: 常用分辨率预设：屏显 / 草稿 / 印刷 / 精细
PRESET_DPIS = (72, 150, 300, 600)
#: 超过该像素总量给出内存提示（约 5000 万像素 ≈ 200 MB RGB32）
_LARGE_PIXELS = 50_000_000

_MM_PER_INCH = 25.4


def pixel_size(page, dpi: float) -> tuple[int, int]:
    """页面在该 DPI 下的像素尺寸——与实际导出的换算保持一致。"""
    return (max(1, round(page.width / _MM_PER_INCH * dpi)),
            max(1, round(page.height / _MM_PER_INCH * dpi)))


class ExportOptionsDialog(QDialog):
    """导出选项：PNG 选分辨率，两种格式都可选透明背景。"""

    def __init__(self, page, dpi: int = 300, fmt: str = "png",
                 transparent: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._page = page
        self._fmt = fmt
        is_png = fmt == "png"
        self.setWindowTitle(tr('导出为 PNG') if is_png else tr('导出为 SVG'))
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)

        self.dpi_spin: Optional[QSpinBox] = None
        if is_png:
            row = QHBoxLayout()
            row.addWidget(QLabel(tr("分辨率"), self))
            spin = QSpinBox(self)
            spin.setRange(36, 1200)
            spin.setSingleStep(10)
            spin.setSuffix(" DPI")
            spin.setValue(int(dpi))
            row.addWidget(spin, 1)
            for v in PRESET_DPIS:
                btn = QPushButton(str(v), self)
                btn.setToolTip(trf('设为 {v0} DPI', v0=v))
                btn.clicked.connect(lambda _=False, v=v: spin.setValue(v))
                row.addWidget(btn)
            root.addLayout(row)
            self.dpi_spin = spin

        self._size_label = QLabel(self)
        self._size_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._size_label.setWordWrap(True)
        root.addWidget(self._size_label)

        self.transparent_check = QCheckBox(tr("透明背景（不输出白色底色）"), self)
        self.transparent_check.setChecked(bool(transparent))
        root.addWidget(self.transparent_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("导出…"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        if self.dpi_spin is not None:
            self.dpi_spin.valueChanged.connect(self._refresh)
        self._refresh()

    # ------------------------------------------------------------ 逻辑
    def dpi(self) -> int:
        return int(self.dpi_spin.value()) if self.dpi_spin is not None else 0

    def transparent(self) -> bool:
        return self.transparent_check.isChecked()

    def options(self) -> tuple[int, bool]:
        """(DPI, 透明背景)；SVG 的 DPI 为 0（不使用）。"""
        return self.dpi(), self.transparent()

    def _refresh(self) -> None:
        if self._fmt != "png":
            set_role(self._size_label, "muted")
            self._size_label.setText(
                trf('输出尺寸：{v0:g} × {v1:g} mm（矢量图，1 单位 = 1 mm）', v0=self._page.width, v1=self._page.height))
            return
        w, h = pixel_size(self._page, self.dpi())
        text = (trf('输出尺寸：{v0} × {v1} 像素（页面 {v2:g} × {v3:g} mm）', v0=w, v1=h, v2=self._page.width, v3=self._page.height))
        if w * h > _LARGE_PIXELS:
            set_role(self._size_label, "danger")
            text += "\n" + tr("尺寸很大，导出会占用较多内存与时间")
        else:
            set_role(self._size_label, "muted")
        self._size_label.setText(text)

    @classmethod
    def ask(cls, page, default_dpi: int = 300, fmt: str = "png",
            transparent: bool = False,
            parent=None) -> Optional[tuple[int, bool]]:
        """弹窗询问导出选项：确认返回 (DPI, 透明背景)，取消返回 None。"""
        dlg = cls(page, default_dpi, fmt=fmt, transparent=transparent,
                  parent=parent)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.options()
        return None
