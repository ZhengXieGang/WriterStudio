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

import math
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
    "∞": ("mathupp", "^"),
    "°": ("mathupp", "`"), "≠": ("mathupp", "?"),
    "≡": ("mathupp", "@"), "∈": ("mathupp", "h"),
    "→": ("mathupp", "i"), "←": ("mathupp", "j"),
    "↓": ("mathupp", "k"),
    "∃": ("mathupp", "v"), "÷": ("mathupp", "x"),
    "∥": ("mathupp", "y"), "⊥": ("mathupp", "z"),
    "·": ("mathupp", "$"),
    "⋅": ("mathupp", "$"),
}
# √ 也不在这张表里：内置字体的根号是**三段分开**的笔画（小横 / 斜下 / 长斜线），
# 描出来在转折处会断开，而且不随 mathtext 把根号拉高（大根号会缩成一个小 V，
# 与顶线脱开）。根号改由 _radical_stroke 按骨架画**一笔**，并把顶线接在同一笔上。
#
# ∑ ∏ ∫ ∂ ∇ ∠ 没有挂在这张表里：mathupp 的字形（';' ':' 'p' 'm' 'n' '{'）
# 是**细楔形闭合轮廓**——描出来是双线空心，∠ 的 '{' 更是画成一个方括号。
# 它们改由 _draw_procedural_symbol 按几何单线画（见该函数的「大型运算符」段）。
# 同样地，± 曾挂在 mathupp 的空格键上（那里没有任何笔画），也改为几何画线。
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

#: 单线字体里没有键位、但文档中常见的符号：按几何直接画线。
# 宁可几何近似，也不让它们落回 mathtext 填充轮廓（空心字）。
_PROCEDURAL_SYMBOLS = frozenset(
    "× ≤ ≥ ≈ ≃ ≅ ≌ ∼ ∝ ≪ ≫ ≮ ≯ ⊂ ⊃ ⊆ ⊇ ⊊ ∪ ∩ ∅ ⊕ ⊖ ⊗ ⊘ ⊚ ⊙ ○ "
    "∈ ∉ ∋ ∀ ∄ ∴ ∵ ⇒ ⇐ ⇔ ↔ ↦ ∓ ± ∗ ∘ △ □ ◇ ∮ ′ ″ ‰ ≐ ≑ "
    "∑ ∏ ∫ ∂ ∇ ∠".split())


def _arc_pts(cx: float, cy: float, r: float, a0: float, a1: float,
             n: int = 12) -> list:
    """圆弧采样（角度制，逆时针，含两端点）。"""
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n)))
            for i in range(n + 1)]


def _circle_pts(cx: float, cy: float, r: float, n: int = 16) -> list:
    return [(cx + r * math.cos(2.0 * math.pi * i / n),
             cy + r * math.sin(2.0 * math.pi * i / n)) for i in range(n)]


def _cubic_pts(p0, p1, p2, p3, n: int = 18) -> list:
    """三次贝塞尔采样（含两端点）——积分号的 S 形靠它，直线近似不行。"""
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1.0 - t
        out.append((
            u * u * u * p0[0] + 3 * u * u * t * p1[0]
            + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1]
            + 3 * u * t * t * p2[1] + t * t * t * p3[1],
        ))
    return out


def _wave_pts(x0: float, y: float, w: float, amp: float,
              n: int = 8) -> list:
    """一段波浪线（先上后下，一个完整周期）——≈ ∼ 等符号的构件。"""
    return [(x0 + w * i / n, y + amp * math.sin(2.0 * math.pi * i / n))
            for i in range(n + 1)]


