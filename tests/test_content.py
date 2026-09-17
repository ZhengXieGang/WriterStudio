"""P4 富内容测试：Markdown/表格、公式、SVG 导入、LaTeX 工具链探测。"""

from __future__ import annotations

from pathlib import Path

import pytest

from writerstudio.core.geometry import AffineTransform
from writerstudio.core.strokes import Stroke
from writerstudio.content.builder import (
    SOURCE_EQUATION,
    SOURCE_MARKDOWN,
    make_equation_object,
    make_markdown_object,
    make_svg_object,
    regenerate_content_object,
)
from writerstudio.content.equation import (
    equation_bbox,
    mathtext_available,
    render_equation,
    wrap_math,
)
from writerstudio.content.markdown import MarkdownStyle, render_markdown
from writerstudio.content.svg_import import import_svg, svg_available
from writerstudio.fonts.manager import FontManager
from writerstudio.fonts.model import FontFamily, Glyph
from writerstudio.perturb.params import PerturbParams

# 本机参考字库目录（不随仓库分发；克隆机上不存在时相关测试自动跳过）
REF = Path(__file__).resolve().parents[2] / "references"


@pytest.fixture(scope="module")
def fonts():
    f = FontFamily(name="t", kind="hershey", units_per_em=10.0, glyphs={
        **{c: Glyph(c, [[(0, 0), (0, 6), (4, 6), (4, 0)]], advance=5)
           for c in "abcdefghijklmnopqrstuvwxyz"},
        **{c: Glyph(c, [[(0, 0), (0, 6), (4, 6), (4, 0)]], advance=5)
           for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
        **{c: Glyph(c, [[(0, 0), (0, 5), (4, 5), (4, 0)]], advance=5)
           for c in "0123456789"},
        " ": Glyph(" ", [], advance=3),
    })
    return [f]


@pytest.fixture(scope="module")
def manager():
    return FontManager(user_font_dir="/nonexistent-xyz")


# ============================================================ Markdown
def test_markdown_heading_and_paragraph(fonts):
    r = render_markdown("# Title\n\nHello world", fonts, MarkdownStyle(size=5))
    assert "h1" in r.blocks
    assert "p" in r.blocks
    assert r.strokes


def test_markdown_bad_formula_does_not_abort(fonts):
    """坏公式（$\\frac{1}{2$）应退化为原样文本而不是让整篇渲染抛异常。"""
    r = render_markdown("Hello $\\frac{1}{2$ world", fonts, MarkdownStyle(size=5))
    assert r.strokes


def test_markdown_overlong_token_wrapped(fonts):
    """超出整行宽度的长 token（URL 等）必须被硬切，不得超宽。"""
    style = MarkdownStyle(size=5, wrap_width=30.0)
    r = render_markdown(
        "see https://example.com/" + "a" * 120 + " end", fonts, style)
    max_x = max(p[0] for s in r.strokes for p in s.points)
    assert max_x <= 30.0 + 6.0     # 行宽 + 单字符容差


def test_markdown_table_structure(fonts):
    src = ("| A | B |\n|:--|--:|\n| 1 | 2 |\n| 3 | 4 |\n")
    r = render_markdown(src, fonts, MarkdownStyle(size=5))
    assert "table" in r.blocks
    # 表格线：3 条横线(表头上下+分隔)以上 + 竖线
    lines = [s for s in r.strokes if len(s.points) == 2]
    assert len(lines) >= 4


def test_markdown_table_cell_text_present(fonts):
    src = "| Name | Qty |\n|:--|--:|\n| apple | 3 |\n"
    r = render_markdown(src, fonts, MarkdownStyle(size=5))
    box = r.bbox()
    # 有文字（笔画数应明显多于纯表格线）
    assert len(r.strokes) > 6
    assert box.width > 0 and box.height > 0


def test_markdown_list_and_quote(fonts):
    src = "- one\n- two\n\n> quoted text\n"
    r = render_markdown(src, fonts, MarkdownStyle(size=5))
    assert "ul" in r.blocks
    assert "quote" in r.blocks


def test_markdown_hr(fonts):
    r = render_markdown("a\n\n---\n\nb", fonts, MarkdownStyle(size=5))
    assert "hr" in r.blocks


def test_markdown_wrap_respects_width(fonts):
    long = "word " * 40
    r = render_markdown(long, fonts, MarkdownStyle(size=5, wrap_width=40))
    assert r.width <= 45  # 允许少量超差


def test_markdown_table_width_shrinks_and_wraps(fonts):
    """设了表格宽度：总宽贴合该值，过宽单元格折行（行高增加）。"""
    src = ("| A | B |\n|:--|:--|\n"
           "| aaaa bbbb cccc dddd | ee ff gg |\n| x | y |\n")
    auto = render_markdown(src, fonts, MarkdownStyle(size=5, wrap_width=200))
    narrow = render_markdown(src, fonts,
                             MarkdownStyle(size=5, wrap_width=200,
                                           table_width=25.0))
    assert auto.tables and narrow.tables
    assert auto.tables[0][2] > 25.0              # 自然宽（不受限时按内容）
    assert narrow.tables[0][2] == pytest.approx(25.0, abs=0.5)
    # 折行使表格变高
    auto_h = auto.tables[0][1] - auto.tables[0][3]
    nar_h = narrow.tables[0][1] - narrow.tables[0][3]
    assert nar_h > auto_h


def test_markdown_table_width_never_negative_cells(fonts):
    """表格宽度过小：列不小于一个字符宽，文字不越出表格右缘过多。"""
    src = "| 名称 | 说明 |\n|:--|:--|\n| 项目 | 一段比较长的说明文字 |\n"
    # 显式关掉线端随机：本测试只关心列宽塌缩下限（出头会故意越框）
    r = render_markdown(src, fonts,
                        MarkdownStyle(size=5, wrap_width=200, table_width=1.0,
                                      table_end_jitter=0.0,
                                      table_overshoot=0.0))
    assert r.tables
    # 列宽有下限，表格不会塌缩成 0；且所有单元格文字仍在表格左缘之后
    assert r.tables[0][2] > 0
    assert min(p[0] for s in r.strokes for p in s.points) >= -1e-6


def test_markdown_table_auto_respects_wrap_width(fonts):
    """未设表格宽度：表格自然宽不超过换行宽度（与文字列宽一致）。"""
    src = ("| c1 | c2 | c3 |\n|:--|:--|:--|\n"
           "| aaaaaaaaaa | bbbbbbbbbb | cccccccccc |\n")
    r = render_markdown(src, fonts, MarkdownStyle(size=5, wrap_width=30.0))
    assert r.tables and r.tables[0][2] <= 30.0 + 1e-6


# ============================================================ 表格随机化（P41）
_MD_TABLE = ("| ab | cd | ef |\n|:--|:--|:--|\n"
             "| gh | ij | kl |\n| mn | op | qr |\n")


def _hlines(result):
    """表格横线（2 点、两端 y 相近、非零长度）。"""
    return [s.points for s in result.strokes
            if len(s.points) == 2
            and abs(s.points[0][1] - s.points[1][1]) < 0.9
            and s.points[0][0] != s.points[1][0]]


def test_table_jitter_deterministic_and_seedable(fonts):
    """同种子重排（拖宽度）结果不跳变；换种子换一套抖动。"""
    a = render_markdown(_MD_TABLE, fonts, MarkdownStyle(size=5))
    b = render_markdown(_MD_TABLE, fonts, MarkdownStyle(size=5))
    assert [s.points for s in a.strokes] == [s.points for s in b.strokes]
    c = render_markdown(_MD_TABLE, fonts,
                        MarkdownStyle(size=5, table_seed=12345))
    assert [s.points for s in a.strokes] != [s.points for s in c.strokes]


def test_table_zero_jitter_is_exact_grid(fonts):
    """线端偏移/出头/波浪全 0：横线端点精确落在 0 与总宽（完全规整网格）。"""
    st = MarkdownStyle(size=5, table_end_jitter=0.0, table_overshoot=0.0)
    r = render_markdown(_MD_TABLE, fonts, st)
    hs = _hlines(r)
    total = max(x for pts in hs for x, _ in pts)
    for pts in hs:
        assert abs(pts[0][0]) < 1e-9 and abs(pts[0][1] - pts[1][1]) < 1e-9
        assert abs(pts[1][0] - total) < 1e-9


def test_table_end_jitter_offsets_and_tilts_lines(fonts):
    """默认线端随机（0.5/0.6）：端点离开边界、越框出头、线条微倾。"""
    r = render_markdown(_MD_TABLE, fonts, MarkdownStyle(size=5))
    hs = _hlines(r)
    total = max(x for pts in hs for x, _ in pts)
    # 有端点既不在左缘也不在右缘附近（轴向偏移生效）
    assert any(abs(x) > 0.05 and abs(x - total) > 0.05
               for pts in hs for x, _ in pts)
    # 有端点越过外框（出头生效）
    assert any(x < -1e-6 or x > total + 1e-6 for pts in hs for x, _ in pts)
    # 微倾：存在两端 y 不同的横线（垂直分量生效）
    assert any(abs(pts[0][1] - pts[1][1]) > 1e-6 for pts in hs)


def test_table_row_jitter_moves_internal_boundaries_only(fonts):
    """行界抖动：内部行界移动、行高不再一致；顶/底界不动、保持单调。"""
    base = MarkdownStyle(size=5, table_end_jitter=0.0, table_overshoot=0.0)
    ys0 = sorted({round(p[0][1], 4) for p in _hlines(
        render_markdown(_MD_TABLE, fonts, base))})
    r1 = render_markdown(_MD_TABLE, fonts,
                         MarkdownStyle(size=5, table_end_jitter=0.0,
                                       table_overshoot=0.0,
                                       table_row_jitter=1.0))
    ys1 = sorted({round(p[0][1], 4) for p in _hlines(r1)})
    assert ys0 != ys1
    assert abs(ys0[0] - ys1[0]) < 1e-6 and abs(ys0[-1] - ys1[-1]) < 1e-6
    assert all(b - a > 0 for a, b in zip(ys1, ys1[1:]))     # 行界不交叉


def test_table_col_jitter_keeps_text_inside_column(fonts):
    """列界抖动后单元格文字不越过任何列线（折行走抖动后的列宽）。

    用宽松列宽 + 温和抖动，不触发「最小列宽」下限——触发下限时单行
    文字本就放不下（见 test_markdown_table_width_never_negative_cells），
    属于塌缩场景而非抖动场景。
    """
    src = ("| aaaa bbbb | cccc dddd | eeee ffff |\n|:--|:--|:--|\n"
           "| gg hh | ii jj | kk ll |\n")
    st = MarkdownStyle(size=5, wrap_width=400.0,
                       table_end_jitter=0.0, table_overshoot=0.0,
                       table_col_jitter=0.4)
    r = render_markdown(src, fonts, st)
    vlines = [s.points[0][0] for s in r.strokes
              if s.role != "glyph" and len(s.points) == 2
              and abs(s.points[0][0] - s.points[1][0]) < 1e-9
              and abs(s.points[0][1] - s.points[1][1]) > 0.5]
    assert vlines
    for s in r.strokes:
        if s.role != "glyph":
            continue
        gx0 = min(p[0] for p in s.points)
        gx1 = max(p[0] for p in s.points)
        for x in vlines:
            assert not (gx0 + 0.05 < x < gx1 - 0.05), \
                f"文字 [{gx0:.2f},{gx1:.2f}] 越过列线 x={x:.2f}"


def test_markdown_inline_strip_marks(fonts):
    r = render_markdown("**bold** and `code` and [link](http://x)",
                        fonts, MarkdownStyle(size=5))
    assert r.strokes  # 未崩溃且产出笔画


# ============================================================ 公式
@pytest.mark.skipif(not mathtext_available(), reason="matplotlib 不可用")
def test_wrap_math():
    assert wrap_math("x^2") == "$x^2$"
    assert wrap_math("$x^2$") == "$x^2$"
    assert wrap_math("$$x^2$$") == "$x^2$"


@pytest.mark.skipif(not mathtext_available(), reason="matplotlib 不可用")
def test_equation_renders_paths():
    strokes = render_equation(r"\frac{a}{b}", size_mm=6)
    assert strokes
    b = equation_bbox(r"\frac{a}{b}", 6)
    assert b.width > 0 and b.height > 0


@pytest.mark.skipif(not mathtext_available(), reason="matplotlib 不可用")
def test_equation_size_scales():
    b1 = equation_bbox("x", 4)
    b2 = equation_bbox("x", 8)
    assert b2.width == pytest.approx(b1.width * 2, rel=0.15)
    assert b2.height == pytest.approx(b1.height * 2, rel=0.15)


@pytest.mark.skipif(not mathtext_available(), reason="matplotlib 不可用")
def test_equation_left_aligned_at_zero():
    strokes = render_equation("x+y", 6)
    xs = [p[0] for s in strokes for p in s.points]
    assert min(xs) == pytest.approx(0.0, abs=1e-6)


@pytest.mark.skipif(not mathtext_available(), reason="matplotlib 不可用")
def test_equation_above_and_below_baseline():
    # 分式应有上下结构（y 跨越基线两侧）
    b = equation_bbox(r"\frac{1}{2}", 8)
    assert b.y0 < 0 < b.y1


def _flat_outline_strokes(strokes):
    """「扁平矩形轮廓」笔画：≥4 点、扁（短边 ≤ 长边 1/4）、轴对齐。

    旧版把 mathtext 的分数线矩形描成轮廓，画出来是上下两道线加竖边
    （笔尖下叠成一个方框）——修复后不应再存在这种笔画。
    """
    out = []
    for s in strokes:
        pts = s.points
        if len(pts) < 4:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        short = min(w, h)
        if max(w, h) <= 1e-9 or short > 0.25 * max(w, h):
            continue
        tol = 0.3 * short + 1e-9
        if w >= h:
            if all(min(abs(y - min(ys)), abs(y - max(ys))) <= tol
                   for _, y in pts):
                out.append(pts)
        else:
            if all(min(abs(x - min(xs)), abs(x - max(xs))) <= tol
                   for x, _ in pts):
                out.append(pts)
    return out


@pytest.mark.skipif(not mathtext_available(), reason="matplotlib 不可用")
def test_equation_fraction_bar_is_single_line_native(fonts):
    """原生路径：分数线不再是矩形轮廓（方框），而是水平中线单笔。"""
    strokes = render_equation(r"\frac{1}{2}", 8)
    assert strokes
    assert _flat_outline_strokes(strokes) == []
    # 分数线本体存在：一条 2 点水平线，长度与分子/分母宽同量级
    bars = [s for s in strokes
            if len(s.points) == 2
            and abs(s.points[0][1] - s.points[1][1]) < 1e-9
            and abs(s.points[0][0] - s.points[1][0]) > 0.5]
    assert bars, "应存在分数线（水平单线）"


@pytest.mark.skipif(not mathtext_available(), reason="matplotlib 不可用")
def test_equation_fraction_bar_is_single_line_with_fonts(fonts):
    """字体链路径：结构矩形取中线发一条线（1/2 不在链上则保留轮廓，
    但结构线同样不应再是方框）。"""
    from writerstudio.content.equation import _render_with_fonts
    strokes = _render_with_fonts(r"\frac{1}{2}", 8, fonts, 1.0)
    assert strokes
    assert _flat_outline_strokes(strokes) == []
    bars = [s for s in strokes
            if len(s.points) == 2
            and abs(s.points[0][1] - s.points[1][1]) < 1e-9
            and abs(s.points[0][0] - s.points[1][0]) > 0.5]
    assert bars, "应存在分数线（水平单线）"


@pytest.mark.skipif(not mathtext_available(), reason="matplotlib 不可用")
def test_equation_minus_outline_collapsed_with_fonts(fonts):
    """链上画不出的减号（U+2212，链上无 '-' 可映射）走 mathtext 轮廓：
    扁平矩形必须折叠成中线单笔，不能再是方框。"""
    from writerstudio.content.equation import _render_with_fonts
    strokes = _render_with_fonts("1-2", 8, fonts, 1.0)
    assert strokes
    assert _flat_outline_strokes(strokes) == []
    bars = [s for s in strokes
            if len(s.points) == 2
            and abs(s.points[0][1] - s.points[1][1]) < 1e-9
            and abs(s.points[0][0] - s.points[1][0]) > 0.5]
    assert bars, "减号应是一条水平单线"


@pytest.mark.skipif(not mathtext_available(), reason="matplotlib 不可用")
def test_equation_unicode_minus_falls_back_to_chain_hyphen():
    """链上没有 U+2212 但有 ASCII '-'：数学减号改用字体链画（用户笔迹）。"""
    from writerstudio.content.equation import _render_with_fonts
    from writerstudio.fonts.model import FontFamily, Glyph
    # 链字形故意用「折线」减号（非扁平）：链画出来会有 3 点笔画；
    # 若误走 mathtext 轮廓，减号是扁平矩形（折叠后 2 点）——可区分
    f = FontFamily(name="t", kind="hershey", units_per_em=10.0, glyphs={
        "-": Glyph("-", [[(0, 3), (2, 4.5), (4, 3)]], advance=5)})
    strokes = _render_with_fonts("1-2", 8, [f], 1.0)
    assert strokes
    assert any(len(s.points) == 3 for s in strokes), \
        "减号应由字体链绘制（3 点折线字形）"
    assert _flat_outline_strokes(strokes) == []


# ============================================================ SVG
def _tiny_svg() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="60mm" '
        'viewBox="0 0 100 60">'
        '<rect x="5" y="5" width="40" height="30" fill="none" stroke="#000"/>'
        '<circle cx="70" cy="30" r="15" fill="none" stroke="#000"/>'
        '<path d="M10,50 C30,40 50,60 70,50" fill="none" stroke="#000"/>'
        '</svg>'
    )


