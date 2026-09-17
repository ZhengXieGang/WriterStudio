"""TrueType/OpenType 字体解析器（fontTools）。

TTF/OTF 字形是**填充轮廓**（闭合曲线），写字机无法"填充"，因此这里提取轮廓线
（空心字模式），并把二次/三次贝塞尔曲线离散成折线。这与 GRBL-Plotter / Candle
等上位机对 TTF 的处理方式一致。

坐标：fontTools 输出的字形坐标已是 **Y 轴向上、基线 y=0**，与项目约定一致，
无需翻转。

若需把 TTF 转成"单线字"（骨架提取），属后续增强方向。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from .model import KIND_TRUETYPE, FontFamily, Glyph

try:
    from fontTools.ttLib import TTFont
    from fontTools.pens.basePen import BasePen

    _HAVE_FONTTOOLS = True
except Exception:  # pragma: no cover - fontTools 是必需依赖，正常不会走到
    _HAVE_FONTTOOLS = False


class _FlattenPen(BasePen):
    """把字形轮廓记录为一组折线（轮廓），曲线按容差离散。"""

    def __init__(self, glyphSet, tolerance: float = 0.6) -> None:
        super().__init__(glyphSet)
        self.tolerance = tolerance
        self.strokes: list[list[tuple[float, float]]] = []
        self._cur: list[tuple[float, float]] = []

    # -- 基础图元 -----------------------------------------------------------
    def _moveTo(self, pt) -> None:
        if self._cur:
            self.strokes.append(self._cur)
        self._cur = [pt]

    def _lineTo(self, pt) -> None:
        self._cur.append(pt)

    def _closePath(self) -> None:
        if self._cur:
            if self._cur[0] != self._cur[-1]:
                self._cur.append(self._cur[0])
            self.strokes.append(self._cur)
            self._cur = []

    def _endPath(self) -> None:
        if self._cur:
            self.strokes.append(self._cur)
            self._cur = []

    # -- 曲线离散 -----------------------------------------------------------
    def _curveToOne(self, p1, p2, p3) -> None:
        p0 = self._getCurrentPoint()
        self._flatten_cubic(p0, p1, p2, p3)

    def _qCurveToOne(self, p1, p2) -> None:
        p0 = self._getCurrentPoint()
        # 升阶为三次
        c1 = (p0[0] + 2.0 / 3.0 * (p1[0] - p0[0]), p0[1] + 2.0 / 3.0 * (p1[1] - p0[1]))
        c2 = (p2[0] + 2.0 / 3.0 * (p1[0] - p2[0]), p2[1] + 2.0 / 3.0 * (p1[1] - p2[1]))
        self._flatten_cubic(p0, c1, c2, p2)

    def _flatten_cubic(self, p0, p1, p2, p3, depth: int = 0) -> None:
        if depth > 16 or self._flat_enough(p0, p1, p2, p3):
            self._cur.append(p3)
            return
        # de Casteljau 二分
        p01 = _mid(p0, p1)
        p12 = _mid(p1, p2)
        p23 = _mid(p2, p3)
        p012 = _mid(p01, p12)
        p123 = _mid(p12, p23)
        p0123 = _mid(p012, p123)
        self._flatten_cubic(p0, p01, p012, p0123, depth + 1)
        self._flatten_cubic(p0123, p123, p23, p3, depth + 1)

    def _flat_enough(self, p0, p1, p2, p3) -> bool:
        d1 = _dist_to_line(p1, p0, p3)
        d2 = _dist_to_line(p2, p0, p3)
        return max(d1, d2) <= self.tolerance


def _mid(a, b):
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _dist_to_line(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    denom = math.hypot(dx, dy)
    if denom < 1e-12:
        return math.hypot(px - ax, py - ay)
    return abs(dx * (ay - py) - (ax - px) * dy) / denom


def parse_truetype(path: str | Path, name: str | None = None,
                   tolerance: float | None = None) -> FontFamily:
    """解析 TTF/OTF 为轮廓模式的 :class:`FontFamily`。

    ``tolerance`` 为曲线离散容差（字体单位）；默认按 em 的 0.2% 自适应
    （10mm 字号 ≈ 0.02mm，与机器步进分辨率同量级）。固定小容差会让
    大 upem 的 CJK 字体被海量过采样（Noto Sans 全量解析十几秒）。
    """
    if not _HAVE_FONTTOOLS:
        raise RuntimeError("需要 fontTools 才能解析 TrueType 字体")
    path = Path(path)
    tt = TTFont(str(path), fontNumber=0, lazy=True)
    try:
        upem = float(tt["head"].unitsPerEm)
        if tolerance is None:
            tolerance = max(0.05, upem * 2e-3)
        glyph_set = tt.getGlyphSet()
        cmap = tt.getBestCmap() or {}
        hmtx = tt["hmtx"]

        # 按 unicode 排序，保证空格等也纳入
        glyphs: dict[str, Glyph] = {}
        for codepoint, glyph_name in cmap.items():
            if codepoint < 32:
                continue
            try:
                pen = _FlattenPen(glyph_set, tolerance=tolerance)
                glyph_set[glyph_name].draw(pen)
                pen._endPath()
            except Exception:
                continue
            advance = float(hmtx[glyph_name][0]) if glyph_name in hmtx.metrics else upem * 0.5
            strokes = [[(float(x), float(y)) for x, y in s] for s in pen.strokes if len(s) >= 2]
            glyphs[chr(codepoint)] = Glyph(char=chr(codepoint), strokes=strokes, advance=advance)

        if not glyphs:
            raise ValueError(f"{path} 中未找到可用字形")

        fam = FontFamily(
            name=name or path.stem,
            kind=KIND_TRUETYPE,
            units_per_em=upem,
            glyphs=glyphs,
            source=str(path),
        )
        fam.display_name = _family_name(tt) or fam.name
        return fam
    finally:
        tt.close()


def _family_name(tt) -> Optional[str]:
    try:
        for rec in tt["name"].names:
            if rec.nameID == 1:
                try:
                    return rec.toUnicode()
                except Exception:
                    continue
    except Exception:
        pass
    return None


def load_truetype_dir(directory: str | Path, tolerance: float | None = None,
                      limit: int | None = None) -> dict[str, FontFamily]:
    directory = Path(directory)
    fonts: dict[str, FontFamily] = {}
    count = 0
    for p in sorted(directory.rglob("*")):
        if p.suffix.lower() not in (".ttf", ".otf"):
            continue
        if limit is not None and count >= limit:
            break
        try:
            f = parse_truetype(p, tolerance=tolerance)
        except Exception:
            continue
        fonts[f.name] = f
        count += 1
    return fonts
