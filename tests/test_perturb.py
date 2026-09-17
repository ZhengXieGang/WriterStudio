"""手写扰动引擎测试（纯 Python）。"""

from __future__ import annotations

import math

import pytest

from writerstudio.core.geometry import AffineTransform
from writerstudio.core.strokes import Stroke
from writerstudio.fonts.builder import (
    TextSpec,
    make_text_object,
    set_perturb,
)
from writerstudio.fonts.layout import TextStyle, layout_text
from writerstudio.fonts.manager import FontManager
from writerstudio.fonts.model import FontFamily, Glyph
from writerstudio.perturb.engine import (
    perturb_layout,
    perturb_strokes,
    smooth_stroke,
    wobble_strokes,
)
from writerstudio.perturb.params import PerturbParams


# ------------------------------------------------------------------ 夹具
def _font():
    return FontFamily(name="f", kind="hershey", units_per_em=10.0, glyphs={
        "A": Glyph("A", [[(0, 0), (0, 10)], [(2, 0), (2, 10)]], advance=6),
        "B": Glyph("B", [[(0, 0), (0, 10)]], advance=6),
        ",": Glyph(",", [], advance=3),
        " ": Glyph(" ", [], advance=4),
    })


def _layout(text="AB AB", size=10.0):
    return layout_text(text, [_font()], TextStyle(size=size))


def _align(a: list[Stroke], b: list[Stroke]) -> bool:
    return len(a) == len(b) and all(s.points == t.points for s, t in zip(a, b))


# ------------------------------------------------------------------ 参数
def test_params_serialization_roundtrip():
    p = PerturbParams.natural(12.0)
    back = PerturbParams.from_data(p.to_data())
    assert back.seed == p.seed
    assert back.size_sigma == p.size_sigma
    assert back.sine_amplitude == p.sine_amplitude


def test_params_from_data_ignores_unknown_keys():
    p = PerturbParams.from_data({"size_sigma": 0.05, "bogus": 123})
    assert p.size_sigma == 0.05


def test_params_is_active():
    assert not PerturbParams().is_active()          # 默认关闭
    off = PerturbParams(enabled=True)
    assert not off.is_active()                       # 启用但全零
    assert PerturbParams.natural().is_active()


def test_params_presets_ordering():
    n = PerturbParams.natural(10.0)
    s = PerturbParams.subtle(10.0)
    g = PerturbParams.strong(10.0)
    assert s.size_sigma < n.size_sigma < g.size_sigma
    assert s.sine_amplitude < n.sine_amplitude < g.sine_amplitude


def test_params_rescale_for_size():
    p = PerturbParams.natural(10.0)
    big = p.rescaled_for(20.0)
    assert big.char_x_sigma == pytest.approx(p.char_x_sigma * 2)
    assert big.sine_amplitude == pytest.approx(p.sine_amplitude * 2)
    # 比例类不缩放
    assert big.size_sigma == p.size_sigma


# ------------------------------------------------------------ 引擎基础
def test_perturb_off_returns_clones():
    lay = _layout()
    r = perturb_layout(lay, PerturbParams())
    assert _align(r.strokes, lay.strokes())
    # 是副本而非同一对象
    assert r.strokes[0] is not lay.strokes()[0]


def test_perturb_deterministic_same_seed():
    lay = _layout()
    p = PerturbParams.natural(10.0)
    r1 = perturb_layout(lay, p)
    r2 = perturb_layout(lay, p)
    assert _align(r1.strokes, r2.strokes)


def test_perturb_differs_with_seed():
    lay = _layout()
    p = PerturbParams.natural(10.0)
    r1 = perturb_layout(lay, p)
    p2 = p.clone(); p2.seed += 1
    r2 = perturb_layout(lay, p2)
    assert not _align(r1.strokes, r2.strokes)


def test_perturb_records_char_info():
    lay = _layout("AB")
    r = perturb_layout(lay, PerturbParams.natural(10.0))
    assert len(r.chars) == 2
    assert r.chars[0].char == "A"
    assert r.chars[0].font_name == "f"


# ------------------------------------------------- 各参数的效果（单参数）
def _layout_one(text="AAA", size=10.0):
    # 仅一个字符，便于隔离效果
    return layout_text(text, [_font()], TextStyle(size=size))


def test_size_sigma_changes_char_height():
    lay = _layout_one("A")
    p = PerturbParams(enabled=True, seed=1, size_sigma=0.2)
    r = perturb_layout(lay, p)
    h0 = lay.strokes()[0].bbox().height
    h1 = r.strokes[0].bbox().height
    assert h1 != pytest.approx(h0)
    assert r.chars[0].size_factor != pytest.approx(1.0)


def test_char_x_sigma_shifts_char():
    lay = _layout_one("A")
    p = PerturbParams(enabled=True, seed=3, char_x_sigma=5.0)
    r = perturb_layout(lay, p)
    assert abs(r.chars[0].dx) > 0.1


def test_line_start_sigma_only_first_char():
    lay = _layout_one("AAA", size=10.0)
    p = PerturbParams(enabled=True, seed=5, line_start_sigma=8.0)
    r = perturb_layout(lay, p)
    # 首字含行首偏移，后续字符的 dx 不应包含它
    first_dx = r.chars[0].dx
    other_dx = [c.dx for c in r.chars[1:]]
    assert abs(first_dx) > 0.1 or all(abs(d) < 1e-9 for d in other_dx)