@pytest.mark.skipif(not svg_available(), reason="svgelements 不可用")
def test_svg_import_shapes(tmp_path):
    p = tmp_path / "t.svg"
    p.write_text(_tiny_svg())
    r = import_svg(p, target_width_mm=80)
    assert len(r.strokes) >= 3          # rect + circle + path
    assert r.width == pytest.approx(80.0, rel=0.02)
    assert r.height > 0


@pytest.mark.skipif(not svg_available(), reason="svgelements 不可用")
def test_svg_y_flipped_and_origin(tmp_path):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm" '
           'viewBox="0 0 10 10"><path d="M0,0 L10,0" stroke="#000"/></svg>')
    p = tmp_path / "l.svg"
    p.write_text(svg)
    r = import_svg(p)
    # 顶部线 (y=0) 翻转后应在最高点；包围盒左下角归零
    b = r.bbox()
    assert b.x0 == pytest.approx(0.0, abs=1e-6)
    assert b.y0 == pytest.approx(0.0, abs=1e-6)


@pytest.mark.skipif(not svg_available(), reason="svgelements 不可用")
def test_svg_physical_size_without_target(tmp_path):
    """不给目标宽度时：1 英寸 SVG 应导入为 25.4mm（而非 96mm 像素直读）。"""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="1in" height="0.5in" '
           'viewBox="0 0 96 48"><rect x="0" y="0" width="96" height="48" '
           'fill="none" stroke="#000"/></svg>')
    p = tmp_path / "phys.svg"
    p.write_text(svg)
    r = import_svg(p)
    assert r.width == pytest.approx(25.4, rel=0.02)
    assert r.height == pytest.approx(12.7, rel=0.02)


