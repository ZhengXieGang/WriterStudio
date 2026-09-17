"""Markdown 渲染：把 Markdown 文本转成写字机笔画（含表格支持）。

覆盖常用块级元素：标题、段落（自动换行）、无序/有序列表、引用、代码块、
水平线，以及**表格**（自动列宽 + 表头分隔线 + 对齐）。

内联层面支持 ``$...$`` 行内公式（走 :mod:`equation`）与 ``**粗体**``/`` `代码` `` 等
标记的剥离（写字机为单线，不加粗，仅取文字）。

坐标系：局部坐标，块左上角为 (0,0)，**Y 轴向上**，内容自上而下排版
（y 递减）。输出笔画可直接作为文档对象的 local_strokes。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..core.geometry import BBox
from ..core.strokes import Stroke
from ..fonts.layout import (
    ALIGN_LEFT,
    TextStyle,
    layout_text,
    measure_text_width,
)
from ..fonts.model import FontFamily
from .equation import render_equation

CELL_PAD_MM = 1.5


@dataclass
class MarkdownStyle:
    """Markdown 排版样式（单位 mm）。"""

    size: float = 4.0                # 正文字号
    line_spacing: float = 1.5
    char_spacing: float = 0.0        # 额外字距(mm)，均匀加在每字步距上
    wrap_width: float = 160.0        # 段落自动换行宽度(mm)
    # 表格整体宽度(mm)：0 = 按内容自适应（各列取最宽单元格）。>0 时列宽
    # 按该总宽收缩、单元格内文字按列宽折行（画布宽度手柄改此值）。
    table_width: float = 0.0
    # 表格随机化（手绘感）：真实手画的表格线不会横平竖直——线端不过角、
    # 出头/不到头，行列间距忽宽忽窄。0 = 完全规整。默认给轻微的线端
    # 随机（网格仍对齐，只是线不再机械地精确交在角上）。
    table_end_jitter: float = 0.5    # 线端随机偏移 σ(mm)：沿轴向平移 +
                                     # 垂直分量（两端各自偏 → 线微倾）
    table_overshoot: float = 0.6     # 线端随机出头上限(mm)，过线的笔感
    table_row_jitter: float = 0.0    # 行界随机偏移 σ(mm)：行高不再一致
    table_col_jitter: float = 0.0    # 列界随机偏移 σ(mm)：列宽不再一致
    table_seed: int = 20260915       # 随机种子：同种子重排（拖宽度）结果
                                     # 不跳变；改种子换一套抖动
    heading_scale: tuple = (1.9, 1.6, 1.4, 1.25, 1.15, 1.1)
    para_space: float = 2.0          # 段后间距(倍行高的比例 → 计算时用)
    list_indent: float = 6.0
    quote_indent: float = 6.0
    table_align_default: str = "left"
    math_size_scale: float = 1.0


@dataclass
class MarkdownResult:
    strokes: list[Stroke] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    blocks: list[str] = field(default_factory=list)
    # 每个表格的外框 (x0, y_top, x1, y_bottom)（渲染局部坐标，Y 向上）。
    # 供画布给 Markdown 对象显示「表格宽度」手柄（改 table_width 重排）。
    tables: list = field(default_factory=list)

    def bbox(self) -> BBox:
        return BBox.from_points(p for s in self.strokes for p in s.points)


# ---------------------------------------------------------------------------
# 文本度量与小工具
# ---------------------------------------------------------------------------
def _measure(text: str, fonts: Sequence[FontFamily], style: TextStyle) -> float:
    """文本步距宽度（mm）。走免笔画的快速度量——换行判定与列宽计算会
    反复测宽（大表格每次重排上万次），构造排版对象再丢弃是主要热点。"""
    if not text:
        return 0.0
    return measure_text_width(text, fonts, style)


def _widest_char_width(text: str, fonts: Sequence[FontFamily],
                       style: TextStyle) -> float:
    """文本中最宽单字符的步距宽（列宽下限：至少放得下一个字）。"""
    best = 0.0
    for ch in text:
        best = max(best, _measure(ch, fonts, style))
    return best


def _fit_widths(natural: list[float], max_total: float,
                mins: list[float], exact: bool = False) -> list[float]:
    """把列宽调整到总宽 ``max_total``。

    * ``exact=False``（自动宽度）：只**收缩**到不超过 ``max_total``——
      表格自然宽小于上限时保持贴合内容（不无谓拉宽）。
    * ``exact=True``（用户拖手柄显式设了表格宽）：缩到或**放大**到恰好
      ``max_total``，列一起变宽/变窄。

    每列不缩到 ``mins`` 以下；若最小宽之和仍超总宽（如列数过多），
    按最小宽返回——宁可整体略超宽，也不让文字互相重叠。
    """
    total = sum(natural)
    if max_total <= 0:
        return list(natural)
    if not exact and total <= max_total:
        return list(natural)
    if abs(total - max_total) < 1e-9:
        return list(natural)
    mins_total = sum(mins)
    if mins_total >= max_total:
        return list(mins)
    if total < max_total:
        # 放大：各列按自然宽等比分配增量
        if total <= 0:
            return [max_total / len(natural)] * len(natural)
        return [n * max_total / total for n in natural]
    avail = max_total - mins_total
    slack = [max(0.0, n - m) for n, m in zip(natural, mins)]
    slack_total = sum(slack)
    if slack_total <= 0:
        return list(mins)
    return [m + s * avail / slack_total for m, s in zip(mins, slack)]


def _append_text(strokes: list[Stroke], text: str, fonts: Sequence[FontFamily],
                 style: TextStyle, x: float, baseline_y: float) -> float:
    """在 (x, baseline_y) 处追加一行文字，返回行宽。"""
    if not text:
        return 0.0
    lay = layout_text(text, fonts, style, origin=(x, baseline_y))
    strokes.extend(lay.strokes())
    return lay.lines[0].width if lay.lines else 0.0


def _append_text_on_line(strokes: list[Stroke], text: str,
                         fonts: Sequence[FontFamily], style: TextStyle,
                         x: float, baseline_y: float,
                         seg: Optional[tuple], seg_nominal_y: float) -> float:
    """追加一行文字，基线贴着行界**线段**的实际位置（返回行宽）。

    ``seg`` 是行界的实际线段（含端点随机的微倾/垂直位移）、
    ``seg_nominal_y`` 是它的名义 y。每个字符按自身水平中点在线段上取
    位移（超出线段范围按端点钳制）、整字刚体平移——文字跟随线的位置
    与倾斜，字符本身不旋转不剪切。``seg`` 为 None 时按直基线排。
    """
    if not text:
        return 0.0
    lay = layout_text(text, fonts, style, origin=(x, baseline_y))
    if seg is None or not lay.lines:
        strokes.extend(lay.strokes())
        return lay.lines[0].width if lay.lines else 0.0
    (ax, ay), (bx, by) = seg
    span = bx - ax
    line0 = lay.lines[0]
    for pl in line0.chars:
        if abs(span) < 1e-9:
            dy = 0.0
        else:
            t = min(max((pl.origin[0] + pl.advance * 0.5 - ax) / span, 0.0),
                    1.0)
            dy = (ay + (by - ay) * t) - seg_nominal_y
        for st in pl.strokes:
            strokes.append(Stroke([(px, py + dy) for px, py in st.points],
                                  st.closed, st.role))
    return line0.width


def _strip_inline(text: str) -> str:
    """去除常见内联标记（保留可见文字）。"""
    import re
    # 图片 → alt；链接 → 文本
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "*_`~":
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_inline_math(text: str) -> list[tuple[str, bool]]:
    """把行内内容拆成 [(片段, 是否公式)]，识别 ``$...$``。"""
    parts: list[tuple[str, bool]] = []
    buf = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "$":
            j = text.find("$", i + 1)
            if j > i + 1:
                if buf:
                    parts.append(("".join(buf), False))
                    buf = []
                parts.append((text[i + 1:j], True))
                i = j + 1
                continue
        buf.append(text[i])
        i += 1
    if buf:
        parts.append(("".join(buf), False))
    return parts


def _wrap(text: str, fonts: Sequence[FontFamily], style: TextStyle,
          max_width: float) -> list[str]:
    """按宽度自动换行；对中英文均按字符/单词边界处理。

    超过整行宽度的 token（长 URL、无空格长串）硬切成多行，保证不超页。
    """
    if max_width <= 0 or not text:
        return [text]
    if _measure(text, fonts, style) <= max_width:
        return [text]

    lines: list[str] = []
    cur = ""
    # 以「西文单词 + 单个 CJK 字符」为断行单元
    import re
    tokens = re.findall(r"[A-Za-z0-9@#$%&_.\-]+|\s+|[^\sA-Za-z0-9]", text)
    for tok in tokens:
        # 单个 token 比整行还宽：硬切到放得下为止
        while _measure((cur + tok).strip(), fonts, style) > max_width \
                and _measure(tok.strip(), fonts, style) > max_width:
            # 先收尾当前行（若有内容），再从 tok 头部切出一段填满新行
            if cur.strip():
                lines.append(cur.rstrip())
                cur = ""
            cut = len(tok)
            while cut > 1 and _measure(tok[:cut].strip(), fonts, style) > max_width:
                cut -= 1
            if cut <= 1:
                break            # 一个字符都放不下（max_width 过小）：放弃硬切
            lines.append(tok[:cut].rstrip())
            tok = tok[cut:].lstrip()
            if not tok:
                break
        if not tok:
            continue
        candidate = cur + tok
        if cur and _measure(candidate.strip(), fonts, style) > max_width:
            lines.append(cur.rstrip())
            cur = tok.lstrip()
        else:
            cur = candidate
    if cur.strip():
        lines.append(cur.rstrip())
    return lines or [text]


# ---------------------------------------------------------------------------
# 主渲染器
# ---------------------------------------------------------------------------
class _Renderer:
    def __init__(self, fonts: Sequence[FontFamily], style: MarkdownStyle) -> None:
        self.fonts = list(fonts)
        self.style = style
        self.strokes: list[Stroke] = []
        self.y = 0.0                 # 当前光标（顶部基线参考，向下递减）
        self.max_x = 0.0
        self.blocks: list[str] = []
        self.tables: list[tuple[float, float, float, float]] = []
        self._table_n = 0            # 表格序号：混入随机种子，各表抖动不同

    # -- 基础 ---------------------------------------------------------------
    def _text_style(self, size: float) -> TextStyle:
        return TextStyle(size=size, line_spacing=self.style.line_spacing,
                         char_spacing=self.style.char_spacing,
                         align=ALIGN_LEFT)

    def _line_height(self, size: float) -> float:
        if not self.fonts:
            return size * self.style.line_spacing
        return self.fonts[0].line_height(size, self.style.line_spacing)

    def _emit_line(self, text: str, size: float, indent: float = 0.0,
                   align: str = ALIGN_LEFT, extra_width: float = 0.0) -> None:
        ls = self._text_style(size)
        baseline = self.y - size  # 首行基线在光标下方一个字号
        _append_text(self.strokes, text, self.fonts, ls, indent, baseline)
        w = _measure(text, self.fonts, ls)
        self.max_x = max(self.max_x, indent + w + extra_width)
        self.y = baseline - (self._line_height(size) - size)

    # -- 各块类型 -----------------------------------------------------------
    def heading(self, text: str, level: int) -> None:
        scale = self.style.heading_scale[min(level - 1, 5)]
        size = self.style.size * scale
        text = _strip_inline(text)
        self._emit_line(text, size)
        self.y -= self.style.size * 0.6  # 标题后留白
        self.blocks.append(f"h{level}")

    def paragraph(self, inline: str, indent: float = 0.0) -> None:
        size = self.style.size
        # 行内公式单独处理：拆段后逐段放置
        segments = _split_inline_math(inline)
        if any(is_math for _, is_math in segments):
            self._paragraph_with_math(segments, size, indent)
        else:
            text = _strip_inline(inline)
            for line in _wrap(text, self.fonts, self._text_style(size),
                              self.style.wrap_width - indent):
                self._emit_line(line, size, indent)
        self.y -= size * 0.8
        self.blocks.append("p")

    def _paragraph_with_math(self, segments, size: float, indent: float) -> None:
        # 简化处理：把公式单独成「行内单元」，与文字拼接时以文字基线对齐
        x = indent
        baseline = self.y - size
        ls = self._text_style(size)
        for seg, is_math in segments:
            if is_math:
                try:
                    eq = render_equation(seg, size_mm=size * self.style.math_size_scale)
                except Exception:
                    eq = None      # 坏公式（如 $\\frac{1}{2$）不拖垮整段
                if not eq:
                    # 退化为原样输出公式文本，让用户能看到哪里写错了
                    text = _strip_inline(f"${seg}$")
                    if text:
                        w = _append_text(self.strokes, text, self.fonts, ls, x, baseline)
                        x += w
                    continue
                # 公式基线对齐到当前文字基线
                for s in eq:
                    pts = [(px + x, py + baseline) for px, py in s.points]
                    self.strokes.append(Stroke(pts, s.closed))
                box = BBox.from_points(p for s in eq for p in s.points)
                x += box.width + size * 0.2
            else:
                text = _strip_inline(seg)
                if text:
                    w = _append_text(self.strokes, text, self.fonts, ls, x, baseline)
                    x += w
        self.max_x = max(self.max_x, x)
        self.y = baseline - (self._line_height(size) - size)

    def bullet_list(self, items: list[str], ordered: bool = False,
                    start: int = 1) -> None:
        size = self.style.size
        ind = self.style.list_indent
        ls = self._text_style(size)
        for i, item in enumerate(items):
            marker = f"{start + i}." if ordered else "•"
            baseline = self.y - size
            marker_w = _measure(marker, self.fonts, ls)
            _append_text(self.strokes, marker, self.fonts, ls, 0.0, baseline)
            self._append_wrapped(item, ls, size, ind, baseline)
            self.max_x = max(self.max_x, ind
                             + _measure(item, self.fonts, ls) + marker_w * 0)
            self.y = baseline - (self._line_height(size) - size)
        self.y -= size * 0.4
        self.blocks.append("ul" if not ordered else "ol")

    def _append_wrapped(self, text: str, ls: TextStyle, size: float,
                        indent: float, baseline: float) -> None:
        """在给定基线处按宽度换行追加文本（续行与 indent 对齐）。"""
        avail = self.style.wrap_width - indent
        lines = _wrap(_strip_inline(text), self.fonts, ls, avail)
        y = baseline
        for k, line in enumerate(lines):
            _append_text(self.strokes, line, self.fonts, ls, indent, y)
            self.max_x = max(self.max_x, indent
                             + _measure(line, self.fonts, ls))
            if k < len(lines) - 1:
                y -= self._line_height(size)

    def quote(self, text: str) -> None:
        size = self.style.size
        ind = self.style.quote_indent
        # 左侧竖线
        top = self.y
        for line in _wrap(_strip_inline(text), self.fonts, self._text_style(size),
                          self.style.wrap_width - ind):
            self._emit_line(line, size, ind)
        bottom = self.y
        x = ind * 0.4
        self.strokes.append(Stroke([(x, top - size * 0.4), (x, bottom + size * 0.4)]))
        self.blocks.append("quote")

    def code_block(self, code: str) -> None:
        size = self.style.size * 0.92
        for line in code.split("\n"):
            self._emit_line(line, size)
        self.y -= size * 0.6
        self.blocks.append("code")

    def hr(self) -> None:
        size = self.style.size
        y = self.y - size * 0.5
        self.strokes.append(Stroke([(0.0, y), (self.style.wrap_width, y)]))
        self.y = y - size * 0.5
        self.blocks.append("hr")

    def table(self, header: list[str], rows: list[list[str]],
              aligns: list[str]) -> None:
        size = self.style.size
        # 列数取表头/各行最大值：短行补空、长行扩列，内容不静默丢弃
        ncol = max([len(header)] + [len(r) for r in rows])
        header = list(header) + [""] * (ncol - len(header))
        all_rows = [header] + [list(r) + [""] * (ncol - len(r)) for r in rows]
        ls = self._text_style(size)
        # 表格随机化的随机源：种子混入表格序号（同文档各表抖动不同），
        # 同一样式重排（拖宽度）结果不跳变
        rng = random.Random((int(self.style.table_seed) * 1000003
                             + self._table_n * 7919) & 0xFFFFFFFF)
        self._table_n += 1
        # 1) 自然列宽（按最宽单元格 + 两侧内边距）与最小列宽（放得下一个字）
        natural: list[float] = []
        mins: list[float] = []
        for c in range(ncol):
            w = 0.0
            widest_char = 0.0
            for r in all_rows:
                if c < len(r):
                    txt = _strip_inline(r[c])
                    w = max(w, _measure(txt, self.fonts, ls))
                    widest_char = max(widest_char,
                                      _widest_char_width(txt, self.fonts, ls))
            natural.append(w + 2 * CELL_PAD_MM)
            mins.append(max(widest_char, size * 0.6) + 2 * CELL_PAD_MM)
        # 2) 宽度限制：显式表格宽度（拖手柄）精确贴合；否则不超过换行宽度
        explicit = self.style.table_width > 0
        max_total = self.style.table_width if explicit else self.style.wrap_width
        col_widths = _fit_widths(natural, max_total, mins, exact=explicit)
        total_w = sum(col_widths)
        col_x: list[float] = []
        acc = 0.0
        for w in col_widths:
            col_x.append(acc)
            acc += w
        # 2.5) 列界随机化：内部列界共享抖动（该界两侧列一起动），抖完再
        # 派生列宽——单元格折行走同一套宽度，文字绝不会越过抖动后的列线。
        # 外框（左界 0 / 右界 total_w）不动，表格占位与宽度手柄保持稳定。
        cj = max(0.0, self.style.table_col_jitter)
        if cj > 0 and ncol > 1:
            for i in range(1, ncol):
                col_x[i] += rng.gauss(0.0, cj)
                # 与左邻列至少留半个最小列宽，防止列被抖没
                if col_x[i] < col_x[i - 1] + mins[i - 1] * 0.5:
                    col_x[i] = col_x[i - 1] + mins[i - 1] * 0.5
            if total_w - col_x[-1] < mins[-1] * 0.5:
                col_x[-1] = total_w - mins[-1] * 0.5
            col_widths = [(col_x[i + 1] if i + 1 < ncol else total_w)
                          - col_x[i] for i in range(ncol)]

        lh = self._line_height(size)
        # 3) 每格按列内宽折行 → 行高取该行最高单元格
        cell_lines: list[list[list[str]]] = []
        row_heights: list[float] = []
        for r in all_rows:
            lines_row: list[list[str]] = []
            nlines = 1
            for c in range(ncol):
                inner = max(1.0, col_widths[c] - 2 * CELL_PAD_MM)
                txt = _strip_inline(r[c]) if c < len(r) else ""
                ls_cell = _wrap(txt, self.fonts, ls, inner)
                lines_row.append(ls_cell)
                nlines = max(nlines, len(ls_cell))
            cell_lines.append(lines_row)
            row_heights.append(nlines * lh)

        # 4) 表格线（可变行高 + 随机化）
        # 线端随机化：两端沿轴向各自平移（高斯 σ=线端偏移），并按上限
        # 随机「出头」（越过相交线一点的手绘笔感）；垂直分量两端各自
        # 偏移使线条微倾。全为 0 时原样返回（完全规整网格）。
        ej = max(0.0, self.style.table_end_jitter)
        osv = max(0.0, self.style.table_overshoot)

        def _jline(a, b):
            if ej <= 0 and osv <= 0:
                return [a, b]
            horizontal = abs(a[1] - b[1]) < 1e-9
            lo, hi = a, b
            if horizontal and a[0] > b[0]:
                lo, hi = b, a
            elif not horizontal and a[1] < b[1]:
                lo, hi = b, a
            pts = []
            for k, (px, py) in enumerate((lo, hi)):
                out = rng.uniform(0.0, osv) if osv > 0 else 0.0
                out = -out if k == 0 else out     # 两端都向外出头
                along = rng.gauss(0.0, ej) if ej > 0 else 0.0
                perp = rng.gauss(0.0, ej) if ej > 0 else 0.0
                if horizontal:
                    pts.append((px + along + out, py + perp))
                else:
                    pts.append((px + perp, py + along + out))
            return pts

        top = self.y
        ys = [top]
        for h in row_heights:
            ys.append(ys[-1] - h)
        bottom = ys[-1]
        # 行界随机化：内部行界共享抖动，行高不再一致；顶界/底界不动
        # （表格占位与外框元数据稳定），并保证每行至少留得下一个字。
        rj = max(0.0, self.style.table_row_jitter)
        if rj > 0 and len(ys) > 2:
            min_h = size * 0.8
            for i in range(1, len(ys) - 1):
                ys[i] -= rng.gauss(0.0, rj)
                if ys[i] > ys[i - 1] - min_h:
                    ys[i] = ys[i - 1] - min_h
            if ys[-2] < ys[-1] + min_h:
                ys[-2] = ys[-1] + min_h
            row_heights = [ys[r] - ys[r + 1] for r in range(len(all_rows))]
        # 记录每条行界的实际线段（含端点随机的微倾/位移）：单元格文字
        # 随后逐字符贴着所属行的底界线段走。RNG 消耗顺序与随机化参数
        # 完全一致——线本身的随机效果不受文字跟随影响。
        hsegs: dict[int, tuple] = {}
        for i, yy in enumerate(ys):
            seg = _jline((0.0, yy), (total_w, yy))
            hsegs[i] = (seg[0], seg[1])
            self.strokes.append(Stroke([seg[0], seg[1]]))
        # 表头下加粗线（用双线示意）——两条独立随机，天然错开
        echo = _jline((0.0, ys[1]), (total_w, ys[1]))
        self.strokes.append(Stroke([echo[0], echo[1]]))
        for x in col_x:
            self.strokes.append(Stroke(_jline((x, top), (x, bottom))))
        self.strokes.append(Stroke(_jline((total_w, top), (total_w, bottom))))

        # 5) 单元格文字（垂直居中于所属行，按对齐方式水平定位）；基线
        # 逐字符贴着所属行**底界线段**的实际位置走——线端随机的微倾、
        # 行界抖动都会原样带动文字，字符刚体平移、不旋转不剪切
        for r, lines_row in enumerate(cell_lines):
            row_top = ys[r]
            row_h = row_heights[r]
            seg_r = hsegs.get(r + 1)
            seg_y0 = ys[r + 1]
            for c, cell in enumerate(lines_row):
                if c >= len(col_x):
                    continue
                align = (aligns[c] if c < len(aligns)
                         else self.style.table_align_default)
                cw = col_widths[c]
                block_h = len(cell) * lh
                # 首行基线：整块在行内垂直居中
                base_y = row_top - (row_h - block_h) / 2.0 - size
                for k, line in enumerate(cell):
                    tw = _measure(line, self.fonts, ls)
                    if align == "right":
                        tx = col_x[c] + cw - CELL_PAD_MM - tw
                    elif align == "center":
                        tx = col_x[c] + (cw - tw) / 2.0
                    else:
                        tx = col_x[c] + CELL_PAD_MM
                    _append_text_on_line(self.strokes, line, self.fonts, ls,
                                         tx, base_y - k * lh,
                                         seg_r, seg_y0)
        self.y = bottom - size * 0.6
        self.max_x = max(self.max_x, total_w)
        self.blocks.append("table")
        self.tables.append((0.0, top, total_w, bottom))


# ---------------------------------------------------------------------------
# 从 markdown-it tokens 提取块
# ---------------------------------------------------------------------------
def _parse_table_aligns(tokens, i: int):
    return None


def render_markdown(source: str, fonts: Sequence[FontFamily],
                    style: Optional[MarkdownStyle] = None) -> MarkdownResult:
    """把 Markdown 文本渲染为笔画。"""
    from markdown_it import MarkdownIt

    style = style or MarkdownStyle()
    r = _Renderer(fonts, style)

    md = MarkdownIt("commonmark")
    try:
        md = md.enable("table")
    except Exception:
        pass
    tokens = md.parse(source)

    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t.type == "heading_open":
            level = int(t.tag[1])
            inline = tokens[i + 1].content if i + 1 < n else ""
            r.heading(inline, level)
            i += 3
            continue
        if t.type == "paragraph_open":
            inline = tokens[i + 1].content if i + 1 < n else ""
            r.paragraph(inline)
            i += 3
            continue
        if t.type == "fence" or t.type == "code_block":
            r.code_block(t.content)
            i += 1
            continue
        if t.type == "hr":
            r.hr()
            i += 1
            continue
        if t.type == "blockquote_open":
            # 收集到 blockquote_close 之间的段落文本
            j = i + 1
            texts = []
            while j < n and tokens[j].type != "blockquote_close":
                if tokens[j].type == "inline":
                    texts.append(tokens[j].content)
                j += 1
            if texts:
                r.quote(" ".join(texts))
            i = j + 1
            continue
        if t.type in ("bullet_list_open", "ordered_list_open"):
            ordered = t.type.startswith("ordered")
            start = 1
            if ordered and t.attrs.get("start"):
                try:
                    start = int(t.attrs["start"])
                except Exception:
                    start = 1
            items = []
            j = i + 1
            while j < n and tokens[j].type not in ("bullet_list_close", "ordered_list_close"):
                if tokens[j].type == "inline":
                    items.append(tokens[j].content)
                j += 1
            r.bullet_list(items, ordered=ordered, start=start)
            i = j + 1
            continue
        if t.type == "table_open":
            i = _render_table_tokens(tokens, i, r)
            continue
        i += 1

    box = r.bbox_safe()
    return MarkdownResult(
        strokes=r.strokes,
        width=box.width,
        height=box.height,
        blocks=r.blocks,
        tables=list(r.tables),
    )


def _render_table_tokens(tokens, i: int, r: _Renderer) -> int:
    """解析 table tokens（thead/tbody/tr/th/td）。返回下一个索引。"""
    n = len(tokens)
    header: list[str] = []
    rows: list[list[str]] = []
    aligns: list[str] = []
    cur_row: list[str] = []
    in_head = False
    j = i + 1
    while j < n and tokens[j].type != "table_close":
        t = tokens[j]
        if t.type == "thead_open":
            in_head = True
        elif t.type == "thead_close":
            in_head = False
        elif t.type == "tr_open":
            cur_row = []
        elif t.type == "tr_close":
            if in_head:
                header = cur_row
            else:
                rows.append(cur_row)
        elif t.type in ("th_open", "td_open"):
            # 紧随其后的 inline 是内容；对齐信息可能在 style
            style_str = t.attrGet("style") or ""
            if in_head:
                a = "left"
                if "center" in style_str:
                    a = "center"
                elif "right" in style_str:
                    a = "right"
                aligns.append(a)
            if j + 1 < n and tokens[j + 1].type == "inline":
                cur_row.append(tokens[j + 1].content)
        elif t.type in ("th_close", "td_close"):
            pass
        j += 1
    if header:
        if not aligns:
            aligns = ["left"] * len(header)
        r.table(header, rows, aligns)
    return j + 1


# 给 _Renderer 加一个安全的 bbox
def _renderer_bbox(self) -> BBox:
    return BBox.from_points(p for s in self.strokes for p in s.points)


_Renderer.bbox_safe = _renderer_bbox
