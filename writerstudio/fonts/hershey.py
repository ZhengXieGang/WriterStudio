"""Hershey 单线字体解析器（``.jhf``）。

格式（源自 USENET/James Hurt 的 "fixed" jhf）：
    每条记录 = 5 位字形编号 + 3 位字符数 + 数据
    数据 = 左边界 + 右边界 + 若干 (x,y) 点对，全部以 ``'R'``(0x52) 为原点偏移
    点对 (-50, 0) 表示 **抬笔**（笔画分隔）
    字形按 **位置** 映射到字符：``chr(32 + 序号)``

原始坐标为 **Y 轴向下**，基线在 ``y=9``（大写范围 -12..9，大写高 21 单位）。
本解析器统一输出为 **Y 轴向上、基线 y=0、左边界 x=0**。

Hershey 字体许可：可自由用于任何目的（含商业），需随字库数据保留对
Dr. A. V. Hershey (NBS) 与 James Hurt 的致谢。全文见内置字库目录下的
``builtin/hershey/ACKNOWLEDGEMENT.txt``。
"""

from __future__ import annotations

from pathlib import Path

from .model import KIND_HERSHEY, FontFamily, Glyph

_OFFSET = ord("R")          # 82，坐标原点字符
_PEN_UP = (-50, 0)
_HERSHEY_BASELINE = 9       # 原始（Y 向下）坐标系中的基线
_HERSHEY_EM = 32.0          # 名义 em（大写高 21 / em ≈ 0.66）


def _decode_record(rec: str) -> tuple[float, float, list[list[tuple[int, int]]]]:
    """把一条记录解码为 (left, right, strokes)，坐标为原始 int 单位。"""
    length = int(rec[5:8])
    body = rec[8:]
    if len(body) != length * 2:
        raise ValueError(f"字形长度不匹配：字段 {length}，实际 {len(body) // 2}")
    left = ord(body[0]) - _OFFSET
    right = ord(body[1]) - _OFFSET
    data = body[2:]
    strokes: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    for i in range(0, len(data) - 1, 2):
        x = ord(data[i]) - _OFFSET
        y = ord(data[i + 1]) - _OFFSET
        if (x, y) == _PEN_UP:
            if cur:
                strokes.append(cur)
                cur = []
        else:
            cur.append((x, y))
    if cur:
        strokes.append(cur)
    return left, right, strokes


def _split_records(text: str) -> list[str]:
    """按记录头（5+3 位数字）切分，并把跨行数据拼回同一条记录。"""
    records: list[str] = []
    buf = ""
    for line in text.splitlines():
        stripped = line
        is_header = (
            len(stripped) >= 8
            and stripped[:5].strip().isdigit()
            and stripped[5:8].strip().isdigit()
        )
        if is_header:
            if buf:
                records.append(buf)
            buf = stripped
        elif buf:
            buf += stripped
    if buf:
        records.append(buf)
    return records


def parse_hershey_jhf(path: str | Path, name: str | None = None) -> FontFamily:
    """解析 ``.jhf`` 文件为 :class:`FontFamily`。"""
    path = Path(path)
    raw = path.read_text(encoding="latin-1")
    records = _split_records(raw)

    glyphs: dict[str, Glyph] = {}
    for idx, rec in enumerate(records):
        try:
            left, right, raw_strokes = _decode_record(rec)
        except ValueError:
            continue
        ch = chr(32 + idx)
        # 原始 Y 向下、基线 9 → Y 向上、基线 0；x 减去左边界使字形从 0 开始
        strokes = [
            [(float(x - left), float(_HERSHEY_BASELINE - y)) for x, y in s]
            for s in raw_strokes
        ]
        glyphs[ch] = Glyph(char=ch, strokes=strokes, advance=float(right - left))

    if not glyphs:
        raise ValueError(f"未能从 {path} 解析出任何字形")

    return FontFamily(
        name=name or path.stem,
        kind=KIND_HERSHEY,
        units_per_em=_HERSHEY_EM,
        glyphs=glyphs,
        source=str(path),
    )


def load_hershey_dir(directory: str | Path) -> dict[str, FontFamily]:
    """加载目录下全部 ``.jhf`` 字体。"""
    directory = Path(directory)
    fonts: dict[str, FontFamily] = {}
    for p in sorted(directory.glob("*.jhf")):
        try:
            f = parse_hershey_jhf(p)
        except Exception:
            continue
        fonts[f.name] = f
    return fonts
