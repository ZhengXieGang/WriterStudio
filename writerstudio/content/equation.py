"""LaTeX 公式渲染（matplotlib mathtext）。

用 matplotlib 的 ``TextPath`` 把 ``$...$`` 形式的数学公式转成矢量路径，
再离散为笔画。**无需安装 TeX 环境**，覆盖绝大多数笔记/教案公式（分式、根式、
求和积分、上下标、希腊字母等）。

``TextPath`` 输出单位为 point(1/72 inch)、Y 轴向上、基线 y=0，曲线已预离散。

**字体链替换**（与 TikZ 文字替换同思路）：给出本软件的字体链时，mathtext
只负责排版（每个字符的位置/字号由它保证），字母、数字等链上画得出的字符
改用字体链重排成笔画（如用户的手写体）；链画不出的数学符号（∑ π ∫ ≤ 等）
改用**内置单线 Hershey 字体**（mathupp/greek 等，键位经逐字形渲染核对）
画成单线笔画，个别简单符号（× ≤ ≥）按几何直接画线。mathtext 轮廓
（填充字体的外框线，写出来是空心字）只作为未知符号的最后兜底，
正常不会出现在纸面上。
"""

from __future__ import annotations

import threading
from typing import Any, Optional, Sequence

from ..core.geometry import BBox
from ..core.strokes import Stroke

PT_TO_MM = 25.4 / 72.0

# ---------------------------------------------------------------------------
# 数学符号的单线兜底（键位经逐字形渲染核对，见 tests）：
#   * mathupp/mathlow 是 Hershey 数学符号字体，字形挂在 ASCII 键位上；
#   * greek 按希腊字母的拉丁转写挂键（P=Π pi, S=Σ sigma, Q=Θ theta,
#     F=Φ phi, C=Χ chi, W=Ω omega, 小写同规则 p=π s=σ w=ω q=θ …）。
# mathtext 的字符码是 Unicode（\sum=U+2211，希腊字母=U+03xx），因此
# 需要这张「Unicode → (内置单线字体, 键位)」对照表。链上和这里都查不到
# 的字符才退回 mathtext 轮廓（空心）。
# ---------------------------------------------------------------------------
_SYMBOL_FALLBACK: dict[str, tuple[str, str]] = {
    "∑": ("mathupp", ";"), "∏": ("mathupp", ":"),
    "√": ("mathupp", "b"), "∞": ("mathupp", "^"),
    "°": ("mathupp", "`"), "≠": ("mathupp", "?"),
    "≡": ("mathupp", "@"), "∈": ("mathupp", "h"),
    "→": ("mathupp", "i"), "←": ("mathupp", "j"),
    "↓": ("mathupp", "k"), "∂": ("mathupp", "m"),
    "∇": ("mathupp", "n"), "∫": ("mathupp", "p"),
    "∃": ("mathupp", "v"), "÷": ("mathupp", "x"),
    "∥": ("mathupp", "y"), "⊥": ("mathupp", "z"),
    "∠": ("mathupp", "{"), "±": ("mathupp", " "),
    "·": ("mathupp", "$"), "⋅": ("mathupp", "$"),
}
_GREEK_TRANSLIT = {
    "Α": "A", "Β": "B", "Γ": "G", "Δ": "D", "Ε": "E", "Ζ": "Z", "Η": "H",
    "Θ": "Q", "Ι": "I", "Κ": "K", "Λ": "L", "Μ": "M", "Ν": "N", "Ξ": "X",
    "Ο": "O", "Π": "P", "Ρ": "R", "Σ": "S", "Τ": "T", "Υ": "U", "Φ": "F",
    "Χ": "C", "Ψ": "Y", "Ω": "W",
}
for _g, _k in _GREEK_TRANSLIT.items():
    _SYMBOL_FALLBACK.setdefault(_g, ("greek", _k))
    _SYMBOL_FALLBACK[_g.lower()] = ("greek", _k.lower())
# 希腊字母 β/θ 等在 greek 字体里有专键；变体（ς 等）未收录则走轮廓兜底