@pytest.mark.skipif(not svg_available(), reason="svgelements 不可用")
def test_svg_wobble_changes_geometry(tmp_path):
    p = tmp_path / "t.svg"
    p.write_text(_tiny_svg())
    import_svg(p, target_width_mm=80)
    o1 = make_svg_object(p, target_width_mm=80)
    o2 = make_svg_object(p, target_width_mm=80, wobble_amplitude=0.5, seed=1)
    assert o1.local_strokes and o2.local_strokes
    assert [s.points for s in o1.local_strokes] != [s.points for s in o2.local_strokes]


# ==================================== 富内容 → 文档对象
def test_make_markdown_object(manager):
    src = "# H\n\nText with $x^2$.\n\n| a | b |\n|:--|--:|\n| 1 | 2 |\n"
    obj = make_markdown_object(src, manager, font_names=["futural"],
                               style=MarkdownStyle(size=4))
    assert obj.source.kind == SOURCE_MARKDOWN
    assert obj.local_strokes
    box = obj.local_bbox()
    # 已归位到原点（左上角 y≈0, x≈0）
    assert box.x0 == pytest.approx(0.0, abs=1e-6)
    assert box.y1 == pytest.approx(0.0, abs=1e-6)


@pytest.mark.skipif(not mathtext_available(), reason="matplotlib 不可用")
def test_make_equation_object():
    obj = make_equation_object(r"E=mc^2", size_mm=6)
    assert obj.source.kind == SOURCE_EQUATION
    assert obj.local_strokes
    assert "E=mc^2" in obj.name


def test_make_markdown_object_with_perturb(manager):
    src = "Hello world 中文字符" * 3
    plain = make_markdown_object(src, manager, font_names=["futural", "STRK-Kaiti"])
    pert = make_markdown_object(src, manager, font_names=["futural", "STRK-Kaiti"],
                                perturb=PerturbParams.natural(4.0))
    assert [s.points for s in plain.local_strokes] != \
           [s.points for s in pert.local_strokes]


def test_regenerate_markdown_keeps_transform(manager):
    src = "# Title\n\nbody"
    obj = make_markdown_object(src, manager, font_names=["futural"])
    obj.transform = AffineTransform.translate(20.0, 30.0)
    assert regenerate_content_object(obj, manager)
    assert obj.transform == AffineTransform.translate(20.0, 30.0)


def test_regenerate_unsupported_kind(manager):
    from writerstudio.core.document import DocumentObject, SourceSpec
    obj = DocumentObject(source=SourceSpec("unknown", {}))
    assert not regenerate_content_object(obj, manager)


# ==================================== LaTeX 工具链探测
def test_toolchain_report_shape():
    from writerstudio.content.latex_doc import toolchain_report
    rep = toolchain_report()
    assert set(rep) == {"engine", "xetex_engine", "converter", "ok", "notes"}
    assert isinstance(rep["notes"], list)


def test_source_needs_xetex_detects_font_syntax():
    """XeTeX 专属字体写法（\\font'...'、fontspec/xeCJK、魔法注释）应被识别。"""
    from writerstudio.content.latex_doc import source_needs_xetex
    assert source_needs_xetex(r'\font\zhA="Noto Sans CJK HK" at 9.2pt')
    assert source_needs_xetex(r"\usepackage{fontspec}")
    assert source_needs_xetex(r"\usepackage{xeCJK}")
    assert source_needs_xetex(r"\setmainfont{DejaVu Sans}")
    assert source_needs_xetex("% !TeX program = xelatex\n\\documentclass{article}")
    # 普通文档不需要 xe 引擎
    assert not source_needs_xetex(r"\draw (0,0)--(1,1);")
    assert not source_needs_xetex(r"\usepackage{tikz}\begin{document}x\end{document}")


