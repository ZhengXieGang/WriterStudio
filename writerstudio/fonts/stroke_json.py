"""单线笔画字库解析器（chinese-hershey-font 的 ``STRK-*.json``）。

JSON 结构::

    {
      "U+4E00": [ [[x, y], [x, y], ...],   # 第 1 笔
                  [[x, y], ...] ],          # 第 2 笔
      ...
    }

坐标为 **0..1 归一化**、**Y 轴向下**。本解析器映射为
**Y 轴向上、基线 y=0（方框下沿）、左边界 x=0**，em = 1.0。

来源：`LingDong-/chinese-hershey-font <https://github.com/LingDong-/chinese-hershey-font>`_
（MIT License）。该工具可由任意 TTF/TTC 生成中文单线字库。
"""

from __future__ import annotations

import json
from array import array
from collections.abc import Mapping
from pathlib import Path

from .model import KIND_STROKE_JSON, FontFamily, Glyph, _pct


def _parse_codepoint(key: str) -> str | None:
    """``"U+4E00"`` → ``"一"``。"""
    try:
        if key.upper().startswith("U+"):
            return chr(int(key[2:], 16))
        if len(key) == 1:
            return key
    except (ValueError, OverflowError):
        return None
    return None


class _PackedStrokeGlyphs(Mapping):
    """字符 → 字形的**懒转换**映射（坐标存成紧凑数组表）。

    8 MB 的中文 JSON 解析出来是 57 万个 Python 浮点的嵌套列表，常驻约
    117 MB（每点 ≈ 210 字节）；同一份字库按 ``array('d')`` 扁平存放只要
    4.6 MB。这里在解析时就把坐标压进三张表，随即丢掉 JSON 对象图：

        ``_glyph_start[i] : _glyph_start[i+1]``    字形 i 的笔画区间
        ``_stroke_start[j] : _stroke_start[j+1]``  笔画 j 的点区间
        ``_pts[2k], _pts[2k+1]``                   第 k 个点的 x/y（Y 向下）

    字形本体仍是首次访问才转换并缓存（一次排版只用几十个字），对外语义与
    普通 ``dict[str, Glyph]`` 一致（len/iter/contains/get/values/keys/items）。
    """

    #: 转换后的字形缓存上限。字形转出来是 Python 元组（每点 ≈ 200 字节，
    #: 整套 2 万字库 ≈ 100 MB），全量缓存等于把紧凑存储又还原回去；实际一次
    #: 排版只用几十到几百个字，故设上限、超了整体清空（转换一个字形约 2µs，
    #: 清空重建的代价可忽略，换来内存有界）。
    CACHE_LIMIT = 2048

    __slots__ = ("_chars", "_glyph_start", "_stroke_start", "_pts", "_cache")

    def __init__(self, chars: dict[str, int], glyph_start: array,
                 stroke_start: array, pts: array) -> None:
        self._chars = chars
        self._glyph_start = glyph_start
        self._stroke_start = stroke_start
        self._pts = pts
        self._cache: dict[str, Glyph] = {}

    def __len__(self) -> int:
        return len(self._chars)

    def __iter__(self):
        return iter(self._chars)

    def __contains__(self, ch: object) -> bool:
        return ch in self._chars

    def __getitem__(self, ch: str) -> Glyph:
        cache = self._cache
        g = cache.get(ch)
        if g is None:
            g = self._convert(self._chars[ch], ch)
            if len(cache) >= self.CACHE_LIMIT:
                cache.clear()
            cache[ch] = g
        return g

    def get(self, ch: str, default=None):
        if ch in self._chars:
            return self[ch]
        return default

    def keys(self):
        return self._chars.keys()

    def values(self):
        return [self[ch] for ch in self._chars]

    def items(self):
        return [(ch, self[ch]) for ch in self._chars]

    def _convert(self, i: int, ch: str) -> Glyph:
        pts = self._pts
        strokes = []
        for j in range(self._glyph_start[i], self._glyph_start[i + 1]):
            a = self._stroke_start[j] * 2
            b = self._stroke_start[j + 1] * 2
            # Y 向下 0..1 → Y 向上、下沿为基线 0；x 保持 0..1
            strokes.append([(pts[k], 1.0 - pts[k + 1])
                            for k in range(a, b, 2)])
        return Glyph(char=ch, strokes=strokes, advance=1.0)