#: 语法太简单、单线字体里没有对应键的符号：按几何直接画线（em 为字号）
_PROCEDURAL_SYMBOLS = {"×", "≤", "≥"}


def _draw_procedural_symbol(ch: str, ox: float, oy: float, em: float
                            ) -> list[Stroke]:
    """在字形原点（pt→mm 后的基线左端）按几何画 × ≤ ≥，粗细交给笔尖。"""
    x0, y0 = ox, oy + em * 0.15
    w, h = em * 0.55, em * 0.45
    cx, cy = x0 + w / 2.0, y0 + h / 2.0
    if ch == "×":
        return [Stroke([(x0, y0), (x0 + w, y0 + h)], closed=False),
                Stroke([(x0, y0 + h), (x0 + w, y0)], closed=False)]
    if ch in ("≤", "≥"):
        s = -1.0 if ch == "≥" else 1.0
        tipx = cx - s * w / 2.0
        backx = cx + s * w / 2.0
        return [Stroke([(backx, y0 + h), (tipx, cy), (backx, y0)],
                       closed=False),
                Stroke([(x0, y0), (x0 + w, y0)], closed=False)]
    return []


_matplotlib_lock = threading.Lock()
_textpath = None
_mathtext_parser: Any = None
_load_flags: Any = None


def _get_textpath():
    """延迟导入 matplotlib（启动开销较大），并强制 Agg 后端。"""
    global _textpath
    if _textpath is None:
        with _matplotlib_lock:
            if _textpath is None:
                import matplotlib
                matplotlib.use("Agg", force=True)
                from matplotlib.textpath import TextPath
                _textpath = TextPath
    return _textpath


def _get_mathtext_parser():
    """MathTextParser("path")：能给出每个字形（字符、位置、字号）的解析器。"""
    global _mathtext_parser, _load_flags
    if _mathtext_parser is None:
        with _matplotlib_lock:
            if _mathtext_parser is None:
                import matplotlib
                matplotlib.use("Agg", force=True)
                from matplotlib.ft2font import LoadFlags
                from matplotlib.mathtext import MathTextParser
                _mathtext_parser = MathTextParser("path")
                _load_flags = LoadFlags
    return _mathtext_parser


def mathtext_available() -> bool:
    try:
        _get_textpath()
        return True
    except Exception:
        return False


def wrap_math(s: str) -> str:
    """确保公式被 ``$...$`` 包裹（TextPath 需要 $ 才走 mathtext）。"""
    s = s.strip()
    if not s:
        return s
    if s.startswith("$$") and s.endswith("$$") and len(s) > 4:
        return f"${s[2:-2]}$"
    if s.startswith("$") and s.endswith("$"):
        return s
    return f"${s}$"


def render_equation(latex: str, size_mm: float = 5.0,
                    tolerance: float = 0.05,
                    font_names: Optional[Sequence[str]] = None,
                    manager=None,
                    text_scale: float = 1.0) -> list[Stroke]:
    """把 LaTeX 公式渲染为笔画（单位 mm，Y 向上，基线 y=0，左端 x=0）。

    ``size_mm`` 是公式主体字号(mm)。``tolerance`` 为曲线离散容差(mm)。

    ``font_names`` + ``manager`` 给定时（本软件字体链），链上画得出的字符
    改用字体链重排（``text_scale`` 可整体微调字号）；链画不出的字符与
    分式线等结构保留 mathtext 轮廓。
    """
    if font_names and manager is not None:
        fonts = []
        for n in font_names:
            f = manager.get(n)
            if f is not None:
                fonts.append(f)
        if fonts:
            try:
                return _render_with_fonts(latex, size_mm, fonts, text_scale,
                                          manager)
            except Exception:
                pass        # 解析失败退回原生轮廓，公式不丢
    return _render_native(latex, size_mm, tolerance)