def test_find_engine_for_routes_by_source():
    """含 XeTeX 字体语法的源码须路由到 xe/lua 引擎；普通源码不要求。"""
    from writerstudio.content.latex_doc import find_engine_for
    _, need = find_engine_for(r'\font\zhA="Noto Sans CJK HK" at 9pt')
    assert need is True
    _, need2 = find_engine_for(r"\draw (0,0)--(1,1);")
    assert need2 is False


def test_missing_engine_message_actionable():
    from writerstudio.content.latex_doc import missing_engine_message
    m = missing_engine_message(True)
    assert "xelatex" in m or "lualatex" in m
    assert "texlive-fontsrecommended" in m
    assert missing_engine_message(False)


def test_latex_document_graceful_failure_when_unavailable():
    """工具链不可用时应优雅返回 ok=False 而非抛异常。"""
    from writerstudio.content.latex_doc import (
        latex_toolchain_available,
        render_latex_document,
    )
    if latex_toolchain_available():
        pytest.skip("本机工具链可用，跳过失败路径测试")
    r = render_latex_document(r"\section*{T} $x^2$", timeout=20)
    assert r.ok is False
    assert r.log


# ==================================== TikZ
def test_tikz_report_shape():
    from writerstudio.content.tikz import tikz_report
    rep = tikz_report()
    assert set(rep) == {"engine", "converter", "ok", "notes"}
    assert isinstance(rep["notes"], list)


def test_tikz_ensure_picture_wraps_fragment():
    from writerstudio.content.tikz import _ensure_picture
    doc = _ensure_picture(r"\draw (0,0) -- (1,1);")
    assert r"\documentclass" in doc
    assert r"\begin{tikzpicture}" in doc
    assert r"\end{tikzpicture}" in doc
    assert r"\usepackage{tikz}" in doc


def test_tikz_ensure_picture_keeps_full_document():
    from writerstudio.content.tikz import _ensure_picture
    src = r"\documentclass{standalone}\begin{document}x\end{document}"
    assert _ensure_picture(src) == src


def test_tikz_ensure_picture_keeps_existing_environment():
    from writerstudio.content.tikz import _ensure_picture
    src = r"\begin{tikzpicture}\draw (0,0)--(1,1);\end{tikzpicture}"
    doc = _ensure_picture(src)
    # 不应重复包裹
    assert doc.count(r"\begin{tikzpicture}") == 1


def test_tikz_package_optional_pgfplots():
    from writerstudio.content.tikz import (_TIKZ_BODY_STANDALONE,
                                           _TIKZ_BODY_ARTICLE)
    # pgfplots 以条件加载，未安装也不应导致模板编译失败（模板是 .format 串，花括号成对）
    for tpl in (_TIKZ_BODY_STANDALONE, _TIKZ_BODY_ARTICLE):
        assert r"\IfFileExists{{pgfplots.sty}}" in tpl
        assert r"\usepackage{{tikz}}" in tpl


def test_tikz_template_falls_back_without_standalone():
    """缺 standalone.cls 时应退回 article 模板，而不是直接编译失败。"""
    from writerstudio.content.tikz import _tikz_template
    if not __import__("shutil").which("kpsewhich"):
        pytest.skip("无 kpsewhich，无法判断")
    tpl = _tikz_template()
    assert r"\usepackage{{tikz}}" in tpl
    assert r"\documentclass" in tpl


def test_render_tikz_graceful_when_unavailable():
    """缺 TikZ 宏包/工具链时应优雅返回 ok=False 并给出提示。"""
    from writerstudio.content.tikz import render_tikz, tikz_available
    if tikz_available():
        pytest.skip("本机 TikZ 可用，跳过失败路径测试")
    r = render_tikz(r"\draw (0,0) -- (1,1);", timeout=20)
    assert r.ok is False
    assert r.log


def test_make_tikz_object_raises_clear_error_when_unavailable():
    from writerstudio.content.builder import make_tikz_object
    from writerstudio.content.tikz import tikz_available
    if tikz_available():
        pytest.skip("本机 TikZ 可用")
    with pytest.raises(ValueError):
        make_tikz_object(r"\draw (0,0)--(1,1);")


@pytest.mark.skipif(not __import__("writerstudio.content.tikz",
                                    fromlist=["tikz_available"]).tikz_available(),
                    reason="TikZ 工具链不可用")
def test_render_tikz_produces_strokes():
    """TikZ 可用时应能真正编译出笔画。"""
    from writerstudio.content.tikz import render_tikz
    r = render_tikz(r"\draw (0,0) -- (3,0) -- (3,2) -- cycle;",
                    target_width_mm=40.0, timeout=120)
    assert r.ok, r.log
    assert len(r.strokes) >= 1
    assert r.width == pytest.approx(40.0, rel=0.02)
    assert r.height > 0


def test_import_natural_scale_policies(tmp_path):
    """无 target_width 的两条兜底策略（合成 SVG，不依赖 TeX 环境）。

    * TeX 编译产物（TikZ/LaTeX）走历史帧 px×96/72——全部既有文档的对象
      变换与笔画编辑层坐标都按它标定，不得改动（改成 px→mm 会让旧文档
      打开时整体缩到约 1/5.04）；
    * SVG 文件导入按物理尺寸 px→mm（1in = 25.4mm，既有约定）。
    """
    from writerstudio.content.svg_import import _PT_TO_PX, import_svg
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="96px" height="48px" '
           'viewBox="0 0 96 48"><path d="M0 24 H96" fill="none" stroke="#000"/></svg>')
    p = tmp_path / "u.svg"
    p.write_text(svg)
    assert import_svg(p, natural_scale=_PT_TO_PX).width == \
        pytest.approx(96 * 96 / 72)
    assert import_svg(p).width == pytest.approx(25.4, rel=0.02)


@pytest.mark.skipif(not __import__("writerstudio.content.tikz",
                                    fromlist=["tikz_available"]).tikz_available(),
                    reason="TikZ 工具链不可用")
def test_render_tikz_natural_size_smoke():
    """无 target_width 的 TikZ 也能渲染出笔画。

    帧约定（px×96/72）由 test_import_natural_scale_policies 锁定；此处
    不断言绝对尺寸——原始裁剪随各机器 TeX/poppler 版本而异。
    """
    from writerstudio.content.tikz import render_tikz
    r = render_tikz(r"\draw (0,0) -- (2,0);", timeout=120)
    assert r.ok, r.log
    assert r.strokes


def test_bezier_point_matches_svgelements():
    """回归：三次 Bernstein 首项少乘一个 mt（mt²），每个求值点都被推离

    真实曲线——小圆解析成来回折叠的乱线团、曲线弧长成倍膨胀、包围盒
    虚胀导致文字错位。权重和必须恒等于 1，且与 svgelements 原生
    ``seg.point`` 一致。
    """
    from writerstudio.content.svg_import import _bezier_point
    p0, p1, p2, p3 = (0.0, 0.0), (10.0, 30.0), (40.0, -20.0), (50.0, 10.0)
    for t in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0):
        mt = 1.0 - t
        ref = (mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0]
               + 3 * mt * t * t * p2[0] + t ** 3 * p3[0]), \
              (mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1]
               + 3 * mt * t * t * p2[1] + t ** 3 * p3[1])
        assert _bezier_point(t, p0, p1, p2, p3) == pytest.approx(ref, abs=1e-9)
    # 权重和恒为 1：远离原点的曲线也不会整体被推走
    p0 = (10000.0, -20000.0)
    mt = 1.0 - 0.3
    x, y = _bezier_point(0.3, p0, p1, p2, p3)
    assert (x, y) == pytest.approx(
        (mt ** 3 * p0[0] + 3 * mt * mt * 0.3 * p1[0]
         + 3 * mt * 0.09 * p2[0] + 0.027 * p3[0],
         mt ** 3 * p0[1] + 3 * mt * mt * 0.3 * p1[1]
         + 3 * mt * 0.09 * p2[1] + 0.027 * p3[1]), abs=1e-9)


