"""笔画数据模型。

一条 :class:`Stroke` = 一次落笔到抬笔之间的连续轨迹（折线，单位 mm）。
文档中各对象先以「本地笔画」存在，再通过对象的仿射变换映射到页面坐标。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from .geometry import BBox, Vec2

# 按毫米换算的最大安全笔速(仅用于估算，P5 机器模块会替换)
DEFAULT_FEED_MM_MIN = 1500.0


@dataclass
class Stroke:
    """一条连续笔迹（折线）。`closed=True` 表示首尾闭合。

    ``role`` 标记笔画来源，供扰动引擎区分效果作用面：
    ``"glyph"`` = 字体字形笔画（线条起伏/弯折不作用于文字）；
    ``""`` = 未标注（默认，按图形线条对待，如矢量图/表格线/手绘）。

    ``group`` 标记笔画所属的**书写分组**（如某个字符/某个对象），
    用于笔序规划时「写完一组再写下一组」，避免全局贪心把书写顺序打乱。
    ``-1`` = 未分组。
    """

    points: list[Vec2] = field(default_factory=list)
    closed: bool = False
    role: str = ""
    group: int = -1

    def __post_init__(self) -> None:
        # 允许传入元组/列表混合，统一成 list[tuple]
        self.points = [(float(x), float(y)) for x, y in self.points]

    def __len__(self) -> int:
        return len(self.points)

    @property
    def is_empty(self) -> bool:
        return len(self.points) == 0

    def start(self) -> Vec2:
        return self.points[0]

    def end(self) -> Vec2:
        return self.points[-1]

    def bbox(self) -> BBox:
        return BBox.from_points(self.points)

    def length(self) -> float:
        """折线总长度(mm)。"""
        total = 0.0
        for (x0, y0), (x1, y1) in zip(self.points, self.points[1:]):
            total += math.hypot(x1 - x0, y1 - y0)
        if self.closed and len(self.points) >= 2:
            (x0, y0), (x1, y1) = self.points[-1], self.points[0]
            total += math.hypot(x1 - x0, y1 - y0)
        return total

    def transformed(self, transform) -> Stroke:
        """返回应用仿射变换后的新笔画（依赖 duck typing，避免循环导入）。"""
        return Stroke(transform.apply_many(self.points), self.closed,
                      self.role, self.group)

    def clone(self) -> Stroke:
        return Stroke(list(self.points), self.closed, self.role, self.group)


def strokes_bbox(strokes: Iterable[Stroke]) -> BBox:
    box = BBox()
    for s in strokes:
        for p in s.points:
            box.expand(p)
    return box


def total_length(strokes: Iterable[Stroke]) -> float:
    return sum(s.length() for s in strokes)
