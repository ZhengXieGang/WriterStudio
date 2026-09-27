"""页面导出：PNG 位图与 SVG 矢量图。

两种格式都按**页面物理尺寸**输出——SVG 的 1 用户单位 = 1 mm、PNG 按 DPI
换算像素——打印或导入矢量软件时与纸面 1:1 对应。只导出可见对象；页边距、
参考层等编辑辅助不进入输出（那是定位用的，不属于作品内容）。
"""

from __future__ import annotations

from .core.document import Document

#: 笔迹线宽（mm）：与 G-code 实际书写、AI 预览的观感一致
STROKE_WIDTH_MM = 0.35
#: PNG 导出的默认分辨率（300 DPI 为印刷级）
DEFAULT_DPI = 300.0

_MM_PER_INCH = 25.4


def _iter_strokes(doc: Document):
    """按 z 序产出所有可见对象的页面坐标笔画。"""
    for obj in doc.objects:
        if not obj.visible:
            continue
        for s in obj.world_strokes():
            if s.points:
                yield s


def render_page_image(doc: Document, dpi: float = DEFAULT_DPI):
    """把页面渲染为白底黑线的 ``QImage``。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

    page = doc.page
    scale = dpi / _MM_PER_INCH                     # px / mm
    w = max(1, round(page.width * scale))
    h = max(1, round(page.height * scale))
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor("#ffffff"))

    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor("#111111"), max(1.0, STROKE_WIDTH_MM * scale))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    path = QPainterPath()
    for s in _iter_strokes(doc):
        pts = s.points
        path.moveTo(pts[0][0] * scale, (page.height - pts[0][1]) * scale)
        for x, y in pts[1:]:
            path.lineTo(x * scale, (page.height - y) * scale)
        if len(pts) == 1:                          # 单点笔画按圆点画出
            path.lineTo(pts[0][0] * scale, (page.height - pts[0][1]) * scale)
        if s.closed:
            path.closeSubpath()
    painter.drawPath(path)
    painter.end()
    return img


def save_page_png(doc: Document, path: str, dpi: float = DEFAULT_DPI
                  ) -> tuple[int, int]:
    """导出 PNG，返回 (宽, 高) 像素。"""
    img = render_page_image(doc, dpi)
    if not img.save(str(path), "PNG"):
        raise OSError(f"PNG 写入失败：{path}")
    return img.width(), img.height()


def page_to_svg(doc: Document, stroke_width_mm: float = STROKE_WIDTH_MM) -> str:
    """页面 → SVG 文本（1 单位 = 1 mm，y 轴翻转为 SVG 的向下坐标）。"""
    page = doc.page
    w, h = page.width, page.height

    paths: list[str] = []
    for obj in doc.objects:
        if not obj.visible:
            continue
        parts: list[str] = []
        for s in obj.world_strokes():
            pts = s.points
            if not pts:
                continue
            parts.append(f"M{_f(pts[0][0])} {_f(h - pts[0][1])}")
            for x, y in pts[1:]:
                parts.append(f"L{_f(x)} {_f(h - y)}")
            if len(pts) == 1:
                parts.append(f"L{_f(pts[0][0])} {_f(h - pts[0][1])}")
            if s.closed:
                parts.append("Z")
        if parts:
            paths.append("".join(parts))

    body = "\n".join(f'    <path d="{d}"/>' for d in paths)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_f(w)}mm"'
        f' height="{_f(h)}mm" viewBox="0 0 {_f(w)} {_f(h)}">',
        f'  <rect x="0" y="0" width="{_f(w)}" height="{_f(h)}"'
        ' fill="#ffffff"/>',
        f'  <g fill="none" stroke="#111111"'
        f' stroke-width="{_f(stroke_width_mm)}"'
        ' stroke-linecap="round" stroke-linejoin="round">',
    ]
    if body:
        lines.append(body)
    lines += ["  </g>", "</svg>", ""]
    return "\n".join(lines)


def save_page_svg(doc: Document, path: str,
                  stroke_width_mm: float = STROKE_WIDTH_MM) -> int:
    """导出 SVG，返回写出的 ``<path>`` 元素数（= 有内容的可见对象数）。"""
    text = page_to_svg(doc, stroke_width_mm)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as exc:
        raise OSError(f"SVG 写入失败：{exc}") from exc
    return text.count("<path ")


def _f(v: float) -> str:
    """mm 数值格式化：3 位小数、去掉多余的 0，保持文件紧凑。"""
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s