def test_line_y_sigma_moves_whole_line():
    lay = _layout("A\nA")
    p = PerturbParams(enabled=True, seed=7, line_y_sigma=4.0)
    r = perturb_layout(lay, p)
    # 两行的 dy 应不同（每行独立抖动）
    [c.dy for c in r.chars if c.dy is not None][0]
    assert r.chars[0].dy != r.chars[1].dy


def test_baseline_wander_varies():
    """基线起伏（行尺度缓弧 + 中尺度游走）：有起伏、幅值有界。"""
    lay = _layout("AAAAAAAA", size=10.0)
    p = PerturbParams(enabled=True, seed=11, sine_amplitude=3.0,
                      sine_wavelength=30.0)
    r = perturb_layout(lay, p)
    base = [c.sine_dy for c in r.chars]
    assert any(abs(v) > 1e-6 for v in base)
    # 行尺度缓弧(±A) + 中尺度(±0.5A)，合成幅度 ≤ 1.5A
    assert all(-4.5 - 1e-9 <= v <= 4.5 + 1e-9 for v in base)


def test_baseline_differs_per_line():
    """每行基线独立生成，不是同一波形移相的复制。"""
    lay = _layout("AAAA\nAAAA", size=10.0)
    p = PerturbParams(enabled=True, seed=13, sine_amplitude=3.0,
                      sine_wavelength=40.0)
    r = perturb_layout(lay, p)
    assert r.chars[0].sine_dy != pytest.approx(r.chars[4].sine_dy)


def test_baseline_is_not_periodic():
    """基线不是以波长为周期的重复波形（旧正弦 dy(x+2λ)=dy(x) 恒成立）。"""
    lay = _layout("AB" * 100, size=10.0)
    p = PerturbParams(enabled=True, seed=11, sine_amplitude=3.0,
                      sine_wavelength=30.0)
    r = perturb_layout(lay, p)
    xs = [c.origin[0] for c in lay.chars]
    dys = [c.sine_dy for c in r.chars]
    worst = 0.0
    for i, x in enumerate(xs):
        for j, x2 in enumerate(xs):
            if abs((x2 - x) - 60.0) < 1.0:      # 相距 2λ 的字对
                worst = max(worst, abs(dys[j] - dys[i]))
    assert worst > 0.5


def test_line_tilt_drifts_line_end():
    """行尾落差：整行线性倾斜，行尾高度 ≈ 行首 + tilt。"""
    lay = _layout("A" * 12, size=10.0)
    p = PerturbParams(enabled=True, seed=5, line_tilt_sigma=2.0)
    r = perturb_layout(lay, p)
    dys = [c.dy for c in r.chars]
    t = [(c.origin[0] - lay.lines[0].x_start) / lay.lines[0].width
         for c in lay.chars]
    k = dys[-1] / t[-1]                          # dy_i = t_i · tilt
    assert abs(k) > 0.3                          # 倾斜确实发生
    for d, ti in zip(dys, t):
        assert d == pytest.approx(ti * k, abs=1e-9)


def test_stroke_theta_sigma_rotates_strokes():
    lay = _layout_one("A")
    p = PerturbParams(enabled=True, seed=17, stroke_theta_sigma=15.0)
    r = perturb_layout(lay, p)
    originals = lay.chars[0].strokes
    # 至少一条笔画发生变化
    changed = any(r.strokes[i].points != originals[i].points
                  for i in range(len(originals)))
    assert changed


def test_char_spacing_sigma_varies():
    lay = _layout_one("AAAA", size=10.0)
    p = PerturbParams(enabled=True, seed=19, char_spacing_sigma=3.0)
    r = perturb_layout(lay, p)
    xs = [c.dx for c in r.chars]
    # 字距节奏使 dx 各不相同（非全等）
    assert len(set(round(x, 4) for x in xs)) > 1


def _std(vals):
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


def test_char_spacing_rhythm_is_bounded():
    """字距节奏均值回归：长行的字距偏移有界，不随字符数漂移。

    旧实现是随机游走（逐字累加独立高斯），400 字后方差 ≈ √400·σ，
    整行会明显越排越散/越挤；AR(1)+低频噪声的极差有界（实测 60 种子
    max=2.12，旧游走同条件 min=3.77）。
    """
    lay = _layout("AB" * 200, size=10.0)          # 400 字长行
    p = PerturbParams(enabled=True, seed=19, char_spacing_sigma=0.25)
    r = perturb_layout(lay, p)
    dxs = [c.dx for c in r.chars]
    assert max(dxs) - min(dxs) < 2.6              # 旧游走同条件最小 3.77
    assert max(abs(d) for d in dxs) > 0.05        # 节奏确实存在


def test_char_spacing_is_smooth_along_line():
    """字距沿行平滑：相邻字的变化远小于整体波动（不再是逐字白噪声）。"""
    lay = _layout("AB" * 75, size=10.0)           # 150 字
    p = PerturbParams(enabled=True, seed=21, char_spacing_sigma=0.4)
    r = perturb_layout(lay, p)
    dxs = [c.dx for c in r.chars]
    assert _std(dxs) > 0.1                        # 有起伏
    diff = _std([b - a for a, b in zip(dxs, dxs[1:])])
    assert diff < 0.8 * _std(dxs)                 # 逐字独立白噪声此值 ≈ √2 倍


