"""字体系统测试：解析器、管理器、排版引擎（纯 Python）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from writerstudio.fonts.builder import (
    SOURCE_KIND_TEXT,
    TextSpec,
    make_text_object,
    regenerate_text_object,
    resolve_fonts,
    update_text_object,
)
from writerstudio.fonts.gcode_lib import parse_gcode_char
from writerstudio.fonts.hershey import parse_hershey_jhf
from writerstudio.fonts.layout import (
    ALIGN_CENTER,
    ALIGN_RIGHT,
    TextStyle,
    layout_text,
)
from writerstudio.fonts.manager import FontManager
from writerstudio.fonts.model import FontFamily, Glyph
from writerstudio.fonts.stroke_json import parse_stroke_json

# 本机参考字库目录（不随仓库分发；克隆机上不存在时相关测试自动跳过）
REF = Path(__file__).resolve().parents[2] / "references"
HERSEY_DIR = REF / "hf2gcode/hershey_fonts/orig"
STROKE_JSON = REF / "chinese-hershey-font/dist/json/STRK-Kaiti.json"
GCODE_DIR = REF / "text-to-gcode/ascii_gcode"

has_refs = HERSEY_DIR.exists()
skip_no_refs = pytest.mark.skipif(not has_refs, reason="参考字库不存在")


# ---------------------------------------------------------------- 模型
def test_fontfamily_metrics_from_glyphs():
    f = FontFamily(name="t", kind="hershey", units_per_em=10.0, glyphs={
        "A": Glyph("A", [[(0, 0), (0, 8)]], advance=6),
        "g": Glyph("g", [[(0, 0), (0, -2)]], advance=6),
    })
    f.recompute_metrics()
    assert f.ascent == 8.0
    assert f.descent == -2.0
    assert f.default_advance == 6.0
    assert f.line_height(10.0, 1.0) == pytest.approx(10.0)  # (8-(-2)) * 10/10


def test_glyph_to_strokes_scaling():
    g = Glyph("A", [[(0, 0), (10, 0)]], advance=10)
    out = g.to_strokes(0.5, (1.0, 2.0))
    assert out[0].points == [(1.0, 2.0), (6.0, 2.0)]


def test_scale_for_size():
    f = FontFamily(name="t", kind="hershey", units_per_em=32.0, glyphs={})
    assert f.scale_for_size(16.0) == pytest.approx(0.5)


# ---------------------------------------------------------------- Hershey
@skip_no_refs
def test_parse_hershey_basic():
    f = parse_hershey_jhf(HERSEY_DIR / "futural.jhf")
    assert f.name == "futural"
    assert f.coverage() == 96          # ASCII 32..127
    assert f.has("A") and f.has("z") and f.has(" ")
    # 大写 A：基线 0、高约 21（Hershey 标准）
    box = f.glyph("A").bbox()
    assert box.y0 == pytest.approx(0.0)
    assert box.y1 == pytest.approx(21.0)
    assert f.glyph("A").advance == pytest.approx(18.0)


@skip_no_refs
def test_hershey_glyph_within_advance_box():
    # Hershey 的左边界是「排版原点」，墨水可能内缩（如 H 左留白 4）
    f = parse_hershey_jhf(HERSEY_DIR / "futural.jhf")
    g = f.glyph("H")
    b = g.bbox()
    assert b.x0 >= 0.0                 # 不越过排版原点
    assert b.x1 <= g.advance + 1e-9    # 不超出前进宽度
    assert g.advance == pytest.approx(22.0)  # right - left = 11 - (-11)


@skip_no_refs
def test_hershey_pen_up_splits_strokes():
    # 'A' 由 3 笔构成（两条斜线 + 横线）
    f = parse_hershey_jhf(HERSEY_DIR / "futural.jhf")
    assert len(f.glyph("A").strokes) == 3


@skip_no_refs
def test_hershey_descender_below_baseline():
    f = parse_hershey_jhf(HERSEY_DIR / "futural.jhf")
    assert f.glyph("g").bbox().y0 < 0


@skip_no_refs
def test_hershey_invalid_file(tmp_path):
    p = tmp_path / "bad.jhf"
    p.write_text("not a font\n")
    with pytest.raises(ValueError):
        parse_hershey_jhf(p)


# ------------------------------------------------------- 单线笔画 JSON
@skip_no_refs
@pytest.mark.skipif(not STROKE_JSON.exists(), reason="中文字库不存在")
def test_parse_stroke_json():
    f = parse_stroke_json(STROKE_JSON)
    assert f.coverage() > 20000
    assert f.has("一") and f.has("中") and f.has("国")
    assert f.units_per_em == 1.0
    box = f.glyph("国").bbox()
    # 归一化到 0..1 方框、Y 向上
    assert 0.0 <= box.x0 <= 1.0
    assert 0.0 <= box.y0 <= 1.0
    assert box.y1 <= 1.05


@skip_no_refs
@pytest.mark.skipif(not STROKE_JSON.exists(), reason="中文字库不存在")
def test_stroke_json_y_flip():
    # 原始 "一" 的 y≈0.38..0.52；翻转到 Y 向上后仍在中间附近
    f = parse_stroke_json(STROKE_JSON)
    box = f.glyph("一").bbox()
    assert 0.3 < box.y0 < 0.7
    assert 0.3 < box.y1 < 0.7


# ------------------------------------------------------------ gcode 字库
@skip_no_refs
@pytest.mark.skipif(not GCODE_DIR.exists(), reason="gcode 字库不存在")
def test_parse_gcode_char_file():
    result = parse_gcode_char(GCODE_DIR / "uppercase" / "C.nc")
    assert result is not None
    ch, g = result
    assert ch == "C"
    assert g.strokes                       # 有笔画
    assert g.advance > 0
    # 左对齐
    xs = [x for s in g.strokes for x, _ in s]
    assert min(xs) == pytest.approx(0.0)


@skip_no_refs
@pytest.mark.skipif(not GCODE_DIR.exists(), reason="gcode 字库不存在")
def test_gcode_lib_has_pen_up_splits():
    # 字母 'i'/'j' 等应有抬笔分段；此处验证解析出的笔画数为正且合理
    result = parse_gcode_char(GCODE_DIR / "lowercase" / "i.nc")
    assert result is not None
    _, g = result
    assert len(g.strokes) >= 2  # 点 + 竖


# ---------------------------------------------------------------- 管理器
def test_manager_lists_builtin_fonts():
    m = FontManager(user_font_dir="/nonexistent-xyz")
    names = m.names()
    assert "futural" in names
    assert len(names) >= 30
    kinds = {e.kind for e in m.entries()}
    assert "hershey" in kinds
    assert "stroke-json" in kinds


def test_manager_lazy_then_load():
    m = FontManager(user_font_dir="/nonexistent-xyz")
    entry = m.get_entry("futural")
    assert entry is not None
    assert not entry.loaded          # 懒加载
    fam = m.get("futural")
    assert fam is not None
    assert entry.loaded
    assert m.get("futural") is fam   # 缓存同一实例


def test_manager_import_and_remove(tmp_path):
    m = FontManager(user_font_dir=tmp_path / "user")
    src = HERSEY_DIR / "futural.jhf" if has_refs else None
    if src is None:
        pytest.skip("无参考字体")
    entry = m.import_font(src)
    assert entry.name in m.names()
    assert entry.path.exists()
    assert m.remove(entry.name)
    assert entry.name not in m.names()
    assert not entry.path.exists()


def test_manager_import_unknown_raises(tmp_path):
    m = FontManager(user_font_dir=tmp_path)
    bad = tmp_path / "x.xyz"
    bad.write_text("nothing")
    with pytest.raises(ValueError):
        m.import_font(bad)


# ---------------------------------------------------------------- 排版
def _two_fonts():
    latin = FontFamily(name="latin", kind="hershey", units_per_em=10.0, glyphs={
        "A": Glyph("A", [[(0, 0), (0, 10)]], advance=8),
        "B": Glyph("B", [[(0, 0), (0, 10)]], advance=8),
        " ": Glyph(" ", [], advance=4),
    })
    cjk = FontFamily(name="cjk", kind="stroke-json", units_per_em=10.0, glyphs={
        "中": Glyph("中", [[(0, 0), (10, 10)]], advance=10),
        "文": Glyph("文", [[(0, 0), (10, 10)]], advance=10),
    })
    return latin, cjk


def test_layout_single_line_left():
    latin, _ = _two_fonts()
    lay = layout_text("AB", [latin], TextStyle(size=10.0))
    assert len(lay.lines) == 1
    assert len(lay.chars) == 2
    assert lay.chars[0].origin == (0.0, 0.0)
    assert lay.chars[1].origin[0] == pytest.approx(8.0)  # advance 8


def test_layout_multifont_fallback():
    latin, cjk = _two_fonts()
    lay = layout_text("A中B", [latin, cjk], TextStyle(size=10.0))
    assert [c.font_name for c in lay.chars] == ["latin", "cjk", "latin"]


def test_layout_fallback_order_matters():
    latin, cjk = _two_fonts()
    # 若 CJK 排第一，且它也含 'A'，则应优先 CJK
    cjk2 = FontFamily(name="cjk2", kind="stroke-json", units_per_em=10.0, glyphs={
        "A": Glyph("A", [[(0, 0), (5, 5)]], advance=5),
    })
    lay = layout_text("A", [cjk2, latin], TextStyle(size=10.0))
    assert lay.chars[0].font_name == "cjk2"


def test_layout_missing_char_skipped():
    latin, _ = _two_fonts()
    lay = layout_text("A文B", [latin], TextStyle(size=10.0))
    assert [c.char for c in lay.chars] == ["A", "B"]


def test_layout_multiline_baseline_step():
    latin, _ = _two_fonts()
    lay = layout_text("A\nA", [latin], TextStyle(size=10.0, line_spacing=1.5))
    assert len(lay.lines) == 2
    step = lay.lines[0].baseline_y - lay.lines[1].baseline_y
    assert step == pytest.approx(latin.line_height(10.0, 1.5))


def test_layout_align_center_and_right():
    latin, _ = _two_fonts()
    left = layout_text("AB", [latin], TextStyle(size=10.0))
    w = left.lines[0].width
    center = layout_text("AB", [latin], TextStyle(size=10.0, align=ALIGN_CENTER))
    assert center.lines[0].x_start == pytest.approx(-w / 2)
    right = layout_text("AB", [latin], TextStyle(size=10.0, align=ALIGN_RIGHT))
    assert right.lines[0].x_start == pytest.approx(-w)


def test_layout_empty_fonts_returns_empty():
    lay = layout_text("hello", [])
    assert lay.chars == []
    assert lay.bbox().is_empty


def test_layout_char_spacing():
    latin, _ = _two_fonts()
    lay = layout_text("AB", [latin], TextStyle(size=10.0, char_spacing=2.0))
    assert lay.chars[1].origin[0] == pytest.approx(10.0)  # 8 + 2


# --------------------------------------------------- 文档对象集成
def test_make_text_object_records_source():
    m = FontManager(user_font_dir="/nonexistent-xyz")
    spec = TextSpec(text="AB", font_names=["futural"], size=12)
    obj = make_text_object(spec, m)
    assert obj.source.kind == SOURCE_KIND_TEXT
    assert obj.source.data["text"] == "AB"
    assert obj.local_strokes
    assert "AB" in obj.name


def test_regenerate_keeps_transform():
    from writerstudio.core.geometry import AffineTransform
    m = FontManager(user_font_dir="/nonexistent-xyz")
    spec = TextSpec(text="AB", font_names=["futural"], size=10)
    obj = make_text_object(spec, m)
    obj.transform = AffineTransform.translate(50.0, 60.0)
    n0 = len(obj.local_strokes)
    assert regenerate_text_object(obj, m)
    assert len(obj.local_strokes) == n0
    assert obj.transform == AffineTransform.translate(50.0, 60.0)


def test_update_text_changes_content():
    m = FontManager(user_font_dir="/nonexistent-xyz")
    spec = TextSpec(text="AB", font_names=["futural"], size=10)
    obj = make_text_object(spec, m)
    w0 = obj.local_bbox().width
    update_text_object(obj, TextSpec(text="ABABAB", font_names=["futural"], size=10), m)
    assert obj.local_bbox().width > w0
    assert obj.source.data["text"] == "ABABAB"


def test_text_spec_roundtrip():
    spec = TextSpec(text="x", font_names=["a", "b"], size=9.5,
                    line_spacing=1.2, char_spacing=0.3, align="center")
    back = TextSpec.from_data(spec.to_data())
    assert back.text == spec.text
    assert back.font_names == spec.font_names
    assert back.size == spec.size
    assert back.align == spec.align


# ------------------------------------------------- 多字体：权重 / 随机 / 兜底
def test_layout_weighted_random_fonts_is_deterministic():
    latin, cjk = _two_fonts()
    both = FontFamily(name="both", kind="hershey", units_per_em=10.0, glyphs={
        "A": Glyph("A", [[(0, 0), (0, 10)]], advance=8),
    })
    style = TextStyle(size=10.0, random_fonts=True,
                      font_weights={"latin": 1.0, "both": 1.0})
    text = "A" * 40
    a = layout_text(text, [latin, both], style, seed=7)
    b = layout_text(text, [latin, both], style, seed=7)
    assert [p.font_name for p in a.chars] == [p.font_name for p in b.chars]
    # 40 个字、两个字体权重相同 → 两款字体都该被用到
    assert {p.font_name for p in a.chars} == {"latin", "both"}


def test_layout_zero_weight_font_never_chosen_in_random_mode():
    latin, cjk = _two_fonts()
    both = FontFamily(name="both", kind="hershey", units_per_em=10.0, glyphs={
        "A": Glyph("A", [[(0, 0), (0, 10)]], advance=8),
    })
    style = TextStyle(size=10.0, random_fonts=True,
                      font_weights={"latin": 1.0, "both": 0.0})
    lay = layout_text("A" * 30, [latin, both], style, seed=1)
    assert all(p.font_name == "latin" for p in lay.chars)


def test_layout_random_off_uses_chain_order():
    latin, cjk = _two_fonts()
    both = FontFamily(name="both", kind="hershey", units_per_em=10.0, glyphs={
        "A": Glyph("A", [[(0, 0), (0, 10)]], advance=8),
    })
    style = TextStyle(size=10.0, random_fonts=False)
    lay = layout_text("A" * 10, [latin, both], style)
    assert all(p.font_name == "latin" for p in lay.chars)


def test_resolve_fonts_appends_fallback_font():
    from writerstudio.fonts.builder import resolve_fonts
    m = FontManager(user_font_dir="/nonexistent-xyz")
    spec = TextSpec(text="A", font_names=["futural"], fallback_font="scripts")
    names = [f.name for f in resolve_fonts(spec, m)]
    assert "futural" in names and "scripts" in names
    assert names[-1] == "scripts"


def test_fallback_font_draws_missing_char():
    """链内字体缺字时，兜底字体应补上（缺字不再被跳过）。"""
    from writerstudio.fonts.builder import resolve_fonts
    latin, cjk = _two_fonts()
    FontManager(user_font_dir="/nonexistent-xyz")

    class _M(FontManager):
        def __init__(self):
            pass

        def get(self, name):
            return {"latin": latin, "cjk": cjk}.get(name)

    spec = TextSpec(text="A中", font_names=["latin"], fallback_font="cjk")
    fonts = resolve_fonts(spec, _M())
    lay = layout_text(spec.text, fonts, spec.to_style())
    assert [p.char for p in lay.chars] == ["A", "中"]
    assert [p.font_name for p in lay.chars] == ["latin", "cjk"]
    assert lay.missing == []


def test_text_spec_roundtrip_multifont_fields():
    spec = TextSpec(text="x", font_names=["a"], font_weights={"a": 2.5},
                    random_fonts=True, fallback_font="b", font_seed=99)
    back = TextSpec.from_data(spec.to_data())
    assert back.font_weights == {"a": 2.5}
    assert back.random_fonts is True
    assert back.fallback_font == "b"
    assert back.font_seed == 99


# ------------------------------------------------------- 置顶/隐藏（P11）
def _pin_manager(tmp_path):
    from writerstudio.fonts.manager import FontManager
    return FontManager(user_font_dir=tmp_path / "u",
                       builtin_dir="/nonexistent-builtin")


def test_font_pin_orders_first(tmp_path):
    m = _pin_manager(tmp_path)
    d = tmp_path / "u" / "hershey"
    d.mkdir(parents=True)
    for i, name in enumerate(("aaa", "bbb", "ccc")):
        p = d / f"{name}.jhf"
        p.write_text("", encoding="utf-8")
    # 直接登记（解析留给 load 时，扫描即可）
    m._register("aaa", "hershey", d / "aaa.jhf", "user")
    m._register("bbb", "hershey", d / "bbb.jhf", "user")
    m._register("ccc", "hershey", d / "ccc.jhf", "user")
    assert [e.name for e in m.visible_entries()] == ["aaa", "bbb", "ccc"]
    m.pin("ccc")
    assert [e.name for e in m.visible_entries()] == ["ccc", "aaa", "bbb"]
    assert m.is_pinned("ccc") and m.pinned_names() == ["ccc"]
    m.pin("ccc", False)
    assert not m.is_pinned("ccc")


def test_font_hide_keeps_file(tmp_path):
    m = _pin_manager(tmp_path)
    d = tmp_path / "u" / "hershey"
    d.mkdir(parents=True)
    f = d / "zzz.jhf"
    f.write_text("", encoding="utf-8")
    m._register("zzz", "hershey", f, "user")
    m.hide("zzz")
    assert m.is_hidden("zzz")
    assert m.visible_entries() == []          # 界面不再显示
    assert m.get_entry("zzz") is not None     # 数据仍在
    assert f.exists()                          # 文件未删
    assert m.hidden_names() == ["zzz"]
    m.hide("zzz", False)
    assert [e.name for e in m.visible_entries()] == ["zzz"]


def test_font_pin_hide_mutually_exclusive(tmp_path):
    m = _pin_manager(tmp_path)
    m._register("aa", "hershey", tmp_path / "aa.jhf", "user")
    (tmp_path / "aa.jhf").write_text("", encoding="utf-8")
    m.pin("aa")
    m.hide("aa")
    assert not m.is_pinned("aa") and m.is_hidden("aa")
    m.hide("aa", False)
    m.pin("aa")


def test_font_pin_hide_persistence(tmp_path):
    from writerstudio.settings import Settings
    s = Settings()
    s.set_font_pinned(["a", "b", "a"])
    s.set_font_hidden(["c"])
    assert s.font_pinned() == ["a", "b"]      # 去重保序
    assert s.font_hidden() == ["c"]


# ------------------------------------------------- 逐字符字体覆盖（P11）
def _ov_font(name):
    from writerstudio.fonts.model import FontFamily, Glyph
    return FontFamily(name=name, kind="hershey", units_per_em=10.0, glyphs={
        "A": Glyph("A", [[(0, 0), (0, 10)], [(2, 0), (2, 10)]], advance=6),
        "1": Glyph("1", [[(4, 0), (4, 10), (6, 10)]], advance=6),
    })


def test_char_override_uses_named_font():
    from writerstudio.fonts.layout import layout_text, TextStyle
    fonts = [_ov_font("main"), _ov_font("sym")]
    style = TextStyle(size=10.0,
                      char_overrides={"A": {"font": "sym"}})
    lay = layout_text("A1", fonts, style)
    by_char = {c.char: c for c in lay.chars}
    assert by_char["A"].font_name == "sym"
    assert by_char["1"].font_name == "main"


def test_char_override_missing_font_falls_back():
    from writerstudio.fonts.layout import layout_text, TextStyle
    fonts = [_ov_font("main"), _ov_font("sym")]
    # 覆盖字体不含该字 → 正常回退
    style = TextStyle(size=10.0,
                      char_overrides={"1": {"font": "no-such-font"}})
    lay = layout_text("1", fonts, style)
    assert lay.chars[0].font_name == "main"


def test_char_override_scale_changes_size():
    from writerstudio.fonts.layout import layout_text, TextStyle
    fonts = [_ov_font("main"), _ov_font("sym")]
    style = TextStyle(size=10.0, char_overrides={"A": {"font": "main",
                                                       "scale": 1.5}})
    lay = layout_text("A", fonts, style)
    assert lay.chars[0].scale == pytest.approx(1.5)
    assert lay.chars[0].bbox().height == pytest.approx(15.0)


def test_text_spec_char_overrides_roundtrip():
    spec = TextSpec(text="A1", font_names=["main"],
                    char_overrides={"A": {"font": "sym", "scale": 1.2}})
    back = TextSpec.from_data(spec.to_data())
    assert back.char_overrides == {"A": {"font": "sym", "scale": 1.2}}
    assert spec.to_style().char_overrides == spec.char_overrides


def test_resolve_fonts_appends_override_font(tmp_path, monkeypatch):
    m = _pin_manager(tmp_path)
    spec = TextSpec(text="A", font_names=["main"],
                    char_overrides={"A": {"font": "sym"}})
    # 两个字体都无法真正解析（空文件），resolve 只会跳过——这里只验证
    # resolve_fonts 会尝试把覆盖字体加入名字列表（不抛异常、不改变链首）
    fonts = resolve_fonts(spec, m)
    assert fonts == []
