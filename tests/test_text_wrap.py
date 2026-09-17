"""文本框宽度自动换行（frame_width）与 text_index 映射测试。"""

from __future__ import annotations

import pytest

from writerstudio.fonts.builder import TextSpec, render_text
from writerstudio.fonts.hershey import parse_hershey_jhf
from writerstudio.fonts.layout import DIR_V_RL, layout_text, TextStyle
from writerstudio.fonts.model import FontFamily, Glyph

HERSHEY = "writerstudio/fonts/builtin/hershey/futural.jhf"


@pytest.fixture(scope="module")
def latin_font():
    return parse_hershey_jhf(HERSHEY, name="futural")


def _cjk_font() -> FontFamily:
    """合成 CJK 字库：每字 10×10 单位方框墨迹 + 一字宽步距。"""
    def box(ch):
        return Glyph(ch, [[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]],
                     advance=10.0)
    glyphs = {ch: box(ch) for ch in "我们都是追梦人书写美好未来，。"}
    glyphs[" "] = Glyph(" ", [], advance=5.0)
    return FontFamily(name="cjk-test", kind="test", units_per_em=10.0,
                      glyphs=glyphs)


def test_no_wrap_by_default(latin_font):
    spec = TextSpec(text="abc def ghi", font_names=["futural"])
    lay = render_text(spec, [latin_font])
    assert len(lay.lines) == 1


def test_wrap_respects_width_and_breaks_at_words(latin_font):
    text = "the quick brown fox jumps over the lazy dog"
    spec = TextSpec(text=text, font_names=["futural"], frame_width=45.0)
    lay = render_text(spec, [latin_font])
    assert len(lay.lines) > 1
    prev = -1
    for c in lay.chars:
        assert c.text_index > prev
        prev = c.text_index
    for ln in lay.lines:
        right = max(c.origin[0] + c.advance for c in ln.chars)
        assert right <= 45.0 + 1e-6 or len(ln.chars) == 1
        # 断行发生在空格边界：每行不以空格开头
        assert not ln.text.startswith(" ")
        for c in ln.chars:
            assert text[c.text_index] == c.char


def test_wrap_cjk_no_closing_punct_at_line_head():
    fam = _cjk_font()
    text = "我们都是追梦人，书写美好未来。"
    spec = TextSpec(text=text, font_names=["cjk-test"],
                    frame_width=4.0 * 10.0, size=10.0)
    lay = render_text(spec, [fam])
    assert len(lay.lines) > 1
    for ln in lay.lines:
        assert not ln.text.startswith("，")
        assert not ln.text.startswith("。")
        for c in ln.chars:
            assert text[c.text_index] == c.char


def test_wrap_center_alignment_keeps_lines_centered(latin_font):
    spec = TextSpec(text="the quick brown fox jumps over the lazy dog",
                    font_names=["futural"], frame_width=45.0, align="center")
    lay = render_text(spec, [latin_font])
    for ln in lay.lines:
        assert ln.x_start == pytest.approx(-ln.width / 2.0)


def test_text_index_includes_newlines(latin_font):
    text = "ab\ncd"
    spec = TextSpec(text=text, font_names=["futural"])
    lay = render_text(spec, [latin_font])
    assert len(lay.lines) == 2
    assert lay.chars[0].text_index == 0
    assert lay.chars[1].text_index == 1
    assert lay.chars[2].text_index == 3
    assert lay.chars[3].text_index == 4


def test_vertical_layout_carries_text_index():
    fam = _cjk_font()
    text = "我们\n追梦"
    lay = layout_text(text, [fam],
                      TextStyle(size=10.0, direction=DIR_V_RL),
                      origin=(0.0, 0.0))
    assert len(lay.lines) == 2
    for ln in lay.lines:
        for c in ln.chars:
            assert text[c.text_index] == c.char


def test_frame_box_present_for_wrapped_text(latin_font):
    spec = TextSpec(text="the quick brown fox jumps over the lazy dog",
                    font_names=["futural"], frame_width=45.0)
    lay = render_text(spec, [latin_font])
    from writerstudio.fonts.layout import frame_box
    box = frame_box(lay)
    assert box is not None
    x0, y_top, x1, y_bottom = box
    assert x0 == 0.0
    assert x1 == pytest.approx(45.0)
    assert y_top > y_bottom