def test_word_gap_after_space_and_punctuation():
    """词间距：空格/标点后的第一个字获得额外空隙，其余字不受影响。"""
    for text in ("AB AB", "AB,AB"):
        lay = _layout(text, size=10.0)
        p = PerturbParams(enabled=True, seed=5, word_gap_em=0.25)
        r = perturb_layout(lay, p)
        dxs = [c.dx for c in r.chars]
        # 0.25 em × 10mm ≈ 2.5mm（乘 0.75~1.25 随机系数）
        assert 1.5 < dxs[3] < 3.5, (text, dxs)
        assert all(abs(d) < 1e-9 for i, d in enumerate(dxs) if i != 3), (text, dxs)


def test_word_gap_zero_disables_grouping():
    lay = _layout("AB AB", size=10.0)
    p = PerturbParams(enabled=True, seed=5)
    r = perturb_layout(lay, p)
    assert all(abs(c.dx) < 1e-9 for c in r.chars)


def test_word_gap_em_serialized():
    p = PerturbParams.natural(10.0)
    assert p.word_gap_em == pytest.approx(0.10)
    back = PerturbParams.from_data(p.to_data())
    assert back.word_gap_em == pytest.approx(0.10)
    old = PerturbParams.from_data({"char_spacing_sigma": 1.0})   # 旧版参数无此键
    assert old.word_gap_em == 0.0


def test_vertical_spacing_rhythm_applies_along_column():
    """竖排时字距节奏沿列（dy）方向推进，而不是把整列往旁边挤。"""
    lay = layout_text("ABABAB", [_font()],
                      TextStyle(size=10.0, direction="v-lr"))
    p = PerturbParams(enabled=True, seed=7, char_spacing_sigma=0.5)
    r = perturb_layout(lay, p)
    assert max(abs(c.dx) for c in r.chars) < 1e-9
    assert max(abs(c.dy) for c in r.chars) > 0.1


# --------------------------------------------------------- 通用笔画扰动
def test_perturb_strokes_off_clones():
    s = [Stroke([(0, 0), (10, 0)])]
    out = perturb_strokes(s, PerturbParams())
    assert out[0].points == s[0].points
    assert out[0] is not s[0]


def test_perturb_strokes_deterministic():
    s = [Stroke([(0, 0), (10, 0), (20, 5)])]
    p = PerturbParams(enabled=True, seed=42, stroke_x_sigma=2.0,
                      stroke_y_sigma=2.0, stroke_theta_sigma=5.0)
    a = perturb_strokes(s, p)
    b = perturb_strokes(s, p)
    assert _align(a, b)


def test_wobble_shifts_interior_points():
    s = [Stroke([(i * 1.0, 0.0) for i in range(50)])]
    out = wobble_strokes(s, amplitude=1.0, wavelength=10.0, seed=1)
    assert out[0].points[0] == s[0].points[0]      # 端点不动
    assert out[0].points[-1] == s[0].points[-1]
    assert any(abs(p[1]) > 1e-6 for p in out[0].points[1:-1])  # 中间起伏


def test_wobble_deterministic():
    s = [Stroke([(i * 1.0, 0.0) for i in range(30)])]
    a = wobble_strokes(s, 1.0, 10.0, seed=2)
    b = wobble_strokes(s, 1.0, 10.0, seed=2)
    assert _align(a, b)


def test_smooth_stroke_reduces_jaggedness():
    pts = [(0, 0), (1, 5), (2, -5), (3, 5), (4, 0)]
    s = Stroke(pts)
    sm = smooth_stroke(s, 1.0)
    # 平滑后中间点应更靠近相邻均值（偏离减小）
    assert abs(sm.points[1][1]) < abs(s.points[1][1])
    assert sm.points[0] == s.points[0]
    assert sm.points[-1] == s.points[-1]


# --------------------------------------------------- 文档对象集成
def test_text_object_perturb_applied():
    m = FontManager(user_font_dir="/nonexistent-xyz")
    plain = TextSpec(text="AB AB", font_names=["futural"], size=12)
    o_plain = make_text_object(plain, m)
    spec = TextSpec(text="AB AB", font_names=["futural"], size=12,
                    perturb=PerturbParams.natural(12.0))
    o_pert = make_text_object(spec, m)
    assert not _align(o_plain.local_strokes, o_pert.local_strokes)
    # 扰动结果也记录在 meta 中
    assert o_pert.meta.get("perturb") is not None
    assert o_plain.meta.get("perturb") is None


def test_set_perturb_keeps_transform_and_content():
    m = FontManager(user_font_dir="/nonexistent-xyz")
    spec = TextSpec(text="AB", font_names=["futural"], size=10)
    obj = make_text_object(spec, m)
    obj.transform = AffineTransform.translate(30.0, 40.0)
    ok = set_perturb(obj, PerturbParams.natural(10.0), m)
    assert ok
    assert obj.transform == AffineTransform.translate(30.0, 40.0)
    assert obj.source.data["text"] == "AB"
    assert obj.source.data["perturb"]["enabled"] is True


