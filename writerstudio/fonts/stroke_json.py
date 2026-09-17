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


class _LazyStrokeGlyphs(Mapping):
    """字符 → 字形的**懒转换**映射。

    8MB 中文 JSON 若在加载时把 2 万字形全转成元组要 250ms+；实际一次
    排版只用几十个字，故首次访问才转换并缓存。对外语义与普通
    ``dict[str, Glyph]`` 一致（len/iter/contains/get/values/keys/items）。
    """

    __slots__ = ("_raw", "_cache")

    def __init__(self, raw: dict[str, list]):
        self._raw = raw
        self._cache: dict[str, Glyph] = {}

    def __len__(self) -> int:
        return len(self._raw)

    def __iter__(self):
        return iter(self._raw)

    def __contains__(self, ch: object) -> bool:
        return ch in self._raw

    def __getitem__(self, ch: str) -> Glyph:
        g = self._cache.get(ch)
        if g is None:
            g = self._convert(self._raw[ch], ch)
            self._cache[ch] = g
        return g

    def get(self, ch: str, default=None):
        if ch in self._raw:
            return self[ch]
        return default

    def keys(self):
        return self._raw.keys()

    def values(self):
        return [self[ch] for ch in self._raw]

    def items(self):
        return [(ch, self[ch]) for ch in self._raw]

    @staticmethod
    def _convert(raw_strokes, ch: str) -> Glyph:
        strokes = []
        for s in raw_strokes:
            if not s:
                continue
            # Y 向下 0..1 → Y 向上、下沿为基线 0；x 保持 0..1。
            # ``x + 0.0`` 比 float(x) 快（json 数值转 float 的热路径）
            strokes.append([(x + 0.0, 1.0 - y) for x, y in s])
        return Glyph(char=ch, strokes=strokes, advance=1.0)


def parse_stroke_json(path: str | Path, name: str | None = None) -> FontFamily:
    """解析单线笔画 JSON 为 :class:`FontFamily`。

    加载只建「字符 → 原始笔画」索引与度量（y 范围扫描），字形本体
    首次使用才转换——大字库加载从 ~530ms 降到 ~300ms。
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))

    char_raw: dict[str, list] = {}
    for key, raw_strokes in data.items():
        ch = _parse_codepoint(key)
        if ch is None:
            continue
        char_raw[ch] = raw_strokes

    if not char_raw:
        raise ValueError(f"未能从 {path} 解析出任何字形")

    # 度量与 model.FontFamily.recompute_metrics 语义逐比特一致：
    # 上伸/下延用 95%/5% 分位而非极值；advance 恒为 1.0
    tops: list[float] = []
    bottoms: list[float] = []
    for raw_strokes in char_raw.values():
        ymin = ymax = None
        for s in raw_strokes:
            for pt in s:
                y = pt[1]
                if ymin is None:
                    ymin = ymax = y
                elif y < ymin:
                    ymin = y
                elif y > ymax:
                    ymax = y
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
        glyphs=_LazyStrokeGlyphs(char_raw),
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