def _collapse_flat_rect(pts: list) -> Optional[list]:
    """把「扁平矩形」轮廓折叠成中线；非扁平矩形返回 None。

    mathtext 把分式线/根号顶线输出为**有厚度的细矩形**（实测 \frac 的
    分数线约 2.2×0.31mm），描轮廓会画出上下两道线加两个竖边——0.31mm
    厚的矩形在 0.5mm 笔尖下墨水叠成一团，观感是「方框」而非一条线。
    判定：≥4 个点、轴对齐（全部点贴在两条长边上）、短边 ≤ 长边的 1/4
    → 取中线发单笔。不设点数上限——不同数学字体的减号轮廓可能是
    5 点矩形，也可能是带倒角的多点轮廓，轴对齐检查已足以排除真实字形。
    """
    if len(pts) < 4:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    long_, short = max(w, h), min(w, h)
    if long_ <= 1e-9 or short > 0.25 * long_:
        return None
    tol = 0.2 * short + 1e-9
    if w >= h:
        # 横向扁平：每个点都必须贴在上下两条长边上（排除斜线/楔形）
        if any(min(abs(y - min(ys)), abs(y - max(ys))) > tol for _, y in pts):
            return None
        ym = (min(ys) + max(ys)) / 2.0
        return [(min(xs), ym), (max(xs), ym)]
    if any(min(abs(x - min(xs)), abs(x - max(xs))) > tol for x, _ in pts):
        return None
    xm = (min(xs) + max(xs)) / 2.0
    return [(xm, min(ys)), (xm, max(ys))]


def _render_native(latex: str, size_mm: float, tolerance: float) -> list[Stroke]:
    """原生路径：整条公式用 mathtext 轮廓（与历史行为一致）。"""
    TextPath = _get_textpath()
    text = wrap_math(latex)
    if not text:
        return []

    # TextPath 的 size 单位是 point；先把点转 mm 的系数算进去
    # 期望主体字号为 size_mm(mm) → 用 size_mm/PT_TO_MM 作为 point 尺寸
    pt_size = size_mm / PT_TO_MM
    tp = TextPath((0.0, 0.0), text, size=pt_size, usetex=False, prop=None)

    # 提取多边形（to_polygons 会按容差离散曲线），再转到 mm
    # 注意：to_polygons 的顶点单位与 TextPath 相同(point)
    polys = tp.to_polygons(closed_only=False)
    scale = PT_TO_MM
    strokes: list[Stroke] = []
    for poly in polys:
        if len(poly) < 2:
            continue
        pts = [(float(p[0]) * scale, float(p[1]) * scale) for p in poly]
        flat = _collapse_flat_rect(pts)
        if flat is not None:
            strokes.append(Stroke(flat, closed=False))
        else:
            strokes.append(Stroke(pts, closed=False))

    # 归位：把整体左移到 x=0（TextPath 起笔可能带左侧留白）
    box = BBox.from_points(p for s in strokes for p in s.points)
    if not box.is_empty and abs(box.x0) > 1e-9:
        dx = -box.x0
        for s in strokes:
            s.points = [(x + dx, y) for x, y in s.points]

    return strokes