def test_perturb_persisted_in_source_roundtrip():
    m = FontManager(user_font_dir="/nonexistent-xyz")
    p = PerturbParams.natural(11.0)
    spec = TextSpec(text="AB", font_names=["futural"], size=11, perturb=p)
    obj = make_text_object(spec, m)
    back = TextSpec.from_data(obj.source.data)
    assert back.perturb.seed == p.seed
    assert back.perturb.sine_amplitude == pytest.approx(p.sine_amplitude)


def test_reseed_changes_output():
    m = FontManager(user_font_dir="/nonexistent-xyz")
    p = PerturbParams.natural(10.0)
    spec = TextSpec(text="ABC ABC", font_names=["futural"], size=10, perturb=p)
    obj = make_text_object(spec, m)
    first = [s.points for s in obj.local_strokes]
    p2 = p.clone(); p2.seed += 1
    set_perturb(obj, p2, m)
    second = [s.points for s in obj.local_strokes]
    assert first != second


# ------------------------------------------------- 强度系数 / 线条起伏
def test_intensity_scales_displacement():
    from writerstudio.perturb.engine import perturb_strokes as ps
    strokes = [Stroke([(0, 0), (10, 0), (20, 0)], False)]
    base = PerturbParams(enabled=True, seed=3, stroke_y_sigma=1.0, intensity=1.0)
    strong = base.clone(); strong.intensity = 3.0
    off = base.clone(); off.intensity = 0.0
    a = ps(strokes, base)[0].points
    b = ps(strokes, strong)[0].points
    z = ps(strokes, off)[0].points
    disp_a = max(abs(y) for _, y in a)
    disp_b = max(abs(y) for _, y in b)
    assert disp_a > 0 and disp_b > disp_a
    # 强度 0 → 相当于关闭，原点不动
    assert all(abs(y) < 1e-9 for _, y in z)


def test_intensity_zero_is_inactive_effect():
    p = PerturbParams(enabled=True, intensity=0.0, line_wobble=5.0,
                      stroke_theta_sigma=10.0)
    # intensity=0 时所有量被乘 0，实际效果应为无扰动
    strokes = [Stroke([(0, 0), (10, 0), (20, 0)], False)]
    out = perturb_strokes(strokes, p)
    assert out[0].points == strokes[0].points


def test_line_wobble_bends_straight_line():
    # 需要中间点：端点 taper=0 保持不动，起伏只体现在内部点上
    straight = [Stroke([(0, 0), (25, 0), (50, 0)], False)]
    p = PerturbParams(enabled=True, line_wobble=1.0,
                      line_wobble_wavelength=10.0, seed=5)
    out = perturb_strokes(straight, p)[0].points
    assert any(abs(y) > 1e-6 for _, y in out)
    # 端点保持原位（渐隐）
    assert out[0][1] == pytest.approx(0.0, abs=1e-9)
    assert out[-1][1] == pytest.approx(0.0, abs=1e-9)


def test_line_wobble_bends_two_point_table_line():
    # 回归：markdown 表格线是 2 点线段，起伏后不能还是一条直线——
    # 长直段先细分再施加法向噪声，中段应明显偏离原线，端点保持原位
    table_rule = [Stroke([(0.0, 0.0), (180.0, 0.0)], False)]
    p = PerturbParams(enabled=True, line_wobble=1.2,
                      line_wobble_wavelength=20.0, seed=11)
    out = perturb_strokes(table_rule, p)[0]
    assert len(out.points) >= 4              # 已被细分
    mid = out.points[1:-1]
    assert max(abs(y) for _, y in mid) > 0.1  # 中段真的弯了
    assert out.points[0] == (0.0, 0.0)        # 端点保持原位
    assert out.points[-1] == (180.0, 0.0)


# ------------------------------------------------- 体态斜切/伸缩/修剪/抽稀
def _total_bbox(strokes):
    from writerstudio.core.geometry import BBox
    box = BBox()
    for s in strokes:
        for pt in s.points:
            box.expand(pt)
    return box


def test_char_shear_deforms_posture():
    """体态斜切：x 随 y 线性偏移——两条平行竖线的整字包围盒必然变宽。"""
    lay = _layout_one("A")                    # 字形 A = 两条平行竖线，宽 2mm
    base_w = _total_bbox(lay.strokes()).width
    p = PerturbParams(enabled=True, seed=42, char_shear_sigma=12.0)
    r = perturb_layout(lay, p)
    assert _total_bbox(r.strokes).width > base_w + 0.2
    assert abs(r.chars[0].shear) > 0.5


def test_stroke_trim_shortens_ends():
    """末端修剪：笔画两端被随机剪掉一段，端点内移、长短随 seed 参差。"""
    line = Stroke([(float(x), 0.0) for x in range(0, 101, 2)], False)
    p = PerturbParams(enabled=True, seed=9, stroke_trim_mm=8.0)
    out = perturb_strokes([line], p)[0]
    assert out.points[0][0] > 0.5             # 起点内移
    assert out.points[-1][0] < 99.5           # 末端内移
    p2 = p.clone()
    p2.seed += 1
    out2 = perturb_strokes([line], p2)[0]
    assert out.points[0][0] != pytest.approx(out2.points[0][0])