@pytest.mark.skipif(not __import__("writerstudio.content.tikz",
                                    fromlist=["tikz_available"]).tikz_available(),
                    reason="TikZ 工具链不可用")
def test_make_tikz_object_and_regenerate():
    from writerstudio.content.builder import SOURCE_TIKZ, make_tikz_object, \
        regenerate_content_object
    m = FontManager(user_font_dir="/nonexistent-xyz")
    obj = make_tikz_object(r"\draw (0,0) -- (2,2);", target_width_mm=30.0)
    assert obj.source.kind == SOURCE_TIKZ
    assert obj.local_strokes
    len(obj.local_strokes)
    obj.transform = AffineTransform.translate(11.0, 22.0)
    assert regenerate_content_object(obj, m)
    assert obj.transform == AffineTransform.translate(11.0, 22.0)
    assert len(obj.local_strokes) >= 1


def test_perturb_on_all_kinds_changes_strokes_and_is_reversible():
    """文本/矢量/公式对象经统一扰动入口都能改变笔画且可还原。"""
    from writerstudio.core.document import make_static_object
    from writerstudio.core.sample import make_rect
    from writerstudio.perturb.apply import apply_perturb
    m = FontManager(user_font_dir="/nonexistent-xyz")
    objs = [
        make_static_object([make_rect(0, 0, 30, 15)]),
        make_equation_object(r"x^2"),
        make_markdown_object("Hi", m, font_names=["futural"]),
    ]
    p = PerturbParams.vector_hand_drawn(20.0)
    for o in objs:
        base = [s.points for s in o.local_strokes]
        apply_perturb(o, p, m)
        assert [s.points for s in o.local_strokes] != base
        apply_perturb(o, PerturbParams(enabled=False), m)
        assert [s.points for s in o.local_strokes] == base


def test_markdown_char_spacing_changes_advance():
    """Markdown 字距：字符步距均匀增加；末字的多余字距不计入宽度。"""
    from writerstudio.content.markdown import MarkdownStyle, render_markdown
    from writerstudio.fonts.manager import FontManager
    mgr = FontManager()
    fonts = [mgr.get(e.name) for e in mgr.entries() if e.kind == "hershey"]
    fonts = [f for f in fonts if f is not None][:1]
    md = "ab"
    r0 = render_markdown(md, fonts, MarkdownStyle())
    r1 = render_markdown(md, fonts, MarkdownStyle(char_spacing=2.0))
    # 2 个字符只有 1 个「字间间隙」计入总宽（末字的额外字距被排除）
    assert r1.width == pytest.approx(r0.width + 1 * 2.0)


def test_markdown_style_char_spacing_serialization():
    from writerstudio.content.builder import _style_from_data, _style_to_data
    from writerstudio.content.markdown import MarkdownStyle
    st = MarkdownStyle(char_spacing=1.5)
    back = _style_from_data(_style_to_data(st))
    assert back.char_spacing == 1.5


def test_markdown_glyph_and_rule_roles(fonts):
    """Markdown：字形笔画带 glyph 标记，表格线未标记。

    线条起伏（弯折）只作用于未标记的图形线条，文字笔画不弯折。
    """
    r = render_markdown(
        "# 标题 Head\n\n| 甲 A | 乙 B |\n| --- | --- |\n| 1 | 2 |\n",
        fonts, MarkdownStyle(size=5))
    assert any(s.role == "glyph" for s in r.strokes)     # 文字笔画已标记
    rules = [s for s in r.strokes if s.role == ""]
    assert rules                                          # 表格线未标记
    # 未标记笔画应包含长的水平线（表格横线），而非只有字形残余
    assert any(s.bbox().width >= 9.0 for s in rules)


# ==================================== 手工笔画编辑层
def test_stroke_edits_delete_survives_regenerate():
    """删除的笔画经重生成（调扰动/换种子）后仍被删除。"""
    from writerstudio.content.stroke_edits import apply_edits, record_delete
    from writerstudio.perturb.apply import apply_perturb

    obj = make_equation_object(r"a+b=c", 6.0)
    n0 = len(obj.local_strokes)
    record_delete(obj.source.data, 0, n0)
    obj.local_strokes = apply_edits(obj.local_strokes, obj.source.data)
    assert len(obj.local_strokes) == n0 - 1

    apply_perturb(obj, PerturbParams.natural(6.0))
    assert len(obj.local_strokes) == n0 - 1
    p = PerturbParams.natural(6.0)
    p.seed = 424242
    apply_perturb(obj, p)
    assert len(obj.local_strokes) == n0 - 1
    # 清扰动（重渲染）后仍保持删除
    apply_perturb(obj, PerturbParams(enabled=False))
    assert len(obj.local_strokes) == n0 - 1


def test_stroke_edits_modify_survives_regenerate():
    from writerstudio.content.stroke_edits import apply_edits, record_modify
    from writerstudio.core.strokes import Stroke
    from writerstudio.perturb.apply import apply_perturb

    obj = make_equation_object(r"a+b=c", 6.0)
    n0 = len(obj.local_strokes)
    pts = [(x + 3.5, y - 1.25) for x, y in obj.local_strokes[1].points]
    record_modify(obj.source.data, 1, Stroke(pts), n0)
    obj.local_strokes = apply_edits(obj.local_strokes, obj.source.data)
    want = obj.local_strokes[1].points
    apply_perturb(obj, PerturbParams.natural(6.0))
    assert obj.local_strokes[1].points == want


def test_stroke_edits_delete_then_modify_maps_indices():
    """删除后再改动的显示序号 → 原始序号映射正确。"""
    from writerstudio.content.stroke_edits import (
        apply_edits, record_delete, record_modify)
    from writerstudio.core.strokes import Stroke

    data: dict = {}
    record_delete(data, 0, 5)            # 删原始 0
    record_modify(data, 0, Stroke([(9.0, 9.0), (8.0, 8.0)]), 4)  # 显示0=原始1
    raw = [Stroke([(i, 0), (i, 1)]) for i in range(5)]
    out = apply_edits(raw, data)
    assert len(out) == 4
    assert out[0].points == [(9.0, 9.0), (8.0, 8.0)]   # 原始1 被改
    assert out[1].points == raw[2].points              # 原始2 原样


def test_stroke_edits_survive_project_roundtrip():
    """编辑层随项目文件往返（JSON 键字符串化后仍能还原）。"""
    import json
    from writerstudio.content.stroke_edits import (
        apply_edits, has_edits, record_delete, record_modify)
    from writerstudio.core.strokes import Stroke

    data: dict = {}
    record_delete(data, 0, 6)
    record_modify(data, 0, Stroke([(1.0, 2.0), (3.0, 4.0)]), 5)
    back = json.loads(json.dumps(data))
    assert has_edits(back)
    raw = [Stroke([(i, 0), (i, 1)]) for i in range(6)]
    out = apply_edits(raw, back)
    assert len(out) == 5
    assert out[0].points == [(1.0, 2.0), (3.0, 4.0)]


def test_text_stroke_edit_kept_on_font_change_but_not_text_change():
    """文本对象：改字号保留编辑层；改文字则丢弃（下标会错位）。"""
    from writerstudio.content.stroke_edits import apply_edits, record_delete
    from writerstudio.fonts.builder import TextSpec, make_text_object, \
        update_text_object

    m = FontManager(user_font_dir="/nonexistent-xyz")
    spec = TextSpec(text="AB", font_names=["futural"], size=10)
    obj = make_text_object(spec, m)
    n0 = len(obj.local_strokes)
    record_delete(obj.source.data, 0, n0)
    obj.local_strokes = apply_edits(obj.local_strokes, obj.source.data)

    spec2 = TextSpec.from_data(obj.source.data)
    spec2.size = 12.0
    update_text_object(obj, spec2, m)
    from writerstudio.content.stroke_edits import KEY
    assert KEY in obj.source.data          # 字号改动保留编辑层

    spec3 = TextSpec.from_data(obj.source.data)
    spec3.text = "ABC"
    update_text_object(obj, spec3, m)
    assert KEY not in obj.source.data      # 改文字丢弃编辑层