def _iter_top_level(text: str):
    """逐条产出顶层 ``key, 值``——不一次性建出整个对象图。

    8.8 MB 的中文字库整份 ``json.loads`` 会在解析瞬间占用约 120 MB（57 万个
    Python 浮点的嵌套列表）；逐条解析把峰值压到「文本 + 单个字形」。
    只认「顶层是对象、值是数组」这一种结构（本模块自己的格式）。
    """
    dec = json.JSONDecoder()
    n = len(text)
    i = text.index("{") + 1
    while True:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] == "}":
            return
        key, i = dec.raw_decode(text, i)
        while i < n and text[i] in " \t\r\n:":
            i += 1
        value, i = dec.raw_decode(text, i)
        yield key, value


def _pack_glyphs(pairs) -> tuple[dict[str, int], array, array, array]:
    """把 ``(码点键, [[[x,y],...], ...])`` 压成紧凑数组表（布局见上文）。

    ``pairs`` 可以是生成器——每个字形的原始列表用完即弃，不在内存里堆积。
    """
    chars: dict[str, int] = {}
    glyph_start = array("i", [0])
    stroke_start = array("i", [0])
    pts = array("d")
    for key, raw_strokes in pairs:
        ch = _parse_codepoint(key)
        if ch is None:
            continue
        chars[ch] = len(chars)
        for s in raw_strokes:
            if not s:
                continue
            for pt in s:
                pts.append(pt[0])
                pts.append(pt[1])
            stroke_start.append(len(pts) // 2)
        glyph_start.append(len(stroke_start) - 1)
    return chars, glyph_start, stroke_start, pts


def parse_stroke_json(path: str | Path, name: str | None = None) -> FontFamily:
    """解析单线笔画 JSON 为 :class:`FontFamily`。

    加载即把坐标压成紧凑数组（字形本体仍是首次使用才转换），度量按
    Y 范围扫描现算——大字库（2 万字形 / 57 万点）常驻内存从 117 MB
    降到 ~5 MB。
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        packed = _pack_glyphs(_iter_top_level(text))
    except Exception:
        # 顶层结构不是预期的「对象套数组」（手改过的字库等）：退回整份解析
        packed = _pack_glyphs(iter(json.loads(text).items()))
    del text
    chars, glyph_start, stroke_start, pts = packed
    if not chars:
        raise ValueError(f"未能从 {path} 解析出任何字形")

    # 度量与 model.FontFamily.recompute_metrics 语义逐比特一致：
    # 上伸/下延用 95%/5% 分位而非极值；advance 恒为 1.0。
    # 逐字形取 y 切片求极值（切片/极值都在 C 层，比逐点 Python 循环快）
    tops: list[float] = []
    bottoms: list[float] = []
    for i in range(len(chars)):
        ymin = ymax = None
        for j in range(glyph_start[i], glyph_start[i + 1]):
            ys = pts[stroke_start[j] * 2 + 1: stroke_start[j + 1] * 2: 2]
            if not ys:
                continue
            lo, hi = min(ys), max(ys)
            ymin = lo if ymin is None or lo < ymin else ymin
            ymax = hi if ymax is None or hi > ymax else ymax
        if ymin is not None:
            tops.append(1.0 - ymin)        # 翻转后的 y1
            bottoms.append(1.0 - ymax)     # 翻转后的 y0
    asc = _pct(tops, 0.95) if tops else 0.0
    desc = _pct(bottoms, 0.05) if bottoms else 0.0
    ascent = asc if asc > 0 else 0.8       # units_per_em = 1.0
    descent = desc if desc < 0 else -0.2

    return FontFamily(
        name=name or path.stem,
        kind=KIND_STROKE_JSON,
        units_per_em=1.0,
        glyphs=_PackedStrokeGlyphs(chars, glyph_start, stroke_start, pts),
        source=str(path),
        ascent=ascent,
        descent=descent,
        default_advance=1.0,
    )


def load_stroke_json_dir(directory: str | Path) -> dict[str, FontFamily]:
    directory = Path(directory)
    fonts: dict[str, FontFamily] = {}
    for p in sorted(directory.glob("*.json")):
        try:
            f = parse_stroke_json(p)
        except Exception:
            continue
        fonts[f.name] = f
    return fonts
