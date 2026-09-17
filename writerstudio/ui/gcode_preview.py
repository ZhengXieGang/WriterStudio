"""G-code 预览对话框：路径可视化 + 文本 + 统计。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.strokes import Stroke
from ..machine.gcode_gen import GCodeResult, format_duration


class GCodePreview(QWidget):
    """把 G-code 的绘制路径画出来（含空程虚线与起终点）。

    路径在 :meth:`set_paths` 里一次性构建成 :class:`QPainterPath`（mm 坐标），
    绘制时经画笔变换映射到像素——整页手写（数万段）重绘从逐段 ``drawLine``
    变成一次 ``drawPath``，拖动分隔条/缩放窗口不再发卡。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.strokes: list[Stroke] = []
        self.travels: list[tuple[tuple[float, float], tuple[float, float]]] = []
        self._ink_path: Optional[QPainterPath] = None
        self._travel_path: Optional[QPainterPath] = None
        self._rect_cache = None
        self.setMinimumSize(360, 280)

    def set_paths(self, strokes: list[Stroke],
                  travels: Optional[list[tuple]] = None) -> None:
        self.strokes = strokes
        self.travels = travels or []
        self._ink_path = None
        self._travel_path = None
        self._rect_cache = None
        self.update()

    def _content_rect(self):
        if self._rect_cache is not None:
            return self._rect_cache
        from ..core.geometry import BBox
        box = BBox()
        for s in self.strokes:
            for p in s.points:
                box.expand(p)
        for a, b in self.travels:
            box.expand(a)
            box.expand(b)
        self._rect_cache = box
        return box

    def _build_paths(self) -> None:
        """mm 坐标的绘制路径与空程路径（各构建一次，重绘复用）。"""
        ink = QPainterPath()
        for s in self.strokes:
            if len(s.points) < 2:
                continue
            ink.moveTo(*s.points[0])
            for p in s.points[1:]:
                ink.lineTo(*p)
        travel = QPainterPath()
        for a, b in self.travels:
            travel.moveTo(*a)
            travel.lineTo(*b)
        self._ink_path = ink
        self._travel_path = travel

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(255, 255, 255))
        box = self._content_rect()
        if box.is_empty:
            p.setPen(QColor(120, 120, 120))
            p.drawText(self.rect(), Qt.AlignCenter, "无路径")
            return

        w = max(1e-6, box.width)
        h = max(1e-6, box.height)
        margin = 12.0
        sx = (self.width() - 2 * margin) / w
        sy = (self.height() - 2 * margin) / h
        s = min(sx, sy)

        if self._ink_path is None or self._travel_path is None:
            self._build_paths()

        # mm（Y 向上）→ 像素（Y 向下）的画笔变换；线宽用 cosmetic 保持像素宽
        p.save()
        p.translate(margin - box.x0 * s, self.height() - margin - box.y0 * s)
        p.scale(s, -s)
        tp = QPen(QColor(200, 120, 120))
        tp.setCosmetic(True)
        tp.setStyle(Qt.DashLine)
        p.setPen(tp)
        p.drawPath(self._travel_path)
        pen = QPen(QColor(30, 60, 160))
        pen.setCosmetic(True)
        pen.setWidthF(1.3)
        p.setPen(pen)
        p.drawPath(self._ink_path)
        p.restore()

        # 起点标记
        if self.strokes:
            first = self.strokes[0].points[0]
            fx = margin - box.x0 * s + first[0] * s
            fy = self.height() - margin - box.y0 * s - first[1] * s
            p.setBrush(QColor(0, 160, 0))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(fx, fy), 4, 4)


class GCodePreviewDialog(QDialog):
    def __init__(self, result: GCodeResult, strokes: list[Stroke],
                 travels: Optional[list[tuple]] = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("G-code 预览")
        self.resize(900, 620)

        root = QVBoxLayout(self)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split, 1)

        self.preview = GCodePreview()
        self.preview.set_paths(strokes, travels)
        split.addWidget(self.preview)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        f = QFont("monospace")
        f.setStyleHint(QFont.Monospace)
        self.text.setFont(f)
        self.text.setPlainText(result.text())
        split.addWidget(self.text)
        split.setSizes([520, 380])

        info_text = (
            f"共 {len(result.lines)} 行　|　笔画 {result.stroke_count} 条　|　"
            f"绘制 {result.draw_length:.1f} mm　|　空程 {result.travel_length:.1f} mm　|　"
            f"预计 {format_duration(result.estimated_seconds)}")
        if getattr(result, "suggested_draw_feed", 0.0) > 0:
            # 加速度感知估算发现「设置的进给跑不出来」：短笔画受加速度
            # 限制峰速有物理上限，给一条可达速度的建议（只建议不代改）
            info_text += (
                f"\n提示：当前书写速度按本机加速度跑不满，"
                f"一半笔画实际可达约 {result.suggested_draw_feed:.0f} mm/min"
                f"（耗时估算已按真实速度模型计算）")
        info = QLabel(info_text)
        info.setWordWrap(True)
        root.addWidget(info)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)
