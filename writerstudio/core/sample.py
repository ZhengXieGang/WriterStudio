"""演示内容生成器（P1 占位）。

P2 引入字体系统后，文字类内容将由字体轮廓/单线字库生成；
P1 阶段用简单几何图形验证画布、选择、移动、缩放、撤销等交互。
"""

from __future__ import annotations

import math

from .document import Document, PageSpec, make_static_object
from .geometry import AffineTransform
from .strokes import Stroke


def make_polyline(points, closed: bool = False) -> Stroke:
    return Stroke(list(points), closed)


def make_rect(x: float, y: float, w: float, h: float) -> Stroke:
    """左下角 (x,y)、宽 w、高 h 的矩形（Y 向上）。"""
    return Stroke([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], closed=True)


def make_star(cx: float, cy: float, r: float, points: int = 5,
              inner_ratio: float = 0.4) -> Stroke:
    """正 n 角星（一笔闭合）。"""
    pts = []
    for i in range(points * 2):
        radius = r if i % 2 == 0 else r * inner_ratio
        angle = math.pi / 2 + i * math.pi / points
        pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return Stroke(pts, closed=True)


def make_text_placeholder(text: str, x: float, y: float, size: float = 10.0) -> Stroke:
    """用锯齿折线模拟一行文字（P1 占位，P2 替换为真实字体渲染）。

    生成一条「高度随字符变化」的连续折线，只为在画布上呈现文字块尺寸。
    """
    pts = []
    cursor = x
    for ch in text:
        h = size * (0.5 + 0.5 * (ord(ch) % 5) / 5.0)
        pts.extend([(cursor, y), (cursor, y + h), (cursor + size * 0.6, y + h),
                    (cursor + size * 0.6, y)])
        cursor += size * 0.8
    return Stroke(pts, closed=False)


def demo_document() -> Document:
    """构造一个含多个对象、便于交互测试的演示文档（A4 横向）。"""
    doc = Document(page=PageSpec(width=297.0, height=210.0, margin=10.0))

    # 1) 矩形（在页面右侧）
    doc.add(make_static_object(
        [make_rect(0.0, 0.0, 60.0, 40.0)],
        name="矩形",
        transform=AffineTransform.translate(200.0, 140.0),
    ))

    # 2) 五角星（页面中部，带旋转）
    doc.add(make_static_object(
        [make_star(0.0, 0.0, 30.0, 5)],
        name="五角星",
        transform=AffineTransform.translate(120.0, 100.0) @ AffineTransform.rotate(15.0),
    ))

    # 3) 模拟一行文字（页面左下）
    doc.add(make_static_object(
        [make_text_placeholder("WriterStudio P1", 0.0, 0.0, 12.0)],
        name="文字占位",
        transform=AffineTransform.translate(40.0, 60.0),
    ))

    # 4) 正弦曲线（演示「手写基线」的几何形态）
    wave = Stroke([(t * 2.0, 8.0 * math.sin(t * 0.5)) for t in range(0, 100)], False)
    doc.add(make_static_object(
        [wave],
        name="正弦线",
        transform=AffineTransform.translate(40.0, 150.0),
    ))

    return doc