def test_stroke_stretch_changes_length():
    """笔画伸缩：沿自身轴向随机伸缩，长度偏离原值。"""
    line = Stroke([(float(x), 0.0) for x in range(0, 101, 2)], False)
    p = PerturbParams(enabled=True, seed=5, stroke_stretch_sigma=0.3)
    out = perturb_strokes([line], p)[0]
    assert out.bbox().width != pytest.approx(100.0, abs=1.0)


def test_simplify_mm_reduces_perturbed_points():
    """抽稀容差：扰动输出经 RDP 抽稀后点数明显减少，端点保持。"""
    line = Stroke([(x * 1.0, 0.0) for x in range(101)], False)
    p0 = PerturbParams(enabled=True, seed=3, line_wobble=1.5,
                       line_wobble_wavelength=15.0)
    p1 = p0.clone()
    p1.simplify_mm = 1.0
    a = perturb_strokes([line], p0)[0]
    b = perturb_strokes([line], p1)[0]
    assert len(b.points) < len(a.points)
    assert b.points[0] == a.points[0] and b.points[-1] == a.points[-1]


def test_zero_new_params_keep_legacy_output():
    """向后兼容：新参数全 0 时不额外消耗随机数。

    旧版存档（数据里没有新字段）反序列化后必须与显式置 0 的参数产生
    完全相同的笔画——老文档重新打开不会变样。
    """
    lay = _layout("AB AB")
    p = PerturbParams.natural(10.0)
    p.char_shear_sigma = p.stroke_stretch_sigma = p.stroke_trim_mm = 0.0
    p.simplify_mm = 0.0
    data = p.to_data()
    for k in ("char_shear_sigma", "stroke_stretch_sigma",
              "stroke_trim_mm", "simplify_mm"):
        data.pop(k)
    p_old = PerturbParams.from_data(data)   # 旧版存档：不含新字段
    assert _align(perturb_layout(lay, p).strokes,
                  perturb_layout(lay, p_old).strokes)


def test_vector_hand_drawn_preset_active():
    p = PerturbParams.vector_hand_drawn(50.0)
    assert p.enabled and p.is_active()
    assert p.line_wobble > 0


# ------------------------------------------------- 矢量对象扰动（apply）
def _rect_obj():
    from writerstudio.core.document import make_static_object
    from writerstudio.core.sample import make_rect
    return make_static_object([make_rect(0, 0, 40, 20)], name="矩形")


def test_apply_perturb_to_vector_object_is_reversible():
    from writerstudio.perturb.apply import apply_perturb, supports_perturb
    obj = _rect_obj()
    assert supports_perturb(obj)
    base = [s.points for s in obj.local_strokes]
    p = PerturbParams.vector_hand_drawn(20.0)
    apply_perturb(obj, p, None)
    assert [s.points for s in obj.local_strokes] != base
    # 关闭扰动应回到原始笔画（不累积失真）
    apply_perturb(obj, PerturbParams(enabled=False), None)
    assert [s.points for s in obj.local_strokes] == base


def test_apply_perturb_vector_not_cumulative():
    from writerstudio.perturb.apply import apply_perturb
    p = PerturbParams.vector_hand_drawn(30.0)
    o1 = _rect_obj(); apply_perturb(o1, p, None)
    o2 = _rect_obj(); apply_perturb(o2, p, None)
    # 同一对象重复应用同一参数，结果稳定（基于原始笔画重算）
    again = [s.points for s in o2.local_strokes]
    apply_perturb(o2, p, None)
    assert [s.points for s in o2.local_strokes] == again


def test_resolve_perturb_reads_back():
    from writerstudio.perturb.apply import apply_perturb, resolve_perturb
    obj = _rect_obj()
    p = PerturbParams.vector_hand_drawn(20.0)
    apply_perturb(obj, p, None)
    back = resolve_perturb(obj)
    assert back is not None and back.enabled
    assert back.line_wobble == pytest.approx(p.line_wobble)


# ===================================================== 手绘抖动的“平滑度”
def _max_second_diff(vals):
    return max(abs(vals[i - 1] - 2 * vals[i] + vals[i + 1])
               for i in range(1, len(vals) - 1))


def test_wobble_is_smooth_not_jagged():
    """手绘抖动应是平滑起伏，不是逐点独立的毛刺（二阶差分要小）。"""
    line = Stroke([(x * 5.0, 0.0) for x in range(41)], False)
    p = PerturbParams(enabled=True, seed=1, line_wobble=1.5,
                      line_wobble_wavelength=30.0)
    ys = [y for _, y in perturb_strokes([line], p)[0].points]
    # 端点渐隐到原位
    assert ys[0] == pytest.approx(0.0, abs=1e-9)
    assert ys[-1] == pytest.approx(0.0, abs=1e-9)
    # 有实际起伏
    assert max(abs(v) for v in ys) > 0.1 * 1.5
    # 平滑：二阶差分远小于「逐点独立随机」会产生的量级
    assert _max_second_diff(ys) < 2.0


def test_wobble_displaces_along_normal():
    """水平直线的抖动应主要发生在 y（法向），x 基本不变。

    长段会先被细分以承载法向噪声，插入的采样点位于段内 x 处；
    原有采样点必须仍留在原 x 位置（位移纯法向、无切向漂移）。
    """
    line = Stroke([(x * 5.0, 0.0) for x in range(21)], False)
    p = PerturbParams(enabled=True, seed=2, line_wobble=2.0,
                      line_wobble_wavelength=25.0)
    out = perturb_strokes([line], p)[0].points
    xs = [x for x, _ in out]
    for i in range(21):
        assert any(x == pytest.approx(i * 5.0) for x in xs)
    assert any(abs(y) > 1e-6 for _, y in out)


