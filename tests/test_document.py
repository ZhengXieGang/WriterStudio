"""笔画与文档模型测试（纯 Python）。"""

from __future__ import annotations



from writerstudio.core.document import (
    Document,
    PageSpec,
    SourceSpec,
    make_static_object,
)
from writerstudio.core.geometry import AffineTransform
from writerstudio.core.sample import demo_document, make_rect
from writerstudio.core.strokes import Stroke


def test_stroke_length_open():
    s = Stroke([(0.0, 0.0), (3.0, 0.0), (3.0, 4.0)])
    assert abs(s.length() - 7.0) < 1e-9


def test_stroke_length_closed():
    s = Stroke([(0.0, 0.0), (3.0, 0.0), (3.0, 4.0)], closed=True)
    assert abs(s.length() - 12.0) < 1e-9


def test_stroke_bbox():
    s = Stroke([(0.0, 0.0), (3.0, 4.0)])
    b = s.bbox()
    assert b.as_tuple() == (0.0, 0.0, 3.0, 4.0)


def test_stroke_transformed():
    s = Stroke([(1.0, 1.0)])
    t = AffineTransform.translate(10.0, 20.0)
    out = s.transformed(t)
    assert out.points == [(11.0, 21.0)]
    # 原笔画不被修改
    assert s.points == [(1.0, 1.0)]


def test_object_world_bbox_uses_transform():
    obj = make_static_object([make_rect(0.0, 0.0, 10.0, 5.0)],
                             transform=AffineTransform.translate(100.0, 200.0))
    b = obj.world_bbox()
    assert b.as_tuple() == (100.0, 200.0, 110.0, 205.0)
    # 本地包围盒保持原样
    assert obj.local_bbox().as_tuple() == (0.0, 0.0, 10.0, 5.0)


def test_object_translate_is_composed_not_baked():
    obj = make_static_object([make_rect(0.0, 0.0, 10.0, 10.0)])
    obj.translate(5.0, 5.0)
    assert obj.local_bbox().as_tuple() == (0.0, 0.0, 10.0, 10.0)
    assert obj.world_bbox().as_tuple() == (5.0, 5.0, 15.0, 15.0)


def test_object_scale_about_local():
    obj = make_static_object([make_rect(0.0, 0.0, 10.0, 10.0)])
    obj.scale_about_local(2.0, 2.0, (0.0, 0.0))
    assert obj.world_bbox().as_tuple() == (0.0, 0.0, 20.0, 20.0)


def test_object_rotate_about_local_center():
    obj = make_static_object([make_rect(-5.0, -5.0, 10.0, 10.0)])
    obj.rotate_about_local(90.0, (0.0, 0.0))
    b = obj.world_bbox()
    assert abs(b.x0 - (-5.0)) < 1e-9 and abs(b.y1 - 5.0) < 1e-9


def test_document_add_remove_find():
    doc = Document()
    a = make_static_object([make_rect(0, 0, 1, 1)])
    b = make_static_object([make_rect(0, 0, 2, 2)])
    doc.add(a)
    doc.add(b)
    assert len(doc) == 2
    assert doc.find(a.id) is a
    idx = doc.remove(a)
    assert idx == 0 and len(doc) == 1
    assert doc.find(a.id) is None


def test_document_move_z():
    doc = Document()
    objs = [make_static_object([make_rect(0, 0, 1, 1)]) for _ in range(3)]
    for o in objs:
        doc.add(o)
    doc.move_z(objs[0], 2)
    assert doc.objects == [objs[1], objs[2], objs[0]]
    doc.move_z(objs[0], -1)
    assert doc.objects == [objs[1], objs[0], objs[2]]


def test_document_move_z_clamped():
    doc = Document()
    a = make_static_object([make_rect(0, 0, 1, 1)])
    doc.add(a)
    assert doc.move_z(a, 5) is False


def test_object_clone_independent():
    obj = make_static_object([make_rect(0, 0, 10, 10)], name="orig")
    clone = obj.clone()
    assert clone.id != obj.id
    clone.translate(100.0, 0.0)
    assert obj.world_bbox().as_tuple()[0] == 0.0
    assert clone.world_bbox().as_tuple()[0] == 100.0
    assert clone.source is not obj.source


def test_document_bbox_includes_page():
    doc = Document(page=PageSpec(width=100.0, height=50.0))
    doc.add(make_static_object([make_rect(10, 10, 5, 5)]))
    assert doc.bbox().as_tuple() == (0.0, 0.0, 100.0, 50.0)


def test_source_spec_clone():
    s = SourceSpec("text", {"text": "hi", "nested": {"a": 1}})
    c = s.clone()
    c.data["text"] = "bye"
    assert s.data["text"] == "hi"


def test_demo_document_has_objects():
    doc = demo_document()
    assert len(doc) >= 4
    assert doc.total_length() > 0.0


def test_page_defaults_a4_landscape():
    p = PageSpec()
    assert p.width == 297.0 and p.height == 210.0


# ==================================================== 笔画版本号（性能）
def test_strokes_rev_bumps_on_assignment_and_touch():
    obj = make_static_object([make_rect(0, 0, 10, 10)])
    r0 = obj.strokes_rev
    obj.local_strokes = [make_rect(0, 0, 5, 5)]
    assert obj.strokes_rev == r0 + 1
    obj.touch()
    assert obj.strokes_rev == r0 + 2


def test_strokes_rev_not_serialized(project_roundtrip=None):
    """版本号是运行时状态，不进序列化、不影响对象克隆。"""
    import copy
    obj = make_static_object([make_rect(0, 0, 10, 10)])
    obj.local_strokes = [make_rect(0, 0, 5, 5)]
    dup = copy.deepcopy(obj)
    assert dup.strokes_rev == obj.strokes_rev   # 随 __dict__ 拷贝即可
