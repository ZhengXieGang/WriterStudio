"""文本排版引擎。

核心设计 —— **字体回退链（fallback chain）= 单文档多字体的实现机制**：

    一次排版传入若干字体构成的候选序列 ``fonts``。逐字符从左到右，
    第一个包含该字符的字体负责绘制该字符；都不含时跳过并留空。
    因此「中文楷体 + Hershey 英文」组合即可自动实现中英混排，
    无需用户手动切换字体——这就是「单文档多字体」。

    在回退链之上还支持两种进阶用法：

    * **按概率随机选字体**（``TextStyle.random_fonts`` + ``font_weights``）：
      当多个字体都能画同一个字时，按权重随机挑一个，从而在一段文字里混用
      多款手写字体（例如 70% 用 A 体、30% 用 B 体），更像真人所写。
    * **兜底字体**：链中所有（有权重的）字体都缺该字时，用兜底字体补上。
      由于任何字都会先在整条链里找候选，链内字体天然互相兜底，兜底字体
      只是「最后一道保险」。

每个字符的排版结果记录其**归属字体**（:class:`CharPlacement`），
供后续渲染与扰动引擎逐字处理（扰动需要知道每个字的位置与字体）。

所有输出坐标为页面单位(mm)，Y 轴向上，行首基线起点由 ``origin`` 指定。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..core.geometry import BBox, Vec2
from ..core.strokes import Stroke
from .model import FontFamily

ALIGN_LEFT = "left"
ALIGN_CENTER = "center"
ALIGN_RIGHT = "right"

# 书写方向（3=横向、1=竖排左→右、2=竖排右→左）
DIR_H = "h"          # 横向：字符沿 +X 排，行沿 -Y 推进
DIR_V_RL = "v-rl"    # 竖排·右到左：列内字符向下，列从右往左排（传统中文）
DIR_V_LR = "v-lr"    # 竖排·左到右：列内字符向下，列从左往右排

DEFAULT_FONT_SEED = 20240910


@dataclass
class TextStyle:
    size: float = 10.0                 # 字号(mm，em 高度)
    line_spacing: float = 1.4          # 行距倍数（× 行高）
    char_spacing: float = 0.0          # 额外字距(mm)
    align: str = ALIGN_LEFT
    tolerance: float = 0.0             # 预留：轮廓离散精度
    random_fonts: bool = False         # 是否在同可绘字体间按权重随机
    font_weights: dict[str, float] = field(default_factory=dict)  # 字体名 → 权重
    direction: str = DIR_H             # 书写方向（横向 / 竖排两种）
    # 空格宽度百分比（100=按字体空格步距），random 区间为 ±%
    word_space: float = 100.0
    word_space_random: float = 0.0
    # 文本框宽度(mm)：>0 时按此宽度自动换行；0 = 不限宽（按 \n 手动换行）
    frame_width: float = 0.0
    # 逐字符字体覆盖：字符 → {"font": 字体名, "scale": 可选缩放}
    # 用于「为指定字符设置另一套字体」（如个别符号用符号字体）。
    char_overrides: dict[str, dict] = field(default_factory=dict)

    def line_height(self, font: FontFamily) -> float:
        return font.line_height(self.size, self.line_spacing)

    def weight_of(self, font: FontFamily) -> float:
        """取字体权重；未配置默认 1.0，负值按 0 处理。"""
        w = self.font_weights.get(font.name, 1.0)
        try:
            return max(0.0, float(w))
        except (TypeError, ValueError):
            return 1.0



@dataclass
class CharPlacement:
    """单个已排版字符（页面单位，已含 origin 平移）。"""

    char: str
    font_name: str
    origin: Vec2                  # 该字基线左端点在页面坐标中的位置
    scale: float                  # 字体单位 → mm
    advance: float
    strokes: list[Stroke] = field(default_factory=list)
    line_index: int = 0
    char_index: int = 0
    #: 该字符在整段文本（含换行符）中的绝对下标，供画布文字编辑器
    #: 在「排版结果 ↔ 源文本」之间映射光标与选区。
    text_index: int = -1

    def bbox(self) -> BBox:
        return BBox.from_points(p for s in self.strokes for p in s.points)


@dataclass
class LineLayout:
    text: str
    baseline_y: float
    width: float
    chars: list[CharPlacement] = field(default_factory=list)
    x_start: float = 0.0
    #: 本行首字符在整段文本中的绝对下标（空行/缺字行也有值），
    #: 供画布文字编辑器定位空行上的光标。
    text_start: int = -1

    def bbox(self) -> BBox:
        box = BBox()
        for c in self.chars:
            box = box.union(c.bbox())
        return box


@dataclass
class TextLayout:
    """整段文本的排版结果。"""

    lines: list[LineLayout] = field(default_factory=list)
    origin: Vec2 = (0.0, 0.0)
    style: Optional[TextStyle] = None
    # 回退链中没有任何字体可绘制、被跳过并留空的字符（按首次出现顺序去重）
    missing: list[str] = field(default_factory=list)

    @property
    def chars(self) -> list[CharPlacement]:
        return [c for ln in self.lines for c in ln.chars]

    def strokes(self) -> list[Stroke]:
        return [s for c in self.chars for s in c.strokes]

    def advance_span(self) -> float:
        """非空行的最大**步距跨度**（mm），不依赖字形笔画。

        ``with_strokes=False`` 排版时 ``strokes`` 为空、``bbox()`` 退化为
        零，故测宽只能用行步距跨度。换行判定（``_split_wrapped``）本就
        按步距比较，用它与断行语义一致；表格列宽/文本度量走这里，避免
        「只取宽度却构造全部笔画」的开销。
        """
        return max((ln.x_start + ln.width for ln in self.lines
                    if ln.chars), default=0.0)

    def bbox(self) -> BBox:
        box = BBox()
        for ln in self.lines:
            box = box.union(ln.bbox())
        return box

    def width(self) -> float:
        b = self.bbox()
        return b.width

    def height(self) -> float:
        b = self.bbox()
        return b.height


def _char_override(style: TextStyle, ch: str) -> dict:
    """取某字符的字体覆盖配置（无覆盖返回空 dict）。"""
    ov = style.char_overrides.get(ch) if style.char_overrides else None
    return ov if isinstance(ov, dict) else {}


def _override_scale(style: TextStyle, ch: str) -> float:
    """取某字符覆盖配置里的缩放系数（缺省/非法值均按 1.0）。"""
    try:
        v = float(_char_override(style, ch).get("scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        return 1.0
    return max(0.1, min(5.0, v))


def _pick_font(fonts: Sequence[FontFamily], ch: str, style: TextStyle,
               rng: Optional[random.Random]) -> Optional[FontFamily]:
    """为字符挑选字体。

    * 字符级覆盖（``char_overrides``）最优先：指定字体在链中且能画该字
      时直接用它（并跳过随机挑选）。
    * 先在所有字体中找出能画该字的候选（这一步天然实现「互相兜底」）。
    * 未启用随机，或只有唯一候选 → 取链中靠前者。
    * 启用随机且有多个候选 → 按权重随机；权重全为 0 时退回首候选。
    """
    ov_font = _char_override(style, ch).get("font")
    if ov_font:
        for f in fonts:
            if f.name == ov_font and f.has(ch):
                return f
        # 覆盖字体不在链中或缺该字 → 按正常规则挑选
    candidates = [f for f in fonts if f.has(ch)]
    if not candidates:
        return None
    if not style.random_fonts or len(candidates) == 1 or rng is None:
        return candidates[0]
    weights = [style.weight_of(f) for f in candidates]
    if sum(weights) <= 0.0:
        return candidates[0]
    return rng.choices(candidates, weights=weights, k=1)[0]


def _line_height(fonts: Sequence[FontFamily], style: TextStyle) -> float:
    """以链首字体为准计算行高（保证行距稳定，不随字符变化跳动）。"""
    if not fonts:
        return style.size * style.line_spacing
    return fonts[0].line_height(style.size, style.line_spacing)


def _space_factor(style: TextStyle, rng: Optional[random.Random]) -> float:
    """空格宽度系数：word_space 百分比 ± word_space_random 随机。"""
    base = style.word_space if style.word_space > 0 else 100.0
    if style.word_space_random > 0 and rng is not None:
        r = style.word_space_random
        return max(0.0, base + rng.uniform(-r, r)) / 100.0
    return base / 100.0


# ---------------------------------------------------------------- 断行规则
def _is_wide(ch: str) -> bool:
    """宽字符（CJK 各块 + 全角形式）：排版上可在其前后断行。"""
    o = ord(ch)
    return (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF
            or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF
            or 0xFE30 <= o <= 0xFE4F or 0xFF00 <= o <= 0xFF60
            or 0xFFE0 <= o <= 0xFFE6 or 0x20000 <= o <= 0x3FFFD)


# 行首禁排（闭合标点不落行首）与行尾禁排（开口标点不悬行尾）
_CLOSING_NO_HEAD = set("、。，．！？：；）］｝》」』】〉％‰°…—”’.,;:!?%)]}")
_OPENING_NO_TAIL = set("（［｛《「『【〈“‘([{")


def _can_break_before(prev: Optional[str], ch: str) -> bool:
    """prev 与 ch 之间是否允许换行（中文排版惯例的简化版）。

    * 空格之后（词边界）总可断；空格本身不落行首；
    * 闭合标点不落行首、开口标点不悬行尾；
    * 宽字符（CJK）前后可断；西文单词内部不断。
    """
    if prev is None or prev == "\n":
        return False
    if ch.isspace():
        return False
    if prev.isspace():
        return True
    if ch in _CLOSING_NO_HEAD:
        return False
    if prev in _OPENING_NO_TAIL:
        return False
    return _is_wide(ch) or _is_wide(prev)


def _split_wrapped(cand: list[tuple[CharPlacement, float]], raw: str,
                   line_text0: int, wrap_w: float) -> list[list[int]]:
    """把一行的候选字符（下标 + 行内 x）按宽度分成若干行。

    贪心断行：越界的字符若之前存在合适断点则回退到断点，否则从它
    之前硬断；单个字符比整行还宽时允许独占超宽一行。
    返回每个「视觉行」的候选下标列表。
    """
    groups: list[list[int]] = []
    start = 0           # 当前视觉行首个候选下标
    last_break: Optional[int] = None    # 最近的可断位置（在该候选前断）
    for i, (pl, xi) in enumerate(cand):
        while i > start and xi + pl.advance - cand[start][1] > wrap_w:
            bp = last_break if (last_break is not None
                                and last_break > start) else i
            if bp <= start:
                break       # 当前字符独占一行也放不下：允许超宽，防止死循环
            groups.append(list(range(start, bp)))
            start = bp
            last_break = None
        t = pl.text_index
        if t > line_text0:
            if _can_break_before(raw[t - 1 - line_text0], pl.char):
                last_break = i
    groups.append(list(range(start, len(cand))))
    return groups


def layout_text(text: str,
                fonts: Sequence[FontFamily],
                style: Optional[TextStyle] = None,
                origin: Vec2 = (0.0, 0.0),
                *,
                seed: Optional[int] = None,
                with_strokes: bool = True) -> TextLayout:
    """把 ``text`` 按 ``fonts`` 回退链排版。

    ``text`` 中的 ``\\n`` 换行；空行保留行高。
    启用 ``style.random_fonts`` 时，``seed`` 决定字体随机分配的可复现性。

    ``with_strokes=False`` 时跳过字形笔画的构造（只算步距与行分组），
    返回的 ``TextLayout`` 仅供**测宽**（用 :meth:`TextLayout.advance_span`，
    勿取 ``strokes()``/``bbox()``）——Markdown 换行度量与表格列宽计算会
    反复测宽，构造笔画再丢弃是主要热点。

    书写方向（``style.direction``）：
        * :data:`DIR_H` 横向（默认）：字符沿 +X，行沿 -Y；
        * :data:`DIR_V_RL` 竖排右→左：列内字符向下、列从右往左（传统中文）；
        * :data:`DIR_V_LR` 竖排左→右：列内字符向下、列从左往右。
    竖排时 ``LineLayout`` 表示一列，``baseline_y`` 为列顶 y、``x_start`` 为列 x。
    """
    style = style or TextStyle()
    # Windows/老 Mac 换行归一化：游离的 \r 会被当成未知字符
    # （进 missing 列表、占一个空位）
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if style.direction in (DIR_V_RL, DIR_V_LR):
        return _layout_vertical(text, fonts, style, origin, seed=seed)
    return _layout_horizontal(text, fonts, style, origin, seed=seed,
                              with_strokes=with_strokes)


def measure_text_width(text: str,
                       fonts: Sequence[FontFamily],
                       style: Optional[TextStyle] = None,
                       *,
                       seed: Optional[int] = None) -> float:
    """只算一行文本的**步距宽度**（mm），不构造任何排版对象。

    与 ``_layout_horizontal`` 第一遍的推进逻辑保持一致（同一 ``_pick_font``、
    同一缩放/字距/词距规则、同一固定种子），因此结果与
    ``layout_text(..., with_strokes=False).advance_span()`` 相同——但省去
    逐字符 ``CharPlacement`` 与列表构造。Markdown 换行/列宽会做上万次
    测宽（``_measure``），这是重排卡顿的主要来源。
    """
    if not text or not fonts:
        return 0.0
    style = style or TextStyle()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    rng = random.Random(DEFAULT_FONT_SEED if seed is None else seed) \
        if (style.random_fonts or style.word_space_random > 0) else None
    total = 0.0
    for ch in text:
        if ch == "\n":
            break                       # 只度量第一行
        font = _pick_font(fonts, ch, style, rng)
        if font is None:
            total += (fonts[0].default_advance * fonts[0].scale_for_size(style.size)
                      + style.char_spacing)
            continue
        if ch == "\t":
            total += 4.0 * font.default_advance * font.scale_for_size(style.size)
            continue
        scale = font.scale_for_size(style.size)
        if _char_override(style, ch).get("font") == font.name:
            scale *= _override_scale(style, ch)
        g = font.glyph(ch)
        if g is None:
            continue
        adv = g.advance * scale + style.char_spacing
        if ch == " ":
            adv = (g.advance * scale * _space_factor(style, rng)
                   + style.char_spacing)
        total += adv
    return total


def _layout_horizontal(text: str,
                       fonts: Sequence[FontFamily],
                       style: TextStyle,
                       origin: Vec2,
                       *,
                       seed: Optional[int] = None,
                       with_strokes: bool = True) -> TextLayout:
    origin_x, origin_y = origin
    result = TextLayout(origin=origin, style=style)

    if not fonts:
        return result

    rng = random.Random(DEFAULT_FONT_SEED if seed is None else seed) \
        if (style.random_fonts or style.word_space_random > 0) else None

    raw_lines = text.split("\n")
    lh = _line_height(fonts, style)
    wrap_w = style.frame_width
    missing: list[str] = []
    missing_seen: set[str] = set()
    text_pos = 0            # 当前 raw 行首字符在整段文本中的绝对下标
    row_index = 0           # 视觉行号（换行/自动换行统一递增）

    for raw in raw_lines:
        # -- 第一遍：沿一条基线累积候选字符（x 为行内相对位置），不断行 --
        cand: list[tuple[CharPlacement, float]] = []
        x = 0.0
        for ci, ch in enumerate(raw):
            font = _pick_font(fonts, ch, style, rng)
            if font is None:
                if ch != " " and ch not in missing_seen:
                    missing_seen.add(ch)
                    missing.append(ch)
                # 无字体可绘制：按链首字体的平均宽度留空
                x += fonts[0].default_advance * fonts[0].scale_for_size(style.size) \
                    + style.char_spacing
                continue
            if ch == "\t":
                x += 4.0 * font.default_advance * font.scale_for_size(style.size)
                continue
            # 字符级覆盖的缩放只在覆盖字体真正被采用时生效
            ov_font = _char_override(style, ch).get("font")
            scale = font.scale_for_size(style.size)
            if ov_font and font.name == ov_font:
                scale *= _override_scale(style, ch)
            g = font.glyph(ch)
            if g is None:
                continue
            adv = g.advance * scale + style.char_spacing
            if ch == " ":
                # 空格宽度按词距百分比（可随机），保持额外字距
                adv = g.advance * scale * _space_factor(style, rng) + style.char_spacing
            pl = CharPlacement(
                char=ch, font_name=font.name,
                origin=(origin_x + x, origin_y), scale=scale,
                advance=adv,
                strokes=(g.to_strokes(scale, (origin_x + x, origin_y))
                         if with_strokes else []),
                line_index=row_index, char_index=ci,
                text_index=text_pos + ci,
            )
            cand.append((pl, x))
            x += adv

        # -- 第二遍：按文本框宽度分组（自动换行），逐行落基线与对齐 --
        groups = _split_wrapped(cand, raw, text_pos, wrap_w) \
            if wrap_w > 0 and cand else [list(range(len(cand)))]
        for idxs in groups:
            baseline_y = origin_y - row_index * lh
            line = LineLayout(text="", baseline_y=baseline_y, width=0.0,
                              text_start=text_pos)
            if idxs:
                pl_first, x0 = cand[idxs[0]]
                pl_last, x_last = cand[idxs[-1]]
                w_line = x_last + pl_last.advance - x0
                # 末字额外字距是「字与字之间」的量，不计入行宽
                if style.char_spacing:
                    w_line -= style.char_spacing
                # 行尾空白不占宽度（断行点的空格/制表符）
                for j in reversed(idxs):
                    pl_j = cand[j][0]
                    if pl_j.char in " \t":
                        w_line -= pl_j.advance
                    else:
                        break
                dx = 0.0
                if style.align == ALIGN_CENTER:
                    dx = -w_line / 2.0
                elif style.align == ALIGN_RIGHT:
                    dx = -w_line
                for j in idxs:
                    pl, xi = cand[j]
                    # 笔画在第一遍已落在 (origin_x+xi, origin_y)，而最终
                    # 位置 = origin_x + (xi - x0) + dx；故笔画只需平移
                    # (dx - x0, 基线差)，绝对位置以 origin 为准直接赋值。
                    sdx = dx - x0
                    sdy = baseline_y - origin_y
                    if sdx or sdy:
                        for s_ in pl.strokes:
                            s_.points = [(px + sdx, py + sdy)
                                         for px, py in s_.points]
                    pl.origin = (origin_x + xi - x0 + dx, baseline_y)
                    pl.line_index = row_index
                    line.chars.append(pl)
                line.width = w_line
                line.x_start = origin_x + dx
                t_a = pl_first.text_index
                t_b = pl_last.text_index
                line.text = raw[t_a - text_pos: t_b - text_pos + 1]
                line.text_start = t_a
            else:
                line.x_start = origin_x
                line.text = raw
            result.lines.append(line)
            row_index += 1

        text_pos += len(raw) + 1

    result.missing = missing
    return result


def _layout_vertical(text: str,
                     fonts: Sequence[FontFamily],
                     style: TextStyle,
                     origin: Vec2,
                     *,
                     seed: Optional[int] = None) -> TextLayout:
    """竖排：列内字符向下堆叠，列间沿水平推进。

    * 字符保持**直立**不旋转（传统中文竖排习惯）；
    * 字符在列内按其步距向下推进，步距 = 字形步距 + 额外字距；
    * 列间距 = 行高（``line_spacing`` × 字号）；
    * ``DIR_V_RL``：第一列在 origin 处、后续列向 **左**；
      ``DIR_V_LR``：第一列在 origin 处、后续列向 **右**。
    ``LineLayout`` 表示一列：``baseline_y``=列顶 y、``x_start``=列 x、
    ``width``=列长（向下延伸量）。
    """
    origin_x, origin_y = origin
    result = TextLayout(origin=origin, style=style)
    if not fonts:
        return result

    rng = random.Random(DEFAULT_FONT_SEED if seed is None else seed) \
        if (style.random_fonts or style.word_space_random > 0) else None
    lh = _line_height(fonts, style)
    col_step = -lh if style.direction == DIR_V_RL else lh   # 列推进方向
    missing: list[str] = []
    missing_seen: set[str] = set()
    text_pos = 0            # 当前 raw 行首字符在整段文本中的绝对下标

    for li, raw in enumerate(text.split("\n")):
        col_x = origin_x + li * col_step
        line = LineLayout(text=raw, baseline_y=origin_y, width=0.0,
                          x_start=col_x, text_start=text_pos)
        placements: list[CharPlacement] = []
        y = origin_y                     # 列顶（字符从顶向下堆叠）
        trailing_cs = 0.0                # 末字多出的额外字距（不计入列长）
        for ci, ch in enumerate(raw):
            font = _pick_font(fonts, ch, style, rng)
            if font is None:
                if ch != " " and ch not in missing_seen:
                    missing_seen.add(ch)
                    missing.append(ch)
                y -= fonts[0].default_advance * fonts[0].scale_for_size(style.size) \
                    + style.char_spacing
                trailing_cs = 0.0
                continue
            if ch == "\t":
                y -= 4.0 * font.default_advance * font.scale_for_size(style.size)
                trailing_cs = 0.0
                continue
            # 字符级覆盖的缩放只在覆盖字体真正被采用时生效
            ov_font = _char_override(style, ch).get("font")
            scale = font.scale_for_size(style.size)
            if ov_font and font.name == ov_font:
                scale *= _override_scale(style, ch)
            g = font.glyph(ch)
            if g is None:
                trailing_cs = 0.0
                continue
            adv = g.advance * scale + style.char_spacing
            if ch == " ":
                adv = g.advance * scale * _space_factor(style, rng) + style.char_spacing
            if _is_wide(ch):
                # 宽字符竖放按一个 em 步进（西文步距与竖排无关）
                adv = style.size + style.char_spacing
            # 字形单元竖放：单元区间 [y-adv, y]，基线落在单元底部，
            # 使 CJK 字形（墨迹在基线上方）恰好填满单元并保持直立。
            strokes = g.to_strokes(scale, (col_x, y - adv))
            placements.append(CharPlacement(
                char=ch, font_name=font.name,
                origin=(col_x, y - adv), scale=scale,
                advance=adv, strokes=strokes,
                line_index=li, char_index=ci,
                text_index=text_pos + ci,
            ))
            y -= adv
            trailing_cs = style.char_spacing
        # 末字的额外字距不计入列长（与横向排版同理）
        if placements and trailing_cs:
            placements[-1].advance -= trailing_cs
        line.width = origin_y - y - trailing_cs   # 列长（向下延伸量）
        line.chars = placements
        result.lines.append(line)
        text_pos += len(raw) + 1

    result.missing = missing
    return result


def layout_text_object(text: str,
                       fonts: Sequence[FontFamily],
                       style: Optional[TextStyle] = None,
                       origin: Vec2 = (0.0, 0.0),
                       *,
                       seed: Optional[int] = None):
    """排版并直接返回 (笔画列表, 每字符信息)，供文档对象使用。"""
    lay = layout_text(text, fonts, style, origin, seed=seed)
    return lay.strokes(), lay


def frame_box(lay: TextLayout,
              fonts_by_name: Optional[dict[str, "FontFamily"]] = None,
              ) -> Optional[tuple[float, float, float, float]]:
    """文本框几何（排版坐标，mm）：``(x0, y_top, x1, y_bottom)``。

    Y 轴向上，故 ``y_top > y_bottom``。x0 恒为 0（排版起点）；x1 在
    设了 ``style.frame_width`` 时就是换行边界宽度，否则取排版实际宽度。
    行带按每行首字符所属字体的上伸/下延估算，缺字体时退化为
    0.8/0.25 倍字号。无行时返回 None。
    """
    if not lay.lines:
        return None
    fonts_by_name = fonts_by_name or {}
    style = lay.style
    size = style.size if style is not None else 10.0
    fw = style.frame_width if style is not None else 0.0
    y_top = -1e9
    y_bottom = 1e9
    x1 = 0.0
    for ln in lay.lines:
        f0 = fonts_by_name.get(ln.chars[0].font_name) if ln.chars else None
        if f0 is not None:
            sc = f0.scale_for_size(size)
            asc = f0.ascent * sc
            desc = -f0.descent * sc
        else:
            asc, desc = size * 0.8, size * 0.25
        y_top = max(y_top, ln.baseline_y + asc)
        y_bottom = min(y_bottom, ln.baseline_y - desc)
        x1 = max(x1, ln.x_start + ln.width)
    if fw > 0:
        return (0.0, y_top, fw, y_bottom)
    return (0.0, y_top, max(x1, size * 0.5), y_bottom)