def _draw_procedural_symbol(ch: str, ox: float, oy: float, em: float
                            ) -> list[Stroke]:
    """按几何画单线数学符号（粗细交给笔尖，绝无空心轮廓）。

    记号约定：``oy`` 为基线，``em`` 为字号。关系符/运算符围绕数学轴线
    ``cy = oy + 0.27em`` 上下分布，与字体里既有符号保持同一视觉高度。
    """
    w = em * 0.62                  # 符号宽度
    x0 = ox
    cy = oy + em * 0.27            # 数学轴线
    g = em * 0.13                  # 双线间距
    a = em * 0.05                  # 波浪振幅
    r_ring = em * 0.26             # 圆圈类半径

    def line(p, q) -> Stroke:
        return Stroke([p, q], closed=False)

    def wave(y, amp=a, xx=None, ww=None) -> Stroke:
        return Stroke(_wave_pts(xx if xx is not None else x0, y,
                                ww if ww is not None else w, amp),
                      closed=False)

    def circle(cx, cyy, r, n=16) -> Stroke:
        return Stroke(_circle_pts(cx, cyy, r, n), closed=True)

    def arc(cx, cyy, r, a0, a1, n=12) -> Stroke:
        return Stroke(_arc_pts(cx, cyy, r, a0, a1, n), closed=False)

    def dot(p) -> Stroke:
        return Stroke([p, p], closed=False)      # 零长笔画 → 圆点

    def chevron(cx, right=True) -> Stroke:
        """尖括号 < >（尖端在左/右）。"""
        s = 1.0 if right else -1.0
        return Stroke([(cx + s * em * 0.20, cy + em * 0.22), (cx, cy),
                       (cx + s * em * 0.20, cy - em * 0.22)], closed=False)

    def over_slash(pad=0.0) -> Stroke:
        return line((x0 + pad, cy - em * 0.34), (x0 + w - pad, cy + em * 0.34))

    if ch == "×":
        return [Stroke([(x0, cy - g), (x0 + w * 0.72, cy + g)], closed=False),
                Stroke([(x0, cy + g), (x0 + w * 0.72, cy - g)], closed=False)]
    if ch in ("≤", "≥"):
        s = -1.0 if ch == "≥" else 1.0
        tipx = x0 + w * (0.5 - s * 0.5)
        backx = x0 + w * (0.5 + s * 0.5)
        return [Stroke([(backx, cy + g * 1.6), (tipx, cy), (backx, cy - g * 1.6)],
                       closed=False),
                line((x0, cy - g * 2.1), (x0 + w, cy - g * 2.1))]
    # -- 约等/相似类：波浪 + 横线 ---------------------------------------
    if ch == "≈":
        return [wave(cy + g * 0.55), wave(cy - g * 0.55)]
    if ch == "∼":
        return [wave(cy)]
    if ch == "≃":
        return [wave(cy + g * 0.7),
                line((x0, cy - g * 0.8), (x0 + w, cy - g * 0.8))]
    if ch in ("≅", "≌"):
        return [wave(cy + g * 0.95),
                line((x0, cy - g * 0.35), (x0 + w, cy - g * 0.35)),
                line((x0, cy - g * 1.25), (x0 + w, cy - g * 1.25))]
    if ch == "≐":
        return [line((x0, cy - g * 0.5), (x0 + w, cy - g * 0.5)),
                dot((x0 + w / 2.0, cy + g * 0.7))]
    if ch == "≑":
        return [line((x0, cy - g * 0.9), (x0 + w, cy - g * 0.9)),
                dot((x0 + w / 2.0, cy)),
                line((x0, cy + g * 0.9), (x0 + w, cy + g * 0.9))]
    if ch == "∝":
        hh = em * 0.24
        return [Stroke([(x0, cy), (x0 + w * 0.5, cy + hh * 1.2),
                        (x0 + w, cy + hh)], closed=False),
                Stroke([(x0, cy), (x0 + w * 0.5, cy - hh * 1.2),
                        (x0 + w, cy - hh)], closed=False)]
    if ch in ("≪", "≫"):
        s = 1.0 if ch == "≪" else -1.0
        return [chevron(x0 + (0.0 if s > 0 else em * 0.26), right=s > 0),
                chevron(x0 + (em * 0.26 if s > 0 else 0.0), right=s > 0)]
    if ch in ("≮", "≯"):
        return [chevron(x0, right=(ch == "≮")), over_slash()]
    # -- 集合类 ---------------------------------------------------------
    if ch in ("⊂", "⊃", "⊆", "⊇", "⊊"):
        r = em * 0.26
        if ch in ("⊂", "⊆", "⊊"):
            out = [arc(x0 + r * 0.75, cy, r, 60, 300)]
        else:
            out = [arc(x0 + w - r * 0.75, cy, r, -120, 120)]
        if ch in ("⊆", "⊇"):
            y = cy - r * math.sin(math.radians(60)) - em * 0.05
            out.append(line((x0, y), (x0 + w, y)))
        if ch == "⊊":
            out.append(line((x0 + w * 0.80, cy - em * 0.32),
                            (x0 + w * 0.34, cy + em * 0.32)))
        return out
    if ch in ("∪", "∩"):
        r = em * 0.27
        if ch == "∪":
            return [arc(x0 + r, cy + r * 0.35, r, 200, 340)]
        return [arc(x0 + r, cy - r * 0.35, r, 20, 160)]
    if ch in ("∈", "∉", "∋"):
        r = em * 0.29
        if ch == "∋":
            out = [arc(x0 + w - r, cy, r, -105, 105),
                   line((x0, cy), (x0 + w, cy))]
        else:
            out = [arc(x0 + r, cy, r, 75, 285),
                   line((x0, cy), (x0 + w * 0.98, cy))]
        if ch == "∉":
            out.append(over_slash(em * 0.05))
        return out
    if ch == "∅":
        return [circle(x0 + w / 2.0, cy, r_ring),
                over_slash(em * 0.02)]
    # -- 圆圈类 ---------------------------------------------------------
    if ch == "○":
        return [circle(x0 + w / 2.0, cy, r_ring)]
    if ch in ("⊕", "⊖", "⊗", "⊘", "⊚", "⊙"):
        cx, r = x0 + w / 2.0, r_ring
        out = [circle(cx, cy, r)]
        if ch == "⊕":
            out += [line((cx - r, cy), (cx + r, cy)),
                    line((cx, cy - r), (cx, cy + r))]
        elif ch == "⊖":
            out.append(line((cx - r, cy), (cx + r, cy)))
        elif ch == "⊗":
            d = r * 0.7071
            out += [line((cx - d, cy - d), (cx + d, cy + d)),
                    line((cx - d, cy + d), (cx + d, cy - d))]
        elif ch == "⊘":
            out.append(over_slash(em * 0.02))
        elif ch == "⊚":
            out.append(circle(cx, cy, r * 0.45, 12))
        else:                                     # ⊙
            out.append(dot((cx, cy)))
        return out
    # -- 逻辑/推理 ------------------------------------------------------
    if ch in ("∀", "∄"):
        half, top = w * 0.5, cy + em * 0.26
        bot = cy - em * 0.26
        if ch == "∀":
            out = [Stroke([(x0, bot), (x0 + half, top), (x0 + w, bot)],
                          closed=False)]
            ty = cy - em * 0.06
            f = (ty - bot) / (top - bot)
            out.append(line((x0 + half * (1.0 - f), ty),
                            (x0 + half * (1.0 + f), ty)))
            return out
        out = [line((x0, top), (x0 + w, top)),
               line((x0, bot), (x0 + w, bot)),
               line((x0 + w, top), (x0 + w, bot)),
               over_slash(em * 0.05)]
        return out
    if ch in ("∴", "∵"):
        up = ch == "∴"
        y1 = cy + em * 0.15 if up else cy - em * 0.15
        y2 = cy - em * 0.15 if up else cy + em * 0.15
        return [dot((x0 + w / 2.0, y1)),
                dot((x0 + em * 0.08, y2)),
                dot((x0 + w - em * 0.08, y2))]
    # -- 箭头 -----------------------------------------------------------
    if ch in ("⇒", "⇐", "⇔", "→", "←", "↔", "↦"):
        go_left = ch in ("⇐", "⇔", "←")
        go_right = ch in ("⇒", "⇔", "→", "↦")
        dbl = ch in ("⇒", "⇐", "⇔")
        out = [line((x0, cy), (x0 + w, cy))]
        if go_right:
            out.append(Stroke([(x0 + w - em * 0.17, cy + em * 0.15),
                               (x0 + w, cy),
                               (x0 + w - em * 0.17, cy - em * 0.15)],
                              closed=False))
            if dbl:
                out.append(Stroke([(x0 + w - em * 0.29, cy + em * 0.15),
                                   (x0 + w - em * 0.12, cy),
                                   (x0 + w - em * 0.29, cy - em * 0.15)],
                                  closed=False))
        if go_left:
            out.append(Stroke([(x0 + em * 0.17, cy + em * 0.15), (x0, cy),
                               (x0 + em * 0.17, cy - em * 0.15)],
                              closed=False))
            if dbl:
                out.append(Stroke([(x0 + em * 0.29, cy + em * 0.15),
                                   (x0 + em * 0.12, cy),
                                   (x0 + em * 0.29, cy - em * 0.15)],
                                  closed=False))
        if ch == "↦":
            out.append(line((x0 + w - em * 0.02, cy - em * 0.16),
                            (x0 + w - em * 0.02, cy + em * 0.16)))
        return out
    # -- 大型运算符：占满整个字身（Hershey 的字形是细楔形闭合轮廓，
    #    画出来是双线空心，这里改用单线几何）-----------------------------
    if ch in ("∑", "∏"):
        top = oy + em * 0.79          # 实测 mathtext 大型运算符占 -0.21..0.79 em
        bot = oy - em * 0.21
        ww = em * (0.78 if ch == "∑" else 0.86)
        if ch == "∏":
            return [line((x0, bot), (x0, top)), line((x0 + ww, bot), (x0 + ww, top)),
                    line((x0, top), (x0 + ww, top))]
        mid = (top + bot) / 2.0
        vx = x0 + ww * 0.52           # 尖角在右侧约一半宽处
        return [line((x0, top), (x0 + ww, top)),
                Stroke([(x0 + ww * 0.07, top), (vx, mid), (x0 + ww * 0.07, bot)],
                       closed=False),
                line((x0, bot), (x0 + ww, bot))]
    if ch == "∫":
        # 积分号：中段近竖直、两端带钩的 S——用三次贝塞尔（控制点交叉）得到
        top, bot = oy + em * 0.79, oy - em * 0.21
        cx = x0 + em * 0.31
        h = top - bot
        return [Stroke(_cubic_pts(
            (cx - em * 0.29, bot), (cx + em * 0.21, bot + h * 0.27),
            (cx - em * 0.21, top - h * 0.27), (cx + em * 0.29, top)), closed=False)]
    if ch in ("∂", "∇"):
        ww = em * 0.62
        if ch == "∇":
            top, bot = cy + em * 0.30, cy - em * 0.32
            return [Stroke([(x0, top), (x0 + ww, top), (x0 + ww / 2.0, bot)],
                           closed=True)]
        # ∂：右上缺口的圆碗 + 一条水平横杠（横杠齐碗口上沿向右伸出）
        r = em * 0.26
        ccx = x0 + r + em * 0.02
        return [arc(ccx, cy, r, 70.0, 390.0, 20),
                line((ccx + r * math.cos(math.radians(70.0)),
                      cy + r * math.sin(math.radians(70.0))),
                     (x0 + ww, cy + r * 0.92))]
    if ch == "∠":
        # 角：顶点在左下，一条水平边 + 一条斜边 + 一个小弧示意夹角
        vx, vy = x0, cy - em * 0.22
        return [line((vx, vy), (x0 + w, vy)),
                line((vx, vy), (x0 + w * 0.78, cy + em * 0.26)),
                arc(vx, vy, em * 0.22, 0.0, 33.0, 8)]
    # -- 其余常见符号 ---------------------------------------------------
    if ch in ("±", "∓"):
        # ±：上面「+」、下面一横；∓ 反过来（up 决定哪一半在上）
        up = 1.0 if ch == "±" else -1.0
        yy = cy + g * 0.6 * up
        bar = cy - g * 1.1 * up
        return [line((x0, yy), (x0 + w * 0.8, yy)),
                line((x0 + w * 0.4, yy - g * 0.8), (x0 + w * 0.4, yy + g * 0.8)),
                line((x0, bar), (x0 + w * 0.8, bar))]
    if ch == "∗":
        cx, d = x0 + w / 2.0, em * 0.13
        return [line((cx - d, cy), (cx + d, cy)),
                line((cx - d * 0.7, cy - d * 0.7), (cx + d * 0.7, cy + d * 0.7)),
                line((cx - d * 0.7, cy + d * 0.7), (cx + d * 0.7, cy - d * 0.7))]
    if ch == "∘":
        return [circle(x0 + w / 2.0, cy, em * 0.08, 10)]
    if ch in ("△", "□", "◇"):
        cx, rr = x0 + w / 2.0, r_ring
        if ch == "△":
            pts = [(cx, cy + rr), (cx - rr * 0.95, cy - rr * 0.72),
                   (cx + rr * 0.95, cy - rr * 0.72)]
        elif ch == "□":
            pts = [(cx - rr, cy - rr), (cx + rr, cy - rr), (cx + rr, cy + rr),
                   (cx - rr, cy + rr)]
        else:
            pts = [(cx, cy + rr), (cx + rr, cy), (cx, cy - rr), (cx - rr, cy)]
        return [Stroke(pts, closed=True)]
    if ch == "′":
        return [line((x0 + em * 0.17, cy + em * 0.08),
                     (x0 + em * 0.07, cy + em * 0.36))]
    if ch == "″":
        return [line((x0 + em * 0.15, cy + em * 0.08),
                     (x0 + em * 0.05, cy + em * 0.36)),
                line((x0 + em * 0.33, cy + em * 0.08),
                     (x0 + em * 0.23, cy + em * 0.36))]
    if ch == "‰":
        rr = em * 0.09
        return [circle(x0 + rr + em * 0.05, cy + em * 0.15, rr, 10),
                circle(x0 + rr + em * 0.05, cy - em * 0.15, rr, 10),
                line((x0 + em * 0.05, cy - em * 0.30),
                     (x0 + em * 0.35, cy + em * 0.30))]
    if ch == "∮":
        # 环路积分：与 ∫ 同一副骨架（一整个字身高的 S），中间加一个小圈
        top, bot = oy + em * 0.79, oy - em * 0.21
        cx = x0 + em * 0.31
        h = top - bot
        return [Stroke(_cubic_pts(
                    (cx - em * 0.29, bot), (cx + em * 0.21, bot + h * 0.27),
                    (cx - em * 0.21, top - h * 0.27), (cx + em * 0.29, top)),
                    closed=False),
                circle(cx, cy, em * 0.10, 12)]
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
                    text_scale: float = 1.0,
                    fonts: Optional[Sequence[Any]] = None) -> list[Stroke]:
    """把 LaTeX 公式渲染为笔画（单位 mm，Y 向上，基线 y=0，左端 x=0）。

    ``size_mm`` 是公式主体字号(mm)。``tolerance`` 为曲线离散容差(mm)。

    给定了字体链（``fonts`` 直接给字体，或 ``font_names`` + ``manager``
    按名字解析）时，链上画得出的字符改用字体链重排（``text_scale`` 可
    整体微调字号）；链画不出的字符走内置单线符号与几何画线，结构线
    （分数线/根号顶线）取中线单笔。没有字体链时用 mathtext 原生轮廓。
    """
    chain = list(fonts) if fonts else []
    if not chain and font_names and manager is not None:
        for n in font_names:
            f = manager.get(n)
            if f is not None:
                chain.append(f)
    if chain:
        try:
            return _render_with_fonts(latex, size_mm, chain, text_scale,
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


def _glyph_ink_box(font, fontsize: float, glyph_index: int,
                   ox: float, oy: float, scale: float) -> Optional[BBox]:
    """字形墨迹在页面坐标（mm）里的包围盒；取不到返回 None。

    与轮廓兜底同一条取路径（``font.get_path()``）；字号由调用方随后按需
    重设，这里直接改字体对象的状态即可（mathtext 的解析器每次都重设）。
    """
    from matplotlib.path import Path as MplPath

    font.clear()
    font.set_size(fontsize, 72)
    font.load_glyph(glyph_index, flags=_load_flags.NO_HINTING)
    verts, codes = font.get_path()
    box = BBox()
    for poly in MplPath(verts, codes).to_polygons(closed_only=False):
        for px, py in poly:
            box.expand((float(px) * scale + ox * scale,
                        float(py) * scale + oy * scale))
    return None if box.is_empty else box


#: 根号骨架（归一化到墨迹包围盒）：左上小横 → 折到最低点 → 拉长斜线到右上角。
#: 取自内置 Hershey 数学字体的根号字形，与 mathtext 根号的骨架一致。
_RADICAL_SPINE = ((0.0, 0.56), (0.211, 0.56), (0.526, 0.0), (1.0, 1.0))


def _radical_stroke(box: BBox, bar: Optional[tuple[float, float, float]]
                    ) -> Stroke:
    """单笔根号：一笔画完「小横 → 最低点 → 右上角」，顶线接在同一笔上。

    ``bar`` 为顶线中线 ``(x0, x1, y)``；给了就顺着斜线一笔写到顶线末端
    ——写根号本就是一笔到底，分成「根号 + 顶线」两笔在衔接处总有断口。

    mathtext 的根号是**有厚度的填充字形**，描轮廓出来是双线空心；内置字体
    的根号又是三段分开的笔画。这里按骨架一次画成单线。
    """
    w, h = box.width, box.height
    pts = [(box.x0 + fx * w, box.y0 + fy * h) for fx, fy in _RADICAL_SPINE]
    if bar is not None:
        pts.append((bar[0], bar[2]))
        pts.append((bar[1], bar[2]))
    return Stroke(pts, closed=False)


def _take_vinculum(rects: list, box: BBox) -> Optional[tuple[float, float, float]]:
    """找根号的顶线矩形并摘掉，返回其中线 ``(x0, x1, y)``；没有则 None。

    顶线是 mathtext 给的结构矩形：横线、y 贴根号右上角、x 从根号右侧开始。
    容差按根号高度取（字号不同的公式里偏移量同比例变化），嵌套根号按
    最近的一条配对。
    """
    tol = max(0.5, 0.12 * box.height)
    best = -1
    best_d = tol
    for i, (_ox, oy, rw, rh) in enumerate(rects):
        if rw < rh:                      # 竖规则线：不是顶线
            continue
        y = (oy + rh / 2.0) * PT_TO_MM
        x0 = _ox * PT_TO_MM
        d = abs(y - box.y1)
        if d <= best_d and box.x1 - tol <= x0 <= box.x1 + tol:
            best, best_d = i, d
    if best < 0:
        return None
    ox, oy, rw, rh = rects.pop(best)
    return (ox * PT_TO_MM, (ox + rw) * PT_TO_MM,
            (oy + rh / 2.0) * PT_TO_MM)


def _render_native(latex: str, size_mm: float, tolerance: float) -> list[Stroke]:
    """无字体链：与字体链路径同一套渲染，只是字形全部走轮廓。

    这样符号（× ≤ ∑ √ …）仍是单线/几何画线、结构线取中线——与字体链路径
    一致，不会退化成空心轮廓。``tolerance`` 仅为兼容旧签名保留。

    失败时退回 :func:`_render_outlines`（纯 mathtext 轮廓）：宁可空心，
    也不让公式消失。
    """
    try:
        return _render_with_fonts(latex, size_mm, [], 1.0, None)
    except Exception:
        return _render_outlines(latex, size_mm)


def _render_outlines(latex: str, size_mm: float) -> list[Stroke]:
    """纯轮廓兜底：整条公式用 mathtext 轮廓（历史行为）。"""
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
    # 解析结果由解析器**缓存**（同一公式下次直接返回同一份 list）：根号会
    # 摘走自己的顶线矩形，必须改副本，否则第二次渲染同一条公式时顶线就
    # 没了（联动缓存还会把别的公式一起带坏）。
    rects = list(rects)

    scale = PT_TO_MM
    strokes: list[Stroke] = []
    radicals: list[BBox] = []          # 根号墨迹盒：顶线要按它配对（见下）
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
        # 根号：按骨架画单笔，并与顶线合成一笔（见 _radical_stroke）。
        # 字号/位置仍取 mathtext 的排版结果——大根号会被拉高，字形墨迹盒
        # 就是它该占的范围，顶线随后按位置配对。
        if ch == "\u221a":
            box = _glyph_ink_box(font, fontsize, glyph_index, ox, oy, scale)
            if box is not None:
                radicals.append(box)
                continue
        # 单线兜底：内置 Hershey 数学/希腊字体（绝不出空心轮廓）。
        # 没有 manager（如 Markdown 里的行内公式）时按名字直接加载内置字体
        fb = _SYMBOL_FALLBACK.get(ch)
        if fb is not None:
            from ..fonts.layout import TextStyle, _symbol_font, layout_text
            fam = symbol_fonts.get(fb[0]) or _symbol_font(fb[0])
            if fam is not None and fam.has(fb[1]):
                gsize = fontsize * scale * max(0.1, min(5.0, text_scale))
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

    # 根号：单笔骨架 + 配对的顶线合成一笔（根号顶线因此不再单独描一遍）
    for rbox in radicals:
        strokes.append(_radical_stroke(rbox, _take_vinculum(rects, rbox)))

    # 分式线/上划线等结构矩形：mathtext 给的是「有厚度的规则线」
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
