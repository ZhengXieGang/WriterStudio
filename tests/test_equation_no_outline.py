"""公式渲染不得产出「空心字」（mathtext 填充轮廓）。

写字的笔只有一根线：字形轮廓描出来是双线空心，在纸上是明显的事故。
这里锁住常见逻辑符号——字体链没有时也必须由几何单线画出。
"""

import pytest

from writerstudio.content import equation as E
from writerstudio.fonts.manager import FontManager


@pytest.fixture(scope="module")
def manager():
    return FontManager()


@pytest.fixture(autouse=True)
def _reset():
    E.reset_hollow_chars()
    yield
    E.reset_hollow_chars()


def _render(manager, latex):
    return E.render_equation(latex, size_mm=5.0, font_names=["futural"],
                             manager=manager)


@pytest.mark.parametrize("latex", [
    r"\neg P",
    r"P\wedge Q",
    r"P\vee Q",
    r"\exists x",
    r"\neg P\wedge Q\vee R",
    r"P\wedge Q\models S\wedge R",
    r"p\rightarrow q\wedge\neg r",
    r"\forall x(F(x)\wedge G(x))",
])
def test_no_hollow_outline(manager, latex):
    strokes = _render(manager, latex)
    assert strokes, latex
    assert E.hollow_chars() == set(), latex


def test_exists_is_single_line(manager):
    strokes = _render(manager, r"\exists x")
    # ∃ 两笔：外框（上/右/下）一笔 + 中间一横一笔，没有闭合轮廓
    assert not any(s.closed for s in strokes)


def test_plain_letters_still_use_outline_without_fonts():
    """没有字体链时字母仍走轮廓（历史行为）；符号已经被几何画法接住。"""
    E.reset_hollow_chars()
    E.render_equation(r"\exists x", size_mm=5.0, fonts=[])
    assert E.hollow_chars() == {"x"}


def test_unknown_glyph_is_recorded(manager):
    """真兜不住的字符必须被记下来（便于定位，而不是悄悄画成空心）。"""
    _render(manager, r"\bigstar")   # ✭，字体链与几何画法都没有
    assert E.hollow_chars() == {"\u2605"}


def test_text_inline_symbol_fallback_draws():
    """正文行内兜底也要真画出笔画：曾因几何画法没接住而只留空白。"""
    from writerstudio.fonts.layout import TextStyle, layout_text

    fm = FontManager()
    for text in ("∃x", "P∧Q∨¬R", "a⊨b"):
        lay = layout_text(text, [fm.get("futural")], TextStyle(size=5.0),
                          origin=(0.0, 0.0))
        assert lay.missing == [], text
        assert lay.strokes(), text