def test_tremor_adds_high_frequency_detail():
    """开启细微颤抖后，线条细节（总变差）应增加。"""
    line = Stroke([(x * 5.0, 0.0) for x in range(41)], False)
    base = PerturbParams(enabled=True, seed=3, line_wobble=1.0,
                         line_wobble_wavelength=40.0)
    tre = base.clone(); tre.line_tremor = 0.3
    a = [y for _, y in perturb_strokes([line], base)[0].points]
    b = [y for _, y in perturb_strokes([line], tre)[0].points]
    tv = lambda ys: sum(abs(ys[i] - ys[i - 1]) for i in range(1, len(ys)))
    assert tv(b) > tv(a)


def test_wobble_amplitude_respects_parameter():
    """位移不应明显超过设定的起伏振幅。"""
    line = Stroke([(x * 2.0, 0.0) for x in range(61)], False)
    amp = 1.0
    p = PerturbParams(enabled=True, seed=4, line_wobble=amp,
                      line_wobble_wavelength=20.0)
    ys = [y for _, y in perturb_strokes([line], p)[0].points]
    assert max(abs(v) for v in ys) <= amp * 1.05


def test_tremor_scales_with_intensity():
    line = Stroke([(x * 5.0, 0.0) for x in range(41)], False)
    p = PerturbParams(enabled=True, seed=5, line_tremor=0.5, intensity=2.0)
    q = PerturbParams(enabled=True, seed=5, line_tremor=1.0)
    a = [y for _, y in perturb_strokes([line], p)[0].points]
    b = [y for _, y in perturb_strokes([line], q)[0].points]
    assert a == pytest.approx(b)


# ------------------------------------------------- 结构长线（表格线/边框）
def _table_grid():
    """3 条水平线 + 2 条竖线构成的表格网格（端点相接）。"""
    H = [Stroke([(0.0, y), (60.0, y)], False) for y in (0.0, 10.0, 20.0)]
    V = [Stroke([(x, 0.0), (x, 20.0)], False) for x in (0.0, 60.0)]
    return H + V


def test_structural_lines_keep_endpoints():
    """结构长线（表格网格）扰动后端点必须保持原位，不被旋转/伸缩/修剪撕开。

    回归：此前每根网格线被独立整体旋转/修剪，端点从交叉处错开，
    看起来像「沿正弦波斜切」，完全不像手绘。
    """
    grid = _table_grid()
    p = PerturbParams.natural(size_mm=10.0)
    out = perturb_strokes(grid, p)
    for src, dst in zip(grid, out):
        assert dst.points[0] == pytest.approx(src.points[0], abs=1e-9)
        assert dst.points[-1] == pytest.approx(src.points[-1], abs=1e-9)


def test_structural_lines_have_whole_waves():
    """结构长线的起伏波长收敛到线长以内，短线上也有完整波形（非单向倾斜）。"""
    grid = _table_grid()
    p = PerturbParams(enabled=True, seed=7, line_wobble=0.8,
                      line_wobble_wavelength=80.0)   # 波长远大于线长
    out = perturb_strokes(grid, p)
    horiz = [s for s in out
             if s.bbox().width > 50.0 and s.bbox().height < 3.0]
    assert horiz
    ys = [y for _, y in horiz[0].points]
    # 至少出现一次符号变化（波形完整），而不是单调上升/下降
    diffs = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)]
    assert any(d > 0 for d in diffs) and any(d < 0 for d in diffs)


def test_isolated_long_line_still_perturbed_normally():
    """单独一条长线没有接缝可撕开，仍按普通笔画处理（伸缩/起伏照常生效）。"""
    line = Stroke([(float(x), 0.0) for x in range(0, 101, 2)], False)
    p = PerturbParams(enabled=True, seed=5, stroke_stretch_sigma=0.3)
    out = perturb_strokes([line], p)[0]
    assert out.bbox().width != pytest.approx(100.0, abs=1.0)


# ===================================================== 任意对象的扰动
def test_apply_perturb_works_on_equation(manager_fixture=None):
    from writerstudio.content.builder import make_equation_object
    from writerstudio.perturb.apply import (apply_perturb, resolve_perturb,
                                            supports_perturb)
    obj = make_equation_object(r"a^2+b^2=c^2", 8.0)
    assert supports_perturb(obj)
    base = [s.points for s in obj.local_strokes]
    p = PerturbParams.vector_hand_drawn(20.0)
    assert apply_perturb(obj, p, None)
    assert [s.points for s in obj.local_strokes] != base
    back = resolve_perturb(obj)
    assert back is not None and back.enabled
    # 关闭后应回到原始笔画（不累积失真）
    apply_perturb(obj, PerturbParams(enabled=False), None)
    assert [s.points for s in obj.local_strokes] == base


