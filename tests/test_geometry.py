"""核心几何与变换测试（纯 Python，无需 Qt）。"""

from __future__ import annotations


import pytest

from writerstudio.core.geometry import AffineTransform, BBox


def approx(a, b, tol=1e-9):
    return abs(a - b) < tol


def test_identity():
    t = AffineTransform.identity()
    assert t.apply((3.0, 4.0)) == (3.0, 4.0)


def test_translate():
    t = AffineTransform.translate(10.0, -5.0)
    assert t.apply((1.0, 2.0)) == (11.0, -3.0)


def test_scale():
    t = AffineTransform.scale(2.0, 3.0)
    assert t.apply((1.0, 1.0)) == (2.0, 3.0)


def test_rotate_90_ccw():
    t = AffineTransform.rotate(90.0)
    x, y = t.apply((1.0, 0.0))
    assert approx(x, 0.0) and approx(y, 1.0)


def test_rotate_about_point_keeps_origin():
    t = AffineTransform.rotate_about(37.0, (5.0, 7.0))
    x, y = t.apply((5.0, 7.0))
    assert approx(x, 5.0) and approx(y, 7.0)


def test_scale_about_keeps_origin():
    t = AffineTransform.scale_about(3.0, 0.5, (2.0, 2.0))
    x, y = t.apply((2.0, 2.0))
    assert approx(x, 2.0) and approx(y, 2.0)
    # 相对不动点的距离按比例缩放
    x, y = t.apply((4.0, 4.0))
    assert approx(x, 2.0 + 2.0 * 3.0) and approx(y, 2.0 + 2.0 * 0.5)


def test_invert_roundtrip():
    t = AffineTransform.translate(10.0, 20.0) @ AffineTransform.rotate(33.0) @ AffineTransform.scale(2.0, 4.0)
    p = (3.3, -7.7)
    q = t.apply(p)
    r = t.invert().apply(q)
    assert approx(r[0], p[0], 1e-9) and approx(r[1], p[1], 1e-9)


def test_matmul_order():
    # (A @ B) 表示先 B 后 A
    a = AffineTransform.translate(10.0, 0.0)
    b = AffineTransform.scale(2.0, 2.0)
    t = a @ b
    assert t.apply((1.0, 0.0)) == (12.0, 0.0)  # 先 ×2 再 +10


def test_then_order():
    a = AffineTransform.translate(10.0, 0.0)
    b = AffineTransform.scale(2.0, 2.0)
    assert (a.then(b)).apply((1.0, 0.0)) == (22.0, 0.0)  # 先 +10 再 ×2


def test_invert_singular_raises():
    with pytest.raises(ValueError):
        AffineTransform.scale(0.0, 1.0).invert()


def test_as_tuple_matches_qt_convention():
    t = AffineTransform(1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    assert t.as_tuple() == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)


def test_bbox_empty():
    b = BBox()
    assert b.is_empty
    assert b.width == 0.0 and b.height == 0.0
    import math
    assert not any(math.isnan(v) for v in b.center)   # 空盒 center 不能是 nan
    assert b.center == (0.0, 0.0)


def test_affine_from_sequence_validates_length():
    with pytest.raises(ValueError):
        AffineTransform.from_sequence([1, 0, 0, 1])   # 4 个分量：静默错位是事故
    t = AffineTransform.from_sequence([1, 0, 0, 1, 5, 6])
    assert (t.e, t.f) == (5.0, 6.0)


def test_bbox_from_points():
    b = BBox.from_points([(0.0, 0.0), (2.0, 5.0), (-1.0, 3.0)])
    assert b.as_tuple() == (-1.0, 0.0, 2.0, 5.0)
    assert approx(b.width, 3.0) and approx(b.height, 5.0)
    assert b.center == (0.5, 2.5)


def test_bbox_union():
    a = BBox(0.0, 0.0, 1.0, 1.0)
    b = BBox(2.0, 3.0, 4.0, 5.0)
    u = a.union(b)
    assert u.as_tuple() == (0.0, 0.0, 4.0, 5.0)
