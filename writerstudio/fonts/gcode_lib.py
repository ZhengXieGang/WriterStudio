"""gcode 字符库解析器（``text-to-gcode`` 风格）。

格式：**每个字符一个文件**，文件内为逐行 G 代码::

    (a)                 ← 首行注释标注字符（可选）
    G0 X0.13 Y3.19      ← 抬笔快速移动
    G1 X0.60 Y4.09      ← 落笔划线
    ...

坐标为 mm，**Y 轴向上、基线 y=0**（大写约 6mm、x 高约 4.4mm）。
本解析器据此推断 em（约 9mm）并归一化到统一字体单位。

来源：`Stypox/text-to-gcode <https://github.com/Stypox/text-to-gcode>`_（无 License，
此处仅支持读取该**数据格式**，不复制其代码）。
"""

from __future__ import annotations

import re
from pathlib import Path

from .model import KIND_GCODE, FontFamily, Glyph

_COORD_RE = re.compile(r"([XY])\s*(-?\d+(?:\.\d+)?)")
_CMD_RE = re.compile(r"^(G0?0|G0?1)\b", re.IGNORECASE)
_COMMENT_RE = re.compile(r"^\((.*)\)\s*$")

# 该字库的推断 em（大写高约 6.15mm，6.15 / 0.68 ≈ 9.0）
_GCODE_EM = 9.0


def parse_gcode_char(path: str | Path) -> tuple[str, Glyph] | None:
    """解析单个字符文件，返回 (字符, Glyph)。无法解析时返回 None。"""
    path = Path(path)
    char: str | None = None
    strokes: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    pen_down = False
    last: tuple[float, float] | None = None
    min_x: float | None = None     # 用实际最小 X，避免全正坐标时左侧留缺口

    for line in path.read_text(encoding="latin-1").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _COMMENT_RE.match(line)
        if m:
            if char is None:
                text = m.group(1).strip()
                if len(text) == 1:
                    char = text
            continue
        cmd = _CMD_RE.match(line)
        if not cmd:
            continue
        is_move = cmd.group(1).upper() in ("G1", "G01")
        coords = {k.upper(): float(v) for k, v in _COORD_RE.findall(line)}
        x = coords.get("X", last[0] if last else 0.0)
        y = coords.get("Y", last[1] if last else 0.0)
        last = (x, y)
        if min_x is None or x < min_x:
            min_x = x
        if is_move:
            if not pen_down:
                # 落笔起始：新起一笔
                if cur:
                    strokes.append(cur)
                cur = []
                pen_down = True
            cur.append((x, y))
        else:
            # 抬笔移动
            if pen_down:
                if cur:
                    strokes.append(cur)
                cur = []
                pen_down = False
    if cur:
        strokes.append(cur)

    if char is None:
        # 无注释：仅当文件名为单字符时采用
        stem = path.stem
        if len(stem) == 1:
            char = stem
        else:
            return None

    # 平移到左边界 x=0（无坐标时 min_x 为 None，直接取 0 避免除/减 None）
    if min_x is None:
        min_x = 0.0
    shifted = [[(x - min_x, y) for x, y in s] for s in strokes]
    advance = 0.0
    for s in shifted:
        for x, _ in s:
            advance = max(advance, x)
    advance = max(advance, 1.0)
    # 轻微右留白
    advance += _GCODE_EM * 0.06
    return char, Glyph(char=char, strokes=shifted, advance=advance)


def parse_gcode_char_dir(directory: str | Path, name: str | None = None) -> FontFamily:
    """解析一个字符库目录（可含子目录）为 :class:`FontFamily`。"""
    directory = Path(directory)
    glyphs: dict[str, Glyph] = {}
    for p in directory.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".nc", ".gcode", ".ngc", ".txt"):
            continue
        try:
            result = parse_gcode_char(p)
        except Exception:
            continue
        if result is None:
            continue
        ch, g = result
        glyphs[ch] = g

    if not glyphs:
        raise ValueError(f"未能从 {directory} 解析出任何字形")

    return FontFamily(
        name=name or directory.name,
        kind=KIND_GCODE,
        units_per_em=_GCODE_EM,
        glyphs=glyphs,
        source=str(directory),
    )
