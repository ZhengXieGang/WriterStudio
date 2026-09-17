"""字体缩略图的**轻量**预览加载。

缩略图只需要几个示例字符的字形，但 ``entry.load()`` 会全量解析整款字体——
gfont 中文库一款几百毫秒、大型 TrueType 数秒，字体列表的缩略图
就全耗在等待上。本模块按格式**只读取示例字符对应的字形**：

    * ``gfont``    —— 直接从 ZIP 按码点取对应条目（毫秒级）；
    * ``truetype`` —— fontTools 惰性字形集，只 draw 需要的几个字形；
    * 其它格式     —— 本身解析很快（内置 Hershey/gcode 库），返回 None，
                     由调用方退回全量加载。

产出 :class:`FontPreview` 鸭子类型兼容 ``font_thumbnail`` 所需的
``has``/``glyph``/``default_advance``/``units_per_em`` 接口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .model import Glyph

#: 缩略图示例文字（字体缺哪个字就跳过；全缺时调用方回退到任意字形）
SAMPLE_CHARS = "中文Aa123"


@dataclass
class FontPreview:
    """只含示例字形的轻量「字体」。"""

    glyphs: dict[str, Glyph] = field(default_factory=dict)
    units_per_em: float = 100.0
    default_advance: float = 50.0

    def has(self, ch: str) -> bool:
        return ch in self.glyphs

    def glyph(self, ch: str) -> Optional[Glyph]:
        return self.glyphs.get(ch)


def load_preview(path: str | Path, kind: str,
                 sample: str = SAMPLE_CHARS) -> Optional[FontPreview]:
    """按格式轻量加载示例字形；该格式不支持快路径时返回 None。"""
    path = Path(path)
    if kind == "gfont":
        return _from_gfont(path, sample)
    if kind == "truetype":
        return _from_truetype(path, sample)
    return None


def _finish(glyphs: dict[str, Glyph], upem: float) -> Optional[FontPreview]:
    if not glyphs:
        return None
    return FontPreview(glyphs=glyphs, units_per_em=upem,
                       default_advance=upem * 0.5)


def _from_gfont(path: Path, sample: str) -> Optional[FontPreview]:
    import zipfile

    from .gfont import _parse_glyph
    try:
        with zipfile.ZipFile(path) as zf:
            # 示例字符都是 BMP 码点，条目名就是十进制码点字符串——
            # 直接查表，避免对几万个条目名逐个做码点解析
            names = set(zf.namelist())
            glyphs: dict[str, Glyph] = {}
            upem = 100.0
            for ch in sample:
                name = str(ord(ch))
                if name not in names:
                    continue
                try:
                    r = _parse_glyph(zf.read(name))
                except Exception:
                    continue
                if r is None:
                    continue
                _cp, strokes, bbox = r
                x0, y0, x1, y1 = bbox
                ink_w = max(x1 - x0, 1e-3)
                ink_h = max(y1 - y0, 1e-3)
                # 原始坐标 **Y 向下**，与渲染端（Y 向上）相反：不翻转缩略图
                # 就是上下颠倒的。绕 0 翻即可，整体位置由缩略图归一化。
                flipped = [[(x, -y) for x, y in s] for s in strokes]
                glyphs[ch] = Glyph(char=ch, strokes=flipped,
                                   advance=ink_w)      # 步距先记墨宽，下面统一加间隙
                upem = max(upem, ink_h * 1.1)
            if not glyphs:
                return None
            # 统一字间距：间隙按「最高字的墨高」的固定比例，而不是各字
            # 自身墨宽的比例——否则「中」前宽后松、拉丁字母挤在一起
            cell = max((g.bbox().height for g in glyphs.values() if g.strokes),
                       default=upem)
            for g in glyphs.values():
                g.advance = g.advance + cell * 0.28
            return _finish(glyphs, upem)
    except Exception:
        return None


def _from_truetype(path: Path, sample: str) -> Optional[FontPreview]:
    try:
        from fontTools.ttLib import TTFont
    except Exception:
        return None
    from .truetype import _FlattenPen
    tt = None
    try:
        tt = TTFont(str(path), fontNumber=0, lazy=True)
        upem = float(tt["head"].unitsPerEm)
        cmap = tt.getBestCmap() or {}
        glyph_set = tt.getGlyphSet()
        hmtx = tt["hmtx"]
        tol = max(0.05, upem * 2e-3)
        glyphs: dict[str, Glyph] = {}
        for ch in sample:
            gn = cmap.get(ord(ch))
            if gn is None:
                continue
            pen = _FlattenPen(glyph_set, tolerance=tol)
            try:
                glyph_set[gn].draw(pen)
                pen._endPath()
            except Exception:
                continue
            strokes = [[(float(x), float(y)) for x, y in s]
                       for s in pen.strokes if len(s) >= 2]
            if not strokes:
                continue
            adv = float(hmtx[gn][0]) if gn in hmtx.metrics else upem * 0.5
            glyphs[ch] = Glyph(char=ch, strokes=strokes, advance=adv)
        return _finish(glyphs, upem)
    except Exception:
        return None
    finally:
        if tt is not None:
            tt.close()
