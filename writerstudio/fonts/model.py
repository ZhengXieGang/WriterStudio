"""字体数据模型。

统一约定（各解析器都必须产出此格式）：
    * 字形笔画坐标使用 **字体单位**，**Y 轴向上**，**基线在 y=0**，
      **排版原点在 x=0**（墨水可能因字体边距内缩，属正常）。
    * ``advance`` 为字形前进宽度（字体单位），决定了下一个字符的起点。
    * ``units_per_em`` 定义字体单位与 em 的换算；页面尺寸(mm) = 字号(mm) / units_per_em。
    * 单个字形只含笔画（单线），不含填充，可直接用于写字机。

这样文本排版只需 ``scale = size_mm / units_per_em``，无需关心来源是
Hershey、gcode 字符库、中文单线字库还是 TrueType。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..core.geometry import BBox, Vec2
from ..core.strokes import Stroke

KIND_HERSHEY = "hershey"
KIND_GCODE = "gcode"
KIND_STROKE_JSON = "stroke-json"
KIND_TRUETYPE = "truetype"


def _pct(vals: list[float], q: float) -> float:
    """分位数（q∈[0,1]）；空列表返回 0。"""
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q))]


@dataclass
class Glyph:
    """一个字形：若干笔画（字体单位，Y 向上，基线 y=0，左边界 x=0）。"""

    char: str
    strokes: list[list[Vec2]] = field(default_factory=list)
    advance: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not any(self.strokes)

    def bbox(self) -> BBox:
        return BBox.from_points(p for s in self.strokes for p in s)

    def to_strokes(self, scale: float, origin: Vec2 = (0.0, 0.0)) -> list[Stroke]:
        """按 scale 缩放到页面单位，并平移到 origin。

        产出的笔画带 ``role="glyph"`` 标记：线条起伏/弯折只作用于图形
        线条（矢量图/表格线等），不作用于文字笔画。
        """
        ox, oy = origin
        out: list[Stroke] = []
        for s in self.strokes:
            if len(s) < 1:
                continue
            out.append(Stroke([(ox + x * scale, oy + y * scale) for x, y in s],
                              role="glyph"))
        return out


@dataclass
class FontFamily:
    """一套字体的字形集合与度量。"""

    name: str
    kind: str
    units_per_em: float = 32.0
    glyphs: dict[str, Glyph] = field(default_factory=dict)
    source: str = ""
    display_name: str = ""

    # 度量（字体单位）。由 :meth:`recompute_metrics` 从字形统计得出。
    ascent: float = 0.0
    descent: float = 0.0
    default_advance: float = 0.0

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = self.name
        if self.glyphs and self.ascent == 0.0 and self.descent == 0.0:
            self.recompute_metrics()

    # -- 查询 ---------------------------------------------------------------
    def has(self, ch: str) -> bool:
        return ch in self.glyphs

    def glyph(self, ch: str) -> Optional[Glyph]:
        return self.glyphs.get(ch)

    def advance(self, ch: str) -> float:
        g = self.glyphs.get(ch)
        if g is not None:
            return g.advance
        return self.default_advance

    def coverage(self) -> int:
        return len(self.glyphs)

    # -- 度量 ---------------------------------------------------------------
    def recompute_metrics(self) -> None:
        # 上伸/下延用百分位而非极值：手写字库常有零星超出字身的字形
        # （如写着了的全角符号），max 统计会把行高撑大一截
        tops: list[float] = []
        bottoms: list[float] = []
        total_adv = 0.0
        n = 0
        for g in self.glyphs.values():
            box = g.bbox()
            if not box.is_empty:
                tops.append(box.y1)
                bottoms.append(box.y0)
            if g.advance:
                total_adv += g.advance
                n += 1
        asc = _pct(tops, 0.95) if tops else 0.0
        desc = _pct(bottoms, 0.05) if bottoms else 0.0
        self.ascent = asc if asc > 0 else self.units_per_em * 0.8
        self.descent = desc if desc < 0 else -self.units_per_em * 0.2
        self.default_advance = (total_adv / n) if n else self.units_per_em * 0.5
        if self.default_advance <= 0:
            self.default_advance = self.units_per_em * 0.5

    def scale_for_size(self, size_mm: float) -> float:
        """字号(mm，em 尺寸) → 字体单位到 mm 的缩放系数。"""
        if self.units_per_em <= 0:
            return 1.0
        return size_mm / self.units_per_em

    def line_height(self, size_mm: float, spacing: float = 1.0) -> float:
        scale = self.scale_for_size(size_mm)
        return (self.ascent - self.descent) * scale * spacing

    def __repr__(self) -> str:
        return (f"FontFamily({self.name!r}, kind={self.kind}, "
                f"glyphs={len(self.glyphs)}, upem={self.units_per_em})")


def glyphs_bbox(font: FontFamily, chars: Iterable[str], size_mm: float,
                origin: Vec2 = (0.0, 0.0)) -> BBox:
    """估算某串字符在给定字号下的包围盒（不含字距），用于度量校验。"""
    scale = font.scale_for_size(size_mm)
    ox, oy = origin
    box = BBox()
    x = ox
    for ch in chars:
        g = font.glyph(ch)
        if g is None:
            x += font.default_advance * scale
            continue
        for s in g.strokes:
            for px, py in s:
                box.expand((x + px * scale, oy + py * scale))
        x += g.advance * scale
    return box