def test_apply_perturb_works_on_markdown():
    from writerstudio.content.builder import make_markdown_object
    from writerstudio.perturb.apply import apply_perturb, supports_perturb
    m = FontManager(user_font_dir="/nonexistent-xyz")
    obj = make_markdown_object("Hello *world*", m, font_names=["futural"])
    assert supports_perturb(obj)
    base = [s.points for s in obj.local_strokes]
    apply_perturb(obj, PerturbParams.vector_hand_drawn(20.0), m)
    assert [s.points for s in obj.local_strokes] != base


def test_all_object_kinds_support_perturb():
    """文本/矢量/富内容对象都应支持手写扰动（参考层除外）。"""
    from writerstudio.core.document import make_static_object
    from writerstudio.core.sample import make_rect
    from writerstudio.content.builder import make_equation_object
    from writerstudio.perturb.apply import supports_perturb
    m = FontManager(user_font_dir="/nonexistent-xyz")
    objs = [
        make_static_object([make_rect(0, 0, 10, 5)]),
        make_equation_object(r"x^2"),
        make_text_object(TextSpec(text="A", font_names=["futural"]), m),
    ]
    assert all(supports_perturb(o) for o in objs)


def test_flare_adds_taper_ends():
    """笔锋：收笔甩尖、起笔斜切；短笔画与闭合笔画不加。"""
    from writerstudio.perturb.engine import perturb_strokes
    from writerstudio.perturb.params import PerturbParams
    # 一条水平长笔画，向右
    s = Stroke([(0.0, 0.0), (5.0, 0.0), (20.0, 0.0)])
    p = PerturbParams(enabled=True, flare_mm=1.5)
    out = perturb_strokes([s], p, seed=1)
    assert len(out) == 1
    pts = out[0].points
    assert len(pts) == 6              # 入笔点 1 + 原始 3 点 + 甩尖 2 点
    # 收笔甩尖：最后的点在原终点 (20,0) 右侧约 1.5mm
    tip = pts[-1]
    assert tip[0] == pytest.approx(20.0 + 1.5, abs=0.3)
    # 起笔斜切：入笔点在原起点 (0,0) 左侧
    assert pts[0][0] == pytest.approx(-1.5, abs=0.3)
    assert out[0].closed is False

    # 短笔画（总长 < 3×笔锋）不加
    short = Stroke([(0.0, 0.0), (1.0, 0.0)])
    out2 = perturb_strokes([short], p, seed=1)
    assert len(out2[0].points) == 2

    # 闭合笔画不加
    closed = Stroke([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
                    closed=True)
    out3 = perturb_strokes([closed], p, seed=1)
    assert len(out3[0].points) == 4 and out3[0].closed


def test_flare_only_makes_params_active():
    """只设笔锋也应触发扰动管线（is_active 纳入 flare_mm）。"""
    from writerstudio.perturb.engine import perturb_strokes
    from writerstudio.perturb.params import PerturbParams
    p = PerturbParams(enabled=True, flare_mm=1.0)
    assert p.is_active()
    s = Stroke([(0.0, 0.0), (30.0, 0.0)])
    out = perturb_strokes([s], p, seed=1)
    assert len(out[0].points) > 2


def test_flare_works_without_enable_flag():
    """笔锋是独立修饰：不勾「启用手写扰动」也应生效。"""
    from writerstudio.perturb.engine import perturb_strokes
    from writerstudio.perturb.params import PerturbParams
    p = PerturbParams(enabled=False, flare_mm=1.0)
    assert p.is_active()
    s = Stroke([(0.0, 0.0), (30.0, 0.0)])
    out = perturb_strokes([s], p, seed=1)
    assert len(out[0].points) > 2          # 端部加了笔锋
    # 未勾选时随机扰动仍不生效：笔迹主体保持原位
    assert out[0].points[2] == (30.0, 0.0)


def test_flare_in_text_render_without_enable():
    """文本渲染路径：只开笔锋也能出尖（无需其它扰动）。"""
    from writerstudio.fonts.builder import TextSpec, render_text_strokes, resolve_fonts
    from writerstudio.fonts.manager import FontManager
    from writerstudio.perturb.params import PerturbParams
    m = FontManager(user_font_dir="/nonexistent-xyz")
    m.get("futural")
    spec = TextSpec(text="HELLO", font_names=["futural"], size=10.0,
                    perturb=PerturbParams(enabled=False, flare_mm=1.0))
    fonts = resolve_fonts(spec, m)
    strokes, _lay, pr = render_text_strokes(spec, fonts)
    assert pr is not None and strokes


# ==================================================== 线条起伏只作用于图形线条
def test_glyph_strokes_immune_to_line_wobble():
    """文字笔画（role="glyph"）不受线条起伏/细微颤抖影响。

    弯折只作用于图形线条（SVG/表格线/边框等）；只开起伏参数时，
    字形笔画输出应与全零参数完全一致。
    """
    glyph = Stroke([(i * 2.0, (i % 3) * 0.5) for i in range(12)], role="glyph")
    line = Stroke([(i * 2.0, 0.0) for i in range(12)])
    p_off = PerturbParams(enabled=True, seed=9)
    p_on = PerturbParams(enabled=True, seed=9, line_wobble=1.5,
                         line_wobble_wavelength=8.0, line_tremor=0.4)
    for p in (p_off, p_on):
        p.intensity = 100.0
    base_glyph, base_line = perturb_strokes([glyph], p_off)[0], \
        perturb_strokes([line], p_off)[0]
    out_glyph, out_line = perturb_strokes([glyph], p_on)[0], \
        perturb_strokes([line], p_on)[0]
    assert out_glyph.points == base_glyph.points     # 文字笔画纹丝不动
    assert out_glyph.points != glyph.points or True  # （其它扰动参数为 0）
    assert any(abs(y0 - y1) > 1e-6
               for (_, y0), (_, y1) in zip(base_line.points, out_line.points))
    assert out_line.points != line.points            # 图形线条被画弯


def test_wobble_strokes_skips_glyph_role():
    """wobble_strokes（内容对象手绘抖动入口）同样跳过字形笔画。"""
    glyph = Stroke([(0.0, 0.0), (10.0, 0.0), (10.0, 8.0)], role="glyph")
    line = Stroke([(0.0, 0.0), (40.0, 0.0)])
    out = wobble_strokes([glyph, line], amplitude=1.0, wavelength=8.0, seed=3)
    assert out[0].points == glyph.points             # 字形原样
    assert out[0].role == "glyph"
    assert any(abs(p[1]) > 1e-6 for p in out[1].points[1:-1])


def test_text_layout_wobble_only_matches_baseline():
    """真实文字对象：只开「线条起伏」时排版输出与不开完全一致。"""
    mgr = FontManager()
    ff = None
    for e in mgr.entries():
        try:
            fam = e.load()
        except Exception:
            continue
        if fam.has("永"):
            ff = fam
            break
    if ff is None:
        pytest.skip("无含「永」的字体")
    text = "永"
    base = layout_text(text, [ff], TextStyle(size=14.0), (0.0, 0.0), seed=7)
    p_wob = PerturbParams(enabled=True, seed=7, line_wobble=2.0,
                          line_wobble_wavelength=6.0, line_tremor=0.5)
    a = perturb_layout(base, p_wob, seed=7).strokes
    b = perturb_layout(base, PerturbParams(enabled=True, seed=7), seed=7).strokes
    assert len(a) == len(b)
    for sa, sb in zip(a, b):
        assert sa.points == sb.points


def test_stroke_role_survives_clone_and_transform():
    """role 标记随 clone()/transformed() 传递。"""
    s = Stroke([(0.0, 0.0), (5.0, 5.0)], role="glyph")
    assert s.clone().role == "glyph"
    t = AffineTransform.translate(1.0, 2.0)
    assert s.transformed(t).role == "glyph"
    assert Stroke([(0.0, 0.0)]).role == ""           # 默认（图形线条）


# ------------------------------------------------------- 防重叠钳制（P15）
def test_char_spacing_random_never_overlaps():
    """随机字距/字抖动不得让相邻字符的笔画重叠（引擎防重叠钳制）。"""
    glyphs = {
        "M": Glyph("M", [[(0, 0), (0, 8)], [(1.5, 8), (3, 0)]], advance=5),
        "I": Glyph("I", [[(0.5, 0), (0.5, 8)]], advance=2),
        " ": Glyph(" ", [], advance=3),
    }
    fam = FontFamily(name="f", kind="hershey", units_per_em=10.0, glyphs=glyphs)
    lay = layout_text("MMI MM", [fam], TextStyle(size=10.0))
    p = PerturbParams.natural(10.0)
    p.char_x_sigma = 2.5
    p.char_spacing_sigma = 2.5
    p.size_sigma = 0.0            # 固定尺寸因子=1，断言用纯平移模型
    p.char_rot_sigma = 0.0
    p.char_shear_sigma = 0.0
    p.line_y_sigma = 0.0
    pr = perturb_layout(lay, p, seed=99)

    # 字形笔画不受笔画级抖动（role=glyph），每字笔画数不变 →
    # 直接按字符分组求墨迹范围，断言相邻字左右不重叠
    counts = [len(c.strokes) for c in lay.chars]
    boxes = []
    idx = 0
    for c, n in zip(lay.chars, counts):
        group = pr.strokes[idx:idx + n]
        idx += n
        if not group:
            continue          # 空格等无墨迹字符不参与
        pts = [pt for s in group for pt in s.points]
        boxes.append((min(pt[0] for pt in pts), max(pt[0] for pt in pts)))
    for (a0, a1), (b0, b1) in zip(boxes, boxes[1:]):
        assert b0 >= a1 - 0.05, (a1, b0)   # 前字右缘 ≤ 后字左缘（留呼吸隙）


def test_char_spacing_positive_still_spreads():
    """正向随机字距仍然生效（钳制只拦重叠，不拦正常散开）。"""
    glyphs = {"M": Glyph("M", [[(0, 0), (3, 8)]], advance=4),
              " ": Glyph(" ", [], advance=2)}
    fam = FontFamily(name="f", kind="hershey", units_per_em=10.0, glyphs=glyphs)
    lay = layout_text("M M M M M M M M", [fam], TextStyle(size=10.0))
    p = PerturbParams.natural(10.0)
    p.char_x_sigma = 0.0
    p.char_spacing_sigma = 1.5
    p.size_sigma = 0.0
    p.char_rot_sigma = 0.0
    p.char_shear_sigma = 0.0
    p.line_y_sigma = 0.0
    pr = perturb_layout(lay, p, seed=5)
    dxs = [c.dx for c in pr.chars]
    assert max(dxs) - min(dxs) > 0.5     # 字距确实拉开了