# ==================================== TikZ 文字改用本软件字体
def test_tikz_sanitize_font_name():
    from writerstudio.content.tikz import sanitize_font_name
    assert sanitize_font_name('Noto Sans CJK SC') == "Noto Sans CJK SC"
    assert sanitize_font_name('华文楷体') == "华文楷体"
    assert sanitize_font_name('bad"font{}\\x%') == "badfontx"
    assert sanitize_font_name("") == ""


def test_tikz_has_cjk():
    from writerstudio.content.tikz import has_cjk
    assert has_cjk("实验仪器")
    assert not has_cjk("ABC 123")
    assert has_cjk("mix 中文")


def test_tikz_ensure_picture_injects_measurement_font():
    """测量字体注入导言区，并经 every node 样式让 TeX 真把文字排出来。"""
    from writerstudio.content.tikz import _ensure_picture
    doc = _ensure_picture(r"\draw (0,0)--(1,1);", measurement_font="华文楷体")
    assert r'\font\wsfont="华文楷体"' in doc
    assert "every node" in doc
    # 注入点在 \begin{document} 之前（导言区）
    assert doc.index("every node") < doc.index(r"\begin{document}")
    # 字体命令不能出现在正文里（TikZ node 会重置字体）
    body = doc.split(r"\begin{document}", 1)[1]
    assert r"\font\wsfont" not in body


def test_tikz_ensure_picture_injects_into_full_document():
    from writerstudio.content.tikz import _ensure_picture
    src = r"\documentclass{standalone}\begin{document}\begin{tikzpicture}" \
          r"\node{A};\end{tikzpicture}\end{document}"
    doc = _ensure_picture(src, measurement_font="Noto Sans CJK SC")
    assert r'\font\wsfont="Noto Sans CJK SC"' in doc
    assert "every node" in doc
    assert doc.index("every node") < doc.index(r"\begin{document}")


def test_tikz_no_font_leaves_fragment_unchanged():
    from writerstudio.content.tikz import _ensure_picture
    doc = _ensure_picture(r"\draw (0,0)--(1,1);")
    assert r"\font\wsfont" not in doc
    assert "every node" not in doc


def test_tikz_drop_text_elements():
    """SVG 文字元素（use/text/tspan/defs）应被摘除，图形路径保留。"""
    from writerstudio.content.tikz import _strip_text_from_svg
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" '
           'xmlns:xlink="http://www.w3.org/1999/xlink">'
           '<defs><g id="glyph-0-0"><path d="M0 0 L1 1"/></g></defs>'
           '<path id="graphic" d="M0 0 L2 2"/>'
           '<g><use xlink:href="#glyph-0-0" x="3" y="4"/></g>'
           '<text x="0" y="0">hi</text></svg>')
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "a.svg"); dst = os.path.join(d, "b.svg")
        open(src, "w").write(svg)
        assert _strip_text_from_svg(src, dst)
        out = open(dst).read()
    assert "glyph-0-0" not in out and "<use" not in out and "<text" not in out
    assert 'id="graphic"' in out


@pytest.mark.skipif(not __import__("writerstudio.content.tikz",
                                    fromlist=["tikz_available"]).tikz_available(),
                    reason="TikZ 工具链不可用")
def test_tikz_text_uses_app_font_not_outline(manager):
    """TikZ 节点文字应改用本软件字体重排，而非 TeX 的轮廓字形。

    判据：替换后的中文文字笔画数与「用该字体单独排版同一串字」一致，
    且与未替换（TeX 轮廓）时的笔画数明显不同。
    """
    from writerstudio.content.tikz import default_cjk_font, render_tikz
    if not default_cjk_font():
        pytest.skip("系统无可用 CJK 字体")
    src = (r"\begin{tikzpicture}\draw (0,0) rectangle (3,1.6);"
           r"\node at (1.5,0.8) {实验仪器};\end{tikzpicture}")
    native = render_tikz(src, target_width_mm=60.0, timeout=120)
    assert native.ok, native.log
    appfont = render_tikz(src, target_width_mm=60.0, timeout=120,
                          font_names=["STRK-Kaiti"], manager=manager)
    assert appfont.ok, appfont.log
    # 图形都只有 1 条矩形；差异全在文字笔画上
    assert native.strokes and appfont.strokes
    assert len(appfont.strokes) != len(native.strokes)
    # 尺寸由 TeX 决定，替换后总体尺寸保持
    assert appfont.width == pytest.approx(native.width, rel=0.02)
    assert appfont.height == pytest.approx(native.height, rel=0.02)


# ==================================== TikZ 文字重排（tikz_text）
def test_tikz_text_extract_words_parses_bboxxml():
    """extract_words 解析 pdftotext -bbox 的 XML 并反转义实体。"""
    import subprocess as _sp
    from writerstudio.content import tikz_text
    xml = ('<word xMin="1.5" yMin="2.0" xMax="10.0" yMax="8.0">A&amp;B</word>'
           '<word xMin="20" yMin="2" xMax="30" yMax="8">中文</word>')

    class _P:
        returncode = 0
        stdout = xml

    orig = _sp.run
    _sp.run = lambda *a, **k: _P()
    try:
        ws = tikz_text.extract_words("/nonexistent.pdf")
    finally:
        _sp.run = orig
    assert [w.text for w in ws] == ["A&B", "中文"]
    assert ws[0].x0 == 1.5 and ws[0].width == pytest.approx(8.5)


def test_tikz_place_words_uses_app_font(manager):
    """place_words 在词框位置用字体链排出笔画（尺寸贴合词框）。"""
    from writerstudio.content.svg_import import BBox
    from writerstudio.content.tikz_text import TextWord, place_words
    from writerstudio.content.tikz_text import _PT_TO_PX
    # 词框：pt → SVG 单位；给一个 10px 高的框
    w = TextWord("AB", 0.0, 0.0, 30.0, 10.0)
    box = BBox.from_points([(0.0, 0.0), (100.0, 50.0)])
    strokes = place_words([w], box, 1.0, manager, font_names=["futural"])
    assert strokes
    b = BBox.from_points(p for s in strokes for p in s.points)
    # 归一化后词框 mm 宽 = 30*_PT_TO_PX - 0 = 40px
    assert b.width == pytest.approx(30.0 * _PT_TO_PX, rel=0.02)
    # 无字体时返回空（不抛异常）
    assert place_words([w], box, 1.0, manager, font_names=["不存在字体"]) == []


def test_tikz_place_words_marks_strokes_as_glyph(manager):
    """重排的 TikZ 文字笔画必须带 role="glyph"。

    否则扰动引擎会把节点文字当图形线条施加「线条起伏」，字被扭歪。
    """
    from writerstudio.content.svg_import import BBox
    from writerstudio.content.tikz_text import TextWord, place_words
    w = TextWord("AB", 0.0, 0.0, 30.0, 10.0)
    box = BBox.from_points([(0.0, 0.0), (100.0, 50.0)])
    strokes = place_words([w], box, 1.0, manager, font_names=["futural"])
    assert strokes
    assert all(s.role == "glyph" for s in strokes)