def _render_with_fonts(latex: str, size_mm: float, fonts, text_scale: float,
                       manager=None) -> list[Stroke]:
    """字体链替换路径：mathtext 负责排版，字符用本软件字体重排。

    * 链上画得出的字符 → 字体链笔画（``role="glyph"``）；
    * 链画不出的数学符号 → 内置单线字体（Hershey mathupp/greek，见
      ``_SYMBOL_FALLBACK``）或几何画线（× ≤ ≥）——都不产生空心轮廓；
    * 分式线/根号顶线等结构矩形 → 直线笔画。
    """
    from matplotlib.font_manager import FontProperties
    from matplotlib.path import Path as MplPath

    parser = _get_mathtext_parser()
    LoadFlags = _load_flags
    text = wrap_math(latex)
    if not text:
        return []

    # 单线兜底字体（懒加载，首次解析后由 FontManager 缓存）
    symbol_fonts: dict[str, Any] = {}
    if manager is not None:
        for n in {f for f, _ in _SYMBOL_FALLBACK.values()}:
            fam = manager.get(n)
            if fam is not None:
                symbol_fonts[n] = fam

    pt_size = size_mm / PT_TO_MM
    _w, _h, _d, glyphs, rects = parser.parse(
        text, dpi=72, prop=FontProperties(size=pt_size))

    scale = PT_TO_MM
    strokes: list[Stroke] = []
    for font, fontsize, ccode, glyph_index, ox, oy in glyphs:
        ch = chr(int(ccode))
        # 链上没有 U+2212（数学减号）但有 ASCII '-' 时按 '-' 走字体链：
        # mathtext 把 '-' 一律画成数学减号，手写字体链通常只有后者；
        # 减号本就该是用户笔迹里的一横，而不是数学字体的细矩形
        if ch == "\u2212" and not any(f.has(ch) for f in fonts) \
                and any(f.has("-") for f in fonts):
            ch = "-"
        if not ch.isspace() and any(f.has(ch) for f in fonts):
            # 字体链重排：字号随 mathtext 的字形字号（上下标自动缩小），
            # 原点 = 字形基线左端
            gsize = fontsize * scale * max(0.1, min(5.0, text_scale))
            from ..fonts.layout import TextStyle, layout_text
            lay = layout_text(ch, fonts, TextStyle(size=gsize),
                              origin=(ox * scale, oy * scale))
            strokes.extend(lay.strokes())
            continue
        # 单线兜底：内置 Hershey 数学/希腊字体（绝不出空心轮廓）
        fb = _SYMBOL_FALLBACK.get(ch)
        if fb is not None:
            fam = symbol_fonts.get(fb[0])
            if fam is not None and fam.has(fb[1]):
                gsize = fontsize * scale * max(0.1, min(5.0, text_scale))
                from ..fonts.layout import TextStyle, layout_text
                lay = layout_text(fb[1], [fam], TextStyle(size=gsize),
                                  origin=(ox * scale, oy * scale))
                if lay.strokes():
                    strokes.extend(lay.strokes())
                    continue
        if ch in _PROCEDURAL_SYMBOLS:
            strokes.extend(_draw_procedural_symbol(
                ch, ox * scale, oy * scale, fontsize * scale))
            continue
        # 保留 mathtext 轮廓：按字形索引取出路径，散化为折线。
        # 数学字体的减号/正负号横杠等是**扁平矩形**（约 2.2×0.29mm），
        # 描轮廓在笔尖下会叠成一个方框——折叠成中线单笔
        font.clear()
        font.set_size(fontsize, 72)
        font.load_glyph(glyph_index, flags=LoadFlags.NO_HINTING)
        verts, codes = font.get_path()
        for poly in MplPath(verts, codes).to_polygons(closed_only=False):
            if len(poly) < 2:
                continue
            pts = [(float(p[0]) * scale + ox * scale,
                    float(p[1]) * scale + oy * scale) for p in poly]
            flat = _collapse_flat_rect(pts)
            strokes.append(Stroke(flat if flat is not None else pts,
                                  closed=False))

    # 分式线/根号顶线等结构矩形：mathtext 给的是「有厚度的规则线」
    # （实测 \frac 分数线 2.2×0.31mm），描轮廓会画出上下两道线加竖边，
    # 笔尖下墨水叠成一个「方框」。取中线发**一条**线——粗细由笔尖
    # 物理宽度体现，与手写一致。
    for ox, oy, rw, rh in rects:
        w, h = rw * scale, rh * scale
        x, y = ox * scale, oy * scale
        if w >= h:      # 横规则线（分数线/根号顶线/上划线）
            strokes.append(Stroke([(x, y + h / 2.0),
                                   (x + w, y + h / 2.0)], closed=False))
        else:           # 竖规则线（mathtext 实际不产出，防御性保留）
            strokes.append(Stroke([(x + w / 2.0, y),
                                   (x + w / 2.0, y + h)], closed=False))

    # 归位：整体左移到 x=0（与原生路径一致）
    box = BBox.from_points(p for s in strokes for p in s.points)
    if not box.is_empty and abs(box.x0) > 1e-9:
        dx = -box.x0
        for s in strokes:
            s.points = [(px + dx, py) for px, py in s.points]
    return strokes


def equation_bbox(latex: str, size_mm: float = 5.0) -> BBox:
    return BBox.from_points(
        p for s in render_equation(latex, size_mm) for p in s.points)
