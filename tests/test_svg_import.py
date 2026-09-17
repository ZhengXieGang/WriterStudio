"""SVG 曲线离散（自适应采样 + Bernstein 直接求值）回归测试。

守护两类历史问题：
* 二次曲线控制点属性名是 ``control``（不是 ``control1``），取错会把
  Q/T 曲线静默退化成直线；
* 贝塞尔逐点走 svgelements 通用求值太慢（27µs/点），直接求值后需与
  其保持同 t 逐点一致。
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("svgelements",
                    reason="svgelements 不可用时跳过 SVG 导入测试")

from writerstudio.content.svg_import import import_svg


def _write(tmp_path, body: str):
    p = tmp_path / "t.svg"
    p.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" '
        f'height="100mm">{body}</svg>', encoding="utf-8")
    return str(p)


def test_quadratic_curve_not_degenerated(tmp_path):
    """Q 曲线必须真正离散成折线，而不是被当成直线（历史 bug）。"""
    path = _write(tmp_path, '<path d="M 0 50 Q 25 0 50 50 T 100 50"/>')
    res = import_svg(path)
    assert res.strokes
    pts = res.strokes[0].points
    assert len(pts) >= 10, f"Q/T 曲线点数过少: {len(pts)}"
    # 起伏存在：y 不全相等（控制点把曲线拉向上方，Y 翻转后在下方）
    ys = [p[1] for p in pts]
    assert max(ys) - min(ys) > 1.0


def test_collinear_cubic_fast_path(tmp_path):
    """控制点贴弦的伪曲线应退化为直线（端点直连）。"""
    path = _write(tmp_path, '<path d="M 0 10 C 5 10, 15 10, 20 10"/>')
    res = import_svg(path)
    pts = res.strokes[0].points
    ys = {round(p[1], 6) for p in pts}
    assert len(ys) == 1                       # 全部在同一条水平线上


def test_circle_radius_uniform(tmp_path):
    path = _write(tmp_path, '<circle cx="50" cy="50" r="40"/>')
    res = import_svg(path)
    pts = [p for s in res.strokes for p in s.points]
    cx, cy = res.width / 2, res.height / 2
    ds = [math.hypot(p[0] - cx, p[1] - cy) for p in pts]
    assert max(ds) - min(ds) < 0.05           # 半径波动 ≤0.05mm


def test_cubic_points_on_true_curve(tmp_path):
    """离散点必须落在真实贝塞尔曲线上（容差 ≤0.1 用户单位）。"""
    path = _write(tmp_path, '<path d="M 0 0 C 5 15, 15 15, 20 0"/>')
    res = import_svg(path)
    from svgelements import CubicBezier
    seg = CubicBezier((0, 0), (5, 15), (15, 15), (20, 0))
    dense = [(float(q[0]), float(q[1])) for q in
             (seg.point(i / 400) for i in range(401))]
    raw = res.strokes[0].points
    box_w = max(p[0] for p in raw) - min(p[0] for p in raw)
    scale = box_w / 20.0                      # mm / 用户单位
    x_min = min(q[0] for q in dense)
    y_min = min(q[1] for q in dense)
    worst = 0.0
    for p in raw:
        ux = p[0] / scale + x_min             # 逆归一化回用户单位
        uy = (res.height - p[1]) / scale + y_min
        d = min(math.hypot(ux - q[0], uy - q[1]) for q in dense)
        worst = max(worst, d)
    assert worst < 0.1, f"离散点偏离真实曲线 {worst:.3f}px"


def test_mixed_path_single_stroke(tmp_path):
    """M+C+L 连续路径应保持为一条笔画（直线段不再打断连续性）。"""
    path = _write(tmp_path, '<path d="M 0 0 C 2 8, 8 8, 10 0 L 30 0"/>')
    res = import_svg(path)
    assert len(res.strokes) == 1
    assert len(res.strokes[0].points) >= 5