def test_tikz_text_not_distorted_by_wobble(manager):
    """开启「线条起伏」后，TikZ 节点文字笔画形态不变（字形不受起伏影响）。

    回归：此前 place_words 丢掉了 role，文字被当作图形线条做起伏而扭曲。
    """
    from writerstudio.content.svg_import import BBox
    from writerstudio.content.tikz_text import TextWord, place_words
    from writerstudio.perturb.engine import perturb_strokes

    w = TextWord("AB", 0.0, 0.0, 30.0, 10.0)
    box = BBox.from_points([(0.0, 0.0), (100.0, 50.0)])
    text = place_words([w], box, 1.0, manager, font_names=["futural"])
    graphic = [Stroke([(0.0, 0.0), (40.0, 0.0)])]

    params = PerturbParams(enabled=True, seed=7)
    params.line_wobble = 0.8
    params.stroke_x_sigma = params.stroke_y_sigma = 0.0
    params.stroke_theta_sigma = 0.0
    out = perturb_strokes(text + graphic, params)

    # 文字笔画逐点不变；图形长线在起伏后应被画弯（点数变多）
    text_out, graphic_out = out[:len(text)], out[len(text):]
    for a, b in zip(text, text_out):
        assert b.points == pytest.approx(a.points)
    assert len(graphic_out[0].points) > len(graphic[0].points)


@pytest.mark.skipif(not __import__("writerstudio.content.tikz",
                                    fromlist=["tikz_available"]).tikz_available(),
                    reason="TikZ 工具链不可用")
def test_tikz_pure_text_node_renders(manager):
    """纯文字 TikZ（只有 \\node，无图形）也应能渲染（此前会判为空）。"""
    from writerstudio.content.tikz import render_tikz
    r = render_tikz(r"\node at (0,0) {AB};", target_width_mm=40.0,
                    timeout=120, font_names=["futural"], manager=manager)
    assert r.ok, r.log
    assert r.strokes
    assert r.width == pytest.approx(40.0, rel=0.03)


@pytest.mark.skipif(not __import__("writerstudio.content.tikz",
                                    fromlist=["tikz_available"]).tikz_available(),
                    reason="TikZ 工具链不可用")
def test_tikz_appfont_same_overall_size_when_all_chars_drawable(manager):
    """替换文字不改变整张图的尺寸（归一化基准为 图形∪文字框）。

    用汉字（字体链可画）避免缺字：文字里含画不出的字符时会被跳过、整体
    尺寸自然变小，那属于缺字提示的范畴，不在本用例范围。
    """
    from writerstudio.content.tikz import render_tikz
    src = (r"\begin{tikzpicture}\draw[->] (0,0)--(4,0) node[right] {右};"
           r"\node at (2,1.5) {坐标};\end{tikzpicture}")
    n = render_tikz(src, target_width_mm=50.0, timeout=120)
    a = render_tikz(src, target_width_mm=50.0, timeout=120,
                    font_names=["STRK-Kaiti"], manager=manager)
    assert n.ok and a.ok, (n.log, a.log)
    assert a.width == pytest.approx(n.width, rel=0.05)


def test_tikz_reports_missing_chars(manager):
    """字体链画不出的字符应被记入 missing_chars（供界面提示）。"""
    from writerstudio.content.tikz import render_tikz
    src = (r"\begin{tikzpicture}\draw (0,0) rectangle (3,1);"
           r"\node at (1.5,0.5) {AB};\end{tikzpicture}")
    r = render_tikz(src, target_width_mm=40.0, timeout=120,
                    font_names=["不存在字体xyz"], manager=manager)
    # 图形仍在；无可用字体时不留文字笔画、也不报特定缺字
    assert r.ok, r.log
    assert r.missing_chars == []




# ============================================================ 渲染缓存
def test_perturb_change_reuses_render_cache(manager, monkeypatch):
    """只调扰动参数时命中渲染缓存，不再重渲染（TikZ 免重编译）。"""
    from writerstudio.content import builder
    from writerstudio.perturb.apply import apply_perturb

    calls = {"n": 0}
    real = builder.render_tikz

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(builder, "render_tikz", counting)
    src = (r"\begin{tikzpicture}\draw (0,0) -- (4,0) -- (4,3) -- cycle;"
           r"\node at (2,1.5) {A};\end{tikzpicture}")
    obj = builder.make_tikz_object(src, target_width_mm=60.0, manager=manager,
                                   font_names=["futural"])
    assert calls["n"] == 1
    strokes_a = [s.clone() for s in obj.local_strokes]

    p = PerturbParams.natural(10.0, seed=99)
    apply_perturb(obj, p, manager)
    p2 = PerturbParams.natural(10.0, seed=1234)
    apply_perturb(obj, p2, manager)
    assert calls["n"] == 1              # 两次调扰动都命中缓存，零重编译
    assert len(obj.local_strokes) == len(strokes_a)
    # 换种子后笔画确实变了（扰动真实生效，不是把同一份缓存原样返回）
    changed = any(a.points != b.points
                  for a, b in zip(strokes_a, obj.local_strokes))
    assert changed


def test_render_cache_invalidated_on_source_change(manager, monkeypatch):
    """源渲染输入变化（target_width）→ 缓存失效，重渲染并刷新缓存。"""
    from writerstudio.content import builder
    from writerstudio.perturb.apply import apply_perturb

    calls = {"n": 0}
    real = builder.render_tikz

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(builder, "render_tikz", counting)
    src = r"\begin{tikzpicture}\draw (0,0) -- (4,3);\end{tikzpicture}"
    obj = builder.make_tikz_object(src, target_width_mm=60.0, manager=manager)
    assert calls["n"] == 1

    apply_perturb(obj, PerturbParams.natural(8.0, seed=3), manager)
    assert calls["n"] == 1              # 只调扰动 → 命中
    obj.source.data["target_width"] = 90.0
    apply_perturb(obj, PerturbParams.natural(8.0, seed=3), manager)
    assert calls["n"] == 2              # 源变了 → 重渲染
    apply_perturb(obj, PerturbParams.natural(8.0, seed=4), manager)
    assert calls["n"] == 2              # 新缓存已就位


def test_render_cache_invalidated_on_font_version_bump(tmp_path, manager,
                                                       monkeypatch):
    """字体集版本变化（FontManager.version）→ 内容对象缓存失效。"""
    from writerstudio.content import builder
    from writerstudio.perturb.apply import apply_perturb

    calls = {"n": 0}
    real = builder.render_markdown

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(builder, "render_markdown", counting)
    obj = builder.make_markdown_object("# Title\nBody", manager,
                                       font_names=["futural"])
    assert calls["n"] == 1
    apply_perturb(obj, PerturbParams.natural(6.0, seed=1), manager)
    assert calls["n"] == 1
    manager.version += 1                # 模拟导入/删除字体
    apply_perturb(obj, PerturbParams.natural(6.0, seed=1), manager)
    assert calls["n"] == 2


def test_svg_render_cache_invalidated_on_file_change(tmp_path, monkeypatch):
    """SVG 文件磁盘内容变化（mtime/大小）→ 缓存失效重新导入。"""
    import os
    from writerstudio.content import builder
    from writerstudio.perturb.apply import apply_perturb

    calls = {"n": 0}
    real = builder.import_svg

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(builder, "import_svg", counting)
    p = tmp_path / "s.svg"
    p.write_text("<svg xmlns='http://www.w3.org/2000/svg' "
                 "width='50' height='50'><line x1='0' y1='0' "
                 "x2='40' y2='40'/></svg>")
    obj = builder.make_svg_object(str(p), target_width_mm=40.0)
    assert calls["n"] == 1
    apply_perturb(obj, PerturbParams.natural(6.0, seed=1))
    assert calls["n"] == 1              # 命中
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    apply_perturb(obj, PerturbParams.natural(6.0, seed=2))
    assert calls["n"] == 2              # 文件变了 → 重导入


def test_cache_hit_keeps_stroke_edits_applied():
    """缓存命中路径同样要重新施加笔画编辑层（删掉的笔画不得复活）。"""
    from writerstudio.content.builder import regenerate_content_object
    from writerstudio.content.stroke_edits import record_delete
    from writerstudio.perturb.apply import apply_perturb

    obj = make_equation_object(r"a+b=c", 6.0)
    n0 = len(obj.local_strokes)
    record_delete(obj.source.data, 0, n0)
    assert regenerate_content_object(obj, None)
    assert len(obj.local_strokes) == n0 - 1     # 走重建路径：编辑层生效
    # 换种子：这次走缓存命中路径，编辑层仍要生效
    apply_perturb(obj, PerturbParams.natural(6.0, seed=777))
    assert len(obj.local_strokes) == n0 - 1


