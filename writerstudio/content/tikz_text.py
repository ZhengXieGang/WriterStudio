"""TikZ 文字替换：用本软件的 gcode/单线字体重排 TikZ 里的节点文字。

为什么需要它：TikZ（以及 LaTeX）里的文字最终是**轮廓字形**，而写字机要的是
可书写折线、且希望用用户自己的手写字体。做法是把 TeX 的排版结果当作**版式
参考**：

    1. TeX 正常编译（文字用系统字体测量），得到图形与文字位置；
    2. ``pdftotext -bbox`` 读出每个词的外框（pt，与图形同一页坐标）；
    3. 图形按原有 SVG 管线转笔画；文字**丢掉**，改用本软件的字体链
       （``fonts/layout``）在对应位置重新排版成笔画。

这样节点文字既有 TeX 保证的位置/对齐，又落在用户的手写字迹上。

坐标系：pdftocairo 的 SVG 用户单位是 96dpi 像素（svgelements 也按此），而
``pdftotext -bbox`` 输出 pt（1/72 英寸），换算系数 96/72 = 4/3。文字的归一化
复用 :func:`~writerstudio.content.svg_import.normalize_point`，与图形同一套
``(包围盒, 缩放)``，因此两者严格对齐。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from ..core.strokes import Stroke
from ..fonts.builder import TextSpec, resolve_fonts
from ..fonts.layout import layout_text
from ..fonts.manager import FontManager
from .svg_import import BBox, normalize_point

#: pt（pdftotext）→ SVG 用户单位（96dpi 像素）
_PT_TO_PX = 96.0 / 72.0

_WORD_RE = re.compile(
    r'<word xMin="([\d.eE+-]+)" yMin="([\d.eE+-]+)" '
    r'xMax="([\d.eE+-]+)" yMax="([\d.eE+-]+)">(.*?)</word>', re.S)


@dataclass
class TextWord:
    """一个词（或一段无空格文本）及其在 PDF 页面上的外框（pt，Y 向下）。"""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


def _unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'")
             .replace("&amp;", "&"))


def extract_words(pdf_path: str | Path) -> list[TextWord]:
    """用 ``pdftotext -bbox`` 读 PDF 里的词与外框；失败返回空表。

    ``pdftotext``（poppler）是 TeX→PDF→SVG 管线的既有依赖，通常已随
    ``pdftocairo`` 一同安装；缺失时只是放弃文字替换，不影响图形。
    """
    import shutil
    exe = shutil.which("pdftotext")
    if not exe:
        return []
    try:
        proc = subprocess.run([exe, "-bbox", str(pdf_path), "-"],
                              capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    out: list[TextWord] = []
    for m in _WORD_RE.finditer(proc.stdout or ""):
        try:
            x0, y0, x1, y1 = (float(m.group(i)) for i in range(1, 5))
        except ValueError:
            continue
        text = _unescape((m.group(5) or "").strip())
        if text:
            out.append(TextWord(text, x0, y0, x1, y1))
    return out


def _layout_word(text: str, fonts, size_mm: float
                 ) -> tuple[list[Stroke], BBox, list[str]]:
    """用字体链排版一个词，返回 (笔画, 墨迹包围盒, 缺字)。

    空字体或空排版返回空笔画。缺字来自排版结果的 ``missing``。
    """
    if not fonts or not text:
        return [], BBox(), []
    spec = TextSpec(text=text, size=size_mm)
    lay = layout_text(text, fonts, spec.to_style(), origin=(0.0, 0.0))
    strokes = lay.strokes()
    box = BBox.from_points(p for s in strokes for p in s.points)
    return strokes, box, list(getattr(lay, "missing", []) or [])


def place_words(words: Sequence[TextWord], box: BBox, scale: float,
                manager: FontManager,
                font_names: Optional[Sequence[str]] = None,
                size_scale: float = 1.0,
                missing_out: Optional[set] = None) -> list[Stroke]:
    """把词用本软件字体重排到 TikZ 给的位置上，返回 mm 坐标笔画。

    ``box``/``scale`` 为图形的归一化参数（同一套，保证与图形对齐）。
    每个词：字号取 TeX 词框高度换算的 mm（``size_scale`` 可整体微调）；
    再等比缩放使墨迹宽度贴合词框宽度，并把墨迹中心对准词框中心。
    TeX 的词框本来就贴着文字，因此居中即等价于按 TeX 的排版位置摆放。
    字体链画不出的字（``layout.missing``）会被跳过，不产生笔画；
    ``missing_out`` 给定时把缺字收集进去（供界面提示）。
    """
    if not words:
        return []
    fonts = resolve_fonts(TextSpec(font_names=list(font_names or [])), manager)
    if not fonts:
        return []

    out: list[Stroke] = []
    for w in words:
        # 词框四角 → mm（与图形同一套归一化）
        mx0, my0 = normalize_point(w.x0 * _PT_TO_PX, w.y0 * _PT_TO_PX, box, scale)
        mx1, my1 = normalize_point(w.x1 * _PT_TO_PX, w.y1 * _PT_TO_PX, box, scale)
        w_mm = abs(mx1 - mx0)
        h_mm = abs(my1 - my0)
        if w_mm <= 1e-6 or h_mm <= 1e-6:
            continue
        size = h_mm * size_scale
        strokes, ink, missing = _layout_word(w.text, fonts, size)
        if missing and missing_out is not None:
            missing_out.update(missing)
        if not strokes or ink.is_empty:
            continue
        # 等比缩放到词框宽度；词框过窄（单字）时退回按高度匹配
        target_w = w_mm
        k = (target_w / ink.width) if ink.width > 1e-6 else 1.0
        if not (0.05 <= k <= 20.0):
            k = 1.0
        # 缩放绕墨迹右下角无关，直接先缩放再平移对齐：
        #   水平：墨迹左缘 → 词框左缘
        #   垂直：墨迹中线 → 词框中线
        cx = (mx0 + mx1) / 2.0
        cy = (my0 + my1) / 2.0
        ink_cx = (ink.x0 + ink.x1) / 2.0
        ink_cy = (ink.y0 + ink.y1) / 2.0
        tx = cx - ink_cx * k
        ty = cy - ink_cy * k
        for s in strokes:
            pts = [(x * k + tx, y * k + ty) for x, y in s.points]
            # 保留 role="glyph"：否则这些笔画会被扰动引擎当成图形线条，
            # 「线条起伏/弯折」会把节点文字扭歪（文字形态应由字体决定）。
            out.append(Stroke(pts, s.closed, s.role, s.group))
    return out
