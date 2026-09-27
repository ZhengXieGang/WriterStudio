"""导出选项对话框：PNG 分辨率（DPI）。

DPI 决定导出位图的像素尺寸（页面物理尺寸固定不变）。对话框实时显示
换算结果，避免用户在不清楚「300 DPI 到底多大」的情况下导出一张
几百 MB 的图。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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


class PngExportDialog(QDialog):
    """选择 PNG 导出分辨率（预设按钮 + 任意输入）。"""

    def __init__(self, page, dpi: int = 300,
                 parent=None) -> None:
        super().__init__(parent)
        self._page = page
        self.setWindowTitle("导出为 PNG")
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("分辨率", self))
        self.dpi_spin = QSpinBox(self)
        self.dpi_spin.setRange(36, 1200)
        self.dpi_spin.setSingleStep(10)
        self.dpi_spin.setSuffix(" DPI")
        self.dpi_spin.setValue(int(dpi))
        row.addWidget(self.dpi_spin, 1)
        for v in PRESET_DPIS:
            btn = QPushButton(str(v), self)
            btn.setToolTip(f"设为 {v} DPI")
            btn.clicked.connect(lambda _=False, v=v: self.dpi_spin.setValue(v))
            row.addWidget(btn)
        root.addLayout(row)

        self._size_label = QLabel(self)
        self._size_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._size_label.setWordWrap(True)
        root.addWidget(self._size_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("导出…")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.dpi_spin.valueChanged.connect(self._refresh)
        self._refresh()

    # ------------------------------------------------------------ 逻辑
    def dpi(self) -> int:
        return int(self.dpi_spin.value())

    def _refresh(self) -> None:
        w, h = pixel_size(self._page, self.dpi())
        text = (f"输出尺寸：{w} × {h} 像素"
                f"（页面 {self._page.width:g} × {self._page.height:g} mm）")
        if w * h > _LARGE_PIXELS:
            self._size_label.setStyleSheet("color: #c0392b;")
            text += "\n尺寸很大，导出会占用较多内存与时间"
        else:
            self._size_label.setStyleSheet("")
        self._size_label.setText(text)

    @classmethod
    def ask(cls, page, default_dpi: int = 300,
            parent=None) -> Optional[int]:
        """弹窗询问分辨率：确认返回 DPI，取消返回 None。"""
        dlg = cls(page, default_dpi, parent)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg.dpi()
        return None