def test_structural_junction_detection_matches_pairwise():
    """空间哈希的结点判定与逐对扫描版等价（网格/丁字接/孤线）。"""
    from writerstudio.perturb.engine import _is_structural_strokes

    import math

    def _point_near_seg(p, a, b, tol):
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 <= 1e-12:
            return math.hypot(p[0] - ax, p[1] - ay) <= tol
        t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / seg2))
        return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy)) <= tol

    def _join(a, b, tol):
        checks = ((a.points[0], b), (a.points[-1], b),
                  (b.points[0], a), (b.points[-1], a))
        for p, other in checks:
            for u, v in zip(other.points, other.points[1:]):
                if _point_near_seg(p, u, v, tol):
                    return True
        return False

    def pairwise(strokes):
        from writerstudio.perturb.engine import (
            _JUNCTION_TOL_MM, _STRUCTURAL_MIN_MM)
        n = len(strokes)
        flags = [False] * n
        cand = [i for i, s in enumerate(strokes)
                if s.role != "glyph" and len(s.points) >= 2
                and s.length() >= _STRUCTURAL_MIN_MM]
        parent = {i: i for i in cand}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a in range(len(cand)):
            for b in range(a + 1, len(cand)):
                i, j = cand[a], cand[b]
                if _join(strokes[i], strokes[j], _JUNCTION_TOL_MM):
                    ra, rb = find(i), find(j)
                    if ra != rb:
                        parent[ra] = rb
        groups = {}
        for i in cand:
            groups.setdefault(find(i), []).append(i)
        for members in groups.values():
            if len(members) >= 2:
                for i in members:
                    flags[i] = True
        return flags

    import random as _random
    rng = _random.Random(20260913)
    for _trial in range(40):
        strokes = []
        for _ in range(rng.randrange(2, 40)):
            x = rng.randrange(0, 80) * 1.0
            y = rng.randrange(0, 80) * 1.0
            horizontal = rng.random() < 0.5
            length = rng.randrange(0, 20)
            if horizontal:
                strokes.append(Stroke([(x, y), (x + length, y)]))
            else:
                strokes.append(Stroke([(x, y), (x, y + length)]))
        if rng.random() < 0.3:          # 混入字形笔画：不参与结构判定
            strokes.append(Stroke([(1.0, 1.0), (9.0, 1.0)], role="glyph"))
        assert _is_structural_strokes(strokes) == pairwise(strokes)


# ============================================================ 公式字体链
def test_equation_font_chain_replaces_letters(manager):
    """公式带字体链时，链上画得出的字符改用字体链笔画（role=glyph）。"""
    from writerstudio.content.equation import render_equation
    from writerstudio.core.geometry import BBox

    strokes = render_equation("x", 6.0, font_names=["futural"], manager=manager)
    assert strokes
    assert all(s.role == "glyph" for s in strokes)
    # 单字符替换后墨迹贴着基线原点（布局原点 = 字形基线左端）
    b = BBox.from_points(p for s in strokes for p in s.points)
    assert b.x0 == pytest.approx(0.0, abs=1e-6)
    assert 1.0 < b.height < 4.8   # 小写 x 高约 0.4×字号，在基线上方


def test_equation_font_chain_keeps_symbols_and_rules(manager):
    """链画不出的字符（θ ∑）与分式线保留 mathtext 轮廓，语义不变。"""
    from writerstudio.content.equation import render_equation

    strokes = render_equation(r"\frac{a}{\theta}", 6.0,
                              font_names=["futural"], manager=manager)
    glyph = [s for s in strokes if s.role == "glyph"]
    outline = [s for s in strokes if s.role != "glyph"]
    assert glyph and outline            # a 被替换；θ 与分式线保留轮廓
    # 分式线 = 一条水平直线笔画
    bars = [s for s in outline
            if max(p[1] for p in s.points) - min(p[1] for p in s.points) < 1.2
            and max(p[0] for p in s.points) - min(p[0] for p in s.points) > 0.8]
    assert bars


def test_equation_font_chain_fallback_on_bad_chain(manager):
    """字体链全部不可用时退回原生轮廓（与无链行为一致）。"""
    from writerstudio.content.equation import render_equation
    native = render_equation("x", 6.0)
    fallback = render_equation("x", 6.0, font_names=["不存在字体"], manager=manager)
    assert fallback
    assert all(s.role == "" for s in fallback)
    assert len(fallback) == len(native)


def test_equation_object_font_names_roundtrip(manager):
    """公式对象记录字体链；重生成（换扰动种子）后替换仍在。"""
    obj = make_equation_object(r"\frac{a}{b}+x", 6.0,
                               font_names=["futural"], manager=manager)
    assert obj.source.data["font_names"] == ["futural"]
    assert any(s.role == "glyph" for s in obj.local_strokes)

    p = PerturbParams.natural(6.0, seed=3)
    from writerstudio.perturb.apply import apply_perturb
    assert apply_perturb(obj, p, manager)
    # 重生成（缓存命中路径）后字体链替换保持
    assert any(s.role == "glyph" for s in obj.local_strokes)
    # 去掉字体链重渲染 → 全部轮廓
    obj.source.data["font_names"] = []
    assert regenerate_content_object(obj, manager)
    assert all(s.role == "" for s in obj.local_strokes)


def test_table_text_follows_line_position(fonts):
    """单元格文字逐字符贴着所属行底界线段：线的微倾/位移带动文字。

    线的随机效果不受影响——文字跟随只消费已生成的线段端点，不改
    RNG 流；线端随机为 0 时线是直的，文字也回到直基线。
    """
    flat = render_markdown(_MD_TABLE, fonts, MarkdownStyle(
        size=5, table_end_jitter=0.0, table_overshoot=0.0))
    tilt = render_markdown(_MD_TABLE, fonts, MarkdownStyle(
        size=5, table_end_jitter=1.2, table_overshoot=0.0))
    hf = _hlines(flat)
    assert len(hf) >= 3
    nominal = hf[1][0][1]                          # 表头底界的名义 y
    band_top = hf[0][0][1]

    def _hseg(y_near):
        """微倾横线段：按输出顺序取第一条中点接近 y_near 的主线。

        表头底界有两条线（主线 + 加粗回描，独立随机）；文字跟随的
        是先输出的主线。
        """
        for s in tilt.strokes:
            if s.role == "glyph" or len(s.points) != 2:
                continue
            (x0, y0), (x1, y1) = s.points
            if abs(x1 - x0) < 1.0:
                continue
            if abs((y0 + y1) / 2 - y_near) < 2.5:
                return s.points
        return None

    seg = _hseg(nominal)
    assert seg is not None
    (ax, ay), (bx, by) = seg

    ga = [s for s in tilt.strokes if s.role == "glyph"]
    gf = [s for s in flat.strokes if s.role == "glyph"]
    assert len(ga) == len(gf) and ga
    checked = 0
    for sa, sf in zip(ga, gf):
        cy = sum(p[1] for p in sf.points) / len(sf.points)
        if not (min(nominal, band_top) <= cy <= max(nominal, band_top)):
            continue                              # 只查表头行内的字符
        cx = sum(p[0] for p in sf.points) / len(sf.points)
        t = min(max((cx - ax) / (bx - ax), 0.0), 1.0) if abs(bx - ax) > 1e-9 else 0.0
        expect = (ay + (by - ay) * t) - nominal
        got = (sum(p[1] for p in sa.points) / len(sa.points)
               - sum(p[1] for p in sf.points) / len(sf.points))
        assert got == pytest.approx(expect, abs=0.15)
        checked += 1
    assert checked >= 2, "表头行内应有可校验的字符"


